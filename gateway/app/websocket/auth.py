"""WebSocket auth resolution (M6b).

``/ws/voice`` authenticates from a ``bearer.<jwt>`` entry offered in the
``Sec-WebSocket-Protocol`` handshake header (the browser WebSocket API can't set
arbitrary headers, but it *can* offer subprotocols — and unlike a ``?token=``
query param, the header does not land in the request *target*, so it is absent
from the request-line logging most proxies and access logs do by default; an
operator running header-level logging or APM capture must still redact it).
Auth is
**optional by default** so the anonymous echo demo keeps working; set
``WS_REQUIRE_AUTH=true`` to lock the socket down.

A token sent via the legacy ``?token=`` query param is rejected in ``main.py``
(4001), never read and never silently downgraded to anonymous — that carrier
leaks. This module itself logs only the rejection *reason*, never the token.

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

# The app subprotocol the browser offers (and the server echoes back) on
# /ws/voice; the token rides alongside it as a second `bearer.<jwt>` entry.
WS_SUBPROTOCOL = "mockingbird"
_WS_BEARER_PREFIX = "bearer."


def ws_token_from_subprotocols(header: str | None) -> tuple[str | None, str | None]:
    """Extract ``(token, subprotocol_to_accept)`` from ``Sec-WebSocket-Protocol``.

    The server must echo one *offered* subprotocol back in the handshake or
    browsers fail the connection, so alongside the token this returns which
    subprotocol to accept: ``mockingbird`` when offered, else ``None`` (bare
    non-browser clients that offered nothing). The ``bearer.<jwt>`` entry is
    never echoed — that would reflect the credential into a response header.
    """
    if not header:
        return None, None
    offered = [p.strip() for p in header.split(",") if p.strip()]
    token = next(
        (p.removeprefix(_WS_BEARER_PREFIX) for p in offered if p.startswith(_WS_BEARER_PREFIX)),
        None,
    )
    subprotocol = WS_SUBPROTOCOL if WS_SUBPROTOCOL in offered else None
    return token, subprotocol


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
    if token is None:
        if settings.ws_require_auth:
            return WsAuth(outcome="rejected", reason="authentication required")
        return WsAuth(outcome="anonymous")

    if not token:
        # An offered-but-empty `bearer.` entry is a malformed auth *attempt*,
        # not the absence of one — never downgrade it to anonymous.
        return WsAuth(outcome="rejected", reason="invalid or expired token")

    try:
        claims = verify_token(token)
    except AuthError as exc:
        # Log the specific reason; the client only learns "unauthorized" via 4001.
        log.info("ws.token_rejected", reason=str(exc))
        return WsAuth(outcome="rejected", reason="invalid or expired token")

    plan = await load_user_plan(claims.sub)
    return WsAuth(outcome="authenticated", user_id=str(claims.sub), plan=plan)
