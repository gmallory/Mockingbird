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
| M4 | First real voice transform + clone-your-voice | done |
| → M4a | VAD-segmented Cartesia conversion | done (#10) |
| → M4b | Voice cloning + `voices` registry | done (#11, #13) |
| → M4c | Voice Studio UI + voice selection | done (#14) |
| **M5** | **Self-hosted GPU backend (RVC/OpenVoice) — primary engine** | **next** |
| M6 | Auth (Supabase/JWT) + multi-user voice library | pending |
| M7 | Infra / CI hardening (CI, Grafana; Dockerfiles/compose landed in #14) | pending |
| M8 | Calling — Twilio WebRTC↔PSTN (PRODUCT_SPEC Phase 2) | pending |

## Backend priority (owner decision, 2026-07-04)

**Self-hosted GPU inference (`self_hosted`) is the first-priority engine** — the primary
path the product is built around, even while its <172ms per-frame latency budget remains
a target rather than a measured fact. **Cartesia (`cartesia`) and cloud-hosted GPU
(`cloud_gpu`, planned) are separate, selectable modes** — not fallbacks, not the spine.
This supersedes the earlier "no-GPU clip spine primary / GPU optional tier" framing from
the 2026-06-30 interview and is why self-hosted work is promoted to M5, ahead of auth.

---

## M4 — first usable real voice transform + clone-your-voice

**Goal:** turn the loop from "echo with a gRPC hop" into "clone my voice, then hear
another speaker rendered as me."

**Reality check (verified against the live Cartesia API):** Cartesia's Voice Changer is
**clip-based** — `/voice-changer/bytes` and `/voice-changer/sse` both take a whole clip;
there is no streaming-input voice changer (the realtime WebSocket is TTS-only). So true
per-frame `<172ms` morphing is **not** possible with Cartesia — that latency profile
belongs to the self-hosted RVC GPU path (M5, was M6). With Cartesia the honest model is
**utterance-segmented** (walkie-talkie feel).

**Decisions (confirmed with the project owner):**
- Backend: **Cartesia** (cloud, no GPU) — matches the "cloud not a hard dependency /
  momentum" goal in CLAUDE.md.
- Capture: **VAD auto-segment** — buffer mic frames, detect end-of-speech, convert each
  utterance via `/voice-changer/sse`.
- Scope: **single-user, NO auth.** Store the cloned Cartesia `voice_id` in a small Postgres
  `voices` table. Defer Supabase/multi-user to M6 (was M5 before the 2026-07-04 reorder).

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

### M4b — Voice cloning + registry — DONE (#11, #13)

Landed as specced below: `inference/app/voices.py` clone route, gateway `voices` table
(`gateway/app/db/models.py::Voice`), `GET`/`POST /voices` in `gateway/app/voices/routes.py`,
inference HTTP client, Alembic migration. #13 addressed the five review findings.

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

### M4c — Voice Studio UI + voice selection — DONE (#14)

Landed as specced below, plus three deviations/extras:
- The **utterance-latency metric flagged in the caveat below shipped in M4c itself** (not
  deferred): the Live Monitor now reports end-of-speech → first-output-frame and the old
  per-frame roundtrip readout is gone.
- A standalone **voice-changer spike harness** (`inference/scripts/voice_changer_spike.py`,
  `pipeline` subcommand: transcode → normalize → clone → convert → stage) measured Cartesia
  utterance latency: **~500ms VAD + ~0.8–1s fixed overhead + ~0.45× realtime; felt floor
  ~2.0s for a 2s utterance**. Sub-second is impossible on the Cartesia backend — the
  conversational <300ms target needs the GPU path (M5).
- A chunk of M7 landed early: uv-based Dockerfiles for all three services, extended
  `infrastructure/docker-compose.yml`, and `scripts/dev.sh` one-command dev stack.

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

### M5 — Self-hosted GPU backend — NEXT

**Goal:** make the first-priority engine real — `self_hosted` voice conversion
(RVC/OpenVoice + ONNX Runtime GPU) behind the **same** per-stream `BackendSession`
interface from M4a. Backend swap only; no WS/gRPC contract change.

**What needs to change to make self-hosted the primary path:**

- **Inference:** implement `inference/app/backends/self_hosted.py` as a `BackendSession`
  (per-frame `push()` streaming, unlike the Cartesia utterance session). Model loading from
  reference-audio / model-weight storage (MinIO/S3 — `S3_ENDPOINT`/`S3_BUCKET` already in
  `.env.example`).
- **Backend enum:** add `cloud_gpu` as a **distinct `INFERENCE_BACKEND` value** in
  `inference/app/config.py` and `.env.example` — same self-hosted inference stack deployed
  on a rented GPU (A10G/L4), with its own config keys (endpoint, credentials, provisioning
  notes). Four modes total: `self_hosted` (primary) | `cloud_gpu` | `cartesia` | `elevenlabs`.
- **GPU provisioning:** document/script the two deployment shapes — local dev GPU
  (`self_hosted`) and rented GPU (`cloud_gpu`).
- **Latency benchmark ("measured-then-locked"):** measure the real per-frame end-to-end
  latency of the self-hosted path and record the numbers in PRODUCT_SPEC §4.1 next to the
  ~172ms budget (which stays a *target* until then). **Self-hosted remains primary even if
  the first measurement misses 172ms.** Cartesia's utterance latency is already measured
  and locked (see M4c: ~2s felt floor).
- **Cartesia untouched:** the `cartesia` clip/utterance mode stays as-is, a separate
  selectable mode — not a fallback of the GPU path.

**Before starting:** expand this section to M4b-level detail (file paths, done-when, test
plan) — the M4b section above is the proven template.

### M6 — Auth + multi-user voice library

Supabase/JWT on `/ws/voice` and `/voices`, per-user `voices` rows keyed to the existing
user table, login-gated Studio. Reuses M2's user table. (Was M5; slid behind self-hosted
per the 2026-07-04 priority decision.)

### M7 — Infra / CI hardening

GitHub Actions (`ruff` + `pytest` per service), Prometheus + Grafana latency dashboard.
Dockerfiles for all three services, extended `infrastructure/docker-compose.yml`, and
`scripts/dev.sh` already landed in #14 — remaining scope is CI + observability.

### M8 — Calling (PRODUCT_SPEC Phase 2)

Twilio WebRTC↔PSTN, dialer UI with contacts/history, mid-call model switching, call
recording.

Mapping to PRODUCT_SPEC §13: M4 + M5 complete Phase 1 (MVP, including the GPU path);
M6 adds auth; M7 is the deployment/observability cut of Phase 1; M8 begins Phase 2.
