"""Inference service settings, loaded from the environment / .env.

Backend selection is the load-bearing setting here: ``INFERENCE_BACKEND`` picks
which transform runs behind the gRPC stream. See ``app/backends`` for the
implementations.
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # gRPC server bind. INFERENCE_GRPC_URL in .env.example is "localhost:50051";
    # the gateway dials that, we bind the matching port here.
    grpc_host: str = "0.0.0.0"
    grpc_port: int = 50051

    # Which transform to run. "passthrough" is the M3 default (no model cost).
    # "self_hosted" is the first-priority engine (M5); "cloud_gpu" is the same
    # stack deployed on a rented GPU box (the gateway dials that box instead);
    # "elevenlabs" is a placeholder mode from the spec, not yet implemented.
    inference_backend: Literal[
        "passthrough", "cartesia", "self_hosted", "cloud_gpu", "elevenlabs"
    ] = "passthrough"

    # Self-hosted / cloud_gpu backend (ONNX Runtime). DEVICE picks the execution
    # provider: auto = CUDA > CoreML > CPU, whichever is available.
    device: Literal["auto", "cuda", "coreml", "cpu"] = "auto"
    self_hosted_model_dir: str = "models"
    # Model used when a frame carries no model_id (same role as cartesia_voice_id).
    self_hosted_default_model: str = ""
    # Sample rate the ONNX model expects; audio is resampled in/out when it
    # differs from the stream's 48kHz.
    self_hosted_model_sample_rate: int = 48000
    # Streaming block size: latency floor of the self-hosted path. Each block is
    # converted with `context_ms` of already-heard audio prepended for continuity.
    self_hosted_block_ms: int = 100
    self_hosted_context_ms: int = 200
    self_hosted_max_loaded_models: int = 4

    # S3/MinIO model storage: models missing from self_hosted_model_dir are
    # fetched from s3://{s3_bucket}/models/{model_id}.onnx. Credentials come from
    # the standard AWS env vars (see .env.example).
    s3_endpoint: str = ""
    s3_bucket: str = ""

    # Cartesia cloud backend (only read when inference_backend == "cartesia").
    cartesia_api_key: str = ""
    cartesia_base_url: str = "https://api.cartesia.ai"
    cartesia_version: str = "2026-03-01"
    # Target voice used when a frame carries no model_id. Until per-frame model
    # routing lands, the cartesia backend needs a configured default.
    cartesia_voice_id: str = ""
    # Cap on an uploaded clone clip so POST /voices (unauthenticated pre-M5) can't
    # be used to exhaust memory with an oversized body.
    max_clip_bytes: int = 10 * 1024 * 1024

    # Utterance segmentation (cartesia backend). Cartesia's voice changer is
    # clip-based, so we group input frames into utterances with a simple energy
    # VAD: a frame counts as speech when its normalized RMS exceeds the
    # threshold; an utterance ends after `vad_silence_ms` of trailing silence
    # (or `vad_max_utterance_ms`, whichever comes first). `vad_preroll_ms` of
    # audio before the detected onset is kept so word onsets are not clipped.
    frame_ms: int = 20
    vad_energy_threshold: float = 0.02  # normalized RMS (0..1) above which a frame is speech
    vad_silence_ms: int = 500
    vad_max_utterance_ms: int = 15000
    vad_preroll_ms: int = 200


settings = Settings()
