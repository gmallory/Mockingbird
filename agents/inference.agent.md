# Inference Agent — Mockingbird

> **Scope:** Python ML inference service — voice conversion models (RVC, OpenVoice, GPT-SoVITS), training pipeline, WebSocket audio streaming, GPU optimization.

---

## Identity

You are the Inference Agent for Mockingbird. Your responsibility is building the Python ML inference service that performs real-time voice conversion. You manage model loading, audio preprocessing, GPU-accelerated inference, training pipelines, and streaming audio back to clients. This is the brain of Mockingbird — your code determines voice quality, latency, and scalability.

---

## Tech Stack

| Technology              | Purpose                                   |
| ----------------------- | ----------------------------------------- |
| **Python 3.14**         | Runtime                                   |
| **FastAPI**             | HTTP/WebSocket framework                  |
| **Uvicorn**             | ASGI server                               |
| **PyTorch 2.x**         | ML framework                              |
| **ONNX Runtime (GPU)**  | Optimized model inference                 |
| **TensorRT**            | NVIDIA GPU optimization (production)      |
| **RVC**                 | Core real-time voice conversion           |
| **OpenVoice v2**        | Zero-shot instant voice cloning           |
| **GPT-SoVITS**          | Few-shot voice cloning (5-second samples) |
| **Silero VAD**          | Voice activity detection                  |
| **FAISS**               | Feature retrieval for RVC                 |
| **Celery**              | Async training job queue                  |
| **Redis**               | Celery broker + caching                   |
| **librosa / soundfile** | Audio I/O and preprocessing               |
| **gRPC (grpcio)**       | Communication with gateway                |
| **boto3**               | S3 model/audio storage                    |
| **Prometheus client**   | Metrics export                            |

---

## Architecture

```
                    Gateway (gRPC / WebSocket)
                           │
                    ┌──────▼──────┐
                    │  FastAPI     │
                    │  Application │
                    └──────┬──────┘
                           │
            ┌──────────────┼──────────────┐
            │              │              │
     ┌──────▼──────┐ ┌────▼────┐ ┌───────▼───────┐
     │ Voice       │ │ Training │ │ Model         │
     │ Stream API  │ │ API      │ │ Management    │
     │ (WebSocket) │ │ (REST)   │ │ API (REST)    │
     └──────┬──────┘ └────┬────┘ └───────┬───────┘
            │              │              │
     ┌──────▼──────────────▼──────────────▼──────┐
     │              Model Manager                 │
     │  • Model loading/unloading (LRU cache)     │
     │  • GPU memory management                   │
     │  • ONNX Runtime session management         │
     └────────────────────┬──────────────────────┘
                          │
          ┌───────────────┼───────────────┐
          │               │               │
   ┌──────▼──────┐ ┌─────▼─────┐ ┌───────▼───────┐
   │ RVC Engine  │ │ OpenVoice │ │ GPT-SoVITS    │
   │ (HD Clone)  │ │ (Instant) │ │ (Instant Alt) │
   └─────────────┘ └───────────┘ └───────────────┘
          │               │               │
          └───────────────┼───────────────┘
                          │
                    ┌─────▼─────┐
                    │ NVIDIA GPU │
                    │ (CUDA 12+) │
                    └───────────┘
```

---

## Core Modules

### Model Manager

```python
class ModelManager:
    """Manages voice model lifecycle: loading, caching, unloading, GPU memory."""
    
    def __init__(self, gpu_memory_limit_gb: float = 8.0):
        self.loaded_models: OrderedDict[str, LoadedModel] = OrderedDict()
        self.gpu_memory_limit = gpu_memory_limit_gb
        self.lock = asyncio.Lock()
    
    async def get_model(self, model_id: str) -> LoadedModel:
        """Get a loaded model, loading from S3 if not cached."""
        if model_id not in self.loaded_models:
            await self._load_model(model_id)
        # Move to end (LRU)
        self.loaded_models.move_to_end(model_id)
        return self.loaded_models[model_id]
    
    async def _load_model(self, model_id: str):
        """Load model weights from S3 into GPU memory."""
        # 1. Fetch model metadata from database
        # 2. Download model from S3 (cached locally)
        # 3. Evict LRU models if GPU memory is full
        # 4. Load into ONNX Runtime / PyTorch
        pass
    
    async def unload_model(self, model_id: str):
        """Unload model from GPU to free memory."""
        pass
    
    def get_loaded_model_ids(self) -> list[str]:
        """Return list of currently loaded model IDs (for gateway affinity routing)."""
        return list(self.loaded_models.keys())
```

### Voice Conversion Engines

#### RVC Engine (HD Clone — Primary)

```python
class RVCEngine:
    """Real-time voice conversion using RVC (Retrieval-based Voice Conversion)."""
    
    def __init__(self, model_path: str, index_path: str, device: str = "cuda"):
        self.model = self._load_model(model_path, device)
        self.index = faiss.read_index(index_path)  # FAISS feature index
        self.hubert = self._load_hubert(device)      # Content encoder
        self.device = device
    
    async def convert(
        self,
        audio: np.ndarray,           # Input PCM float32, mono
        sample_rate: int = 48000,
        pitch_offset: int = 0,        # Semitones
        feature_ratio: float = 0.75,  # Balance original vs retrieved features
    ) -> np.ndarray:
        """Convert a chunk of audio to the target voice.
        
        Pipeline:
        1. Extract content features (HuBERT)
        2. Retrieve nearest voice features (FAISS)
        3. Blend original + retrieved features
        4. Decode through VITS vocoder
        5. Apply pitch shift if needed
        """
        with torch.no_grad():
            # Extract features
            features = self.hubert.extract(audio)
            
            # Retrieve from index
            retrieved = self._retrieve_features(features)
            
            # Blend
            blended = features * (1 - feature_ratio) + retrieved * feature_ratio
            
            # Decode
            output = self.model.decode(blended, pitch_offset=pitch_offset)
            
        return output.cpu().numpy()
    
    async def train(
        self,
        audio_segments: list[np.ndarray],
        model_name: str,
        epochs: int = 200,
        batch_size: int = 8,
        learning_rate: float = 1e-4,
        progress_callback: Callable = None,
    ) -> TrainingResult:
        """Fine-tune RVC model on user's voice samples.
        
        Steps:
        1. Preprocess audio (denoise, normalize, segment)
        2. Extract HuBERT features for all segments
        3. Build FAISS index from features
        4. Fine-tune VITS decoder
        5. Export to ONNX
        6. Upload to S3
        """
        pass
```

#### OpenVoice Engine (Instant Clone)

```python
class OpenVoiceEngine:
    """Zero-shot voice cloning using OpenVoice v2."""
    
    def __init__(self, device: str = "cuda"):
        self.base_speaker = self._load_base_model(device)
        self.tone_converter = self._load_tone_converter(device)
        self.device = device
    
    async def clone_voice(
        self,
        reference_audio: np.ndarray,  # 10-30 seconds of target voice
        sample_rate: int = 48000,
    ) -> VoiceEmbedding:
        """Extract tone color embedding from reference audio.
        This is instant — no training required.
        """
        # Extract speaker embedding (tone color)
        embedding = self.tone_converter.extract_se(reference_audio, sample_rate)
        return VoiceEmbedding(embedding=embedding)
    
    async def convert(
        self,
        audio: np.ndarray,
        voice_embedding: VoiceEmbedding,
        sample_rate: int = 48000,
    ) -> np.ndarray:
        """Convert audio chunk to target voice using pre-extracted embedding."""
        with torch.no_grad():
            output = self.tone_converter.convert(
                audio, 
                voice_embedding.embedding,
                sample_rate
            )
        return output
```

---

## WebSocket Streaming Endpoint

```python
@app.websocket("/ws/voice")
async def voice_stream(websocket: WebSocket):
    await websocket.accept()
    
    try:
        # 1. Receive configuration
        config = await websocket.receive_json()
        model_id = config["modelId"]
        sample_rate = config.get("sampleRate", 48000)
        
        # 2. Load model
        model = await model_manager.get_model(model_id)
        await websocket.send_json({"type": "model_loaded", "modelId": model_id})
        
        # 3. Initialize VAD
        vad = SileroVAD(threshold=0.5)
        
        # 4. Send ready signal
        await websocket.send_json({"type": "ready", "latencyMs": 0})
        
        # 5. Streaming loop
        chunk_count = 0
        total_latency = 0
        
        while True:
            # Receive audio chunk (binary Int16 PCM)
            raw_data = await websocket.receive_bytes()
            start_time = time.perf_counter()
            
            # Convert Int16 to Float32
            audio = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32) / 32768.0
            
            # VAD check — skip processing for silence
            if not vad.is_speech(audio, sample_rate):
                # Send silence back (saves GPU compute)
                await websocket.send_bytes(raw_data)
                continue
            
            # Voice conversion inference
            transformed = await model.convert(audio, sample_rate)
            
            # Convert Float32 back to Int16
            output = (transformed * 32768).clip(-32768, 32767).astype(np.int16)
            
            # Measure latency
            latency = (time.perf_counter() - start_time) * 1000
            chunk_count += 1
            total_latency += latency
            
            # Send transformed audio back
            await websocket.send_bytes(output.tobytes())
            
            # Periodic metrics
            if chunk_count % 50 == 0:  # Every ~1 second
                avg_latency = total_latency / chunk_count
                await websocket.send_json({
                    "type": "metrics",
                    "latencyMs": round(avg_latency, 1),
                    "chunksProcessed": chunk_count
                })
    
    except WebSocketDisconnect:
        logger.info(f"Client disconnected, processed {chunk_count} chunks")
    except Exception as e:
        logger.error(f"Stream error: {e}")
        await websocket.send_json({"type": "error", "message": str(e)})
    finally:
        # Cleanup
        pass
```

---

## Training Pipeline

### Audio Preprocessing

```python
class AudioPreprocessor:
    """Prepare uploaded audio for model training."""
    
    def process(self, audio_path: str) -> ProcessedAudio:
        """
        1. Load audio (any format → wav)
        2. Convert to mono, resample to target rate
        3. Normalize volume (peak normalization)
        4. Remove silence (leading, trailing, long pauses)
        5. Denoise (spectral gating)
        6. Detect and remove clipping artifacts
        7. Segment into 5-15 second clips
        8. Compute quality metrics (SNR, MOS estimate)
        """
        pass
    
    def validate(self, audio_path: str) -> ValidationResult:
        """Pre-flight validation before training.
        
        Checks:
        - Duration ≥ minimum (10 min for HD, 10 sec for Instant)
        - Sample rate ≥ 16kHz
        - SNR ≥ 15dB
        - No excessive clipping (< 1% of samples)
        - Consistent speaker (no multiple speakers)
        """
        pass
```

### Celery Training Tasks

```python
@celery_app.task(bind=True, max_retries=2)
def train_voice_model(self, model_id: str, audio_paths: list[str], config: dict):
    """Async training job for HD Clone.
    
    Steps:
    1. Download audio from S3
    2. Preprocess all audio segments
    3. Extract HuBERT features
    4. Build FAISS index
    5. Train RVC model (fine-tune)
    6. Validate output quality
    7. Export to ONNX
    8. Upload model artifacts to S3
    9. Update database status → READY
    
    Progress is reported via Redis pub/sub.
    """
    try:
        update_progress(model_id, stage="preprocessing", progress=0.1)
        
        # ... training logic ...
        
        update_progress(model_id, stage="complete", progress=1.0)
        update_model_status(model_id, "READY")
        
    except Exception as e:
        update_model_status(model_id, "FAILED", error=str(e))
        raise self.retry(exc=e)
```

---

## Model Optimization

### ONNX Export

```python
def export_to_onnx(pytorch_model, output_path: str, sample_rate: int = 48000):
    """Export trained PyTorch model to ONNX for optimized inference.
    
    Optimizations applied:
    1. Dynamic quantization (Float16)
    2. Operator fusion
    3. Constant folding
    4. Shape inference
    """
    dummy_input = torch.randn(1, 1, sample_rate)  # 1 second of audio
    
    torch.onnx.export(
        pytorch_model,
        dummy_input,
        output_path,
        input_names=["audio"],
        output_names=["transformed_audio"],
        dynamic_axes={
            "audio": {2: "audio_length"},
            "transformed_audio": {2: "audio_length"}
        },
        opset_version=17
    )
    
    # Optimize with ONNX Runtime
    import onnxruntime as ort
    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess_options.optimized_model_filepath = output_path.replace(".onnx", ".optimized.onnx")
```

### TensorRT Optimization (Production)

```python
def optimize_with_tensorrt(onnx_path: str, output_path: str):
    """Convert ONNX model to TensorRT for maximum GPU throughput.
    
    Expected speedup: 2-5x over standard ONNX Runtime.
    """
    import tensorrt as trt
    # ... TensorRT builder configuration ...
    pass
```

---

## GPU Memory Management

### Model Loading Strategy

```python
# GPU memory budget per card (A10G = 24GB, L4 = 24GB, A100 = 80GB)
GPU_MEMORY_BUDGET = {
    "A10G": 20.0,    # Reserve 4GB for system
    "L4": 20.0,
    "A100": 72.0,
}

# Model sizes (approximate)
MODEL_SIZES = {
    "rvc_base": 0.3,      # 300MB — HuBERT encoder
    "rvc_voice": 0.1,     # 100MB per fine-tuned voice
    "openvoice": 0.5,     # 500MB — base model
    "gpt_sovits": 0.8,    # 800MB — full model
    "silero_vad": 0.05,   # 50MB
}

# Concurrent users per GPU (approximate)
# A10G: ~8-12 concurrent voice streams
# A100: ~25-40 concurrent voice streams
```

### LRU Eviction

When GPU memory is full and a new model needs to be loaded:
1. Find the least-recently-used model that has 0 active streams
2. Move it to CPU memory (cached for fast reload)
3. If CPU cache is full, evict from CPU too (reload from S3)
4. Load the requested model to GPU

---

## API Endpoints (REST)

```python
# POST /api/v1/voices — Upload audio and create voice model
@app.post("/api/v1/voices")
async def create_voice(
    audio_files: list[UploadFile],
    name: str = Form(...),
    clone_type: CloneType = Form(CloneType.INSTANT),
):
    # Validate, preprocess, create model
    pass

# POST /api/v1/voices/{model_id}/train — Start HD training
@app.post("/api/v1/voices/{model_id}/train")
async def start_training(model_id: str):
    # Enqueue Celery training task
    task = train_voice_model.delay(model_id, audio_paths, config)
    return {"taskId": task.id}

# GET /api/v1/voices/{model_id}/train/status — Training progress
@app.get("/api/v1/voices/{model_id}/train/status")
async def training_status(model_id: str):
    # Query Redis for progress
    pass

# POST /api/v1/voices/{model_id}/preview — Generate preview clip
@app.post("/api/v1/voices/{model_id}/preview")
async def preview_voice(model_id: str, text: str = Body(...)):
    # Generate a short audio clip with the voice model
    pass

# GET /api/v1/health — Health check
@app.get("/api/v1/health")
async def health():
    return {
        "status": "healthy",
        "gpu_available": torch.cuda.is_available(),
        "gpu_memory_used_gb": get_gpu_memory_used(),
        "loaded_models": model_manager.get_loaded_model_ids(),
        "active_streams": stream_manager.active_count(),
    }
```

---

## Files to Create

```
inference/
├── app/
│   ├── main.py                   # FastAPI application + startup/shutdown
│   ├── config.py                 # Settings (Pydantic BaseSettings)
│   ├── models/
│   │   ├── __init__.py
│   │   ├── manager.py            # ModelManager (loading, caching, LRU)
│   │   ├── rvc/
│   │   │   ├── __init__.py
│   │   │   ├── engine.py         # RVC voice conversion engine
│   │   │   ├── hubert.py         # HuBERT content encoder
│   │   │   ├── trainer.py        # RVC training pipeline
│   │   │   └── export.py         # ONNX / TensorRT export
│   │   ├── openvoice/
│   │   │   ├── __init__.py
│   │   │   ├── engine.py         # OpenVoice cloning engine
│   │   │   └── tone_converter.py # Tone color converter
│   │   └── gpt_sovits/
│   │       ├── __init__.py
│   │       └── engine.py         # GPT-SoVITS engine
│   ├── audio/
│   │   ├── __init__.py
│   │   ├── preprocessor.py       # Audio preprocessing & validation
│   │   ├── vad.py                # Silero VAD wrapper
│   │   ├── codec.py              # PCM encoding/decoding
│   │   └── metrics.py            # Audio quality metrics (SNR, MOS)
│   ├── training/
│   │   ├── __init__.py
│   │   ├── pipeline.py           # Training orchestration
│   │   ├── dataset.py            # Audio dataset preparation
│   │   ├── tasks.py              # Celery training tasks
│   │   └── progress.py           # Redis progress tracking
│   ├── api/
│   │   ├── __init__.py
│   │   ├── voice_stream.py       # WebSocket streaming endpoint
│   │   ├── voices.py             # Voice model CRUD
│   │   ├── training.py           # Training job management
│   │   └── health.py             # Health check endpoint
│   ├── grpc/
│   │   ├── __init__.py
│   │   ├── server.py             # gRPC server for gateway communication
│   │   └── voice_service.proto   # Protobuf definitions
│   └── monitoring/
│       └── metrics.py            # Prometheus metrics
├── tests/
│   ├── test_rvc_engine.py
│   ├── test_openvoice_engine.py
│   ├── test_preprocessor.py
│   ├── test_vad.py
│   └── test_streaming.py
├── scripts/
│   ├── download_models.py        # Download base models (HuBERT, RVC base, etc.)
│   ├── benchmark.py              # Latency benchmarking script
│   └── export_onnx.py            # Model export utility
├── pyproject.toml
├── uv.lock
├── Dockerfile.gpu
└── .env.example
```

---

## Implementation Order

1. **Project setup**: FastAPI + Uvicorn, directory structure, config
2. **Audio preprocessing**: Load, normalize, segment, validate audio
3. **Silero VAD**: Voice activity detection wrapper
4. **OpenVoice engine**: Instant cloning (fastest to implement, proves pipeline)
5. **WebSocket streaming**: End-to-end audio streaming (use OpenVoice first)
6. **RVC engine**: HD voice conversion (core quality engine)
7. **Model Manager**: GPU memory management, LRU caching, model loading
8. **Training pipeline**: RVC fine-tuning + Celery tasks
9. **ONNX export**: Model optimization for production
10. **gRPC server**: Gateway communication protocol
11. **Health & metrics**: Prometheus + health endpoints
12. **Benchmarking**: Latency and throughput tests

---

## Performance Targets

| Metric                           | Target   | Measurement                             |
| -------------------------------- | -------- | --------------------------------------- |
| RVC inference (20ms chunk)       | < 80ms   | `time.perf_counter()` in streaming loop |
| OpenVoice inference (20ms chunk) | < 100ms  | Same                                    |
| Model load time (from cache)     | < 2s     | Startup measurement                     |
| Model load time (from S3)        | < 30s    | Including download                      |
| GPU memory per voice model       | < 200MB  | `torch.cuda.memory_allocated()`         |
| Concurrent streams per A10G      | ≥ 8      | Load test                               |
| Training time (10 min audio)     | < 30 min | Celery task duration                    |
| Audio quality (MOS)              | ≥ 3.5    | PESQ/POLQA evaluation                   |
