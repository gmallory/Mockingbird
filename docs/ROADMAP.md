# Mockingbird Roadmap & Milestones

Canonical, build-order milestone tracker. [PRODUCT_SPEC §13](PRODUCT_SPEC.md) has the
high-level phased rollout (Phase 1–4); **this file** tracks the concrete `M`-numbered
milestones referenced in commit messages and code comments, with enough detail for an
agent to pick one up in a fresh context window.

Before starting a milestone, read this file plus the relevant `agents/*.agent.md`
(behavior/contracts) and [CLAUDE.md](../CLAUDE.md) (stack/tooling). Shared contracts:
[agents/AGENTS.md](../agents/AGENTS.md) and [proto/audio.proto](../proto/audio.proto).

## Status at a glance

| Milestone | Scope                                                                       | State                                         |
| --------- | --------------------------------------------------------------------------- | --------------------------------------------- |
| M1        | Vertical echo slice (mic → WS → gateway echo → playback)                    | done (#6)                                     |
| M2        | Data foundation (Postgres + Redis, wired to `/healthz`)                     | done (#7, #9)                                 |
| M3        | gRPC proxy + swappable backend (passthrough / cartesia)                     | done (#8)                                     |
| M4        | First real voice transform + clone-your-voice                               | done                                          |
| → M4a     | VAD-segmented Cartesia conversion                                           | done (#10)                                    |
| → M4b     | Voice cloning + `voices` registry                                           | done (#11, #13)                               |
| → M4c     | Voice Studio UI + voice selection                                           | done (#14)                                    |
| **M5**    | **Self-hosted GPU backend (RVC/OpenVoice) — primary engine**                | **in progress**                               |
| → M5a     | Streaming ONNX engine + `cloud_gpu` mode + latency benchmark                | done                                          |
| → M5b     | Real voice weights (OpenVoice V2 ONNX export) + instant clone + tuning      | done (local); GPU bench run pending           |
| **M6**    | **Auth (Supabase) + multi-user voice library**                              | **done**                                      |
| → M6a     | Supabase token verification + `/auth` proxy + per-user `/voices` + login UI | done                                          |
| → M6b     | Auth on the `/ws/voice` socket + per-user rate limiting                     | done                                          |
| **M7**    | **Infra / CI hardening (GitHub Actions CI + Prometheus/Grafana)**           | **done**                                      |
| **M8**    | **Calling — Twilio PSTN (PRODUCT_SPEC Phase 2, outbound cut)**              | **in progress**                               |
| → M8a     | Outbound PSTN calls + media-stream bridge + dialer UI                       | done (local); live Twilio run pending         |
| → M8b     | Inbound calls, recording, WebRTC peer calls, contacts                       | **descoped** (2026-07-07 — future/commercial) |
| **M9**    | **HD Clone tier — RVC training + single-graph ONNX export**                 | done (local); GPU fine-tune run pending       |
| **M10**   | **UI completion (Dashboard, Settings, fine-tune, meters) + v1 sign-off**    | done (local); GPU-gated §15 items pending     |

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
  rides as a `bearer.<jwt>` `Sec-WebSocket-Protocol` entry (the browser can't set
  arbitrary WS headers; hardened 2026-07-12 from the original `?token=` query
  param, which leaked tokens into access/proxy logs and is now rejected with 4001).
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
  `finally`. `main.py` extracts the token from the `Sec-WebSocket-Protocol`
  header, resolves auth, and builds the limiter from `app.state.redis` (or `None`).
- **Config**: `ws_require_auth` (env `WS_REQUIRE_AUTH`, default false), in `.env.example`.
- **Browser**: `websocket-worker.js` offers `["mockingbird", "bearer.<jwt>"]`
  subprotocols and treats 4001/4029 closes as
  terminal (no reconnect-storm); `audio-engine.js` `start(modelId, token)` forwards
  the token and emits `authError` / `limited`; `monitor.html` passes `getToken()` and
  surfaces both (logged-out visitors still get the echo demo).
- **Tests**: `tests/test_rate_limit.py` (concurrency, usage, per-plan caps, fail-open;
  real Redis, skips when down) + `tests/test_ws_voice.py` additions (anonymous
  echo-lock, tokenless-when-required → 4001, invalid token → 4001, authenticated
  round-trip, `acquire`-denied → 4029, real-Redis concurrency enforced end-to-end via
  the lifespan, `load_user_plan` against Postgres). `uv run pytest` is green (48) with
  Redis + Postgres up.

**Done when (met):** a valid token yields an authenticated, rate-limited session;
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

### M9 — HD Clone tier (RVC) — DONE (local); GPU fine-tune run pending

**Goal:** the PRODUCT_SPEC §4.2 second tier — fine-tuned RVC voices that beat the
instant clone, behind the **same** `BackendSession` / M5a ONNX contract (backend swap
only; no WS/gRPC change). This is the "M5c candidate" deferred in M5b. Same posture
as M5b's GPU bench and M8a's live-Twilio run: the pipeline, contracts, and UI are
built and offline-tested end to end; the real multi-hour GPU fine-tune that actually
beats the instant clone is the deferred tail.

What landed:

- **Gateway `VoiceModel` table** (`gateway/app/db/models.py`) + migration
  `ca561f6a0421` (chained after `a8f4c2d7e1b3`, same `checkfirst`-enum pattern as
  M8a; verified up/down/up against Postgres). One row per training job:
  status/progress/stage/error plus the PRODUCT_SPEC §6 artifact/quality/fine-tune
  fields.
- **Training API** (`gateway/app/training/routes.py`): `POST
  /api/voices/{voice_id}/train` (multipart clip, owned-voice check mirroring
  `calls/routes.py`, 202 + the new `VoiceModel` row) and `GET
  /api/voices/{voice_id}/train/status` (latest job for that voice, ETA derived
  from elapsed/progress). Feature-flagged like calling — `ENABLE_TRAINING`
  (already in `.env.example`) off, or the job queue unreachable, both return a
  clean 503 rather than touching the rest of the app.
- **Celery + Redis job queue** (`gateway/app/training/celery_app.py` + `tasks.py`
  + `db.py`): `train_voice` drives validation → preprocessing →
  feature_extraction → training → export → ready over a **synchronous**
  SQLAlchemy session (new `psycopg` dep — the rest of the app is async/asyncpg,
  but a Celery task runs outside any event loop). The one heavy step calls the
  inference service's `POST /train_hd` over HTTP
  (`gateway/app/inference/http.py::train_hd`, a sync httpx client for the
  worker). On success it registers a **new**, additive `Voice` registry row for
  the trained model (the source instant-clone row is untouched) so it streams
  through the existing `self_hosted` session unchanged. On any failure — a bad
  clip, a disconnected inference service, a `Voice.voice_id` collision — the row
  is marked `failed` with `error` set; Celery's default reconnect backoff
  (~20s) is tuned down so a dead broker/backend fails the enqueue in well under
  a second, never hangs the request.
- **Inference `POST /train_hd`** (`inference/app/training.py`, mounted on the
  health app): runs the pipeline in a thread (like `_clone_self_hosted`); no DB
  access — inference owns artifacts only, gateway is the sole DB owner, same
  split as M4b/M5b.
- **Torch-free HD pipeline** (`inference/app/export/hd_train.py`, mirrors
  `app/export/clone.py`): validates and decodes the clip (reusing
  `app.export.clone.decode_clip`, generalized to take a target rate + minimum
  length), reports progress through all five named stages, and — since no real
  RVC template exists yet — writes a **contract-valid synthetic
  `{model_id}.onnx`** (a tiny deterministic ONNX gain graph keyed off the
  clip's own hash), loudly logged as a stand-in. Verified end-to-end
  (`_verify_contract` loads and runs the graph in ORT) and confirmed to stream
  through the **unchanged** `self_hosted` backend with an exact 1:1 sample count.
- **Real single-graph export scaffold** (`inference/app/export/rvc/compose.py`
  + `scripts/export_rvc_onnx.py`, torch, `export` group only — no new
  dependency, M5b already added torch there): composes a HuBERT-content-encoder
  stand-in + F0-estimator stand-in + RVC/VITS-synthesizer stand-in into ONE
  audio-in/audio-out ONNX graph satisfying the M5a contract. Real, exportable
  architecture, but **placeholder weights, not a trained voice** — no vendored
  HuBERT/RVC checkpoint, no fine-tune loop (needs the rented GPU). When a real
  export lands at `models/rvc/rvc_converter.onnx`, `hd_train_local`
  automatically switches from the synthetic stand-in to baking a per-clip
  conditioning value into a copy of it, reusing
  `app.export.clone.bake_tgt_se` (generalized in M9 to take any initializer
  name, not just OpenVoice's `tgt_se`) — both branches verified directly.
- **Studio UI** (`frontend/templates/pages/studio.html`): an "HD Clone (train)"
  section — pick a registered voice, upload a longer sample, "Train HD" posts
  to the gateway, then polls `GET .../train/status` every 3s for a progress
  bar + stage label + ETA until `ready`/`failed`. Same Bearer-auth +
  vanilla-JS idiom as the rest of Studio; no npm/React.

**Verified live**, not just mocked: a real WAV upload through `POST
/api/voices/{id}/train`, against a real gateway + real inference process (over
real HTTP) + real Postgres/Redis, with Celery in eager mode — the job reaches
`ready`, the trained voice appears in `GET /voices`, and its ONNX file loads
and streams through `self_hosted`. Failure paths (inference error, dead
broker, a registry id collision) all degrade to a clean failed row + logged
warning, never a crash.

**Remaining (the real "beats the instant clone" bar; moves to its own GPU
run):** vendor real HuBERT + F0 + RVC/VITS pretrained weights into
`app/export/rvc/compose.py` in place of the placeholder blocks, implement the
actual per-speaker fine-tune loop (PRODUCT_SPEC §4.2: 30min–2hrs on a GPU), and
run `scripts/export_rvc_onnx.py` for real on the M5 `cloud_gpu` box — then
re-run this same pipeline end to end and do the side-by-side listening check
(PRODUCT_SPEC §15 criterion 4). Self-hosted/OpenVoice remains the primary
instant-clone tier regardless of this outcome.

**Done when (met, offline-testable slice):** a reference clip trains through
the full pipeline, exports to the M5a ONNX contract, and streams through
`self_hosted` end-to-end with progress/ETA visible in the Studio UI. **Not yet
met (needs the GPU run):** beating the instant clone of the same speaker in a
side-by-side listening check.

### M10 — UI completion + v1 sign-off — DONE (local); GPU-gated §15 items pending

**Goal:** close PRODUCT_SPEC §4.5 and run the §15 checklist. Last milestone of v1. Same
posture as M5b/M8a/M9: everything that can be built and offline-tested without a rented
GPU or a live Twilio/Supabase project is done; the handful of §15 criteria that
inherently need one of those are the deferred tail — sign-off on those, not more code,
is what remains.

What landed:

- **Routing (`/` → Dashboard, Monitor → `/monitor`):** `frontend/app/main.py` now serves
  `pages/dashboard.html` at `/` and moved the Live Monitor to `/monitor`. No HTTP
  redirect from the old path — Dashboard is a real page at `/`, not a bounce — but
  `base.html`'s nav and the Dashboard's own quick-start buttons put "Live Monitor" one
  click away for anyone with the old URL bookmarked.
- **Dashboard** (`frontend/templates/pages/dashboard.html`): client-fetched (no new
  gateway routes needed) — `GET /voices` for a voice-library summary (count + most
  recent, not a fabricated "active voice" concept: there is no persisted
  currently-selected-voice state anywhere yet, so this only ever claims what the
  registry actually shows) and `GET /api/calls` for the 5 most recent calls, plus
  quick-start links to Studio/Monitor/Dialer. Logged-out shows a sign-in prompt instead
  of crashing; the quick-start links still work anonymously (Monitor's echo demo).
- **Settings API** (`gateway/app/settings/`, new module, mirrors `app/voices/`'s
  package shape): `GET`/`PATCH /api/settings`, both scoped via `get_current_user`,
  read/merge-patch the `User.settings` JSON `MutableDict` (audio input/output device
  id, a `quality_preset` literal, and the three `getUserMedia` toggles the audio engine
  already exposed as config). `PATCH` is a true merge — `exclude_unset` means an
  omitted field never overwrites the stored value, only fields actually present in the
  request body do. Account fields (email/plan) are deliberately NOT stored here; the
  Settings page reads those from the existing `GET /auth/me` instead, so this module
  only ever owns the audio/quality blob, never identity.
- **Settings page** (`frontend/templates/pages/settings.html`): device pickers
  populated via `navigator.mediaDevices.enumerateDevices()` (a "Detect devices" button
  requests `getUserMedia` briefly, purely to unlock real device labels, then
  re-enumerates), a quality-preset select, the three audio toggles, and a read-only
  account panel from `/auth/me`. The selected input/output device ids round-trip
  through `AudioEngine`'s config (`inputDeviceId`/`outputDeviceId`) — Monitor best-effort
  fetches them before `start()` and applies the input device via a `getUserMedia`
  constraint and the output device via `AudioContext.setSinkId` (Chromium-only,
  feature-detected, silently a no-op elsewhere).
- **Fine-tune controls — the mini-spike** (PRODUCT_SPEC §6's `pitch_offset`/
  `speed_factor`/`breathiness`, added to `VoiceModel` in M9 "inert until M10"):
  - **Gateway** (`gateway/app/voices/routes.py`): `GET`/`PATCH /api/voices/{voice_id}`
    (`voice_id` = the `Voice.id` registry row, same convention as the M9 training
    routes). The knobs live on `VoiceModel`, not `Voice` — but a plain instant-clone
    `Voice` (M4b/M5b) has no `VoiceModel` row at all. Resolved with a single
    unambiguous join instead of a special case per path: `VoiceModel.model_path ==
    Voice.voice_id`. For an HD-trained voice this is already true (the M9 training
    task sets both to the same exported model id), so PATCHing an HD voice edits the
    *real* training-job row and its `similarity_score`/`mos_score` ride along in the
    response for free; a never-trained voice creates one lightweight `VoiceModel`
    companion row on first PATCH (`model_path` set immediately so the next PATCH finds
    it the same way). `PATCH` is a merge (unset fields keep their current value),
    ranges 422 via Pydantic `Field(ge=..., le=...)` matching PRODUCT_SPEC §6, and it
    best-effort pushes the merged values to inference — a failed push logs a warning
    and still returns 200 (the DB row is the source of truth regardless; the next
    stream/PATCH re-syncs it).
  - **Out-of-band channel** (`inference/app/tuning.py`): the gRPC `AudioFrame` proto and
    the WS JSON control protocol are both frozen, shared contracts (`agents/AGENTS.md`)
    — adding three low-frequency per-voice settings to either was rejected outright.
    Landed the same pattern already used for `POST /train_hd`: a plain HTTP side-channel
    on the inference FastAPI app, `POST /voices/{model_id}/tune`, called by the gateway.
    Params are stored process-locally on `SelfHostedBackend` (keyed by the same
    `model_id` a gRPC frame already carries — no new lookup key), not a database:
    inference owns model artifacts and now this adjacent config, gateway remains the
    sole durable owner in Postgres.
  - **DSP** (`inference/app/dsp.py`): three pure, allocation-cheap numpy functions —
    `shift_pitch` (single-frame spectral resample: remap FFT bins by `2**(semitones/12)`,
    inverse-FFT back to the exact input length), `adjust_speed` (resample to
    `round(N/factor)` then pad/truncate back to `N` — a "tape speed" effect that ties a
    small pitch shift to the tempo change, an accepted simplification for a per-block
    real-time effect), and `add_breathiness` (loudness-enveloped, high-pass-shaped
    noise mixed in proportion to `amount`). Every function is a no-op at its identity
    value and, critically, **always returns exactly as many samples as it was given** —
    that invariant is what let the hook drop into
    `_SelfHostedSession._convert_block` (`inference/app/backends/self_hosted.py`) as a
    single call after the existing seam crossfade, with zero changes to the
    block/frame-chunking math. The 1:1 input-frame-count == output-frame-count
    streaming cadence M5b/M9 protect is untouched — verified directly (a tuned stream
    and an untuned stream over the same input produce the same frame *count*, only
    different content) rather than assumed.
- **Monitor polish** (`frontend/templates/pages/monitor.html` +
  `frontend/static/js/audio-engine/`):
  - **Waveform visualizer**: real oscilloscope-style traces, not a level-history bar
    chart — an `AnalyserNode` tapped off the existing capture/playback graph (fan-out
    `connect()`, changes nothing about what reaches the speakers) feeds a
    `requestAnimationFrame` canvas draw loop for input and output.
  - **Voice similarity meter**: surfaces the real `VoiceModel.similarity_score` (via the
    new `GET /api/voices/{id}`) when one has been measured — which is rare in v1 (M9:
    "no automated scorer exists yet"). Otherwise falls back to a genuinely-computed,
    clearly-labeled **live estimate** — a new `CorrelationTracker`
    (`utils/metrics.js`) over the input/output level-meter samples already flowing
    every ~80ms — rather than inventing a number and presenting it as measured. Labeled
    in the UI as "live estimate (signal correlation) — not a trained similarity score."
  - **Transform on/off toggle**: a checkbox that calls the *existing*
    `engine.switchModel(null)` when off (pins the stream to echo without losing the
    dropdown's selection) and restores the selected voice when back on. No engine or
    protocol change — this is exactly the `switch_model` path M4a already wired.
  - Level meters unchanged.
- **Dialer polish** (`frontend/templates/pages/dialer.html` + `audio-engine.js`):
  **mute** (`engine.setMuted` — stops sending mic frames via the existing
  capture-worklet start/stop message), **hold** (`engine.setHold` — mutes both
  directions locally: mic capture *and* pausing playback of arriving audio; there is no
  Twilio hold-music/server-side bridge pause in v1, so this is a client-side
  approximation, but it delivers the user-visible behavior that matters — silence on
  both ends for the duration), and **volume** (`engine.setVolume` — a `GainNode`
  between playback and the destination, native and allocation-free). Mute/hold gates
  are independent booleans combined in one `_applyCaptureGate()` so toggling one never
  silently overrides the other; both reset on hangup, volume persists as a preference.
- **Tests deferred to the test-author pass** (per this milestone's brief — see the
  commit description / PR for the exact list): DSP unit tests (length preservation,
  identity no-ops, a synthetic-sine pitch-shift-direction check), the tuning route
  (validation, backend-mismatch 400, uninitialized-backend 503), the gateway
  GET/PATCH `/api/voices/:id` resolution (plain vs. HD voice, merge semantics,
  ownership 404s), the settings API (defaults, merge-patch, per-user scoping,
  validation), and the new frontend routes/nav. All exercised manually end-to-end
  during this milestone (real Postgres + Redis + a live browser session against both
  services) — see the PR description for what was actually run.

**Remaining (the GPU/live-credential tail, same posture as every other milestone):**
none of M10's own scope needs a GPU or live credentials — it's fully done. What's left
is entirely the **§15 sign-off items inherited from earlier milestones** (GPU latency
run, live-Twilio call, HD-clone listening check) — see the table below.

**Done when (met):** Dashboard/Settings/fine-tune controls/similarity meter/waveform
visualizer are present and working, offline-tested end to end against real Postgres +
Redis. **Not yet met (§15 criteria that need a GPU box or live credentials, not more
M10 code):** GPU-measured per-frame latency, the live-Twilio call, and the HD-clone
listening check — tracked in PRODUCT_SPEC §15 and the README's v1 status writeup.
