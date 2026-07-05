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
| **M5** | **Self-hosted GPU backend (RVC/OpenVoice) — primary engine** | **in progress** |
| → M5a | Streaming ONNX engine + `cloud_gpu` mode + latency benchmark | done |
| → M5b | Real voice weights (OpenVoice V2 ONNX export) + instant clone + tuning | done (local); GPU bench run pending |
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

### M5 — Self-hosted GPU backend — IN PROGRESS (M5a done)

**Goal:** make the first-priority engine real — `self_hosted` voice conversion
(RVC/OpenVoice + ONNX Runtime) behind the **same** per-stream `BackendSession`
interface from M4a. Backend swap only; no WS/gRPC contract change. Split like M4:
**M5a** = streaming engine + modes + benchmark (landed); **M5b** = real voice weights.

### M5a — Streaming ONNX engine + `cloud_gpu` mode + benchmark — DONE

- `inference/app/backends/self_hosted.py`: `SelfHostedBackend` + block-streaming
  session. Frames buffer into `SELF_HOSTED_BLOCK_MS` blocks (default 100ms); each
  block runs through the ONNX model with `SELF_HOSTED_CONTEXT_MS` (default 200ms)
  of left context for continuity; output re-chunked to 20ms frames and emitted
  immediately — latency is block + inference, independent of utterance length.
  Inference runs in a worker thread (`asyncio.to_thread`) so concurrent gRPC
  streams aren't starved.
- **ONNX model contract** (what M5b's export must satisfy): one `{model_id}.onnx`
  per voice; first input float32 mono `[1, N]` at `SELF_HOSTED_MODEL_SAMPLE_RATE`
  (audio resampled in/out when it differs from the 48kHz stream); first output
  same layout, length may differ (mapped back proportionally).
- **Model resolution + cache:** `SELF_HOSTED_MODEL_DIR/{model_id}.onnx`, else
  downloaded from `s3://$S3_BUCKET/models/{model_id}.onnx` (boto3, MinIO-compatible
  via `S3_ENDPOINT`). LRU cache of loaded sessions (`SELF_HOSTED_MAX_LOADED_MODELS`).
  Model ids are sanitized (no path traversal).
- **Error posture mirrors Cartesia:** missing/corrupt model, bad model id, S3 miss,
  or a failed inference passes audio through unchanged with a logged warning —
  never kills the gRPC stream.
- **Device selection:** `DEVICE=auto|cuda|coreml|cpu` → ONNX Runtime execution
  providers (auto prefers CUDA > CoreML > CPU); unavailable devices degrade to CPU.
  `/healthz` reports the device for self-hosted modes.
- **Backend enum:** `INFERENCE_BACKEND` now `passthrough | cartesia | self_hosted |
  cloud_gpu | elevenlabs` (elevenlabs = placeholder raising NotImplementedError).
  `cloud_gpu` constructs the identical backend — it exists so deploys are explicit:
  run inference on the rented GPU box (A10G/L4) with `INFERENCE_BACKEND=cloud_gpu`
  and point the gateway's `INFERENCE_GRPC_URL` at that box (notes in `.env.example`).
- **Benchmark:** `inference/scripts/self_hosted_bench.py` — synthetic speech-like
  frames through the real session, reports per-block p50/p95/max, real-time factor,
  and effective added latency; `--model` accepts real weights. Measured numbers
  recorded in PRODUCT_SPEC §4.1 (pipeline overhead p95 0.6–4.3ms/block; the 100ms
  block buffer is the floor, not compute).
- **Tests** (`inference/tests/test_self_hosted.py`): tiny ONNX gain graphs built
  on the fly prove audio flows through ORT; blocking cadence, flush drain,
  resample path, default model, degrade paths, LRU cache, mocked S3 fetch.
  No GPU or network needed.
- **Cartesia untouched:** the clip/utterance mode stays as-is, a separate
  selectable mode — not a fallback of the GPU path.

**Done when (met):** `INFERENCE_BACKEND=self_hosted` boots, streams per-block
converted audio through an ONNX model end-to-end, degrades to passthrough when no
model is available, and the benchmark's measured numbers are in PRODUCT_SPEC §4.1.

### M5b — Real voice weights (OpenVoice V2 export) + instant clone — DONE (local)

**Engine decision:** shipped the **OpenVoice v2 zero-shot path** (the sanctioned
alternative above) instead of RVC-first. Rationale: the OpenVoice tone-color
converter exports to a *single* audio-in/audio-out ONNX graph satisfying the M5a
contract, and its zero-shot cloning wires directly into the `voices` registry
(clip → voice in seconds, no training job). RVC cannot satisfy the single-graph
contract without composing HuBERT feature extraction + F0 estimation + synthesizer
into one graph — deferred as its own work item (M5c candidate, needed for the
§4.2 "HD Clone" tier).

What landed:

- **Vendored converter** (`inference/app/export/openvoice/`, MIT, trimmed to the
  conversion path, state-dict compatible with the published
  `myshell-ai/OpenVoiceV2` checkpoint). Deviations for streaming determinism:
  posterior encoder runs at `tau=0` (mean, no sampling → no RandomNormal op),
  and the *source* SE is computed in-graph from each ~200ms window.
- **Export** (`scripts/export_openvoice_onnx.py`, torch via `uv sync --group
  export`; the service itself never imports torch): downloads the 131MB
  checkpoint, exports `models/openvoice/openvoice_converter.onnx` (template;
  STFT is baked in as a conv — 22050Hz) + `openvoice_se_encoder.onnx`, verifies
  both with ORT. The target voice lives in the graph's `tgt_se` initializer.
- **Instant clone, torch-free** (`app/export/clone.py`): decode clip (stdlib WAV
  or ffmpeg) → SE encoder → patch `tgt_se` into a copy of the template →
  `{model_id}.onnx`. Wired into inference `POST /voices` for
  `self_hosted`/`cloud_gpu`, so the existing gateway route + Studio UI work
  unchanged; the stored `voice_id` **is** the streaming `model_id`.
- **Streaming fixes/tuning** (`app/backends/self_hosted.py`): seam crossfade
  (`SELF_HOSTED_CROSSFADE_MS=5` — held-back tail blended against the next
  block's re-rendering of the same span); hop-truncation deficit rule (a model
  dropping <25ms per window no longer compresses time — streams stay exactly
  1:1); defaults tuned to `BLOCK_MS=60` / `CONTEXT_MS=140` on real-weight
  measurements.
- **Measured** (dev Mac M-series CPU, real weights, details in PRODUCT_SPEC
  §4.1): per-block p95 47.5ms, RTF 0.77–0.83, ~107ms effective added latency.
  Laptop CPU already beats the 80ms GPU inference line.
- **Tests** (`tests/test_export_openvoice.py` + extended self-hosted/voices
  tests): tiny random converter through the real torch→ONNX→ORT path
  (determinism, dynamic lengths, SE patching, clone→stream end-to-end); module
  skips cleanly without the export group.

**Remaining (moves to the `cloud_gpu` bench run):** execute
`infrastructure/scripts/provision_cloud_gpu.sh` on a rented A10G/L4 box to lock
the 80ms GPU line in PRODUCT_SPEC §4.1 and validate `DEVICE=cuda` end-to-end.
**Self-hosted remains primary regardless of that measurement.**

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
