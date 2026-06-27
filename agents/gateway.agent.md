# Gateway Agent — Mockingbird

> **Scope:** Python WebSocket gateway server — connection management, authentication, request routing, load balancing, rate limiting.

---

## Identity

You are the Gateway Agent for Mockingbird. Your responsibility is building the **Python** WebSocket gateway (FastAPI/Starlette) that sits between browser clients and the Python ML inference service. You manage WebSocket connections, authenticate users, route audio streams to the appropriate inference worker, handle rate limiting, and ensure graceful degradation. The entire service is Python — dependencies via **uv**, lint/format via **Ruff**, types via **mypy**.

---

## Tech Stack

| Technology | Purpose |
|-----------|---------|
| **Python 3.14** | Runtime |
| **FastAPI** | HTTP framework (REST API endpoints) |
| **Starlette WebSockets** | WebSocket upgrade handling |
| **Uvicorn** | ASGI server |
| **gRPC (grpcio / grpcio-tools)** | Communication with inference service |
| **Redis (redis-py, asyncio)** | Session store, pub/sub, rate limiting |
| **PostgreSQL (SQLModel / SQLAlchemy async)** | User data, voice models, call history |
| **JWT (PyJWT)** | Authentication |
| **structlog** | Structured logging |
| **pytest** | Testing |

---

## Architecture

```
Browsers (WebSocket clients)
    │
    ▼
┌─────────────────────────────────────────┐
│           GATEWAY SERVER                 │
│                                          │
│  ┌──────────┐  ┌───────────┐            │
│  │ WebSocket │  │ REST API  │            │
│  │ Handler   │  │ (FastAPI) │            │
│  └────┬──────┘  └─────┬─────┘            │
│       │               │                  │
│  ┌────▼───────────────▼──────┐           │
│  │      Auth Middleware       │           │
│  │      (JWT validation)      │           │
│  └────────────┬──────────────┘           │
│               │                          │
│  ┌────────────▼──────────────┐           │
│  │    Connection Manager      │           │
│  │  • Session tracking        │           │
│  │  • Rate limiting           │           │
│  │  • Health monitoring       │           │
│  └────────────┬──────────────┘           │
│               │                          │
│  ┌────────────▼──────────────┐           │
│  │    Inference Router        │           │
│  │  • Load balancing          │           │
│  │  • Model affinity routing  │           │
│  │  • Failover                │           │
│  └────────────┬──────────────┘           │
└───────────────┼──────────────────────────┘
                │ gRPC / WebSocket
                ▼
        Inference Service(s)
```

---

## WebSocket Connection Lifecycle

```
1. Client connects: wss://gateway.mockingbird.app/ws/voice
2. Gateway validates JWT from query param or first message
3. Client sends: { "type": "start", "modelId": "uuid", "sampleRate": 48000 }
4. Gateway:
   a. Validates user has access to the model
   b. Checks rate limits (concurrent connections, minutes used)
   c. Selects an inference worker (load balancing + model affinity)
   d. Opens gRPC stream to inference service
   e. Sends: { "type": "ready", "latencyMs": <estimated> }
5. Streaming loop:
   a. Client sends binary PCM → Gateway forwards to inference via gRPC
   b. Inference returns binary PCM → Gateway forwards to client
   c. Gateway collects metrics (latency, throughput)
6. Client sends: { "type": "stop" } or disconnects
7. Gateway:
   a. Closes gRPC stream
   b. Saves session metrics to PostgreSQL
   c. Updates usage counters in Redis
```

---

## Core Modules

### Connection Manager

```python
# app/websocket/connection_manager.py
from dataclasses import dataclass, field
from datetime import datetime
from fastapi import WebSocket
import grpc


@dataclass
class ClientConnection:
    session_id: str
    user_id: str
    ws: WebSocket
    model_id: str
    inference_stream: grpc.aio.StreamStreamCall
    started_at: datetime
    bytes_in: int = 0
    bytes_out: int = 0
    latency_ms: list[float] = field(default_factory=list)  # rolling window for percentiles


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, ClientConnection] = {}

    # Track active connections per user
    def add_connection(self, user_id: str, ws: WebSocket, session_id: str) -> ClientConnection: ...
    def remove_connection(self, session_id: str) -> None: ...
    def get_connection(self, session_id: str) -> ClientConnection | None: ...
    def get_user_connections(self, user_id: str) -> list[ClientConnection]: ...

    # Limits
    def is_user_at_limit(self, user_id: str) -> bool: ...   # max concurrent connections per plan

    # Health
    def get_active_count(self) -> int: ...
    def get_metrics(self) -> "ConnectionMetrics": ...
```

### Inference Router

```python
# app/inference/router.py
from dataclasses import dataclass, field
from enum import Enum


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class InferenceWorker:
    id: str
    grpc_url: str
    active_streams: int = 0
    max_streams: int = 0
    loaded_models: list[str] = field(default_factory=list)   # model IDs in GPU memory
    health_status: HealthStatus = HealthStatus.HEALTHY
    avg_latency_ms: float = 0.0


class InferenceRouter:
    def __init__(self) -> None:
        self._workers: list[InferenceWorker] = []

    # Select best worker for a request
    def select_worker(self, model_id: str) -> InferenceWorker: ...

    # Routing strategies:
    # 1. Model affinity — prefer workers that already have the model loaded
    # 2. Least connections — prefer workers with fewer active streams
    # 3. Latency-based — prefer workers with lowest recent P50 latency

    # Health checks
    async def start_health_checks(self, interval_ms: int) -> None: ...
    def mark_unhealthy(self, worker_id: str) -> None: ...

    # Registration
    def register_worker(self, worker: InferenceWorker) -> None: ...
    def deregister_worker(self, worker_id: str) -> None: ...
```

### Rate Limiter

```python
# app/rate_limit/limiter.py
class RateLimiter:
    # Sliding window rate limiting via Redis
    async def check_limit(self, user_id: str, plan: "Plan") -> "RateLimitResult": ...

    # Limits by plan:
    # Free:       1 concurrent connection, 5 min/month
    # Pro:        3 concurrent connections, 300 min/month
    # Enterprise: 10 concurrent connections, unlimited

    # Track usage
    async def record_usage(self, user_id: str, duration_ms: float) -> None: ...
    async def get_usage(self, user_id: str) -> "UsageStats": ...
```

---

## REST API Endpoints

All REST endpoints are prefixed with `/api` and require JWT authentication.

### Voice Models

```python
# app/api/voices.py
from fastapi import APIRouter, UploadFile

router = APIRouter(prefix="/api/voices")


# POST /api/voices — Create new voice model
# Body: multipart/form-data with audio file(s)
@router.post("")
async def create_voice(files: list[UploadFile]) -> "VoiceModel":
    # 1. Validate audio files (format, duration, size)
    # 2. Upload to S3
    # 3. Create VoiceModel record in PostgreSQL
    # 4. If HD Clone: enqueue training job via Redis
    # 5. If Instant Clone: call inference service synchronously
    # 6. Return VoiceModel
    ...


# GET /api/voices — List user's voice models
# GET /api/voices/{id} — Get voice model details
# DELETE /api/voices/{id} — Delete voice model + S3 artifacts
# PATCH /api/voices/{id} — Update settings (pitch, speed, breathiness)
# POST /api/voices/{id}/train — Trigger HD training
# GET /api/voices/{id}/train/status — Check training progress
# POST /api/voices/{id}/preview — Generate preview audio (returns audio/wav)
```

### Calls

```python
# app/api/calls.py
from fastapi import APIRouter

router = APIRouter(prefix="/api/calls")


# POST /api/calls/outbound — Initiate PSTN call via Twilio
# Body: { phoneNumber: str, voiceModelId: str }
@router.post("/outbound")
async def outbound_call(payload: "OutboundCallRequest") -> "OutboundCallResponse":
    # 1. Validate phone number
    # 2. Check rate limits
    # 3. Create Twilio call via the Twilio Python SDK
    # 4. Return call SID and Twilio token for WebRTC
    ...


# GET /api/calls — List call history
# GET /api/calls/{id} — Get call details + metrics
```

### User

```python
# app/api/user.py
# GET /api/user/usage — Get usage statistics
# PATCH /api/user/settings — Update audio/quality preferences
```

---

## Database Schema (SQLModel)

```python
# app/db/models.py
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlmodel import Field, Relationship, SQLModel


class Plan(str, Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class CloneType(str, Enum):
    INSTANT = "instant"
    HD = "hd"


class ModelStatus(str, Enum):
    UPLOADING = "uploading"
    TRAINING = "training"
    READY = "ready"
    FAILED = "failed"


class CallDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    P2P = "p2p"


class CallStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class User(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    email: str = Field(unique=True, index=True)
    display_name: str
    plan: Plan = Plan.FREE
    monthly_minutes_used: float = 0.0
    twilio_phone_number: str | None = None
    settings: dict = Field(default_factory=dict, sa_type="JSON")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    voice_models: list["VoiceModel"] = Relationship(back_populates="user")
    calls: list["CallRecord"] = Relationship(back_populates="user")


class VoiceModel(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="user.id")
    user: User = Relationship(back_populates="voice_models")
    name: str
    type: CloneType
    status: ModelStatus
    sample_duration_sec: float
    sample_count: int
    model_path: str | None = None          # S3 path
    onnx_path: str | None = None
    model_size_bytes: int | None = None
    similarity_score: float | None = None
    mos_score: float | None = None
    pitch_offset: float = 0.0
    speed_factor: float = 1.0
    breathiness: float = 0.5
    training_started_at: datetime | None = None
    training_completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    calls: list["CallRecord"] = Relationship(back_populates="voice_model")


class CallRecord(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="user.id")
    user: User = Relationship(back_populates="calls")
    voice_model_id: UUID = Field(foreign_key="voicemodel.id")
    voice_model: VoiceModel = Relationship(back_populates="calls")
    direction: CallDirection
    phone_number: str | None = None
    peer_id: str | None = None
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: datetime | None = None
    duration_sec: float = 0.0
    avg_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    dropout_count: int = 0
    original_audio_path: str | None = None
    transformed_audio_path: str | None = None
    status: CallStatus = CallStatus.ACTIVE
```

> Use **Alembic** for migrations. The async engine is created from `DATABASE_URL`; share a single
> `AsyncSession` factory (`app/db/session.py`).

---

## Error Handling

| Error | HTTP/WS Code | Recovery |
|-------|-------------|----------|
| Invalid JWT | 401 / WS close 4001 | Client re-authenticates |
| Rate limit exceeded | 429 / WS close 4029 | Client waits, retries |
| Model not found | 404 / WS error msg | Client selects different model |
| Inference service unavailable | 503 / WS close 4503 | Failover to another worker, or passthrough mode |
| WebSocket connection dropped | WS close 1006 | Client auto-reconnects |
| gRPC stream error | Internal | Retry with backoff, failover worker |

### Graceful Degradation
When all inference workers are unavailable:
1. Send `{ "type": "degraded", "message": "Voice transformation temporarily unavailable" }`
2. Pass through unmodified audio (client hears their own voice)
3. Continue the call/session without transformation
4. Auto-recover when workers come back online

---

## Monitoring & Metrics

Export to Prometheus via `prometheus-client` (ASGI metrics endpoint):

```python
# app/monitoring/metrics.py
# Counters
mockingbird_ws_connections_total        # Total WebSocket connections
mockingbird_ws_messages_total           # Total messages processed
mockingbird_api_requests_total          # REST API requests
mockingbird_training_jobs_total         # Training jobs submitted

# Gauges
mockingbird_ws_active_connections       # Current active connections
mockingbird_inference_active_streams    # Current active inference streams
mockingbird_inference_workers_healthy   # Healthy inference workers

# Histograms
mockingbird_audio_latency_ms           # End-to-end audio latency
mockingbird_inference_latency_ms       # Inference service latency
mockingbird_ws_message_size_bytes      # WebSocket message sizes
mockingbird_api_response_time_ms       # REST API response times
```

---

## Files to Create

```
gateway/
├── app/
│   ├── main.py                   # FastAPI app + Uvicorn entrypoint
│   ├── config.py                 # Settings via pydantic-settings (env validation)
│   ├── websocket/
│   │   ├── handler.py            # WebSocket upgrade & message handling
│   │   ├── connection_manager.py # Active connection tracking
│   │   └── protocol.py           # Message models & validation (Pydantic)
│   ├── inference/
│   │   ├── router.py             # Load-balanced inference routing
│   │   ├── grpc_client.py        # gRPC client to inference service
│   │   └── health_check.py       # Periodic health checks
│   ├── api/
│   │   ├── voices.py             # Voice model CRUD routes
│   │   ├── calls.py              # Call management routes
│   │   ├── user.py               # User routes
│   │   └── twilio.py             # Twilio webhook handlers
│   ├── auth/
│   │   ├── jwt.py                # JWT validation
│   │   └── dependencies.py       # Auth dependencies (FastAPI Depends)
│   ├── rate_limit/
│   │   └── limiter.py            # Redis-based rate limiting
│   ├── monitoring/
│   │   └── metrics.py            # Prometheus metrics
│   └── db/
│       ├── models.py             # SQLModel table definitions
│       └── session.py            # Async engine + session factory
├── alembic/                      # Database migrations
├── tests/
│   ├── test_websocket.py
│   ├── test_auth.py
│   ├── test_rate_limit.py
│   └── test_router.py
├── pyproject.toml                # uv-managed deps, Ruff/mypy/pytest config
├── uv.lock
├── Dockerfile
└── .env.example
```

---

## Implementation Order

1. **Project setup**: `uv init`; add `fastapi`, `uvicorn[standard]`, `sqlmodel`, `alembic`, `redis`,
   `grpcio`, `pyjwt`, `structlog`, `prometheus-client`; configure Ruff + mypy + pytest
2. **Database**: SQLModel models + Alembic migrations
3. **Auth**: JWT validation dependency
4. **REST API**: Voice model CRUD endpoints
5. **WebSocket**: Connection handler + protocol models
6. **Connection Manager**: Session tracking + limits
7. **gRPC Client**: Connect to inference service (async stub)
8. **Inference Router**: Load balancing + model affinity
9. **Rate Limiting**: Redis-based sliding window
10. **Twilio Integration**: Outbound call initiation via the Twilio Python SDK
11. **Monitoring**: Prometheus metrics export
12. **Testing**: Unit + integration tests (pytest, `httpx.AsyncClient`, WS test client)
