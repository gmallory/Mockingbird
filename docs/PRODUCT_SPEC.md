# Mockingbird — Product Specification

> **Version:** 1.0.0  
> **Last Updated:** 2026-05-30  
> **Status:** Draft  

---

## 1. Executive Summary

**Mockingbird** is a web-based, real-time voice cloning application that captures a user's speech, transforms it to sound like a different person, and outputs the transformed audio — all with sub-300ms latency. The primary use case is live voice-over during phone calls, enabling the user to speak in a cloned voice in real time.

Users upload a short audio sample of a target voice, train (or instantly clone) a voice model, and then use Mockingbird to transform their live speech into that voice during calls made through the app's built-in VoIP system.

---

## 2. Problem Statement

Current voice transformation tools are either:
- **Desktop-only** and require installing system-level virtual audio drivers (Voicemod, MorphVOX)
- **Offline/batch-only** with no real-time capability (Murf.ai, Bark, Tortoise-TTS)
- **Low-quality** pitch-shifting effects that don't produce convincing voice changes

There is no web-based product that provides **real-time, high-fidelity voice cloning** with integrated calling. Mockingbird fills this gap.

---

## 3. Target Users

| Persona | Description |
|---------|-------------|
| **Content Creators** | YouTubers, podcasters, streamers who want to create character voices |
| **Voice Actors** | Professionals who need to quickly audition different voice styles |
| **Privacy-Conscious Callers** | Users who want to mask their real voice during calls |
| **Entertainment Users** | People who want to prank friends or have fun with voice transformation |

---

## 4. Core Features

### 4.1 Real-Time Voice Cloning Engine

The heart of Mockingbird — transforms live speech into a target voice with <300ms latency.

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
│  │ • Viz      │   │ • Ring Buffer  │   │ • Opus codec   │   │
│  │ • Settings │   │ • VAD          │   │ • Reconnect    │   │
│  └────────────┘   └────────────────┘   └───────┬────────┘   │
│                          ▲                      │            │
│                          │ SharedArrayBuffer     │ wss://     │
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
6. **Playback** — `AudioWorkletProcessor` reads from `SharedArrayBuffer` ring buffer and outputs to destination

#### Latency Budget

| Stage | Target | Notes |
|-------|--------|-------|
| Audio Capture + Buffering | 10ms | 128-sample AudioWorklet render quanta |
| Encoding (PCM → Int16) | 2ms | Lightweight conversion |
| Network Upload (edge) | 20ms | Edge-deployed server, <40ms RTT |
| Server Preprocessing | 5ms | VAD check, normalization |
| **Model Inference (GPU)** | **80ms** | Optimized RVC on ONNX Runtime + TensorRT |
| Server Postprocessing | 5ms | Volume normalization, artifact smoothing |
| Network Download | 20ms | Same edge path |
| Decode + Playback Buffer | 30ms | Jitter buffer for smooth output |
| **Total** | **~172ms** | ✅ Well under 300ms target |

---

### 4.2 Voice Model Training & Management

#### Two-Tier Cloning System

| Mode | Audio Required | Ready In | Quality | Use Case |
|------|---------------|----------|---------|----------|
| **Instant Clone** (OpenVoice / GPT-SoVITS) | 10–30 seconds | Immediate | ★★★★ Good | Quick setup, previewing |
| **HD Clone** (RVC fine-tune) | 10–30 minutes | 30 min–2 hrs | ★★★★★ Excellent | Production calls |

#### Training Pipeline

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
- **Voice Library** — Browse, search, and manage saved voice models
- **Model Sharing** — Export/import voice models (encrypted `.mbv` format)
- **A/B Testing** — Compare Instant vs HD clone side by side
- **Fine-Tune Controls** — Adjust pitch offset, breathiness, speed after training
- **Training Progress** — Real-time progress bar with estimated time remaining

---

### 4.3 Built-in VoIP Calling (Primary Audio Routing)

Since a web browser cannot install system-level virtual audio drivers, Mockingbird integrates its own calling system.

#### Architecture

```
User's Browser (Mockingbird)
    │
    ├── WebRTC Audio Track (transformed voice)
    │
    ▼
Twilio / Vonage / SignalWire (WebRTC-to-PSTN Gateway)
    │
    ▼
Recipient's Phone (regular phone call)
```

#### Calling Features
- **Outbound PSTN calls** — Dial any phone number; voice is transformed before reaching the recipient
- **Inbound calls** — Dedicated Mockingbird phone number (via Twilio) that users can receive calls on
- **WebRTC peer calls** — Browser-to-browser calls within the app (free, lowest latency)
- **Call controls** — Mute, hold, volume, voice on/off toggle, model hot-swap mid-call
- **Call recording** — Save both original and transformed audio (with consent)
- **Dial pad & contacts** — Full phone interface within the web app

#### PSTN Gateway Integration (Twilio)
- Cost: ~$0.013/min outbound, ~$0.0085/min inbound (US numbers)
- Phone number provisioning: $1.15/mo per number
- WebRTC SDK: `twilio-client.js` for browser integration

---

### 4.4 Audio Routing Modes

Mockingbird supports multiple audio routing strategies for different use cases:

| Mode | How It Works | Best For | Latency |
|------|-------------|----------|---------|
| **Built-in VoIP** (Primary) | Calls made through Mockingbird's integrated dialer | Phone calls | ~170ms |
| **Browser Extension** (Future) | Chrome extension intercepts WebRTC in Google Meet, Zoom Web, Discord | Video conferencing | ~150ms |
| **Companion App** (Future) | Electron/Tauri desktop app with virtual audio driver | Any desktop app | ~120ms |
| **Preview Mode** | Real-time mic → transformed output through speakers | Testing & demo | ~170ms |

---

### 4.5 Web Application UI

#### Pages & Views

| Page | Description |
|------|-------------|
| **Dashboard** | Overview: active voice model, recent calls, quick-start actions |
| **Voice Studio** | Upload samples, train models, manage voice library, A/B preview |
| **Dialer** | Phone interface — dial pad, contacts, recent calls, mid-call controls |
| **Live Monitor** | Real-time waveform visualization, latency metrics, voice comparison |
| **Settings** | Audio I/O config, quality presets, account, billing |

#### Key UI Components
- **Waveform Visualizer** — Real-time input vs. output waveform comparison
- **Latency Indicator** — Live latency measurement (green/yellow/red)
- **Voice Similarity Meter** — How closely the output matches the target voice
- **Quick Switch** — One-click voice model switching during calls
- **Transformation Toggle** — Instant on/off for voice transformation

---

## 5. Technology Stack

### Frontend
| Technology | Purpose |
|-----------|---------|
| **FastAPI + Jinja2 + HTMX** (Python) | Server-rendered UI, routing, page handlers |
| **Uvicorn** | ASGI server |
| **Web Audio API** | AudioWorklet for real-time audio processing (minimal browser JS glue) |
| **SharedArrayBuffer** | Zero-copy audio data sharing between threads |
| **WebSocket (binary)** | Streaming audio to/from inference server |
| **Twilio Python SDK + Voice JS SDK** | WebRTC-to-PSTN calling integration |
| **Canvas API** | Waveform and spectrogram visualizations |
| **Redis (server-side session)** | UI state management |

### Backend — Gateway
| Technology | Purpose |
|-----------|---------|
| **Python 3.14** (FastAPI) | WebSocket connection management, auth, routing |
| **Redis** | Session management, model routing, pub/sub |
| **PostgreSQL** | User accounts, voice models metadata, call history |
| **S3 / GCS** | Audio sample storage, model weight storage |

### Backend — ML Inference
| Technology | Purpose |
|-----------|---------|
| **Python 3.14** (FastAPI + Uvicorn) | ML service orchestration, WebSocket audio streaming |
| **RVC** (Retrieval-based Voice Conversion) | Core real-time voice conversion engine |
| **OpenVoice v2** | Zero-shot instant voice cloning |
| **GPT-SoVITS** | Alternative zero-shot cloning (5-second samples) |
| **ONNX Runtime** (GPU) | Optimized model inference |
| **TensorRT** | NVIDIA GPU acceleration for production |
| **Silero VAD** | Voice activity detection |
| **FAISS** | Feature retrieval for RVC |
| **Celery + Redis** | Async training job queue |

### Infrastructure
| Technology | Purpose |
|-----------|---------|
| **Docker + Kubernetes** | Container orchestration |
| **NVIDIA GPU nodes** (A10G / L4) | Inference compute |
| **CloudFlare / Fly.io Edge** | Edge deployment for low latency |
| **Twilio** | PSTN gateway, phone numbers |
| **Prometheus + Grafana** | Latency monitoring, system metrics |
| **Sentry** | Error tracking |

---

## 6. Data Models

### Voice Model

```python
# Pydantic/SQLModel — see gateway/app/db/models.py
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

### Call Record

```python
class CallRecord(BaseModel):
    id: UUID
    user_id: UUID
    voice_model_id: UUID

    # Call details
    direction: Literal["inbound", "outbound", "p2p"]
    phone_number: str | None = None                     # PSTN calls
    peer_id: str | None = None                          # P2P WebRTC calls

    # Timing
    started_at: datetime
    ended_at: datetime | None = None
    duration_sec: float

    # Quality metrics
    avg_latency_ms: float
    p95_latency_ms: float
    dropout_count: int                                  # Audio dropout events

    # Recording
    original_audio_path: str | None = None              # S3 path
    transformed_audio_path: str | None = None           # S3 path

    status: Literal["active", "completed", "failed"]
```

### User

```python
class User(BaseModel):
    id: UUID
    email: str
    display_name: str

    # Subscription
    plan: Literal["free", "pro", "enterprise"]
    monthly_minutes_used: float
    monthly_minutes_limit: float

    # Settings
    preferred_sample_rate: Literal[16000, 44100, 48000]
    preferred_buffer_size: Literal[128, 256, 512]
    noise_suppression_enabled: bool
    echo_cancellation_enabled: bool

    # Phone
    twilio_phone_number: str | None = None

    created_at: datetime
```

---

## 7. API Design

### WebSocket — Audio Streaming

```
Client → Server (binary): Raw PCM Int16 audio frames (20ms chunks, 960 samples at 48kHz)
Server → Client (binary): Transformed PCM Int16 audio frames

Client → Server (JSON control messages):
  { "type": "start", "modelId": "uuid", "sampleRate": 48000 }
  { "type": "switch_model", "modelId": "uuid" }
  { "type": "stop" }
  { "type": "ping" }

Server → Client (JSON control messages):
  { "type": "ready", "latencyMs": 172 }
  { "type": "model_loaded", "modelId": "uuid" }
  { "type": "error", "message": "..." }
  { "type": "metrics", "latencyMs": 165, "similarity": 0.92 }
  { "type": "pong" }
```

### REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/voices` | Create new voice model (upload audio) |
| GET | `/api/voices` | List user's voice models |
| GET | `/api/voices/:id` | Get voice model details |
| DELETE | `/api/voices/:id` | Delete voice model |
| PATCH | `/api/voices/:id` | Update model settings (pitch, speed) |
| POST | `/api/voices/:id/train` | Trigger HD training |
| GET | `/api/voices/:id/train/status` | Check training progress |
| POST | `/api/voices/:id/preview` | Generate preview audio clip |
| POST | `/api/calls/outbound` | Initiate outbound PSTN call |
| GET | `/api/calls` | List call history |
| GET | `/api/calls/:id` | Get call details + metrics |
| GET | `/api/user/usage` | Get usage statistics |

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

### Server-Side (FastAPI)

```python
@app.websocket("/ws/voice")
async def voice_stream(websocket: WebSocket):
    await websocket.accept()
    
    # Load user's voice model
    config = await websocket.receive_json()
    model = model_manager.get_model(config["modelId"])
    
    while True:
        # Receive 20ms audio chunk (binary)
        raw_audio = await websocket.receive_bytes()
        audio_np = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32) / 32768.0
        
        # Voice conversion inference (~80ms on GPU)
        transformed = await model.convert(audio_np)
        
        # Send back transformed audio (binary)
        output_bytes = (transformed * 32768).astype(np.int16).tobytes()
        await websocket.send_bytes(output_bytes)
```

---

## 9. Voice Cloning Models — Detailed Comparison

### Primary Engine: RVC (Retrieval-based Voice Conversion)

**Why RVC:**
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

### Secondary Engine: OpenVoice v2 (Zero-Shot Cloning)

**Why OpenVoice for Instant Clone:**
- Requires only 10–30 seconds of reference audio (no training!)
- Fast inference (12x–40x real-time on GPU)
- Decoupled architecture: tone color is separated from style/language
- MIT / Apache 2.0 license

### Tertiary Engine: GPT-SoVITS (Zero-Shot Alternative)

**Why GPT-SoVITS as fallback:**
- Needs only ~5 seconds of audio for usable clone
- Very high quality output
- MIT licensed
- Good for languages beyond English

### Cloud API Fallback: Cartesia Sonic-3

For users who need the absolute lowest latency or when self-hosted GPU capacity is exhausted:
- **40–90ms TTFB** — fastest commercial option
- WebSocket streaming API
- Voice cloning from 3–10 second samples
- Usage-based pricing

---

## 10. Non-Functional Requirements

### Performance
| Metric | Target |
|--------|--------|
| End-to-end voice latency | < 300ms (P95) |
| Model inference time | < 100ms per 20ms chunk |
| Audio dropout rate | < 0.1% of chunks |
| Concurrent users per GPU | 8–12 (A10G), 15–20 (A100) |
| Time to first audio output | < 500ms after call connect |

### Scalability
| Metric | Target |
|--------|--------|
| Concurrent active calls | 10,000+ (with auto-scaling GPU cluster) |
| Voice model storage | Unlimited (S3/GCS) |
| Training job throughput | 50+ concurrent jobs |

### Reliability
| Metric | Target |
|--------|--------|
| Service uptime | 99.9% |
| WebSocket reconnect time | < 2 seconds |
| Graceful degradation | Passthrough (unmodified voice) on failure |

### Security
| Requirement | Implementation |
|-------------|---------------|
| Audio encryption in transit | TLS 1.3 (wss://) |
| Audio encryption at rest | AES-256 for stored recordings |
| Voice model isolation | Per-user model storage, no cross-user access |
| Authentication | JWT + refresh tokens |
| Rate limiting | Per-user WebSocket connections and API calls |

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

## 12. Pricing Model (Proposed)

| Plan | Price | Included | Features |
|------|-------|----------|----------|
| **Free** | $0/mo | 5 min/mo transformed calls, 1 Instant Clone | Preview mode, basic UI |
| **Pro** | $29/mo | 300 min/mo, 5 HD Clones, 20 Instant Clones | All calling features, call recording, priority GPU |
| **Enterprise** | Custom | Unlimited minutes, unlimited clones | Dedicated GPU, SLA, API access, custom integrations |

#### Add-ons
- Extra phone number: $3/mo
- Additional training minutes: $0.10/min
- PSTN calling overage: $0.05/min

---

## 13. Milestones & Phased Rollout

### Phase 1 — MVP (Weeks 1–8)
- [ ] Core audio pipeline (AudioWorklet → WebSocket → server inference → playback)
- [ ] RVC model integration with ONNX optimization
- [ ] Instant Clone via OpenVoice (10-30 second samples)
- [ ] Preview Mode (mic → transform → speaker)
- [ ] Basic web UI (FastAPI + Jinja2 + HTMX): Voice Studio + Live Monitor
- [ ] User authentication (Supabase)
- [ ] Single-server deployment (1 GPU node)

### Phase 2 — Calling (Weeks 9–14)
- [ ] Twilio WebRTC-to-PSTN integration
- [ ] Dialer UI with contacts and call history
- [ ] HD Clone training pipeline (RVC fine-tuning)
- [ ] Mid-call model switching
- [ ] Call recording (original + transformed)
- [ ] Latency monitoring dashboard (Prometheus + Grafana)

### Phase 3 — Scale & Polish (Weeks 15–20)
- [ ] Multi-region edge deployment (Fly.io / CloudFlare)
- [ ] Auto-scaling GPU cluster (Kubernetes)
- [ ] Browser extension for Google Meet / Zoom Web
- [ ] Advanced voice controls (breathiness, formant, emotion)
- [ ] Billing & subscription system (Stripe)
- [ ] Usage analytics and reporting

### Phase 4 — Expansion (Weeks 21+)
- [ ] Companion desktop app (Electron/Tauri) with virtual audio driver
- [ ] Mobile-responsive PWA
- [ ] Voice marketplace (share/sell voice models)
- [ ] Multi-language support
- [ ] Enterprise API with webhooks

---

## 14. Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|-----------|------------|
| Latency exceeds 300ms | High | Medium | Edge deployment, model optimization, adaptive quality |
| GPU costs too high | High | Medium | Model distillation, per-user quotas, hybrid cloud/self-hosted |
| Audio quality artifacts | Medium | High | Overlap-add smoothing, post-processing, user-adjustable quality |
| WebSocket connection drops | Medium | Medium | Auto-reconnect with exponential backoff, voice passthrough fallback |
| Browser compatibility | Medium | Low | Progressive enhancement, fallback to cloud-rendered audio |
| Model training failures | Low | Medium | Automated retries, audio quality pre-validation, user guidance |

---

## 15. Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| P95 voice latency | < 300ms | Server-side instrumentation |
| Voice similarity score | > 0.85 | Automated PESQ/POLQA comparison |
| User retention (Day 7) | > 40% | Analytics |
| Call completion rate | > 95% | Twilio + app metrics |
| NPS Score | > 50 | User surveys |
| Instant Clone success rate | > 90% | Model quality validation |

---

## Appendix A: Competitive Landscape

| Product | Type | Real-time? | Voice Cloning? | Web-based? | Phone Calls? |
|---------|------|-----------|---------------|-----------|-------------|
| **Voicemod** | Desktop app | ✅ | ❌ Effects only | ❌ | ❌ |
| **MorphVOX** | Desktop app | ✅ | ❌ Presets | ❌ | ❌ |
| **ElevenLabs** | Cloud API | ⚠️ 400ms+ | ✅ Excellent | ✅ API | ❌ |
| **Resemble.ai** | Cloud API | ⚠️ 200-400ms | ✅ Good | ✅ API | ❌ |
| **Murf.ai** | Cloud Studio | ❌ Batch | ✅ Production | ✅ | ❌ |
| **Mockingbird** | Web App | ✅ <300ms | ✅ User-trainable | ✅ | ✅ |

## Appendix B: Open-Source Model Comparison

| Model | Latency | Real-time? | Min Audio | Quality | License |
|-------|---------|-----------|-----------|---------|---------|
| **RVC** | ~40-80ms | ✅ | 10 min | ★★★★★ | MIT |
| **OpenVoice v2** | ~200ms | ⚠️ Adaptable | 10-30 sec | ★★★★ | Apache 2.0 |
| **GPT-SoVITS** | ~200ms | ✅ Streaming | 5 sec | ★★★★★ | MIT |
| **CosyVoice 2** | ~150ms | ✅ Native | Zero-shot | ★★★★★ | Apache 2.0 |
| **Fish Speech** | ~150ms | ✅ | 10-30 sec | ★★★★ | Apache 2.0 |
| **Kokoro** | ~100ms | ✅ Browser! | Pre-trained | ★★★★ | Apache 2.0 |
| **So-VITS-SVC** | ~100ms | ⚠️ Fork | 30 min | ★★★★★ | MIT |
| **Bark** | 5-30s | ❌ | Zero-shot | ★★★★★ | MIT |
| **Tortoise-TTS** | 10-60s | ❌ | 5 min | ★★★★★ | Apache 2.0 |
