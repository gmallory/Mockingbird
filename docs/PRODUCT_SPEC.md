# Mockingbird — Product Specification

> **Version:** 2.1.0  
> **Last Updated:** 2026-07-07  
> **Status:** Active  

> **v2.1.0 changes (owner decisions, 2026-07-07):** success is now defined as a
> **demo-ready portfolio piece** — §15 replaced with measurable, binding criteria; the
> commercial metrics moved to Appendix C as aspirational. Scope settled: HD Clone (RVC)
> and the remaining UI pages **stay in scope** (scheduled as M9/M10 in
> [ROADMAP.md](ROADMAP.md)); inbound calls, call recording, WebRTC peer calls, contacts,
> model export/sharing, and A/B testing are **descoped to future/commercial**.
> Implemented-state sync: M6b (WS auth + rate limiting), M7 (CI + Prometheus/Grafana),
> and M8a (outbound Twilio PSTN calls) landed; data models (§6), API tables (§7), and
> the milestone view (§13) updated to match the code.

> **v2.0.0 changes:** synced to the implemented system (M1–M4 done, see
> [ROADMAP.md](ROADMAP.md)): explicit inference backend modes with self-hosted GPU as the
> confirmed first-priority engine and Cartesia/cloud-GPU as separate modes (owner decision
> 2026-07-04); corrected Cartesia capabilities (clip-based, not streaming); data models and
> APIs split into *implemented* vs *planned*; WebSocket contract deferred to
> [agents/AGENTS.md](../agents/AGENTS.md); latency budget marked as a target with measured
> Cartesia numbers recorded.

---

## 1. Executive Summary

**Mockingbird** is a web-based, real-time voice cloning application that captures a user's speech, transforms it to sound like a different person, and outputs the transformed audio — all with sub-300ms latency. The primary use case is live voice-over during phone calls, enabling the user to speak in a cloned voice in real time.

Users upload a short audio sample of a target voice, train (or instantly clone) a voice model, and then use Mockingbird to transform their live speech into that voice during calls made through the app's built-in VoIP system.

Mockingbird is built as a **portfolio piece** (owner decision, 2026-07-07): the binding
definition of success is §15, not the commercial metrics (those are aspirational,
Appendix C). The headline demo: clone a voice, speak through it live into a real phone
call, with GPU-measured latency inside the budget in §4.1.

---

## 2. Problem Statement

Current voice transformation tools are either:
- **Desktop-only** and require installing system-level virtual audio drivers (Voicemod, MorphVOX)
- **Offline/batch-only** with no real-time capability (Murf.ai, Bark, Tortoise-TTS)
- **Low-quality** pitch-shifting effects that don't produce convincing voice changes

There is no web-based product that provides **real-time, high-fidelity voice cloning** with integrated calling. Mockingbird fills this gap.

---

## 3. Target Users

| Persona                       | Description                                                            |
| ----------------------------- | ---------------------------------------------------------------------- |
| **Content Creators**          | YouTubers, podcasters, streamers who want to create character voices   |
| **Voice Actors**              | Professionals who need to quickly audition different voice styles      |
| **Privacy-Conscious Callers** | Users who want to mask their real voice during calls                   |
| **Entertainment Users**       | People who want to prank friends or have fun with voice transformation |

---

## 4. Core Features

### 4.1 Real-Time Voice Cloning Engine

The heart of Mockingbird — transforms live speech into a target voice with <300ms latency.

#### Inference Backend Modes

The inference engine is selected via `INFERENCE_BACKEND` (see `inference/app/config.py` and
`.env.example`). All backends implement the same per-stream `BackendSession` interface (§8),
so switching modes never changes the WS/gRPC contract. These are **separate, co-equal
modes** — the cloud modes are not fallbacks for the GPU path.

| Mode                        | What it is                                                                                                                                                                                                                                                                                                                           | Latency model                                                                                      | Status                     |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- | -------------------------- |
| **`self_hosted`** (primary) | OpenVoice V2 (+ RVC later) on ONNX Runtime. **First-priority engine** — the path this spec's per-frame latency budget applies to. Streaming block engine + ONNX model contract landed in M5a; real OpenVoice weights, ONNX export, and instant clone landed in M5b (RVC single-graph export deferred — needs HuBERT+F0 composition). | Per-frame streaming; **measured on real weights** (see below); GPU (`cloud_gpu`) run still pending | Real weights running (M5b) |
| **`cloud_gpu`**             | The same self-hosted inference stack deployed on a rented GPU (A10G/L4): run inference there with `INFERENCE_BACKEND=cloud_gpu`, point the gateway's `INFERENCE_GRPC_URL` at that box.                                                                                                                                               | Per-frame streaming, + extra network RTT                                                           | Mode implemented (M5a)     |
| **`cartesia`**              | Cartesia's **clip-based** voice changer (`/voice-changer/sse`). Utterance-segmented: VAD detects end-of-speech, the whole utterance is converted as a clip. Walkie-talkie feel.                                                                                                                                                      | Utterance latency (see below); **measured felt floor ~2s**                                         | Implemented (M4a–c)        |
| **`elevenlabs`**            | ElevenLabs speech-to-speech (also clip-in / stream-out).                                                                                                                                                                                                                                                                             | Utterance latency                                                                                  | Placeholder                |
| `passthrough`               | Echo, for dev/tests.                                                                                                                                                                                                                                                                                                                 | 1:1 frames                                                                                         | Implemented (M1/M3)        |

Two latency metrics apply, one per latency model:

- **Per-frame latency** (`self_hosted` / `cloud_gpu`): the ~172ms end-to-end budget below.
  **Measured on real weights as of M5b** (2026-07-05, dev Mac M-series CPU, exported
  OpenVoice V2 converter @22050Hz, `scripts/self_hosted_bench.py`):
  - Tuned defaults `SELF_HOSTED_BLOCK_MS=60` / `SELF_HOSTED_CONTEXT_MS=140`:
    per-block conversion **p50 46.2ms / p95 47.5ms / max 54.4ms**, real-time factor
    **0.77x**, effective server-side added latency (block buffer + p95 conversion)
    **~107ms** (+ up to 5ms crossfade holdback; on the OpenVoice export the
    hop-truncation deficit rule keeps seams contiguous instead, so the blend —
    and its latency — is inactive).
  - The context+block window drives compute: 100/200 gives p95 ~90ms (~190ms added,
    RTF 0.71); 60ms blocks with the old 200ms context push RTF past 1.0 on CPU.
    CoreML EP is no faster than CPU here (graph partially falls back).
  - Real speech through the full streaming session (clone → convert): RTF 0.83,
    output stream exactly 1:1 with input frames.
  - **Laptop-CPU p95 (47.5ms) already sits under the 80ms GPU inference line**; the
    line is locked from a proper GPU run — `infrastructure/scripts/provision_cloud_gpu.sh`
    provisions a rented A10G/L4 box and re-runs this benchmark (still pending).
  - M5a pipeline-overhead floor (identity graph) for reference: p95 0.57–4.27ms/block.
  Self-hosted remains the primary engine even though the first full budget
  measurement (with network legs, on GPU) is still ahead.
- **Utterance latency** (`cartesia` / `elevenlabs`): end-of-speech → first output frame,
  measured gateway-side (implemented in the Live Monitor as of M4c). Measured for Cartesia
  (2026-07-03 spike): ~500ms VAD hangover + ~0.8–1s fixed overhead + ~0.45× realtime;
  felt floor ~2.0s for a 2s utterance. Sub-second is not achievable in this mode.

#### Architecture: Hybrid Server-Side Processing

```
┌─────────────────────────────────────────────────────────────┐
│                    BROWSER (Client)                          │
│                                                              │
│  ┌────────────┐   ┌────────────────┐   ┌────────────────┐   │
│  │ FastAPI UI │   │  Audio Engine  │   │  WebSocket     │   │
│  │ +Jinja/HTMX│◄─►│  (Vanilla JS)  │◄─►│  Worker        │   │
│  │            │   │                │   │                │   │
│  │ • Controls │   │ • AudioWorklet │   │ • Binary PCM   │   │
│  │ • Viz      │   │ • Ring Buffer  │   │ • Auth token   │   │
│  │ • Settings │   │ • VAD          │   │ • Reconnect    │   │
│  └────────────┘   └────────────────┘   └───────┬────────┘   │
│                          ▲                      │            │
│                          │ postMessage           │ wss://     │
│                          ▼                      ▼            │
├──────────────────────────────────────────────────────────────┤
│                        NETWORK                                │
├──────────────────────────────────────────────────────────────┤
│                    EDGE GPU SERVER                            │
│                                                              │
│  ┌───────────────┐         ┌──────────────────────────┐     │
│  │ Python        │  gRPC   │  Python (FastAPI)         │     │
│  │ Gateway       │────────►│  ML Inference Service     │     │
│  │               │         │                          │     │
│  │ • WebSocket   │         │  ┌──────────────────┐    │     │
│  │   management  │         │  │  RVC / OpenVoice  │    │     │
│  │ • Auth        │         │  │  ONNX Runtime     │    │     │
│  │ • Rate limit  │         │  │  GPU-accelerated  │    │     │
│  │ • Routing     │         │  └──────────────────┘    │     │
│  └───────────────┘         │  + Silero VAD            │     │
│                            │  + Streaming inference   │     │
│                            │  + Model hot-swap        │     │
│                            └──────────────────────────┘     │
└──────────────────────────────────────────────────────────────┘
```

#### Voice Conversion Pipeline

1. **Capture** — Browser captures mic audio via `getUserMedia()` at 48kHz
2. **Preprocess** — `AudioWorkletProcessor` buffers 20ms chunks, applies lightweight VAD (Silero), and noise gating
3. **Transport** — `WebSocket Worker` sends binary PCM frames to the edge server over `wss://`
4. **Inference** — Server-side RVC model (ONNX-optimized, GPU-accelerated) performs voice conversion in ~80ms
5. **Return** — Transformed audio streamed back as binary PCM via WebSocket
6. **Playback** — `AudioWorkletProcessor` reads from its ring buffer (fed via
   `postMessage`; a `SharedArrayBuffer` hand-off is a noted future optimization) and
   outputs to destination

#### Latency Budget (per-frame path: `self_hosted` / `cloud_gpu`)

Server-side stages measured on real weights in M5b (laptop CPU: model inference
p95 47.5ms, inside the 80ms line); network legs and the GPU run still pending.

| Stage                     | Target     | Notes                                    |
| ------------------------- | ---------- | ---------------------------------------- |
| Audio Capture + Buffering | 10ms       | 128-sample AudioWorklet render quanta    |
| Encoding (PCM → Int16)    | 2ms        | Lightweight conversion                   |
| Network Upload (edge)     | 20ms       | Edge-deployed server, <40ms RTT          |
| Server Preprocessing      | 5ms        | VAD check, normalization                 |
| **Model Inference (GPU)** | **80ms**   | Optimized RVC on ONNX Runtime + TensorRT |
| Server Postprocessing     | 5ms        | Volume normalization, artifact smoothing |
| Network Download          | 20ms       | Same edge path                           |
| Decode + Playback Buffer  | 30ms       | Jitter buffer for smooth output          |
| **Total**                 | **~172ms** | ✅ Well under 300ms target                |

---

### 4.2 Voice Model Training & Management

#### Two-Tier Cloning System

| Mode                                     | Audio Required | Ready In     | Quality         | Use Case                | Status                                                 |
| ---------------------------------------- | -------------- | ------------ | --------------- | ----------------------- | ------------------------------------------------------ |
| **Instant Clone** (OpenVoice / Cartesia) | 10–30 seconds  | Immediate    | ★★★★ Good       | Quick setup, previewing | **Implemented** (M4b Cartesia, M5b self-hosted)        |
| **HD Clone** (RVC fine-tune)             | 10–30 minutes  | 30 min–2 hrs | ★★★★★ Excellent | Production calls        | **Planned — M9** (in scope, owner decision 2026-07-07) |

#### Training Pipeline (planned — M9)

```
Audio Upload → Validation → Preprocessing → Feature Extraction → Training → Export
     │              │             │                │                │          │
     ▼              ▼             ▼                ▼                ▼          ▼
  Format check  Noise level   Denoise,       Mel spectrograms,  Fine-tune   ONNX model
  Duration check SNR analysis  Normalize,     F0 extraction,     base RVC    checkpoint
  Sample rate    Clipping      Silence trim,  Speaker embedding  on GPU      for inference
                 detection     Segmentation   (d-vector)         cluster
```

#### Voice Model Features
- **Voice Library** — Browse and manage saved voice models — *implemented* (per-user
  registry + Studio list, M4b/M6a)
- **Model Sharing** — Export/import voice models (encrypted `.mbv` format) —
  *descoped: future/commercial*
- **A/B Testing** — Compare Instant vs HD clone side by side — *descoped: future/commercial*
- **Fine-Tune Controls** — Adjust pitch offset, breathiness, speed after training —
  *planned (M10)*
- **Training Progress** — Real-time progress bar with estimated time remaining —
  *planned (M9, with HD training)*

---

### 4.3 Built-in VoIP Calling (Primary Audio Routing)

Since a web browser cannot install system-level virtual audio drivers, Mockingbird integrates its own calling system.

#### Architecture (implemented — M8a)

The gateway drives Twilio's REST API directly (thin httpx wrapper, no vendor SDK) and
terminates the call's **Media Stream** itself — there is no browser WebRTC leg:

```
User's Browser ── /ws/voice (existing session, join_call) ──► Gateway
                                                                │  CallBridge (per-call queues)
Twilio ◄── REST create call (TwiML <Connect><Stream>) ──────────┤
   │                                                            │
   └── Media Stream ── wss://…/ws/twilio/{call_id} (mu-law 8k) ─┘
                                                                ▼
                                              Recipient's Phone (regular phone call)
```

The browser session's *converted* output is transcoded 48kHz PCM → G.711 mu-law 8kHz
(`gateway/app/calls/telephony.py`) and routed to the phone leg; callee audio flows back
to the browser. Teardown via Twilio status callback (signature-validated) or explicit
hangup.

#### Calling Features
- **Outbound PSTN calls** — Dial any phone number; voice is transformed before reaching
  the recipient — *implemented (M8a); live-Twilio verification run pending*
- **Call controls** — Hangup + mid-call model hot-swap (`switch_model`) implemented;
  mute/hold/volume — *planned (M10, with the dialer polish)*
- **Dial pad** — E.164 input, per-user call history, live meters on `/dialer` —
  *implemented (M8a)*
- **Inbound calls** — Dedicated Mockingbird phone number — *descoped: future/commercial*
- **WebRTC peer calls** — Browser-to-browser calls — *descoped: future/commercial*
- **Call recording** — Original + transformed audio (with consent) — *descoped:
  future/commercial*
- **Contacts** — *descoped: future/commercial*

#### PSTN Gateway Integration (Twilio)
- Cost: ~$0.013/min outbound (US numbers); phone number provisioning $1.15/mo
- Integration: Twilio REST API + Media Streams, gateway-terminated (no `twilio-client.js`,
  no browser SDK)

---

### 4.4 Audio Routing Modes

Mockingbird supports multiple audio routing strategies for different use cases:

| Mode                           | How It Works                                                         | Best For           | Latency |
| ------------------------------ | -------------------------------------------------------------------- | ------------------ | ------- |
| **Built-in VoIP** (Primary)    | Calls made through Mockingbird's integrated dialer                   | Phone calls        | ~170ms  |
| **Browser Extension** (Future) | Chrome extension intercepts WebRTC in Google Meet, Zoom Web, Discord | Video conferencing | ~150ms  |
| **Companion App** (Future)     | Electron/Tauri desktop app with virtual audio driver                 | Any desktop app    | ~120ms  |
| **Preview Mode**               | Real-time mic → transformed output through speakers                  | Testing & demo     | ~170ms  |

---

### 4.5 Web Application UI

#### Pages & Views

| Page             | Description                                                            | Status                               |
| ---------------- | ---------------------------------------------------------------------- | ------------------------------------ |
| **Dashboard**    | Overview: active voice model, recent calls, quick-start actions        | Planned — M10                        |
| **Voice Studio** | Upload samples, clone voices, manage voice library                     | Implemented (M4c, login-gated M6a)   |
| **Dialer**       | Phone interface — dial pad, recent calls, mid-call controls            | Implemented (M8a; contacts descoped) |
| **Live Monitor** | Live mic → transform → playback loop, utterance latency + level meters | Implemented (M4a/M4c, at `/`)        |
| **Settings**     | Audio I/O config, quality presets, account                             | Planned — M10 (billing descoped)     |

#### Key UI Components
- **Latency Indicator** — utterance latency + p95 on the Monitor — *implemented (M4c)*;
  per-frame path latency lives in the Grafana dashboard (M7)
- **Quick Switch** — voice dropdown switching the live model — *implemented (M4c)*
- **Waveform Visualizer** — input vs. output waveform comparison — *planned (M10; level
  meters exist today)*
- **Voice Similarity Meter** — how closely output matches the target — *planned (M10)*
- **Transformation Toggle** — instant on/off for voice transformation — *planned (M10;
  today: deselecting the voice reverts to echo)*

---

## 5. Technology Stack

### Frontend
| Technology                                                           | Purpose                                                               |
| -------------------------------------------------------------------- | --------------------------------------------------------------------- |
| **FastAPI + Jinja2 + HTMX** (Python)                                 | Server-rendered UI, routing, page handlers                            |
| **Uvicorn**                                                          | ASGI server                                                           |
| **Web Audio API**                                                    | AudioWorklet for real-time audio processing (minimal browser JS glue) |
| **WebSocket (binary)**                                               | Streaming audio to/from inference server (raw PCM Int16)              |
| **Twilio REST + Media Streams** (httpx, gateway-side; no vendor SDK) | PSTN calling integration (M8a)                                        |
| **Canvas API**                                                       | Waveform and spectrogram visualizations *(planned — M10)*             |
| **SharedArrayBuffer**                                                | Zero-copy worklet hand-off *(future optimization; postMessage today)* |

### Backend — Gateway
| Technology                | Purpose                                            |
| ------------------------- | -------------------------------------------------- |
| **Python 3.14** (FastAPI) | WebSocket connection management, auth, routing     |
| **Redis**                 | Session management, model routing, pub/sub         |
| **PostgreSQL**            | User accounts, voice models metadata, call history |
| **S3 / GCS**              | Audio sample storage, model weight storage         |

### Backend — ML Inference
| Technology                                 | Purpose                                                                                        |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| **Python 3.14** (FastAPI + Uvicorn)        | ML service orchestration, WebSocket audio streaming                                            |
| **OpenVoice v2**                           | Zero-shot instant voice cloning — *implemented (M5b, ONNX export)*                             |
| **ONNX Runtime**                           | Optimized model inference — *implemented (M5a; CUDA/CoreML/CPU)*                               |
| **RVC** (Retrieval-based Voice Conversion) | HD Clone conversion engine — *planned (M9)*                                                    |
| **FAISS**                                  | Feature retrieval for RVC — *planned (M9)*                                                     |
| **Celery + Redis**                         | Async training job queue — *planned (M9)*                                                      |
| **TensorRT**                               | NVIDIA GPU acceleration — *pending the rented-GPU deploy (M5 bench)*                           |
| **Silero VAD**                             | Voice activity detection — *energy/RMS VAD implemented instead (M4a); Silero optional upgrade* |
| **GPT-SoVITS**                             | Alternative zero-shot cloning — *descoped: future/optional*                                    |

### Infrastructure
| Technology                              | Purpose                                                                             |
| --------------------------------------- | ----------------------------------------------------------------------------------- |
| **Docker Compose**                      | Full local stack (Postgres, Redis, 3 services, monitoring) — *implemented (M4c/M7)* |
| **GitHub Actions**                      | CI: format + lint + tests × 3 services + image builds — *implemented (M7)*          |
| **Prometheus + Grafana**                | Latency monitoring, system metrics — *implemented (M7)*                             |
| **Twilio**                              | PSTN gateway, phone numbers — *implemented (M8a)*                                   |
| **NVIDIA GPU node** (A10G / L4, rented) | Inference compute — *provisioning script ready; bench run pending (M5)*             |
| **Kubernetes**                          | Container orchestration — *descoped: future/commercial*                             |
| **CloudFlare / Fly.io Edge**            | Edge deployment for low latency — *descoped: future/commercial*                     |
| **Sentry**                              | Error tracking — *descoped: future/commercial*                                      |

---

## 6. Data Models

### Voice (implemented — M4b; per-user M6a)

The registry row backing both clone flows. See `gateway/app/db/models.py::Voice`
(SQLModel, Alembic-migrated).

```python
class Voice(SQLModel, table=True):
    id: UUID                    # pk
    user_id: UUID               # owner (FK -> user.id, indexed) — M6a
    voice_id: str               # unique, indexed — Cartesia voice id OR self-hosted
                                # ONNX model id; feeds the streaming path directly
    label: str                  # human name shown in the UI
    language: str
    created_at: datetime
```

### VoiceModel (planned — M9, HD Clone training)

The richer model below is the **future** shape for self-hosted HD training (model
artifacts, training status, quality metrics), scheduled with the RVC tier in M9.

```python
class VoiceModel(BaseModel):
    id: UUID                                            # UUID
    user_id: UUID                                       # Owner
    name: str                                           # Display name (e.g., "Morgan Freeman")
    type: Literal["instant", "hd"]                      # Clone quality tier
    status: Literal["training", "ready", "failed"]

    # Training metadata
    sample_duration_sec: float                          # Total training audio duration
    sample_count: int                                   # Number of audio segments
    training_started_at: datetime
    training_completed_at: datetime | None = None

    # Model artifacts
    model_path: str                                     # S3/GCS path to model weights
    model_size_bytes: int
    onnx_path: str | None = None                        # Optimized ONNX model path

    # Quality metrics
    similarity_score: float | None = None               # 0-1 voice similarity to target
    mos_score: float | None = None                      # Mean Opinion Score (quality)

    # Configuration
    pitch_offset: float                                 # Semitones adjustment (-12 to +12)
    speed_factor: float                                 # Playback speed (0.5 to 2.0)
    breathiness: float                                  # 0.0 to 1.0

    created_at: datetime
    updated_at: datetime
```

### CallRecord (implemented — M8a)

One outbound PSTN call. See `gateway/app/db/models.py::CallRecord`. The `id` doubles as
the media-bridge/stream id (`/ws/twilio/{id}`, `join_call` target).

```python
class CallRecord(SQLModel, table=True):
    id: UUID                    # pk; also the bridge/stream id
    user_id: UUID               # FK -> user.id, indexed
    voice_id: UUID | None       # FK -> voice.id (None = echo/passthrough call)
    direction: CallDirection    # outbound (inbound descoped)
    status: CallStatus          # active -> completed | failed
    phone_number: str           # E.164 destination
    twilio_call_sid: str | None # set after REST create; status callback correlates on it
    started_at: datetime
    ended_at: datetime | None
    duration_sec: float
```

Descoped with M8b (future/commercial): `peer_id` (P2P), per-call latency/dropout
metrics (live in Prometheus histograms instead — M7), recording paths.

### User (implemented — M2/M6a)

Mirror of the Supabase identity (id = Supabase `sub`), materialized on first
authenticated request. See `gateway/app/db/models.py::User`.

```python
class User(SQLModel, table=True):
    id: UUID                    # Supabase auth user id (sub)
    email: str                  # cached display attribute (Supabase owns identity)
    display_name: str
    plan: Plan                  # free | pro | enterprise — drives WS rate limits (M6b)
    monthly_minutes_used: float # banked on WS session close (M6b)
    twilio_phone_number: str | None
    settings: dict              # JSON — audio prefs land here (Settings UI, M10)
    created_at: datetime
    updated_at: datetime
```

Per-plan limits (`monthly_minutes_limit`, connection caps) are code constants
(`PLAN_LIMITS`), not columns.

---

## 7. API Design

### WebSocket — Audio Streaming

**The canonical WS contract (binary frame format + JSON control messages) lives in
[agents/AGENTS.md](../agents/AGENTS.md)** — it is the shared contract across frontend,
gateway, and inference, and **it wins over any copy elsewhere, including this file**.
Summary: binary raw PCM Int16 frames (20ms, 960 samples at 48kHz) both directions; JSON
control messages `start` / `switch_model` / `stop` / `ping` client→server and `ready` /
`model_loaded` / `error` (with `code`) / `degraded` / `pong` server→client.

### REST API — implemented (M2–M8a)

`/voices` and `/api/calls*` are per-user (Supabase bearer token, M6a). `/ws/voice` auth
is **optional** (M6b): a valid `?token=` yields an authenticated, rate-limited session;
no token yields the echo-only demo (unless `WS_REQUIRE_AUTH=true`); an invalid token is
always rejected (close 4001; over-cap 4029). Gateway on `:3001`, inference on `:8001`.

| Method | Endpoint                 | Service            | Auth                     | Description                                                                                              |
| ------ | ------------------------ | ------------------ | ------------------------ | -------------------------------------------------------------------------------------------------------- |
| GET    | `/healthz`               | gateway, inference | none                     | Health incl. Postgres/Redis (gateway)                                                                    |
| GET    | `/metrics`               | gateway, inference | none                     | Prometheus exposition (M7)                                                                               |
| WS     | `/ws/voice`              | gateway            | optional `?token=` (M6b) | Binary audio streaming (contract above); `join_call` routes output to a call bridge (M8a)                |
| WS     | `/ws/twilio/{call_id}`   | gateway            | per-call secret (M8a)    | Twilio Media Stream leg of an outbound call                                                              |
| POST   | `/auth/signup`           | gateway            | none                     | Proxy signup to Supabase (GoTrue); returns the session                                                   |
| POST   | `/auth/login`            | gateway            | none                     | Proxy password login to Supabase; returns the session                                                    |
| GET    | `/auth/me`               | gateway            | Bearer                   | Verify the token, return the mirrored local user                                                         |
| GET    | `/voices`                | gateway            | Bearer                   | List the caller's cloned voices                                                                          |
| POST   | `/voices`                | gateway            | Bearer                   | Multipart `clip` + `label` + `language` → proxies to inference clone, persists a row owned by the caller |
| POST   | `/voices`                | inference          | none                     | Multipart clone, returns `voice_id` (internal, called by gateway)                                        |
| POST   | `/api/calls/outbound`    | gateway            | Bearer                   | Create Twilio call + bridge, persist `CallRecord` (M8a)                                                  |
| GET    | `/api/calls`             | gateway            | Bearer                   | List the caller's call history (M8a)                                                                     |
| GET    | `/api/calls/{id}`        | gateway            | Bearer                   | Call details (M8a)                                                                                       |
| POST   | `/api/calls/{id}/hangup` | gateway            | Bearer                   | End the call; record closes regardless of Twilio (M8a)                                                   |
| POST   | `/api/twilio/status`     | gateway            | X-Twilio-Signature       | Status callback; drives teardown (M8a)                                                                   |

Auth model (M6a): Supabase (GoTrue) mints access tokens; the gateway proxies
signup/login to it and verifies the returned HS256 token offline against the project
JWT secret. A local `User` row (id = Supabase `sub`) is mirrored on first authenticated
request so `Voice.user_id` can FK to it.

### REST API — planned

Bearer-authenticated; milestones per [ROADMAP.md](ROADMAP.md).

| Method | Endpoint                       | Description                                       | Milestone         |
| ------ | ------------------------------ | ------------------------------------------------- | ----------------- |
| POST   | `/api/voices/:id/train`        | Trigger HD training (RVC fine-tune)               | M9                |
| GET    | `/api/voices/:id/train/status` | Check training progress                           | M9                |
| GET    | `/api/voices/:id`              | Get voice model details                           | M10               |
| DELETE | `/api/voices/:id`              | Delete voice model                                | M10               |
| PATCH  | `/api/voices/:id`              | Update model settings (pitch, speed, breathiness) | M10               |
| POST   | `/api/voices/:id/preview`      | Generate preview audio clip                       | M10               |
| GET    | `/api/user/usage`              | Get usage statistics                              | future/commercial |

---

## 8. Audio Processing Pipeline — Detailed

### Browser-Side (AudioWorklet)

```javascript
// voice-capture-processor.js
class VoiceCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(960); // 20ms at 48kHz
    this.bufferIndex = 0;
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0]?.[0]; // Mono channel
    if (!input) return true;

    // Accumulate 128-sample render quanta into 960-sample (20ms) chunks
    for (let i = 0; i < input.length; i++) {
      this.buffer[this.bufferIndex++] = input[i];
      if (this.bufferIndex >= 960) {
        // Send 20ms chunk to main thread → WebSocket worker
        this.port.postMessage({
          type: 'audio',
          data: this.buffer.slice()
        });
        this.bufferIndex = 0;
      }
    }
    return true;
  }
}
registerProcessor('voice-capture', VoiceCaptureProcessor);
```

### Server-Side (implemented shape — M3/M4a)

Conversion is **not** 1:1 per chunk. The gateway terminates the browser WS
(`gateway/app/websocket/handler.py`) and proxies frames over a gRPC `Convert` stream to
inference, which opens one **per-stream `BackendSession`**
(`inference/app/backends/base.py`). A session may buffer many input frames and emit zero
or many output frames per push — this is what lets clip-based backends (Cartesia) and
per-frame backends (self-hosted) share one interface:

```python
# inference/app/backends/base.py (shape)
class BackendSession:
    async def push(self, frame: bytes) -> list[bytes]: ...   # 0..n output frames
    async def flush(self) -> list[bytes]: ...                # drain on stream end
    async def aclose(self) -> None: ...

class Backend:
    def open_session(self, model_id: str) -> BackendSession: ...
```

- `passthrough`: `push()` returns `[frame]` (1:1 echo).
- `cartesia`: energy/RMS VAD buffers frames; on end-of-speech, wraps the utterance as WAV,
  POSTs to `/voice-changer/sse`, re-chunks the result into 1920-byte frames.
- `self_hosted` / `cloud_gpu` (M5a, tuned in M5b): block-streaming ONNX inference —
  frames buffer into 60ms blocks, each converted with 140ms of left context; `push()`
  emits output frames every block, so latency is block size + inference, independent of
  utterance length. Block seams are crossfaded (`SELF_HOSTED_CROSSFADE_MS`, default 5ms
  of held-back tail blended with the next block's re-rendering of the same span), and a
  model that truncates a partial STFT hop per window (<25ms) still yields an exactly 1:1
  output stream. Models are `{model_id}.onnx` files (float32 `[1, N]` audio in/out at
  `SELF_HOSTED_MODEL_SAMPLE_RATE`), resolved from `SELF_HOSTED_MODEL_DIR` or
  `s3://$S3_BUCKET/models/`, LRU-cached per process. Real models are exported OpenVoice
  V2 converters (M5b): the target speaker embedding is baked into the graph as the
  `tgt_se` initializer; instant clone = run the exported SE encoder on a reference clip
  and patch that initializer (`inference/app/export/clone.py`, no torch in the service).

The gateway degrades to echo if inference is unavailable, and serializes all WS sends
while a concurrent reader task forwards inference output.

---

## 9. Voice Cloning Models — Detailed Comparison

### Shipped Engine: OpenVoice v2 (Instant Clone — M5b)

**Why OpenVoice shipped first** (engine decision, M5b): the tone-color converter exports
to a *single* audio-in/audio-out ONNX graph satisfying the M5a streaming contract, and
zero-shot cloning wires directly into the `voices` registry (clip → voice in seconds,
no training job).

- Requires only 10–30 seconds of reference audio (no training!)
- Fast inference (measured: laptop-CPU p95 47.5ms/block, RTF 0.77 — §4.1)
- Decoupled architecture: tone color separated from style/language
- MIT / Apache 2.0 license

### HD Engine: RVC (Retrieval-based Voice Conversion — planned, M9)

**Why RVC for the HD tier:**
- Purpose-built for real-time speech-to-speech voice conversion
- Best latency/quality tradeoff (~40–80ms inference on GPU)
- MIT licensed — fully open for commercial use
- Large community, well-documented, actively maintained
- FAISS-based retrieval produces natural-sounding output
- ONNX export supported for optimized inference

**Architecture:**
```
Input Audio → Content Encoder (HuBERT) → FAISS Retrieval → VITS Decoder → Transformed Audio
                                                ▲
                                                │
                                          Target Voice
                                          Feature Index
```

### GPT-SoVITS (Zero-Shot Alternative — descoped: future/optional)

**Why GPT-SoVITS as fallback:**
- Needs only ~5 seconds of audio for usable clone
- Very high quality output
- MIT licensed
- Good for languages beyond English

### Cloud Mode: Cartesia (implemented — M4)

A separate no-GPU mode (not a fallback for the GPU path). **Verified against the live API:
Cartesia's voice changer is clip-based** — `/voice-changer/bytes` and `/voice-changer/sse`
take a whole clip; the realtime WebSocket is **TTS-only**, so there is no streaming-input
voice conversion. Per-frame <172ms is not possible in this mode; it runs
utterance-segmented (walkie-talkie), with measured felt latency ~2s+ (§4.1).
- Voice cloning from short samples via `POST /voices/clone` (implemented in M4b)
- Prosody-preserving audio→audio conversion (keeps the speaker's delivery and tics)
- Usage-based pricing; runs anywhere, no GPU required

---

## 10. Non-Functional Requirements

The **Performance** table is binding for v1 (per-frame path; verified by the M5 GPU
bench + M8a live run — see §15). **Scalability** and the uptime row are
future/commercial targets, not v1 gates.

### Performance (binding, v1)
| Metric                     | Target                                                                |
| -------------------------- | --------------------------------------------------------------------- |
| End-to-end voice latency   | < 300ms (P95); ~172ms budget in §4.1                                  |
| Model inference time       | < 100ms per block (80ms GPU line; laptop-CPU p95 47.5ms measured M5b) |
| Audio dropout rate         | < 0.1% of chunks                                                      |
| Time to first audio output | < 500ms after call connect                                            |

### Scalability (future/commercial)
| Metric                   | Target                                  |
| ------------------------ | --------------------------------------- |
| Concurrent users per GPU | 8–12 (A10G), 15–20 (A100)               |
| Concurrent active calls  | 10,000+ (with auto-scaling GPU cluster) |
| Voice model storage      | Unlimited (S3/GCS)                      |
| Training job throughput  | 50+ concurrent jobs                     |

### Reliability
| Metric                   | Target                                                             |
| ------------------------ | ------------------------------------------------------------------ |
| Service uptime           | 99.9% *(future/commercial — no hosted deployment in v1)*           |
| WebSocket reconnect time | < 2 seconds — *implemented (backoff reconnect, M1)*                |
| Graceful degradation     | Passthrough (unmodified voice) on failure — *implemented (M3/M5a)* |

### Security
| Requirement                 | Implementation                                                          |
| --------------------------- | ----------------------------------------------------------------------- |
| Audio encryption in transit | TLS 1.3 (wss://) — deployment/tunnel concern                            |
| Audio encryption at rest    | AES-256 for stored recordings *(future — recording descoped)*           |
| Voice model isolation       | Per-user rows + owned-voice checks — *implemented (M6a/M8a)*            |
| Authentication              | Supabase (GoTrue) JWT — *implemented (M6a/M6b)*                         |
| Rate limiting               | Per-user WS concurrency + monthly minutes (Redis) — *implemented (M6b)* |

---

## 11. User Flows

### Flow 1: First-Time Voice Clone

```
1. Sign up / Log in
2. Navigate to Voice Studio
3. Click "Create New Voice"
4. Choose clone mode:
   a. Instant Clone → Record/upload 10-30 seconds → Model ready instantly
   b. HD Clone → Record/upload 10-30 minutes → Training begins (30 min–2 hrs)
5. Preview the cloned voice with sample text
6. Adjust settings (pitch, breathiness, speed)
7. Save to voice library
```

### Flow 2: Making a Transformed Call

```
1. Open Dialer page
2. Select voice model from Quick Switch bar
3. Toggle "Voice Transform" ON
4. Dial phone number or select contact
5. Call connects → Voice is transformed in real-time
6. Mid-call options:
   - Switch voice models
   - Toggle transform on/off
   - Adjust pitch/speed
   - Mute
7. End call → Summary with latency metrics displayed
```

### Flow 3: Preview / Testing Mode

```
1. Select a voice model
2. Click "Live Preview"
3. Speak into microphone
4. Hear transformed voice through speakers in real-time
5. View side-by-side waveform comparison
6. Adjust settings until satisfied
```

---

## 12. Pricing Model (future/commercial — out of v1 scope)

Plan tiers exist in code only to parameterize WS rate limits (`PLAN_LIMITS`, M6b).
No billing is built or planned for v1.

| Plan           | Price  | Included                                    | Features                                            |
| -------------- | ------ | ------------------------------------------- | --------------------------------------------------- |
| **Free**       | $0/mo  | 5 min/mo transformed calls, 1 Instant Clone | Preview mode, basic UI                              |
| **Pro**        | $29/mo | 300 min/mo, 5 HD Clones, 20 Instant Clones  | All calling features, call recording, priority GPU  |
| **Enterprise** | Custom | Unlimited minutes, unlimited clones         | Dedicated GPU, SLA, API access, custom integrations |

#### Add-ons
- Extra phone number: $3/mo
- Additional training minutes: $0.10/min
- PSTN calling overage: $0.05/min

---

## 13. Milestones & Phased Rollout

> **The canonical milestone tracker is [ROADMAP.md](ROADMAP.md)** (M-numbered milestones,
> current state, next steps). This section is the high-level phase view only; when they
> disagree, ROADMAP wins. Mapping: M4 + M5 complete Phase 1; M6 auth; M7
> deployment/observability; M8a is the in-scope cut of Phase 2; M9/M10 close v1.
> Phases 3–4 are out of v1 scope (owner decision, 2026-07-07).

### Phase 1 — MVP — done (GPU bench pending)
- [x] Core audio pipeline (AudioWorklet → WebSocket → server inference → playback) — M1/M4a
- [x] Real VC model on the streaming ONNX engine — OpenVoice V2 (M5b); RVC moved to M9
- [x] Instant clone (short samples) — Cartesia `/voices/clone` (M4b) **and**
      self-hosted OpenVoice SE-baking clone (M5b), both behind `POST /voices`
- [x] Preview Mode (mic → transform → speaker) — Live Monitor loop (M4a/M4c)
- [x] Basic web UI (FastAPI + Jinja2 + HTMX): Voice Studio + Live Monitor — M4c
- [x] User authentication (Supabase) — REST auth + per-user voices (M6a);
      `/ws/voice` auth + per-user rate limiting (M6b)
- [~] Single-server deployment — compose stack + dev.sh landed (#14); **rented-GPU
      bench run pending** (closes M5; §15 criterion 2)

### Phase 2 — Calling (in-scope cut)
- [x] Twilio PSTN integration (REST + Media Streams, gateway-terminated) — M8a;
      **live-Twilio verification run pending** (§15 criterion 1)
- [x] Dialer UI with call history — M8a (contacts descoped)
- [x] Mid-call model switching — `switch_model` (M4a)
- [x] Latency monitoring dashboard (Prometheus + Grafana) — M7
- [ ] HD Clone training pipeline (RVC fine-tuning) — **M9**
- Descoped to future/commercial (owner decision, 2026-07-07): inbound calls,
  call recording, WebRTC peer calls, contacts

### Remaining v1 work (see ROADMAP)
- [ ] M5 close: rented-GPU `cloud_gpu` bench run (locks the 80ms line)
- [ ] M8a close: live-Twilio call run
- [ ] M9 — HD Clone tier (RVC training + single-graph ONNX export)
- [ ] M10 — UI completion (Dashboard, Settings, fine-tune controls, similarity meter,
      waveform viz) + v1 sign-off against §15

### Out of v1 scope — future/commercial backlog
Inbound calls, call recording, WebRTC peer calls, contacts, model export/sharing
(`.mbv`), A/B testing, multi-region edge deployment, K8s auto-scaling, browser
extension, billing (Stripe), analytics, companion desktop app, PWA, voice marketplace,
multi-language, enterprise API.

---

## 14. Risks & Mitigations

| Risk                       | Impact | Likelihood | Mitigation                                                          |
| -------------------------- | ------ | ---------- | ------------------------------------------------------------------- |
| Latency exceeds 300ms      | High   | Medium     | Edge deployment, model optimization, adaptive quality               |
| GPU costs too high         | High   | Medium     | Model distillation, per-user quotas, hybrid cloud/self-hosted       |
| Audio quality artifacts    | Medium | High       | Overlap-add smoothing, post-processing, user-adjustable quality     |
| WebSocket connection drops | Medium | Medium     | Auto-reconnect with exponential backoff, voice passthrough fallback |
| Browser compatibility      | Medium | Low        | Progressive enhancement, fallback to cloud-rendered audio           |
| Model training failures    | Low    | Medium     | Automated retries, audio quality pre-validation, user guidance      |

---

## 15. Success Criteria (v1 — binding)

Mockingbird is a **portfolio piece** (owner decision, 2026-07-07). v1 is successful when
every criterion below passes. The commercial metrics that previously lived here moved to
Appendix C and are aspirational only — nothing in v1 measures them.

| #   | Criterion                                                                                                                                                                               | How it's checked                                                                                                     | Status (2026-07-07)                                    |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| 1   | **Live cloned call**: clone a voice in the Studio, dial a real phone from `/dialer`, callee hears the cloned voice; two-way audio stays stable; status callback closes the record       | M8a live-Twilio run (real `TWILIO_*` creds + `PUBLIC_BASE_URL` tunnel, ~15 min)                                      | Pending — code landed & offline-tested                 |
| 2   | **GPU latency inside budget**: self-hosted conversion p95 ≤ 80ms/block on a rented A10G/L4 (`DEVICE=cuda`), effective added latency consistent with the ~172ms end-to-end budget (§4.1) | `infrastructure/scripts/provision_cloud_gpu.sh` + `inference/scripts/self_hosted_bench.py`; numbers recorded in §4.1 | Pending — laptop-CPU p95 47.5ms already under the line |
| 3   | **Instant clone quality**: converted output is recognizably the target voice in a side-by-side listening check                                                                          | Owner listening check (no automated PESQ harness in v1)                                                              | Pending — exercised locally in M5b                     |
| 4   | **HD Clone tier**: an RVC fine-tuned voice trains, exports to the M5a ONNX contract, streams end-to-end, and beats the instant clone of the same speaker in a listening check           | M9                                                                                                                   | Not started                                            |
| 5   | **UI complete**: Dashboard, Settings, fine-tune controls, similarity meter, waveform visualizer present and working                                                                     | M10                                                                                                                  | Not started                                            |
| 6   | **Engineering health**: CI green on `main` (format + lint + tests × 3 services + image builds)                                                                                          | GitHub Actions (M7)                                                                                                  | Passing                                                |
| 7   | **Docs current**: README / spec / roadmap match the code; architecture + measured numbers presentable as a portfolio artifact                                                           | Review at v1 sign-off (M10)                                                                                          | Ongoing                                                |

Order of remaining work: M5 GPU bench → M8a live run → M9 → M10 (sign-off).

---

## Appendix A: Competitive Landscape

| Product         | Type         | Real-time?   | Voice Cloning?   | Web-based? | Phone Calls? |
| --------------- | ------------ | ------------ | ---------------- | ---------- | ------------ |
| **Voicemod**    | Desktop app  | ✅            | ❌ Effects only   | ❌          | ❌            |
| **MorphVOX**    | Desktop app  | ✅            | ❌ Presets        | ❌          | ❌            |
| **ElevenLabs**  | Cloud API    | ⚠️ 400ms+    | ✅ Excellent      | ✅ API      | ❌            |
| **Resemble.ai** | Cloud API    | ⚠️ 200-400ms | ✅ Good           | ✅ API      | ❌            |
| **Murf.ai**     | Cloud Studio | ❌ Batch      | ✅ Production     | ✅          | ❌            |
| **Mockingbird** | Web App      | ✅ <300ms     | ✅ User-trainable | ✅          | ✅            |

## Appendix B: Open-Source Model Comparison

| Model            | Latency  | Real-time?   | Min Audio   | Quality | License    |
| ---------------- | -------- | ------------ | ----------- | ------- | ---------- |
| **RVC**          | ~40-80ms | ✅            | 10 min      | ★★★★★   | MIT        |
| **OpenVoice v2** | ~200ms   | ⚠️ Adaptable | 10-30 sec   | ★★★★    | Apache 2.0 |
| **GPT-SoVITS**   | ~200ms   | ✅ Streaming  | 5 sec       | ★★★★★   | MIT        |
| **CosyVoice 2**  | ~150ms   | ✅ Native     | Zero-shot   | ★★★★★   | Apache 2.0 |
| **Fish Speech**  | ~150ms   | ✅            | 10-30 sec   | ★★★★    | Apache 2.0 |
| **Kokoro**       | ~100ms   | ✅ Browser!   | Pre-trained | ★★★★    | Apache 2.0 |
| **So-VITS-SVC**  | ~100ms   | ⚠️ Fork      | 30 min      | ★★★★★   | MIT        |
| **Bark**         | 5-30s    | ❌            | Zero-shot   | ★★★★★   | MIT        |
| **Tortoise-TTS** | 10-60s   | ❌            | 5 min       | ★★★★★   | Apache 2.0 |

## Appendix C: Commercial Metrics (aspirational — not v1 success gates)

The former §15 table, kept as the bar a commercial launch would be judged against.
Nothing in v1 measures these (no analytics, surveys, or automated quality scoring).

| Metric                     | Target  | Measurement                                                         |
| -------------------------- | ------- | ------------------------------------------------------------------- |
| P95 voice latency          | < 300ms | Server-side instrumentation *(the one row v1 does verify — §15 #2)* |
| Voice similarity score     | > 0.85  | Automated PESQ/POLQA comparison                                     |
| User retention (Day 7)     | > 40%   | Analytics                                                           |
| Call completion rate       | > 95%   | Twilio + app metrics                                                |
| NPS Score                  | > 50    | User surveys                                                        |
| Instant Clone success rate | > 90%   | Model quality validation                                            |
