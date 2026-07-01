"""Contract test: inference's clone-voice request/response shape.

Gateway and inference are separate uv-managed services with no shared Python
module (see docs/m4b-review-skipped-fixes.md #5), so
``docs/contracts/voice_clone_multipart.json`` is the single source of truth for
the wire shape. This test asserts the inference side against it;
``gateway/tests/test_contracts.py`` asserts the gateway side against the same
fixture.
"""

import json
from pathlib import Path

import httpx
from fastapi import FastAPI

from app import voices

_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / "docs/contracts/voice_clone_multipart.json").read_text()
)

app = FastAPI()
app.include_router(voices.router)


async def test_clone_route_accepts_contract_fields(monkeypatch):
    monkeypatch.setattr(voices.settings, "inference_backend", "cartesia")
    monkeypatch.setattr(voices.settings, "cartesia_api_key", "sk_test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "vid_1", "name": "Alice", "language": "en"})

    app.dependency_overrides[voices._get_transport] = lambda: httpx.MockTransport(handler)

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            files = {_CONTRACT["file_field"]: ("sample.wav", b"RIFFDATA", "audio/wav")}
            data = dict.fromkeys(_CONTRACT["form_fields"], "v")
            resp = await client.post("/voices", files=files, data=data)
        assert resp.status_code == 200
        for field in _CONTRACT["response_fields"]:
            assert field in resp.json()
    finally:
        app.dependency_overrides.clear()
