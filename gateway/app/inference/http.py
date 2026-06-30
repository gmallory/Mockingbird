"""HTTP client for the inference service (off the hot path).

The gRPC client in ``client.py`` carries the live audio stream; this thin httpx
wrapper carries the one-shot voice-clone upload. The gateway ``POST /voices`` route
proxies a recorded clip here, to the inference service, which owns the Cartesia
key. On any transport or HTTP error this raises :class:`InferenceHTTPError` so the
route returns a clean 502 instead of leaking httpx internals.
"""

import httpx


class InferenceHTTPError(Exception):
    """The inference HTTP call failed; the route should surface a 502."""


async def clone_voice(
    base_url: str,
    clip: bytes,
    filename: str,
    content_type: str,
    name: str,
    language: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """POST a clip to inference ``/voices``; return its ``{voice_id, name, language}``."""
    files = {"clip": (filename or "clip", clip, content_type or "application/octet-stream")}
    data = {"name": name, "language": language}
    async with httpx.AsyncClient(
        base_url=base_url,
        transport=transport,
        timeout=httpx.Timeout(30.0, connect=10.0),
    ) as client:
        try:
            resp = await client.post("/voices", files=files, data=data)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise InferenceHTTPError(str(exc)) from exc
        return resp.json()
