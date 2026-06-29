"""The ``/ws/voice`` endpoint.

For the vertical echo slice this does the thinnest possible thing that proves
the latency loop: it accepts the binary PCM frames the browser sends and writes
the *same bytes* straight back, with no transformation and no model.

The streaming loop interleaves two kinds of message on one socket:
  * binary frames  -> Int16 PCM audio (echoed back verbatim)
  * text frames    -> JSON control messages (start / stop / ping)

>>> SEAM: in Milestone 3 the ``# echo`` line below is replaced by a gRPC call
>>> to the inference service; everything else here stays the same.
"""

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.websocket.protocol import (
    ErrorMessage,
    PongMessage,
    ReadyMessage,
    client_message_adapter,
)


async def voice_stream(websocket: WebSocket) -> None:
    await websocket.accept()

    try:
        # First message should be `start`, but we stay lenient: any control
        # message is fine, and we simply signal readiness.
        await websocket.send_json(ReadyMessage().model_dump())

        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            data_bytes = message.get("bytes")
            if data_bytes is not None:
                # echo — same PCM bytes straight back to the client
                await websocket.send_bytes(data_bytes)
                continue

            text = message.get("text")
            if text is not None:
                await _handle_control(websocket, text)

    except WebSocketDisconnect:
        pass


async def _handle_control(websocket: WebSocket, text: str) -> None:
    try:
        msg = client_message_adapter.validate_json(text)
    except ValidationError as exc:
        await websocket.send_json(ErrorMessage(code="bad_message", message=str(exc)).model_dump())
        return

    if msg.type == "ping":
        await websocket.send_json(PongMessage().model_dump())
    elif msg.type == "start":
        # Already sent `ready` on connect; re-affirm for clients that (re)start.
        await websocket.send_json(ReadyMessage().model_dump())
    elif msg.type == "stop":
        await websocket.close()
    # switch_model is a no-op in the echo slice (no models yet).
