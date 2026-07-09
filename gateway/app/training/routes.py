"""HD training API (M9): kick off an RVC fine-tune and poll its progress.

``POST /api/voices/{voice_id}/train`` accepts the 10-30 minute reference clip
for a voice already in the caller's registry (``Voice``, typically an
instant-clone row from the Studio), stages it to a temp file — Celery task
arguments are JSON, so the clip travels as a path, not raw bytes, to keep a
large upload off the message broker — and enqueues :func:`app.training.tasks.
train_voice`. ``GET .../train/status`` returns the latest training job for
that voice, scoped to the caller, with an ETA derived from elapsed/progress.

Feature-flagged like calling (``ENABLE_TRAINING``, mirrors ``ENABLE_CALLING``
in ``app/calls/routes.py``): disabled or a broker outage both return a clean
503 rather than a crash, and a broker failure marks the just-created row
failed instead of leaving it stuck at ``training`` forever.
"""

import asyncio
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth.dependencies import get_current_user
from app.config import settings
from app.db.models import User, Voice, VoiceModel, VoiceModelStatus, VoiceModelType
from app.db.session import get_session
from app.training.tasks import train_voice
from app.voices.routes import _read_clip

log = structlog.get_logger(__name__)

router = APIRouter()

_STAGING_PREFIX = "mockingbird-train-"
_STAGING_SUFFIX = ".clip"


def _require_training_enabled() -> None:
    if not settings.enable_training:
        raise HTTPException(status_code=503, detail="HD training is disabled (ENABLE_TRAINING)")


async def _owned_voice(voice_id: UUID, user: User, session: AsyncSession) -> Voice:
    voice = await session.get(Voice, voice_id)
    if voice is None or voice.user_id != user.id:
        raise HTTPException(status_code=404, detail="voice not found")
    return voice


def _stage_clip(clip_bytes: bytes) -> str:
    """Write the clip to a private temp file; returns its path."""
    fd, path = tempfile.mkstemp(prefix=_STAGING_PREFIX, suffix=_STAGING_SUFFIX)
    try:
        with open(fd, "wb") as fh:
            fh.write(clip_bytes)
    except OSError:
        Path(path).unlink(missing_ok=True)
        raise
    return path


def _unstage_clip(path: str) -> None:
    """Remove a staged clip. ``os.unlink`` (not ``Path.unlink``): callers of
    this include async function bodies, and ASYNC240 flags blocking pathlib
    calls there (``os.unlink`` is exempt)."""
    with suppress(FileNotFoundError):
        os.unlink(path)


@router.post("/api/voices/{voice_id}/train", status_code=202)
async def start_training(
    voice_id: UUID,
    clip: UploadFile = File(...),
    name: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> VoiceModel:
    """Kick off an HD (RVC) fine-tune from a long reference clip."""
    _require_training_enabled()
    voice = await _owned_voice(voice_id, user, session)
    clip_bytes = await _read_clip(clip, max_bytes=settings.max_hd_clip_bytes)
    if not clip_bytes:
        raise HTTPException(status_code=400, detail="clip is empty")

    label = name.strip() or voice.label
    model = VoiceModel(
        user_id=user.id,
        voice_id=voice.id,
        name=label,
        type=VoiceModelType.HD,
        status=VoiceModelStatus.TRAINING,
        progress=0.0,
        stage="queued",
    )
    session.add(model)
    await session.commit()
    await session.refresh(model)

    staged_path = _stage_clip(clip_bytes)
    try:
        await asyncio.to_thread(train_voice.delay, str(model.id), staged_path, label)
    except Exception as exc:  # noqa: BLE001 - broker down must 503, never crash the request
        _unstage_clip(staged_path)
        log.warning("training.enqueue_failed", voice_model_id=str(model.id), error=str(exc))
        model.status = VoiceModelStatus.FAILED
        model.stage = "failed"
        model.error = "training queue unavailable"
        await session.commit()
        raise HTTPException(status_code=503, detail="training queue unavailable") from exc

    return model


class TrainStatusResponse(BaseModel):
    id: UUID
    voice_id: UUID | None
    name: str
    status: VoiceModelStatus
    stage: str
    progress: float
    error: str | None
    model_path: str
    sample_duration_sec: float
    sample_count: int
    eta_seconds: float | None


def _eta_seconds(model: VoiceModel) -> float | None:
    """Naive linear projection from elapsed wall time / progress so far."""
    if model.status != VoiceModelStatus.TRAINING or model.progress <= 0:
        return None
    started = model.training_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - started).total_seconds()
    remaining = elapsed * (1 - model.progress) / model.progress
    return max(0.0, remaining)


@router.get("/api/voices/{voice_id}/train/status")
async def training_status(
    voice_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TrainStatusResponse:
    """The most recent HD training job for this voice, scoped to the caller."""
    await _owned_voice(voice_id, user, session)
    result = await session.execute(
        select(VoiceModel)
        .where(VoiceModel.voice_id == voice_id, VoiceModel.user_id == user.id)
        .order_by(VoiceModel.created_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    model = result.scalars().first()
    if model is None:
        raise HTTPException(status_code=404, detail="no training job for this voice")
    return TrainStatusResponse(
        id=model.id,
        voice_id=model.voice_id,
        name=model.name,
        status=model.status,
        stage=model.stage,
        progress=model.progress,
        error=model.error,
        model_path=model.model_path,
        sample_duration_sec=model.sample_duration_sec,
        sample_count=model.sample_count,
        eta_seconds=_eta_seconds(model),
    )
