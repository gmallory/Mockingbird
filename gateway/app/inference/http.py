"""HTTP client for the inference service (off the hot path).

The gRPC client in ``client.py`` carries the live audio stream; this thin httpx
wrapper carries one-shot uploads. The gateway ``POST /voices`` route proxies a
recorded clip here, to the inference service, which owns the Cartesia key.
``train_hd`` (M9) is the HD-training equivalent, called synchronously from the
Celery worker (``app/training/tasks.py``), which has no event loop to run an
async client in. On any transport or HTTP error both raise
:class:`InferenceHTTPError` so the caller returns a clean 502/failed row
instead of leaking httpx internals.
"""

import httpx


class InferenceHTTPError(Exception):
    """The inference HTTP call failed; the caller should surface it cleanly."""


async def clone_voice(
    base_url: str,
    clip: bytes,
    filename: str | None,
    content_type: str | None,
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
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # ValueError covers a 2xx with a non-JSON body (resp.json() raises
            # json.JSONDecodeError, a ValueError that is not an httpx.HTTPError);
            # fold it into the same clean 502 instead of leaking a 500.
            raise InferenceHTTPError(str(exc)) from exc


def train_hd(
    base_url: str,
    clip: bytes,
    name: str,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """POST a clip to inference ``/train_hd``; return the trained model's metadata.

    Synchronous (``httpx.Client``, not ``AsyncClient``): called from the Celery
    training worker, which runs outside any event loop. Timeout is generous —
    a real GPU fine-tune can run long (PRODUCT_SPEC §4.2: 30min-2hrs); the
    torch-free synthetic stand-in inference falls back to returns in well
    under a second.
    """
    files = {"clip": ("clip.wav", clip, "application/octet-stream")}
    data = {"name": name}
    with httpx.Client(
        base_url=base_url,
        transport=transport,
        timeout=httpx.Timeout(600.0, connect=10.0),
    ) as client:
        try:
            resp = client.post("/train_hd", files=files, data=data)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise InferenceHTTPError(str(exc)) from exc
