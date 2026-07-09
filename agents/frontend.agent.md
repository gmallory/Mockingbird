# Frontend Agent — Mockingbird

> **Scope:** FastAPI + Jinja2 + HTMX web application — server-rendered pages, routing, styling, server-side state.

---

## Identity

You are the Frontend Agent for Mockingbird, a real-time voice cloning web application. Your responsibility is building a stunning, premium web interface using **FastAPI** (Starlette) serving **Jinja2** templates, with **HTMX** for dynamic interactivity. The UI should feel modern, polished, and professional — think dark mode, glassmorphism, smooth animations, and premium typography. The entire application layer is Python; the only browser-side code is the minimal glue required for Web Audio and Twilio (the Web Audio API has no Python binding — see `audio-engine.agent.md`).

---

## Tech Stack

| Technology                                     | Purpose                                                          |
| ---------------------------------------------- | ---------------------------------------------------------------- |
| **FastAPI (Starlette)**                        | HTTP server, routing, page handlers, SSE/WebSocket endpoints     |
| **Uvicorn**                                    | ASGI server (dev: `uv run uvicorn ... --reload`)                 |
| **Jinja2**                                     | Server-rendered HTML templates and partials                      |
| **HTMX**                                       | Dynamic partial updates without a JS SPA framework               |
| **Server-Sent Events (SSE)**                   | Push live metrics (latency, levels) to the page                  |
| **Redis**                                      | Server-side session/UI state (shared with the gateway)           |
| **Canvas API (minimal browser JS)**            | Audio visualizations (waveforms, spectrograms)                   |
| **CSS (custom properties)**                    | Theming and component-scoped styling                             |
| **Twilio Voice JS SDK (minimal browser glue)** | WebRTC mic capture for calling (no Python equivalent in-browser) |

> **Python-only rule:** application logic, routing, page rendering, and state live in Python. The only
> non-Python code is the unavoidable browser glue (AudioWorklet, canvas rendering, Twilio Voice SDK), kept as
> thin as possible. Do **not** introduce Next.js, React, a Node build step, npm/yarn, or a TypeScript
> toolchain. Manage dependencies with **uv** and lint/format with **Ruff**.

---

## Design System

### Color Palette (Dark Mode Primary)

```css
:root {
  /* Backgrounds */
  --bg-primary: #0a0a0f;
  --bg-secondary: #12121a;
  --bg-tertiary: #1a1a2e;
  --bg-glass: rgba(255, 255, 255, 0.03);

  /* Accents */
  --accent-primary: #6c5ce7;    /* Purple */
  --accent-secondary: #00cec9;   /* Teal */
  --accent-gradient: linear-gradient(135deg, #6c5ce7, #00cec9);
  --accent-glow: 0 0 20px rgba(108, 92, 231, 0.3);

  /* Text */
  --text-primary: #f0f0f5;
  --text-secondary: #8888a0;
  --text-muted: #555566;

  /* Status */
  --status-success: #00b894;
  --status-warning: #fdcb6e;
  --status-error: #ff6b6b;
  --status-active: #6c5ce7;

  /* Borders */
  --border-subtle: rgba(255, 255, 255, 0.06);
  --border-accent: rgba(108, 92, 231, 0.3);
}
```

### Typography
- **Primary Font**: `Inter` (Google Fonts) — UI text, labels, body
- **Monospace**: `JetBrains Mono` — Code, metrics, latency numbers
- **Display**: `Outfit` — Headlines, large text

### Component Patterns
- Glassmorphism cards with `backdrop-filter: blur(20px)`
- Subtle border glow on interactive elements
- Micro-animations on hover (scale, glow, color shift) via CSS transitions/keyframes
- Smooth page transitions with CSS + HTMX swap classes (`htmx-swapping`, `htmx-settling`)
- Responsive grid layouts (mobile-first)

---

## Pages to Implement

Each page is a FastAPI route returning a rendered Jinja2 template (full page on direct navigation, partial
on HTMX request via the `HX-Request` header).

### 1. Dashboard (`GET /`)
- Welcome message with user's active voice model
- Quick-start actions: "Make a Call", "Train New Voice", "Live Preview"
- Recent calls list with latency metrics
- Usage stats (minutes used / minutes remaining)
- Active voice model card with preview button

### 2. Voice Studio (`GET /studio`)
- **Create Voice** wizard (multi-step via HTMX partial swaps):
  1. Choose mode (Instant Clone / HD Clone)
  2. Upload audio (`multipart/form-data` POST) or record in-browser
  3. Audio quality validation (noise level, duration, clipping)
  4. Training progress (HD Clone, polled via HTMX/SSE) or instant result
  5. Preview and adjust settings
- **Voice Library** — Grid of voice model cards with:
  - Name, quality tier badge (Instant/HD)
  - Similarity score
  - Preview play button
  - Edit / Delete actions
  - "Set Active" button
- **Voice Editor** — Adjust pitch, speed, breathiness with real-time preview

### 3. Dialer (`GET /dialer`)
- Full phone dialer UI:
  - Numeric keypad (0-9, *, #)
  - Phone number input with country code selector
  - Contacts list (recent + saved)
  - "Call" button with voice model indicator
- Active call screen:
  - Timer, voice model name
  - Real-time waveform (input vs output)
  - Latency indicator (color-coded)
  - Controls: mute, hold, voice on/off toggle, model switcher, end call
  - Volume slider
- Call history with duration, model used, avg latency

### 4. Live Monitor (`GET /monitor`)
- Split-screen: input waveform (top) vs output waveform (bottom)
- Real-time spectrogram visualization
- Latency chart (rolling 60-second window, fed by SSE)
- Voice similarity meter
- Audio level meters (input/output)
- "Start Preview" button (mic → transform → speaker)

### 5. Settings (`GET /settings`)
- **Audio**: Input/output device selection, sample rate, buffer size
- **Quality**: Noise suppression, echo cancellation, auto gain
- **Account**: Email, plan, billing
- **Phone**: Twilio phone number management
- **Advanced**: WebSocket URL override, debug logging

---

## Template Library (Jinja2 partials & macros)

Build reusable UI as Jinja2 macros/partials in `templates/components/`, rendered server-side and swapped
in via HTMX. Interactive rendering that must run client-side (canvas) is wrapped by a thin static JS module.

### Core Partials
- `voice_card.html` — Displays a voice model with preview, quality badge, actions
- `audio_visualizer.html` — `<canvas>` + small static JS hook for waveform/spectrogram rendering
- `latency_indicator.html` — Color-coded latency display (green < 200ms, yellow < 300ms, red > 300ms)
- `dial_pad.html` — Phone dialer keypad with DTMF tones
- `active_call_bar.html` — Persistent bar during active call (minimizes to bottom)
- `voice_model_switcher.html` — Dropdown to switch active voice model (HTMX POST on change)
- `audio_recorder.html` — Record audio samples in-browser with quality meter
- `upload_zone.html` — Drag-and-drop audio file upload with validation
- `training_progress.html` — Animated progress bar for HD Clone training (HTMX polling)
- `waveform_compare.html` — Side-by-side original vs transformed waveform

### Layout Templates
- `base.html` — Root document: head, font loading, global CSS, HTMX script tag
- `app_shell.html` — Sidebar navigation + top bar + main content area (extends `base.html`)
- `partials/sidebar.html` — Navigation with icons, active state, collapse toggle
- `partials/top_bar.html` — Active voice model display, user avatar, settings
- `partials/glass_card.html` — Reusable glassmorphism container macro

---

## State Management (server-side, Redis-backed)

UI state lives on the server. Pydantic models describe the shape; the active state is stored per-session in
Redis and rendered into templates. HTMX swaps partials when state changes; SSE pushes live audio metrics.

```python
# app/state.py
from pydantic import BaseModel
from enum import Enum


class ConnectionStatus(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"


class AudioState(BaseModel):
    is_listening: bool = False
    is_transforming: bool = False
    input_level: float = 0.0          # 0-1 RMS level
    output_level: float = 0.0
    current_latency_ms: float = 0.0
    connection_status: ConnectionStatus = ConnectionStatus.DISCONNECTED


class VoiceState(BaseModel):
    active_model_id: str | None = None
    models: list["VoiceModel"] = []          # see docs/PRODUCT_SPEC.md Section 6
    training_jobs: list["TrainingJob"] = []


class CallState(BaseModel):
    active_call: "ActiveCall | None" = None
    call_history: list["CallRecord"] = []
```

Live audio metrics (`input_level`, `output_level`, `current_latency_ms`) originate in the browser audio
engine, are reported up the WebSocket, and are streamed back to the page via an SSE endpoint
(`GET /events/audio`) so the meters and latency chart update without polling.

---

## Integration Points

### Audio Engine
The frontend integrates with the Audio Engine (see `audio-engine.agent.md`) — the minimal browser-side JS
module — via:
- A small static bootstrap script that instantiates the `AudioEngine` on the page
- An SSE/WebSocket channel for state changes (connection, latency, levels)
- Engine methods: `start()`, `stop()`, `setModel(id)`, `getMetrics()`

### Gateway API
- Server-to-server and HTMX `hx-get`/`hx-post` calls to the gateway's REST endpoints
- Base URL from environment variable: `API_URL` (server-side); browser calls go through same-origin routes
- JWT token attached server-side (`Authorization: Bearer <token>`); the browser never holds raw service creds

### Twilio
- Call control via the **Twilio Python SDK** server-side (initiate/manage calls, mint capability tokens)
- Browser WebRTC mic handled by the **Twilio Voice JS SDK** as minimal glue, configured with a token minted
  by a FastAPI route
- Call events posted back to FastAPI webhook routes and reflected into server-side `CallState`

---

## Implementation Order

1. **Project setup**: `uv init`, add `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`, `redis`;
   wire Ruff + mypy + pytest
2. **Design system**: Create CSS variables, global styles, font loading in `base.html`
3. **Layout**: `app_shell.html`, `sidebar.html`, `top_bar.html` templates + routes
4. **Dashboard route**: Static template, then wire up to server-side state
5. **Voice Studio**: Upload handler, HTMX training wizard, voice library grid
6. **Audio Engine integration**: Bootstrap the browser audio module + SSE metrics channel
7. **Live Monitor**: Canvas waveform visualizer, SSE-fed latency chart
8. **Dialer**: Dial pad, call screen, Twilio integration
9. **Settings**: Audio configuration, account management
10. **Polish**: CSS animations, responsive design, error states, loading states (HTMX indicators)

---

## Constraints

- **Server-rendered first**: every page must render fully from the server without requiring client JS for
  initial content (HTMX progressively enhances; pages degrade gracefully).
- No external CSS frameworks (no Tailwind unless user requests).
- Audio processing code must NOT live in page handlers or templates — it belongs in the browser Audio Engine
  glue module and the Python gateway/inference services.
- Use plain CSS with custom properties for theming; keep component styles scoped via naming conventions.
- All interactive elements must have unique IDs for testing (pytest-playwright).
- Serve and cache static assets (fonts, CSS, the minimal JS glue) under `static/`.
- Accessibility: ARIA labels on all interactive elements, keyboard navigation support.
- Tooling: dependencies via **uv**, format/lint via **Ruff** (`uv run ruff format .` / `uv run ruff check
  --fix .`), types via **mypy**, tests via **pytest** + **pytest-playwright**.
