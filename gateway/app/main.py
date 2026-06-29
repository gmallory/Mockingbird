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
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.db.session import engine
from app.logging import configure_logging
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


@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket) -> None:
    await voice_stream(
        websocket,
        settings.inference_grpc_url,
        timeout_s=settings.inference_timeout_ms / 1000,
    )
