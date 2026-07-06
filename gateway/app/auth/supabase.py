"""GoTrue (Supabase Auth) REST client — signup + login proxy (M6a, off hot path).

Mirrors ``app/inference/http.py``: a thin httpx wrapper the auth routes call to
exchange email/password for a Supabase session. Only signup/login go through here
— token *verification* is offline (see ``app/auth/jwt.py``). On any transport or
HTTP error this raises :class:`SupabaseAuthError` carrying an HTTP status so the
route maps it honestly (400 bad signup, 400/401 bad login, 502/503 upstream
trouble) instead of leaking httpx internals.
"""

import httpx

from app.config import settings


class SupabaseAuthError(Exception):
    """A GoTrue call failed. ``status_code`` is what the route should return."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    # Hosted Supabase fronts GoTrue with an API gateway that requires the anon key;
    # harmless when talking to GoTrue directly. Only send it when configured.
    if settings.supabase_anon_key:
        headers["apikey"] = settings.supabase_anon_key
    return headers


def _error_detail(resp: httpx.Response) -> str:
    """Pull a human message out of a GoTrue error body (shape varies by version)."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text or f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        return (
            body.get("error_description")
            or body.get("msg")
            or body.get("message")
            or body.get("error")
            or f"HTTP {resp.status_code}"
        )
    return f"HTTP {resp.status_code}"


async def _post(path: str, json: dict, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    if not settings.supabase_url:
        raise SupabaseAuthError("auth is not configured (SUPABASE_URL unset)", 503)
    base = settings.supabase_url.rstrip("/") + "/auth/v1"
    async with httpx.AsyncClient(
        base_url=base,
        transport=transport,
        timeout=httpx.Timeout(15.0, connect=10.0),
        headers=_headers(),
    ) as client:
        try:
            resp = await client.post(path, json=json)
        except httpx.HTTPError as exc:
            raise SupabaseAuthError(f"supabase unreachable: {exc}", 502) from exc
    if resp.status_code >= 400:
        # Bubble the upstream status so bad credentials surface as 400/401 to the
        # browser, not a blanket 502 that looks like our fault.
        raise SupabaseAuthError(_error_detail(resp), resp.status_code)
    try:
        return resp.json()
    except ValueError as exc:
        raise SupabaseAuthError(f"supabase returned a non-JSON body: {exc}", 502) from exc


async def signup(
    email: str,
    password: str,
    display_name: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Register a user with GoTrue; returns its session/user JSON."""
    body: dict = {"email": email, "password": password}
    if display_name:
        # GoTrue copies ``data`` into the user's ``user_metadata``, where our token
        # verifier later reads the display name from.
        body["data"] = {"display_name": display_name}
    return await _post("/signup", body, transport)


async def login(
    email: str,
    password: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Exchange email/password for a session via the password grant."""
    return await _post(
        "/token?grant_type=password", {"email": email, "password": password}, transport
    )
