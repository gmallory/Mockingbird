# Gateway Agent — Mockingbird

> **Scope:** Node.js WebSocket gateway server — connection management, authentication, request routing, load balancing, rate limiting.

---

## Identity

You are the Gateway Agent for Mockingbird. Your responsibility is building the Node.js WebSocket gateway that sits between browser clients and the Python ML inference service. You manage WebSocket connections, authenticate users, route audio streams to the appropriate inference worker, handle rate limiting, and ensure graceful degradation.

---

## Tech Stack

| Technology | Purpose |
|-----------|---------|
| **Node.js 20+** | Runtime |
| **Fastify 5** | HTTP framework (REST API endpoints) |
| **@fastify/websocket** | WebSocket upgrade handling |
| **ws** | WebSocket library |
| **gRPC (@grpc/grpc-js)** | Communication with inference service |
| **Redis (ioredis)** | Session store, pub/sub, rate limiting |
| **PostgreSQL (Prisma)** | User data, voice models, call history |
| **JWT (jose)** | Authentication |
| **TypeScript** | Type safety |
| **Pino** | Structured logging |
| **Vitest** | Testing |

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
│  │ Handler   │  │ (Fastify) │            │
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

```typescript
class ConnectionManager {
  private connections: Map<string, ClientConnection>;
  
  // Track active connections per user
  addConnection(userId: string, ws: WebSocket, sessionId: string): ClientConnection;
  removeConnection(sessionId: string): void;
  getConnection(sessionId: string): ClientConnection | undefined;
  getUserConnections(userId: string): ClientConnection[];
  
  // Limits
  isUserAtLimit(userId: string): boolean;  // Max concurrent connections per plan
  
  // Health
  getActiveCount(): number;
  getMetrics(): ConnectionMetrics;
}

interface ClientConnection {
  sessionId: string;
  userId: string;
  ws: WebSocket;
  modelId: string;
  inferenceStream: grpc.ClientDuplexStream;
  startedAt: Date;
  bytesIn: number;
  bytesOut: number;
  latencyMs: number[];    // Rolling window for percentile calculation
}
```

### Inference Router

```typescript
class InferenceRouter {
  private workers: InferenceWorker[];
  
  // Select best worker for a request
  selectWorker(modelId: string): InferenceWorker;
  
  // Routing strategies:
  // 1. Model affinity — prefer workers that already have the model loaded
  // 2. Least connections — prefer workers with fewer active streams
  // 3. Latency-based — prefer workers with lowest recent P50 latency
  
  // Health checks
  startHealthChecks(intervalMs: number): void;
  markUnhealthy(workerId: string): void;
  
  // Registration
  registerWorker(worker: InferenceWorker): void;
  deregisterWorker(workerId: string): void;
}

interface InferenceWorker {
  id: string;
  grpcUrl: string;
  activeStreams: number;
  maxStreams: number;
  loadedModels: string[];   // Model IDs currently in GPU memory
  healthStatus: 'healthy' | 'degraded' | 'unhealthy';
  avgLatencyMs: number;
}
```

### Rate Limiter

```typescript
class RateLimiter {
  // Sliding window rate limiting via Redis
  async checkLimit(userId: string, plan: Plan): Promise<RateLimitResult>;
  
  // Limits by plan:
  // Free:       1 concurrent connection, 5 min/month
  // Pro:        3 concurrent connections, 300 min/month
  // Enterprise: 10 concurrent connections, unlimited
  
  // Track usage
  async recordUsage(userId: string, durationMs: number): Promise<void>;
  async getUsage(userId: string): Promise<UsageStats>;
}
```

---

## REST API Endpoints

All REST endpoints are prefixed with `/api` and require JWT authentication.

### Voice Models

```typescript
// POST /api/voices — Create new voice model
// Body: multipart/form-data with audio file(s)
fastify.post('/api/voices', async (req, reply) => {
  // 1. Validate audio files (format, duration, size)
  // 2. Upload to S3
  // 3. Create VoiceModel record in PostgreSQL
  // 4. If HD Clone: enqueue training job via Redis
  // 5. If Instant Clone: call inference service synchronously
  // 6. Return VoiceModel
});

// GET /api/voices — List user's voice models
// GET /api/voices/:id — Get voice model details
// DELETE /api/voices/:id — Delete voice model + S3 artifacts
// PATCH /api/voices/:id — Update settings (pitch, speed, breathiness)
// POST /api/voices/:id/train — Trigger HD training
// GET /api/voices/:id/train/status — Check training progress
// POST /api/voices/:id/preview — Generate preview audio (returns audio/wav)
```

### Calls

```typescript
// POST /api/calls/outbound — Initiate PSTN call via Twilio
// Body: { phoneNumber: string, voiceModelId: string }
fastify.post('/api/calls/outbound', async (req, reply) => {
  // 1. Validate phone number
  // 2. Check rate limits
  // 3. Create Twilio call via REST API
  // 4. Return call SID and Twilio token for WebRTC
});

// GET /api/calls — List call history
// GET /api/calls/:id — Get call details + metrics
```

### User

```typescript
// GET /api/user/usage — Get usage statistics
// PATCH /api/user/settings — Update audio/quality preferences
```

---

## Database Schema (Prisma)

```prisma
model User {
  id                    String       @id @default(uuid())
  email                 String       @unique
  displayName           String
  plan                  Plan         @default(FREE)
  monthlyMinutesUsed    Float        @default(0)
  twilioPhoneNumber     String?
  settings              Json         @default("{}")
  createdAt             DateTime     @default(now())
  updatedAt             DateTime     @updatedAt
  
  voiceModels           VoiceModel[]
  calls                 CallRecord[]
}

model VoiceModel {
  id                    String       @id @default(uuid())
  userId                String
  user                  User         @relation(fields: [userId], references: [id])
  name                  String
  type                  CloneType
  status                ModelStatus
  sampleDurationSec     Float
  sampleCount           Int
  modelPath             String?      // S3 path
  onnxPath              String?
  modelSizeBytes        BigInt?
  similarityScore       Float?
  mosScore              Float?
  pitchOffset           Float        @default(0)
  speedFactor           Float        @default(1.0)
  breathiness           Float        @default(0.5)
  trainingStartedAt     DateTime?
  trainingCompletedAt   DateTime?
  createdAt             DateTime     @default(now())
  updatedAt             DateTime     @updatedAt
  
  calls                 CallRecord[]
}

model CallRecord {
  id                    String       @id @default(uuid())
  userId                String
  user                  User         @relation(fields: [userId], references: [id])
  voiceModelId          String
  voiceModel            VoiceModel   @relation(fields: [voiceModelId], references: [id])
  direction             CallDirection
  phoneNumber           String?
  peerId                String?
  startedAt             DateTime     @default(now())
  endedAt               DateTime?
  durationSec           Float        @default(0)
  avgLatencyMs          Float?
  p95LatencyMs          Float?
  dropoutCount          Int          @default(0)
  originalAudioPath     String?
  transformedAudioPath  String?
  status                CallStatus   @default(ACTIVE)
}

enum Plan {
  FREE
  PRO
  ENTERPRISE
}

enum CloneType {
  INSTANT
  HD
}

enum ModelStatus {
  UPLOADING
  TRAINING
  READY
  FAILED
}

enum CallDirection {
  INBOUND
  OUTBOUND
  P2P
}

enum CallStatus {
  ACTIVE
  COMPLETED
  FAILED
}
```

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

Export to Prometheus:

```typescript
// Counters
mockingbird_ws_connections_total        // Total WebSocket connections
mockingbird_ws_messages_total           // Total messages processed
mockingbird_api_requests_total          // REST API requests
mockingbird_training_jobs_total         // Training jobs submitted

// Gauges
mockingbird_ws_active_connections       // Current active connections
mockingbird_inference_active_streams    // Current active inference streams
mockingbird_inference_workers_healthy   // Healthy inference workers

// Histograms
mockingbird_audio_latency_ms           // End-to-end audio latency
mockingbird_inference_latency_ms       // Inference service latency
mockingbird_ws_message_size_bytes      // WebSocket message sizes
mockingbird_api_response_time_ms       // REST API response times
```

---

## Files to Create

```
gateway/
├── src/
│   ├── server.ts                 # Fastify server setup
│   ├── config.ts                 # Environment config with validation
│   ├── websocket/
│   │   ├── handler.ts            # WebSocket upgrade & message handling
│   │   ├── connection-manager.ts # Active connection tracking
│   │   └── protocol.ts           # Message type definitions & validation
│   ├── inference/
│   │   ├── router.ts             # Load-balanced inference routing
│   │   ├── grpc-client.ts        # gRPC client to inference service
│   │   └── health-check.ts       # Periodic health checks
│   ├── api/
│   │   ├── voices.ts             # Voice model CRUD routes
│   │   ├── calls.ts              # Call management routes
│   │   ├── user.ts               # User routes
│   │   └── twilio.ts             # Twilio webhook handlers
│   ├── auth/
│   │   ├── jwt.ts                # JWT validation
│   │   └── middleware.ts         # Auth middleware
│   ├── rate-limit/
│   │   └── limiter.ts            # Redis-based rate limiting
│   ├── monitoring/
│   │   └── metrics.ts            # Prometheus metrics
│   └── db/
│       ├── prisma/
│       │   └── schema.prisma     # Database schema
│       └── client.ts             # Prisma client singleton
├── tests/
│   ├── websocket.test.ts
│   ├── auth.test.ts
│   ├── rate-limit.test.ts
│   └── router.test.ts
├── package.json
├── tsconfig.json
├── Dockerfile
└── .env.example
```

---

## Implementation Order

1. **Project setup**: Initialize Node.js + Fastify + TypeScript
2. **Database**: Prisma schema + migrations
3. **Auth**: JWT middleware
4. **REST API**: Voice model CRUD endpoints
5. **WebSocket**: Connection handler + protocol
6. **Connection Manager**: Session tracking + limits
7. **gRPC Client**: Connect to inference service
8. **Inference Router**: Load balancing + model affinity
9. **Rate Limiting**: Redis-based sliding window
10. **Twilio Integration**: Outbound call initiation
11. **Monitoring**: Prometheus metrics export
12. **Testing**: Unit + integration tests
