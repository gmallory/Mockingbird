"""Gateway FastAPI application.

Hosts the ``/ws/voice`` loop, which (as of M3) proxies audio to the inference
service over gRPC, plus a ``/healthz`` that reports Postgres + Redis reachability.
The WebSocket path is kept independent of the database/Redis so the live demo
loop survives an infra outage; it also degrades to passthrough if inference is
down. Auth and routing arrive in later milestones.
"""

from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.auth import router as auth_router
from app.calls import router as calls_router
from app.calls.media import twilio_media_stream
from app.config import settings
from app.db.session import engine
from app.logging import configure_logging
from app.rate_limit import RateLimiter
from app.training import router as training_router
from app.voices import router as voices_router
from app.websocket.auth import resolve_ws_auth
from app.websocket.handler import voice_stream

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    app.state.redis = aioredis.from_url(settings.redis_url)
    log.info("gateway.startup", redis_url=settings.redis_url)
    try:
        yield
    finally:
        # Close both independently: a failure closing Redis must not skip the
        # engine dispose (and vice versa), or one pool leaks on shutdown.
        try:
            await app.state.redis.aclose()
        finally:
            await engine.dispose()
        log.info("gateway.shutdown")


app = FastAPI(title="Mockingbird Gateway", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(voices_router)
app.include_router(calls_router)
app.include_router(training_router)


async def _check_db() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - health check reports, never raises
        log.warning("healthz.db_unreachable", error=str(exc))
        return False


async def _check_redis() -> bool:
    redis = getattr(app.state, "redis", None)
    if redis is None:  # lifespan hasn't run (e.g. a bare TestClient)
        return False
    try:
        return bool(await redis.ping())
    except Exception as exc:  # noqa: BLE001 - health check reports, never raises
        log.warning("healthz.redis_unreachable", error=str(exc))
        return False


@app.get("/healthz")
async def healthz() -> JSONResponse:
    db_ok = await _check_db()
    redis_ok = await _check_redis()
    healthy = db_ok and redis_ok
    body = {
        "status": "ok" if healthy else "degraded",
        "db": "ok" if db_ok else "down",
        "redis": "ok" if redis_ok else "down",
    }
    return JSONResponse(body, status_code=200 if healthy else 503)


@app.get("/metrics")
async def metrics() -> Response:
    # Prometheus scrape target (M7). Served unauthenticated: in the compose /
    # k8s topology only Prometheus reaches this port path, and the exposition
    # carries counters, not user data.
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket) -> None:
    # Auth rides in the query string (the browser can't set WS headers). Classify
    # before accepting; the handler enforces the outcome + per-user limits. The
    # limiter shares the app's Redis client, or None (bare TestClient / no
    # lifespan) in which case it fail-opens — the anonymous demo needs no Redis.
    auth = await resolve_ws_auth(websocket.query_params.get("token"))
    limiter = RateLimiter(getattr(websocket.app.state, "redis", None))
    await voice_stream(
        websocket,
        settings.inference_grpc_url,
        auth=auth,
        limiter=limiter,
        timeout_s=settings.inference_timeout_ms / 1000,
    )


@app.websocket("/ws/twilio/{call_id}")
async def ws_twilio(websocket: WebSocket, call_id: str) -> None:
    # Twilio's Media Stream for a live call (M8a). Gated by the per-call secret
    # minted at call creation (Twilio can't send auth headers on the stream).
    await twilio_media_stream(websocket, call_id, websocket.query_params.get("secret"))
