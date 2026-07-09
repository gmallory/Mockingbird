"""RVC composed-graph export tests (M9).

Mirrors ``test_export_openvoice.py``'s posture: the real compose path needs
torch (``uv sync --group export``), so the whole module skips cleanly in a
service-only environment. Placeholder weights — proving the composed
HuBERT+F0+synthesizer graph really does export to one ONNX graph, load in
ONNX Runtime, and satisfy the M5a streaming contract with dynamic lengths
(see ``app/export/rvc/compose.py``'s module docstring for exactly which
pieces are real, exportable architecture vs. the deferred GPU fine-tune
quality tail). No network or GPU required.
"""

import io
import wave
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch", reason="export tests need the export dependency group")

import onnxruntime as ort  # noqa: E402

from app.export.clone import bake_tgt_se  # noqa: E402
from app.export.hd_train import TEMPLATE_SUBDIR, hd_train_local  # noqa: E402
from app.export.rvc.compose import SPEAKER_NAME, export_rvc  # noqa: E402
from app.export.rvc_templates import RVC_TEMPLATE  # noqa: E402

_SR = 22050


def _session(path: Path) -> ort.InferenceSession:
    return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])


def _sine(seconds: float, rate: int = _SR) -> np.ndarray:
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _wav_bytes(audio: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        pcm = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


@pytest.fixture(scope="module")
def template_path(tmp_path_factory) -> Path:
    """Export once for the whole module (the slow bit: a real torch.onnx.export)."""
    tmp = tmp_path_factory.mktemp("rvc")
    return export_rvc(tmp / RVC_TEMPLATE)


# ----- exported template satisfies the M5a contract --------------------------


def test_export_rvc_writes_the_named_template(template_path):
    assert template_path.exists()
    assert template_path.name == RVC_TEMPLATE


def test_template_takes_only_audio_and_is_deterministic(template_path):
    """export_rvc's own final step already bakes a zero ``speaker``
    initializer (mirroring the OpenVoice converter template), so the
    returned template is audio-only — ready for bake_tgt_se to re-patch a
    real per-clip speaker feature onto a fresh copy (app.export.hd_train).
    """
    sess = _session(template_path)
    assert [i.name for i in sess.get_inputs()] == ["audio"]

    audio = np.random.default_rng(0).uniform(-0.3, 0.3, (1, _SR)).astype(np.float32)
    (out1,) = sess.run(None, {"audio": audio})
    (out2,) = sess.run(None, {"audio": audio})
    assert out1.ndim == 2 and out1.shape[0] == 1
    assert np.isfinite(out1).all()
    assert np.array_equal(out1, out2), "graph must be deterministic (no sampling ops)"


def test_template_handles_dynamic_lengths(template_path):
    """Dynamic axes: the streaming block+context window can be any length."""
    sess = _session(template_path)
    for seconds in (0.26, 0.5, 1.0, 2.0):
        n = int(_SR * seconds)
        audio = np.zeros((1, n), dtype=np.float32)
        (out,) = sess.run(None, {"audio": audio})
        assert out.ndim == 2 and out.shape[0] == 1
        assert np.isfinite(out).all()


def test_speaker_conditioning_is_patchable_via_bake_tgt_se(template_path, tmp_path):
    """Re-baking a non-zero speaker value must change the conversion — proves
    the composed graph's conditioning path is live, not dead weight."""
    audio = np.random.default_rng(0).uniform(-0.3, 0.3, (1, _SR)).astype(np.float32)
    (base,) = _session(template_path).run(None, {"audio": audio})

    patched_path = tmp_path / "patched.onnx"
    rng = np.random.default_rng(3)
    bake_tgt_se(
        template_path,
        rng.normal(0, 1, (1, 1, 1)).astype(np.float32),
        patched_path,
        tensor_name=SPEAKER_NAME,
    )
    patched = _session(patched_path)
    assert [i.name for i in patched.get_inputs()] == ["audio"]
    (out,) = patched.run(None, {"audio": audio})
    assert out.shape == base.shape
    assert not np.allclose(base, out), "speaker initializer must steer the output"


# ----- torch-free HD pipeline picks up a real template when present ---------


def test_hd_train_local_uses_real_template_when_present(template_path, tmp_path):
    """Once scripts/export_rvc_onnx.py has produced a real rvc_converter.onnx
    (here: the placeholder-weight template), hd_train_local must bake into
    it instead of falling back to the synthetic stand-in."""
    model_dir = tmp_path / "models"
    rvc_dir = model_dir / TEMPLATE_SUBDIR
    rvc_dir.mkdir(parents=True)
    (rvc_dir / RVC_TEMPLATE).write_bytes(template_path.read_bytes())

    clip = _wav_bytes(_sine(6.0), _SR)  # comfortably above MIN_HD_CLIP_SECONDS (5s)
    result = hd_train_local(clip, "Real Template Voice", str(model_dir))
    assert result["synthetic"] is False
    assert (model_dir / f"{result['model_id']}.onnx").exists()
