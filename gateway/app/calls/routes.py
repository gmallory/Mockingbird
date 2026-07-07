"""Call routes (M8a): outbound PSTN via Twilio + call history + status webhook.

``POST /api/calls/outbound`` persists a :class:`CallRecord`, registers a media
bridge, and asks Twilio to dial the number with TwiML that opens a bidirectional
Media Stream back to ``/ws/twilio/{call_id}``. The browser then joins the same
bridge over the existing ``/ws/voice`` session (``join_call`` control message),
so its *converted* audio reaches the callee and the callee's audio reaches the
browser. History and hangup are conventional CRUD on the record.

``POST /api/twilio/status`` is Twilio's status callback (form-encoded, signature
validated) — the authoritative "call ended" signal that closes the record and
the bridge even if the user just closes the tab.
"""

import re
from datetime import UTC, datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth.dependencies import get_current_user
from app.calls import bridge as bridges
from app.calls import twilio
from app.config import settings
from app.db.models import CallRecord, CallStatus, User, Voice
from app.db.session import get_session

log = structlog.get_logger(__name__)

router = APIRouter()

_E164 = re.compile(r"^\+[1-9]\d{1,14}$")

# Twilio's terminal CallStatus values -> ours. Everything else (queued, ringing,
# initiated, answered, in-progress) is a progress edge the record ignores.
_TERMINAL = {
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.FAILED,
    "failed": CallStatus.FAILED,
    "no-answer": CallStatus.FAILED,
    "canceled": CallStatus.FAILED,
}


class OutboundCallRequest(BaseModel):
    phone_number: str
    voice_id: UUID | None = None  # registry row (Voice.id); None -> untransformed


def _require_calling_config() -> None:
    if not settings.enable_calling:
        raise HTTPException(status_code=503, detail="calling is disabled (ENABLE_CALLING)")
    missing = [
        name
        for name, value in (
            ("TWILIO_ACCOUNT_SID", settings.twilio_account_sid),
            ("TWILIO_AUTH_TOKEN", settings.twilio_auth_token),
            ("TWILIO_PHONE_NUMBER", settings.twilio_phone_number),
            ("PUBLIC_BASE_URL", settings.public_base_url),
        )
        if not value
    ]
    if missing:
        raise HTTPException(status_code=503, detail=f"calling not configured: {', '.join(missing)}")


def _stream_url(call_id: UUID, secret: str) -> str:
    # Twilio's Media Stream is a WebSocket: derive ws(s) from the public http(s)
    # base. The per-call secret gates the endpoint (Twilio can't send headers).
    base = re.sub(r"^http", "ws", settings.public_base_url.rstrip("/"), count=1)
    return f"{base}/ws/twilio/{call_id}?secret={secret}"


@router.post("/api/calls/outbound")
async def outbound_call(
    payload: OutboundCallRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CallRecord:
    """Place an outbound PSTN call; returns the record whose id the browser joins."""
    _require_calling_config()
    if not _E164.match(payload.phone_number):
        raise HTTPException(status_code=400, detail="phone_number must be E.164 (+15551234567)")

    if payload.voice_id is not None:
        voice = await session.get(Voice, payload.voice_id)
        if voice is None or voice.user_id != user.id:
            raise HTTPException(status_code=404, detail="voice not found")

    record = CallRecord(
        user_id=user.id, voice_id=payload.voice_id, phone_number=payload.phone_number
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    bridge = bridges.create(str(record.id), str(user.id))
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
        f'<Stream url="{_stream_url(record.id, bridge.secret)}" />'
        "</Connect></Response>"
    )
    try:
        result = await twilio.create_call(
            to=payload.phone_number,
            from_=settings.twilio_phone_number,
            twiml=twiml,
            status_callback=f"{settings.public_base_url.rstrip('/')}/api/twilio/status",
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
        )
    except twilio.TwilioError as exc:
        bridges.close(str(record.id))
        record.status = CallStatus.FAILED
        record.ended_at = datetime.now(UTC)
        await session.commit()
        raise HTTPException(status_code=502, detail=f"twilio call failed: {exc}") from exc

    record.twilio_call_sid = result.get("sid")
    bridge.twilio_call_sid = record.twilio_call_sid
    await session.commit()
    await session.refresh(record)
    return record


@router.get("/api/calls")
async def list_calls(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[CallRecord]:
    """The caller's call history, newest first."""
    result = await session.execute(
        select(CallRecord)
        .where(CallRecord.user_id == user.id)
        .order_by(CallRecord.started_at.desc())  # type: ignore[attr-defined]
        .limit(50)
    )
    return list(result.scalars().all())


async def _owned_call(call_id: UUID, user: User, session: AsyncSession) -> CallRecord:
    record = await session.get(CallRecord, call_id)
    if record is None or record.user_id != user.id:
        raise HTTPException(status_code=404, detail="call not found")
    return record


@router.get("/api/calls/{call_id}")
async def get_call(
    call_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CallRecord:
    return await _owned_call(call_id, user, session)


@router.post("/api/calls/{call_id}/hangup")
async def hangup_call(
    call_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CallRecord:
    """End a live call. Twilio is told best-effort; the record closes regardless."""
    record = await _owned_call(call_id, user, session)
    if record.status == CallStatus.ACTIVE:
        if record.twilio_call_sid:
            try:
                await twilio.complete_call(
                    record.twilio_call_sid,
                    account_sid=settings.twilio_account_sid,
                    auth_token=settings.twilio_auth_token,
                )
            except twilio.TwilioError as exc:
                # The status callback will still close the row if Twilio ends the
                # call on its own; don't block the user's hangup on a flaky API.
                log.warning("calls.hangup_twilio_failed", call_id=str(call_id), error=str(exc))
        _finish(record, CallStatus.COMPLETED)
        bridges.close(str(record.id))
        await session.commit()
        await session.refresh(record)
    return record


async def hang_up_bridge(call_bridge: bridges.CallBridge) -> None:
    """End a call because its browser session dropped (tab closed / WS lost).

    Best-effort: close the bridge (stops buffering, wakes both media drainers)
    and tell Twilio to end the PSTN leg so the callee isn't left connected to a
    session no one is driving. The record is closed by the status callback Twilio
    fires when the leg actually ends (the authoritative signal), so this path
    needs no DB access — which is why the ``/ws/voice`` teardown can call it.
    """
    bridges.close(call_bridge.call_id)
    if call_bridge.twilio_call_sid and settings.twilio_auth_token:
        try:
            await twilio.complete_call(
                call_bridge.twilio_call_sid,
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
            )
        except twilio.TwilioError as exc:
            log.warning(
                "calls.session_drop_hangup_failed",
                call_id=call_bridge.call_id,
                error=str(exc),
            )


def _finish(record: CallRecord, status: CallStatus, duration: float | None = None) -> None:
    record.status = status
    record.ended_at = datetime.now(UTC)
    if duration is not None:
        record.duration_sec = duration
    elif record.started_at is not None:
        started = record.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        record.duration_sec = max(0.0, (record.ended_at - started).total_seconds())


@router.post("/api/twilio/status")
async def twilio_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Twilio status callback: close out the record on a terminal CallStatus.

    Unauthenticated by necessity (Twilio is the caller); the X-Twilio-Signature
    header is validated against the auth token instead. Progress events and
    unknown SIDs are acknowledged with 204 so Twilio doesn't retry them.
    """
    form = {k: str(v) for k, v in (await request.form()).items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    # Twilio signed the public URL it POSTed to, not whatever host header the
    # tunnel rewrote; reconstruct it from config.
    url = f"{settings.public_base_url.rstrip('/')}/api/twilio/status"
    if not settings.twilio_auth_token or not twilio.validate_signature(
        settings.twilio_auth_token, url, form, signature
    ):
        raise HTTPException(status_code=403, detail="invalid twilio signature")

    status = _TERMINAL.get(form.get("CallStatus", ""))
    call_sid = form.get("CallSid", "")
    if status is None or not call_sid:
        return Response(status_code=204)

    result = await session.execute(select(CallRecord).where(CallRecord.twilio_call_sid == call_sid))
    record = result.scalars().first()
    if record is not None and record.status == CallStatus.ACTIVE:
        duration = None
        if form.get("CallDuration", "").isdigit():
            duration = float(form["CallDuration"])
        _finish(record, status, duration)
        bridges.close(str(record.id))
        await session.commit()
    return Response(status_code=204)
