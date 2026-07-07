"""Twilio media stream + call bridge end-to-end (M8a).

Drives both WebSockets with Starlette's TestClient (same event loop, so the
bridge queues wake correctly): a fake Twilio speaks the Media Streams JSON
protocol at ``/ws/twilio/{call_id}`` while a browser session on ``/ws/voice``
joins the same bridge. Audio must flow browser -> phone (converted frames out
as mu-law media events) and phone -> browser (media payloads back as 48k PCM
binary frames). No network, no Twilio account; inference is the same in-process
passthrough gRPC server the /ws/voice tests use.
"""

import asyncio
import base64
import json
import struct
import time
from collections.abc import Iterator
from concurrent import futures
from contextlib import contextmanager
from uuid import uuid4

import grpc
import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.calls import bridge as bridges
from app.calls.media import twilio_media_stream
from app.calls.telephony import mulaw_encode
from app.config import settings
from app.db.models import Plan
from app.main import app
from app.proto_gen import audio_pb2, audio_pb2_grpc
from app.rate_limit import RateLimiter
from app.websocket.auth import WsAuth
from app.websocket.handler import voice_stream

SECRET = "test-secret-at-least-32-characters-long-000"


def _mint(sub: str) -> str:
    now = int(time.time())
    payload = {"sub": sub, "aud": "authenticated", "iat": now, "exp": now + 3600}
    return jwt.encode(payload, SECRET, algorithm="HS256")


@pytest.fixture
def _free_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _plan(_user_id) -> Plan:
        return Plan.FREE

    monkeypatch.setattr("app.websocket.auth.load_user_plan", _plan)


class _PassthroughServicer(audio_pb2_grpc.VoiceConversionServicer):
    def Convert(self, request_iterator, context):  # noqa: N802 - gRPC method name
        for frame in request_iterator:
            yield audio_pb2.AudioFrame(
                pcm=frame.pcm, sample_rate=frame.sample_rate, model_id=frame.model_id
            )


@contextmanager
def _passthrough_inference() -> Iterator[str]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    audio_pb2_grpc.add_VoiceConversionServicer_to_server(_PassthroughServicer(), server)
    port = server.add_insecure_port("localhost:0")
    server.start()
    try:
        yield f"localhost:{port}"
    finally:
        server.stop(grace=None)


def _frame_20ms(value: int = 1000) -> bytes:
    return struct.pack("<960h", *([value] * 960))


def test_media_stream_rejects_bad_secret() -> None:
    call_id = uuid4().hex
    bridges.create(call_id, user_id="someone")
    client = TestClient(app)
    try:
        with pytest.raises(WebSocketDisconnect) as ei:
            with client.websocket_connect(f"/ws/twilio/{call_id}?secret=wrong") as ws:
                ws.receive_text()
        assert ei.value.code == 1008
    finally:
        bridges.close(call_id)


def test_media_stream_rejects_unknown_call() -> None:
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(f"/ws/twilio/{uuid4().hex}?secret=x") as ws:
            ws.receive_text()
    assert ei.value.code == 1008


def test_join_call_requires_auth_and_ownership(monkeypatch: pytest.MonkeyPatch, _free_plan) -> None:
    monkeypatch.setattr(settings, "supabase_jwt_secret", SECRET)
    owner_id = str(uuid4())
    call_id = uuid4().hex
    bridges.create(call_id, user_id=owner_id)
    client = TestClient(app)
    try:
        # Anonymous session: join refused.
        with client.websocket_connect("/ws/voice") as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "join_call", "callId": call_id})
            reply = ws.receive_json()
            assert reply["type"] == "error"
            assert reply["code"] == "auth_required"

        # Authenticated as a *different* user: indistinguishable from no call.
        with client.websocket_connect(f"/ws/voice?token={_mint(str(uuid4()))}") as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "join_call", "callId": call_id})
            reply = ws.receive_json()
            assert reply["type"] == "error"
            assert reply["code"] == "call_not_found"
    finally:
        bridges.close(call_id)


class _FakeWS:
    """Minimal in-loop stand-in for a Starlette WebSocket.

    The end-to-end bridge test cannot use two TestClient sockets: each
    ``websocket_connect`` runs on its *own* event loop, so the bridge's asyncio
    queues would be shared across loops and their getters never wake (a
    test-harness artifact — production runs one uvicorn loop). Driving both
    handlers as tasks on the test's single loop matches production semantics.
    """

    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.close_code: int | None = None

    async def accept(self) -> None:
        pass

    async def receive(self) -> dict:
        return await self.inbox.get()

    async def receive_json(self) -> dict:
        message = await self.inbox.get()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(code=1000)
        return json.loads(message["text"])

    async def send_json(self, payload: dict) -> None:
        await self.outbox.put(("json", payload))

    async def send_bytes(self, data: bytes) -> None:
        await self.outbox.put(("bytes", data))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_code = code

    # -- test-side helpers ----------------------------------------------------

    def send_client_json(self, payload: dict) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(payload)})

    def send_client_bytes(self, data: bytes) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "bytes": data})

    def disconnect(self) -> None:
        self.inbox.put_nowait({"type": "websocket.disconnect"})

    async def expect(self, kind: str):
        got_kind, payload = await asyncio.wait_for(self.outbox.get(), timeout=5)
        assert got_kind == kind, (got_kind, payload)
        return payload


async def test_call_bridge_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    owner_id = str(uuid4())
    call_id = uuid4().hex
    bridge = bridges.create(call_id, user_id=owner_id)

    with _passthrough_inference() as addr:
        browser = _FakeWS()
        twi = _FakeWS()
        auth = WsAuth(outcome="authenticated", user_id=owner_id)
        browser_task = asyncio.create_task(
            voice_stream(browser, addr, auth=auth, limiter=RateLimiter(None))
        )
        twilio_task = asyncio.create_task(twilio_media_stream(twi, call_id, bridge.secret))
        try:
            assert (await browser.expect("json"))["type"] == "ready"
            browser.send_client_json({"type": "start", "sampleRate": 48000})
            assert (await browser.expect("json"))["type"] == "ready"

            browser.send_client_json({"type": "join_call", "callId": call_id})
            assert await browser.expect("json") == {"type": "call_joined", "callId": call_id}

            # Twilio's protocol: connected, then start with the streamSid.
            twi.send_client_json({"event": "connected", "protocol": "Call"})
            twi.send_client_json({"event": "start", "start": {"streamSid": "MZ0123"}})

            # Browser mic frame -> inference (passthrough) -> phone leg as a
            # mu-law media event, NOT echoed back to the browser.
            browser.send_client_bytes(_frame_20ms(2000))
            media = await twi.expect("json")
            assert media["event"] == "media"
            assert media["streamSid"] == "MZ0123"
            assert len(base64.b64decode(media["media"]["payload"])) == 160  # 20ms @ 8kHz

            # Callee audio -> browser as a 48kHz PCM binary frame.
            callee = base64.b64encode(mulaw_encode([500] * 160)).decode()
            twi.send_client_json({"event": "media", "media": {"payload": callee}})
            frame = await browser.expect("bytes")
            assert len(frame) == 1920

            # Twilio ends the stream: bridge closes, media handler returns, and
            # the browser session reverts to the plain echo loop.
            twi.send_client_json({"event": "stop"})
            await asyncio.wait_for(twilio_task, timeout=5)
            assert bridge.closed

            # The gateway tells the browser the call ended (so the dialer can tear
            # its UI down) before the socket reverts to echo.
            assert await browser.expect("json") == {"type": "call_ended", "callId": call_id}

            deadline = time.monotonic() + 5
            while True:
                browser.send_client_bytes(_frame_20ms(3000))
                got = await browser.expect("bytes")
                if got == _frame_20ms(3000):
                    break
                assert time.monotonic() < deadline, "never reverted to echo"

            browser.disconnect()
            await asyncio.wait_for(browser_task, timeout=5)
        finally:
            for task in (browser_task, twilio_task):
                if not task.done():
                    task.cancel()
            bridges.close(call_id)


async def test_browser_drop_hangs_up_live_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # The browser tab closes mid-call while the bridge is still open. The beacon
    # the dialer used to fire can't authenticate, so teardown must hang up the
    # PSTN leg itself: tell Twilio to end the call and close the bridge. DB-free
    # (the sid rides the bridge), so no Postgres is needed here.
    from app.calls import twilio

    monkeypatch.setattr(settings, "twilio_account_sid", "AC" + "0" * 32)
    monkeypatch.setattr(settings, "twilio_auth_token", "twilio-auth-token")
    completed: dict = {}

    async def _fake_complete(call_sid, **kwargs):
        completed["sid"] = call_sid
        return {"sid": call_sid, "status": "completed"}

    monkeypatch.setattr(twilio, "complete_call", _fake_complete)

    owner_id = str(uuid4())
    call_id = uuid4().hex
    bridge = bridges.create(call_id, user_id=owner_id)
    bridge.twilio_call_sid = "CA" + "7" * 32

    with _passthrough_inference() as addr:
        browser = _FakeWS()
        auth = WsAuth(outcome="authenticated", user_id=owner_id)
        browser_task = asyncio.create_task(
            voice_stream(browser, addr, auth=auth, limiter=RateLimiter(None))
        )
        try:
            assert (await browser.expect("json"))["type"] == "ready"
            browser.send_client_json({"type": "start", "sampleRate": 48000})
            assert (await browser.expect("json"))["type"] == "ready"
            browser.send_client_json({"type": "join_call", "callId": call_id})
            assert await browser.expect("json") == {"type": "call_joined", "callId": call_id}

            browser.disconnect()
            await asyncio.wait_for(browser_task, timeout=5)

            # Twilio was asked to end the leg, and the bridge is torn down.
            assert completed["sid"] == "CA" + "7" * 32
            assert bridge.closed
        finally:
            if not browser_task.done():
                browser_task.cancel()
            bridges.close(call_id)
