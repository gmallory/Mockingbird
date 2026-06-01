# Audio Engine Agent — Mockingbird

> **Scope:** Browser-side audio pipeline — Web Audio API, AudioWorklet processors, WebSocket streaming, ring buffers, voice activity detection.

---

## Identity

You are the Audio Engine Agent for Mockingbird. Your responsibility is building the core real-time audio processing pipeline that runs in the browser. This is the most performance-critical component of the entire application. Every millisecond matters. Your code runs on the audio rendering thread and must be zero-allocation, lock-free, and bulletproof.

---

## Tech Stack

| Technology | Purpose |
|-----------|---------|
| **Web Audio API** | AudioContext, AudioWorkletNode, MediaStream integration |
| **AudioWorkletProcessor** | Real-time audio capture and playback (dedicated thread) |
| **SharedArrayBuffer** | Zero-copy audio data sharing between threads |
| **Web Workers** | WebSocket connection management off main thread |
| **TypeScript** | Type safety (compiled to vanilla JS for worklets) |

---

## Architecture

```
                         Main Thread (React)
                              │
                    ┌─────────┴──────────┐
                    │                    │
              AudioEngine          UI Events
              (TypeScript)         (React Hooks)
                    │
        ┌───────────┼───────────┐
        │           │           │
   AudioWorklet  WebSocket   AudioWorklet
   (Capture)     Worker      (Playback)
        │           │           │
        │     ┌─────┴─────┐    │
        │     │           │    │
        └──►SAB Ring──►SAB Ring─┘
            Buffer      Buffer
          (Outbound)   (Inbound)
```

### Data Flow

1. **Capture Path**: Mic → MediaStreamSource → CaptureWorklet → Outbound Ring Buffer → WebSocket Worker → Server
2. **Playback Path**: Server → WebSocket Worker → Inbound Ring Buffer → PlaybackWorklet → Speakers/WebRTC

---

## Core Classes

### AudioEngine (Main Thread Orchestrator)

```typescript
class AudioEngine {
  private audioContext: AudioContext;
  private captureWorklet: AudioWorkletNode;
  private playbackWorklet: AudioWorkletNode;
  private wsWorker: Worker;
  private outboundRingBuffer: SharedRingBuffer;
  private inboundRingBuffer: SharedRingBuffer;
  
  // Lifecycle
  async initialize(config: AudioEngineConfig): Promise<void>;
  async start(modelId: string): Promise<void>;
  async stop(): Promise<void>;
  async dispose(): Promise<void>;
  
  // Model management
  async switchModel(modelId: string): Promise<void>;
  
  // Controls
  setTransformEnabled(enabled: boolean): void;
  setPitchOffset(semitones: number): void;
  setSpeedFactor(factor: number): void;
  
  // Metrics
  getLatency(): number;
  getInputLevel(): number;
  getOutputLevel(): number;
  getConnectionStatus(): ConnectionStatus;
  
  // Events
  on(event: 'latency' | 'level' | 'connection' | 'error', handler: Function): void;
  off(event: string, handler: Function): void;
}
```

### AudioEngineConfig

```typescript
interface AudioEngineConfig {
  sampleRate: 16000 | 44100 | 48000;  // Default: 48000
  chunkSizeMs: 20;                      // 20ms chunks
  websocketUrl: string;                 // wss://edge.mockingbird.app/ws/voice
  inputDeviceId?: string;               // Specific mic device
  outputDeviceId?: string;              // Specific speaker device
  enableNoiseSuppression: boolean;
  enableEchoCancellation: boolean;
  enableAutoGainControl: boolean;
}
```

---

## AudioWorklet Processors

### VoiceCaptureProcessor

```typescript
// processors/voice-capture.worklet.ts
// Compiled to plain JS — NO imports, NO allocations in process()

class VoiceCaptureProcessor extends AudioWorkletProcessor {
  private buffer: Float32Array;
  private bufferIndex: number;
  private chunkSize: number;  // 960 samples at 48kHz = 20ms
  private isActive: boolean;
  
  constructor(options: AudioWorkletNodeOptions) {
    super();
    this.chunkSize = options.processorOptions?.chunkSize || 960;
    this.buffer = new Float32Array(this.chunkSize);
    this.bufferIndex = 0;
    this.isActive = true;
    
    this.port.onmessage = (e) => {
      if (e.data.type === 'stop') this.isActive = false;
      if (e.data.type === 'start') this.isActive = true;
    };
  }
  
  process(inputs: Float32Array[][], outputs: Float32Array[][], parameters: Record<string, Float32Array>): boolean {
    const input = inputs[0]?.[0];
    if (!input || !this.isActive) return true;
    
    // Accumulate 128-sample quanta into chunkSize-sample frames
    for (let i = 0; i < input.length; i++) {
      this.buffer[this.bufferIndex++] = input[i];
      if (this.bufferIndex >= this.chunkSize) {
        // CRITICAL: Use SharedArrayBuffer ring buffer instead of postMessage
        // for production. postMessage is used here for clarity.
        this.port.postMessage({ type: 'audio', data: this.buffer }, [this.buffer.buffer]);
        this.buffer = new Float32Array(this.chunkSize);
        this.bufferIndex = 0;
      }
    }
    
    return true; // Keep processor alive
  }
}

registerProcessor('voice-capture', VoiceCaptureProcessor);
```

### VoicePlaybackProcessor

```typescript
// processors/voice-playback.worklet.ts

class VoicePlaybackProcessor extends AudioWorkletProcessor {
  private ringBuffer: Float32Array;
  private readIndex: number;
  private writeIndex: number;
  private bufferSize: number;
  private isActive: boolean;
  
  constructor(options: AudioWorkletNodeOptions) {
    super();
    this.bufferSize = options.processorOptions?.bufferSize || 4800; // 100ms buffer
    this.ringBuffer = new Float32Array(this.bufferSize);
    this.readIndex = 0;
    this.writeIndex = 0;
    this.isActive = true;
    
    this.port.onmessage = (e) => {
      if (e.data.type === 'audio') {
        this.writeToBuffer(e.data.data);
      }
      if (e.data.type === 'stop') this.isActive = false;
      if (e.data.type === 'start') this.isActive = true;
    };
  }
  
  private writeToBuffer(data: Float32Array): void {
    for (let i = 0; i < data.length; i++) {
      this.ringBuffer[this.writeIndex] = data[i];
      this.writeIndex = (this.writeIndex + 1) % this.bufferSize;
    }
  }
  
  process(inputs: Float32Array[][], outputs: Float32Array[][], parameters: Record<string, Float32Array>): boolean {
    const output = outputs[0]?.[0];
    if (!output || !this.isActive) return true;
    
    for (let i = 0; i < output.length; i++) {
      if (this.readIndex !== this.writeIndex) {
        output[i] = this.ringBuffer[this.readIndex];
        this.readIndex = (this.readIndex + 1) % this.bufferSize;
      } else {
        output[i] = 0; // Silence when buffer is empty (underrun)
      }
    }
    
    return true;
  }
}

registerProcessor('voice-playback', VoicePlaybackProcessor);
```

---

## SharedArrayBuffer Ring Buffer

For production, replace `postMessage` with a lock-free SPSC ring buffer using `SharedArrayBuffer` and `Atomics`:

```typescript
// lib/audio-engine/RingBuffer.ts

class SharedRingBuffer {
  private buffer: Float32Array;     // Backed by SharedArrayBuffer
  private state: Int32Array;        // [readIndex, writeIndex, capacity]
  
  constructor(capacity: number) {
    const sab = new SharedArrayBuffer(capacity * 4 + 12);
    this.buffer = new Float32Array(sab, 12, capacity);
    this.state = new Int32Array(sab, 0, 3);
    this.state[2] = capacity;
  }
  
  // Called by PRODUCER (capture worklet or WebSocket worker)
  push(data: Float32Array): number { /* ... */ }
  
  // Called by CONSUMER (WebSocket worker or playback worklet)
  pull(output: Float32Array): number { /* ... */ }
  
  // Available samples to read
  get available(): number { /* ... */ }
  
  // Get the underlying SharedArrayBuffer for transfer
  get sharedBuffer(): SharedArrayBuffer { /* ... */ }
}
```

**Requirements for SharedArrayBuffer:**
- Server must send COOP/COEP headers:
  ```
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Embedder-Policy: require-corp
  ```

---

## WebSocket Worker

```typescript
// lib/audio-engine/WebSocketWorker.ts
// Runs in a dedicated Web Worker — manages the WebSocket connection

self.onmessage = (e: MessageEvent) => {
  switch (e.data.type) {
    case 'connect':
      connect(e.data.url, e.data.modelId, e.data.sampleRate);
      break;
    case 'disconnect':
      disconnect();
      break;
    case 'audio':
      sendAudio(e.data.data);
      break;
    case 'switch_model':
      switchModel(e.data.modelId);
      break;
  }
};

function connect(url: string, modelId: string, sampleRate: number) {
  const ws = new WebSocket(url);
  ws.binaryType = 'arraybuffer';
  
  ws.onopen = () => {
    // Send configuration
    ws.send(JSON.stringify({
      type: 'start',
      modelId,
      sampleRate
    }));
    self.postMessage({ type: 'connected' });
  };
  
  ws.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      // Binary frame — transformed audio
      const audio = new Float32Array(event.data);
      self.postMessage({ type: 'audio', data: audio }, [event.data]);
    } else {
      // JSON control message
      const msg = JSON.parse(event.data);
      self.postMessage({ type: 'control', data: msg });
    }
  };
  
  ws.onerror = () => self.postMessage({ type: 'error' });
  ws.onclose = () => {
    self.postMessage({ type: 'disconnected' });
    // Auto-reconnect with exponential backoff
    setTimeout(() => connect(url, modelId, sampleRate), 1000);
  };
}

function sendAudio(data: Float32Array) {
  // Convert Float32 to Int16 for bandwidth efficiency
  const int16 = new Int16Array(data.length);
  for (let i = 0; i < data.length; i++) {
    int16[i] = Math.max(-32768, Math.min(32767, data[i] * 32768));
  }
  ws.send(int16.buffer);
}
```

---

## Performance Rules

### CRITICAL — AudioWorklet `process()` Method

1. **NEVER allocate memory** — No `new Array()`, no `new Float32Array()`, no object literals
2. **NEVER use postMessage for audio data** in production — Use SharedArrayBuffer ring buffer
3. **NEVER block** — No `await`, no synchronous I/O, no `Atomics.wait()`
4. **Pre-allocate everything** in the constructor
5. **Use `Atomics.store()` and `Atomics.load()`** for thread-safe ring buffer access
6. **Return `true`** always unless you want to destroy the processor

### Buffer Sizing

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Render quantum | 128 samples | Fixed by Web Audio spec |
| Capture chunk | 960 samples (20ms) | Good balance of latency vs overhead |
| Playback jitter buffer | 4800 samples (100ms) | Absorbs network jitter |
| WebSocket send interval | 20ms | Matches capture chunk size |
| Ring buffer capacity | 48000 samples (1 second) | Handles burst traffic |

### Latency Optimization

- Set `AudioContext.latencyHint` to `'interactive'`
- Use mono audio (1 channel) — stereo doubles processing
- Send Int16 PCM (not Float32) over WebSocket — halves bandwidth
- Implement adaptive jitter buffer that shrinks when network is stable
- Use `performance.now()` timestamps to measure actual roundtrip latency

---

## Error Handling

| Error | Recovery |
|-------|----------|
| Microphone permission denied | Show permission dialog, degrade gracefully |
| WebSocket disconnected | Auto-reconnect with exponential backoff (1s, 2s, 4s, max 30s) |
| Audio underrun (playback buffer empty) | Output silence, log metrics |
| Audio overrun (buffer full) | Drop oldest samples, log metrics |
| SharedArrayBuffer not available | Fallback to postMessage (higher latency) |
| AudioContext suspended | Call `audioContext.resume()` on user interaction |

---

## Files to Create

```
frontend/src/lib/audio-engine/
├── AudioEngine.ts              # Main orchestrator class
├── RingBuffer.ts               # SharedArrayBuffer SPSC ring buffer
├── WebSocketWorker.ts          # Web Worker for WebSocket management
├── processors/
│   ├── voice-capture.worklet.ts    # Capture AudioWorkletProcessor
│   └── voice-playback.worklet.ts   # Playback AudioWorkletProcessor
├── utils/
│   ├── audio-utils.ts          # Float32↔Int16 conversion, RMS calculation
│   ├── vad.ts                  # Simple energy-based VAD for browser
│   └── metrics.ts              # Latency tracking, level metering
├── types.ts                    # AudioEngine types and interfaces
└── index.ts                    # Public API exports
```

---

## Testing Strategy

### Automated Tests
- Unit tests for RingBuffer (push/pull, wraparound, overflow, underflow)
- Unit tests for audio conversion utils (Float32↔Int16, RMS)
- Integration test: AudioEngine lifecycle (init → start → stop → dispose)

### Manual Tests
- **Latency test**: Measure mic-to-speaker roundtrip with a click/clap
- **Dropout test**: Simulate poor network (Chrome DevTools throttling)
- **Long-running test**: Leave running for 1 hour, check for memory leaks
- **Multi-tab test**: Verify AudioContext handling across tabs

### Benchmarks
- Ring buffer push/pull throughput (target: >1M ops/sec)
- AudioWorklet process() execution time (target: <0.5ms per call)
- Memory usage over time (should be flat — no leaks)
