"""End-to-end test for the /ws/voice echo loop using Starlette's TestClient."""

import struct

from fastapi.testclient import TestClient

from app.main import app


def _pcm_frame(samples: list[int]) -> bytes:
    """Pack a list of Int16 samples into little-endian PCM bytes."""
    return struct.pack(f"<{len(samples)}h", *samples)


def test_healthz() -> None:
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ws_echo_roundtrip() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/voice") as ws:
        # Server greets with `ready` on connect.
        assert ws.receive_json() == {"type": "ready", "latencyMs": 0.0}

        # `start` is acknowledged with another `ready`.
        ws.send_json({"type": "start", "sampleRate": 48000})
        assert ws.receive_json()["type"] == "ready"

        # A binary PCM frame comes back byte-for-byte identical.
        frame = _pcm_frame([0, 1, -1, 32767, -32768, 1234])
        ws.send_bytes(frame)
        assert ws.receive_bytes() == frame

        # ping -> pong.
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_ws_bad_control_message() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/voice") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "not_a_real_type"})
        reply = ws.receive_json()
        assert reply["type"] == "error"
        assert reply["code"] == "bad_message"
