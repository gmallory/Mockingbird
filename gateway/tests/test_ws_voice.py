"""End-to-end tests for /ws/voice: the gRPC inference hop and its degradation.

These drive the WebSocket with Starlette's TestClient. For the happy path we
stand up a real in-process gRPC server (a passthrough servicer built on the
gateway's own stubs) so a returned frame proves the gRPC round-trip, not just an
echo. For the degraded path we point the gateway at a dead port and assert it
falls back to passthrough + a `degraded` notice instead of dropping.
"""

import socket
import struct
from collections.abc import Iterator
from concurrent import futures
from contextlib import contextmanager

import grpc
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.proto_gen import audio_pb2, audio_pb2_grpc


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


def test_ws_switch_model_acks() -> None:
    # Control-only: switch_model plumbs the id through and acks (no model yet in M3).
    client = TestClient(app)
    with client.websocket_connect("/ws/voice") as ws:
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
