"""HD training (M9): the VoiceModel migration, the training routes, and the
Celery pipeline that drives them.

Three different postures, in one file (mirrors test_auth.py's multi-section
layout):

- The migration chain runs against a throwaway database created and dropped
  by the test itself (never the shared dev database the other tests use),
  so it can exercise upgrade -> downgrade -> upgrade from a genuinely empty
  schema without colliding with whatever those tests' ``create_all`` has
  already materialized. Skips cleanly when Postgres is unreachable.
- The routes are driven with httpx.ASGITransport, the DB session overridden
  onto a per-test engine (skips when Postgres is down, same as
  test_voices.py/test_calls.py), and ``get_current_user`` overridden to a
  seeded owner — no live Supabase token needed.
- The Celery task runs in-process via ``task_always_eager`` (an autouse
  fixture here, scoped to this file only) so ``train_voice.delay(...)``
  executes synchronously with no live worker or broker; the one blocking
  call it makes to inference is swapped for an ``httpx.MockTransport`` via
  ``app.training.tasks._transport_override``.
"""

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlmodel import SQLModel, select

from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import User, Voice, VoiceModel, VoiceModelStatus, VoiceModelType
from app.db.session import create_engine, create_session_factory, get_session
from app.main import app
from app.training import routes as training_routes
from app.training import tasks as tasks_mod
from app.training.celery_app import celery_app
from app.training.db import normalize_sync_dsn
from app.training.tasks import train_voice


@pytest.fixture(autouse=True)
def _celery_eager(monkeypatch):
    """Run train_voice.delay(...) synchronously, in-process, for every test in
    this file — no live worker or broker needed (see the module docstring).
    """
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", True)
    yield


@pytest.fixture(autouse=True)
def _reset_transport_override():
    yield
    tasks_mod._transport_override = None


# ----- VoiceModel migration: upgrade -> downgrade -> upgrade ----------------


def test_voicemodel_migration_upgrade_downgrade_upgrade(monkeypatch):
    """alembic upgrade head -> downgrade -> upgrade applies cleanly against a
    throwaway database, and the voicemodeltype/voicemodelstatus enum labels
    land as lowercase values (models._enum_column's values_callable), not the
    Python member names.

    Runs against its own CREATE DATABASE/DROP DATABASE-scoped database rather
    than the shared dev database the other tests share via ``create_all`` —
    that database has no ``alembic_version`` bookkeeping (those tests never
    run Alembic itself), so replaying the chain against it from a
    upgrade/downgrade/upgrade angle would collide with tables ``create_all``
    already materialized there.
    """
    from alembic.config import Config

    from alembic import command

    base_url = make_url(normalize_sync_dsn(settings.database_url))
    admin_url = base_url.set(database="postgres")

    try:
        admin_engine = sa_create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc}")

    db_name = f"mockingbird_migration_test_{uuid4().hex[:8]}"
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))

    test_sync_url = base_url.set(database=db_name)
    test_async_url = test_sync_url.set(drivername="postgresql+asyncpg")
    # alembic/env.py re-derives its URL from settings.database_url fresh on
    # every command invocation, so pointing this at the throwaway DB for the
    # duration of the test redirects Alembic there too.
    monkeypatch.setattr(
        settings, "database_url", test_async_url.render_as_string(hide_password=False)
    )

    from pathlib import Path

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    def _tables() -> set[str]:
        engine = sa_create_engine(test_sync_url)
        try:
            return set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

    def _enum_labels(type_name: str) -> list[str]:
        engine = sa_create_engine(test_sync_url)
        try:
            with engine.connect() as conn:
                rows = (
                    conn.execute(
                        text(
                            "SELECT e.enumlabel FROM pg_enum e "
                            "JOIN pg_type t ON t.oid = e.enumtypid "
                            "WHERE t.typname = :name ORDER BY e.enumsortorder"
                        ),
                        {"name": type_name},
                    )
                    .scalars()
                    .all()
                )
            return list(rows)
        finally:
            engine.dispose()

    try:
        command.upgrade(cfg, "head")
        assert "voicemodel" in _tables()
        assert _enum_labels("voicemodeltype") == ["instant", "hd"]
        assert _enum_labels("voicemodelstatus") == ["training", "ready", "failed"]

        command.downgrade(cfg, "a8f4c2d7e1b3")
        assert "voicemodel" not in _tables()

        command.upgrade(cfg, "head")
        assert "voicemodel" in _tables()
        assert _enum_labels("voicemodeltype") == ["instant", "hd"]
    finally:
        admin_engine.dispose()
        admin_engine = sa_create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        admin_engine.dispose()


# ----- shared test env: per-test engine + seeded owner ----------------------


async def _ensure_schema(engine) -> None:
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc}")


def _new_user() -> User:
    return User(email=f"trainer-{uuid4()}@example.com", display_name="Trainer")


class _Env:
    """Per-test DB engine + seeded owner + app overrides, torn down completely."""

    def __init__(self) -> None:
        self.engine = create_engine()
        self.factory = create_session_factory(self.engine)
        self.owner = _new_user()

    async def setup(self) -> None:
        await _ensure_schema(self.engine)
        async with self.factory() as session:
            session.add(self.owner)
            await session.commit()

        async def _override_session():
            async with self.factory() as session:
                yield session

        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[get_current_user] = lambda: self.owner

    async def add(self, obj) -> None:
        async with self.factory() as session:
            session.add(obj)
            await session.commit()
            await session.refresh(obj)

    async def get_model(self, model_id: UUID) -> VoiceModel | None:
        async with self.factory() as session:
            return await session.get(VoiceModel, model_id)

    async def teardown(self) -> None:
        app.dependency_overrides.clear()
        try:
            async with self.engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM voicemodel WHERE user_id = :uid"), {"uid": self.owner.id}
                )
                await conn.execute(
                    text("DELETE FROM voice WHERE user_id = :uid"), {"uid": self.owner.id}
                )
                await conn.execute(
                    text('DELETE FROM "user" WHERE id = :uid'), {"uid": self.owner.id}
                )
        except Exception:  # noqa: BLE001 - teardown after a skip has no schema
            pass
        finally:
            await self.engine.dispose()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _mock_train_hd(model_id: str = "rvc-voice-deadbeef", **fields) -> httpx.MockTransport:
    body = {
        "model_id": model_id,
        "model_size_bytes": 2048,
        "sample_duration_sec": 600.0,
        "sample_count": 60,
        "synthetic": True,
        **fields,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


# ----- POST /api/voices/{id}/train -------------------------------------------


async def test_start_training_happy_path_enqueues_and_completes() -> None:
    env = _Env()
    try:
        await env.setup()
        voice = Voice(voice_id=f"vid-{uuid4()}", label="Alice", language="en", user_id=env.owner.id)
        await env.add(voice)
        tasks_mod._transport_override = _mock_train_hd("rvc-alice-deadbeef")

        async with _client() as client:
            resp = await client.post(
                f"/api/voices/{voice.id}/train",
                files={"clip": ("clip.wav", b"x" * 1000, "audio/wav")},
                data={"name": "Alice HD"},
            )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["voice_id"] == str(voice.id)
        assert body["name"] == "Alice HD"
        # This is the pre-enqueue snapshot the route returns immediately;
        # eager Celery has already run the task inline by this point, so a
        # fresh read (below) shows the completed state.
        assert body["status"] == "training"
        assert body["stage"] == "queued"
        model_id = UUID(body["id"])

        stored = await env.get_model(model_id)
        assert stored.status == VoiceModelStatus.READY
        assert stored.stage == "ready"
        assert stored.progress == 1.0
        assert stored.model_path == "rvc-alice-deadbeef"
        assert stored.model_size_bytes == 2048
        assert stored.training_completed_at is not None

        async with env.factory() as session:
            hd_voice = (
                (await session.execute(select(Voice).where(Voice.voice_id == "rvc-alice-deadbeef")))
                .scalars()
                .one()
            )
            assert hd_voice.label == "Alice HD (HD)"
            assert hd_voice.user_id == env.owner.id
            # The original instant-clone row is untouched (additive, not a mutation).
            original = await session.get(Voice, voice.id)
            assert original.voice_id == voice.voice_id
    finally:
        await env.teardown()


async def test_start_training_defaults_name_to_voice_label() -> None:
    env = _Env()
    try:
        await env.setup()
        voice = Voice(voice_id=f"vid-{uuid4()}", label="Bob", language="en", user_id=env.owner.id)
        await env.add(voice)
        tasks_mod._transport_override = _mock_train_hd("rvc-bob-cafebabe")

        async with _client() as client:
            resp = await client.post(
                f"/api/voices/{voice.id}/train",
                files={"clip": ("clip.wav", b"x" * 1000, "audio/wav")},
            )
        assert resp.status_code == 202, resp.text
        assert resp.json()["name"] == "Bob"
    finally:
        await env.teardown()


async def test_training_routes_require_auth() -> None:
    async with _client() as client:
        resp = await client.post(
            f"/api/voices/{uuid4()}/train",
            files={"clip": ("c.wav", b"x", "audio/wav")},
        )
        assert resp.status_code == 401
        resp2 = await client.get(f"/api/voices/{uuid4()}/train/status")
        assert resp2.status_code == 401


async def test_start_training_404_for_missing_or_unowned_voice() -> None:
    env = _Env()
    stranger = _new_user()
    try:
        await env.setup()
        await env.add(stranger)
        strangers_voice = Voice(
            voice_id=f"vid-{uuid4()}", label="Not yours", language="en", user_id=stranger.id
        )
        await env.add(strangers_voice)

        async with _client() as client:
            resp = await client.post(
                f"/api/voices/{uuid4()}/train",
                files={"clip": ("c.wav", b"x" * 1000, "audio/wav")},
            )
            assert resp.status_code == 404

            resp2 = await client.post(
                f"/api/voices/{strangers_voice.id}/train",
                files={"clip": ("c.wav", b"x" * 1000, "audio/wav")},
            )
            assert resp2.status_code == 404
    finally:
        async with env.factory() as session:
            # The voice row FKs to the user; delete it first or the user
            # delete below violates fk_voice_user_id_user.
            stored_voice = await session.get(Voice, strangers_voice.id)
            if stored_voice is not None:
                await session.delete(stored_voice)
                await session.commit()
            stored = await session.get(User, stranger.id)
            if stored is not None:
                await session.delete(stored)
                await session.commit()
        await env.teardown()


async def test_start_training_rejects_empty_clip() -> None:
    env = _Env()
    try:
        await env.setup()
        voice = Voice(voice_id=f"vid-{uuid4()}", label="V", language="en", user_id=env.owner.id)
        await env.add(voice)

        async with _client() as client:
            resp = await client.post(
                f"/api/voices/{voice.id}/train",
                files={"clip": ("c.wav", b"", "audio/wav")},
            )
        assert resp.status_code == 400
    finally:
        await env.teardown()


async def test_start_training_rejects_oversized_clip(monkeypatch) -> None:
    env = _Env()
    try:
        await env.setup()
        voice = Voice(voice_id=f"vid-{uuid4()}", label="V", language="en", user_id=env.owner.id)
        await env.add(voice)
        monkeypatch.setattr(settings, "max_hd_clip_bytes", 8)

        async with _client() as client:
            resp = await client.post(
                f"/api/voices/{voice.id}/train",
                files={"clip": ("c.wav", b"x" * 9, "audio/wav")},
            )
        assert resp.status_code == 413
    finally:
        await env.teardown()


async def test_start_training_disabled_returns_503(monkeypatch) -> None:
    # No DB needed: the feature-flag gate fires before _owned_voice touches it.
    async def _override_session():
        yield object()

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user] = lambda: _new_user()
    monkeypatch.setattr(settings, "enable_training", False)
    try:
        async with _client() as client:
            resp = await client.post(
                f"/api/voices/{uuid4()}/train",
                files={"clip": ("c.wav", b"x", "audio/wav")},
            )
        assert resp.status_code == 503
        assert "disabled" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


async def test_start_training_enqueue_failure_marks_failed_returns_503(monkeypatch) -> None:
    """A broker/enqueue failure must 503, not crash, and the just-created row
    is marked failed rather than left dangling at ``training`` forever."""
    env = _Env()
    try:
        await env.setup()
        voice = Voice(voice_id=f"vid-{uuid4()}", label="V", language="en", user_id=env.owner.id)
        await env.add(voice)

        class _Boom:
            def delay(self, *args, **kwargs):
                raise RuntimeError("no route to broker")

        monkeypatch.setattr(training_routes, "train_voice", _Boom())

        async with _client() as client:
            resp = await client.post(
                f"/api/voices/{voice.id}/train",
                files={"clip": ("c.wav", b"x" * 1000, "audio/wav")},
            )
        assert resp.status_code == 503

        async with env.factory() as session:
            model = (
                (await session.execute(select(VoiceModel).where(VoiceModel.voice_id == voice.id)))
                .scalars()
                .one()
            )
            assert model.status == VoiceModelStatus.FAILED
            assert model.stage == "failed"
            assert model.error == "training queue unavailable"
    finally:
        await env.teardown()


# ----- GET /api/voices/{id}/train/status -------------------------------------


async def test_training_status_returns_latest_job_for_caller() -> None:
    env = _Env()
    try:
        await env.setup()
        voice = Voice(voice_id=f"vid-{uuid4()}", label="V", language="en", user_id=env.owner.id)
        await env.add(voice)
        model = VoiceModel(
            user_id=env.owner.id,
            voice_id=voice.id,
            name="V (HD)",
            type=VoiceModelType.HD,
            status=VoiceModelStatus.TRAINING,
            stage="training",
            progress=0.55,
        )
        await env.add(model)

        async with _client() as client:
            resp = await client.get(f"/api/voices/{voice.id}/train/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "training"
        assert body["stage"] == "training"
        assert body["progress"] == 0.55
        assert body["error"] is None
        assert body["eta_seconds"] is not None
    finally:
        await env.teardown()


async def test_training_status_404_for_missing_voice_or_non_owner() -> None:
    env = _Env()
    stranger = _new_user()
    try:
        await env.setup()
        await env.add(stranger)
        voice = Voice(voice_id=f"vid-{uuid4()}", label="V", language="en", user_id=env.owner.id)
        await env.add(voice)

        async with _client() as client:
            # A legitimately owned voice with no training job yet.
            resp = await client.get(f"/api/voices/{voice.id}/train/status")
            assert resp.status_code == 404

            # A voice id that doesn't exist at all.
            resp2 = await client.get(f"/api/voices/{uuid4()}/train/status")
            assert resp2.status_code == 404

            # A stranger can't see this voice's training status either.
            app.dependency_overrides[get_current_user] = lambda: stranger
            resp3 = await client.get(f"/api/voices/{voice.id}/train/status")
            assert resp3.status_code == 404
    finally:
        async with env.factory() as session:
            stored = await session.get(User, stranger.id)
            if stored is not None:
                await session.delete(stored)
                await session.commit()
        await env.teardown()


# ----- Celery train_voice task (eager) --------------------------------------


async def test_train_voice_task_completes_and_creates_hd_voice() -> None:
    env = _Env()
    try:
        await env.setup()
        voice = Voice(
            voice_id=f"vid-{uuid4()}", label="Original", language="en", user_id=env.owner.id
        )
        await env.add(voice)
        model = VoiceModel(
            user_id=env.owner.id,
            voice_id=voice.id,
            name="Original (HD)",
            type=VoiceModelType.HD,
            status=VoiceModelStatus.TRAINING,
            stage="queued",
        )
        await env.add(model)

        stages: list[str] = []
        original_set_stage = tasks_mod._set_stage

        def _spy(session, voice_model_id, stage):
            stages.append(stage)
            return original_set_stage(session, voice_model_id, stage)

        with _patched(tasks_mod, "_set_stage", _spy):
            tasks_mod._transport_override = _mock_train_hd("rvc-original-abcd1234")
            clip_path = _stage_temp_clip()
            result = train_voice.delay(str(model.id), clip_path, "Original")
            body = result.get()

        assert body == {"status": "ready", "model_id": "rvc-original-abcd1234"}
        assert stages == [
            "validation",
            "preprocessing",
            "feature_extraction",
            "training",
            "export",
        ]

        stored = await env.get_model(model.id)
        assert stored.status == VoiceModelStatus.READY
        assert stored.progress == 1.0
        assert stored.stage == "ready"
        assert stored.model_path == "rvc-original-abcd1234"
        assert stored.model_size_bytes == 2048
        assert stored.sample_duration_sec == 600.0
        assert stored.sample_count == 60
        assert stored.training_completed_at is not None

        async with env.factory() as session:
            hd_voice = (
                (
                    await session.execute(
                        select(Voice).where(Voice.voice_id == "rvc-original-abcd1234")
                    )
                )
                .scalars()
                .one()
            )
            assert hd_voice.label == "Original (HD)"
            assert hd_voice.user_id == env.owner.id

            original = await session.get(Voice, voice.id)
            assert original.voice_id == voice.voice_id  # untouched by the additive insert
    finally:
        await env.teardown()


async def test_train_voice_task_marks_failed_on_inference_error() -> None:
    env = _Env()
    try:
        await env.setup()
        voice = Voice(voice_id=f"vid-{uuid4()}", label="V", language="en", user_id=env.owner.id)
        await env.add(voice)
        model = VoiceModel(
            user_id=env.owner.id,
            voice_id=voice.id,
            name="V (HD)",
            type=VoiceModelType.HD,
            status=VoiceModelStatus.TRAINING,
            stage="queued",
        )
        await env.add(model)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="inference blew up")

        tasks_mod._transport_override = httpx.MockTransport(handler)
        clip_path = _stage_temp_clip()

        result = train_voice.delay(str(model.id), clip_path, "V")
        body = result.get()
        assert body["status"] == "failed"

        stored = await env.get_model(model.id)
        assert stored.status == VoiceModelStatus.FAILED
        assert stored.stage == "failed"
        assert stored.error  # non-empty, surfaces the inference failure
        assert stored.model_path == ""  # never touched
    finally:
        await env.teardown()


async def test_train_voice_task_voice_id_collision_rolls_back() -> None:
    """Inference minting a model id that collides with an existing
    Voice.voice_id must roll back atomically: the VoiceModel row lands
    failed, and the colliding Voice row is not duplicated."""
    env = _Env()
    other_owner = _new_user()
    try:
        await env.setup()
        await env.add(other_owner)
        voice = Voice(
            voice_id=f"vid-{uuid4()}", label="Original", language="en", user_id=env.owner.id
        )
        await env.add(voice)

        colliding_voice_id = f"rvc-collide-{uuid4().hex[:8]}"
        colliding = Voice(
            voice_id=colliding_voice_id,
            label="Someone else's",
            language="en",
            user_id=other_owner.id,
        )
        await env.add(colliding)

        model = VoiceModel(
            user_id=env.owner.id,
            voice_id=voice.id,
            name="Original (HD)",
            type=VoiceModelType.HD,
            status=VoiceModelStatus.TRAINING,
            stage="queued",
        )
        await env.add(model)

        tasks_mod._transport_override = _mock_train_hd(colliding_voice_id)
        clip_path = _stage_temp_clip()

        result = train_voice.delay(str(model.id), clip_path, "Original")
        body = result.get()
        assert body["status"] == "failed"

        stored = await env.get_model(model.id)
        assert stored.status == VoiceModelStatus.FAILED
        assert stored.stage == "failed"
        assert stored.error

        async with env.factory() as session:
            rows = (
                (await session.execute(select(Voice).where(Voice.voice_id == colliding_voice_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1, "the task's additive insert must not have survived the rollback"
            assert rows[0].user_id == other_owner.id
    finally:
        async with env.factory() as session:
            # The colliding voice row FKs to other_owner; delete it first or
            # the user delete below violates fk_voice_user_id_user.
            stored_voice = await session.get(Voice, colliding.id)
            if stored_voice is not None:
                await session.delete(stored_voice)
                await session.commit()
            stored = await session.get(User, other_owner.id)
            if stored is not None:
                await session.delete(stored)
                await session.commit()
        await env.teardown()


# ----- small local helpers ---------------------------------------------------


class _patched:
    """Minimal monkeypatch-a-module-attribute context manager for use inside
    a test body without threading pytest's ``monkeypatch`` fixture through
    every helper (this test file's ``_celery_eager``/``_reset_transport_
    override`` autouse fixtures already own the file-scoped monkeypatching)."""

    def __init__(self, obj, name: str, value) -> None:
        self._obj = obj
        self._name = name
        self._value = value
        self._original = getattr(obj, name)

    def __enter__(self) -> None:
        setattr(self._obj, self._name, self._value)

    def __exit__(self, *exc) -> None:
        setattr(self._obj, self._name, self._original)


def _stage_temp_clip(data: bytes = b"clip-bytes") -> str:
    import tempfile

    fd, path = tempfile.mkstemp(prefix="test-train-", suffix=".clip")
    with open(fd, "wb") as fh:
        fh.write(data)
    return path
