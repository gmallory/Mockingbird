"""Contract test: gateway's outbound clone-voice multipart shape.

Gateway and inference are separate uv-managed services with no shared Python
module (see docs/m4b-review-skipped-fixes.md #5), so
``docs/contracts/voice_clone_multipart.json`` is the single source of truth for
the wire shape. This test asserts the gateway side against it;
``inference/tests/test_contracts.py`` asserts the inference side against the
same fixture.
"""

import json
from pathlib import Path

import httpx

from app.inference import http as inference_http

_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / "docs/contracts/voice_clone_multipart.json").read_text()
)


async def test_clone_voice_sends_contract_fields():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        return httpx.Response(200, json={"voice_id": "vid_1", "name": "Alice", "language": "en"})

    result = await inference_http.clone_voice(
        base_url="http://inference.test",
        clip=b"RIFFDATA",
        filename="sample.wav",
        content_type="audio/wav",
        name="Alice",
        language="en",
        transport=httpx.MockTransport(handler),
    )

    body = captured["content"]
    assert f'name="{_CONTRACT["file_field"]}"'.encode() in body
    for field in _CONTRACT["form_fields"]:
        assert f'name="{field}"'.encode() in body
    for field in _CONTRACT["response_fields"]:
        assert field in result
