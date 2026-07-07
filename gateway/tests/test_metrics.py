"""Prometheus metrics (M7): the /metrics endpoint and WS handler instrumentation.

Metrics live on the process-global default registry, so assertions are deltas
around the action under test, never absolute values — other tests in the run
move the same counters. Reads go through the public ``REGISTRY.get_sample_value``
API rather than child internals.
"""

import socket

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.config import settings
from app.main import app


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


def _free_port() -> int:
    s = socket.socket()
    s.bind(("localhost", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_metrics_endpoint_serves_prometheus_exposition():
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    # Metric families are registered at import; present even before any session.
    assert "mockingbird_gateway_ws_sessions_total" in resp.text
    assert "mockingbird_gateway_ws_first_output_seconds" in resp.text


def test_ws_session_counts_frames_and_outcomes(monkeypatch: pytest.MonkeyPatch):
    # Point at a dead port so the degrade path is deterministic — a dev
    # inference service that happens to be running must not buffer the frame
    # (and hang the receive below) or convert it.
    monkeypatch.setattr(settings, "inference_grpc_url", f"localhost:{_free_port()}")

    client = TestClient(app)
    accepted_before = _sample("mockingbird_gateway_ws_sessions_total", outcome="accepted")
    frames_in_before = _sample("mockingbird_gateway_ws_audio_frames_total", direction="in")
    frames_out_before = _sample("mockingbird_gateway_ws_audio_frames_total", direction="out")
    active_before = _sample("mockingbird_gateway_ws_sessions_active", mode="anonymous")

    # Anonymous echo session; inference is unreachable, so the session degrades
    # to passthrough — the frame still counts once in and once out (the echo).
    with client.websocket_connect("/ws/voice") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"\x01\x00" * 960)
        while True:  # skip the `degraded` notice; stop at the echoed frame
            msg = ws.receive()
            if "bytes" in msg and msg["bytes"] is not None:
                break

    assert (
        _sample("mockingbird_gateway_ws_sessions_total", outcome="accepted") == accepted_before + 1
    )
    assert (
        _sample("mockingbird_gateway_ws_audio_frames_total", direction="in") == frames_in_before + 1
    )
    assert (
        _sample("mockingbird_gateway_ws_audio_frames_total", direction="out")
        >= frames_out_before + 1
    )
    # The session closed, so the active gauge nets back to where it started.
    assert _sample("mockingbird_gateway_ws_sessions_active", mode="anonymous") == active_before
