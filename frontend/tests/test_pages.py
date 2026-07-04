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


def test_monitor_page_injects_gateway_url_and_selector() -> None:
    # The voice selector needs the gateway HTTP base to fetch/POST /voices.
    body = client.get("/").text
    assert "http://localhost:3001" in body
    assert 'id="voice-select"' in body


def test_studio_page_renders() -> None:
    resp = client.get("/studio")
    assert resp.status_code == 200
    body = resp.text
    # Gateway base for the clone upload + the reused capture-worklet recorder.
    assert "http://localhost:3001" in body
    assert "recorder.js" in body
