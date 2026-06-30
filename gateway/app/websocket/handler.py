"""The ``/ws/voice`` endpoint.

Binary PCM frames are proxied to the inference service over a gRPC ``Convert``
stream; converted frames are sent back to the browser. The control channel
(start / switch_model / stop / ping) is unchanged.

The conversion stream is **decoupled**: a clip-based backend buffers a whole
utterance and emits a burst of output frames only once the speaker pauses, so
output frames do not line up 1:1 with input frames. We therefore pump inbound
frames with :meth:`InferenceSession.send` from the receive loop and forward
converted frames from a concurrent reader task draining
:meth:`InferenceSession.outputs`. Every outbound send goes through ``out_lock``
so the reader and the receive loop never write to the socket at the same time.

Two kinds of message interleave on one socket:
  * binary frames  -> Int16 PCM audio (proxied to inference, converted back)
  * text frames    -> JSON control messages

Inference is dialed lazily on the first audio frame, so control-only sessions
never touch it. If inference is unreachable — at open or mid-session — the
session degrades to passthrough: the original audio is echoed back and a
``degraded`` notice is sent once, so the loop survives the outage.
"""

import asyncio
import contextlib
from dataclasses import dataclass

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.inference.client import InferenceSession, InferenceUnavailable
from app.websocket.protocol import (
    DegradedMessage,
    ErrorMessage,
    ModelLoadedMessage,
    PongMessage,
    ReadyMessage,
    client_message_adapter,
)


@dataclass
class _SessionState:
    sample_rate: int = 48000
    model_id: str = ""
    degraded: bool = False
    reader_started: bool = False


async def voice_stream(websocket: WebSocket, grpc_url: str, timeout_s: float = 2.0) -> None:
    await websocket.accept()
    state = _SessionState()
    session = InferenceSession(grpc_url, timeout_s=timeout_s)
    out_lock = asyncio.Lock()
    reader_task: asyncio.Task | None = None

    async def send_json(payload: dict) -> None:
        async with out_lock:
            await websocket.send_json(payload)

    async def send_bytes(data: bytes) -> None:
        async with out_lock:
            await websocket.send_bytes(data)

    async def degrade() -> None:
        # Idempotent: notify once, then audio is passed through unchanged.
        if state.degraded:
            return
        state.degraded = True
        await send_json(DegradedMessage().model_dump())

    async def reader() -> None:
        try:
            async for out in session.outputs():
                await send_bytes(out)
        except InferenceUnavailable:
            await degrade()
        except Exception:  # noqa: BLE001 - unexpected reader failure: degrade so the client keeps getting audio
            # CancelledError is a BaseException, so shutdown cancellation still
            # propagates past this. Suppress any secondary send failure when the
            # socket is already closing during teardown.
            with contextlib.suppress(Exception):
                await degrade()

    try:
        # First message should be `start`, but we stay lenient: signal readiness.
        await send_json(ReadyMessage().model_dump())

        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            data_bytes = message.get("bytes")
            if data_bytes is not None:
                if state.degraded:
                    await send_bytes(data_bytes)  # passthrough echo
                    continue
                if not state.reader_started:
                    # Lazily dial inference and start the reader on the first frame.
                    try:
                        await session.open()
                    except InferenceUnavailable:
                        await degrade()
                        await send_bytes(data_bytes)
                        continue
                    state.reader_started = True
                    reader_task = asyncio.create_task(reader())
                try:
                    await session.send(data_bytes, state.sample_rate, state.model_id)
                except InferenceUnavailable:
                    await degrade()
                    await send_bytes(data_bytes)
                continue

            text = message.get("text")
            if text is not None:
                if await _handle_control(send_json, state, text, websocket):
                    break  # stop requested

    except WebSocketDisconnect:
        pass
    finally:
        if reader_task is not None:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task
        await session.aclose()


async def _handle_control(send_json, state: _SessionState, text: str, websocket: WebSocket) -> bool:
    """Handle one JSON control message. Returns True if the session should stop."""
    try:
        msg = client_message_adapter.validate_json(text)
    except ValidationError as exc:
        await send_json(ErrorMessage(code="bad_message", message=str(exc)).model_dump())
        return False

    if msg.type == "ping":
        await send_json(PongMessage().model_dump())
    elif msg.type == "start":
        state.sample_rate = msg.sampleRate
        state.model_id = msg.modelId or ""
        await send_json(ReadyMessage().model_dump())
    elif msg.type == "switch_model":
        # The selected voice id rides on each subsequent audio frame's model_id;
        # ack so the client knows the gateway will convert to it from now on.
        state.model_id = msg.modelId
        await send_json(ModelLoadedMessage(modelId=msg.modelId).model_dump())
    elif msg.type == "stop":
        await websocket.close()
        return True
    return False
