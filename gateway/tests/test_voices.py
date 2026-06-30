"""Voice registry: model roundtrip + routes.

The model roundtrip runs against real Postgres and skips if unreachable, exactly
like ``test_db.py`` (the migration itself is verified by ``alembic upgrade head``).
The route tests drive the app with httpx.ASGITransport on the test's own event
loop, override the DB session onto a per-test engine, and mock the inference HTTP
call — so no live API key and no inference service are needed.
"""

from uuid import UUID, uuid4

import httpx
import pytest
from sqlmodel import SQLModel, select

from app.db.models import Voice
from app.db.session import create_engine, create_session_factory, get_session
from app.inference import http as inference_http
from app.main import app


async def _ensure_schema(engine) -> None:
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc}")


async def _delete_voice(factory, voice_id) -> None:
    async with factory() as session:
        stored = await session.get(Voice, voice_id)
        if stored is not None:
            await session.delete(stored)
            await session.commit()


async def test_voice_roundtrip() -> None:
    # Own engine bound to this test's loop (see test_db.py for why the shared
    # module-level engine is avoided in async tests).
    engine = create_engine()
    factory = create_session_factory(engine)
    voice_id = None
    try:
        await _ensure_schema(engine)

        async with factory() as session:
            voice = Voice(voice_id=f"vid-{uuid4()}", label="My Voice", language="en")
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
            assert fetched.voice_id.startswith("vid-")
    finally:
        try:
            if voice_id is not None:
                await _delete_voice(factory, voice_id)
        finally:
            await engine.dispose()


async def test_create_and_list_voice(monkeypatch) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    await _ensure_schema(engine)

    async def _override_session():
        async with factory() as session:
            yield session

    async def _fake_clone(**kwargs):
        # Assert the route forwarded the label as the clone name + the clip bytes.
        assert kwargs["name"] == "Alice"
        assert kwargs["clip"] == b"RIFFDATA"
        return {"voice_id": "vid_abc", "name": kwargs["name"], "language": kwargs["language"]}

    app.dependency_overrides[get_session] = _override_session
    monkeypatch.setattr(inference_http, "clone_voice", _fake_clone)

    created_id = None
    try:
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
            created_id = body["id"]

            listed = await client.get("/voices")
            assert listed.status_code == 200
            assert any(v["voice_id"] == "vid_abc" for v in listed.json())
    finally:
        app.dependency_overrides.clear()
        try:
            if created_id is not None:
                await _delete_voice(factory, UUID(created_id))
        finally:
            await engine.dispose()


async def test_create_voice_returns_502_on_inference_failure(monkeypatch) -> None:
    # The clone fails before any DB write, so yield a dummy the route never touches
    # (any access would raise). Keeps this test independent of Postgres.
    async def _override_session():
        yield object()

    async def _boom(**kwargs):
        raise inference_http.InferenceHTTPError("inference down")

    app.dependency_overrides[get_session] = _override_session
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
