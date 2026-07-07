"""The ``/ws/voice`` endpoint.

Binary PCM frames are proxied to the inference service over a gRPC ``Convert``
stream; converted frames are sent back to the browser. The control channel
(start / switch_model / stop / ping) is unchanged.

**Auth + limits (M6b).** The connection is classified before the socket is
accepted (:func:`app.websocket.auth.resolve_ws_auth`): a valid token yields an
*authenticated* session (rate-limited, may select a voice); no token yields an
*anonymous* echo-only demo session (the model id is pinned to echo, so no
conversion and no limits); a bad token — or a missing one when
``WS_REQUIRE_AUTH`` is set — is *rejected* and the socket is closed with 4001.
Authenticated sessions claim a per-user concurrency slot on open (over the plan
cap -> close 4029) and record their duration against the monthly usage counter on
close. Rate limiting is fail-open: a Redis outage admits the session unenforced.

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
import time
from dataclasses import dataclass
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.inference.client import InferenceSession, InferenceUnavailable
from app.metrics import (
    WS_DEGRADED_TOTAL,
    WS_FIRST_OUTPUT_SECONDS,
    WS_FRAMES_IN,
    WS_FRAMES_OUT,
    WS_SESSION_SECONDS,
    WS_SESSIONS_ACTIVE,
    WS_SESSIONS_TOTAL,
)
from app.rate_limit import RateLimiter
from app.websocket.auth import WsAuth
from app.websocket.protocol import (
    DegradedMessage,
    ErrorMessage,
    ModelLoadedMessage,
    PongMessage,
    ReadyMessage,
    client_message_adapter,
)

# App-defined WebSocket close codes (4000-4999). Match agents/gateway.agent.md.
WS_CLOSE_UNAUTHORIZED = 4001
WS_CLOSE_RATE_LIMITED = 4029


@dataclass
class _SessionState:
    authenticated: bool = False
    sample_rate: int = 48000
    model_id: str = ""
    degraded: bool = False


async def voice_stream(
    websocket: WebSocket,
    grpc_url: str,
    *,
    auth: WsAuth,
    limiter: RateLimiter,
    timeout_s: float = 2.0,
) -> None:
    # Accept first, then enforce. A WebSocket close only carries an app-defined
    # code (4001/4029) once the handshake has completed; closing *before* accept
    # would reach the browser as a generic 1006, and the worker couldn't tell a
    # terminal rejection from a flaky link (it would reconnect-storm instead of
    # stopping). The gate below runs before the receive loop, so nothing the
    # client sends in the meantime is ever read or acted on.
    await websocket.accept()

    # --- Auth + rate-limit gate (before any frame is processed) -------------
    if auth.outcome == "rejected":
        WS_SESSIONS_TOTAL.labels(outcome="rejected").inc()
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason=auth.reason)
        return

    session_id = uuid4().hex
    acquired = False
    started_at = 0.0
    # `and user_id is not None` narrows the type without an `assert` (assertions
    # are stripped under `python -O`); resolve_ws_auth always sets user_id when
    # authenticated, so this is the same invariant the finally block guards on.
    if auth.authenticated and auth.user_id is not None:
        result = await limiter.acquire(auth.user_id, auth.plan, session_id)
        if not result.ok:
            WS_SESSIONS_TOTAL.labels(outcome="rate_limited").inc()
            with contextlib.suppress(Exception):
                await websocket.close(code=WS_CLOSE_RATE_LIMITED, reason=result.reason)
            return
        acquired = True
        started_at = time.monotonic()

    mode = "authenticated" if auth.authenticated else "anonymous"

    state = _SessionState(authenticated=auth.authenticated)
    session = InferenceSession(grpc_url, timeout_s=timeout_s)
    out_lock = asyncio.Lock()
    reader_task: asyncio.Task | None = None
    first_in_at: float | None = None  # set on the session's first inbound audio frame

    async def send_json(payload: dict) -> None:
        async with out_lock:
            await websocket.send_json(payload)

    async def send_bytes(data: bytes) -> None:
        async with out_lock:
            await websocket.send_bytes(data)
        # Count only frames the socket actually accepted: a failed send (client
        # disconnect, close race) must not inflate the "frames out" counter.
        WS_FRAMES_OUT.inc()

    async def degrade() -> None:
        # Idempotent: notify once, then audio is passed through unchanged. The
        # notice send is best-effort — if the socket is already closing, swallow
        # it so callers (the reader task included) never raise from degrading.
        if state.degraded:
            return
        state.degraded = True
        WS_DEGRADED_TOTAL.inc()
        with contextlib.suppress(Exception):
            await send_json(DegradedMessage().model_dump())

    async def reader() -> None:
        # Drains converted frames concurrently with the receive loop. Any exit
        # that is not a teardown cancellation means inference stopped producing
        # usable output — a clean EOF (server closed the stream mid-session) just
        # as much as a failure — so fall back to passthrough. CancelledError is a
        # BaseException, so teardown cancellation propagates past these handlers
        # and skips the degrade below.
        first_output_pending = True
        try:
            async for out in session.outputs():
                if first_output_pending and first_in_at is not None:
                    WS_FIRST_OUTPUT_SECONDS.observe(time.monotonic() - first_in_at)
                    first_output_pending = False
                await send_bytes(out)
        except InferenceUnavailable:
            pass
        except Exception:  # noqa: BLE001 - unexpected reader failure still degrades
            pass
        await degrade()

    # Inc directly before the try whose finally holds the dec, with nothing
    # fallible in between — any other placement can drift the gauge.
    WS_SESSIONS_TOTAL.labels(outcome="accepted").inc()
    WS_SESSIONS_ACTIVE.labels(mode=mode).inc()
    session_opened_at = time.monotonic()
    try:
        # First message should be `start`, but we stay lenient: signal readiness.
        await send_json(ReadyMessage().model_dump())

        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            data_bytes = message.get("bytes")
            if data_bytes is not None:
                WS_FRAMES_IN.inc()
                if first_in_at is None:
                    first_in_at = time.monotonic()
                if state.degraded:
                    await send_bytes(data_bytes)  # passthrough echo
                    continue
                if reader_task is None:
                    # Lazily dial inference and start the reader on the first frame.
                    try:
                        await session.open()
                    except InferenceUnavailable:
                        await degrade()
                        await send_bytes(data_bytes)
                        continue
                    reader_task = asyncio.create_task(reader())
                try:
                    await session.send(data_bytes, state.sample_rate, state.model_id)
                except InferenceUnavailable:
                    # Stop the reader before degrading: a write that timed out
                    # while reads still flow would otherwise keep bursting
                    # converted frames that interleave with the passthrough echo.
                    await degrade()
                    reader_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await reader_task
                    await send_bytes(data_bytes)
                continue

            text = message.get("text")
            if text is not None:
                if await _handle_control(send_json, state, text):
                    break  # stop requested

    except WebSocketDisconnect:
        pass
    finally:
        # Metrics first, before any await below — an exception or cancellation
        # delivered mid-teardown (uvicorn reload with open sessions) must not
        # skip the dec, or the active gauge drifts upward for good.
        WS_SESSIONS_ACTIVE.labels(mode=mode).dec()
        WS_SESSION_SECONDS.observe(time.monotonic() - session_opened_at)
        # Cancel the reader before closing the socket so its send path can never
        # race websocket.close() — out_lock serializes sends, not the close.
        if reader_task is not None:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task
        await session.aclose()
        with contextlib.suppress(Exception):
            await websocket.close()
        # Release the concurrency slot and bank this session's duration. Both are
        # best-effort inside the limiter, so a Redis outage here never raises.
        if acquired and auth.user_id is not None:
            await limiter.release(auth.user_id, session_id)
            await limiter.record_usage(auth.user_id, time.monotonic() - started_at)


async def _handle_control(send_json, state: _SessionState, text: str) -> bool:
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
        # Anonymous sessions are echo-only: pin the model to "" so a demo visitor
        # can never drive conversion with someone else's voice id. Voices are
        # per-user (M6a), so an authenticated caller's id rides each frame as usual.
        state.model_id = (msg.modelId or "") if state.authenticated else ""
        await send_json(ReadyMessage().model_dump())
    elif msg.type == "switch_model":
        if not state.authenticated:
            await send_json(
                ErrorMessage(code="auth_required", message="log in to select a voice").model_dump()
            )
            return False
        # The selected voice id rides on each subsequent audio frame's model_id;
        # ack so the client knows the gateway will convert to it from now on.
        state.model_id = msg.modelId
        await send_json(ModelLoadedMessage(modelId=msg.modelId).model_dump())
    elif msg.type == "stop":
        # The receive loop breaks on True; voice_stream's finally closes the
        # socket after the reader is cancelled, so the close is never raced.
        return True
    return False
