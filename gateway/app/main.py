"""Gateway FastAPI application.

Hosts the ``/ws/voice`` echo loop (Milestone 1) and, as of Slim M2, a ``/healthz``
that reports Postgres + Redis reachability. The WebSocket echo path is kept
independent of the database/Redis so the live demo loop survives an infra outage.
Auth, routing, and the gRPC proxy to inference arrive in later milestones.
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
        await app.state.redis.aclose()
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
    try:
        return bool(await app.state.redis.ping())
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
    await voice_stream(websocket)
