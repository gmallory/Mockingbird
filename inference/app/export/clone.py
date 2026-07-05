"""Torch-free instant voice clone for the self-hosted backend (M5b).

Turns an uploaded reference clip into a per-voice ``{model_id}.onnx`` that the
M5a streaming backend can load, without torch in the service:

1. decode the clip to float32 mono 22.05kHz (stdlib ``wave`` for WAV, ffmpeg
   subprocess for anything else the browser records — webm/opus, m4a, ...);
2. run the exported ``openvoice_se_encoder.onnx`` over it → a 256-dim target
   speaker embedding;
3. copy ``openvoice_converter.onnx`` with its ``tgt_se`` initializer replaced
   by that embedding and write it to the model dir as ``{model_id}.onnx``.

The two template artifacts come from ``scripts/export_openvoice_onnx.py`` (the
torch half) and live in ``{SELF_HOSTED_MODEL_DIR}/openvoice/``.
"""

import hashlib
import io
import re
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import onnx
import structlog

from app.export.openvoice_templates import CONVERTER_TEMPLATE, SE_ENCODER

log = structlog.get_logger(__name__)

# Sample rate the OpenVoice V2 converter was trained at; both exported graphs
# expect audio at this rate (the streaming backend resamples 48kHz ↔ this).
OPENVOICE_SAMPLE_RATE = 22050
TEMPLATE_SUBDIR = "openvoice"
TGT_SE_NAME = "tgt_se"
# SE quality degrades sharply on very short references; also guarantees the
# clip outlives the converter's 384-sample reflect padding.
MIN_CLIP_SECONDS = 1.0

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


class CloneError(Exception):
    """Instant clone failed for a reason the caller should surface verbatim."""


def bake_tgt_se(template_path: Path, se: np.ndarray, out_path: Path) -> None:
    """Write a copy of the converter template with ``tgt_se`` set to ``se``.

    Also used once at export time to demote ``tgt_se`` from a required graph
    input to an initializer default (exporting it as an input is what stops
    constant folding from smearing the embedding into downstream weights).
    """
    model = onnx.load(str(template_path))
    graph = model.graph
    kept_inputs = [i for i in graph.input if i.name != TGT_SE_NAME]
    if len(kept_inputs) != len(graph.input):
        del graph.input[:]
        graph.input.extend(kept_inputs)
    for idx, init in enumerate(graph.initializer):
        if init.name == TGT_SE_NAME:
            del graph.initializer[idx]
            break
    graph.initializer.append(
        onnx.helper.make_tensor(
            TGT_SE_NAME, onnx.TensorProto.FLOAT, se.shape, se.astype(np.float32).tobytes(), raw=True
        )
    )
    onnx.save(model, str(out_path))


def _decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width == 2:
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        audio = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif width == 1:
        audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise CloneError(f"unsupported WAV sample width: {width} bytes")
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, rate


def _decode_ffmpeg(data: bytes) -> tuple[np.ndarray, int]:
    if shutil.which("ffmpeg") is None:
        raise CloneError(
            "clip is not WAV and ffmpeg is not installed; "
            "install ffmpeg or upload a 16-bit WAV clip"
        )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-f",
        "f32le",
        "-ac",
        "1",
        "-ar",
        str(OPENVOICE_SAMPLE_RATE),
        "pipe:1",
    ]
    proc = subprocess.run(cmd, input=data, capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        raise CloneError(f"ffmpeg could not decode the clip: {proc.stderr.decode()[:200]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy(), OPENVOICE_SAMPLE_RATE


def decode_clip(data: bytes) -> np.ndarray:
    """Decode an uploaded clip to float32 mono at ``OPENVOICE_SAMPLE_RATE``."""
    if data[:4] == b"RIFF":
        audio, rate = _decode_wav(data)
    else:
        audio, rate = _decode_ffmpeg(data)
    if rate != OPENVOICE_SAMPLE_RATE:
        # Same linear resample the streaming backend uses on the hot path.
        from app.backends.self_hosted import _resample

        audio = _resample(audio, rate, OPENVOICE_SAMPLE_RATE)
    if audio.size < int(MIN_CLIP_SECONDS * OPENVOICE_SAMPLE_RATE):
        raise CloneError(f"clip is too short; record at least {MIN_CLIP_SECONDS:.0f}s of speech")
    return audio


def extract_se(audio: np.ndarray, se_encoder_path: Path) -> np.ndarray:
    """Run the exported SE encoder over the clip → [1, 256, 1] embedding."""
    import onnxruntime as ort

    session = ort.InferenceSession(str(se_encoder_path), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    (se,) = session.run(None, {input_name: audio.reshape(1, -1).astype(np.float32)})
    return np.asarray(se, dtype=np.float32)


def make_model_id(label: str, clip: bytes) -> str:
    """Filesystem-safe, collision-resistant model id for a cloned voice."""
    slug = _SLUG_RE.sub("-", label).strip("-").lower() or "voice"
    digest = hashlib.sha256(clip).hexdigest()[:8]
    return f"ov2-{slug[:32]}-{digest}"


def clone_voice_local(clip: bytes, label: str, model_dir: str) -> str:
    """Full instant-clone pipeline; returns the new model id.

    Raises :class:`CloneError` with an operator-actionable message when the
    template artifacts are missing or the clip is unusable.
    """
    template_dir = Path(model_dir) / TEMPLATE_SUBDIR

    converter = template_dir / CONVERTER_TEMPLATE
    se_encoder = template_dir / SE_ENCODER
    if not converter.exists() or not se_encoder.exists():
        raise CloneError(
            f"OpenVoice template models not found in {template_dir}; "
            "run `uv run --group export python scripts/export_openvoice_onnx.py` first"
        )
    audio = decode_clip(clip)
    se = extract_se(audio, se_encoder)
    model_id = make_model_id(label, clip)
    out_path = Path(model_dir) / f"{model_id}.onnx"
    bake_tgt_se(converter, se, out_path)
    log.info("clone.voice_created", model=model_id, path=str(out_path), clip_samples=audio.size)
    return model_id
