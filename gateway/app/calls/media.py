"""The ``/ws/twilio/{call_id}`` endpoint — Twilio Media Streams termination (M8a).

Twilio connects here (per the TwiML ``<Connect><Stream>``) once the callee
answers, and speaks its JSON media protocol: ``connected`` -> ``start`` (carries
the ``streamSid``) -> ``media`` events (base64 mu-law 8kHz payloads, ~20ms each)
-> ``stop``. Because the stream was opened with ``<Connect>``, it is
bidirectional: we send ``media`` events back and Twilio plays them to the callee.

Bridging: inbound callee audio is transcoded to 48kHz PCM and queued to the
browser session; the browser session's converted output arrives on the bridge
already mu-law encoded and is drained to Twilio by a concurrent sender task.
The bridge secret minted at call creation rides in the stream URL's query
string and is required — an unknown call id or wrong secret is closed with 1008
before any audio flows.
"""

import asyncio
import base64
import contextlib
import secrets

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from app.calls import bridge as bridges
from app.calls.telephony import mulaw8_to_pcm48

log = structlog.get_logger(__name__)

WS_CLOSE_POLICY_VIOLATION = 1008


async def twilio_media_stream(websocket: WebSocket, call_id: str, secret: str | None) -> None:
    bridge = bridges.get(call_id)
    await websocket.accept()
    if bridge is None or not secrets.compare_digest(bridge.secret, secret or ""):
        log.warning("twilio_media.rejected", call_id=call_id)
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
        return

    stream_sid: str | None = None
    sender: asyncio.Task | None = None

    async def pump_to_callee() -> None:
        # Drains the browser session's converted audio to the phone leg. A None
        # sentinel means the bridge closed (hangup / status callback).
        while True:
            payload = await bridge.to_callee.get()
            if payload is None:
                break
            await websocket.send_json(
                {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": base64.b64encode(payload).decode()},
                }
            )

    log.info("twilio_media.connected", call_id=call_id)
    try:
        while True:
            msg = await websocket.receive_json()
            event = msg.get("event")
            if event == "start":
                stream_sid = msg.get("start", {}).get("streamSid")
                if sender is None:
                    sender = asyncio.create_task(pump_to_callee())
            elif event == "media":
                payload = base64.b64decode(msg.get("media", {}).get("payload", ""))
                if payload:
                    bridge.push_to_browser(mulaw8_to_pcm48(payload))
            elif event == "stop":
                break
            # "connected" and "mark" events need no action.
    except WebSocketDisconnect:
        pass
    finally:
        if sender is not None:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
        # The stream ends when the call ends; tear the bridge down so the
        # browser-side drainer wakes and the session reverts to local echo.
        bridges.close(call_id)
        with contextlib.suppress(Exception):
            await websocket.close()
        log.info("twilio_media.closed", call_id=call_id)
