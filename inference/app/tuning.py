"""Fine-tune controls: the out-of-band channel for pitch/speed/breathiness (M10).

**Mini-spike outcome.** PRODUCT_SPEC §6 added ``pitch_offset``/``speed_factor``/
``breathiness`` to ``VoiceModel`` back in M9, "inert until M10 wires them into
the streaming session." The spike question was: how do three per-voice knobs
reach the self-hosted ``BlockStreamSession`` without touching either frozen
contract — the gRPC ``AudioFrame`` proto (``pcm``, ``sample_rate``,
``model_id`` only, see ``proto/audio.proto``) or the WS JSON control protocol
(``agents/AGENTS.md``)? Both are shared across three services and explicitly
called out as "wins over any copy elsewhere" — adding a field to either means
regenerating stubs in two languages' worth of tooling (well, two Python
services, but still both proto_gen trees) for what is a low-frequency,
per-voice *setting*, not a per-frame *signal*.

The chosen channel mirrors a pattern already in the codebase for exactly this
kind of "occasional config, not every-frame data" problem: ``POST
/train_hd`` — a plain HTTP side-channel on the same inference FastAPI app,
called by the gateway, decoupled from the gRPC stream entirely. Concretely:

    gateway `PATCH /api/voices/{id}` (app/voices/routes.py)
        -> persists pitch/speed/breathiness on the caller's VoiceModel row
        -> best-effort HTTP POST to inference `/voices/{model_id}/tune`
    inference `POST /voices/{model_id}/tune` (this module)
        -> validates ranges, stores a `TuneParams` in `SelfHostedBackend`
           (process-local, keyed by streaming model_id — the same id a
           gRPC AudioFrame already carries on every frame)
    `_SelfHostedSession._convert_block` (app/backends/self_hosted.py)
        -> reads `backend.get_tune_params(model_id)` once per **block**
           (not per 20ms frame) and runs `app.dsp.apply_tuning(...)` on the
           already-converted, already-crossfaded block before it's chunked
           back into 20ms PCM frames.

This keeps the frame contract byte-for-byte unchanged: `model_id` was already
threaded through every `push()` call for model routing, so reusing it as the
tuning lookup key costs nothing new on the hot path, and a stream whose voice
was never tuned pays zero DSP cost (`TuneParams().is_identity` short-circuits
before any numpy call — see `dsp.apply_tuning`).

**Storage posture.** The store lives on `SelfHostedBackend` (one instance for
the process's lifetime, same as its ONNX session cache), not a database —
inference owns model artifacts and now this adjacent per-model config, while
gateway remains the sole source of truth in Postgres (`VoiceModel` rows). A
process restart loses the in-memory copy; the gateway re-pushes the current
values on every `PATCH` (and the settings are inert defaults otherwise, not a
crash), so drift self-heals on the next tune rather than needing its own
migration/persistence story. This mirrors the ``self_hosted`` backend's
existing "gateway is the durable owner, inference resolves what it needs
on demand" split used for model artifacts themselves.
"""

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.dsp import BREATHINESS_MAX, BREATHINESS_MIN, PITCH_MAX, PITCH_MIN, SPEED_MAX, SPEED_MIN

log = structlog.get_logger(__name__)

router = APIRouter()


class TuneParams:
    """Fine-tune knobs for one streaming ``model_id``, treated as immutable.

    Immutability is a convention, not enforced: nothing mutates an instance
    after construction (``set_tune_params`` always stores a brand-new one),
    which is what lets the shared ``IDENTITY_TUNE_PARAMS`` singleton below be
    handed out for every untuned lookup without risk. Do not add in-place
    mutation without revisiting that. A plain class (not a dataclass) so a
    bare ``TuneParams()`` is cheap to construct as the "nobody has tuned this
    voice" default without importing ``dataclasses`` for three fields;
    equality is defined explicitly since it's the identity-check
    ``self_hosted.py`` uses to skip DSP entirely.
    """

    __slots__ = ("pitch_offset", "speed_factor", "breathiness")

    def __init__(
        self, pitch_offset: float = 0.0, speed_factor: float = 1.0, breathiness: float = 0.0
    ) -> None:
        self.pitch_offset = pitch_offset
        self.speed_factor = speed_factor
        self.breathiness = breathiness

    @property
    def is_identity(self) -> bool:
        return self.pitch_offset == 0.0 and self.speed_factor == 1.0 and self.breathiness == 0.0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TuneParams):
            return NotImplemented
        return (
            self.pitch_offset == other.pitch_offset
            and self.speed_factor == other.speed_factor
            and self.breathiness == other.breathiness
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (
            f"TuneParams(pitch_offset={self.pitch_offset}, "
            f"speed_factor={self.speed_factor}, breathiness={self.breathiness})"
        )


# Shared identity singleton (M10): SelfHostedBackend.get_tune_params hands this
# out for an untuned voice instead of allocating a fresh TuneParams() on every
# converted block, keeping the untuned hot path allocation-free (as dsp.py's
# module docstring promises). Safe only because TuneParams is treated as
# immutable (see its docstring): never mutate this object in place.
IDENTITY_TUNE_PARAMS = TuneParams()


class TuneRequest(BaseModel):
    """Body for ``POST /voices/{model_id}/tune``. Same ranges as PRODUCT_SPEC §6."""

    pitch_offset: float = Field(0.0, ge=PITCH_MIN, le=PITCH_MAX)
    speed_factor: float = Field(1.0, ge=SPEED_MIN, le=SPEED_MAX)
    breathiness: float = Field(0.0, ge=BREATHINESS_MIN, le=BREATHINESS_MAX)


@router.post("/voices/{model_id}/tune")
async def tune_voice(model_id: str, payload: TuneRequest, request: Request) -> dict:
    """Store fine-tune params for ``model_id``; applied by the next converted block.

    Unauthenticated at this hop, same reasoning as ``POST /voices`` and ``POST
    /train_hd``: this is an internal call from the gateway, not a client-
    facing endpoint. 400 outside self_hosted/cloud_gpu (only
    ``SelfHostedBackend``'s session reads these params); 503 if the backend
    hasn't finished starting (should not happen once the app's lifespan has
    run, but a bare/partial app state must not 500).
    """
    # NOTE: no auth on this hop by design (same as POST /voices and POST
    # /train_hd): it is an internal gateway->inference call. That is safe only
    # while inference stays on a trusted network the gateway reaches privately.
    # If inference is ever exposed to untrusted callers, any client could
    # mutate per-model tuning and disrupt live sessions; the fix then is a
    # shared-secret header enforced on every write endpoint here, not just this
    # one. Adding it now is production hardening out of v1 scope (the portfolio
    # deployment is single-box with inference not publicly reachable).
    if settings.inference_backend not in ("self_hosted", "cloud_gpu"):
        raise HTTPException(
            status_code=400,
            detail="fine-tune controls require INFERENCE_BACKEND=self_hosted|cloud_gpu",
        )
    backend = getattr(request.app.state, "backend", None)
    if backend is None or not hasattr(backend, "set_tune_params"):
        raise HTTPException(status_code=503, detail="self-hosted backend not initialized")

    params = TuneParams(
        pitch_offset=payload.pitch_offset,
        speed_factor=payload.speed_factor,
        breathiness=payload.breathiness,
    )
    backend.set_tune_params(model_id, params)
    log.info(
        "tuning.updated",
        model=model_id,
        pitch_offset=params.pitch_offset,
        speed_factor=params.speed_factor,
        breathiness=params.breathiness,
    )
    return {
        "model_id": model_id,
        "pitch_offset": params.pitch_offset,
        "speed_factor": params.speed_factor,
        "breathiness": params.breathiness,
    }
