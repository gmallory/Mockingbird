"""Torch-free HD (RVC) training pipeline for the self-hosted backend (M9).

Mirrors ``app.export.clone`` (the instant-clone pipeline) but for the HD tier
(PRODUCT_SPEC §4.2): a longer (10-30 minute) reference clip runs through
validation -> preprocessing -> feature_extraction -> training -> export,
reporting progress through each stage via ``progress_cb``, and produces a
per-voice ``{model_id}.onnx`` satisfying the same M5a streaming contract the
instant clone does — so the EXISTING ``self_hosted`` backend streams it
unchanged, no new streaming code needed.

The real RVC fine-tune (HuBERT content encoding + F0 + a synthesizer
gradient-descent-tuned on the target speaker, on a GPU) is M9's deferred
tail — see ``app.export.rvc`` (torch, ``export`` dependency group) and
``scripts/export_rvc_onnx.py``. Until that has produced a real exported
template, :func:`hd_train_local` writes a **contract-valid synthetic
stand-in**: a tiny deterministic ONNX graph derived from the clip's own
bytes, so different clips/voices still produce distinct (if not distinctly
*better*) models. This proves the whole train -> export -> stream loop
offline with no GPU; it is loudly logged as a stand-in, never presented as a
real fine-tune.
"""

import hashlib
import re
from collections.abc import Callable
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import structlog
from onnx import TensorProto, helper

from app.export.clone import bake_tgt_se, decode_clip
from app.export.rvc_templates import RVC_TEMPLATE

log = structlog.get_logger(__name__)

TEMPLATE_SUBDIR = "rvc"
SPEAKER_TENSOR_NAME = "speaker"
# Real usage is 10-30 minutes (PRODUCT_SPEC §4.2), but the floor here is kept
# low (unlike a hard 10-minute gate) so offline tests can exercise the full
# pipeline with a short synthetic clip instead of a multi-hundred-MB fixture;
# enforcing the full real-world minimum is a UI-level hint, not a hard gate.
MIN_HD_CLIP_SECONDS = 5.0
# Nominal preprocessing segment length, used only to derive a plausible
# sample_count for reporting/telemetry (PRODUCT_SPEC §6's VoiceModel shape).
SEGMENT_SECONDS = 10.0
DEFAULT_SAMPLE_RATE = 22050

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")

ProgressCB = Callable[[str, float], None]

# PRODUCT_SPEC §4.2 pipeline: Validation -> Preprocessing -> Feature
# Extraction -> Training -> Export. "ready" is a status transition the
# caller (the gateway's Celery task) applies once this returns; it is not a
# stage reported from in here.
_STAGES: tuple[tuple[str, float], ...] = (
    ("validation", 0.05),
    ("preprocessing", 0.20),
    ("feature_extraction", 0.40),
    ("training", 0.80),
    ("export", 0.98),
)


class HDTrainError(Exception):
    """HD training failed for a reason the caller should surface verbatim."""


def slugify(label: str) -> str:
    return _SLUG_RE.sub("-", label).strip("-").lower() or "voice"


def make_model_id(label: str, clip: bytes) -> str:
    """Filesystem-safe, collision-resistant model id for a trained HD voice."""
    digest = hashlib.sha256(clip).hexdigest()[:8]
    return f"rvc-{slugify(label)[:32]}-{digest}"


def _write_synthetic_graph(out_path: Path, clip: bytes) -> None:
    """A contract-valid audio-in/audio-out ONNX graph standing in for a real
    RVC fine-tune: ``audio_out = audio_in * gain``, ``gain`` derived from the
    clip's own hash so distinct clips/voices yield distinct (not distinctly
    *better*) models. Not a quality transform — see the module docstring.
    """
    digest = hashlib.sha256(clip).digest()
    raw = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    gain = 0.85 + raw * 0.30  # arbitrary small range, purely illustrative

    inp = helper.make_tensor_value_info("audio", TensorProto.FLOAT, [1, None])
    out = helper.make_tensor_value_info("out", TensorProto.FLOAT, [1, None])
    gain_const = helper.make_tensor("gain", TensorProto.FLOAT, [], [gain])
    node = helper.make_node("Mul", ["audio", "gain"], ["out"])
    graph = helper.make_graph([node], "rvc_synthetic", [inp], [out], initializer=[gain_const])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    onnx.save(model, str(out_path))


def _speaker_feature(audio: np.ndarray) -> np.ndarray:
    """Torch-free, per-clip conditioning scalar for the real composed graph.

    A crude spectral-centroid summary — NOT a real speaker embedding. Real HD
    quality comes from fine-tuning the synthesizer itself on the target
    speaker (see ``app.export.rvc.compose``'s docstring); this only lets the
    placeholder-weight composed graph vary its output per voice at all while
    no real fine-tune loop exists yet.
    """
    if audio.size == 0:
        return np.zeros((1, 1, 1), dtype=np.float32)
    spectrum = np.abs(np.fft.rfft(audio))
    total = spectrum.sum()
    if total <= 0:
        return np.zeros((1, 1, 1), dtype=np.float32)
    freqs = np.linspace(0.0, 1.0, spectrum.size, dtype=np.float64)
    centroid = float((freqs * spectrum).sum() / total)
    return np.array([[[centroid]]], dtype=np.float32)


def _verify_contract(path: Path, sample_rate: int) -> int:
    """Load the exported graph and run one dummy block through it.

    Returns the file size in bytes; raises :class:`HDTrainError` if the graph
    doesn't hold up the M5a streaming contract.
    """
    try:
        session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        inputs = session.get_inputs()
        if not inputs:
            raise HDTrainError("exported graph declares no inputs")
        feeds = {inputs[0].name: np.zeros((1, max(1, sample_rate // 10)), dtype=np.float32)}
        for extra in inputs[1:]:
            # Defensive only (a baked graph should have just the one "audio"
            # input): symbolic/unknown dims fall back to 1.
            shape = [d if isinstance(d, int) and d > 0 else 1 for d in extra.shape]
            feeds[extra.name] = np.zeros(shape, dtype=np.float32)
        (out,) = session.run(None, feeds)
    except HDTrainError:
        raise
    except Exception as exc:  # noqa: BLE001 - any ORT/export failure -> one clean error type
        raise HDTrainError(f"exported graph failed to load/run: {exc}") from exc
    out = np.asarray(out)
    if out.ndim != 2 or out.shape[0] != 1:
        raise HDTrainError(f"exported graph produced an unexpected output shape {out.shape}")
    if not np.isfinite(out).all():
        raise HDTrainError("exported graph produced non-finite audio")
    return path.stat().st_size


def hd_train_local(
    clip_bytes: bytes,
    label: str,
    model_dir: str,
    progress_cb: ProgressCB | None = None,
    sample_rate: int | None = None,
) -> dict:
    """Run the full HD training pipeline; returns the trained model's metadata.

    ``sample_rate`` should match ``SELF_HOSTED_MODEL_SAMPLE_RATE`` — one
    backend instance streams both instant and HD models, so they must share a
    sample rate. Raises :class:`HDTrainError` with an operator-actionable
    message on any validation/export failure. Returns
    ``{model_id, model_size_bytes, sample_duration_sec, sample_count, synthetic}``.
    """
    rate = sample_rate or DEFAULT_SAMPLE_RATE

    def report(idx: int) -> None:
        if progress_cb is not None:
            stage, fraction = _STAGES[idx]
            progress_cb(stage, fraction)

    report(0)  # validation
    if not clip_bytes:
        raise HDTrainError("clip is empty")
    try:
        audio = decode_clip(clip_bytes, target_rate=rate, min_seconds=MIN_HD_CLIP_SECONDS)
    except Exception as exc:  # noqa: BLE001 - CloneError + decode failures -> one error type
        raise HDTrainError(str(exc)) from exc
    duration_sec = audio.size / rate

    report(1)  # preprocessing
    sample_count = max(1, round(duration_sec / SEGMENT_SECONDS))

    report(2)  # feature_extraction
    # Real pipeline: HuBERT content units + F0 contour per segment (see
    # app/export/rvc/compose.py). Torch-free stand-in: a cheap spectral
    # summary, computed regardless of which export branch runs below so the
    # stage does real (if crude) work either way.
    speaker_feature = _speaker_feature(audio)

    report(3)  # training
    model_id = make_model_id(label, clip_bytes)
    out_path = Path(model_dir) / f"{model_id}.onnx"
    template_path = Path(model_dir) / TEMPLATE_SUBDIR / RVC_TEMPLATE
    synthetic = not template_path.exists()
    if synthetic:
        log.warning(
            "hd_train.synthetic_stand_in",
            model=model_id,
            template_path=str(template_path),
            note=(
                "no real RVC template found; run scripts/export_rvc_onnx.py "
                "(needs the export dependency group and, for production "
                "quality, real pretrained weights) on the GPU box. Writing a "
                "contract-valid synthetic stand-in so the pipeline is fully "
                "exercised offline."
            ),
        )
        _write_synthetic_graph(out_path, clip_bytes)
    else:
        bake_tgt_se(template_path, speaker_feature, out_path, tensor_name=SPEAKER_TENSOR_NAME)

    report(4)  # export
    size_bytes = _verify_contract(out_path, rate)
    log.info(
        "hd_train.trained",
        model=model_id,
        path=str(out_path),
        clip_samples=audio.size,
        sample_count=sample_count,
        synthetic=synthetic,
    )

    return {
        "model_id": model_id,
        "model_size_bytes": size_bytes,
        "sample_duration_sec": duration_sec,
        "sample_count": sample_count,
        "synthetic": synthetic,
    }
