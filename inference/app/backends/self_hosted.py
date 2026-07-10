"""Self-hosted backend: per-frame streaming voice conversion via ONNX Runtime.

This is the first-priority engine (owner decision, 2026-07-04). Unlike the
clip-based Cartesia session, this session streams: input frames are grouped
into short blocks (default 100ms), each block is run through a local ONNX
voice-conversion model together with a little left context for continuity,
and the converted block is emitted immediately as 20ms frames. Latency is
block size + inference time, not utterance length.

Model contract (the piece M5b's RVC/OpenVoice export must satisfy):

- a single ``.onnx`` file per voice, named ``{model_id}.onnx``;
- first graph input: float32 mono audio, shape ``[1, N]``, range -1..1, at
  ``SELF_HOSTED_MODEL_SAMPLE_RATE``;
- first graph output: float32 mono audio in the same layout. Output length
  may differ from input length; it is mapped back proportionally.

Models are resolved from ``SELF_HOSTED_MODEL_DIR`` and, when missing there,
downloaded from S3/MinIO (``S3_ENDPOINT`` / ``S3_BUCKET``, key
``models/{model_id}.onnx``). Loaded sessions are LRU-cached per process.

``cloud_gpu`` runs this exact backend — same stack, deployed on a rented GPU
box; the gateway just dials that box's gRPC endpoint (see .env.example).

Error posture mirrors the Cartesia backend: a missing/broken model or a
failed inference must not kill the gRPC stream — the affected audio is
passed through unchanged and the problem is logged.
"""

import asyncio
import re
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np
import structlog

from app.backends.base import BackendSession, InferenceBackend
from app.dsp import apply_tuning
from app.tuning import IDENTITY_TUNE_PARAMS, TuneParams

log = structlog.get_logger(__name__)

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Preference order for "auto"; explicit device names map to one provider.
_DEVICE_PROVIDERS = {
    "cuda": ["CUDAExecutionProvider"],
    "coreml": ["CoreMLExecutionProvider"],
    "cpu": ["CPUExecutionProvider"],
}
_AUTO_ORDER = ["cuda", "coreml", "cpu"]


def pick_providers(device: str, available: list[str]) -> list[str]:
    """Map a DEVICE setting to an ONNX Runtime provider list, CPU as final fallback."""
    if device == "auto":
        for name in _AUTO_ORDER:
            provider = _DEVICE_PROVIDERS[name][0]
            if provider in available:
                return [provider, "CPUExecutionProvider"] if name != "cpu" else [provider]
        return ["CPUExecutionProvider"]
    providers = [p for p in _DEVICE_PROVIDERS[device] if p in available]
    if not providers:
        log.warning("self_hosted.device_unavailable", device=device, available=available)
        return ["CPUExecutionProvider"]
    if providers != ["CPUExecutionProvider"]:
        providers.append("CPUExecutionProvider")
    return providers


def _resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resample. Good enough for the hot path; no scipy dep."""
    if src_rate == dst_rate or audio.size == 0:
        return audio
    dst_len = max(1, round(audio.size * dst_rate / src_rate))
    src_pos = np.linspace(0.0, audio.size - 1, dst_len, dtype=np.float64)
    return np.interp(src_pos, np.arange(audio.size, dtype=np.float64), audio).astype(np.float32)


def _pcm_to_float(pcm: bytes) -> np.ndarray:
    if len(pcm) % 2:  # defensive: whole Int16 samples only
        pcm = pcm[:-1]
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def _float_to_pcm(audio: np.ndarray) -> bytes:
    # Symmetric 32768 scale + rint so a pass-through block round-trips Int16
    # samples exactly (the *32767-and-truncate variant loses 1 LSB).
    return np.rint(np.clip(audio * 32768.0, -32768.0, 32767.0)).astype(np.int16).tobytes()


class _SelfHostedSession(BackendSession):
    """Block-streaming session: buffer ``block_frames`` frames, convert, emit."""

    def __init__(self, backend: SelfHostedBackend) -> None:
        self._backend = backend
        self._block_frames = backend.block_frames
        self._buf: list[bytes] = []
        # Rolling left context (already-heard input) fed to the model ahead of
        # each block so block boundaries don't reset the model cold.
        self._context = np.zeros(0, dtype=np.float32)
        self._out = bytearray()
        self._sample_rate = 48000
        self._model_id = ""
        # Seam crossfade (M5b): the last crossfade_ms of each converted block
        # is held back and linearly blended with the head of the next block.
        # Real VC models decode blocks independently, so seams click without
        # it. Costs crossfade_ms of extra latency; 0 disables.
        self._xfade_tail: np.ndarray | None = None

    async def push(self, pcm: bytes, sample_rate: int, model_id: str) -> list[bytes]:
        # The stream contract is fixed 48kHz/20ms frames; a mid-block
        # sample_rate change would mis-size frames already buffered.
        self._sample_rate = sample_rate
        emitted: list[bytes] = []
        if self._buf and model_id != self._model_id:
            # Voice switched mid-block: convert the buffered frames under the
            # old voice so they don't come out sounding like the new one.
            emitted = await self._convert_block(self._model_id)
        self._model_id = model_id
        self._buf.append(pcm)
        if len(self._buf) < self._block_frames:
            return emitted
        return emitted + await self._convert_block(model_id)

    async def flush(self) -> list[bytes]:
        if self._buf:
            return await self._convert_block(self._model_id, pad_final=True)
        # Release any held-back crossfade tail — it is real audio the stream
        # still owes the listener.
        if self._xfade_tail is not None:
            self._out += _float_to_pcm(self._xfade_tail)
            self._xfade_tail = None
        # A model whose output length isn't exactly proportional can also
        # leave a sub-frame residue in _out — pad and emit rather than drop
        # the stream's final <20ms of audio.
        if self._out:
            frame_bytes = int(self._sample_rate * self._backend.frame_ms / 1000) * 2
            tail = bytes(self._out) + b"\x00" * (frame_bytes - len(self._out))
            self._out.clear()
            return [tail]
        return []

    async def _convert_block(self, model_id: str, pad_final: bool = False) -> list[bytes]:
        block_pcm = b"".join(self._buf)
        self._buf = []
        block = _pcm_to_float(block_pcm)

        xfade = self._backend.xfade_samples(self._sample_rate)
        converted, head = await self._backend.convert_block(
            block, self._context, self._sample_rate, model_id, head_samples=xfade
        )

        # Slide the context window: keep the most recent context_samples of input.
        keep = self._backend.context_samples(self._sample_rate)
        joined = np.concatenate([self._context, block])
        self._context = joined[-keep:] if keep else joined[:0]

        converted = self._seam_crossfade(converted, head, xfade, pad_final)
        # Fine-tune DSP hook (M10): pitch/speed/breathiness, keyed by the same
        # model_id already routing this stream to an ONNX model. Applied once
        # per block (not per 20ms frame) and skipped entirely for an untuned
        # voice (TuneParams().is_identity) — see app/tuning.py's module
        # docstring for the full out-of-band wiring story.
        tune = self._backend.get_tune_params(model_id)
        if not tune.is_identity:
            converted = apply_tuning(
                converted,
                pitch_offset=tune.pitch_offset,
                speed_factor=tune.speed_factor,
                breathiness=tune.breathiness,
            )
        self._out += _float_to_pcm(converted)
        frame_bytes = int(self._sample_rate * self._backend.frame_ms / 1000) * 2
        frames: list[bytes] = []
        while len(self._out) >= frame_bytes:
            frames.append(bytes(self._out[:frame_bytes]))
            del self._out[:frame_bytes]
        if pad_final and self._out:
            frames.append(bytes(self._out) + b"\x00" * (frame_bytes - len(self._out)))
            self._out.clear()
        return frames

    def _seam_crossfade(
        self, converted: np.ndarray, head: int, xfade: int, final: bool
    ) -> np.ndarray:
        """Blend block seams (M5b): real VC models decode each block
        independently, so adjacent blocks meet with an audible click.

        ``converted`` starts with ``head`` samples that re-render audio the
        previous block already produced (rendered from this block's left
        context). The previous block's last ``xfade`` samples were held back
        instead of emitted; here the two renderings of that same time span are
        linearly blended — old rendering fading out, new fading in — and this
        block's own tail is held back for the next seam. Costs ``xfade``
        samples of latency; disabled when crossfade_ms=0.
        """
        head_part, body = converted[:head], converted[head:]
        pieces: list[np.ndarray] = []
        if self._xfade_tail is not None:
            tail = self._xfade_tail
            self._xfade_tail = None
            n = min(tail.size, head_part.size)
            if n:
                # Align at the seam (the ends): both slices end where the new
                # block's own audio begins.
                ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
                pieces.append(tail[: tail.size - n])
                pieces.append(tail[tail.size - n :] * (1.0 - ramp) + head_part[head - n :] * ramp)
            else:
                # Degraded block (no re-rendered head available): emit the held
                # tail as-is so no audio is lost.
                pieces.append(tail)
        if xfade and not final and body.size:
            hold = min(xfade, body.size)
            pieces.append(body[: body.size - hold])
            self._xfade_tail = body[body.size - hold :]
        else:
            pieces.append(body)
        return np.concatenate(pieces) if len(pieces) > 1 else pieces[0]


class SelfHostedBackend(InferenceBackend):
    def __init__(
        self,
        model_dir: str,
        default_model: str = "",
        model_sample_rate: int = 48000,
        device: str = "auto",
        frame_ms: int = 20,
        block_ms: int = 100,
        context_ms: int = 200,
        crossfade_ms: int = 5,
        max_loaded_models: int = 4,
        s3_endpoint: str = "",
        s3_bucket: str = "",
    ) -> None:
        self._model_dir = Path(model_dir)
        self._default_model = default_model
        self._model_sample_rate = model_sample_rate
        self._device = device
        self.frame_ms = frame_ms
        # Ceil-divide so the effective block is never shorter than block_ms
        # (round() would turn 90ms/20ms into 4 frames = 80ms).
        self.block_frames = max(1, -(-block_ms // frame_ms))
        self._context_ms = context_ms
        self._crossfade_ms = crossfade_ms
        self._max_loaded = max(1, max_loaded_models)
        self._s3_endpoint = s3_endpoint
        self._s3_bucket = s3_bucket
        self._sessions: OrderedDict[str, object] = OrderedDict()
        self._load_lock = asyncio.Lock()
        self._warned_missing: set[str] = set()
        # Negative cache: models that failed to resolve/load recently. Without
        # it every 100ms block would retry a full S3 download (inside the lock),
        # turning per-block latency into the S3 error RTT.
        # Both id-keyed caches are fed by client-supplied model ids, so they
        # are capped: a client probing unique bogus ids must not grow memory
        # without bound in a long-lived process.
        self._failed_at: dict[str, float] = {}
        self._failed_retry_s = 30.0
        self._id_cache_cap = 1024
        # Fine-tune params (M10), keyed by the same model_id as the session
        # cache above; see app/tuning.py for how POST /voices/{id}/tune fills
        # this in. Same client-supplied-key bound as the other id-keyed caches.
        self._tune_params: dict[str, TuneParams] = {}
        import onnxruntime as ort

        self.providers = pick_providers(device, ort.get_available_providers())

    def open_session(self) -> BackendSession:
        return _SelfHostedSession(self)

    def set_tune_params(self, model_id: str, params: TuneParams) -> None:
        """Store fine-tune knobs for ``model_id``; read by the next converted block."""
        if len(self._tune_params) >= self._id_cache_cap and model_id not in self._tune_params:
            self._tune_params.pop(next(iter(self._tune_params)))  # oldest insertion first
        self._tune_params[model_id] = params

    def get_tune_params(self, model_id: str) -> TuneParams:
        """Fine-tune knobs for ``model_id``, or the shared identity default if never tuned.

        The untuned case returns the module-level ``IDENTITY_TUNE_PARAMS``
        singleton rather than a fresh allocation, so reading tuning once per
        converted block costs nothing for a voice nobody has tuned.
        """
        return self._tune_params.get(model_id, IDENTITY_TUNE_PARAMS)

    def context_samples(self, sample_rate: int) -> int:
        return int(sample_rate * self._context_ms / 1000)

    def xfade_samples(self, sample_rate: int) -> int:
        return int(sample_rate * self._crossfade_ms / 1000)

    async def convert_block(
        self,
        block: np.ndarray,
        context: np.ndarray,
        sample_rate: int,
        model_id: str,
        head_samples: int = 0,
    ) -> tuple[np.ndarray, int]:
        """Convert one block (with left context) through the ONNX model.

        Returns ``(audio, head)``: the block's converted audio at
        ``sample_rate``, preceded by up to ``head_samples`` of the model's
        re-rendering of the context that came before the block (``head`` says
        how many actually made it — 0 when there was no context or on any
        degrade path). The session crossfades that head against the previous
        block's held-back tail. Any failure passes the block through unchanged
        (never kills the stream).
        """
        if block.size == 0:
            return block, 0
        name = model_id or self._default_model
        session = await self._get_session(name)
        if session is None:
            return block, 0

        model_in = np.concatenate([context, block])
        if sample_rate != self._model_sample_rate:
            model_in = _resample(model_in, sample_rate, self._model_sample_rate)
        try:
            # ONNX Runtime's run() is blocking; keep the event loop free so
            # other gRPC streams aren't starved during inference.
            input_name = session.get_inputs()[0].name
            (model_out,) = await asyncio.to_thread(
                session.run, None, {input_name: model_in.reshape(1, -1)}
            )
        except Exception as exc:  # noqa: BLE001 - any ORT failure degrades, never crashes
            log.warning("self_hosted.inference_failed", model=name, error=str(exc))
            return block, 0
        audio = np.asarray(model_out, dtype=np.float32).reshape(-1)
        if sample_rate != self._model_sample_rate:
            audio = _resample(audio, self._model_sample_rate, sample_rate)

        # The model saw context+block; output length may also differ from input
        # length. Map back proportionally and keep the block's share, plus up
        # to head_samples of the re-rendered context just before it.
        total_in = context.size + block.size
        block_share = max(1, round(audio.size * block.size / total_in)) if audio.size else 0
        block_share = min(block_share, audio.size)
        # STFT-based models truncate a partial hop (<25ms) at the window end;
        # proportional mapping would turn that into a few percent of time
        # compression on every block. The window size is constant, so the next
        # block re-renders exactly the truncated span — emitting the last
        # block-worth instead keeps seams contiguous and the stream 1:1.
        # Note: head below stays non-zero once there's left context, so the
        # seam crossfade keeps running for truncating models. It stays aligned
        # because the rule shifts every block's emitted window by the same
        # constant hop, so a block's held tail and the next block's re-rendered
        # head still span the same real time (count stays exactly 1:1).
        deficit = block.size - block_share
        if 0 < deficit <= int(sample_rate * 0.025):
            block_share = min(block.size, audio.size)
        head = min(head_samples, audio.size - block_share)
        return audio[audio.size - block_share - head :], head

    async def _get_session(self, model_id: str):
        """LRU-cached ONNX session for ``model_id``; None when unresolvable."""
        if not model_id or not _MODEL_ID_RE.match(model_id):
            self._warn_once(model_id or "<empty>", "no usable model id; passing audio through")
            return None
        # Cache-hit fast path outside the lock: a slow S3 download / session
        # build for one stream must not stall other streams already running on
        # cached models. dict reads are atomic; move_to_end is best-effort here.
        session = self._sessions.get(model_id)
        if session is not None:
            self._sessions.move_to_end(model_id)
            return session
        if self._resolve_failed_recently(model_id):
            return None
        async with self._load_lock:
            if model_id in self._sessions:
                self._sessions.move_to_end(model_id)
                return self._sessions[model_id]
            path = await self._resolve_model_path(model_id)
            if path is None:
                self._mark_failed(model_id)
                return None
            try:
                session = await asyncio.to_thread(self._create_ort_session, str(path))
            except Exception as exc:  # noqa: BLE001 - bad weights degrade, never crash
                log.warning("self_hosted.model_load_failed", model=model_id, error=str(exc))
                self._mark_failed(model_id)
                return None
            self._sessions[model_id] = session
            if len(self._sessions) > self._max_loaded:
                evicted, _ = self._sessions.popitem(last=False)
                log.info("self_hosted.model_evicted", model=evicted)
            log.info("self_hosted.model_loaded", model=model_id, path=str(path))
            return session

    def _mark_failed(self, model_id: str) -> None:
        now = time.monotonic()
        if len(self._failed_at) >= self._id_cache_cap:
            cutoff = now - self._failed_retry_s
            self._failed_at = {k: v for k, v in self._failed_at.items() if v > cutoff}
            while len(self._failed_at) >= self._id_cache_cap:
                self._failed_at.pop(next(iter(self._failed_at)))  # oldest insertions first
        self._failed_at[model_id] = now

    def _resolve_failed_recently(self, model_id: str) -> bool:
        failed = self._failed_at.get(model_id)
        if failed is None:
            return False
        if time.monotonic() - failed >= self._failed_retry_s:
            del self._failed_at[model_id]  # cooldown over; allow a retry
            return False
        return True

    def _create_ort_session(self, path: str):
        import onnxruntime as ort

        return ort.InferenceSession(path, providers=self.providers)

    async def _resolve_model_path(self, model_id: str) -> Path | None:
        path = self._model_dir / f"{model_id}.onnx"
        if path.exists():
            return path
        if self._s3_bucket:
            try:
                await asyncio.to_thread(self._download_from_s3, model_id, path)
                return path
            except Exception as exc:  # noqa: BLE001 - S3 miss degrades, never crashes
                log.warning("self_hosted.s3_download_failed", model=model_id, error=str(exc))
                return None
        self._warn_once(model_id, "model file not found; passing audio through")
        return None

    def _download_from_s3(self, model_id: str, dest: Path) -> None:
        import boto3

        dest.parent.mkdir(parents=True, exist_ok=True)
        client = boto3.client("s3", endpoint_url=self._s3_endpoint or None)
        client.download_file(self._s3_bucket, f"models/{model_id}.onnx", str(dest))

    def _warn_once(self, model_id: str, note: str) -> None:
        if model_id in self._warned_missing:
            return
        if len(self._warned_missing) >= self._id_cache_cap:
            self._warned_missing.clear()  # occasional re-warn beats unbounded growth
        self._warned_missing.add(model_id)
        log.warning("self_hosted.no_model", model=model_id, note=note)
