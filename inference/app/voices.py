"""Voice cloning route (Cartesia).

The inference service owns the Cartesia API key, so cloning a user's voice lives
here rather than in the gateway. ``POST /voices`` takes a recorded audio clip and
forwards it to Cartesia ``POST /voices/clone``; the returned ``id`` is a Cartesia
``voice_id`` usable directly as the voice-changer ``voice[id]`` on the streaming
path. The gateway persists that id (it owns the database); this route only mints it.

This is an offline REST call, not the 20ms audio hot path, so a per-request httpx
client is fine — cloning is infrequent. The Cartesia auth/version headers mirror
``app/backends/cartesia.py``.
"""

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.config import settings

router = APIRouter()

_CLONE_PATH = "/voices/clone"

# Test seam: inject an httpx transport (e.g. httpx.MockTransport) so tests exercise
# the request shape without real network or an API key. None -> real transport.
_transport: httpx.AsyncBaseTransport | None = None


def _require_cartesia() -> None:
    """Reject cloning unless the Cartesia backend is configured with a key."""
    if settings.inference_backend != "cartesia":
        raise HTTPException(
            status_code=400, detail="voice cloning requires INFERENCE_BACKEND=cartesia"
        )
    if not settings.cartesia_api_key:
        raise HTTPException(status_code=400, detail="CARTESIA_API_KEY is not set")


@router.post("/voices")
async def clone_voice(
    clip: UploadFile = File(...),
    name: str = Form(...),
    language: str = Form(...),
) -> dict:
    """Clone a voice from an uploaded clip; return the Cartesia voice id."""
    _require_cartesia()

    clip_bytes = await clip.read()
    files = {
        "clip": (
            clip.filename or "clip",
            clip_bytes,
            clip.content_type or "application/octet-stream",
        )
    }
    data = {"name": name, "language": language}

    async with httpx.AsyncClient(
        base_url=settings.cartesia_base_url,
        headers={
            "Authorization": f"Bearer {settings.cartesia_api_key}",
            "Cartesia-Version": settings.cartesia_version,
        },
        transport=_transport,
        timeout=httpx.Timeout(30.0, connect=10.0),
    ) as client:
        try:
            resp = await client.post(_CLONE_PATH, files=files, data=data)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"cartesia clone failed: {exc}") from exc
        meta = resp.json()

    voice_id = meta.get("id")
    if not voice_id:
        raise HTTPException(status_code=502, detail="cartesia clone returned no voice id")
    return {
        "voice_id": voice_id,
        "name": meta.get("name", name),
        "language": meta.get("language", language),
    }
