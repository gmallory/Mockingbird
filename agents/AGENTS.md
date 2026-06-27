# Mockingbird — Agent Instructions

> **Master orchestration document for AI-assisted development of the Mockingbird project.**

This directory contains agent configuration files that enable agentic AI systems (e.g., Gemini, Claude, GPT, Cursor, Codex) to build, extend, and debug the entire Mockingbird codebase. Each agent file defines the scope, responsibilities, constraints, and implementation guidance for a specific domain of the project.

---

## How to Use These Agents

### With an AI Coding Assistant

When working with an AI coding assistant, reference the relevant agent file to scope the work:

```
"Read agents/frontend.agent.md and implement the Voice Studio page according to the product spec."
```

```
"Following agents/inference.agent.md, set up the RVC voice conversion pipeline with WebSocket streaming."
```

```
"Use agents/audio-engine.agent.md to implement the AudioWorklet capture and playback processors."
```

### Agent Invocation Order

For building the project from scratch, invoke agents in this order:

```
1. Infrastructure Agent  → Set up Docker, databases, project scaffolding
2. Inference Agent       → ML models, training pipeline, WebSocket streaming
3. Gateway Agent         → Python (FastAPI) WebSocket gateway, auth, routing
4. Audio Engine Agent    → Browser audio pipeline (AudioWorklet, WebSocket)
5. Frontend Agent        → FastAPI + Jinja2 + HTMX UI, pages, templates
```

Each agent can work independently but should respect shared interfaces (API contracts, WebSocket message formats, data models).

---

## Agent Overview

| Agent | File | Scope | Primary Language |
|-------|------|-------|-----------------|
| **Frontend** | `frontend.agent.md` | FastAPI + Jinja2 + HTMX app, templates, pages, routing | Python |
| **Audio Engine** | `audio-engine.agent.md` | Web Audio API, AudioWorklet, WebSocket streaming, ring buffers | JavaScript (browser glue) |
| **Gateway** | `gateway.agent.md` | WebSocket connection management, auth, model routing | Python |
| **Inference** | `inference.agent.md` | ML model serving, RVC/OpenVoice, training, voice conversion | Python |
| **Infrastructure** | `infrastructure.agent.md` | Docker, Kubernetes, CI/CD, monitoring, deployment | YAML/HCL |

---

## Shared Contracts

All agents must adhere to these shared interfaces:

### WebSocket Audio Protocol

```
Client → Server (binary): Int16 PCM audio frames (20ms chunks = 960 samples at 48kHz)
Server → Client (binary): Int16 PCM transformed audio frames

Client → Server (JSON):
  { "type": "start", "modelId": "<uuid>", "sampleRate": 48000 }
  { "type": "switch_model", "modelId": "<uuid>" }
  { "type": "stop" }
  { "type": "ping" }

Server → Client (JSON):
  { "type": "ready", "latencyMs": 172 }
  { "type": "model_loaded", "modelId": "<uuid>" }
  { "type": "error", "code": "<error_code>", "message": "<description>" }
  { "type": "metrics", "latencyMs": 165, "similarity": 0.92 }
  { "type": "pong" }
```

### REST API Base URL
- Development: `http://localhost:3001/api`
- Production: `https://api.mockingbird.app/api`

### Environment Variables (Shared)

```env
# Database
DATABASE_URL=postgresql://user:password@localhost:5432/mockingbird
REDIS_URL=redis://localhost:6379

# Auth
JWT_SECRET=<secret>
SUPABASE_URL=<url>
SUPABASE_ANON_KEY=<key>

# Twilio
TWILIO_ACCOUNT_SID=<sid>
TWILIO_AUTH_TOKEN=<token>
TWILIO_PHONE_NUMBER=<number>

# ML Inference
INFERENCE_SERVICE_URL=http://localhost:8001
INFERENCE_GRPC_URL=localhost:50051

# Storage
S3_BUCKET=mockingbird-models
S3_REGION=us-east-1
AWS_ACCESS_KEY_ID=<key>
AWS_SECRET_ACCESS_KEY=<secret>

# Feature Flags
INFERENCE_BACKEND=self_hosted  # or 'cartesia', 'elevenlabs'
ENABLE_CALLING=true
ENABLE_RECORDING=false
```

### Data Models (Python)

All agents should reference the shared Pydantic/SQLModel models defined in `gateway/app/db/models.py`:

```python
# VoiceModel, CallRecord, User — see docs/PRODUCT_SPEC.md Section 6
```

---

## Development Workflow

### Branch Strategy
- `main` — Production-ready code
- `develop` — Integration branch
- `feature/<agent>/<feature>` — Feature branches (e.g., `feature/frontend/voice-studio`)

### Testing Requirements
- **Frontend**: pytest for route/handler tests, pytest-playwright for E2E
- **Gateway**: pytest for unit tests, WebSocket integration tests (httpx / Starlette TestClient)
- **Inference**: pytest for unit tests, audio quality benchmarks
- **Audio Engine**: Manual browser testing + automated latency benchmarks

### Code Quality
- Ruff (format + lint) + mypy across all Python services
- uv for dependency management (no npm/pip/poetry); commit `uv.lock`
- Pre-commit hooks for formatting
- CI pipeline runs all tests on PR

---

## Key Reference Documents

- [Product Specification](../docs/PRODUCT_SPEC.md) — Architecture, data models, API design, latency budgets
- [README](../README.md) — Project overview, getting started, tech stack
