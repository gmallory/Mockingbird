"""Smoke tests for the frontend routes.

Routing swapped in M10: the Dashboard now owns ``/`` and the Live Monitor moved
to ``/monitor`` (it held ``/`` since M1). The monitor assertions below target
the new path; the added Dashboard/Settings cases pin the two new M10 pages and
the nav links that reach them.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz() -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_monitor_page_renders_ws_url() -> None:
    resp = client.get("/monitor")
    assert resp.status_code == 200
    body = resp.text
    # The WS URL must be injected so the browser knows where to connect.
    assert "ws://localhost:3001/ws/voice" in body
    assert "audio-engine.js" in body


def test_monitor_page_injects_gateway_url_and_selector() -> None:
    # The voice selector needs the gateway HTTP base to fetch/POST /voices.
    body = client.get("/monitor").text
    assert "http://localhost:3001" in body
    assert 'id="voice-select"' in body


def test_studio_page_renders() -> None:
    resp = client.get("/studio")
    assert resp.status_code == 200
    body = resp.text
    # Gateway base for the clone upload + the reused capture-worklet recorder.
    assert "http://localhost:3001" in body
    assert "recorder.js" in body


def test_dashboard_page_renders_at_root() -> None:
    # M10: / now serves the Dashboard (overview: voices + call history), not the
    # Monitor. Its client-side panels fetch the gateway, so the base is injected.
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "http://localhost:3001" in body
    assert 'id="voice-list"' in body
    assert 'id="call-list"' in body
    # Quick-start links reach the other pages (so an old / bookmark isn't stranded).
    assert 'href="/monitor"' in body
    assert 'href="/studio"' in body
    # Guard the routing swap: the Monitor's voice selector must NOT be here.
    assert 'id="voice-select"' not in body


def test_settings_page_renders() -> None:
    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.text
    # M10 Settings page: audio devices + quality preset, backed by the gateway's
    # GET/PATCH /api/settings.
    assert "http://localhost:3001" in body
    assert 'id="quality-preset"' in body
    assert 'id="input-device"' in body
    assert 'id="noise-suppression"' in body


def test_nav_links_include_dashboard_and_settings() -> None:
    # The shared nav (base.html) is on every page; the two M10 destinations must
    # be reachable from it.
    body = client.get("/").text
    assert 'href="/">Dashboard' in body
    assert 'href="/settings">Settings' in body
    assert 'href="/monitor">Live Monitor' in body
