"""Frontend FastAPI app.

Serves the Dashboard (landing page, M10), Live Monitor, Voice Studio, Dialer,
Settings (M10), and Login pages plus the static browser audio-engine assets.
The Monitor page wires up mic capture -> WebSocket -> playback against the
gateway and shows live latency/level readouts.

**Routing note (M10):** the Dashboard now takes ``/``; Monitor moved to
``/monitor`` (it held ``/`` since M1). There is no HTTP redirect from the old
path — ``/`` still serves a real page — but the Dashboard's own quick-start
links (and the nav in ``base.html``) put "Live Monitor" one click away so
anyone with the old URL bookmarked or muscle-memory'd isn't stranded.
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Mockingbird Frontend", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    # Overview page (M10): active/recent voices + call history + quick-start
    # links. Entirely client-fetched (GET /voices, GET /api/calls) so the
    # server-rendered shell needs no gateway calls of its own and degrades the
    # same way every other page does when the gateway or Postgres is down.
    return templates.TemplateResponse(
        request,
        "pages/dashboard.html",
        {"public_gateway_url": settings.public_gateway_url},
    )


@app.get("/monitor", response_class=HTMLResponse)
async def monitor(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "pages/monitor.html",
        {
            "public_ws_url": settings.public_ws_url,
            "public_gateway_url": settings.public_gateway_url,
        },
    )


@app.get("/studio", response_class=HTMLResponse)
async def studio(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "pages/studio.html",
        {"public_gateway_url": settings.public_gateway_url},
    )


@app.get("/dialer", response_class=HTMLResponse)
async def dialer(request: Request) -> HTMLResponse:
    # Login-gated client-side like the Studio; needs both the REST base (place/
    # hang up calls) and the WS URL (the live audio session that joins the call).
    return templates.TemplateResponse(
        request,
        "pages/dialer.html",
        {
            "public_gateway_url": settings.public_gateway_url,
            "public_ws_url": settings.public_ws_url,
        },
    )


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request) -> HTMLResponse:
    # Auth is enforced client-side (M6a): the page posts credentials to the
    # gateway, stores the returned token, and gates the Studio. Serving the
    # template is unconditional.
    return templates.TemplateResponse(
        request,
        "pages/login.html",
        {"public_gateway_url": settings.public_gateway_url},
    )


@app.get("/settings", response_class=HTMLResponse)
async def user_settings(request: Request) -> HTMLResponse:
    # Login-gated client-side like Studio/Dialer (requireAuth() in auth.js).
    # Backed by the gateway's GET/PATCH /api/settings (M10).
    return templates.TemplateResponse(
        request,
        "pages/settings.html",
        {"public_gateway_url": settings.public_gateway_url},
    )
