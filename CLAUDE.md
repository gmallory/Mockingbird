# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Mockingbird is under active implementation. The three Python services exist and have test suites:
`frontend/`, `gateway/`, and `inference/` (plus `infrastructure/`, `proto/`). Milestones **M1–M7 are
done**: echo slice, Postgres/Redis, gRPC proxy + swappable backends, Cartesia conversion + cloning +
Studio UI, self-hosted OpenVoice V2 streaming engine, Supabase auth (REST + WebSocket, per-user
rate limiting), and CI/observability (GitHub Actions, Prometheus `/metrics`, Grafana latency dashboard).
**M8a** (outbound Twilio PSTN calls: `/api/calls`, media-stream bridge, `/dialer` UI) is built and
tested offline; the live-Twilio run remains.

**Do not trust this paragraph to stay current.** The canonical milestone tracker — current state,
per-milestone detail, and the concrete next steps (the rented-GPU `cloud_gpu` bench run closing M5;
the live-Twilio M8a run; then M9 HD Clone and M10 UI/sign-off — M8b was descoped 2026-07-07) — is
**[docs/ROADMAP.md](docs/ROADMAP.md)**. Read it plus the relevant `agents/*.agent.md` before picking
up work. `docs/PRODUCT_SPEC.md` remains the detailed spec (data models, API design, latency budgets);
its **§15 is the binding v1 success criteria** (demo-ready portfolio piece, owner decision 2026-07-07).
Still verify a directory/command/file exists before assuming it — later roadmap items (M9, M10) are
not built yet.

## What this project is

Real-time AI voice cloning for live conversations: capture mic audio in the browser, transform it to a
different voice in <300ms, and play it back or pipe it into a phone/WebRTC call. Full feature/architecture
description is in [README.md](README.md); the authoritative detailed spec (data models, API design, latency
budgets) is in [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md).

## Goal & current focus

Mockingbird is a **portfolio / learning** project — optimize for clean architecture and forward momentum,
not production hardening. Bias toward small, compartmentalized specs (one service or one vertical slice at a
time) and surface key decisions for explicit sign-off before implementing.

Per the 2026-07-04 owner decision, self-hosted is the first-priority engine; Cartesia and `cloud_gpu`
are separate co-equal modes, not fallbacks. Don't re-litigate that hierarchy without a new owner
decision. Current open work is tracked in [docs/ROADMAP.md](docs/ROADMAP.md), not here.

## Architecture

The project is an **all-Python stack**: frontend, gateway/middleware, and inference are all Python services.
Three independently-developed services connected by a binary WebSocket audio protocol:

```
frontend/ — FastAPI + Jinja2 + HTMX, AudioWorklet JS shim for mic capture (port 3000)
   │ wss:// binary PCM frames (20ms chunks, Int16, 48kHz)
   ▼
gateway/ — FastAPI, WS connection mgmt, auth, rate limiting, model routing (port 3001)
   │ gRPC (port 50051)
   ▼
inference/ — FastAPI ML service, OpenVoice V2 / Cartesia backends, ONNX Runtime (port 8001)
```

- All three services live in the same language and should share tooling (uv for dependencies, Ruff for
  lint/format) rather than each having its own bespoke setup.
- The browser still needs some JS/TS for `AudioWorkletProcessor` (the Web Audio API has no Python binding),
  but that should be the minimal glue layer only — application logic, routing, and the server side of the
  frontend belong in Python.
- **Gateway and Inference communicate over gRPC**, not REST; the Gateway terminates the browser WebSocket
  and proxies audio frames to the GPU inference service.
- **Total latency budget is ~172ms** end-to-end (capture → encode → upload → inference → postprocess →
  download → decode/playback); GPU inference itself is budgeted at 80ms. Changes to the streaming path should
  be evaluated against this budget, not just functional correctness.
- **Inference backend is swappable** via `INFERENCE_BACKEND` (`self_hosted` | `cartesia` | `elevenlabs`;
  `cloud_gpu` is added in M5 — same self-hosted stack on a rented GPU, own config). Self-hosted is the
  **first-priority engine**; the cloud modes are separate co-equal modes, and frontend/gateway
  development never hard-depends on a GPU being present.

The WebSocket JSON control protocol and binary audio frame format (shared contract across frontend, gateway,
and inference) are defined in [agents/AGENTS.md](agents/AGENTS.md) — keep all three services in sync with it
when changing message shapes.

## Agent-driven development

This repo is meant to be built using the per-domain agent specs in `agents/`:

| Agent          | File                             | Scope                                                  |
| -------------- | -------------------------------- | ------------------------------------------------------ |
| Orchestrator   | `agents/AGENTS.md`               | Shared contracts, env vars, build order                |
| Frontend       | `agents/frontend.agent.md`       | FastAPI + Jinja2 + HTMX app, server-rendered pages     |
| Audio Engine   | `agents/audio-engine.agent.md`   | AudioWorklet, ring buffer, WS client (browser JS glue) |
| Gateway        | `agents/gateway.agent.md`        | Python (FastAPI) WS gateway, auth, routing             |
| Inference      | `agents/inference.agent.md`      | Python ML service, RVC/OpenVoice, training             |
| Infrastructure | `agents/infrastructure.agent.md` | Docker, K8s, CI/CD, monitoring                         |

Build order matters because later agents depend on contracts established earlier: **Infrastructure →
Inference → Gateway → Audio Engine → Frontend**. When asked to implement a feature, read the corresponding
agent file first — it contains the concrete class/interface signatures, file layout, and constraints expected
for that domain, and is more detailed than the README summary.

## Tech stack

The entire stack is **Python 3.14** — frontend, gateway/middleware, and inference. Use one toolchain across
all three services:

- **Package manager / runner**: [uv](https://docs.astral.sh/uv/) for everything — dependency resolution,
  virtualenvs, and running code. Each service has its own `pyproject.toml`; add deps with `uv add`, run with
  `uv run`, and commit the `uv.lock`. Do not introduce `pip install`, `poetry`, `requirements.txt`, npm, or
  yarn workflows.
- **Lint & format**: Ruff via uv — `uv run ruff format .` to format and `uv run ruff check --fix .` to lint.
  Ruff replaces Black/isort/Flake8; do not add Prettier/ESLint for the Python code.
- **Frontend**: Python web framework (e.g. FastAPI/Starlette) serving the UI. The only non-Python code is the
  minimal browser-side `AudioWorkletProcessor` glue needed for Web Audio mic capture/playback.
- **Gateway / middleware**: Python (FastAPI or similar) for WebSocket connection management, auth, and model
  routing; Redis (sessions/pub-sub/routing), PostgreSQL, S3/GCS.
- **Inference**: Python (FastAPI), RVC, OpenVoice v2, GPT-SoVITS, ONNX Runtime GPU, TensorRT, Silero VAD,
  Celery + Redis for training jobs.
- **Infra**: Docker + Kubernetes, NVIDIA GPU (A10G/L4), Twilio (PSTN), Prometheus + Grafana.
- **Testing**: pytest across all services; manual + latency benchmarks for the audio path.

## Common commands

All commands run from inside the relevant service directory (`frontend/`, `gateway/`, `inference/`) —
each has its own `pyproject.toml` and `uv.lock`:

```bash
uv run pytest                 # test one service
uv run ruff format .          # format
uv run ruff check --fix .     # lint
```

Full local stack (Postgres + Redis + all three services, CPU-only):

```bash
docker compose -f infrastructure/docker-compose.yml up --build
# frontend http://localhost:3000/studio, gateway :3001, inference :8001 (gRPC :50051)
```

Running one service directly, e.g. inference:
`INFERENCE_BACKEND=passthrough uv run uvicorn app.health:app --port 8001`. Gateway DB migrations are
Alembic (`uv run alembic upgrade head` in `gateway/`; the compose stack runs them automatically).

## Important notes

- **The agent specs and this file agree: the stack is all-Python.** `agents/*.agent.md` have been converted
  (FastAPI + Jinja2 + HTMX frontend, FastAPI gateway). Treat them as the detailed source of truth for
  *behavior and contracts* (class signatures, WS/gRPC message shapes, latency budgets), and this file as the
  source of truth for *stack and tooling*. If a stray Next.js/React/Node reference survives anywhere, it's a
  leftover — Python wins.
- **uv + Ruff only.** Reach for npm/yarn/pip/poetry or Prettier/ESLint and you're off-spec. The lone
  exception is the browser AudioWorklet shim, which is unavoidably JS — keep it as thin as possible.
- The audio hot path still matters regardless of language: the `process()` AudioWorklet callback must stay
  zero-allocation, and changes to the streaming path are judged against the ~172ms latency budget, not just
  functional correctness.
- **Hooks enforce the rules above** (`.claude/settings.json` + `.claude/hooks/`): writing `.env`/secret/key
  files, off-stack tools (npm/yarn/pnpm/pip/poetry/conda), and destructive git (`push --force`,
  `reset --hard`, `branch -D`, `clean -f`) are **blocked**, not just discouraged. Edited `.py` files are
  auto-formatted with Ruff on save. Don't route around a block — fix the underlying action.

## Environment variables

`.env.example` enumerates the full set (DB, Redis, JWT/Supabase auth, Twilio, S3, inference backend
selection, feature flags). Copy to `.env` and fill in real values before running any service — never commit
`.env`.
