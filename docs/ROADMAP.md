# Mockingbird Roadmap & Milestones

Canonical, build-order milestone tracker. [PRODUCT_SPEC §13](PRODUCT_SPEC.md) has the
high-level phased rollout (Phase 1–4); **this file** tracks the concrete `M`-numbered
milestones referenced in commit messages and code comments, with enough detail for an
agent to pick one up in a fresh context window.

Before starting a milestone, read this file plus the relevant `agents/*.agent.md`
(behavior/contracts) and [CLAUDE.md](../CLAUDE.md) (stack/tooling). Shared contracts:
[agents/AGENTS.md](../agents/AGENTS.md) and [proto/audio.proto](../proto/audio.proto).

## Status at a glance

| Milestone | Scope | State |
|-----------|-------|-------|
| M1 | Vertical echo slice (mic → WS → gateway echo → playback) | done (#6) |
| M2 | Data foundation (Postgres + Redis, wired to `/healthz`) | done (#7, #9) |
| M3 | gRPC proxy + swappable backend (passthrough / cartesia) | done (#8) |
| **M4** | **First real voice transform + clone-your-voice** | **in progress** |
| → M4a | VAD-segmented Cartesia conversion | done |
| → M4b | Voice cloning + `voices` registry | **next** |
| → M4c | Voice Studio UI + voice selection | pending |
| M5 | Auth (Supabase/JWT) + multi-user voice library | pending |
| M6 | Self-hosted GPU backend (RVC/OpenVoice, true low-latency) | pending |
| M7 | Infra / CI hardening (Dockerfiles, compose, CI, Grafana) | pending |
| M8 | Calling — Twilio WebRTC↔PSTN (PRODUCT_SPEC Phase 2) | pending |

---

## M4 — first usable real voice transform + clone-your-voice

**Goal:** turn the loop from "echo with a gRPC hop" into "clone my voice, then hear
another speaker rendered as me."

**Reality check (verified against the live Cartesia API):** Cartesia's Voice Changer is
**clip-based** — `/voice-changer/bytes` and `/voice-changer/sse` both take a whole clip;
there is no streaming-input voice changer (the realtime WebSocket is TTS-only). So true
per-frame `<172ms` morphing is **not** possible with Cartesia — that latency profile
belongs to the self-hosted RVC GPU path (M6). With Cartesia the honest model is
**utterance-segmented** (walkie-talkie feel).

**Decisions (confirmed with the project owner):**
- Backend: **Cartesia** (cloud, no GPU) — matches the "cloud not a hard dependency /
  momentum" goal in CLAUDE.md.
- Capture: **VAD auto-segment** — buffer mic frames, detect end-of-speech, convert each
  utterance via `/voice-changer/sse`.
- Scope: **single-user, NO auth.** Store the cloned Cartesia `voice_id` in a small Postgres
  `voices` table. Defer Supabase/multi-user to M5.

### M4a — VAD-segmented Cartesia conversion — DONE

Landed in `feat(inference,gateway): VAD-segmented Cartesia voice conversion (M4a)`.

- `inference/app/backends/base.py`: stateless `convert()` replaced by a per-stream
  `BackendSession` (`push(frame) -> list[bytes]`, `flush() -> list[bytes]`, `aclose()`) plus
  an `open_session()` factory. Conversion is no longer 1:1.
- `inference/app/backends/passthrough.py`: session returns `[frame]` (still 1:1, echo intact).
- `inference/app/backends/cartesia.py`: energy/RMS VAD over 20ms frames (silence hangover,
  pre-roll, max-utterance cap); on a trailing pause wraps the utterance as WAV (`_pcm_to_wav`),
  POSTs to `/voice-changer/sse`, decodes the base64 `data` chunks, re-chunks to 1920-byte
  frames. Bearer auth, version `2026-03-01`.
- `inference/app/server.py`: one session per `Convert` stream; yields pushed + flushed frames.
- `inference/app/config.py`: VAD params (`vad_*`, `frame_ms`).
- `gateway/app/inference/client.py`: 1:1 `convert()` split into `send(frame)` + `outputs()`
  async iterator + bounded `open()`.
- `gateway/app/websocket/handler.py`: lazy inference dial on first audio frame, concurrent
  reader task forwarding `outputs()`, all sends serialized via `out_lock`, degrade-to-echo
  preserved.

**Voice selection is already wired end-to-end:** `switch_model` → `state.model_id` → gRPC
frame `model_id` → Cartesia `voice[id]`. M4b/M4c only need to *produce* and *choose* a
`voice_id`; the audio hot path needs no further change.

### M4b — Voice cloning + registry — NEXT

**Goal:** clone the user's own voice from a recorded sample, store the returned `voice_id`,
so another speaker can be rendered as that voice by selecting it.

**Cartesia clone API** (verified): `POST /voices/clone`, `multipart/form-data`:
`clip` (binary audio: wav/mp3/webm/…), `name` (required), `language` (required, e.g. `en`),
optional `mode` (e.g. `similarity`), `description`, `base_voice_id`. Auth: `Authorization:
Bearer sk_car_…`, header `Cartesia-Version: 2026-03-01`. Returns `VoiceMetadata` JSON:
`{ id, name, description, language, created_at, … }` — `id` is the `voice_id` usable
directly as the voice-changer `voice[id]`.

**Inference** (owns the Cartesia client + key):
- Add `POST /voices` to the existing FastAPI (`inference/app/health.py`, already on `:8001`):
  accept multipart `clip` + `name` + `language`, call Cartesia `/voices/clone`, return
  `{ voice_id, name, language }`. Build a small `app/voices.py` (or extend the cartesia module)
  rather than bloating `health.py`. Reuse the shared `httpx` client pattern from
  `cartesia.py`. Guard with a clear error when `INFERENCE_BACKEND != cartesia` or no key.

**Gateway** (sole DB owner):
- `voices` table — SQLModel in `gateway/app/db/models.py`: `id` (pk), `voice_id` (Cartesia),
  `label`, `language`, `created_at`. New Alembic migration under `gateway/alembic/versions/`
  (model after the existing `7a23be3fa599_user_table.py`).
- Routes (new `gateway/app/voices/` module): `GET /voices` (list rows) and `POST /voices`
  (proxy the sample to inference clone, persist `voice_id` + `label` + `language`, return the row).
- Add an **inference HTTP client** mirroring the structure of
  `gateway/app/inference/client.py` (the gRPC one) — a thin httpx wrapper to `POST` the clip
  to inference. Add `inference_service_url` to `gateway/app/config.py` (env
  `INFERENCE_SERVICE_URL`, default `http://localhost:8001`, already in `.env.example`).
- Persist via the existing async session (`gateway/app/db/session.py`).

**Tests:** inference clone route (mock Cartesia clone → `voice_id`); gateway `voices` model +
migration + `GET`/`POST` routes (mock the inference HTTP call). No live API key required.

**Done when:** `POST /voices` with an audio clip returns a stored row with a Cartesia
`voice_id`, and `GET /voices` lists it.

### M4c — Voice Studio UI + voice selection — PENDING

**Goal:** a browser flow to create your voice and pick it for the live loop.

- New page `frontend/templates/pages/studio.html` + route in `frontend/app/main.py`:
  record/upload a voice sample → `POST` (multipart) to gateway `/voices` → show result.
- Voice selector: fetch `GET /voices`, render a dropdown; on change call the **existing**
  `engine.switchModel(voiceId)` in `frontend/static/js/audio-engine/audio-engine.js`. Add the
  same dropdown to `frontend/templates/pages/monitor.html`.
- Recording in the browser: capture a few seconds of mic audio (the audio-engine already has
  capture worklets) or a simple `MediaRecorder`; upload as a file field named `clip`.
- Runtime config to actually transform: `INFERENCE_BACKEND=cartesia`, `CARTESIA_API_KEY`,
  and a default `CARTESIA_VOICE_ID` (keys already enumerated in `.env.example`).
- Note: the stale "pre-implementation" status in `CLAUDE.md` was corrected when this roadmap
  was added; M4c need only keep it current.

**Known UI caveat (from M4a):** under utterance mode the browser's FIFO latency tracker
(`LatencyTracker` in the audio engine) pairs continuously-sent frames against bursty
received frames, so the roundtrip readout is unreliable. M4c is the natural place to relabel
or replace it with an utterance-latency metric (e.g. time from end-of-speech to first
output frame, measured gateway-side).

### M4 verification (end-to-end, once M4a–c land)

- `uv run pytest` in `inference/` and `gateway/`; `uv run ruff check` across touched services.
- `docker compose up -d` (postgres + redis) → run inference (`INFERENCE_BACKEND=cartesia
  uv run uvicorn app.health:app --port 8001`) → gateway → frontend.
- In the browser: record a sample, clone, select your voice, have a second person speak,
  confirm output is your voice. Read the latency readout and note headroom.

---

## Forward roadmap (after M4)

- **M5 — Auth + multi-user voice library:** Supabase/JWT on `/ws/voice` and `/voices`,
  per-user `voices` rows keyed to the existing user table, login-gated Studio. Reuses M2's
  user table.
- **M6 — Self-hosted GPU backend:** implement the `self_hosted` backend (RVC/OpenVoice + ONNX
  Runtime GPU) behind the **same** per-stream `BackendSession` interface from M4a — true
  per-frame `<172ms` path; reference-audio storage (MinIO/S3, already in `.env.example`).
  Backend swap only, no contract change.
- **M7 — Infra / CI hardening:** Dockerfiles for frontend/gateway/inference, extend
  `infrastructure/docker-compose.yml` to run all three app services, GitHub Actions (`ruff` +
  `pytest` per service), Prometheus + Grafana latency dashboard.
- **M8 — Calling (PRODUCT_SPEC Phase 2):** Twilio WebRTC↔PSTN, dialer UI with contacts/history,
  mid-call model switching, call recording.

Mapping to PRODUCT_SPEC §13: M4–M5 complete Phase 1 (MVP) minus GPU; M6 adds self-hosted GPU;
M7 is the deployment/observability cut of Phase 1; M8 begins Phase 2.
