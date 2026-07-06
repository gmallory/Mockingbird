"""Voice registry routes (M4b), per-user as of M6a.

``POST /voices`` proxies a recorded clip to the inference clone route, then persists
the returned ``voice_id`` with its label/language, owned by the authenticated user.
``GET /voices`` lists that user's registry. Both require a valid Supabase bearer
token (``get_current_user``). Independent of the audio hot path — this is a one-shot
upload, not the 20ms stream.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import User, Voice
from app.db.session import get_session
from app.inference import http as inference_http

router = APIRouter()


@router.get("/voices")
async def list_voices(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Voice]:
    """List the caller's registered voices, oldest first."""
    result = await session.execute(
        select(Voice).where(Voice.user_id == user.id).order_by(Voice.created_at)
    )
    return list(result.scalars().all())


async def _read_clip(clip: UploadFile) -> bytes:
    """Read the upload in chunks, aborting once it exceeds ``max_clip_bytes``.

    Bounds memory use regardless of what (if anything) the client's Content-Length
    header claims.
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
async def create_voice(
    clip: UploadFile = File(...),
    label: str = Form(...),
    language: str = Form("en"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Voice:
    """Clone a voice from the uploaded clip and persist it, owned by the caller."""
    clip_bytes = await _read_clip(clip)
    if not clip_bytes:
        raise HTTPException(status_code=400, detail="clip is empty")
    try:
        result = await inference_http.clone_voice(
            base_url=settings.inference_service_url,
            clip=clip_bytes,
            filename=clip.filename,
            content_type=clip.content_type,
            name=label,
            language=language,
        )
    except inference_http.InferenceHTTPError as exc:
        raise HTTPException(status_code=502, detail=f"voice clone failed: {exc}") from exc

    voice_id = result.get("voice_id")
    if not voice_id:
        raise HTTPException(status_code=502, detail="inference returned no voice_id")

    voice = Voice(voice_id=voice_id, label=label, language=language, user_id=user.id)
    session.add(voice)
    try:
        await session.commit()
    except IntegrityError as exc:
        # Cartesia mints a fresh id per clone call, so this means a retried/
        # double-submitted request raced its own earlier insert.
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"voice {voice_id} is already registered"
        ) from exc
    await session.refresh(voice)
    return voice
