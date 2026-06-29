"""Gateway FastAPI application.

Echo-slice scope: a health check and the ``/ws/voice`` WebSocket that echoes
audio frames back to the browser. Auth, routing, and the gRPC proxy to
inference are added in later milestones.
"""

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.websocket.handler import voice_stream

app = FastAPI(title="Mockingbird Gateway", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket) -> None:
    await voice_stream(websocket)
