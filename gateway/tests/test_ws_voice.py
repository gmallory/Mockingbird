"""End-to-end tests for /ws/voice: the gRPC inference hop and its degradation.

These drive the WebSocket with Starlette's TestClient. For the happy path we
stand up a real in-process gRPC server (a passthrough servicer built on the
gateway's own stubs) so a returned frame proves the gRPC round-trip, not just an
echo. For the degraded path we point the gateway at a dead port and assert it
falls back to passthrough + a `degraded` notice instead of dropping.
"""

import socket
import struct
import time
from collections.abc import Iterator
from concurrent import futures
from contextlib import contextmanager
from uuid import uuid4

import grpc
import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import settings
from app.db.models import Plan
from app.main import app
from app.proto_gen import audio_pb2, audio_pb2_grpc
from app.rate_limit import AcquireResult, RateLimiter

SECRET = "test-secret-at-least-32-characters-long-000"


def _mint(sub: str, *, aud: str = "authenticated", exp_delta: int = 3600) -> str:
    now = int(time.time())
    payload = {"sub": sub, "aud": aud, "iat": now, "exp": now + exp_delta}
    return jwt.encode(payload, SECRET, algorithm="HS256")


@pytest.fixture
def _free_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the DB plan lookup on the WS auth path (default everyone to FREE)."""

    async def _plan(_user_id) -> Plan:
        return Plan.FREE

    monkeypatch.setattr("app.websocket.auth.load_user_plan", _plan)


def _pcm_frame(samples: list[int]) -> bytes:
    """Pack a list of Int16 samples into little-endian PCM bytes."""
    return struct.pack(f"<{len(samples)}h", *samples)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("localhost", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _PassthroughServicer(audio_pb2_grpc.VoiceConversionServicer):
    def Convert(self, request_iterator, context):  # noqa: N802 - gRPC method name
        for frame in request_iterator:
            yield audio_pb2.AudioFrame(
                pcm=frame.pcm,
                sample_rate=frame.sample_rate,
                model_id=frame.model_id,
            )


@contextmanager
def _passthrough_inference() -> Iterator[str]:
    """Run a sync gRPC passthrough server in background threads; yield its address."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    audio_pb2_grpc.add_VoiceConversionServicer_to_server(_PassthroughServicer(), server)
    port = server.add_insecure_port("localhost:0")
    server.start()
    try:
        yield f"localhost:{port}"
    finally:
        server.stop(grace=None)


class _BatchServicer(audio_pb2_grpc.VoiceConversionServicer):
    """Utterance-like backend: emits output only in bursts of `every` inputs.

    Models the non-1:1 conversion path — many frames go in silently, then a
    burst comes back — so the gateway's decoupled reader is exercised.
    """

    def __init__(self, every: int) -> None:
        self._every = every

    def Convert(self, request_iterator, context):  # noqa: N802 - gRPC method name
        buf = []
        for frame in request_iterator:
            buf.append(frame)
            if len(buf) >= self._every:
                for f in buf:
                    yield audio_pb2.AudioFrame(
                        pcm=f.pcm, sample_rate=f.sample_rate, model_id=f.model_id
                    )
                buf = []


@contextmanager
def _batch_inference(every: int) -> Iterator[str]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    audio_pb2_grpc.add_VoiceConversionServicer_to_server(_BatchServicer(every), server)
    port = server.add_insecure_port("localhost:0")
    server.start()
    try:
        yield f"localhost:{port}"
    finally:
        server.stop(grace=None)


class _EndAfterFirstServicer(audio_pb2_grpc.VoiceConversionServicer):
    """Reads one frame then ends the Convert stream cleanly (EOF, no output).

    Models inference closing the stream mid-session without an error, so the
    gateway's reader must degrade to passthrough rather than going silent.
    """

    def Convert(self, request_iterator, context):  # noqa: N802 - gRPC method name
        next(request_iterator, None)  # consume one inbound frame, then end
        return
        yield  # unreachable: makes Convert a generator that yields nothing


@contextmanager
def _end_after_first_inference() -> Iterator[str]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    audio_pb2_grpc.add_VoiceConversionServicer_to_server(_EndAfterFirstServicer(), server)
    port = server.add_insecure_port("localhost:0")
    server.start()
    try:
        yield f"localhost:{port}"
    finally:
        server.stop(grace=None)


def test_healthz() -> None:
    # `with` runs the lifespan so the Redis client on app.state exists.
    with TestClient(app) as client:
        resp = client.get("/healthz")
    body = resp.json()
    assert set(body) == {"status", "db", "redis"}
    assert resp.status_code in (200, 503)
    if resp.status_code == 200:
        assert body == {"status": "ok", "db": "ok", "redis": "ok"}


def test_ws_inference_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    with _passthrough_inference() as addr:
        monkeypatch.setattr(settings, "inference_grpc_url", addr)
        client = TestClient(app)
        with client.websocket_connect("/ws/voice") as ws:
            assert ws.receive_json() == {"type": "ready", "latencyMs": 0.0}

            ws.send_json({"type": "start", "sampleRate": 48000})
            assert ws.receive_json()["type"] == "ready"

            # Frame travels gateway -> gRPC -> inference -> back, byte-for-byte.
            frame = _pcm_frame([0, 1, -1, 32767, -32768, 1234])
            ws.send_bytes(frame)
            assert ws.receive_bytes() == frame

            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


def test_ws_forwards_non_1to1_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    # Backend emits nothing until the 3rd frame, then returns the whole burst.
    with _batch_inference(3) as addr:
        monkeypatch.setattr(settings, "inference_grpc_url", addr)
        client = TestClient(app)
        with client.websocket_connect("/ws/voice") as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "start", "sampleRate": 48000})
            assert ws.receive_json()["type"] == "ready"

            frames = [_pcm_frame([i, i, i, i]) for i in range(1, 4)]
            for frame in frames:
                ws.send_bytes(frame)
            # The decoupled reader forwards the burst back in order.
            got = [ws.receive_bytes() for _ in frames]
            assert got == frames


def test_ws_degrades_on_clean_midsession_stream_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inference reads one frame then ends the Convert stream cleanly (EOF, no
    # error). The reader must degrade so the session falls back to echo rather
    # than going silent for the rest of its life.
    with _end_after_first_inference() as addr:
        monkeypatch.setattr(settings, "inference_grpc_url", addr)
        client = TestClient(app)
        with client.websocket_connect("/ws/voice") as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "start", "sampleRate": 48000})
            assert ws.receive_json()["type"] == "ready"

            frame = _pcm_frame([1, 2, 3, 4])
            ws.send_bytes(frame)
            assert ws.receive_json()["type"] == "degraded"

            # Already degraded: subsequent audio is echoed back unchanged.
            ws.send_bytes(frame)
            assert ws.receive_bytes() == frame


def test_ws_degrades_when_inference_down(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing is listening here, so the gRPC call fails fast.
    monkeypatch.setattr(settings, "inference_grpc_url", f"localhost:{_free_port()}")
    client = TestClient(app)
    with client.websocket_connect("/ws/voice") as ws:
        assert ws.receive_json()["type"] == "ready"

        frame = _pcm_frame([5, 6, 7, 8])
        ws.send_bytes(frame)

        # First: a one-time `degraded` notice; then the original audio passed through.
        notice = ws.receive_json()
        assert notice["type"] == "degraded"
        assert ws.receive_bytes() == frame

        # Already degraded: a second frame is passed through with no repeat notice.
        ws.send_bytes(frame)
        assert ws.receive_bytes() == frame


def test_ws_switch_model_acks(monkeypatch: pytest.MonkeyPatch, _free_plan) -> None:
    # Control-only: an *authenticated* switch_model plumbs the id through and acks.
    # (Anonymous sessions are echo-locked — see test_ws_anonymous_is_echo_locked.)
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    token = _mint(str(uuid4()))
    client = TestClient(app)
    with client.websocket_connect(f"/ws/voice?token={token}") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "switch_model", "modelId": "abc-123"})
        assert ws.receive_json() == {"type": "model_loaded", "modelId": "abc-123"}


def test_ws_bad_control_message() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/voice") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "not_a_real_type"})
        reply = ws.receive_json()
        assert reply["type"] == "error"
        assert reply["code"] == "bad_message"


# ----- M6b: socket auth + rate limiting -------------------------------------


def test_ws_anonymous_is_echo_locked() -> None:
    # No token + WS_REQUIRE_AUTH off (default): the session connects but is
    # echo-only — selecting a voice is refused so a demo visitor can't drive
    # conversion with someone else's voice id.
    client = TestClient(app)
    with client.websocket_connect("/ws/voice") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "switch_model", "modelId": "someone-elses-voice"})
        reply = ws.receive_json()
        assert reply["type"] == "error"
        assert reply["code"] == "auth_required"


def test_ws_rejects_tokenless_when_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ws_require_auth", True)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/ws/voice") as ws:
            ws.receive_text()  # server accepted then closed 4001
    assert ei.value.code == 4001


def test_ws_rejects_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # A present-but-bad token is rejected even when auth is optional — never
    # silently downgraded to the anonymous echo demo.
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/ws/voice?token=not-a-jwt") as ws:
            ws.receive_text()
    assert ei.value.code == 4001


def test_ws_authenticated_roundtrip(monkeypatch: pytest.MonkeyPatch, _free_plan) -> None:
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    token = _mint(str(uuid4()))
    with _passthrough_inference() as addr:
        monkeypatch.setattr(settings, "inference_grpc_url", addr)
        client = TestClient(app)
        with client.websocket_connect(f"/ws/voice?token={token}") as ws:
            assert ws.receive_json()["type"] == "ready"
            # Authenticated: a voice can be selected (ack, not the anon refusal).
            ws.send_json({"type": "switch_model", "modelId": "my-voice"})
            assert ws.receive_json() == {"type": "model_loaded", "modelId": "my-voice"}
            frame = _pcm_frame([7, 8, 9, 10])
            ws.send_bytes(frame)
            assert ws.receive_bytes() == frame


def test_ws_authenticated_rate_limited(monkeypatch: pytest.MonkeyPatch, _free_plan) -> None:
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)

    async def _deny(self, user_id, plan, session_id) -> AcquireResult:
        return AcquireResult(ok=False, reason="concurrent")

    monkeypatch.setattr(RateLimiter, "acquire", _deny)
    token = _mint(str(uuid4()))
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(f"/ws/voice?token={token}") as ws:
            ws.receive_text()  # accepted then closed 4029, no `ready`
    assert ei.value.code == 4029


def test_ws_concurrency_enforced_end_to_end(monkeypatch: pytest.MonkeyPatch, _free_plan) -> None:
    # Full path: `with TestClient` runs the lifespan, so app.state.redis is the
    # real client and main.ws_voice builds a Redis-backed RateLimiter (not the
    # fail-open None used elsewhere). FREE = 1 concurrent, so a second live socket
    # for the same user is closed 4029. Skips when Redis is down.
    import redis

    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    sub = str(uuid4())
    token = _mint(sub)
    try:
        with TestClient(app) as client:  # lifespan -> app.state.redis
            with client.websocket_connect(f"/ws/voice?token={token}") as ws1:
                assert ws1.receive_json()["type"] == "ready"  # first holds the slot
                with pytest.raises(WebSocketDisconnect) as ei:
                    with client.websocket_connect(f"/ws/voice?token={token}") as ws2:
                        ws2.receive_text()  # second is over cap -> closed 4029
                assert ei.value.code == 4029
    except redis.exceptions.RedisError as exc:
        pytest.skip(f"Redis not reachable: {exc}")


async def test_load_user_plan_reads_plan() -> None:
    # The one real DB path M6b adds: an existing user's plan drives their limits;
    # an unknown subject (WS hit before any REST call mirrored a row) is FREE.
    # Own loop-bound engine, per the test_db pattern; skips without Postgres.
    from sqlmodel import SQLModel

    from app.db.models import User
    from app.db.session import create_engine, create_session_factory
    from app.websocket.auth import load_user_plan

    engine = create_engine()
    factory = create_session_factory(engine)
    user_id = None
    try:
        try:
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Postgres not reachable: {exc}")

        async with factory() as session:
            user = User(email=f"pro-{uuid4()}@x.com", display_name="Pro", plan=Plan.PRO)
            session.add(user)
            await session.commit()
            await session.refresh(user)
            user_id = user.id

        assert await load_user_plan(user_id, session_factory=factory) is Plan.PRO
        assert await load_user_plan(uuid4(), session_factory=factory) is Plan.FREE
    finally:
        try:
            if user_id is not None:
                async with factory() as session:
                    stored = await session.get(User, user_id)
                    if stored is not None:
                        await session.delete(stored)
                        await session.commit()
        finally:
            await engine.dispose()
