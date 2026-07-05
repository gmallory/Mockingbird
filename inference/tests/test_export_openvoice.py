"""OpenVoice ONNX export + instant-clone tests (M5b).

The real converter checkpoint is 131MB, so these tests export a **tiny
randomly-initialized** converter with the same architecture through the real
torch → ONNX pipeline, then drive it with ONNX Runtime: contract shape,
determinism, SE patching, clip decoding, and the full clone → stream path.

Needs torch (``uv sync --group export``); the whole module skips cleanly in a
service-only environment. No network or GPU required.
"""

import io
import json
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch", reason="export tests need the export dependency group")

import onnxruntime as ort  # noqa: E402

from app.export.clone import (  # noqa: E402
    CloneError,
    bake_tgt_se,
    clone_voice_local,
    decode_clip,
    make_model_id,
)
from app.export.openvoice.models import SynthesizerTrn  # noqa: E402
from app.export.openvoice_onnx import export_openvoice  # noqa: E402
from app.export.openvoice_templates import CONVERTER_TEMPLATE, SE_ENCODER  # noqa: E402

# Same architecture as the published converter config, tiny dimensions. The
# model sample rate is 16000 (real one is 22050) — the contract is rate-agnostic.
TINY_SR = 16000
TINY_CONFIG = {
    "_version_": "v2",
    "data": {
        "sampling_rate": TINY_SR,
        "filter_length": 128,
        "hop_length": 32,
        "win_length": 128,
        "n_speakers": 0,
    },
    "model": {
        "zero_g": True,
        "inter_channels": 32,
        "hidden_channels": 32,
        "filter_channels": 64,
        "n_heads": 2,
        "n_layers": 2,
        "kernel_size": 3,
        "p_dropout": 0.0,
        "resblock": "1",
        "resblock_kernel_sizes": [3],
        "resblock_dilation_sizes": [[1, 3, 5]],
        "upsample_rates": [4, 4, 2],
        "upsample_initial_channel": 64,
        "upsample_kernel_sizes": [8, 8, 4],
        "gin_channels": 256,
    },
}


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory) -> Path:
    """Export a tiny converter once for the whole module (the slow bit)."""
    tmp = tmp_path_factory.mktemp("openvoice")
    config = tmp / "config.json"
    config.write_text(json.dumps(TINY_CONFIG))

    torch.manual_seed(0)
    model = SynthesizerTrn(
        0, TINY_CONFIG["data"]["filter_length"] // 2 + 1, n_speakers=0, **TINY_CONFIG["model"]
    )
    # The coupling layers' post conv is zero-initialized (identity flow); give
    # it weights so the speaker-conditioning path is live like a trained model.
    with torch.no_grad():
        for layer in model.flow.flows[::2]:
            layer.post.weight.normal_(0, 0.05)
    checkpoint = tmp / "checkpoint.pth"
    torch.save({"model": model.state_dict()}, checkpoint)

    export_openvoice(config, checkpoint, tmp / "models" / "openvoice")
    return tmp / "models"


def _sine(seconds: float, rate: int = TINY_SR) -> np.ndarray:
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _wav_bytes(audio: np.ndarray, rate: int, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        pcm = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
        if channels > 1:
            pcm = np.repeat(pcm[:, None], channels, axis=1).reshape(-1)
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


def _session(path: Path) -> ort.InferenceSession:
    return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])


# ----- exported artifacts satisfy the M5a model contract ----------------------


def test_converter_takes_only_audio_and_is_deterministic(model_dir):
    sess = _session(model_dir / "openvoice" / CONVERTER_TEMPLATE)
    assert [i.name for i in sess.get_inputs()] == ["audio"]

    audio = _sine(1.0).reshape(1, -1)
    (out1,) = sess.run(None, {"audio": audio})
    (out2,) = sess.run(None, {"audio": audio})
    assert out1.ndim == 2 and out1.shape[0] == 1
    assert np.isfinite(out1).all()
    assert np.array_equal(out1, out2), "graph must be deterministic (no sampling ops)"


def test_converter_handles_variable_lengths(model_dir):
    """Dynamic axes: block+context windows of any size map through 1:1-ish."""
    sess = _session(model_dir / "openvoice" / CONVERTER_TEMPLATE)
    hop = TINY_CONFIG["data"]["hop_length"]
    for seconds in (0.26, 0.5, 1.0):  # 0.26 ≈ the streaming block+context window
        n = int(TINY_SR * seconds)
        (out,) = sess.run(None, {"audio": _sine(seconds).reshape(1, -1)})
        assert abs(out.shape[1] - n) <= hop, f"{n} in -> {out.shape[1]} out"


def test_se_encoder_shape(model_dir):
    sess = _session(model_dir / "openvoice" / SE_ENCODER)
    (se,) = sess.run(None, {"audio": _sine(1.0).reshape(1, -1)})
    assert se.shape == (1, 256, 1)
    assert np.isfinite(se).all()


def test_baked_tgt_se_changes_output(model_dir, tmp_path):
    """Patching a different speaker embedding must change the conversion."""
    template = model_dir / "openvoice" / CONVERTER_TEMPLATE
    audio = _sine(0.5).reshape(1, -1)
    (base,) = _session(template).run(None, {"audio": audio})

    patched_path = tmp_path / "patched.onnx"
    rng = np.random.default_rng(7)
    bake_tgt_se(template, rng.normal(0, 1, (1, 256, 1)).astype(np.float32), patched_path)
    patched = _session(patched_path)
    assert [i.name for i in patched.get_inputs()] == ["audio"]
    (out,) = patched.run(None, {"audio": audio})
    assert out.shape == base.shape
    assert not np.allclose(base, out), "tgt_se initializer must steer the output"


# ----- clip decoding ----------------------------------------------------------


def test_decode_clip_wav_stereo_resamples_to_openvoice_rate():
    from app.export.clone import OPENVOICE_SAMPLE_RATE

    audio = decode_clip(_wav_bytes(_sine(2.0, rate=48000), 48000, channels=2))
    assert audio.dtype == np.float32
    assert abs(audio.size - 2 * OPENVOICE_SAMPLE_RATE) <= 1
    assert np.abs(audio).max() <= 1.0


def test_decode_clip_rejects_too_short():
    with pytest.raises(CloneError, match="too short"):
        decode_clip(_wav_bytes(_sine(0.2), TINY_SR))


def test_make_model_id_is_backend_safe():
    from app.backends.self_hosted import _MODEL_ID_RE

    for label in ("My Voice!", "../../etc/passwd", "émile", ""):
        assert _MODEL_ID_RE.match(make_model_id(label, b"clip-bytes"))


# ----- instant clone -> streaming backend, end to end -------------------------


async def test_cloned_voice_streams_through_backend(model_dir):
    """clone_voice_local output must load and convert in the real session."""
    from app.backends.self_hosted import SelfHostedBackend

    clip = _wav_bytes(_sine(1.5), TINY_SR)
    model_id = clone_voice_local(clip, "Test Voice", str(model_dir))
    assert (model_dir / f"{model_id}.onnx").exists()

    backend = SelfHostedBackend(
        model_dir=str(model_dir),
        default_model="",
        model_sample_rate=TINY_SR,
        device="cpu",
        block_ms=100,
        context_ms=100,
    )
    session = backend.open_session()
    frame = struct.pack("<960h", *([2000] * 960))  # 20ms @48kHz
    out = []
    for _ in range(10):
        out += await session.push(frame, 48000, model_id)
    out += await session.flush()
    total = sum(len(f) for f in out) // 2
    assert total == 10 * 960, "conversion must preserve the stream's sample count"
    converted = np.frombuffer(b"".join(out), dtype=np.int16)
    assert np.abs(converted).sum() > 0, "model output should be non-silent"
    # Passthrough would return the input exactly; the model must have run.
    assert not np.array_equal(converted, np.frombuffer(frame * 10, dtype=np.int16))


def test_clone_without_templates_gives_actionable_error(tmp_path):
    with pytest.raises(CloneError, match="export_openvoice_onnx"):
        clone_voice_local(_wav_bytes(_sine(1.5), TINY_SR), "v", str(tmp_path))
