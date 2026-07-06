"""RateLimiter tests (M6b).

Concurrency + monthly-usage enforcement runs against a real Redis (the atomic
admission is a Lua script, so a hand-rolled in-memory fake would prove nothing);
these skip cleanly when Redis is down, matching ``test_db.py`` / the Postgres
tests. Each test uses a fresh random ``user_id`` and deletes its keys afterward so
runs don't interfere. The fail-open path needs no Redis and always runs.
"""

from uuid import uuid4

import pytest
import redis.asyncio as aioredis

from app.config import settings
from app.db.models import Plan
from app.rate_limit.limiter import (
    RateLimiter,
    _conn_key,
    _usage_key,
)


@pytest.fixture
async def redis_client():
    client = aioredis.from_url(settings.redis_url)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis not reachable: {exc}")
    yield client
    await client.aclose()


async def _cleanup(client, user_id: str) -> None:
    await client.delete(_conn_key(user_id), _usage_key(user_id))


# ----- concurrency ----------------------------------------------------------


async def test_concurrency_denies_over_cap(redis_client) -> None:
    limiter = RateLimiter(redis_client)
    user = str(uuid4())
    try:
        first = await limiter.acquire(user, Plan.FREE, "s1")  # FREE = 1 concurrent
        assert first.ok
        second = await limiter.acquire(user, Plan.FREE, "s2")
        assert not second.ok
        assert second.reason == "concurrent"
    finally:
        await _cleanup(redis_client, user)


async def test_release_frees_a_slot(redis_client) -> None:
    limiter = RateLimiter(redis_client)
    user = str(uuid4())
    try:
        assert (await limiter.acquire(user, Plan.FREE, "s1")).ok
        assert not (await limiter.acquire(user, Plan.FREE, "s2")).ok
        await limiter.release(user, "s1")
        # Slot freed: the next session is admitted.
        assert (await limiter.acquire(user, Plan.FREE, "s2")).ok
    finally:
        await _cleanup(redis_client, user)


async def test_pro_allows_more_concurrent(redis_client) -> None:
    limiter = RateLimiter(redis_client)
    user = str(uuid4())
    try:
        for i in range(3):  # PRO = 3 concurrent
            assert (await limiter.acquire(user, Plan.PRO, f"s{i}")).ok
        assert not (await limiter.acquire(user, Plan.PRO, "s3")).ok
    finally:
        await _cleanup(redis_client, user)


# ----- monthly usage --------------------------------------------------------


async def test_monthly_minutes_block_when_exhausted(redis_client) -> None:
    limiter = RateLimiter(redis_client)
    user = str(uuid4())
    try:
        # FREE = 5 min/month. Bank 6 minutes (360s), then admission is denied.
        await limiter.record_usage(user, 360)
        result = await limiter.acquire(user, Plan.FREE, "s1")
        assert not result.ok
        assert result.reason == "monthly_minutes"
        assert result.used_minutes >= 5
    finally:
        await _cleanup(redis_client, user)


async def test_usage_accumulates_across_sessions(redis_client) -> None:
    limiter = RateLimiter(redis_client)
    user = str(uuid4())
    try:
        await limiter.record_usage(user, 120)  # 2 min
        await limiter.record_usage(user, 120)  # +2 min = 4, still under 5
        assert (await limiter.acquire(user, Plan.FREE, "s1")).ok
        await limiter.release(user, "s1")
        await limiter.record_usage(user, 120)  # +2 min = 6, now over
        assert not (await limiter.acquire(user, Plan.FREE, "s2")).ok
    finally:
        await _cleanup(redis_client, user)


async def test_enterprise_usage_is_unlimited(redis_client) -> None:
    limiter = RateLimiter(redis_client)
    user = str(uuid4())
    try:
        await limiter.record_usage(user, 100 * 60 * 60)  # 100 hours
        # Unlimited minutes: only concurrency can gate an ENTERPRISE session.
        assert (await limiter.acquire(user, Plan.ENTERPRISE, "s1")).ok
    finally:
        await _cleanup(redis_client, user)


# ----- fail-open (no Redis needed) ------------------------------------------


async def test_fail_open_without_redis() -> None:
    # A limiter with no client (bare TestClient / Redis down) admits every
    # session, flagged degraded, so the live loop survives an outage.
    limiter = RateLimiter(None)
    result = await limiter.acquire(str(uuid4()), Plan.FREE, "s1")
    assert result.ok
    assert result.degraded
    # release / record are no-ops, not errors.
    await limiter.release("u", "s1")
    await limiter.record_usage("u", 60)


async def test_fail_open_on_redis_error() -> None:
    class _BrokenRedis:
        def register_script(self, _script):
            async def _call(*args, **kwargs):
                raise ConnectionError("redis down")

            return _call

        async def get(self, *args, **kwargs):
            raise ConnectionError("redis down")

    limiter = RateLimiter(_BrokenRedis())
    result = await limiter.acquire(str(uuid4()), Plan.FREE, "s1")
    assert result.ok
    assert result.degraded
