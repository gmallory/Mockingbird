"""FastAPI auth dependencies (M6a).

``get_current_claims`` verifies the bearer token; ``get_current_user`` materializes
(and then reuses) a local ``User`` row mirroring the Supabase identity so the rest
of the schema (``Voice.user_id``, later ``CallRecord``) can FK to a real row.
Supabase owns the credential; we own the app-side mirror, created lazily on the
first authenticated request.
"""

import structlog
from fastapi import Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import AuthError, TokenClaims, verify_token
from app.db.models import User
from app.db.session import get_session

log = structlog.get_logger(__name__)


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="missing bearer token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="malformed Authorization header")
    return token


async def get_current_claims(
    authorization: str | None = Header(default=None),
) -> TokenClaims:
    """Extract and verify the ``Authorization: Bearer`` token, or 401."""
    token = _bearer(authorization)
    try:
        return verify_token(token)
    except AuthError as exc:
        # One 401 for every token problem; the reason goes to the log, not the wire.
        log.info("auth.token_rejected", reason=str(exc))
        raise HTTPException(status_code=401, detail="invalid or expired token") from exc


async def get_current_user(
    claims: TokenClaims = Depends(get_current_claims),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Return the local ``User`` mirroring the verified Supabase identity.

    Created on first sight and reused thereafter. Two concurrent first requests for
    the same identity can race the insert, so a unique violation is treated as
    "someone else just created it" and the row is re-fetched.
    """
    user_id = claims.sub  # already a UUID (validated in verify_token)
    user = await session.get(User, user_id)
    if user is not None:
        return user

    email = claims.email or f"{user_id}@users.noreply.supabase"
    display = claims.display_name or (claims.email.split("@")[0] if claims.email else "user")
    user = User(id=user_id, email=email, display_name=display)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        # Email is not unique on the mirror, so the only constraint that can bite
        # is the primary key: a concurrent first request for the same ``sub``
        # inserted the row between our get and commit. Re-fetch and reuse it.
        await session.rollback()
        existing = await session.get(User, user_id)
        if existing is None:
            raise
        return existing
    await session.refresh(user)
    return user
