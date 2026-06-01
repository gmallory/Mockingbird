# 🐦 Mockingbird

> **Real-time AI voice cloning for live conversations.**

Mockingbird is a web application that captures your speech, transforms it into a different voice in real-time (<300ms latency), and outputs the cloned voice — enabling you to sound like someone else during phone calls, video conferences, and voice chats.

---

## ✨ Features

### 🎤 Real-Time Voice Cloning
- **Sub-300ms latency** — Transform your voice in real-time during live conversations
- **Hybrid inference** — On-device preprocessing + edge GPU server inference
- **Streaming architecture** — AudioWorklet → WebSocket → GPU → WebSocket → AudioWorklet

### 🧬 User-Trainable Voice Models
- **Instant Clone** — Upload 10–30 seconds of audio, get a working voice clone immediately (OpenVoice / GPT-SoVITS)
- **HD Clone** — Upload 10–30 minutes of audio for studio-quality voice cloning (RVC fine-tuning)
- **Voice Library** — Save, manage, and hot-swap between multiple voice models

### 📞 Built-in VoIP Calling
- **PSTN calls** — Dial any phone number with your transformed voice via Twilio WebRTC-to-PSTN gateway
- **Browser-to-browser** — Free WebRTC calls within the app
- **Mid-call controls** — Switch voices, toggle transformation, adjust pitch/speed during live calls

### 🎛️ Audio Controls
- Pitch offset (-12 to +12 semitones)
- Speed factor (0.5x to 2.0x)
- Breathiness control
- Noise suppression & echo cancellation
- Real-time waveform visualization

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────┐
│              BROWSER (Client)                     │
│                                                   │
│  Next.js UI ◄──► Audio Engine ◄──► WS Worker     │
│                  (AudioWorklet)     (Binary PCM)  │
│                  + Ring Buffer      + Opus        │
│                  + VAD              + Reconnect   │
└──────────────────────┬───────────────────────────┘
                       │ wss:// (binary frames)
┌──────────────────────▼───────────────────────────┐
│              EDGE GPU SERVER                      │
│                                                   │
│  Node.js Gateway ──gRPC──► FastAPI ML Service    │
│  (WebSocket mgmt,          (RVC / OpenVoice,     │
│   auth, routing)            ONNX Runtime GPU,    │
│                             Silero VAD,          │
│                             streaming inference) │
└──────────────────────────────────────────────────┘
```

### Latency Budget (~172ms total)

| Stage | Time |
|-------|------|
| Audio capture + buffering | 10ms |
| Encoding (PCM → Int16) | 2ms |
| Network upload (edge) | 20ms |
| Server preprocessing | 5ms |
| **Model inference (GPU)** | **80ms** |
| Server postprocessing | 5ms |
| Network download | 20ms |
| Decode + playback buffer | 30ms |

---

## 🛠️ Tech Stack

### Frontend
- **Next.js 15** (React 19) — Application framework
- **TypeScript** — Type safety
- **Web Audio API** — AudioWorklet for real-time processing
- **Twilio Client SDK** — WebRTC-to-PSTN calling
- **Zustand** — State management
- **D3.js** — Audio visualizations

### Backend — Gateway
- **Node.js** (Fastify) — WebSocket management, auth, routing
- **Redis** — Sessions, pub/sub, model routing
- **PostgreSQL** — Users, voice models, call history
- **S3 / GCS** — Audio samples, model weights

### Backend — ML Inference
- **Python 3.12** (FastAPI) — ML service orchestration
- **RVC** — Core real-time voice conversion (MIT license)
- **OpenVoice v2** — Zero-shot instant cloning (Apache 2.0)
- **GPT-SoVITS** — Alternative zero-shot cloning (MIT)
- **ONNX Runtime GPU** — Optimized inference
- **TensorRT** — NVIDIA GPU acceleration
- **Silero VAD** — Voice activity detection
- **Celery + Redis** — Async training jobs

### Infrastructure
- **Docker + Kubernetes** — Orchestration
- **NVIDIA GPU** (A10G / L4) — Inference compute
- **Fly.io / CloudFlare** — Edge deployment
- **Twilio** — PSTN gateway
- **Prometheus + Grafana** — Monitoring

---

## 📁 Project Structure

```
Mockingbird/
├── docs/
│   └── PRODUCT_SPEC.md          # Full product specification
│
├── frontend/                     # Next.js web application
│   ├── src/
│   │   ├── app/                  # Next.js app router pages
│   │   │   ├── page.tsx          # Landing / Dashboard
│   │   │   ├── studio/           # Voice Studio (train & manage)
│   │   │   ├── dialer/           # Phone dialer & calling
│   │   │   ├── monitor/          # Live audio monitor
│   │   │   └── settings/         # User settings
│   │   ├── components/           # React components
│   │   │   ├── AudioVisualizer/
│   │   │   ├── Dialer/
│   │   │   ├── VoiceCard/
│   │   │   └── WaveformDisplay/
│   │   ├── lib/
│   │   │   ├── audio-engine/     # Core audio processing (vanilla TS)
│   │   │   │   ├── AudioEngine.ts
│   │   │   │   ├── processors/
│   │   │   │   │   ├── voice-capture.worklet.ts
│   │   │   │   │   └── voice-playback.worklet.ts
│   │   │   │   ├── WebSocketWorker.ts
│   │   │   │   └── RingBuffer.ts
│   │   │   ├── twilio/           # Twilio calling integration
│   │   │   └── api/              # API client
│   │   ├── hooks/                # React hooks
│   │   └── stores/               # Zustand stores
│   ├── public/
│   ├── package.json
│   └── next.config.js
│
├── gateway/                      # Node.js WebSocket gateway
│   ├── src/
│   │   ├── server.ts             # Fastify server
│   │   ├── websocket/            # WS connection management
│   │   ├── auth/                 # JWT authentication
│   │   ├── routing/              # Model routing & load balancing
│   │   └── middleware/           # Rate limiting, logging
│   ├── package.json
│   └── Dockerfile
│
├── inference/                    # Python ML inference service
│   ├── app/
│   │   ├── main.py               # FastAPI application
│   │   ├── models/
│   │   │   ├── rvc/              # RVC voice conversion
│   │   │   ├── openvoice/        # OpenVoice zero-shot cloning
│   │   │   └── gpt_sovits/       # GPT-SoVITS cloning
│   │   ├── audio/
│   │   │   ├── processor.py      # Audio preprocessing
│   │   │   ├── vad.py            # Voice activity detection
│   │   │   └── codec.py          # Encoding/decoding
│   │   ├── training/
│   │   │   ├── pipeline.py       # Training orchestration
│   │   │   ├── dataset.py        # Audio dataset preparation
│   │   │   └── export.py         # ONNX model export
│   │   └── api/
│   │       ├── voice_stream.py   # WebSocket streaming endpoint
│   │       ├── voices.py         # Voice model CRUD
│   │       └── training.py       # Training job management
│   ├── requirements.txt
│   ├── Dockerfile.gpu
│   └── pyproject.toml
│
├── infrastructure/               # Deployment & infrastructure
│   ├── docker-compose.yml        # Local development
│   ├── docker-compose.gpu.yml    # GPU development
│   ├── k8s/                      # Kubernetes manifests
│   │   ├── gateway/
│   │   ├── inference/
│   │   └── monitoring/
│   └── terraform/                # Cloud infrastructure
│
├── agents/                       # Agentic AI configuration
│   ├── AGENTS.md                 # Master agent instructions
│   ├── frontend.agent.md         # Frontend development agent
│   ├── gateway.agent.md          # Gateway service agent
│   ├── inference.agent.md        # ML inference service agent
│   ├── audio-engine.agent.md     # Audio engine agent
│   └── infrastructure.agent.md   # DevOps/infra agent
│
├── .env.example                  # Environment variables template
├── .gitignore
├── LICENSE
└── README.md
```

---

## 🚀 Getting Started

### Prerequisites
- Node.js 20+
- Python 3.12+
- Docker & Docker Compose
- NVIDIA GPU with CUDA 12+ (for local inference)
- Twilio account (for PSTN calling)

### Quick Start (Development)

```bash
# 1. Clone the repository
git clone https://github.com/yourusername/Mockingbird.git
cd Mockingbird

# 2. Copy environment variables
cp .env.example .env
# Edit .env with your API keys (Twilio, Supabase, etc.)

# 3. Start infrastructure (Redis, PostgreSQL, GPU inference)
docker compose -f docker-compose.gpu.yml up -d

# 4. Start the frontend
cd frontend
npm install
npm run dev

# 5. Start the gateway
cd ../gateway
npm install
npm run dev

# 6. Start the inference service (requires GPU)
cd ../inference
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8001 --workers 1
```

### Quick Start (CPU-only / Cloud API Fallback)

```bash
# For development without a local GPU, use Cartesia API as the inference backend
docker compose up -d  # Starts Redis + PostgreSQL only
INFERENCE_BACKEND=cartesia npm run dev  # Frontend with cloud API fallback
```

---

## 📖 Documentation

- [Product Specification](docs/PRODUCT_SPEC.md) — Full product spec with architecture, data models, and API design
- [Agent Instructions](agents/AGENTS.md) — How to use AI agents to build the project

---

## 🤖 AI-Assisted Development

This project includes agent configuration files in the `agents/` directory that enable agentic AI systems to build, extend, and debug the entire codebase. See [agents/AGENTS.md](agents/AGENTS.md) for details.

### Agent Roles
| Agent | Scope | File |
|-------|-------|------|
| **Orchestrator** | Full project coordination | `AGENTS.md` |
| **Frontend** | Next.js UI, components, pages | `frontend.agent.md` |
| **Audio Engine** | Web Audio API, AudioWorklet, WebSocket streaming | `audio-engine.agent.md` |
| **Gateway** | Node.js WebSocket gateway, auth, routing | `gateway.agent.md` |
| **Inference** | Python ML service, RVC/OpenVoice, training pipeline | `inference.agent.md` |
| **Infrastructure** | Docker, K8s, CI/CD, monitoring | `infrastructure.agent.md` |

---

## 📜 License

MIT License — See [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgments

- [RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) — Core voice conversion engine
- [OpenVoice](https://github.com/myshell-ai/OpenVoice) — Zero-shot voice cloning
- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) — Few-shot voice cloning
- [Silero VAD](https://github.com/snakers4/silero-vad) — Voice activity detection
- [Twilio](https://www.twilio.com/) — WebRTC-to-PSTN calling
