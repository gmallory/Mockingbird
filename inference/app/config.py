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
    inference_backend: Literal["passthrough", "cartesia", "self_hosted"] = "passthrough"

    # Cartesia cloud backend (only read when inference_backend == "cartesia").
    cartesia_api_key: str = ""
    cartesia_base_url: str = "https://api.cartesia.ai"
    cartesia_version: str = "2024-11-13"
    # Target voice used when a frame carries no model_id. There is no voice-model
    # registry until M4, so the cartesia flip in M3 needs a configured default.
    cartesia_voice_id: str = ""


settings = Settings()
