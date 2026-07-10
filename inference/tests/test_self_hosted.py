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
from app.tuning import TuneParams

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
        "crossfade_ms": 0,  # seam blending off: keeps M5a cadence/exactness tests literal
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


def _write_truncating_model(path, drop: int) -> None:
    """audio_out = audio_in[:, :-drop] — models an STFT partial-hop truncation."""
    inp = helper.make_tensor_value_info("audio", TensorProto.FLOAT, [1, None])
    out = helper.make_tensor_value_info("out", TensorProto.FLOAT, [1, None])
    starts = helper.make_tensor("starts", TensorProto.INT64, [1], [0])
    ends = helper.make_tensor("ends", TensorProto.INT64, [1], [-drop])
    axes = helper.make_tensor("axes", TensorProto.INT64, [1], [1])
    node = helper.make_node("Slice", ["audio", "starts", "ends", "axes"], ["out"])
    graph = helper.make_graph([node], "trunc", [inp], [out], initializer=[starts, ends, axes])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    onnx.save(model, str(path))


async def test_hop_truncating_model_does_not_compress_time(model_dir):
    """A model that drops <25ms per window must still yield a 1:1 stream.

    Without the deficit rule, a constant per-window truncation (OpenVoice
    drops a partial STFT hop) becomes a few percent time compression on every
    block — hundreds of ms lost over a call.
    """
    _write_truncating_model(model_dir / "trunc.onnx", drop=500)  # ~10ms @48kHz
    backend = _backend(model_dir)
    session = backend.open_session()
    out = []
    for _ in range(20):  # 4 blocks
        out += await session.push(_frame(8000), 48000, "trunc")
    out += await session.flush()
    assert sum(len(f) for f in out) // 2 == 20 * _FRAME_SAMPLES


# ----- seam crossfade (M5b) ---------------------------------------------------


async def test_crossfade_preserves_total_sample_count(model_dir):
    """Holdback shifts emission by crossfade_ms but flush releases every sample."""
    backend = _backend(model_dir, crossfade_ms=5)  # 240 samples @48kHz
    session = backend.open_session()
    out = []
    for _ in range(12):
        out += await session.push(_frame(8000), 48000, "identity")
    out += await session.flush()
    total = sum(len(f) for f in out) // 2
    assert total == 12 * _FRAME_SAMPLES
    assert all(len(f) == _FRAME_BYTES for f in out)


async def test_crossfade_blends_the_seam(model_dir):
    """A gain step between blocks must ramp across the seam, not jump."""
    backend = _backend(model_dir, crossfade_ms=5, context_ms=40)
    session = backend.open_session()
    out = []
    # Block 1 under identity (8000), block 2 under halver (4000 from the same
    # input): the seam between them must pass through intermediate values.
    for _ in range(5):
        out += await session.push(_frame(8000), 48000, "identity")
    for _ in range(5):
        out += await session.push(_frame(8000), 48000, "halver")
    out += await session.flush()
    samples = np.abs(np.frombuffer(b"".join(out), dtype=np.int16))
    seam = samples[(samples > 4500) & (samples < 7500)]
    assert seam.size > 0, "expected blended samples between 4000 and 8000 at the seam"
    # The blend region is bounded by the crossfade length.
    assert seam.size <= int(48000 * 5 / 1000)


async def test_crossfade_zero_is_bit_exact_passthrough(model_dir):
    """crossfade_ms=0 keeps the M5a exact int16 round-trip guarantee."""
    backend = _backend(model_dir, crossfade_ms=0)
    session = backend.open_session()
    out = []
    for _ in range(5):
        out += await session.push(_frame(8000), 48000, "identity")
    assert b"".join(out) == _frame(8000) * 5


async def test_crossfade_degraded_block_loses_no_audio(model_dir):
    """Model disappearing mid-stream: held tail still emitted, count preserved."""
    backend = _backend(model_dir, crossfade_ms=5, default_model="")
    session = backend.open_session()
    out = []
    for _ in range(5):
        out += await session.push(_frame(8000), 48000, "identity")
    for _ in range(5):
        out += await session.push(_frame(8000), 48000, "no-such-model")  # degrades
    out += await session.flush()
    total = sum(len(f) for f in out) // 2
    assert total == 10 * _FRAME_SAMPLES


async def test_crossfade_with_truncating_model_stays_1to1(model_dir):
    """Deficit rule and seam crossfade together must still yield a 1:1 stream.

    The crossfade head is non-zero on a truncating model once context exists,
    so the crossfade runs — but the held tail and the next block's re-rendered
    head cover the same real-time span, so no samples are gained or lost.
    """
    _write_truncating_model(model_dir / "trunc.onnx", drop=500)  # ~10ms @48kHz
    backend = _backend(model_dir, crossfade_ms=5)
    session = backend.open_session()
    out = []
    for _ in range(20):  # 4 blocks
        out += await session.push(_frame(8000), 48000, "trunc")
    out += await session.flush()
    assert sum(len(f) for f in out) // 2 == 20 * _FRAME_SAMPLES


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


# ----- fine-tune DSP hook (M10) ------------------------------------------------


async def test_untuned_voice_get_tune_params_returns_identity_default(model_dir):
    backend = _backend(model_dir)
    assert backend.get_tune_params("identity") == TuneParams()
    assert backend.get_tune_params("identity").is_identity


async def test_tune_params_are_keyed_independently_per_model_id(model_dir):
    backend = _backend(model_dir)
    backend.set_tune_params("identity", TuneParams(pitch_offset=5.0))
    backend.set_tune_params("halver", TuneParams(breathiness=0.5))
    assert backend.get_tune_params("identity").pitch_offset == 5.0
    assert backend.get_tune_params("identity").breathiness == 0.0  # untouched
    assert backend.get_tune_params("halver").breathiness == 0.5
    assert backend.get_tune_params("halver").pitch_offset == 0.0  # untouched


async def test_tuned_stream_preserves_1to1_frame_cadence(model_dir):
    """A tuned voice must emit exactly one output frame per input frame, same
    as an untuned stream over identical input (app/dsp.py's length-preserving
    invariant must hold through the full streaming path, not just in isolation
    against the pure functions — see test_dsp.py for those).
    """
    backend = _backend(model_dir)
    backend.set_tune_params(
        "identity", TuneParams(pitch_offset=3.0, speed_factor=1.2, breathiness=0.3)
    )
    session = backend.open_session()
    frames_in = 12  # 2 full blocks + 2 leftover frames drained by flush
    out = []
    for _ in range(frames_in):
        out += await session.push(_frame(8000), 48000, "identity")
    out += await session.flush()
    assert len(out) == frames_in
    assert all(len(f) == _FRAME_BYTES for f in out)


async def test_tuned_stream_with_extreme_speed_factor_preserves_cadence(model_dir):
    backend = _backend(model_dir)
    backend.set_tune_params("identity", TuneParams(speed_factor=2.0))
    session = backend.open_session()
    out = []
    for _ in range(10):
        out += await session.push(_frame(8000), 48000, "identity")
    out += await session.flush()
    assert len(out) == 10
    assert all(len(f) == _FRAME_BYTES for f in out)


async def test_tuned_output_differs_from_untuned_for_identical_input(model_dir):
    """Proves the DSP hook actually runs on the streaming path (not just
    wired-but-inert): tuned output audio must differ from untuned, even
    though the frame count matches exactly."""
    plain = _backend(model_dir)
    plain_session = plain.open_session()
    out_plain = []
    for _ in range(5):
        out_plain += await plain_session.push(_frame(8000), 48000, "identity")

    tuned = _backend(model_dir)
    tuned.set_tune_params("identity", TuneParams(breathiness=1.0))
    tuned_session = tuned.open_session()
    out_tuned = []
    for _ in range(5):
        out_tuned += await tuned_session.push(_frame(8000), 48000, "identity")

    assert b"".join(out_plain) != b"".join(out_tuned)


async def test_tuning_explicit_identity_matches_never_tuned_output(model_dir):
    """A voice explicitly tuned to the identity values must sound exactly like
    a voice nobody has ever tuned — proves apply_tuning's is_identity
    short-circuit (skips the DSP chain entirely) is not just an optimization
    but also behaviorally transparent.
    """
    untuned = _backend(model_dir)
    untuned_session = untuned.open_session()
    out_untuned = []
    for _ in range(5):
        out_untuned += await untuned_session.push(_frame(8000), 48000, "identity")

    explicit_identity = _backend(model_dir)
    explicit_identity.set_tune_params("identity", TuneParams())
    explicit_session = explicit_identity.open_session()
    out_explicit = []
    for _ in range(5):
        out_explicit += await explicit_session.push(_frame(8000), 48000, "identity")

    assert b"".join(out_untuned) == b"".join(out_explicit)
