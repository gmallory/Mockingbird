"""Fine-tune controls: GET/PATCH /api/voices/{id} (M10).

Same harness as ``test_voices.py``: a per-test Postgres engine (skips when
unreachable), ``get_session``/``get_current_user`` overridden onto a seeded
owner, and the inference HTTP push (``inference_http.tune_voice``) mocked with
``monkeypatch.setattr`` exactly like the ``clone_voice`` mocks there — so no
live Supabase, token, or inference service is required.

The two storage paths PRODUCT_SPEC §6 / the route docstring describe are both
covered: an HD voice that already owns a ``VoiceModel`` row (PATCH edits it in
place, and ``similarity_score``/``mos_score`` ride along), and a plain
instant-clone voice that has no model row until the first PATCH mints a
companion one keyed by ``VoiceModel.model_path == Voice.voice_id``.
"""

from uuid import uuid4

import httpx
import pytest
from sqlmodel import SQLModel, select

from app.auth.dependencies import get_current_user
from app.db.models import User, Voice, VoiceModel, VoiceModelStatus, VoiceModelType
from app.db.session import create_engine, create_session_factory, get_session
from app.inference import http as inference_http
from app.main import app


async def _ensure_schema(engine) -> None:
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc}")


def _new_user() -> User:
    return User(email=f"owner-{uuid4()}@example.com", display_name="Owner")


async def _seed_user(factory, user: User) -> None:
    async with factory() as session:
        session.add(user)
        await session.commit()


async def _seed_voice(factory, user: User, **overrides) -> Voice:
    async with factory() as session:
        voice = Voice(
            voice_id=overrides.pop("voice_id", f"vid-{uuid4()}"),
            label=overrides.pop("label", "My Voice"),
            language="en",
            user_id=user.id,
        )
        session.add(voice)
        await session.commit()
        await session.refresh(voice)
        return voice


async def _seed_voice_model(factory, user: User, voice: Voice, **fields) -> VoiceModel:
    """An HD-trained ``VoiceModel`` whose ``model_path`` matches ``voice.voice_id``.

    That equality is exactly what ``_tuning_for`` joins on, so the PATCH route
    finds and edits this row instead of minting a new one.
    """
    async with factory() as session:
        model = VoiceModel(
            user_id=user.id,
            voice_id=voice.id,
            name=voice.label,
            type=VoiceModelType.HD,
            status=VoiceModelStatus.READY,
            model_path=voice.voice_id,
            **fields,
        )
        session.add(model)
        await session.commit()
        await session.refresh(model)
        return model


async def _purge_user(factory, user_id) -> None:
    """Delete the user's rows FK-safe order: VoiceModel -> Voice -> User."""
    async with factory() as session:
        for model in (
            (await session.execute(select(VoiceModel).where(VoiceModel.user_id == user_id)))
            .scalars()
            .all()
        ):
            await session.delete(model)
        for voice in (
            (await session.execute(select(Voice).where(Voice.user_id == user_id))).scalars().all()
        ):
            await session.delete(voice)
        user = await session.get(User, user_id)
        if user is not None:
            await session.delete(user)
        await session.commit()


async def _models_for(factory, user_id) -> list[VoiceModel]:
    async with factory() as session:
        return list(
            (await session.execute(select(VoiceModel).where(VoiceModel.user_id == user_id)))
            .scalars()
            .all()
        )


def _override_session(factory):
    async def _dep():
        async with factory() as session:
            yield session

    return _dep


async def test_patch_hd_voice_updates_existing_model(monkeypatch) -> None:
    """An HD voice's existing VoiceModel is edited in place (no new row), unset
    fields are preserved (merge-patch), and its quality scores ride along."""
    engine = create_engine()
    factory = create_session_factory(engine)
    owner = _new_user()
    seeded = False
    pushed: dict = {}
    try:
        await _ensure_schema(engine)
        await _seed_user(factory, owner)
        voice = await _seed_voice(factory, owner, label="HD Voice")
        model = await _seed_voice_model(
            factory, owner, voice, speed_factor=1.5, similarity_score=0.9, mos_score=4.1
        )
        seeded = True

        async def _fake_tune(**kwargs):
            pushed.update(kwargs)
            return {"model_id": kwargs["model_id"]}

        monkeypatch.setattr(inference_http, "tune_voice", _fake_tune)
        app.dependency_overrides[get_session] = _override_session(factory)
        app.dependency_overrides[get_current_user] = lambda: owner

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(f"/api/voices/{voice.id}", json={"pitch_offset": 5.0})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["pitch_offset"] == 5.0
            assert body["speed_factor"] == 1.5  # untouched field preserved
            assert body["breathiness"] == 0.0
            assert body["similarity_score"] == 0.9  # HD scores ride along
            assert body["mos_score"] == 4.1
            assert body["model_id"] == voice.voice_id

        # The best-effort inference push carried the merged values and the
        # streaming model_id (Voice.voice_id), not the registry row UUID.
        assert pushed["model_id"] == voice.voice_id
        assert pushed["pitch_offset"] == 5.0
        assert pushed["speed_factor"] == 1.5

        # No companion row was minted: the same HD model was edited in place.
        models = await _models_for(factory, owner.id)
        assert len(models) == 1
        assert models[0].id == model.id
        assert models[0].pitch_offset == 5.0
        assert models[0].speed_factor == 1.5
    finally:
        app.dependency_overrides.clear()
        try:
            if seeded:
                await _purge_user(factory, owner.id)
        finally:
            await engine.dispose()


async def test_patch_instant_clone_mints_companion_model(monkeypatch) -> None:
    """A never-trained instant-clone voice has no VoiceModel until the first
    PATCH mints a companion row (INSTANT, model_path == voice_id); GET reads it back."""
    engine = create_engine()
    factory = create_session_factory(engine)
    owner = _new_user()
    seeded = False
    pushed: dict = {}
    try:
        await _ensure_schema(engine)
        await _seed_user(factory, owner)
        voice = await _seed_voice(factory, owner, label="Instant Voice")
        seeded = True
        assert await _models_for(factory, owner.id) == []  # nothing to start

        async def _fake_tune(**kwargs):
            pushed.update(kwargs)
            return {"model_id": kwargs["model_id"]}

        monkeypatch.setattr(inference_http, "tune_voice", _fake_tune)
        app.dependency_overrides[get_session] = _override_session(factory)
        app.dependency_overrides[get_current_user] = lambda: owner

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                f"/api/voices/{voice.id}",
                json={"pitch_offset": -3.0, "speed_factor": 0.8, "breathiness": 0.4},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["pitch_offset"] == -3.0
            assert body["speed_factor"] == 0.8
            assert body["breathiness"] == 0.4
            assert body["similarity_score"] is None  # never HD-trained
            assert body["model_id"] == voice.voice_id

            # Read-back through the API returns the persisted values.
            got = await client.get(f"/api/voices/{voice.id}")
            assert got.status_code == 200
            gbody = got.json()
            assert gbody["pitch_offset"] == -3.0
            assert gbody["speed_factor"] == 0.8
            assert gbody["breathiness"] == 0.4

        assert pushed["model_id"] == voice.voice_id

        # Exactly one companion row, minted with the discoverable join key.
        models = await _models_for(factory, owner.id)
        assert len(models) == 1
        minted = models[0]
        assert minted.type == VoiceModelType.INSTANT
        assert minted.model_path == voice.voice_id
        assert minted.voice_id == voice.id
        assert minted.pitch_offset == -3.0
        assert minted.speed_factor == 0.8
        assert minted.breathiness == 0.4
    finally:
        app.dependency_overrides.clear()
        try:
            if seeded:
                await _purge_user(factory, owner.id)
        finally:
            await engine.dispose()


async def test_get_voice_tuning_returns_defaults_for_untuned() -> None:
    """GET on an untuned voice returns identity defaults and never creates a row."""
    engine = create_engine()
    factory = create_session_factory(engine)
    owner = _new_user()
    seeded = False
    try:
        await _ensure_schema(engine)
        await _seed_user(factory, owner)
        voice = await _seed_voice(factory, owner)
        seeded = True

        app.dependency_overrides[get_session] = _override_session(factory)
        app.dependency_overrides[get_current_user] = lambda: owner

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/voices/{voice.id}")
            assert resp.status_code == 200
            body = resp.json()
            assert body["pitch_offset"] == 0.0
            assert body["speed_factor"] == 1.0
            assert body["breathiness"] == 0.0
            assert body["similarity_score"] is None
            assert body["mos_score"] is None
            assert body["model_id"] == voice.voice_id

        # A read must not have minted a settings row.
        assert await _models_for(factory, owner.id) == []
    finally:
        app.dependency_overrides.clear()
        try:
            if seeded:
                await _purge_user(factory, owner.id)
        finally:
            await engine.dispose()


async def test_patch_and_get_reject_unowned_or_missing() -> None:
    """Another user's voice and a nonexistent id both 404 on GET and PATCH."""
    engine = create_engine()
    factory = create_session_factory(engine)
    alice = _new_user()
    bob = _new_user()  # never seeded; only bob.id is read on the 404 path
    seeded = False
    try:
        await _ensure_schema(engine)
        await _seed_user(factory, alice)
        voice = await _seed_voice(factory, alice)
        seeded = True

        app.dependency_overrides[get_session] = _override_session(factory)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Bob cannot see or tune Alice's voice.
            app.dependency_overrides[get_current_user] = lambda: bob
            assert (await client.get(f"/api/voices/{voice.id}")).status_code == 404
            not_owner = await client.patch(f"/api/voices/{voice.id}", json={"pitch_offset": 1.0})
            assert not_owner.status_code == 404

            # A nonexistent voice 404s even for a valid owner.
            app.dependency_overrides[get_current_user] = lambda: alice
            missing = uuid4()
            assert (await client.get(f"/api/voices/{missing}")).status_code == 404
            assert (
                await client.patch(f"/api/voices/{missing}", json={"pitch_offset": 1.0})
            ).status_code == 404

        # A rejected PATCH minted nothing.
        assert await _models_for(factory, alice.id) == []
    finally:
        app.dependency_overrides.clear()
        try:
            if seeded:
                await _purge_user(factory, alice.id)
        finally:
            await engine.dispose()


async def test_tuning_requires_auth() -> None:
    """Without a token both verbs 401 (no override, no Authorization header)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rid = uuid4()
        assert (await client.get(f"/api/voices/{rid}")).status_code == 401
        resp = await client.patch(f"/api/voices/{rid}", json={"pitch_offset": 1.0})
        assert resp.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"pitch_offset": 12.1},
        {"pitch_offset": -12.1},
        {"speed_factor": 2.1},
        {"speed_factor": 0.49},
        {"breathiness": -0.1},
        {"breathiness": 1.1},
    ],
)
async def test_patch_rejects_out_of_range(body) -> None:
    """Out-of-range values 422 at request validation, before the route runs.

    Auth is overridden (so the 422 is the body's, not a 401) but the DB is a
    throwaway ``object()`` — Pydantic rejects the body before the route touches
    the session or mints a row, so no Postgres is needed here.
    """

    async def _override_bad_session():
        yield object()

    app.dependency_overrides[get_session] = _override_bad_session
    app.dependency_overrides[get_current_user] = lambda: _new_user()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(f"/api/voices/{uuid4()}", json=body)
            assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


async def test_patch_persists_even_when_inference_push_fails(monkeypatch) -> None:
    """The DB write is durable and returns 200 even if the inference push errors
    (best-effort side channel — the route logs and moves on)."""
    engine = create_engine()
    factory = create_session_factory(engine)
    owner = _new_user()
    seeded = False
    try:
        await _ensure_schema(engine)
        await _seed_user(factory, owner)
        voice = await _seed_voice(factory, owner)
        seeded = True

        async def _boom(**kwargs):
            raise inference_http.InferenceHTTPError("inference down")

        monkeypatch.setattr(inference_http, "tune_voice", _boom)
        app.dependency_overrides[get_session] = _override_session(factory)
        app.dependency_overrides[get_current_user] = lambda: owner

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(f"/api/voices/{voice.id}", json={"breathiness": 0.5})
            assert resp.status_code == 200, resp.text
            assert resp.json()["breathiness"] == 0.5

        # The push failing did not roll back the row.
        models = await _models_for(factory, owner.id)
        assert len(models) == 1
        assert models[0].breathiness == 0.5
    finally:
        app.dependency_overrides.clear()
        try:
            if seeded:
                await _purge_user(factory, owner.id)
        finally:
            await engine.dispose()
