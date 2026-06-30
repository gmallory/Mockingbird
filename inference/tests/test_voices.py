"""Clone route tests.

The route forwards an uploaded clip to Cartesia /voices/clone and returns the
minted voice id. Cartesia is mocked with an httpx.MockTransport (injected via the
module's ``_transport`` seam) so no network or API key is needed; the inbound
request is driven through the app with httpx.ASGITransport.
"""

import httpx
from fastapi import FastAPI

from app import voices

app = FastAPI()
app.include_router(voices.router)


def _configure_cartesia(monkeypatch) -> None:
    monkeypatch.setattr(voices.settings, "inference_backend", "cartesia")
    monkeypatch.setattr(voices.settings, "cartesia_api_key", "sk_test")
    monkeypatch.setattr(voices.settings, "cartesia_base_url", "https://api.cartesia.ai")
    monkeypatch.setattr(voices.settings, "cartesia_version", "2026-03-01")


async def test_clone_returns_voice_id(monkeypatch):
    _configure_cartesia(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/voices/clone"
        assert request.headers["authorization"] == "Bearer sk_test"
        assert request.headers["cartesia-version"] == "2026-03-01"
        # multipart carries the name + language fields and the clip bytes
        assert b"My Voice" in request.content
        assert b"RIFFDATA" in request.content
        return httpx.Response(200, json={"id": "vid_123", "name": "My Voice", "language": "en"})

    monkeypatch.setattr(voices, "_transport", httpx.MockTransport(handler))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/voices",
            files={"clip": ("sample.wav", b"RIFFDATA", "audio/wav")},
            data={"name": "My Voice", "language": "en"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"voice_id": "vid_123", "name": "My Voice", "language": "en"}


async def test_clone_rejects_non_cartesia_backend(monkeypatch):
    monkeypatch.setattr(voices.settings, "inference_backend", "passthrough")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/voices",
            files={"clip": ("sample.wav", b"x", "audio/wav")},
            data={"name": "n", "language": "en"},
        )

    assert resp.status_code == 400


async def test_clone_rejects_missing_key(monkeypatch):
    monkeypatch.setattr(voices.settings, "inference_backend", "cartesia")
    monkeypatch.setattr(voices.settings, "cartesia_api_key", "")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/voices",
            files={"clip": ("sample.wav", b"x", "audio/wav")},
            data={"name": "n", "language": "en"},
        )

    assert resp.status_code == 400


async def test_clone_surfaces_cartesia_error(monkeypatch):
    _configure_cartesia(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    monkeypatch.setattr(voices, "_transport", httpx.MockTransport(handler))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/voices",
            files={"clip": ("sample.wav", b"x", "audio/wav")},
            data={"name": "n", "language": "en"},
        )

    assert resp.status_code == 502
