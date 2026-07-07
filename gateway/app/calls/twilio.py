"""Twilio REST client + webhook signature validation (M8a).

A thin httpx wrapper, same posture as the GoTrue client (``app/auth/supabase.py``):
no vendor SDK, an injectable transport so tests stay offline, and one exception
type (:class:`TwilioError`) so routes surface a clean 502 instead of leaking
httpx internals. Only the two calls M8a needs exist: create an outbound call and
complete (hang up) a live one.
"""

import base64
import hashlib
import hmac

import httpx

TWILIO_API = "https://api.twilio.com"


class TwilioError(Exception):
    """The Twilio REST call failed; the route should surface a 502."""


async def _post(
    path: str,
    data: dict,
    *,
    account_sid: str,
    auth_token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    async with httpx.AsyncClient(
        base_url=TWILIO_API,
        auth=(account_sid, auth_token),
        transport=transport,
        timeout=httpx.Timeout(15.0, connect=5.0),
    ) as client:
        try:
            resp = await client.post(path, data=data)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # ValueError covers a 2xx with a non-JSON body, same as the
            # inference HTTP client.
            raise TwilioError(str(exc)) from exc


async def create_call(
    *,
    to: str,
    from_: str,
    twiml: str,
    status_callback: str,
    account_sid: str,
    auth_token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """POST /Calls: dial ``to`` and run ``twiml`` when answered. Returns Twilio's JSON."""
    return await _post(
        f"/2010-04-01/Accounts/{account_sid}/Calls.json",
        {
            "To": to,
            "From": from_,
            "Twiml": twiml,
            "StatusCallback": status_callback,
            # Fire the callback on every lifecycle edge so a busy/no-answer call
            # is closed out in the DB, not just a completed one.
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
        },
        account_sid=account_sid,
        auth_token=auth_token,
        transport=transport,
    )


async def complete_call(
    call_sid: str,
    *,
    account_sid: str,
    auth_token: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """POST /Calls/{sid}: force Status=completed (hang up a live call)."""
    return await _post(
        f"/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json",
        {"Status": "completed"},
        account_sid=account_sid,
        auth_token=auth_token,
        transport=transport,
    )


def validate_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    """Check a webhook's ``X-Twilio-Signature``.

    Twilio signs the exact URL it requested plus the form params sorted by key,
    HMAC-SHA1 with the account's auth token, base64-encoded.
    """
    payload = url + "".join(k + v for k, v in sorted(params.items()))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")
