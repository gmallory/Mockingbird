# 🐦‍⬛ Mockingbird

> **Real-time AI voice cloning for live conversations.**

Mockingbird is a web application that captures your speech, transforms it into a different voice in real-time, and outputs the cloned voice — enabling you to sound like someone else during phone calls and live preview sessions. It's a **portfolio / learning project** (owner decision, 2026-07-07): the goal is a demo-ready, honestly-documented build, not a production SaaS.

---

## ✅ v1 Status — Portfolio Sign-off (2026-07-09)

Every milestone through **M10** is built and offline-tested (real Postgres/Redis, no
mocked-out DB). The binding success criteria are **[PRODUCT_SPEC §15](docs/PRODUCT_SPEC.md)**; this table is the portfolio-facing summary. **Deferred** means fully built and offline-verified, with only a real GPU box, live Twilio credentials, or a subjective listening session standing between it and a checked box — not more code.

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Live cloned phone call (Studio → Dialer → real callee) | **Deferred** — needs a live Twilio run | Outbound PSTN + media-stream bridge built and offline-tested (M8a); needs real `TWILIO_*` creds + a tunnel (~15 min) |
| 2 | GPU latency inside the ~172ms end-to-end budget | **Deferred** — needs a rented GPU box | Laptop-CPU numbers already beat the 80ms GPU-inference line (below); the GPU run locks it in |
| 3 | Instant-clone quality (listening check) | **Deferred** — needs an owner listening session | Exercised locally in M5b; no automated PESQ harness in v1 by design |
| 4 | HD Clone beats the instant clone (listening check) | **Deferred** — needs the GPU fine-tune run | Training pipeline + single-graph ONNX export built and offline-tested end to end (M9); no trained HuBERT/F0/RVC weights yet |
| 5 | UI complete (Dashboard, Settings, fine-tune controls, similarity meter, waveform viz) | **Pass** | All five built in M10 — see below |
| 6 | CI green (format + lint + tests × 3 services) | **Partial** | Gateway (83 tests) and inference (79 tests, 1 skipped) pass unchanged; M10's `/` → Dashboard move leaves 2 frontend assertions stale until the test-author pass updates them |
| 7 | Docs current | **Partial** | ROADMAP + PRODUCT_SPEC are current as of this sign-off; this README got a full accuracy pass too, but see the note at the bottom of this section |

### Measured numbers (dev Mac M-series CPU, real OpenVoice V2 weights — full detail in **[PRODUCT_SPEC §4.1](docs/PRODUCT_SPEC.md)**)

- **Self-hosted per-block conversion**: p50 46.2ms / p95 47.5ms / max 54.4ms at the tuned `BLOCK_MS=60`/`CONTEXT_MS=140` defaults — real-time factor 0.77×, ~107ms effective added latency. **Already under the 80ms GPU-inference budget line on a laptop CPU**; the rented-GPU run (§15 criterion 2) locks in the number on real hardware rather than changing the architecture.
- **Full streaming session** (clone → convert, real speech): RTF 0.83, output stream exactly 1:1 with input frames.
- **Cartesia (cloud, utterance-based) mode**: ~500ms VAD hangover + ~0.8–1s fixed overhead + ~0.45× realtime — felt floor ~2.0s for a 2-second utterance. This mode is walkie-talkie by design (Cartesia's voice changer is clip-based, not streaming-input); the self-hosted path above is where the <300ms conversational target lives.

### What M10 actually shipped

- **Dashboard** at `/` (Live Monitor moved to `/monitor`): voice-library summary, recent calls, quick-start links.
- **Settings** (`/settings`): audio input/output device selection (`MediaDevices` enumeration), a latency/quality preset, the three `getUserMedia` toggles, and a read-only account panel — backed by a new `GET`/`PATCH /api/settings` on the gateway (merge-patch semantics over the existing `User.settings` JSON column).
- **Fine-tune controls**: `GET`/`PATCH /api/voices/:id` persists pitch offset (±12 semitones), speed factor (0.5–2×), and breathiness (0–1) on the `VoiceModel` row (added with the shape in M9), and pushes them to the inference service out-of-band — the gRPC audio-frame proto and the WS control protocol are both unchanged. Applied as a per-block DSP post-process (pure numpy: a spectral pitch shift, a resample-based speed effect, and shaped-noise breathiness) inside the self-hosted streaming session, after the existing seam crossfade — the 1:1 frame-count streaming cadence is untouched.
- **Monitor polish**: a real oscilloscope-style waveform visualizer (`AnalyserNode` + canvas, not a repurposed level meter), a voice-similarity meter that shows a real measured score when one exists and otherwise a clearly-labeled *live estimate* rather than a fabricated number, and an explicit transform on/off toggle.
- **Dialer polish**: mute, hold (a client-side both-directions-silent approximation — there's no Twilio hold-music integration in v1), and a volume slider (native `GainNode`).

**Known documentation debt** (not fixed in this pass — flagged rather than left silently inaccurate): the agent specs (`agents/*.agent.md`) describe an HTMX-driven, Redis-backed-UI-state architecture that was never actually built this way — the shipped frontend is server-rendered Jinja2 pages with small, page-local vanilla-JS modules and no HTMX. That drift predates M10 and is a good candidate for its own cleanup pass.

---

## ✨ Features

### 🎤 Real-Time Voice Cloning
- **Self-hosted streaming engine** (primary) — OpenVoice V2 on ONNX Runtime, block-streaming (not clip-based): ~107ms effective added latency measured on a laptop CPU, already under the 80ms GPU-inference budget line
- **Cartesia** (cloud, no GPU) and **`cloud_gpu`** (the same self-hosted stack on a rented GPU box) as separate, selectable modes — not fallbacks
- **Swappable backend** — `INFERENCE_BACKEND` picks the mode; frontend/gateway development never hard-depends on a GPU being present

### 🗣️ User-Trainable Voice Models
- **Instant Clone** — record ~10–20 seconds, get a working clone in seconds (Cartesia `/voices/clone` or the self-hosted OpenVoice speaker-embedding bake)
- **HD Clone** — upload a longer sample to fine-tune a higher-quality voice (RVC pipeline + single-graph ONNX export); the pipeline, training UI, and streaming integration are done — the real pretrained weights and GPU fine-tune loop are the deferred tail (§15 criterion 4)
- **Voice Library** — per-user registry, browsable in the Studio and the Dashboard

### 📞 Built-in VoIP Calling
- **Outbound PSTN calls** — dial any phone number with your transformed voice; the gateway drives Twilio's REST API directly and terminates the call's Media Stream itself (no vendor SDK, no browser WebRTC leg)
- **Mid-call controls** — hangup, model hot-swap, mute, hold, volume
- Inbound calls, browser-to-browser WebRTC calls, and call recording are **descoped** (future/commercial — see [ROADMAP.md](docs/ROADMAP.md))

### 🎛️ Audio & UI Controls
- Fine-tune controls: pitch offset (±12 semitones), speed factor (0.5–2×), breathiness — applied server-side per voice
- Noise suppression, echo cancellation, auto gain control (configurable in Settings)
- Real-time waveform visualization, a voice-similarity meter, an explicit transform on/off toggle, and per-frame/utterance latency readouts

---

## 🏗️ Architecture

Three independently-developed, **all-Python** services connected by a binary WebSocket audio protocol. The only non-Python code is the minimal browser-side `AudioWorkletProcessor` glue Web Audio requires — everything else, including the frontend's server-rendered pages, is Python.

```
frontend/ (FastAPI + Jinja2, :3000)
   │ wss:// binary PCM frames (20ms chunks, Int16, 48kHz) + JSON control messages
   ▼
gateway/ (FastAPI, :3001) — WS connection mgmt, auth, rate limiting, model routing
   │ gRPC (:50051)
   ▼
inference/ (FastAPI, :8001) — OpenVoice V2 / RVC / Cartesia backends, ONNX Runtime
```

### Latency Budget (~172ms total, per-frame self-hosted path)

| Stage                     | Target     | Measured (laptop CPU, real weights) |
| ------------------------- | ---------- | ------------------------------------ |
| Audio capture + buffering | 10ms       | —                                     |
| Encoding (PCM → Int16)    | 2ms        | —                                     |
| Network upload (edge)     | 20ms       | not yet measured (needs the GPU/network run) |
| Server preprocessing      | 5ms        | —                                     |
| **Model inference**       | **80ms (GPU target)** | **p95 47.5ms on CPU** — already under budget |
| Server postprocessing     | 5ms        | —                                     |
| Network download          | 20ms       | not yet measured                     |
| Decode + playback buffer  | 30ms       | —                                     |

Cartesia (cloud, utterance-based) has a different latency model entirely — see the measured numbers above.

---

## 🛠️ Tech Stack

Everything is **Python 3.14** and **[uv](https://docs.astral.sh/uv/)** — one dependency manager, one lockfile per service, no npm/pip/poetry anywhere. Ruff (via `uv run`) is the only formatter/linter.

### Frontend
- **FastAPI + Jinja2** — server-rendered pages (Dashboard, Studio, Monitor, Dialer, Settings, Login)
- **Uvicorn** — ASGI server
- **Vanilla JS** (ES modules, `// @ts-check` + JSDoc, no build step, no framework) — `AudioWorkletProcessor` capture/playback, a WebSocket Worker, and small page-local scripts
- **Canvas API** — the Monitor's waveform visualizer

### Backend — Gateway
- **FastAPI** — WebSocket connection management, Supabase-token auth, per-user rate limiting, REST routes (`/voices`, `/api/calls*`, `/api/voices/*/train*`, `/api/voices/:id`, `/api/settings`)
- **PostgreSQL** (SQLModel + Alembic) — users, voices, voice models, call history
- **Redis** — per-user rate limiting; the Celery broker/result backend for HD training jobs
- **S3 / MinIO** — voice model artifacts (self-hosted/`cloud_gpu` modes)

### Backend — ML Inference
- **FastAPI + gRPC** — the streaming `Convert` service, plus small HTTP side-routes for cloning/training/tuning
- **OpenVoice V2** — the primary self-hosted engine; real exported weights, zero-shot instant cloning (Apache 2.0)
- **RVC** — the HD tier; single-graph ONNX export composition scaffolded (M9), real pretrained weights + the fine-tune loop are the deferred GPU work (MIT)
- **ONNX Runtime** — CPU today; CUDA/CoreML execution providers selected automatically when available
- **Cartesia** — cloud, clip-based voice changer (a separate mode, not a fallback)
- **Celery + Redis** — the HD training job queue

### Infrastructure
- **Docker Compose** — the full local stack (all 3 services + Postgres + Redis + Prometheus + Grafana); `scripts/dev.sh` is a one-command version outside Docker
- **A rented GPU box** (A10G/L4, provisioned via `infrastructure/scripts/provision_cloud_gpu.sh`) — for the `cloud_gpu` mode and the §15 latency measurement
- **Twilio** — REST API + Media Streams, gateway-terminated, no vendor SDK
- **Prometheus + Grafana** — a provisioned latency dashboard (conversion latency, first-output latency, throughput, session outcomes)
- **GitHub Actions** — lint + format + tests × 3 services + image builds on every PR

---

## 📁 Project Structure

```
Mockingbird/
├── docs/
│   ├── PRODUCT_SPEC.md           # Architecture, data models, API design, §15 success criteria
│   └── ROADMAP.md                # Canonical milestone tracker — read this first
│
├── frontend/                     # FastAPI + Jinja2, port 3000
│   ├── app/main.py                 # Routes: /, /monitor, /studio, /dialer, /settings, /login
│   ├── templates/                  # base.html + pages/*.html
│   └── static/
│       ├── css/app.css
│       └── js/
│           ├── auth.js             # Bearer-token session glue
│           └── audio-engine/       # AudioEngine, worklets, WS worker, utils
│
├── gateway/                       # FastAPI, port 3001 (WS) + gRPC client on :50051
│   ├── app/
│   │   ├── main.py                  # App wiring + /ws/voice, /ws/twilio/{call_id}
│   │   ├── auth/                    # Supabase token verification + signup/login proxy
│   │   ├── voices/                  # Voice registry + fine-tune controls (GET/PATCH)
│   │   ├── calls/                   # Twilio outbound calls + media bridge
│   │   ├── training/                # Celery HD-training pipeline
│   │   ├── settings/                # Audio/quality prefs API
│   │   ├── websocket/               # WS auth + the /ws/voice handler
│   │   ├── rate_limit/              # Redis-backed per-plan limiter
│   │   └── db/                      # SQLModel models + Alembic migrations
│   └── Dockerfile
│
├── inference/                     # FastAPI, port 8001 (HTTP) + gRPC server on :50051
│   ├── app/
│   │   ├── health.py                 # Process entrypoint: HTTP + gRPC server
│   │   ├── server.py                 # The gRPC Convert service
│   │   ├── backends/                 # passthrough / cartesia / self_hosted (+ cloud_gpu)
│   │   ├── export/                   # OpenVoice + RVC ONNX export/clone/training pipelines
│   │   ├── dsp.py                    # Fine-tune DSP transforms (pitch/speed/breathiness)
│   │   ├── tuning.py                 # Out-of-band fine-tune params channel
│   │   ├── voices.py                 # POST /voices (clone)
│   │   └── training.py               # POST /train_hd
│   └── Dockerfile
│
├── infrastructure/
│   ├── docker-compose.yml          # Full local stack
│   ├── monitoring/                 # Prometheus config + Grafana dashboards
│   └── scripts/provision_cloud_gpu.sh
│
├── proto/audio.proto               # The gRPC contract shared by gateway + inference
├── agents/                         # Per-domain build specs (AGENTS.md + one per service)
├── scripts/dev.sh                  # One-command local dev stack
├── .env.example
└── README.md
```

---

## 🚀 Getting Started

### Prerequisites
- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- Docker & Docker Compose (for Postgres/Redis/observability)
- A GPU is **not required** — the self-hosted engine runs on CPU; `cloud_gpu` and Twilio are optional, separately-configured modes

### Quick Start (one command)

```bash
git clone <this-repo>
cd Mockingbird
cp .env.example .env   # fill in real values before using auth/calling/training
./scripts/dev.sh        # Postgres + Redis, migrations, then all 3 services with streamed logs
# frontend http://localhost:3000, gateway :3001, inference :8001 (gRPC :50051)
```

### Full stack via Docker Compose

```bash
docker compose -f infrastructure/docker-compose.yml up --build
# frontend http://localhost:3000/studio, gateway :3001, inference :8001
# Grafana http://localhost:3002/d/mockingbird-latency, Prometheus scraping both services' /metrics
```

### Running one service directly

```bash
cd inference && uv run uvicorn app.health:app --port 8001   # INFERENCE_BACKEND=self_hosted by default
cd gateway   && uv run uvicorn app.main:app --port 3001
cd frontend  && uv run uvicorn app.main:app --port 3000
```

### Tests

```bash
cd gateway && uv run pytest     # or frontend/, inference/
uv run ruff format . && uv run ruff check --fix .
```

---

## 📖 Documentation

- [ROADMAP.md](docs/ROADMAP.md) — canonical milestone tracker: current state, per-milestone detail, concrete next steps. **Start here.**
- [PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) — architecture, data models, API design, latency budgets, and the binding §15 success criteria
- [agents/AGENTS.md](agents/AGENTS.md) — shared contracts (WS protocol, gRPC proto) and how the per-domain agent specs are used

---

## 🤖 AI-Assisted Development

This project was built with the per-domain agent specs in `agents/` guiding each slice of work — read the relevant one before picking up a domain; they're more detailed than this README on class signatures, message shapes, and constraints. `agents/AGENTS.md` is the shared-contract index; CLAUDE.md and ROADMAP.md are the current source of truth when a spec's aspirational framing (e.g. HTMX, Redis-backed UI state) drifted from what actually shipped.

| Agent              | Scope                                               | File                      |
| ------------------ | ---------------------------------------------------- | ------------------------- |
| **Orchestrator**   | Shared contracts, env vars, build order             | `AGENTS.md`               |
| **Frontend**       | FastAPI + Jinja2 pages, templates, routing          | `frontend.agent.md`       |
| **Audio Engine**   | Web Audio API, AudioWorklet, WebSocket streaming    | `audio-engine.agent.md`   |
| **Gateway**        | WebSocket gateway, auth, rate limiting, routing     | `gateway.agent.md`        |
| **Inference**      | ML service, OpenVoice/RVC, training pipeline        | `inference.agent.md`      |
| **Infrastructure** | Docker, CI/CD, monitoring                            | `infrastructure.agent.md` |

---

## 📜 License

MIT License — See [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgments

- [OpenVoice](https://github.com/myshell-ai/OpenVoice) — the self-hosted engine's zero-shot voice cloning (vendored converter, trimmed to the conversion path)
- [RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) — the HD-tier engine (single-graph ONNX export composed in M9; real pretrained weights pending)
- [Cartesia](https://cartesia.ai/) — the cloud voice-changer mode
- [Twilio](https://www.twilio.com/) — PSTN calling
