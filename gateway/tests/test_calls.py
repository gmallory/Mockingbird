"""Call routes (M8a): outbound placement, history scoping, hangup, status webhook.

Same posture as test_voices.py: routes driven via httpx.ASGITransport with the
DB session overridden onto a per-test engine (skips when Postgres is down),
``get_current_user`` overridden to a seeded owner, and the Twilio REST calls
monkeypatched — no live Twilio account or network needed. The webhook's
signature check is exercised with a real HMAC computed the way Twilio does.
"""

import base64
import hashlib
import hmac
from uuid import UUID, uuid4

import httpx
import pytest
from sqlmodel import SQLModel

from app.auth.dependencies import get_current_user
from app.calls import bridge as bridges
from app.calls import twilio
from app.config import settings
from app.db.models import CallRecord, CallStatus, User, Voice
from app.db.session import create_engine, create_session_factory, get_session
from app.main import app


async def _ensure_schema(engine) -> None:
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc}")


def _new_user() -> User:
    return User(email=f"caller-{uuid4()}@example.com", display_name="Caller")


def _configure_twilio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "twilio_account_sid", "AC" + "0" * 32)
    monkeypatch.setattr(settings, "twilio_auth_token", "twilio-auth-token")
    monkeypatch.setattr(settings, "twilio_phone_number", "+15550000001")
    monkeypatch.setattr(settings, "public_base_url", "https://tunnel.example")
    monkeypatch.setattr(settings, "enable_calling", True)


class _Env:
    """Per-test DB engine + seeded owner + app overrides, torn down completely."""

    def __init__(self) -> None:
        self.engine = create_engine()
        self.factory = create_session_factory(self.engine)
        self.owner = _new_user()

    async def setup(self) -> None:
        await _ensure_schema(self.engine)
        async with self.factory() as session:
            session.add(self.owner)
            await session.commit()

        async def _override_session():
            async with self.factory() as session:
                yield session

        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[get_current_user] = lambda: self.owner

    async def add(self, obj) -> None:
        async with self.factory() as session:
            session.add(obj)
            await session.commit()

    async def get_call(self, call_id: UUID) -> CallRecord | None:
        async with self.factory() as session:
            return await session.get(CallRecord, call_id)

    async def teardown(self) -> None:
        app.dependency_overrides.clear()
        try:
            async with self.engine.begin() as conn:
                from sqlalchemy import text

                await conn.execute(
                    text("DELETE FROM callrecord WHERE user_id = :uid"), {"uid": self.owner.id}
                )
                await conn.execute(
                    text("DELETE FROM voice WHERE user_id = :uid"), {"uid": self.owner.id}
                )
                await conn.execute(
                    text('DELETE FROM "user" WHERE id = :uid'), {"uid": self.owner.id}
                )
        except Exception:  # noqa: BLE001 - teardown after a skip has no schema
            pass
        finally:
            await self.engine.dispose()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_outbound_call_places_and_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _Env()
    created = {}
    try:
        await env.setup()
        _configure_twilio(monkeypatch)

        async def _fake_create_call(**kwargs):
            created.update(kwargs)
            return {"sid": "CA" + "1" * 32}

        monkeypatch.setattr(twilio, "create_call", _fake_create_call)

        async with _client() as client:
            resp = await client.post("/api/calls/outbound", json={"phone_number": "+15551234567"})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "active"
            assert body["phone_number"] == "+15551234567"
            assert body["twilio_call_sid"] == "CA" + "1" * 32

        # Twilio was asked to dial the number from our caller id, with TwiML
        # opening the media stream back to this call's endpoint + secret.
        assert created["to"] == "+15551234567"
        assert created["from_"] == "+15550000001"
        bridge = bridges.get(body["id"])
        assert bridge is not None
        assert (
            f"wss://tunnel.example/ws/twilio/{body['id']}?secret={bridge.secret}"
            in created["twiml"]
        )
        assert created["status_callback"] == "https://tunnel.example/api/twilio/status"
    finally:
        if "id" in (body := locals().get("body", {})):
            bridges.close(body["id"])
        await env.teardown()


async def test_outbound_unconfigured_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    # No DB or Twilio needed: the config gate fires before anything else.
    async def _override_session():
        yield object()

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user] = lambda: _new_user()
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    monkeypatch.setattr(settings, "public_base_url", "")
    try:
        async with _client() as client:
            resp = await client.post("/api/calls/outbound", json={"phone_number": "+15551234567"})
            assert resp.status_code == 503
            assert "TWILIO_ACCOUNT_SID" in resp.json()["detail"]

            monkeypatch.setattr(settings, "enable_calling", False)
            resp = await client.post("/api/calls/outbound", json={"phone_number": "+15551234567"})
            assert resp.status_code == 503
            assert "disabled" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


async def test_outbound_rejects_non_e164(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _override_session():
        yield object()

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user] = lambda: _new_user()
    _configure_twilio(monkeypatch)
    try:
        async with _client() as client:
            for bad in ("5551234567", "+0123", "not-a-number", "+1 555 123"):
                resp = await client.post("/api/calls/outbound", json={"phone_number": bad})
                assert resp.status_code == 400, bad
    finally:
        app.dependency_overrides.clear()


async def test_outbound_unknown_voice_404(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _Env()
    try:
        await env.setup()
        _configure_twilio(monkeypatch)
        async with _client() as client:
            resp = await client.post(
                "/api/calls/outbound",
                json={"phone_number": "+15551234567", "voice_id": str(uuid4())},
            )
            assert resp.status_code == 404
    finally:
        await env.teardown()


async def test_outbound_twilio_failure_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _Env()
    try:
        await env.setup()
        _configure_twilio(monkeypatch)

        async def _boom(**kwargs):
            raise twilio.TwilioError("twilio down")

        monkeypatch.setattr(twilio, "create_call", _boom)

        async with _client() as client:
            resp = await client.post("/api/calls/outbound", json={"phone_number": "+15551234567"})
            assert resp.status_code == 502

            # The record survives as a failed row in the history.
            listed = await client.get("/api/calls")
            assert listed.status_code == 200
            assert [c["status"] for c in listed.json()] == ["failed"]
            # And its bridge was torn down.
            assert bridges.get(listed.json()[0]["id"]) is None
    finally:
        await env.teardown()


async def test_calls_scoped_per_user_and_hangup(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _Env()
    stranger = _new_user()
    completed = {}
    try:
        await env.setup()
        await env.add(stranger)
        _configure_twilio(monkeypatch)

        record = CallRecord(
            user_id=env.owner.id,
            phone_number="+15559876543",
            twilio_call_sid="CA" + "2" * 32,
        )
        await env.add(record)

        async def _fake_complete(call_sid, **kwargs):
            completed["sid"] = call_sid
            return {"sid": call_sid, "status": "completed"}

        monkeypatch.setattr(twilio, "complete_call", _fake_complete)

        async with _client() as client:
            # Owner sees the call; a stranger sees neither the list entry nor the row.
            assert any(c["id"] == str(record.id) for c in (await client.get("/api/calls")).json())
            assert (await client.get(f"/api/calls/{record.id}")).status_code == 200

            app.dependency_overrides[get_current_user] = lambda: stranger
            assert all(c["id"] != str(record.id) for c in (await client.get("/api/calls")).json())
            assert (await client.get(f"/api/calls/{record.id}")).status_code == 404
            assert (await client.post(f"/api/calls/{record.id}/hangup")).status_code == 404

            # Owner hangs up: Twilio told, record closed out.
            app.dependency_overrides[get_current_user] = lambda: env.owner
            resp = await client.post(f"/api/calls/{record.id}/hangup")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "completed"
            assert body["ended_at"] is not None
            assert completed["sid"] == "CA" + "2" * 32

            # Hanging up again is a no-op, not an error.
            assert (await client.post(f"/api/calls/{record.id}/hangup")).status_code == 200
    finally:
        try:
            async with env.factory() as session:
                stored = await session.get(User, stranger.id)
                if stored is not None:
                    await session.delete(stored)
                    await session.commit()
        except Exception:  # noqa: BLE001 - cleanup after a skip has no schema
            pass
        await env.teardown()


def _sign(url: str, params: dict[str, str], token: str) -> str:
    payload = url + "".join(k + v for k, v in sorted(params.items()))
    return base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()


async def test_status_webhook_closes_call(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _Env()
    try:
        await env.setup()
        _configure_twilio(monkeypatch)
        record = CallRecord(
            user_id=env.owner.id,
            phone_number="+15559876543",
            twilio_call_sid="CA" + "3" * 32,
        )
        await env.add(record)

        url = "https://tunnel.example/api/twilio/status"
        form = {
            "CallSid": record.twilio_call_sid,
            "CallStatus": "completed",
            "CallDuration": "42",
        }

        async with _client() as client:
            # Wrong signature -> rejected, record untouched.
            resp = await client.post(
                "/api/twilio/status", data=form, headers={"X-Twilio-Signature": "bogus"}
            )
            assert resp.status_code == 403
            assert (await env.get_call(record.id)).status == CallStatus.ACTIVE

            # Valid signature -> record closed with Twilio's duration.
            resp = await client.post(
                "/api/twilio/status",
                data=form,
                headers={"X-Twilio-Signature": _sign(url, form, "twilio-auth-token")},
            )
            assert resp.status_code == 204
            stored = await env.get_call(record.id)
            assert stored.status == CallStatus.COMPLETED
            assert stored.duration_sec == 42.0
            assert stored.ended_at is not None

            # A progress event (or unknown sid) is acknowledged and ignored.
            form2 = {"CallSid": "CA" + "9" * 32, "CallStatus": "ringing"}
            resp = await client.post(
                "/api/twilio/status",
                data=form2,
                headers={"X-Twilio-Signature": _sign(url, form2, "twilio-auth-token")},
            )
            assert resp.status_code == 204
    finally:
        await env.teardown()


async def test_calls_require_auth() -> None:
    async with _client() as client:
        assert (await client.get("/api/calls")).status_code == 401
        assert (
            await client.post("/api/calls/outbound", json={"phone_number": "+15551234567"})
        ).status_code == 401
        assert (await client.post(f"/api/calls/{uuid4()}/hangup")).status_code == 401


async def test_outbound_with_owned_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _Env()
    call_id = None
    try:
        await env.setup()
        _configure_twilio(monkeypatch)
        voice = Voice(voice_id=f"vid-{uuid4()}", label="Me", language="en", user_id=env.owner.id)
        await env.add(voice)

        async def _fake_create_call(**kwargs):
            return {"sid": "CA" + "4" * 32}

        monkeypatch.setattr(twilio, "create_call", _fake_create_call)

        async with _client() as client:
            resp = await client.post(
                "/api/calls/outbound",
                json={"phone_number": "+15551234567", "voice_id": str(voice.id)},
            )
            assert resp.status_code == 200
            call_id = resp.json()["id"]
            assert resp.json()["voice_id"] == str(voice.id)
    finally:
        if call_id:
            bridges.close(call_id)
        await env.teardown()
