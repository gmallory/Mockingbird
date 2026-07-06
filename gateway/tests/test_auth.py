"""Auth (M6a): token verification, the Supabase proxy client, and the routes.

All offline. Token verification is exercised by minting our own HS256 tokens with
a known secret (Supabase signs the same way), the GoTrue client by an
``httpx.MockTransport`` standing in for Supabase, and the routes by driving the
app with ``httpx.ASGITransport``. Only ``/auth/me``'s upsert touches Postgres and
skips cleanly when it is unreachable, matching ``test_db.py`` / ``test_voices.py``.
"""

import json
import time
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from sqlmodel import SQLModel

from app.auth import supabase
from app.auth.dependencies import get_current_user  # noqa: F401 (import parity/readability)
from app.auth.jwt import AuthError, verify_token
from app.auth.supabase import SupabaseAuthError
from app.config import settings
from app.db.models import User
from app.db.session import create_engine, create_session_factory, get_session
from app.main import app

SECRET = "test-secret-at-least-32-characters-long-000"


def _mint(
    secret: str,
    *,
    sub: str,
    email: str | None = None,
    aud: str = "authenticated",
    exp_delta: int = 3600,
    **extra,
) -> str:
    now = int(time.time())
    payload: dict = {"sub": sub, "aud": aud, "iat": now, "exp": now + exp_delta}
    if email is not None:
        payload["email"] = email
    payload.update(extra)
    return jwt.encode(payload, secret, algorithm="HS256")


# ----- verify_token ---------------------------------------------------------


def test_verify_token_accepts_valid(monkeypatch) -> None:
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    sub = str(uuid4())
    token = _mint(SECRET, sub=sub, email="a@b.c", user_metadata={"display_name": "Al"})
    claims = verify_token(token)
    assert str(claims.sub) == sub
    assert claims.email == "a@b.c"
    assert claims.display_name == "Al"


def test_verify_token_rejects_bad_signature(monkeypatch) -> None:
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    token = _mint("a-totally-different-secret-value-32-chars", sub=str(uuid4()))
    with pytest.raises(AuthError):
        verify_token(token)


def test_verify_token_rejects_expired(monkeypatch) -> None:
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    token = _mint(SECRET, sub=str(uuid4()), exp_delta=-10)
    with pytest.raises(AuthError):
        verify_token(token)


def test_verify_token_rejects_wrong_audience(monkeypatch) -> None:
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    token = _mint(SECRET, sub=str(uuid4()), aud="anon")
    with pytest.raises(AuthError):
        verify_token(token)


def test_verify_token_rejects_missing_sub(monkeypatch) -> None:
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    now = int(time.time())
    token = jwt.encode({"aud": "authenticated", "exp": now + 3600}, SECRET, algorithm="HS256")
    with pytest.raises(AuthError):
        verify_token(token)


def test_verify_token_rejects_non_uuid_sub(monkeypatch) -> None:
    # A fully signature-valid token whose sub is not a UUID must 401, not 500.
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    token = _mint(SECRET, sub="not-a-uuid", email="a@b.c")
    with pytest.raises(AuthError):
        verify_token(token)


def test_verify_token_rejects_missing_exp(monkeypatch) -> None:
    # PyJWT does not require exp by default; verify_token must, so a token minted
    # without an expiry is rejected rather than living forever.
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    token = jwt.encode({"sub": str(uuid4()), "aud": "authenticated"}, SECRET, algorithm="HS256")
    with pytest.raises(AuthError):
        verify_token(token)


def test_verify_token_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "supabase_jwt_secret", "")
    with pytest.raises(AuthError):
        verify_token("anything")


# ----- Supabase (GoTrue) client --------------------------------------------


async def test_supabase_login_success(monkeypatch) -> None:
    monkeypatch.setattr(settings, "supabase_url", "http://supa.test")
    monkeypatch.setattr(settings, "supabase_anon_key", "anon-key")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["grant"] = request.url.params.get("grant_type")
        seen["apikey"] = request.headers.get("apikey")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"access_token": "tok", "user": {"id": "u1"}})

    result = await supabase.login("a@b.c", "pw", transport=httpx.MockTransport(handler))
    assert result["access_token"] == "tok"
    assert seen["path"] == "/auth/v1/token"
    assert seen["grant"] == "password"
    assert seen["apikey"] == "anon-key"
    assert seen["body"] == {"email": "a@b.c", "password": "pw"}


async def test_supabase_signup_forwards_display_name(monkeypatch) -> None:
    monkeypatch.setattr(settings, "supabase_url", "http://supa.test/")  # trailing slash trimmed
    monkeypatch.setattr(settings, "supabase_anon_key", "")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/v1/signup"
        assert "apikey" not in request.headers  # not sent when unconfigured
        assert json.loads(request.content) == {
            "email": "new@x.com",
            "password": "pw",
            "data": {"display_name": "New Person"},
        }
        return httpx.Response(200, json={"access_token": "tok", "user": {"id": "u2"}})

    result = await supabase.signup(
        "new@x.com", "pw", "New Person", transport=httpx.MockTransport(handler)
    )
    assert result["user"]["id"] == "u2"


async def test_supabase_maps_upstream_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "supabase_url", "http://supa.test")
    monkeypatch.setattr(settings, "supabase_anon_key", "")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error_description": "Invalid login credentials"})

    with pytest.raises(SupabaseAuthError) as ei:
        await supabase.login("a@b.c", "wrong", transport=httpx.MockTransport(handler))
    assert ei.value.status_code == 400
    assert "Invalid login" in str(ei.value)


async def test_supabase_unconfigured_url(monkeypatch) -> None:
    monkeypatch.setattr(settings, "supabase_url", "")
    with pytest.raises(SupabaseAuthError) as ei:
        await supabase.login("a@b.c", "pw")
    assert ei.value.status_code == 503


# ----- Routes ---------------------------------------------------------------


async def test_signup_route_proxies(monkeypatch) -> None:
    async def fake_signup(email, password, display_name=None, transport=None):
        assert (email, password, display_name) == ("new@x.com", "pw", "New")
        return {"access_token": "tok", "user": {"id": "u1"}}

    monkeypatch.setattr(supabase, "signup", fake_signup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/auth/signup",
            json={"email": "new@x.com", "password": "pw", "display_name": "New"},
        )
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "tok"


async def test_login_route_maps_bad_credentials(monkeypatch) -> None:
    async def fake_login(email, password, transport=None):
        raise SupabaseAuthError("Invalid login credentials", 400)

    monkeypatch.setattr(supabase, "login", fake_login)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/auth/login", json={"email": "a@b.c", "password": "wrong"})
    assert resp.status_code == 400


async def test_me_requires_token() -> None:
    # No Postgres needed: the missing-token 401 fires before any query runs.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/auth/me")
    assert resp.status_code == 401


async def test_me_rejects_garbage_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


async def test_me_upserts_and_reuses_user(monkeypatch) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    sub = str(uuid4())
    created = False
    try:
        try:
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Postgres not reachable: {exc}")

        monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)

        async def _override_session():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_session] = _override_session
        token = _mint(SECRET, sub=sub, email="me@x.com")

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            first = await client.get("/auth/me")
            assert first.status_code == 200
            created = True
            body = first.json()
            assert body["id"] == sub
            assert body["email"] == "me@x.com"

            # Second call must hit the reuse branch (no duplicate row / no error).
            second = await client.get("/auth/me")
            assert second.status_code == 200
            assert second.json()["id"] == sub
    finally:
        app.dependency_overrides.clear()
        try:
            if created:
                async with factory() as session:
                    stored = await session.get(User, UUID(sub))
                    if stored is not None:
                        await session.delete(stored)
                        await session.commit()
        finally:
            await engine.dispose()
