"""Redis-backed per-user rate limiting (M6b).

Two limits per plan, checked when an authenticated ``/ws/voice`` session opens:

* **Concurrent connections** — how many live sockets one user may hold at once.
* **Monthly minutes** — cumulative streaming time in the current calendar month.

Concurrency is tracked as a Redis **sorted set** per user (``mb:conn:{user_id}``),
one member per live session scored by open time. Admission is a single atomic Lua
step — prune dead slots (older than ``STALE_TTL``, a crash-leak safety net), count,
and add only if under the cap — so two simultaneous connects can't both slip past
the limit. Usage is a per-month ``INCRBYFLOAT`` counter (``mb:usage:{user_id}:{YYYYMM}``)
that resets naturally when the month rolls over.

**Fail-open (owner decision, 2026-07-06):** if Redis is unreachable the limiter
logs and *admits* the session rather than dropping it, matching the gateway's
"the live loop survives an infra outage" posture (degrade-to-passthrough when
inference is down). Limits simply go unenforced for the duration of the outage.
A ``RateLimiter`` built with ``redis=None`` fail-opens on every call, which is how
a bare ``TestClient`` (no lifespan, no Redis) keeps the anonymous demo working.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from redis.exceptions import RedisError

from app.db.models import Plan

log = structlog.get_logger(__name__)

# A live session refreshes nothing after admission, so a slot older than this is
# assumed dead (crashed release) and pruned. Must exceed any real session length;
# well above a plausible call so a legitimately long session is never evicted.
STALE_TTL_S = 6 * 60 * 60
# Monthly counters are keyed by calendar month, so they only need to outlive the
# month; 45 days guarantees the current bucket survives until it is irrelevant.
USAGE_TTL_S = 45 * 24 * 60 * 60

_KEY_PREFIX = "mb"

# Atomic admission: prune stale members, count survivors, add this session only if
# under the cap. Returns 1 (admitted) or 0 (at capacity). One round-trip, no race.
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[2]) then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return 1
"""


@dataclass(frozen=True)
class PlanLimit:
    """A plan's caps. ``monthly_minutes=None`` means unlimited usage."""

    max_concurrent: int
    monthly_minutes: float | None


# Values from agents/gateway.agent.md (Rate Limiter section).
PLAN_LIMITS: dict[Plan, PlanLimit] = {
    Plan.FREE: PlanLimit(max_concurrent=1, monthly_minutes=5),
    Plan.PRO: PlanLimit(max_concurrent=3, monthly_minutes=300),
    Plan.ENTERPRISE: PlanLimit(max_concurrent=10, monthly_minutes=None),
}


@dataclass
class AcquireResult:
    """Outcome of an admission check.

    ``ok`` gates the session. ``reason`` (``"concurrent"`` / ``"monthly_minutes"``)
    is surfaced to the client as the WS close reason. ``degraded`` marks a decision
    made while Redis was unreachable (admitted without enforcement).
    """

    ok: bool
    reason: str = ""
    used_minutes: float = 0.0
    degraded: bool = False


def _conn_key(user_id: str) -> str:
    return f"{_KEY_PREFIX}:conn:{user_id}"


def _usage_key(user_id: str, when: datetime | None = None) -> str:
    month = (when or datetime.now(UTC)).strftime("%Y%m")
    return f"{_KEY_PREFIX}:usage:{user_id}:{month}"


class RateLimiter:
    """Per-user concurrency + monthly-usage limits over Redis.

    One instance per request is cheap; it holds only the shared async Redis client
    (or ``None`` when Redis is unconfigured/absent, in which case every op
    fail-opens).
    """

    def __init__(self, redis) -> None:  # noqa: ANN001 - redis.asyncio.Redis | None
        self._redis = redis
        # register_script binds to this client; skip when there is no client.
        self._acquire = redis.register_script(_ACQUIRE_LUA) if redis is not None else None

    async def acquire(self, user_id: str, plan: Plan, session_id: str) -> AcquireResult:
        """Check monthly usage then claim a concurrency slot for ``session_id``.

        Fail-open: any Redis error admits the session (logged), so a Redis outage
        never takes the live loop down. Callers that get ``ok`` must later
        :meth:`release` the same ``session_id`` and :meth:`record_usage`.
        """
        if self._redis is None:
            return AcquireResult(ok=True, degraded=True)

        limit = PLAN_LIMITS.get(plan, PLAN_LIMITS[Plan.FREE])
        try:
            if limit.monthly_minutes is not None:
                used = await self._get_usage(user_id)
                if used >= limit.monthly_minutes:
                    return AcquireResult(ok=False, reason="monthly_minutes", used_minutes=used)

            now = time.time()
            admitted = await self._acquire(
                keys=[_conn_key(user_id)],
                args=[now - STALE_TTL_S, limit.max_concurrent, now, session_id, STALE_TTL_S],
            )
            if not admitted:
                return AcquireResult(ok=False, reason="concurrent")
            return AcquireResult(ok=True)
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning(
                "ratelimit.redis_unavailable", op="acquire", user_id=user_id, error=str(exc)
            )
            return AcquireResult(ok=True, degraded=True)

    async def release(self, user_id: str, session_id: str) -> None:
        """Free the concurrency slot held by ``session_id`` (best-effort)."""
        if self._redis is None:
            return
        try:
            await self._redis.zrem(_conn_key(user_id), session_id)
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning(
                "ratelimit.redis_unavailable", op="release", user_id=user_id, error=str(exc)
            )

    async def record_usage(self, user_id: str, seconds: float) -> None:
        """Add a finished session's duration to this month's usage (best-effort)."""
        if self._redis is None or seconds <= 0:
            return
        minutes = seconds / 60.0
        key = _usage_key(user_id)
        try:
            await self._redis.incrbyfloat(key, minutes)
            await self._redis.expire(key, USAGE_TTL_S)
        except (RedisError, OSError, TimeoutError) as exc:
            log.warning("ratelimit.redis_unavailable", op="record", user_id=user_id, error=str(exc))

    async def _get_usage(self, user_id: str) -> float:
        raw = await self._redis.get(_usage_key(user_id))
        return float(raw) if raw is not None else 0.0
