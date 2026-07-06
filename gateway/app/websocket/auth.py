"""WebSocket auth resolution (M6b).

``/ws/voice`` authenticates from a ``?token=<jwt>`` query param (the browser can't
set headers on a WebSocket, so the query string is the idiomatic carrier). Auth is
**optional by default** so the anonymous echo demo keeps working; set
``WS_REQUIRE_AUTH=true`` to lock the socket down.

.. warning::
   A query-string token lands in HTTP access logs and in any reverse proxy in
   front of the socket, so redact the ``token`` param at those layers (the
   browser WebSocket API offers no header carrier, so this is the tradeoff for
   auth on the socket). This module itself logs only the rejection *reason*,
   never the token.

Three outcomes, resolved *before* the socket is accepted:

* ``authenticated`` — a valid token: the session gets the user's id + plan and is
  rate-limited.
* ``anonymous`` — no token and ``ws_require_auth`` is off: an echo-only demo
  session (the handler forces the model to echo; no conversion, no limits).
* ``rejected`` — a token that fails verification (always), or a missing token when
  ``ws_require_auth`` is on: the handler closes the socket with 4001.

A present-but-invalid token is **never** silently downgraded to anonymous — that
would hide an expired session behind a working-looking echo loop. It is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import structlog

from app.auth.jwt import AuthError, verify_token
from app.config import settings
from app.db.models import Plan, User
from app.db.session import async_session

log = structlog.get_logger(__name__)

WsOutcome = Literal["authenticated", "anonymous", "rejected"]


@dataclass
class WsAuth:
    outcome: WsOutcome
    user_id: str | None = None
    plan: Plan = Plan.FREE
    reason: str = ""

    @property
    def authenticated(self) -> bool:
        return self.outcome == "authenticated"


async def load_user_plan(user_id: UUID, session_factory=async_session) -> Plan:  # noqa: ANN001
    """Read the user's plan for limit selection; default FREE if it can't be read.

    Best-effort and off the per-frame path (one lookup at connect). If Postgres is
    unreachable we fall back to the FREE caps rather than dropping the connection,
    keeping the "session survives an infra outage" posture; an unknown user (token
    valid but no mirror row yet — the WS may be hit before any REST call) is also
    treated as FREE. ``session_factory`` is injectable so tests can bind their own
    loop-scoped engine.
    """
    try:
        async with session_factory() as session:
            user = await session.get(User, user_id)
            return user.plan if user is not None else Plan.FREE
    except Exception as exc:  # noqa: BLE001 - plan lookup degrades, never raises
        log.warning("ws.plan_lookup_failed", user_id=str(user_id), error=str(exc))
        return Plan.FREE


async def resolve_ws_auth(token: str | None) -> WsAuth:
    """Classify a ``/ws/voice`` connection from its token (or absence of one)."""
    if not token:
        if settings.ws_require_auth:
            return WsAuth(outcome="rejected", reason="authentication required")
        return WsAuth(outcome="anonymous")

    try:
        claims = verify_token(token)
    except AuthError as exc:
        # Log the specific reason; the client only learns "unauthorized" via 4001.
        log.info("ws.token_rejected", reason=str(exc))
        return WsAuth(outcome="rejected", reason="invalid or expired token")

    plan = await load_user_plan(claims.sub)
    return WsAuth(outcome="authenticated", user_id=str(claims.sub), plan=plan)
