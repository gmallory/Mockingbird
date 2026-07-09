"""Celery application for the HD training pipeline (M9).

Reuses the gateway's existing Redis instance as both broker and result
backend by default (``CELERY_BROKER_URL`` / ``CELERY_RESULT_BACKEND`` in
``.env.example`` override this independently, e.g. to point training at a
different Redis). ``include`` registers ``app.training.tasks`` so both
``celery -A app.training.celery_app worker`` and a plain
``from app.training.tasks import train_voice`` import path see the task.

``task_always_eager`` is the test seam: set ``CELERY_TASK_ALWAYS_EAGER=true``
(or monkeypatch ``celery_app.conf.task_always_eager`` / ``task_eager_
propagates`` directly) to run ``train_voice.delay(...)`` synchronously
in-process with no live worker or broker.
"""

from celery import Celery

from app.config import settings

_broker_url = settings.celery_broker_url or settings.redis_url
_result_backend = settings.celery_result_backend or settings.redis_url

celery_app = Celery(
    "mockingbird_training",
    broker=_broker_url,
    backend=_result_backend,
    include=["app.training.tasks"],
)

celery_app.conf.update(
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=settings.celery_task_always_eager,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Training jobs can legitimately run long (PRODUCT_SPEC §4.2: 30min-2hrs on
    # a real GPU fine-tune); ack-late + no visibility-timeout-driven redelivery
    # storm on a slow-but-alive worker.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Fail fast on a dead broker/backend instead of Celery's default ~20s
    # reconnect backoff: the route already turns any enqueue failure into a
    # clean 503 + a failed row (app/training/routes.py), so there's no reason
    # to make the caller wait through a generous retry policy first.
    broker_connection_retry_on_startup=False,
    broker_transport_options={
        "retry_policy": {
            "max_retries": 1,
            "interval_start": 0,
            "interval_step": 0.1,
            "interval_max": 0.2,
        }
    },
    result_backend_always_retry=False,
    result_backend_max_retries=1,
    result_backend_transport_options={
        "retry_policy": {
            "max_retries": 1,
            "interval_start": 0,
            "interval_step": 0.1,
            "interval_max": 0.2,
        }
    },
)
