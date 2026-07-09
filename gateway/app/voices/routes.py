"""Voice registry routes (M4b), per-user as of M6a.

``POST /voices`` proxies a recorded clip to the inference clone route, then persists
the returned ``voice_id`` with its label/language, owned by the authenticated user.
``GET /voices`` lists that user's registry. Both require a valid Supabase bearer
token (``get_current_user``). Independent of the audio hot path — this is a one-shot
upload, not the 20ms stream.

``GET``/``PATCH /api/voices/{voice_id}`` (M10) read/update the fine-tune knobs
(pitch/speed/breathiness) for one of the caller's voices. Both knobs live on
``VoiceModel`` (added with the shape in M9, "inert until M10"), not ``Voice``
itself — but a plain instant-clone ``Voice`` (M4b/M5b) has no ``VoiceModel``
row at all, only an HD-trained one does (the training job that produced it).
``_tuning_for``/``_get_or_create_tuning`` resolve both cases through a single,
unambiguous join: ``VoiceModel.model_path == Voice.voice_id``. For an
HD-trained voice this is already true — the M9 training task sets
``model.model_path`` and the new ``Voice.voice_id`` to the exact same string
(the exported model id) — so PATCHing an HD voice edits the real training
job's row (and its ``similarity_score``/``mos_score`` ride along for free). A
plain instant-clone voice never matches on first PATCH, so one lightweight
``VoiceModel`` row is created with ``model_path`` set to ``voice.voice_id``
up front, making it self-discoverable by the same query from then on. Both
routes push the merged values to the inference service's ``POST
/voices/{model_id}/tune`` (best-effort — see ``update_voice_tuning``) so the
self-hosted streaming session applies them without the gRPC audio frame
contract ever carrying them (mini-spike writeup: ``inference/app/tuning.py``).
"""

from datetime import UTC, datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import User, Voice, VoiceModel, VoiceModelStatus, VoiceModelType
from app.db.session import get_session
from app.inference import http as inference_http

log = structlog.get_logger(__name__)

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


async def _read_clip(clip: UploadFile, max_bytes: int | None = None) -> bytes:
    """Read the upload in chunks, aborting once it exceeds ``max_bytes``.

    Bounds memory use regardless of what (if anything) the client's Content-Length
    header claims. ``max_bytes`` defaults to ``settings.max_clip_bytes`` (the
    instant-clone cap); the HD training route (M9, ``app/training/routes.py``)
    reuses this same helper with the much larger ``max_hd_clip_bytes`` instead
    of duplicating the chunked-read loop.
    """
    limit = max_bytes if max_bytes is not None else settings.max_clip_bytes
    chunks: list[bytes] = []
    total = 0
    while chunk := await clip.read(1 << 16):
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413, detail=f"clip exceeds maximum size of {limit} bytes"
            )
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


# ----- fine-tune controls (M10) --------------------------------------------


async def _owned_voice(voice_id: UUID, user: User, session: AsyncSession) -> Voice:
    voice = await session.get(Voice, voice_id)
    if voice is None or voice.user_id != user.id:
        raise HTTPException(status_code=404, detail="voice not found")
    return voice


async def _tuning_for(voice: Voice, user: User, session: AsyncSession) -> VoiceModel | None:
    """Find (never create) the ``VoiceModel`` that owns this voice's tuning knobs."""
    result = await session.execute(
        select(VoiceModel).where(
            VoiceModel.user_id == user.id, VoiceModel.model_path == voice.voice_id
        )
    )
    return result.scalars().first()


async def _get_or_create_tuning(voice: Voice, user: User, session: AsyncSession) -> VoiceModel:
    """Find this voice's settings row, or create a minimal one for a never-trained voice.

    The new row's ``model_path`` is set to ``voice.voice_id`` immediately so
    the very next lookup (another PATCH, or a GET) finds it by the same
    unambiguous query — see the module docstring.
    """
    model = await _tuning_for(voice, user, session)
    if model is not None:
        return model
    now = datetime.now(UTC)
    model = VoiceModel(
        user_id=user.id,
        voice_id=voice.id,
        name=voice.label,
        type=VoiceModelType.INSTANT,
        status=VoiceModelStatus.READY,
        progress=1.0,
        stage="ready",
        model_path=voice.voice_id,
        training_started_at=now,
        training_completed_at=now,
    )
    session.add(model)
    await session.flush()  # assign an id without ending the caller's transaction
    return model


def _tuning_response(voice: Voice, model: VoiceModel | None) -> VoiceTuningResponse:
    return VoiceTuningResponse(
        voice_id=voice.id,
        model_id=voice.voice_id,
        label=voice.label,
        pitch_offset=model.pitch_offset if model else 0.0,
        speed_factor=model.speed_factor if model else 1.0,
        breathiness=model.breathiness if model else 0.0,
        similarity_score=model.similarity_score if model else None,
        mos_score=model.mos_score if model else None,
    )


class VoiceTuningResponse(BaseModel):
    voice_id: UUID
    model_id: str
    label: str
    pitch_offset: float
    speed_factor: float
    breathiness: float
    similarity_score: float | None
    mos_score: float | None


class VoiceTuneRequest(BaseModel):
    """Merge-patch body: every field optional, unset fields keep their current value.

    Ranges match PRODUCT_SPEC §6 (``VoiceModel.pitch_offset``/``speed_factor``/
    ``breathiness``); out-of-range values 422 automatically via these
    constraints, no extra validation code needed.
    """

    pitch_offset: float | None = Field(default=None, ge=-12.0, le=12.0)
    speed_factor: float | None = Field(default=None, ge=0.5, le=2.0)
    breathiness: float | None = Field(default=None, ge=0.0, le=1.0)


@router.get("/api/voices/{voice_id}")
async def get_voice_tuning(
    voice_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> VoiceTuningResponse:
    """Current fine-tune knobs (+ quality metrics, if measured) for one of the caller's voices.

    Defaults (pitch 0 / speed 1.0 / breathiness 0, no scores) for a voice
    nobody has tuned yet — this is a read, so it never creates a row.
    """
    voice = await _owned_voice(voice_id, user, session)
    model = await _tuning_for(voice, user, session)
    return _tuning_response(voice, model)


@router.patch("/api/voices/{voice_id}")
async def update_voice_tuning(
    voice_id: UUID,
    payload: VoiceTuneRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> VoiceTuningResponse:
    """Merge-patch pitch/speed/breathiness, then best-effort push to inference.

    The DB write is the durable source of truth and always succeeds/fails on
    its own; the inference push is a convenience so the *next* stream picks up
    the change immediately; instead of on-demand. If inference is unreachable
    this logs a warning and still returns 200 — the gateway's row is correct
    regardless, and a later PATCH (or a session that queries the identical
    value again) re-syncs it. Never leaves the row half-written: the push only
    runs after the commit succeeds.
    """
    voice = await _owned_voice(voice_id, user, session)
    model = await _get_or_create_tuning(voice, user, session)

    if payload.pitch_offset is not None:
        model.pitch_offset = payload.pitch_offset
    if payload.speed_factor is not None:
        model.speed_factor = payload.speed_factor
    if payload.breathiness is not None:
        model.breathiness = payload.breathiness
    session.add(model)
    await session.commit()
    await session.refresh(model)

    try:
        await inference_http.tune_voice(
            base_url=settings.inference_service_url,
            model_id=voice.voice_id,
            pitch_offset=model.pitch_offset,
            speed_factor=model.speed_factor,
            breathiness=model.breathiness,
        )
    except inference_http.InferenceHTTPError as exc:
        log.warning("voices.tune_push_failed", voice_id=str(voice.id), error=str(exc))

    return _tuning_response(voice, model)
