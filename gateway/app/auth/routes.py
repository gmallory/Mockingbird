"""Auth routes (M6a): signup/login proxy to Supabase + whoami.

The browser posts credentials here; the gateway exchanges them with Supabase
(GoTrue) and returns the session (``access_token`` + ``user``). The browser then
sends that token as ``Authorization: Bearer`` on ``/voices``. The gateway stores
no passwords — Supabase owns the credential. ``GET /auth/me`` verifies the token
and returns the mirrored local ``User`` (handy for the frontend to render login
state and gate pages).
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import supabase
from app.auth.dependencies import get_current_user
from app.auth.supabase import SupabaseAuthError
from app.db.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupRequest(BaseModel):
    email: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/signup")
async def signup(body: SignupRequest) -> dict:
    """Register with Supabase; returns the session (or user, if email confirm is on)."""
    try:
        return await supabase.signup(body.email, body.password, body.display_name)
    except SupabaseAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/login")
async def login(body: LoginRequest) -> dict:
    """Exchange email/password for a Supabase session."""
    try:
        return await supabase.login(body.email, body.password)
    except SupabaseAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> User:
    """Return the authenticated user (verifies the token, mirrors the identity)."""
    return user
