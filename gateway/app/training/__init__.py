"""HD Clone training (M9): Celery + Redis job queue for RVC fine-tuning.

``routes`` exposes ``POST /api/voices/{voice_id}/train`` and
``GET /api/voices/{voice_id}/train/status``. ``celery_app`` + ``tasks`` run the
PRODUCT_SPEC §4.2 pipeline in a worker process (or synchronously, under
Celery's eager mode, in tests) via ``db``'s synchronous session. The one heavy
step calls the inference service's ``POST /train_hd`` over HTTP — inference
owns model artifacts, gateway owns the ``VoiceModel``/``Voice`` rows (sole DB
owner, same split as the M4b/M5b instant-clone flow).
"""

from app.training.routes import router

__all__ = ["router"]
