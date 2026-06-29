"""Frontend FastAPI app.

Serves a single Live Monitor page plus the static browser audio-engine assets.
The page wires up mic capture -> WebSocket -> playback against the gateway echo
endpoint and shows a live roundtrip-latency readout.
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
async def monitor(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "pages/monitor.html",
        {"public_ws_url": settings.public_ws_url},
    )
