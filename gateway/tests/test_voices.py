"""Voice registry: model roundtrip + per-user routes (M4b, scoped in M6a).

The model roundtrip runs against real Postgres and skips if unreachable, exactly
like ``test_db.py`` (the migration itself is verified by ``alembic upgrade head``).
The route tests drive the app with httpx.ASGITransport, override the DB session
onto a per-test engine, override ``get_current_user`` to a seeded owner (so no live
Supabase or token is needed), and mock the inference HTTP call — so no live API key
and no inference service are required.
"""

from uuid import UUID, uuid4

import httpx
import pytest
from sqlmodel import SQLModel, select

from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import User, Voice
from app.db.session import create_engine, create_session_factory, get_session
from app.inference import http as inference_http
from app.main import app


async def _ensure_schema(engine) -> None:
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc}")


async def _seed_user(factory, user: User) -> None:
    """Insert an owner row so a voice's ``user_id`` FK is satisfiable."""
    async with factory() as session:
        session.add(user)
        await session.commit()


async def _delete_voice(factory, voice_id) -> None:
    async with factory() as session:
        stored = await session.get(Voice, voice_id)
        if stored is not None:
            await session.delete(stored)
            await session.commit()


async def _delete_user(factory, user_id) -> None:
    async with factory() as session:
        stored = await session.get(User, user_id)
        if stored is not None:
            await session.delete(stored)
            await session.commit()


def _new_user() -> User:
    return User(email=f"owner-{uuid4()}@example.com", display_name="Owner")


async def test_voice_roundtrip() -> None:
    # Own engine bound to this test's loop (see test_db.py for why the shared
    # module-level engine is avoided in async tests).
    engine = create_engine()
    factory = create_session_factory(engine)
    owner = _new_user()
    voice_id = None
    seeded = False
    try:
        await _ensure_schema(engine)  # skips (no connection) when Postgres is down
        await _seed_user(factory, owner)
        seeded = True

        async with factory() as session:
            voice = Voice(
                voice_id=f"vid-{uuid4()}", label="My Voice", language="en", user_id=owner.id
            )
            session.add(voice)
            await session.commit()
            await session.refresh(voice)
            voice_id = voice.id

        async with factory() as session:
            fetched = (
                await session.execute(select(Voice).where(Voice.id == voice_id))
            ).scalar_one()
            assert fetched.label == "My Voice"
            assert fetched.language == "en"
            assert fetched.user_id == owner.id
            assert fetched.voice_id.startswith("vid-")
    finally:
        try:
            # Guard on ``seeded`` so a skip (DB down) never reconnects here and
            # turns the Skipped outcome into an OSError failure.
            if seeded:
                if voice_id is not None:
                    await _delete_voice(factory, voice_id)
                await _delete_user(factory, owner.id)
        finally:
            await engine.dispose()


async def test_create_and_list_voice(monkeypatch) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    owner = _new_user()
    created_id = None
    seeded = False
    try:
        await _ensure_schema(engine)
        await _seed_user(factory, owner)
        seeded = True

        async def _override_session():
            async with factory() as session:
                yield session

        async def _fake_clone(**kwargs):
            assert kwargs["name"] == "Alice"
            assert kwargs["clip"] == b"RIFFDATA"
            return {"voice_id": "vid_abc", "name": kwargs["name"], "language": kwargs["language"]}

        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[get_current_user] = lambda: owner
        monkeypatch.setattr(inference_http, "clone_voice", _fake_clone)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/voices",
                files={"clip": ("sample.wav", b"RIFFDATA", "audio/wav")},
                data={"label": "Alice", "language": "en"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["voice_id"] == "vid_abc"
            assert body["label"] == "Alice"
            assert body["user_id"] == str(owner.id)
            created_id = body["id"]

            listed = await client.get("/voices")
            assert listed.status_code == 200
            assert any(v["voice_id"] == "vid_abc" for v in listed.json())
    finally:
        app.dependency_overrides.clear()
        try:
            if seeded:
                if created_id is not None:
                    await _delete_voice(factory, UUID(created_id))
                await _delete_user(factory, owner.id)
        finally:
            await engine.dispose()


async def test_voices_scoped_per_user(monkeypatch) -> None:
    """A voice created by one user is invisible to another."""
    engine = create_engine()
    factory = create_session_factory(engine)
    alice = _new_user()
    bob = _new_user()
    created_id = None
    seeded = False
    try:
        await _ensure_schema(engine)
        await _seed_user(factory, alice)
        await _seed_user(factory, bob)
        seeded = True

        async def _override_session():
            async with factory() as session:
                yield session

        async def _fake_clone(**kwargs):
            return {"voice_id": f"vid-{uuid4()}", "name": kwargs["name"], "language": "en"}

        app.dependency_overrides[get_session] = _override_session
        monkeypatch.setattr(inference_http, "clone_voice", _fake_clone)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Alice creates a voice.
            app.dependency_overrides[get_current_user] = lambda: alice
            resp = await client.post(
                "/voices",
                files={"clip": ("s.wav", b"RIFFDATA", "audio/wav")},
                data={"label": "AliceVoice", "language": "en"},
            )
            assert resp.status_code == 200
            created_id = resp.json()["id"]

            # Bob sees an empty registry (none of Alice's rows).
            app.dependency_overrides[get_current_user] = lambda: bob
            bob_list = await client.get("/voices")
            assert bob_list.status_code == 200
            assert all(v["id"] != created_id for v in bob_list.json())

            # Alice still sees hers.
            app.dependency_overrides[get_current_user] = lambda: alice
            alice_list = await client.get("/voices")
            assert any(v["id"] == created_id for v in alice_list.json())
    finally:
        app.dependency_overrides.clear()
        try:
            if seeded:
                if created_id is not None:
                    await _delete_voice(factory, UUID(created_id))
                await _delete_user(factory, alice.id)
                await _delete_user(factory, bob.id)
        finally:
            await engine.dispose()


async def test_voices_require_auth() -> None:
    """Without a valid token both routes 401 (no override, no Authorization header)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/voices")).status_code == 401
        resp = await client.post(
            "/voices",
            files={"clip": ("s.wav", b"x", "audio/wav")},
            data={"label": "X", "language": "en"},
        )
        assert resp.status_code == 401


async def test_create_voice_rejects_oversized_clip(monkeypatch) -> None:
    # FastAPI resolves the get_session/get_current_user overrides before the body
    # runs, but _read_clip raises 413 before the route touches the session or
    # ``user.id``, so a bare object() session and a throwaway user are fine here.
    async def _override_session():
        yield object()

    monkeypatch.setattr(settings, "max_clip_bytes", 8)
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user] = lambda: _new_user()

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/voices",
                files={"clip": ("sample.wav", b"x" * 9, "audio/wav")},
                data={"label": "Bob", "language": "en"},
            )
        assert resp.status_code == 413
    finally:
        app.dependency_overrides.clear()


async def test_create_voice_rejects_duplicate_voice_id(monkeypatch) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    owner = _new_user()
    created_ids = []
    seeded = False
    try:
        await _ensure_schema(engine)
        await _seed_user(factory, owner)
        seeded = True

        async def _override_session():
            async with factory() as session:
                yield session

        async def _fake_clone(**kwargs):
            # Both clone calls mint the same voice_id, simulating a retried/
            # double-submitted request.
            return {"voice_id": "vid_dup", "name": kwargs["name"], "language": kwargs["language"]}

        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[get_current_user] = lambda: owner
        monkeypatch.setattr(inference_http, "clone_voice", _fake_clone)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/voices",
                files={"clip": ("sample.wav", b"RIFFDATA", "audio/wav")},
                data={"label": "Alice", "language": "en"},
            )
            assert first.status_code == 200
            created_ids.append(UUID(first.json()["id"]))

            second = await client.post(
                "/voices",
                files={"clip": ("sample.wav", b"RIFFDATA", "audio/wav")},
                data={"label": "Alice Again", "language": "en"},
            )
            assert second.status_code == 409
    finally:
        app.dependency_overrides.clear()
        try:
            if seeded:
                for voice_id in created_ids:
                    await _delete_voice(factory, voice_id)
                await _delete_user(factory, owner.id)
        finally:
            await engine.dispose()


async def test_create_voice_returns_502_on_inference_failure(monkeypatch) -> None:
    # The clone fails before any DB write, so yield a dummy the route never touches.
    async def _override_session():
        yield object()

    async def _boom(**kwargs):
        raise inference_http.InferenceHTTPError("inference down")

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user] = lambda: _new_user()
    monkeypatch.setattr(inference_http, "clone_voice", _boom)

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/voices",
                files={"clip": ("sample.wav", b"x", "audio/wav")},
                data={"label": "Bob", "language": "en"},
            )
        assert resp.status_code == 502
    finally:
        app.dependency_overrides.clear()
