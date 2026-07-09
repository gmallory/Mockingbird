"""FastAPI health app — and the process entrypoint.

Running ``uvicorn app.health:app`` serves ``/healthz`` on the HTTP port *and*
starts the gRPC server (the lifespan owns it), so one ``uvicorn`` command brings
up the whole inference service.
"""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.backends import get_backend
from app.config import settings
from app.logging import configure_logging
from app.server import create_server
from app.training import router as training_router
from app.voices import router as voices_router

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    backend = get_backend(settings)
    server = await create_server(backend, settings.grpc_host, settings.grpc_port)
    await server.start()
    app.state.backend = backend
    app.state.grpc_server = server
    log.info("inference.startup", backend=settings.inference_backend, grpc_port=settings.grpc_port)
    try:
        yield
    finally:
        await server.stop(grace=1.0)
        await backend.aclose()
        log.info("inference.shutdown")


app = FastAPI(title="Mockingbird Inference", version="0.1.0", lifespan=lifespan)
app.include_router(voices_router)
app.include_router(training_router)


@app.get("/metrics")
async def metrics() -> Response:
    # Prometheus scrape target (M7); see app/metrics.py for the metric set.
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    payload = {
        "status": "ok",
        "backend": settings.inference_backend,
        "grpcPort": settings.grpc_port,
    }
    if settings.inference_backend in ("self_hosted", "cloud_gpu"):
        payload["device"] = settings.device
        # Resolved ONNX Runtime providers — "device": "auto" alone can't tell
        # you whether CUDA actually loaded.
        backend = getattr(request.app.state, "backend", None)
        providers = getattr(backend, "providers", None)
        if providers:
            payload["providers"] = providers
    return JSONResponse(payload)
