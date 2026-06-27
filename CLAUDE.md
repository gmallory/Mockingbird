# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Mockingbird is currently **pre-implementation**. The repository contains only documentation and agent
configuration — there is no `frontend/`, `gateway/`, `inference/`, or `infrastructure/` code yet, no
`package.json`, and no test suite. There are no build/lint/test commands to run because nothing has been
scaffolded. Before assuming a command, directory, or file exists, check for it — the structure described
below and in the docs is the *planned* layout, not the current one.

When implementing a piece of this project, treat `docs/PRODUCT_SPEC.md` and the relevant `agents/*.agent.md`
file as the spec to build against, and create the directory structure they describe under the repo root.

## What this project is

Real-time AI voice cloning for live conversations: capture mic audio in the browser, transform it to a
different voice in <300ms, and play it back or pipe it into a phone/WebRTC call. Full feature/architecture
description is in [README.md](README.md); the authoritative detailed spec (data models, API design, latency
budgets) is in [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md).

## Architecture

The project is an **all-Python stack**: frontend, gateway/middleware, and inference are all Python services.
Three independently-developed services connected by a binary WebSocket audio protocol:

```
Browser-facing Python frontend (e.g. FastAPI/Starlette + AudioWorklet JS shim for mic capture)
   │ wss:// binary PCM frames (20ms chunks, Int16, 48kHz)
   ▼
Python Gateway / middleware (FastAPI or similar) — WS connection mgmt, auth, model routing
   │ gRPC
   ▼
Python FastAPI ML Inference Service (RVC / OpenVoice / GPT-SoVITS, ONNX Runtime GPU)
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
- **Inference backend is swappable** via `INFERENCE_BACKEND` (`self_hosted` | `cartesia` | `elevenlabs`),
  so self-hosted GPU inference is not a hard dependency for frontend/gateway development.

The WebSocket JSON control protocol and binary audio frame format (shared contract across frontend, gateway,
and inference) are defined in [agents/AGENTS.md](agents/AGENTS.md) — keep all three services in sync with it
when changing message shapes.

## Agent-driven development

This repo is meant to be built using the per-domain agent specs in `agents/`:

| Agent | File | Scope |
|-------|------|-------|
| Orchestrator | `agents/AGENTS.md` | Shared contracts, env vars, build order |
| Frontend | `agents/frontend.agent.md` | Next.js app, pages, components |
| Audio Engine | `agents/audio-engine.agent.md` | AudioWorklet, ring buffer, WS client |
| Gateway | `agents/gateway.agent.md` | Node.js WS gateway, auth, routing |
| Inference | `agents/inference.agent.md` | Python ML service, RVC/OpenVoice, training |
| Infrastructure | `agents/infrastructure.agent.md` | Docker, K8s, CI/CD, monitoring |

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

## Important notes

- **This file's stack decision overrides the agent specs.** The files under `agents/` and parts of
  `README.md` still describe a polyglot stack (Next.js 15 + React 19 frontend, Node.js/Fastify gateway,
  TypeScript). That is **superseded** — build frontend and gateway in Python per the section above. Treat the
  agent specs as the source of truth for *behavior and contracts* (class signatures, WS/gRPC message shapes,
  latency budgets), not for language or runtime choices.
- **uv + Ruff only.** Reach for npm/yarn/pip/poetry or Prettier/ESLint and you're off-spec. The lone
  exception is the browser AudioWorklet shim, which is unavoidably JS — keep it as thin as possible.
- The audio hot path still matters regardless of language: the `process()` AudioWorklet callback must stay
  zero-allocation, and changes to the streaming path are judged against the ~172ms latency budget, not just
  functional correctness.

## Environment variables

`.env.example` enumerates the full set (DB, Redis, JWT/Supabase auth, Twilio, S3, inference backend
selection, feature flags). Copy to `.env` and fill in real values before running any service — never commit
`.env`.
