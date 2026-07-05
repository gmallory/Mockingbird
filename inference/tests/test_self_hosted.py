"""Self-hosted (ONNX Runtime) backend tests.

Real voice-conversion weights are large and GPU-bound, so these tests exercise
the streaming engine against tiny ONNX graphs built on the fly with the model
contract the backend documents: float32 audio in ``[1, N]`` -> float32 audio
out. Identity and gain (x0.5) graphs prove the audio actually flows through
ONNX Runtime; the rest covers blocking, resampling, error posture, and the
model cache. No GPU or network required.
"""

import struct
from pathlib import Path

import numpy as np
import onnx
import pytest
from onnx import TensorProto, helper

from app.backends.self_hosted import (
    SelfHostedBackend,
    _resample,
    pick_providers,
)

_FRAME_SAMPLES = 960  # 20ms @ 48kHz
_FRAME_BYTES = _FRAME_SAMPLES * 2


def _frame(value: int, n: int = _FRAME_SAMPLES) -> bytes:
    return struct.pack(f"<{n}h", *([value] * n))


def _write_gain_model(path, gain: float) -> None:
    """audio_out = audio_in * gain — the simplest graph honoring the contract."""
    inp = helper.make_tensor_value_info("audio", TensorProto.FLOAT, [1, None])
    out = helper.make_tensor_value_info("out", TensorProto.FLOAT, [1, None])
    gain_const = helper.make_tensor("gain", TensorProto.FLOAT, [], [gain])
    node = helper.make_node("Mul", ["audio", "gain"], ["out"])
    graph = helper.make_graph([node], "gain", [inp], [out], initializer=[gain_const])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    onnx.save(model, str(path))


@pytest.fixture
def model_dir(tmp_path):
    _write_gain_model(tmp_path / "identity.onnx", 1.0)
    _write_gain_model(tmp_path / "halver.onnx", 0.5)
    return tmp_path


def _backend(model_dir, **overrides) -> SelfHostedBackend:
    params = {
        "model_dir": str(model_dir),
        "default_model": "identity",
        "device": "cpu",
        "frame_ms": 20,
        "block_ms": 100,  # 5 frames per block
        "context_ms": 40,
        "max_loaded_models": 4,
    }
    params.update(overrides)
    return SelfHostedBackend(**params)


# ----- helpers ---------------------------------------------------------------


def test_resample_preserves_duration_proportionally():
    audio = np.sin(np.linspace(0, 20, 48000, dtype=np.float32))
    down = _resample(audio, 48000, 16000)
    assert down.size == 16000
    back = _resample(down, 16000, 48000)
    assert back.size == 48000


def test_pick_providers_auto_prefers_gpu_then_falls_back():
    available = ["CUDAExecutionProvider", "CoreMLExecutionProvider", "CPUExecutionProvider"]
    assert pick_providers("auto", available)[0] == "CUDAExecutionProvider"
    assert pick_providers("auto", ["CoreMLExecutionProvider", "CPUExecutionProvider"])[0] == (
        "CoreMLExecutionProvider"
    )
    assert pick_providers("auto", ["CPUExecutionProvider"]) == ["CPUExecutionProvider"]
    # Requesting an unavailable device degrades to CPU instead of crashing.
    assert pick_providers("cuda", ["CPUExecutionProvider"]) == ["CPUExecutionProvider"]


# ----- streaming session -----------------------------------------------------


async def test_streams_blocks_not_utterances(model_dir):
    """Output appears every block (100ms), not after end-of-speech."""
    backend = _backend(model_dir)
    session = backend.open_session()
    loud = _frame(8000)

    for _ in range(4):
        assert await session.push(loud, 48000, "identity") == []
    out = await session.push(loud, 48000, "identity")  # 5th frame completes the block

    assert len(out) == 5
    assert all(len(f) == _FRAME_BYTES for f in out)


async def test_total_output_matches_total_input(model_dir):
    backend = _backend(model_dir)
    session = backend.open_session()
    frames_in = 12  # 2 full blocks + 2 leftover frames drained by flush
    out = []
    for _ in range(frames_in):
        out += await session.push(_frame(8000), 48000, "identity")
    out += await session.flush()
    assert len(out) == frames_in
    assert all(len(f) == _FRAME_BYTES for f in out)


async def test_model_switch_flushes_partial_block_under_old_voice(model_dir):
    """Frames buffered before a switch_model must convert with the old voice."""
    backend = _backend(model_dir)
    session = backend.open_session()

    out = []
    for _ in range(3):
        out += await session.push(_frame(8000), 48000, "halver")
    assert out == []  # partial block still buffered
    # Switch voices mid-block: the 3 buffered frames flush under "halver".
    out += await session.push(_frame(8000), 48000, "identity")
    out += await session.flush()

    assert len(out) == 4
    halved = np.frombuffer(b"".join(out[:3]), dtype=np.int16)
    passed = np.frombuffer(out[3], dtype=np.int16)
    assert 3500 <= np.abs(halved).max() <= 4500, "pre-switch frames must use the old voice"
    assert 7500 <= np.abs(passed).max() <= 8500, "post-switch frame must use the new voice"


async def test_block_frames_never_rounds_below_block_ms(model_dir):
    """90ms/20ms must give 5 frames (100ms), not banker's-round to 4 (80ms)."""
    backend = _backend(model_dir, block_ms=90)
    assert backend.block_frames == 5


async def test_audio_flows_through_the_onnx_model(model_dir):
    """The halver model must actually halve amplitude — proves ORT ran."""
    backend = _backend(model_dir)
    session = backend.open_session()
    out = []
    for _ in range(5):
        out += await session.push(_frame(8000), 48000, "halver")
    samples = np.frombuffer(b"".join(out), dtype=np.int16)
    peak = np.abs(samples).max()
    assert 3500 <= peak <= 4500, f"expected ~4000 after x0.5 gain, got {peak}"


async def test_resampled_model_rate_preserves_frame_count(model_dir):
    backend = _backend(model_dir, model_sample_rate=16000)
    session = backend.open_session()
    out = []
    for _ in range(10):
        out += await session.push(_frame(8000), 48000, "identity")
    out += await session.flush()
    assert len(out) == 10
    assert all(len(f) == _FRAME_BYTES for f in out)


async def test_default_model_used_when_frame_has_none(model_dir):
    backend = _backend(model_dir, default_model="halver")
    session = backend.open_session()
    out = []
    for _ in range(5):
        out += await session.push(_frame(8000), 48000, "")
    peak = np.abs(np.frombuffer(b"".join(out), dtype=np.int16)).max()
    assert peak < 5000  # halved -> the default model ran


# ----- error posture: degrade, never crash -----------------------------------


async def test_missing_model_passes_audio_through(model_dir):
    backend = _backend(model_dir, default_model="")
    session = backend.open_session()
    out = []
    for _ in range(5):
        out += await session.push(_frame(8000), 48000, "no-such-model")
    assert len(out) == 5
    assert out[0] == _frame(8000)  # unchanged


async def test_path_traversal_model_id_rejected(model_dir):
    backend = _backend(model_dir, default_model="")
    session = backend.open_session()
    out = []
    for _ in range(5):
        out += await session.push(_frame(8000), 48000, "../../etc/passwd")
    assert len(out) == 5
    assert out[0] == _frame(8000)


async def test_corrupt_model_file_passes_audio_through(model_dir):
    (model_dir / "broken.onnx").write_bytes(b"not an onnx file")
    backend = _backend(model_dir, default_model="")
    session = backend.open_session()
    out = []
    for _ in range(5):
        out += await session.push(_frame(8000), 48000, "broken")
    assert len(out) == 5
    assert out[0] == _frame(8000)


# ----- model cache -----------------------------------------------------------


async def test_lru_evicts_oldest_model(model_dir):
    backend = _backend(model_dir, max_loaded_models=1)
    assert await backend._get_session("identity") is not None
    assert await backend._get_session("halver") is not None
    assert list(backend._sessions) == ["halver"]


async def test_cached_session_is_reused(model_dir):
    backend = _backend(model_dir)
    first = await backend._get_session("identity")
    second = await backend._get_session("identity")
    assert first is second


async def test_flush_emits_subframe_residue(model_dir):
    """A leftover <20ms tail in the output buffer must not be dropped at end of stream."""
    backend = _backend(model_dir)
    session = backend.open_session()
    for _ in range(5):
        await session.push(_frame(8000), 48000, "identity")
    session._out += b"\x11\x22" * 10  # simulate a non-proportional model leaving 10 samples
    out = await session.flush()
    assert len(out) == 1
    assert len(out[0]) == _FRAME_BYTES
    assert out[0].startswith(b"\x11\x22" * 10)
    assert out[0].endswith(b"\x00\x00")


async def test_failed_model_is_negative_cached(model_dir, monkeypatch):
    """A model that failed to resolve must not be re-attempted every block."""
    backend = _backend(model_dir, s3_bucket="models-bucket", default_model="")
    attempts = {"n": 0}

    def _always_fail(model_id, dest):
        attempts["n"] += 1
        raise RuntimeError("no such key")

    monkeypatch.setattr(backend, "_download_from_s3", _always_fail)
    assert await backend._get_session("ghost") is None
    assert await backend._get_session("ghost") is None  # served by negative cache
    assert attempts["n"] == 1
    backend._failed_at["ghost"] -= backend._failed_retry_s + 1  # cooldown elapsed
    assert await backend._get_session("ghost") is None
    assert attempts["n"] == 2  # retried after cooldown


# ----- S3 fetch --------------------------------------------------------------


async def test_missing_model_downloaded_from_s3(model_dir, monkeypatch):
    """Exercises the real boto3 wiring: endpoint, bucket, key format, dest mkdir."""
    import boto3

    calls = {}

    class _FakeS3Client:
        def download_file(self, bucket, key, dest):
            calls["bucket"], calls["key"] = bucket, key
            _write_gain_model(Path(dest), 0.5)

    def _fake_client(service, endpoint_url=None):
        calls["service"], calls["endpoint"] = service, endpoint_url
        return _FakeS3Client()

    monkeypatch.setattr(boto3, "client", _fake_client)
    nested = model_dir / "not-yet-created"  # download must mkdir the model dir
    backend = _backend(
        nested, s3_bucket="models-bucket", s3_endpoint="http://localhost:9000", default_model=""
    )
    session = backend.open_session()
    out = []
    for _ in range(5):
        out += await session.push(_frame(8000), 48000, "cloud-voice")
    assert calls == {
        "service": "s3",
        "endpoint": "http://localhost:9000",
        "bucket": "models-bucket",
        "key": "models/cloud-voice.onnx",
    }
    peak = np.abs(np.frombuffer(b"".join(out), dtype=np.int16)).max()
    assert peak < 5000  # downloaded halver model ran
