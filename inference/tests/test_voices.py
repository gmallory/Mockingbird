"""Clone route tests.

The route forwards an uploaded clip to Cartesia /voices/clone and returns the
minted voice id. Cartesia is mocked with an httpx.MockTransport, injected via
FastAPI's ``app.dependency_overrides`` on the ``voices._get_transport`` dependency
(same pattern gateway/tests/test_voices.py uses for ``get_session``), so no network
or API key is needed; the inbound request is driven through the app with
httpx.ASGITransport.
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

    app.dependency_overrides[voices._get_transport] = lambda: httpx.MockTransport(handler)

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/voices",
                files={"clip": ("sample.wav", b"RIFFDATA", "audio/wav")},
                data={"name": "My Voice", "language": "en"},
            )
    finally:
        app.dependency_overrides.clear()

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

    app.dependency_overrides[voices._get_transport] = lambda: httpx.MockTransport(handler)

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/voices",
                files={"clip": ("sample.wav", b"x", "audio/wav")},
                data={"name": "n", "language": "en"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 502


async def test_clone_self_hosted_returns_model_id(monkeypatch, tmp_path):
    """self_hosted/cloud_gpu clone locally: voice_id is the ONNX model id."""
    import app.export.clone as clone_mod

    monkeypatch.setattr(voices.settings, "inference_backend", "self_hosted")
    monkeypatch.setattr(voices.settings, "self_hosted_model_dir", str(tmp_path))
    calls = {}

    def _fake_clone(clip_bytes: bytes, name: str, model_dir: str) -> str:
        calls["args"] = (clip_bytes, name, model_dir)
        return "ov2-my-voice-abcd1234"

    monkeypatch.setattr(clone_mod, "clone_voice_local", _fake_clone)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/voices",
            files={"clip": ("sample.wav", b"RIFFDATA", "audio/wav")},
            data={"name": "My Voice", "language": "en"},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "voice_id": "ov2-my-voice-abcd1234",
        "name": "My Voice",
        "language": "en",
    }
    assert calls["args"] == (b"RIFFDATA", "My Voice", str(tmp_path))


async def test_clone_cloud_gpu_uses_local_clone_too(monkeypatch, tmp_path):
    import app.export.clone as clone_mod

    monkeypatch.setattr(voices.settings, "inference_backend", "cloud_gpu")
    monkeypatch.setattr(voices.settings, "self_hosted_model_dir", str(tmp_path))
    monkeypatch.setattr(clone_mod, "clone_voice_local", lambda *a: "ov2-x-00000000")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/voices",
            files={"clip": ("s.wav", b"RIFFDATA", "audio/wav")},
            data={"name": "x", "language": "en"},
        )
    assert resp.status_code == 200
    assert resp.json()["voice_id"] == "ov2-x-00000000"


async def test_clone_self_hosted_surfaces_clone_error_as_400(monkeypatch):
    import app.export.clone as clone_mod

    monkeypatch.setattr(voices.settings, "inference_backend", "self_hosted")

    def _raise(*args):
        raise clone_mod.CloneError("OpenVoice template models not found")

    monkeypatch.setattr(clone_mod, "clone_voice_local", _raise)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/voices",
            files={"clip": ("s.wav", b"RIFFDATA", "audio/wav")},
            data={"name": "x", "language": "en"},
        )
    assert resp.status_code == 400
    assert "template models not found" in resp.json()["detail"]


async def test_clone_rejects_oversized_clip(monkeypatch):
    _configure_cartesia(monkeypatch)
    monkeypatch.setattr(voices.settings, "max_clip_bytes", 8)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/voices",
            files={"clip": ("sample.wav", b"x" * 9, "audio/wav")},
            data={"name": "n", "language": "en"},
        )

    assert resp.status_code == 413
