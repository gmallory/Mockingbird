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
| **M6** | **Auth (Supabase) + multi-user voice library** | **done** |
| → M6a | Supabase token verification + `/auth` proxy + per-user `/voices` + login UI | done |
| → M6b | Auth on the `/ws/voice` socket + per-user rate limiting | done |
| **M7** | **Infra / CI hardening (GitHub Actions CI + Prometheus/Grafana)** | **done** |
| **M8** | **Calling — Twilio PSTN (PRODUCT_SPEC Phase 2, outbound cut)** | **in progress** |
| → M8a | Outbound PSTN calls + media-stream bridge + dialer UI | done (local); live Twilio run pending |
| → M8b | Inbound calls, recording, WebRTC peer calls, contacts | **descoped** (2026-07-07 — future/commercial) |
| **M9** | **HD Clone tier — RVC training + single-graph ONNX export** | pending |
| **M10** | **UI completion (Dashboard, Settings, fine-tune, meters) + v1 sign-off** | pending |

## Backend priority (owner decision, 2026-07-04)

**Self-hosted GPU inference (`self_hosted`) is the first-priority engine** — the primary
path the product is built around, even while its <172ms per-frame latency budget remains
a target rather than a measured fact. **Cartesia (`cartesia`) and cloud-hosted GPU
(`cloud_gpu`, planned) are separate, selectable modes** — not fallbacks, not the spine.
This supersedes the earlier "no-GPU clip spine primary / GPU optional tier" framing from
the 2026-06-30 interview and is why self-hosted work is promoted to M5, ahead of auth.

## Definition of success (owner decision, 2026-07-07)

Mockingbird v1 is a **demo-ready portfolio piece**. The binding success criteria are
**[PRODUCT_SPEC §15](PRODUCT_SPEC.md)** — live cloned phone call, GPU-measured latency
inside budget, listening-check clone quality, HD tier, complete UI, green CI, current
docs. Scope settled the same day:

- **In scope:** HD Clone tier (RVC) → **M9**; remaining UI pages (Dashboard, Settings,
  fine-tune controls, similarity meter, waveform viz) → **M10**.
- **Descoped to future/commercial:** inbound calls, call recording, WebRTC peer calls,
  contacts, model export/sharing, A/B testing, billing, K8s/edge scaling (full list in
  PRODUCT_SPEC §13).
- **Order of remaining work:** M5 GPU bench → M8a live-Twilio run → M9 → M10 (sign-off).

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

### M6 — Auth + multi-user voice library — DONE (M6a + M6b)

Supabase-hosted auth on `/voices` (M6a) and later `/ws/voice` (M6b), per-user `voices`
rows keyed to the existing user table, login-gated Studio. Reuses M2's user table. (Was
M5; slid behind self-hosted per the 2026-07-04 priority decision.)

**Auth mechanism (owner decision, 2026-07-05):** **Supabase (hosted)**, not the
self-issued-JWT alternative. Supabase (GoTrue) owns credentials and mints access tokens;
the gateway only **proxies** signup/login to it and **verifies** the returned token. This
keeps password handling out of our code at the cost of a real Supabase project for the
live login flow (the test suite stays offline — see below).

### M6a — Supabase verification + `/auth` proxy + per-user `/voices` — DONE

- **Config** (`gateway/app/config.py`): `supabase_url`, `supabase_anon_key`,
  `supabase_jwt_secret`, `supabase_jwt_audience` (default `authenticated`).
  `SUPABASE_JWT_SECRET` added to `.env.example`.
- **Token verification** (`gateway/app/auth/jwt.py`): `verify_token()` decodes the
  bearer token **offline** — HS256 against the project JWT secret, checking signature,
  `exp`, and `aud` — and returns `TokenClaims{sub, email, display_name}`. Swapping to
  Supabase's asymmetric (ES256 + JWKS) keys later is a change behind this one function.
- **GoTrue client** (`gateway/app/auth/supabase.py`): thin httpx wrapper (mirrors
  `app/inference/http.py`) — `signup()` → `POST /auth/v1/signup`, `login()` →
  `POST /auth/v1/token?grant_type=password`; bubbles the upstream status via
  `SupabaseAuthError` so bad credentials surface as 400/401, not a blanket 502.
- **Dependencies** (`gateway/app/auth/dependencies.py`): `get_current_claims` (verify
  bearer → 401) and `get_current_user` (lazily mirror the Supabase identity into a local
  `User` row keyed by `sub`, reused thereafter; races resolved via unique-violation +
  re-fetch).
- **Routes** (`gateway/app/auth/routes.py`, `/auth`): `POST /auth/signup`,
  `POST /auth/login` (proxy), `GET /auth/me` (whoami). Wired into `main.py`.
- **Per-user voices**: `Voice.user_id` FK (`gateway/app/db/models.py`) + Alembic
  migration `c4d2f8a17b9e` (clears pre-auth unowned rows, adds NOT NULL `user_id` +
  index + FK). `GET /voices` filters to the caller; `POST /voices` stamps the caller as
  owner. Both now require a valid token.
- **Frontend**: `pages/login.html` + `GET /login` route (email/password, signup toggle);
  `static/js/auth.js` (token in localStorage, Bearer helpers, `requireAuth`); Studio
  gated + Bearer on its `/voices` calls; Monitor attaches the token when present and
  stays echo-only when anonymous (the demo loop still runs logged-out); auth-aware nav
  in `base.html`.
- **Deps**: added `pyjwt`; promoted `httpx` from dev-only to a runtime dependency (the
  clone proxy and the new Supabase client both use it at runtime).
- **Tests** (`gateway/tests/test_auth.py` + updated `test_voices.py`): fully offline —
  self-minted HS256 tokens exercise verification (accept/expired/bad-sig/wrong-aud/
  missing-sub/unconfigured), `httpx.MockTransport` stands in for GoTrue (URL, apikey,
  error mapping), routes driven via `httpx.ASGITransport`, per-user scoping + the
  `get_current_user` upsert verified against Postgres (skips when it is down). No live
  Supabase or key needed; the migration is verified by `alembic upgrade head`.

**Done when (met):** `POST /voices` and `GET /voices` require a Supabase bearer token and
are scoped to that user; `POST /auth/signup` / `POST /auth/login` return a session;
`GET /auth/me` returns the mirrored user; the Studio is login-gated; `alembic upgrade
head` applies the `user_id` FK; `uv run pytest` + `uv run ruff check` are green.

**Manual (needs a real Supabase project):** set `SUPABASE_URL` / `SUPABASE_ANON_KEY` /
`SUPABASE_JWT_SECRET`, sign up + sign in through `/login`, clone a voice, confirm it is
listed only for that user.

### M6b — Auth on the realtime socket + rate limiting — DONE

Authenticated `/ws/voice` + per-user Redis rate limiting, reusing `verify_token`.
Two owner decisions (2026-07-06) shaped it: **optional auth** (a flag, not a hard
requirement) and a **fail-open** limiter — both to keep the anonymous echo demo and
the "session survives an infra outage" posture that M6a established.

What landed:

- **Socket auth** (`gateway/app/websocket/auth.py`): `resolve_ws_auth(token)` runs
  *before* the socket is accepted and returns one of three outcomes —
  `authenticated` (valid token → user id + plan, rate-limited), `anonymous` (no
  token and `WS_REQUIRE_AUTH` off → echo-only demo), or `rejected` (bad token
  always, or missing token when the flag is on → the handler closes **4001**). A
  present-but-invalid token is never silently downgraded to anonymous. The token
  rides in a `?token=<jwt>` query param (the browser can't set WS headers).
  `load_user_plan` reads the plan off the connect path (one lookup), defaulting to
  FREE if Postgres is unreachable.
- **Rate limiting** (`gateway/app/rate_limit/limiter.py`): `RateLimiter` over Redis
  with per-plan caps from `agents/gateway.agent.md` (`PLAN_LIMITS`: Free 1 conn / 5
  min, Pro 3 / 300, Enterprise 10 / unlimited). Concurrency is a per-user sorted set
  admitted by a single atomic Lua step (prune stale slots → count → add under cap);
  usage is a per-month `INCRBYFLOAT` counter that resets on the calendar rollover.
  `acquire()` on connect (over cap → close **4029**), `release()` + `record_usage()`
  on close. Fail-open: a Redis outage (or a `None` client, e.g. a bare `TestClient`)
  admits the session unenforced and logs.
- **Handler** (`gateway/app/websocket/handler.py`): auth/limit gate after
  `accept()`, before `ready`; anonymous sessions are **echo-locked** (model id pinned
  to `""`, `switch_model` refused with `auth_required`) so a demo visitor can't drive
  conversion with someone else's voice id; slot released + duration banked in the
  `finally`. `main.py` extracts the query token, resolves auth, and builds the
  limiter from `app.state.redis` (or `None`).
- **Config**: `ws_require_auth` (env `WS_REQUIRE_AUTH`, default false), in `.env.example`.
- **Browser**: `websocket-worker.js` appends `?token=` and treats 4001/4029 closes as
  terminal (no reconnect-storm); `audio-engine.js` `start(modelId, token)` forwards
  the token and emits `authError` / `limited`; `monitor.html` passes `getToken()` and
  surfaces both (logged-out visitors still get the echo demo).
- **Tests**: `tests/test_rate_limit.py` (concurrency, usage, per-plan caps, fail-open;
  real Redis, skips when down) + `tests/test_ws_voice.py` additions (anonymous
  echo-lock, tokenless-when-required → 4001, invalid token → 4001, authenticated
  round-trip, `acquire`-denied → 4029, real-Redis concurrency enforced end-to-end via
  the lifespan, `load_user_plan` against Postgres). `uv run pytest` is green (48) with
  Redis + Postgres up.

**Done when (met):** a valid `?token=` yields an authenticated, rate-limited session;
no token yields the echo-only demo (unless `WS_REQUIRE_AUTH=true` → 4001); an invalid
token → 4001; over a plan's concurrency cap or monthly minutes → 4029; the browser
stops retrying on those; a Redis outage degrades to unenforced rather than dropping.

### M7 — Infra / CI hardening — DONE

Scope was CI + observability (Dockerfiles, extended compose, and `scripts/dev.sh` had
already landed in #14). What landed:

- **CI** (`.github/workflows/ci.yml`): one matrix job per service (frontend, gateway,
  inference) running `uv sync --locked` → `ruff format --check` → `ruff check` →
  `pytest`, plus a `docker compose build` job for the three images. Postgres 16 +
  Redis 7 service containers mirror the compose stack's credentials, so the gateway
  tests that skip locally when infra is down (DB, rate limiter, WS end-to-end) run
  for real in CI, and `alembic upgrade head` verifies the migration chain before the
  gateway's tests. Ruff was added to the gateway/frontend dev groups (inference
  already had it); `inference/.python-version` now pins 3.14 like the other two.
- **Gateway metrics** (`gateway/app/metrics.py`, served at `GET /metrics` via
  `prometheus-client`): session outcomes (accepted / rejected 4001 / rate_limited
  4029), active-session gauge by mode, session-duration histogram, audio-frame
  counters (in/out), degraded-session counter, and a **first-output latency
  histogram** — first inbound audio frame to first *converted* frame back (covers
  the lazy gRPC dial + block fill + inference; buckets bracket the 172ms budget).
- **Inference metrics** (`inference/app/metrics.py`, `GET /metrics` on the health
  app): active Convert-stream gauge, frame counters, and a **conversion-latency
  histogram** observed only for push/flush calls that emitted audio — one sample
  per converted block (self_hosted) or utterance (cartesia), labelled by backend
  class; buckets bracket the 80ms GPU-inference line. Instrumented in
  `app/server.py` so every backend is covered without touching them.
- **Compose observability** (`infrastructure/docker-compose.yml` +
  `infrastructure/monitoring/`): `prometheus` (v3.1, 5s scrape of gateway:3001 +
  inference:8001) and `grafana` (11.4, anonymous-admin dev config, host port 3002)
  with a provisioned datasource and a **latency dashboard**
  (`monitoring/grafana/dashboards/latency.json`, uid `mockingbird-latency`):
  conversion-latency quantiles with the 80ms threshold line, first-output latency
  with the 172ms line, frame throughput, active sessions/streams, session outcomes
  + degradations, session duration. Both services are optional — the app stack
  runs unchanged without them.
- **Tests**: `gateway/tests/test_metrics.py` (exposition endpoint + WS counters via
  a real degraded session) and `inference/tests/test_metrics.py` (endpoint + Convert
  stream instrumentation through the passthrough backend).

Out of scope, unchanged: K8s manifests, Terraform, Sentry, alerting rules, and the
CD/deploy pipeline from `agents/infrastructure.agent.md` — none are needed until
there's a real deployment target (the rented-GPU bench box is provisioned by script,
not K8s).

**Done when (met):** CI runs lint + format + tests for all three services and builds
the compose images on every PR; `docker compose -f infrastructure/docker-compose.yml
up` serves a Grafana latency dashboard at `http://localhost:3002/d/mockingbird-latency`
fed by Prometheus scraping both services' `/metrics`.

### M8 — Calling (PRODUCT_SPEC Phase 2) — IN PROGRESS (M8a done locally)

Twilio PSTN calling, dialer UI with history, mid-call model switching. Split:
**M8a** = outbound calls end-to-end (landed); **M8b** = inbound calls, recording,
WebRTC peer calls, contacts — **descoped 2026-07-07** (future/commercial).

Mapping to PRODUCT_SPEC §13: M4 + M5 complete Phase 1 (MVP, including the GPU path);
M6 adds auth; M7 is the deployment/observability cut of Phase 1; M8a is the in-scope
cut of Phase 2; M9/M10 close v1.

### M8a — Outbound PSTN + media-stream bridge + dialer — DONE (local)

**Architecture (decided in-slice, follows existing patterns):** the gateway drives
Twilio's REST API through a thin httpx wrapper (`gateway/app/calls/twilio.py`, same
posture as the GoTrue client — no vendor SDK, injectable transport, offline tests)
and terminates the call's **Media Stream** itself. Flow: `POST /api/calls/outbound`
persists a `CallRecord`, registers an in-memory `CallBridge`, and creates the Twilio
call with TwiML `<Connect><Stream url="wss://…/ws/twilio/{call_id}?secret=…">`;
the browser joins the same bridge over the existing `/ws/voice` session with a new
`join_call` control message (contract in agents/AGENTS.md). While joined, the
session's *converted* output is transcoded (48kHz PCM → G.711 mu-law 8kHz,
`app/calls/telephony.py`, pure stdlib — `audioop` is gone in 3.13+) and routed to
the phone leg; the callee's audio comes back as binary frames. Call teardown is
driven by Twilio's status callback (`POST /api/twilio/status`, X-Twilio-Signature
validated) or an explicit `POST /api/calls/{id}/hangup`.

What landed:

- **DB**: `CallRecord` (`gateway/app/db/models.py`) + migration `a8f4c2d7e1b3`
  (id doubles as the bridge/stream id; `voice_id` FK nullable; enums
  `calldirection`/`callstatus` created `checkfirst` so a test-`create_all` dev DB
  doesn't break the chain). Verified up/down/up.
- **Routes** (`gateway/app/calls/routes.py`): outbound (E.164-validated, owned-voice
  check, 503 when Twilio env or `PUBLIC_BASE_URL` unset — the rest of the app is
  unaffected), history list/get (per-user), hangup (Twilio best-effort, record
  closes regardless), status webhook (signature-gated, progress events ignored).
- **Bridge** (`gateway/app/calls/bridge.py`): per-call bounded queues (drop-oldest,
  ~2s), per-call random secret gating `/ws/twilio/{call_id}` (close 1008 otherwise),
  None-sentinel close. Process-local — fine for the single-gateway deploy; a
  multi-gateway topology needs Redis routing (M8b note).
- **WS protocol**: `join_call`/`call_joined` messages; handler routes output via the
  bridge only while it's open, reverts to echo when the call ends; anonymous
  sessions can't join (`auth_required`), non-owners get `call_not_found`.
- **Frontend**: `/dialer` page (login-gated: voice picker, E.164 input, call/hangup,
  live meters, history), nav link, `engine.joinCall()` + worker `join_call` (re-sent
  on reconnect so a drop mid-call re-joins the bridge).
- **Tests** (all offline; 68 pass in `gateway/`): mu-law/resample codec unit tests,
  call routes with mocked Twilio + real Postgres, webhook signature accept/reject,
  media-stream secret gating, and a single-loop end-to-end bridge test (fake
  WebSockets — two TestClient sockets each get their own event loop, which
  deadlocks cross-loop queues; production runs one uvicorn loop).

**Remaining for M8a sign-off (needs real credentials, ~15 min):** set
`TWILIO_*` + `PUBLIC_BASE_URL` (ngrok/cloudflared tunnel to gateway :3001), place a
real call from `/dialer`, confirm two-way audio and that the status callback closes
the record. Latency note: the PSTN leg adds Twilio's own transport on top of the
existing conversion budget; measure during the live run.

### M8b — Inbound calls, recording, peer calls — DESCOPED (2026-07-07)

Out of v1 scope (owner decision — see "Definition of success" above). Kept as the
future/commercial sketch: inbound (dedicated Twilio number → `CallDirection.INBOUND`),
call recording (original + transformed, consent language per PRODUCT_SPEC §4.3),
browser↔browser WebRTC calls, contacts. Also: Redis-backed bridge routing if the
gateway ever runs more than one replica.

### M9 — HD Clone tier (RVC) — PENDING

**Goal:** the PRODUCT_SPEC §4.2 second tier — fine-tuned RVC voices that beat the
instant clone, behind the **same** `BackendSession` / M5a ONNX contract (backend swap
only; no WS/gRPC change). This is the "M5c candidate" deferred in M5b, now scheduled.
Sketch only — write the detailed spec when picking it up, and spike the export first:
it is the risk item.

- **Single-graph export (the hard part):** compose HuBERT content encoding + F0
  estimation + the RVC synthesizer into one audio-in/audio-out ONNX graph
  (float32 `[1, N]` @ `SELF_HOSTED_MODEL_SAMPLE_RATE`, M5a contract). Follow the
  M5b pattern (`inference/app/export/`, torch only in the export group).
- **Training pipeline:** Celery + Redis job queue (PRODUCT_SPEC §5): upload →
  preprocess → fine-tune base RVC on the GPU box → export ONNX → register as a
  `voices` row. Introduces the `VoiceModel` shape (PRODUCT_SPEC §6) for status/artifacts.
- **API:** `POST /api/voices/{id}/train` + `GET /api/voices/{id}/train/status`
  (PRODUCT_SPEC §7 planned table).
- **UI:** training progress in the Studio (progress bar + ETA).
- Needs the rented GPU box — the M5 `provision_cloud_gpu.sh` script covers it; run the
  M5 bench first.

**Done when:** a 10–30 min sample fine-tunes to an RVC voice that streams through
`self_hosted` end-to-end and beats the instant clone of the same speaker in a
side-by-side listening check (PRODUCT_SPEC §15 criterion 4).

### M10 — UI completion + v1 sign-off — PENDING

**Goal:** close PRODUCT_SPEC §4.5 and run the §15 checklist. Last milestone of v1.

- **Dashboard**: overview page — active voice, recent calls, quick-start links.
  (Monitor currently holds `/`; decide whether Dashboard takes `/` and Monitor moves.)
- **Settings**: audio I/O config, quality presets, account — backed by the existing
  `User.settings` JSON column.
- **Fine-tune controls:** pitch offset / speed / breathiness per voice
  (`PATCH /api/voices/{id}`), applied in the self-hosted streaming session — the DSP
  hook into `BlockStreamSession` needs its own mini-spike.
- **Monitor polish:** waveform visualizer (level meters exist today), voice similarity
  meter, explicit transform on/off toggle. Mute/hold/volume on the dialer.
- **v1 sign-off:** walk PRODUCT_SPEC §15, record results + measured numbers in the
  README as the portfolio writeup.

**Done when:** every §15 criterion is Pass and the README carries the writeup.
