"""Smoke tests for the frontend routes."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz() -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_monitor_page_renders_ws_url() -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # The WS URL must be injected so the browser knows where to connect.
    assert "ws://localhost:3001/ws/voice" in body
    assert "audio-engine.js" in body
