"""The ``/ws/voice`` endpoint.

Milestone 3: binary PCM frames are proxied to the inference service over a gRPC
``Convert`` stream and the transformed frame is sent back to the browser. The
control channel (start / switch_model / stop / ping) is unchanged.

Two kinds of message interleave on one socket:
  * binary frames  -> Int16 PCM audio (proxied to inference, transformed back)
  * text frames    -> JSON control messages

If inference is unreachable, the session degrades to passthrough — the original
audio is echoed back (the M1 behavior) and a ``degraded`` control message is sent
once, so the loop survives an inference outage instead of dropping.
"""

from collections import deque
from dataclasses import dataclass, field
from time import perf_counter

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.inference.client import InferenceSession, InferenceUnavailable
from app.websocket.protocol import (
    DegradedMessage,
    ErrorMessage,
    MetricsMessage,
    ModelLoadedMessage,
    PongMessage,
    ReadyMessage,
    client_message_adapter,
)

# How often (in frames) to push a metrics update to the client. At 20ms/frame,
# every 50 frames is ~1s.
_METRICS_EVERY = 50


@dataclass
class _SessionState:
    sample_rate: int = 48000
    model_id: str = ""
    degraded: bool = False
    frames: int = 0
    # Bounded to the metrics window: a long session must not grow this unboundedly.
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=_METRICS_EVERY))


async def voice_stream(websocket: WebSocket, grpc_url: str) -> None:
    await websocket.accept()
    state = _SessionState()
    session = InferenceSession(grpc_url)

    try:
        # First message should be `start`, but we stay lenient: signal readiness.
        await websocket.send_json(ReadyMessage().model_dump())

        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            data_bytes = message.get("bytes")
            if data_bytes is not None:
                await _handle_audio(websocket, session, state, data_bytes)
                continue

            text = message.get("text")
            if text is not None:
                await _handle_control(websocket, state, text)

    except WebSocketDisconnect:
        pass
    finally:
        await session.aclose()


async def _handle_audio(
    websocket: WebSocket,
    session: InferenceSession,
    state: _SessionState,
    pcm: bytes,
) -> None:
    started = perf_counter()

    if state.degraded:
        out = pcm  # passthrough: hand the original audio straight back
    else:
        try:
            out = await session.convert(pcm, state.sample_rate, state.model_id)
        except InferenceUnavailable:
            state.degraded = True
            out = pcm
            await websocket.send_json(DegradedMessage().model_dump())

    await websocket.send_bytes(out)

    state.frames += 1
    state.latencies_ms.append((perf_counter() - started) * 1000)
    if state.frames % _METRICS_EVERY == 0:
        # Mean round-trip for the inference hop over the last window (the deque
        # already holds only the most recent _METRICS_EVERY samples).
        window = state.latencies_ms
        await websocket.send_json(
            MetricsMessage(
                latencyMs=sum(window) / len(window),
                framesProcessed=state.frames,
            ).model_dump()
        )


async def _handle_control(websocket: WebSocket, state: _SessionState, text: str) -> None:
    try:
        msg = client_message_adapter.validate_json(text)
    except ValidationError as exc:
        await websocket.send_json(ErrorMessage(code="bad_message", message=str(exc)).model_dump())
        return

    if msg.type == "ping":
        await websocket.send_json(PongMessage().model_dump())
    elif msg.type == "start":
        state.sample_rate = msg.sampleRate
        state.model_id = msg.modelId or ""
        await websocket.send_json(ReadyMessage().model_dump())
    elif msg.type == "switch_model":
        # Passthrough ignores model_id in M3, but plumb it through and ack so the
        # contract is exercised end-to-end (real model loading lands in M4).
        state.model_id = msg.modelId
        await websocket.send_json(ModelLoadedMessage(modelId=msg.modelId).model_dump())
    elif msg.type == "stop":
        await websocket.close()
