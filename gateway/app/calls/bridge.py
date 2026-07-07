"""In-memory registry pairing a browser voice session with a Twilio media stream.

A :class:`CallBridge` is created when ``POST /api/calls/outbound`` places a call
and torn down when the media stream stops (or the status callback reports a
terminal state). It carries two bounded frame queues:

* ``to_callee`` — mu-law 8kHz payloads (already encoded) from the browser
  session's *converted* output, drained by the Twilio media WebSocket.
* ``to_browser`` — 48kHz Int16 PCM frames decoded from the callee's audio,
  drained by the browser session so the user hears the other side.

Both queues drop-oldest when full: on a stall the call loses a moment of audio
instead of growing latency (or memory) without bound. ``close()`` wakes both
drainers with a ``None`` sentinel.

The registry is process-local, which matches the single-gateway deployment: the
same process terminates both WebSockets. A multi-gateway topology would need
Redis-backed routing — noted for M8b, not built.

Each bridge carries a random ``secret`` embedded in the TwiML stream URL;
``/ws/twilio/{call_id}`` requires it, so knowing a call id alone is not enough
to hijack a call's audio (Twilio can't send auth headers on the stream).
"""

import asyncio
import secrets as _secrets
from dataclasses import dataclass, field

# 100 x 20ms frames = 2s of buffered audio per direction, well past jitter but
# small enough that a wedged consumer costs ~380KB, not unbounded growth.
_QUEUE_MAX = 100


@dataclass
class CallBridge:
    call_id: str
    user_id: str  # owner (Supabase sub, as carried by WsAuth) — only they may join
    secret: str
    # Twilio's CallSid, stashed once the call is placed. Lets the browser session's
    # teardown hang up the PSTN leg without a DB read when the tab closes mid-call.
    twilio_call_sid: str | None = None
    to_callee: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(_QUEUE_MAX))
    to_browser: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(_QUEUE_MAX))
    closed: bool = False

    def _push(self, queue: asyncio.Queue, frame: bytes) -> None:
        if self.closed:
            return
        while True:
            try:
                queue.put_nowait(frame)
                return
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()  # drop-oldest
                except asyncio.QueueEmpty:  # raced a consumer; retry the put
                    pass

    def push_to_callee(self, payload: bytes) -> None:
        self._push(self.to_callee, payload)

    def push_to_browser(self, frame: bytes) -> None:
        self._push(self.to_browser, frame)

    def close(self) -> None:
        """Mark closed and wake both drainers. Idempotent."""
        if self.closed:
            return
        self.closed = True
        for queue in (self.to_callee, self.to_browser):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                # Full of frames nobody will drain; clear one slot for the sentinel.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(None)


_bridges: dict[str, CallBridge] = {}


def create(call_id: str, user_id: str) -> CallBridge:
    bridge = CallBridge(call_id=call_id, user_id=user_id, secret=_secrets.token_urlsafe(16))
    _bridges[call_id] = bridge
    return bridge


def get(call_id: str) -> CallBridge | None:
    return _bridges.get(call_id)


def close(call_id: str) -> None:
    bridge = _bridges.pop(call_id, None)
    if bridge is not None:
        bridge.close()
