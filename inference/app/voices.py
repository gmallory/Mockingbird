"""Voice cloning route.

``POST /voices`` takes a recorded audio clip and mints a ``voice_id`` usable on
the streaming path; the gateway persists it (it owns the database). The backend
mode decides how (M5b):

- ``cartesia`` — forward the clip to Cartesia ``POST /voices/clone``; the
  returned ``id`` is used directly as the voice-changer ``voice[id]``. The
  inference service owns the Cartesia API key, which is why this lives here.
- ``self_hosted`` / ``cloud_gpu`` — instant-clone locally: extract a speaker
  embedding from the clip and bake it into the exported OpenVoice converter
  template, producing ``{model_id}.onnx`` in the model dir. The returned
  ``voice_id`` **is** the model id the streaming backend loads.

Either way this is an offline call, not the 20ms audio hot path.
"""

import asyncio

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


async def _read_clip(clip: UploadFile, max_bytes: int | None = None) -> bytes:
    """Read the upload in chunks, aborting once it exceeds ``max_bytes``.

    Bounds memory use regardless of what (if anything) the client's Content-Length
    header claims — this route is unauthenticated pre-M5. ``max_bytes`` defaults
    to ``settings.max_clip_bytes``; ``POST /train_hd`` (M9, ``app/training.py``)
    reuses this same helper with the much larger ``max_hd_clip_bytes``.
    """
    limit = max_bytes if max_bytes is not None else settings.max_clip_bytes
    chunks: list[bytes] = []
    total = 0
    while chunk := await clip.read(1 << 16):
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="clip exceeds max_clip_bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _clone_self_hosted(clip_bytes: bytes, name: str, language: str) -> dict:
    """Instant-clone against the exported OpenVoice template (no network)."""
    from app.export.clone import CloneError, clone_voice_local

    try:
        # CPU-bound (ORT session + 130MB protobuf write); keep the loop free.
        model_id = await asyncio.to_thread(
            clone_voice_local, clip_bytes, name, settings.self_hosted_model_dir
        )
    except CloneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"voice_id": model_id, "name": name, "language": language}


@router.post("/voices")
async def clone_voice(
    clip: UploadFile = File(...),
    name: str = Form(...),
    language: str = Form(...),
    transport: httpx.AsyncBaseTransport | None = Depends(_get_transport),
) -> dict:
    """Clone a voice from an uploaded clip; return the minted voice id."""
    if settings.inference_backend not in ("cartesia", "self_hosted", "cloud_gpu"):
        raise HTTPException(
            status_code=400,
            detail="voice cloning requires INFERENCE_BACKEND=cartesia|self_hosted|cloud_gpu",
        )

    clip_bytes = await _read_clip(clip)
    if not clip_bytes:
        raise HTTPException(status_code=400, detail="clip is empty")

    if settings.inference_backend in ("self_hosted", "cloud_gpu"):
        return await _clone_self_hosted(clip_bytes, name, language)

    _require_cartesia()
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
