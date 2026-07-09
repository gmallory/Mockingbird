"""HD (RVC) training route (M9).

``POST /train_hd`` is the inference-side half of the training pipeline: the
gateway's Celery task (``gateway/app/training/tasks.py``) uploads the
reference clip here — inference has no DB access, same split as the M4b/M5b
instant-clone flow — and this runs the full offline pipeline
(``app.export.hd_train.hd_train_local``) in a thread (like
``_clone_self_hosted``), returning the exported ``{model_id}.onnx``'s
metadata. The returned ``model_id`` **is** the streaming ``model_id`` the
EXISTING ``self_hosted``/``cloud_gpu`` backend loads unchanged — no new
streaming code needed.

Unauthenticated by the same reasoning as ``POST /voices`` (M4b/M5b): this is
an internal call from the gateway, not a client-facing endpoint.
"""

import asyncio
from collections.abc import Callable

import structlog
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.config import settings
from app.voices import _read_clip

log = structlog.get_logger(__name__)

router = APIRouter()


def _progress_logger(model_hint: str) -> Callable[[str, float], None]:
    def _cb(stage: str, fraction: float) -> None:
        log.info("train_hd.progress", model=model_hint, stage=stage, progress=round(fraction, 3))

    return _cb


@router.post("/train_hd")
async def train_hd(
    clip: UploadFile = File(...),
    name: str = Form(...),
) -> dict:
    """Run the HD training pipeline on an uploaded clip; return its artifact metadata."""
    if settings.inference_backend not in ("self_hosted", "cloud_gpu"):
        raise HTTPException(
            status_code=400,
            detail="HD training requires INFERENCE_BACKEND=self_hosted|cloud_gpu",
        )

    clip_bytes = await _read_clip(clip, max_bytes=settings.max_hd_clip_bytes)
    if not clip_bytes:
        raise HTTPException(status_code=400, detail="clip is empty")

    from app.export.hd_train import HDTrainError, hd_train_local

    try:
        # CPU-bound (ORT session + ONNX write, same reasoning as clone_voice_local);
        # keep the event loop free for other requests.
        result = await asyncio.to_thread(
            hd_train_local,
            clip_bytes,
            name,
            settings.self_hosted_model_dir,
            _progress_logger(name),
            settings.self_hosted_model_sample_rate,
        )
    except HDTrainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "voice_id": result["model_id"],
        "model_id": result["model_id"],
        "model_size_bytes": result["model_size_bytes"],
        "sample_duration_sec": result["sample_duration_sec"],
        "sample_count": result["sample_count"],
        "synthetic": result["synthetic"],
    }
