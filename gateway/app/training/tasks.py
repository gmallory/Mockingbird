"""Celery task driving the HD (RVC) training pipeline (M9).

``train_voice`` runs entirely inside the Celery worker process (or inline, in
tests, under Celery's eager mode): a synchronous SQLAlchemy session
(:mod:`app.training.db`) advances the ``VoiceModel`` row's ``progress``/
``stage`` through the PRODUCT_SPEC §4.2 pipeline —

    validation -> preprocessing -> feature_extraction -> training -> export -> ready

— around the one heavy step, a blocking HTTP call to the inference service's
``POST /train_hd`` (:func:`app.inference.http.train_hd`), which owns the
actual model artifacts (gateway never touches ONNX files directly). On
success the row is marked ``ready`` and a new :class:`~app.db.models.Voice`
registry row is created for the trained model, so it streams through the
existing ``self_hosted`` session exactly like any other voice — no change to
the audio hot path. On any failure the row is marked ``failed`` with
``error`` set; the task itself never raises past this boundary.

**Test seam:** ``_transport_override`` is a module-level ``httpx`` transport
used for the (synchronous) call to inference. Task arguments must stay JSON-
serializable for a real Celery dispatch, so a mock transport can't ride along
as a task argument — set it directly instead:
``app.training.tasks._transport_override = httpx.MockTransport(handler)``.
Combined with ``CELERY_TASK_ALWAYS_EAGER=true`` (or monkeypatching
``celery_app.conf.task_always_eager``), this exercises the whole pipeline
with no live worker, broker, or inference service.
"""

import os
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Voice, VoiceModel, VoiceModelStatus
from app.inference import http as inference_http
from app.training.celery_app import celery_app
from app.training.db import sync_session

log = structlog.get_logger(__name__)

# Progress checkpoints around the pipeline's named stages (PRODUCT_SPEC §4.2).
# feature_extraction/training/export all happen inside the one blocking HTTP
# call to inference, so they're reported client-side (here) as we move
# through the request rather than mid-flight — see the module docstring.
_STAGE_PROGRESS: dict[str, float] = {
    "validation": 0.05,
    "preprocessing": 0.15,
    "feature_extraction": 0.30,
    "training": 0.55,
    "export": 0.90,
}

# Test seam only — see the module docstring. Never set outside tests.
_transport_override: httpx.BaseTransport | None = None


def _set_stage(session: Session, voice_model_id: str, stage: str) -> VoiceModel | None:
    model = session.get(VoiceModel, voice_model_id)
    if model is None:
        return None
    model.stage = stage
    model.progress = _STAGE_PROGRESS[stage]
    session.add(model)
    session.commit()
    return model


def _mark_failed(session: Session, voice_model_id: str, error: str) -> None:
    session.rollback()
    model = session.get(VoiceModel, voice_model_id)
    if model is None:
        return
    model.status = VoiceModelStatus.FAILED
    model.stage = "failed"
    model.error = error[:500]
    session.add(model)
    session.commit()


@celery_app.task(name="train_voice")
def train_voice(voice_model_id: str, clip_path: str, name: str) -> dict:
    """Run one HD training job end to end; returns a small status summary.

    ``clip_path`` is a staged temp file (not raw bytes — keeps a 10-30 minute
    clip off the message broker); removed when the job finishes, succeeded or
    failed.
    """
    with sync_session() as session:
        model = session.get(VoiceModel, voice_model_id)
        if model is None:
            log.warning("training.model_missing", voice_model_id=voice_model_id)
            return {"status": "missing"}

        try:
            _set_stage(session, voice_model_id, "validation")
            if not os.path.exists(clip_path):
                raise RuntimeError(f"staged clip missing: {clip_path}")

            _set_stage(session, voice_model_id, "preprocessing")
            with open(clip_path, "rb") as fh:
                clip_bytes = fh.read()
            if not clip_bytes:
                raise RuntimeError("staged clip is empty")

            _set_stage(session, voice_model_id, "feature_extraction")
            _set_stage(session, voice_model_id, "training")
            result = inference_http.train_hd(
                base_url=settings.inference_service_url,
                clip=clip_bytes,
                name=name,
                transport=_transport_override,
            )

            _set_stage(session, voice_model_id, "export")

            model = session.get(VoiceModel, voice_model_id)
            if model is None:  # deleted mid-run; nothing left to update
                return {"status": "missing"}
            model.status = VoiceModelStatus.READY
            model.stage = "ready"
            model.progress = 1.0
            model.model_path = result["model_id"]
            model.model_size_bytes = int(result.get("model_size_bytes", 0))
            model.sample_duration_sec = float(
                result.get("sample_duration_sec", model.sample_duration_sec)
            )
            model.sample_count = int(result.get("sample_count", model.sample_count))
            model.training_completed_at = datetime.now(UTC)
            session.add(model)

            # Additive: register the trained HD model as a selectable voice in
            # the SAME registry the instant clone uses, so it streams through
            # the existing self_hosted backend unchanged. The instant clone's
            # own Voice row (model.voice_id) is left untouched.
            session.add(
                Voice(
                    user_id=model.user_id,
                    voice_id=result["model_id"],
                    label=f"{name} (HD)",
                    language="en",
                )
            )
            session.commit()
            log.info("training.ready", voice_model_id=voice_model_id, model_id=result["model_id"])
            return {"status": "ready", "model_id": result["model_id"]}
        except Exception as exc:  # noqa: BLE001 - task boundary: any failure marks the row
            # failed and returns cleanly; nothing should crash the worker (httpx
            # errors, a bad/missing inference response, a Voice.voice_id unique
            # collision (IntegrityError), a missing staged clip, ...).
            log.warning("training.failed", voice_model_id=voice_model_id, error=str(exc))
            _mark_failed(session, voice_model_id, str(exc))
            return {"status": "failed", "error": str(exc)}
        finally:
            try:
                os.remove(clip_path)
            except OSError:
                pass
