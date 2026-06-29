"""WebSocket JSON control-message models — the shared contract.

These mirror the protocol defined in ``agents/AGENTS.md``. Keep this in sync
with the browser audio engine and (later) the inference service when message
shapes change.

Binary frames (Int16 PCM, 20ms / 960 samples at 48kHz) are *not* modeled here;
they are forwarded as raw bytes. Only the JSON control channel is typed.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

# ----- Client -> Server -----------------------------------------------------


class StartMessage(BaseModel):
    type: Literal["start"] = "start"
    modelId: str | None = None  # noqa: N815 — wire contract is camelCase
    sampleRate: int = 48000  # noqa: N815


class SwitchModelMessage(BaseModel):
    type: Literal["switch_model"] = "switch_model"
    modelId: str  # noqa: N815


class StopMessage(BaseModel):
    type: Literal["stop"] = "stop"


class PingMessage(BaseModel):
    type: Literal["ping"] = "ping"


ClientMessage = Annotated[
    StartMessage | SwitchModelMessage | StopMessage | PingMessage,
    Field(discriminator="type"),
]

client_message_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# ----- Server -> Client -----------------------------------------------------


class ReadyMessage(BaseModel):
    type: Literal["ready"] = "ready"
    latencyMs: float = 0.0  # noqa: N815


class ModelLoadedMessage(BaseModel):
    type: Literal["model_loaded"] = "model_loaded"
    modelId: str  # noqa: N815


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    code: str = "internal_error"
    message: str = ""


class MetricsMessage(BaseModel):
    type: Literal["metrics"] = "metrics"
    latencyMs: float  # noqa: N815
    framesProcessed: int  # noqa: N815


class PongMessage(BaseModel):
    type: Literal["pong"] = "pong"


class DegradedMessage(BaseModel):
    """Inference hop is unavailable; the gateway is passing audio through unchanged.

    The session stays alive (client hears their own voice) instead of dropping.
    """

    type: Literal["degraded"] = "degraded"
    message: str = "voice transformation temporarily unavailable"
