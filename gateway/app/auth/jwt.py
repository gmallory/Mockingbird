"""Verify Supabase-issued access tokens (M6a).

The gateway never mints tokens — Supabase (GoTrue) does. On every authenticated
request we verify the bearer token's signature and claims *offline* against the
project's symmetric JWT secret (HS256), so no network round-trip sits on the
request path. Swapping to Supabase's asymmetric (ES256 + JWKS) keys later is a
change behind this one function; callers only ever see :class:`TokenClaims`.
"""

from uuid import UUID

import jwt
from pydantic import BaseModel, ValidationError

from app.config import settings


class AuthError(Exception):
    """Token missing/invalid/expired, or auth unconfigured. Routes map it to 401."""


class TokenClaims(BaseModel):
    """The slice of a verified Supabase token the app actually uses.

    ``sub`` is typed as ``UUID`` so a token whose subject is not a well-formed
    UUID is rejected at verification time (as a 401) rather than crashing a later
    ``UUID(...)`` conversion; it is also our local ``User.id``.
    """

    sub: UUID
    email: str | None = None
    display_name: str | None = None


def verify_token(token: str) -> TokenClaims:
    """Verify a Supabase access token and return its claims, or raise ``AuthError``."""
    if not settings.supabase_jwt_secret:
        # Server misconfiguration rather than a bad client, but fail closed: with
        # no secret we cannot trust any token. Surfaced as 401 with a clear reason.
        raise AuthError("auth is not configured (SUPABASE_JWT_SECRET unset)")
    try:
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience=settings.supabase_jwt_audience,
            # Require the claims we depend on to be *present*, not just valid when
            # present: PyJWT only checks exp/aud if they exist, so without this a
            # token minted with no exp would never expire.
            options={"require": ["exp", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        # Bad signature, expired, wrong/absent audience, missing required claim,
        # malformed — all one 401 to the client (don't leak which check failed),
        # with detail for the server log.
        raise AuthError(f"invalid token: {exc}") from exc

    # Supabase stashes the signup ``data`` object under ``user_metadata``; pull a
    # display name from the usual keys if present (purely cosmetic, may be absent).
    meta = payload.get("user_metadata") or {}
    display = meta.get("display_name") or meta.get("full_name") or meta.get("name")
    try:
        return TokenClaims(sub=payload["sub"], email=payload.get("email"), display_name=display)
    except ValidationError as exc:
        # e.g. a signature-valid token whose sub is not a UUID.
        raise AuthError(f"malformed claims: {exc}") from exc
