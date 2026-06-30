"""Voice registry routes (M4b).

``POST /voices`` proxies a recorded clip to the inference clone route, then persists
the returned Cartesia ``voice_id`` with its label/language. ``GET /voices`` lists the
registry. Single-user, no auth (M5 adds per-user ownership). Independent of the audio
hot path — this is a one-shot upload, not the 20ms stream.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import settings
from app.db.models import Voice
from app.db.session import get_session
from app.inference import http as inference_http

router = APIRouter()


@router.get("/voices")
async def list_voices(session: AsyncSession = Depends(get_session)) -> list[Voice]:
    """List every registered voice, oldest first."""
    result = await session.execute(select(Voice).order_by(Voice.created_at))
    return list(result.scalars().all())


@router.post("/voices")
async def create_voice(
    clip: UploadFile = File(...),
    label: str = Form(...),
    language: str = Form("en"),
    session: AsyncSession = Depends(get_session),
) -> Voice:
    """Clone a voice from the uploaded clip and persist it in the registry."""
    clip_bytes = await clip.read()
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

    voice = Voice(voice_id=voice_id, label=label, language=language)
    session.add(voice)
    await session.commit()
    await session.refresh(voice)
    return voice
