"""HD (RVC) training pipeline tests (M9).

``hd_train_local`` is the torch-free training path exercised here: no real RVC
fine-tune exists yet (that GPU tail is deferred — see ``app.export.rvc`` +
``scripts/export_rvc_onnx.py``, covered separately in ``test_export_rvc.py``
behind the export dependency group), so absent a real exported template it
writes a contract-valid *synthetic* stand-in graph — the same one-node
``Mul``-gain technique ``test_self_hosted.py``'s ``_write_gain_model`` uses —
proving the whole train -> export -> stream loop offline with no GPU. The
route section below drives ``POST /train_hd`` the same way
``test_voices.py`` drives ``POST /voices``. No GPU or network required.
"""

import io
import shutil
import struct
import subprocess
import wave

import httpx
import numpy as np
import onnxruntime as ort
import pytest
from fastapi import FastAPI

from app import training as training_mod
from app.export.hd_train import (
    MIN_HD_CLIP_SECONDS,
    HDTrainError,
    hd_train_local,
    make_model_id,
    slugify,
)

_CLIP_SECONDS = MIN_HD_CLIP_SECONDS + 1.0  # comfortably above the training floor
_DEFAULT_RATE = 22050


def _sine(seconds: float, rate: int = _DEFAULT_RATE, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _wav_bytes(audio: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        pcm = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


def _clip(seconds: float = _CLIP_SECONDS, rate: int = _DEFAULT_RATE, freq: float = 220.0) -> bytes:
    return _wav_bytes(_sine(seconds, rate, freq), rate)


# ----- hd_train_local: synthetic stand-in (no real RVC template) -------------


def test_hd_train_local_writes_contract_valid_synthetic_model(tmp_path):
    """No rvc_converter.onnx template in the model dir -> the synthetic branch
    writes a graph that loads in ORT and satisfies the M5a contract: float32
    ``[1, N]`` in -> float32 ``[1, M]`` out.
    """
    result = hd_train_local(_clip(), "My Voice", str(tmp_path))
    out_path = tmp_path / f"{result['model_id']}.onnx"

    assert result["synthetic"] is True
    assert out_path.exists()
    assert result["model_size_bytes"] == out_path.stat().st_size
    assert result["sample_count"] >= 1
    assert result["sample_duration_sec"] == pytest.approx(_CLIP_SECONDS, abs=0.01)

    session = ort.InferenceSession(str(out_path), providers=["CPUExecutionProvider"])
    inputs = session.get_inputs()
    assert len(inputs) == 1
    assert inputs[0].type == "tensor(float)"

    frame = np.random.default_rng(0).uniform(-0.5, 0.5, (1, 4410)).astype(np.float32)
    (out,) = session.run(None, {inputs[0].name: frame})
    assert out.ndim == 2 and out.shape[0] == 1
    assert np.isfinite(out).all()


def test_hd_train_local_distinct_clips_yield_distinct_models(tmp_path):
    """Different clips (even for the same label) must mint different model
    ids and different synthetic gains — the stand-in is per-clip, not fixed.
    """
    a = hd_train_local(_clip(freq=220.0), "Voice A", str(tmp_path))
    b = hd_train_local(_clip(freq=440.0), "Voice A", str(tmp_path))
    assert a["model_id"] != b["model_id"]
    assert (tmp_path / f"{a['model_id']}.onnx").exists()
    assert (tmp_path / f"{b['model_id']}.onnx").exists()


def test_hd_train_local_reports_every_stage_in_order(tmp_path):
    seen: list[tuple[str, float]] = []
    hd_train_local(
        _clip(), "V", str(tmp_path), progress_cb=lambda stage, frac: seen.append((stage, frac))
    )
    assert [stage for stage, _ in seen] == [
        "validation",
        "preprocessing",
        "feature_extraction",
        "training",
        "export",
    ]
    fractions = [frac for _, frac in seen]
    assert fractions == sorted(fractions), "progress must be monotonically increasing"
    assert fractions[-1] == 0.98


def test_hd_train_local_rejects_empty_clip(tmp_path):
    with pytest.raises(HDTrainError, match="empty"):
        hd_train_local(b"", "V", str(tmp_path))


def test_hd_train_local_rejects_too_short_clip(tmp_path):
    """The documented error: below MIN_HD_CLIP_SECONDS (5s), not OpenVoice's
    much lower 1s instant-clone floor."""
    with pytest.raises(HDTrainError, match="too short"):
        hd_train_local(_clip(seconds=1.0), "V", str(tmp_path))


def test_hd_train_local_honors_custom_sample_rate(tmp_path):
    """sample_rate must match SELF_HOSTED_MODEL_SAMPLE_RATE — one backend
    instance streams both instant and HD models."""
    result = hd_train_local(_clip(rate=16000), "V", str(tmp_path), sample_rate=16000)
    assert result["sample_duration_sec"] == pytest.approx(_CLIP_SECONDS, abs=0.01)

    session = ort.InferenceSession(
        str(tmp_path / f"{result['model_id']}.onnx"), providers=["CPUExecutionProvider"]
    )
    frame = np.zeros((1, 1600), dtype=np.float32)
    (out,) = session.run(None, {session.get_inputs()[0].name: frame})
    assert np.isfinite(out).all()


def test_hd_train_local_decodes_non_wav_via_ffmpeg(tmp_path):
    """Browser recordings arrive as webm/opus; ffmpeg does the transcode."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    wav_path = tmp_path / "src.wav"
    wav_path.write_bytes(_clip())
    ogg_path = tmp_path / "src.ogg"
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav_path), str(ogg_path)],
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode()

    out_dir = tmp_path / "models"
    out_dir.mkdir()
    result = hd_train_local(ogg_path.read_bytes(), "V", str(out_dir))
    assert (out_dir / f"{result['model_id']}.onnx").exists()


def test_make_model_id_is_backend_safe():
    from app.backends.self_hosted import _MODEL_ID_RE

    for label in ("My Voice!", "../../etc/passwd", "émile", ""):
        assert _MODEL_ID_RE.match(make_model_id(label, b"clip-bytes"))
    assert make_model_id("x", b"a").startswith("rvc-")


def test_slugify_handles_edge_cases():
    assert slugify("") == "voice"
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("../../etc/passwd") == "etc-passwd"


# ----- end-to-end mini: trained model streams through self_hosted -----------


async def test_hd_trained_model_streams_through_self_hosted_backend(tmp_path):
    """The produced synthetic model must load and convert with the same 1:1
    sample cadence as any other self_hosted model — proves the HD tier needs
    no new streaming code (same contract-conformance proof as
    test_export_openvoice.py's test_cloned_voice_streams_through_backend).
    """
    from app.backends.self_hosted import SelfHostedBackend

    result = hd_train_local(_clip(), "Stream Test", str(tmp_path))
    model_id = result["model_id"]

    backend = SelfHostedBackend(
        model_dir=str(tmp_path),
        default_model="",
        model_sample_rate=_DEFAULT_RATE,
        device="cpu",
        block_ms=100,
        context_ms=100,
    )
    session = backend.open_session()
    frame = struct.pack("<960h", *([2000] * 960))  # 20ms @ 48kHz
    out = []
    for _ in range(10):
        out += await session.push(frame, 48000, model_id)
    out += await session.flush()
    total = sum(len(f) for f in out) // 2
    assert total == 10 * 960, "conversion must preserve the stream's sample count"
    converted = np.frombuffer(b"".join(out), dtype=np.int16)
    assert np.isfinite(converted.astype(np.float32)).all()


# ----- POST /train_hd route --------------------------------------------------

_app = FastAPI()
_app.include_router(training_mod.router)


async def _post_train_hd(clip: bytes, name: str = "Route Voice") -> httpx.Response:
    transport = httpx.ASGITransport(app=_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/train_hd",
            files={"clip": ("clip.wav", clip, "audio/wav")},
            data={"name": name},
        )


async def test_train_hd_route_returns_model_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(training_mod.settings, "inference_backend", "self_hosted")
    monkeypatch.setattr(training_mod.settings, "self_hosted_model_dir", str(tmp_path))

    resp = await _post_train_hd(_clip())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["voice_id"] == body["model_id"]
    assert (tmp_path / f"{body['model_id']}.onnx").exists()
    assert body["model_size_bytes"] > 0
    assert body["sample_duration_sec"] == pytest.approx(_CLIP_SECONDS, abs=0.01)
    assert body["synthetic"] is True


async def test_train_hd_route_cloud_gpu_backend_also_allowed(monkeypatch, tmp_path):
    monkeypatch.setattr(training_mod.settings, "inference_backend", "cloud_gpu")
    monkeypatch.setattr(training_mod.settings, "self_hosted_model_dir", str(tmp_path))

    resp = await _post_train_hd(_clip())
    assert resp.status_code == 200


async def test_train_hd_route_rejects_empty_clip(monkeypatch, tmp_path):
    monkeypatch.setattr(training_mod.settings, "inference_backend", "self_hosted")
    monkeypatch.setattr(training_mod.settings, "self_hosted_model_dir", str(tmp_path))

    resp = await _post_train_hd(b"")
    assert resp.status_code == 400


async def test_train_hd_route_rejects_oversized_clip(monkeypatch, tmp_path):
    monkeypatch.setattr(training_mod.settings, "inference_backend", "self_hosted")
    monkeypatch.setattr(training_mod.settings, "self_hosted_model_dir", str(tmp_path))
    monkeypatch.setattr(training_mod.settings, "max_hd_clip_bytes", 8)

    resp = await _post_train_hd(b"x" * 9)
    assert resp.status_code == 413


async def test_train_hd_route_degrades_when_backend_unsupported(monkeypatch):
    """Neither self_hosted nor cloud_gpu -> a clean 400, matching POST /voices."""
    monkeypatch.setattr(training_mod.settings, "inference_backend", "cartesia")

    resp = await _post_train_hd(_clip())
    assert resp.status_code == 400
    assert "self_hosted|cloud_gpu" in resp.json()["detail"]


async def test_train_hd_route_surfaces_hd_train_error_as_400(monkeypatch, tmp_path):
    monkeypatch.setattr(training_mod.settings, "inference_backend", "self_hosted")
    monkeypatch.setattr(training_mod.settings, "self_hosted_model_dir", str(tmp_path))

    resp = await _post_train_hd(_clip(seconds=1.0))  # below MIN_HD_CLIP_SECONDS
    assert resp.status_code == 400
    assert "too short" in resp.json()["detail"]
