"""User settings API: GET/PATCH /api/settings (M10).

Same Postgres-or-skip harness as ``test_voices.py``. ``GET`` merges the caller's
``User.settings`` blob over documented defaults; ``PATCH`` merge-patches only
the fields actually present in the body (``exclude_unset``) and persists them on
the ``MutableDict``-backed column. The 401 and empty-body cases need neither a
DB nor auth; the merge and scoping cases seed real users and verify the commit
by re-reading the row in a fresh session.
"""

from uuid import uuid4

import httpx
import pytest
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

from app.auth.dependencies import get_current_user
from app.db.models import User
from app.db.session import create_engine, create_session_factory, get_session
from app.main import app


async def _ensure_schema(engine) -> None:
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc}")


def _new_user(**settings) -> User:
    return User(
        email=f"owner-{uuid4()}@example.com",
        display_name="Owner",
        settings=dict(settings),
    )


async def _seed_user(factory, user: User) -> None:
    async with factory() as session:
        session.add(user)
        await session.commit()


async def _delete_user(factory, user_id) -> None:
    async with factory() as session:
        stored = await session.get(User, user_id)
        if stored is not None:
            await session.delete(stored)
            await session.commit()


async def _stored_settings(factory, user_id) -> dict:
    async with factory() as session:
        user = await session.get(User, user_id)
        return dict(user.settings)


def _override_session(factory):
    async def _dep():
        async with factory() as session:
            yield session

    return _dep


def _current_user_from_db(user_id):
    """Load the caller from the route's own request-scoped session.

    FastAPI caches ``get_session`` within a request, so this returns a user
    attached to the very session the route commits on — mirroring the real
    ``get_current_user`` (a detached, seeded object would not have its
    ``settings`` MutableDict mutations tracked, and the PATCH would silently
    no-op on commit).
    """

    async def _dep(session: AsyncSession = Depends(get_session)) -> User:
        return await session.get(User, user_id)

    return _dep


_DEFAULT_KEYS = {
    "audio_input_device_id",
    "audio_output_device_id",
    "quality_preset",
    "noise_suppression",
    "echo_cancellation",
    "auto_gain_control",
}


async def test_settings_require_auth() -> None:
    """Both verbs 401 without a token (no override, no Authorization header)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/settings")).status_code == 401
        resp = await client.patch("/api/settings", json={"quality_preset": "latency"})
        assert resp.status_code == 401


async def test_get_settings_returns_defaults_merged() -> None:
    """A brand-new account (empty blob) reads back the full documented defaults."""
    app.dependency_overrides[get_current_user] = lambda: _new_user()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/settings")
            assert resp.status_code == 200
            body = resp.json()
            assert set(body) == _DEFAULT_KEYS
            assert body["quality_preset"] == "balanced"
            assert body["noise_suppression"] is True
            assert body["echo_cancellation"] is True
            assert body["auto_gain_control"] is True
            assert body["audio_input_device_id"] is None
            assert body["audio_output_device_id"] is None
    finally:
        app.dependency_overrides.clear()


async def test_patch_empty_body_is_rejected() -> None:
    """A PATCH that sets no field 422s rather than committing a no-op."""

    async def _override_bad_session():
        yield object()

    app.dependency_overrides[get_session] = _override_bad_session
    app.dependency_overrides[get_current_user] = lambda: _new_user()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch("/api/settings", json={})
            assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


async def test_patch_merge_preserves_untouched_keys() -> None:
    """A partial PATCH updates only its own fields and leaves the rest — both the
    stored keys it did not name and the unset ones — alone."""
    engine = create_engine()
    factory = create_session_factory(engine)
    owner = _new_user(quality_preset="quality", noise_suppression=False)
    seeded = False
    try:
        await _ensure_schema(engine)
        await _seed_user(factory, owner)
        seeded = True

        app.dependency_overrides[get_session] = _override_session(factory)
        app.dependency_overrides[get_current_user] = _current_user_from_db(owner.id)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch("/api/settings", json={"echo_cancellation": False})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["quality_preset"] == "quality"  # pre-existing, untouched
            assert body["noise_suppression"] is False  # pre-existing, untouched
            assert body["echo_cancellation"] is False  # patched
            assert body["auto_gain_control"] is True  # still the default

        # Only the union of the seeded + patched keys is actually stored; the
        # merge over defaults is a read-time concern, not persisted.
        stored = await _stored_settings(factory, owner.id)
        assert stored == {
            "quality_preset": "quality",
            "noise_suppression": False,
            "echo_cancellation": False,
        }
    finally:
        app.dependency_overrides.clear()
        try:
            if seeded:
                await _delete_user(factory, owner.id)
        finally:
            await engine.dispose()


async def test_settings_are_scoped_per_user() -> None:
    """One user's read/write never sees or touches another user's row."""
    engine = create_engine()
    factory = create_session_factory(engine)
    alice = _new_user(quality_preset="quality")
    bob = _new_user(quality_preset="balanced")
    seeded = False
    try:
        await _ensure_schema(engine)
        await _seed_user(factory, alice)
        await _seed_user(factory, bob)
        seeded = True

        app.dependency_overrides[get_session] = _override_session(factory)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Each caller reads only their own blob.
            app.dependency_overrides[get_current_user] = _current_user_from_db(alice.id)
            assert (await client.get("/api/settings")).json()["quality_preset"] == "quality"
            app.dependency_overrides[get_current_user] = _current_user_from_db(bob.id)
            assert (await client.get("/api/settings")).json()["quality_preset"] == "balanced"

            # Bob writes; Alice's row must not move.
            resp = await client.patch("/api/settings", json={"quality_preset": "latency"})
            assert resp.status_code == 200

        assert (await _stored_settings(factory, bob.id))["quality_preset"] == "latency"
        assert await _stored_settings(factory, alice.id) == {"quality_preset": "quality"}
    finally:
        app.dependency_overrides.clear()
        try:
            if seeded:
                await _delete_user(factory, alice.id)
                await _delete_user(factory, bob.id)
        finally:
            await engine.dispose()
