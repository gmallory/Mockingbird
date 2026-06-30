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
