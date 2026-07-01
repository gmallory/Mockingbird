"""Voice cloning route (Cartesia).

The inference service owns the Cartesia API key, so cloning a user's voice lives
here rather than in the gateway. ``POST /voices`` takes a recorded audio clip and
forwards it to Cartesia ``POST /voices/clone``; the returned ``id`` is a Cartesia
``voice_id`` usable directly as the voice-changer ``voice[id]`` on the streaming
path. The gateway persists that id (it owns the database); this route only mints it.

This is an offline REST call, not the 20ms audio hot path, so a per-request httpx
client is fine — cloning is infrequent. The Cartesia client is the same factory
``app/backends/cartesia.py`` uses for the long-lived voice-changer client.
"""

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.backends.cartesia import cartesia_client
from app.config import settings

router = APIRouter()

_CLONE_PATH = "/voices/clone"


def _get_transport() -> httpx.AsyncBaseTransport | None:
    """FastAPI dependency for the Cartesia transport: real network in prod, swapped
    for an ``httpx.MockTransport`` in tests via ``app.dependency_overrides``."""
    return None


def _require_cartesia() -> None:
    """Reject cloning unless the Cartesia backend is configured with a key."""
    if settings.inference_backend != "cartesia":
        raise HTTPException(
            status_code=400, detail="voice cloning requires INFERENCE_BACKEND=cartesia"
        )
    if not settings.cartesia_api_key:
        raise HTTPException(status_code=400, detail="CARTESIA_API_KEY is not set")


async def _read_clip(clip: UploadFile) -> bytes:
    """Read the upload in chunks, aborting once it exceeds ``max_clip_bytes``.

    Bounds memory use regardless of what (if anything) the client's Content-Length
    header claims — this route is unauthenticated pre-M5.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await clip.read(1 << 16):
        total += len(chunk)
        if total > settings.max_clip_bytes:
            raise HTTPException(status_code=413, detail="clip exceeds max_clip_bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/voices")
async def clone_voice(
    clip: UploadFile = File(...),
    name: str = Form(...),
    language: str = Form(...),
    transport: httpx.AsyncBaseTransport | None = Depends(_get_transport),
) -> dict:
    """Clone a voice from an uploaded clip; return the Cartesia voice id."""
    _require_cartesia()

    clip_bytes = await _read_clip(clip)
    if not clip_bytes:
        raise HTTPException(status_code=400, detail="clip is empty")
    files = {
        "clip": (
            clip.filename or "clip",
            clip_bytes,
            clip.content_type or "application/octet-stream",
        )
    }
    data = {"name": name, "language": language}

    async with cartesia_client(
        api_key=settings.cartesia_api_key,
        base_url=settings.cartesia_base_url,
        version=settings.cartesia_version,
        transport=transport,
    ) as client:
        try:
            resp = await client.post(_CLONE_PATH, files=files, data=data)
            resp.raise_for_status()
            meta = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # ValueError covers a 2xx with a non-JSON body (resp.json() raises
            # json.JSONDecodeError, a ValueError that is not an httpx.HTTPError);
            # surface it as the same 502 as every other Cartesia failure.
            raise HTTPException(status_code=502, detail=f"cartesia clone failed: {exc}") from exc

    voice_id = meta.get("id")
    if not voice_id:
        raise HTTPException(status_code=502, detail="cartesia clone returned no voice id")
    return {
        "voice_id": voice_id,
        "name": meta.get("name", name),
        "language": meta.get("language", language),
    }
