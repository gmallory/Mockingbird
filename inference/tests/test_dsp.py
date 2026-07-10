"""Fine-tune DSP transform tests (M10): pitch/speed/breathiness.

Pure numpy functions (``app/dsp.py``) tested directly, no fixtures, no ONNX/ORT
needed. Every transform must preserve ``audio.size`` (the streaming session in
``app/backends/self_hosted.py`` depends on that invariant to keep the 20ms
frame cadence 1:1 — see ``test_self_hosted.py``'s fine-tune section for the
integration-level proof) and must be a true no-op (the exact same array
object, no allocation) at its identity parameter value.
"""

import numpy as np

from app.dsp import (
    BREATHINESS_MAX,
    BREATHINESS_MIN,
    PITCH_MAX,
    PITCH_MIN,
    SPEED_MAX,
    SPEED_MIN,
    add_breathiness,
    adjust_speed,
    apply_tuning,
    shift_pitch,
)


def _sine(n: int = 2048, freq: float = 220.0, rate: int = 48000, amp: float = 0.5) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ----- shift_pitch -------------------------------------------------------------


def test_shift_pitch_identity_returns_same_array_unchanged():
    audio = _sine()
    out = shift_pitch(audio, 0.0)
    assert out is audio  # no-op, no allocation


def test_shift_pitch_preserves_length():
    audio = _sine(2048)
    for semitones in (-12.0, -3.5, 3.5, 12.0):
        out = shift_pitch(audio, semitones)
        assert out.size == audio.size


def test_shift_pitch_changes_the_signal():
    audio = _sine()
    out = shift_pitch(audio, 5.0)
    assert not np.array_equal(out, audio)
    assert np.isfinite(out).all()


def test_shift_pitch_extremes_stay_finite():
    audio = _sine()
    for semitones in (PITCH_MIN, PITCH_MAX):
        out = shift_pitch(audio, semitones)
        assert out.size == audio.size
        assert np.isfinite(out).all()


def test_shift_pitch_empty_array_is_noop():
    audio = np.zeros(0, dtype=np.float32)
    assert shift_pitch(audio, 5.0) is audio


# ----- adjust_speed --------------------------------------------------------------


def test_adjust_speed_identity_returns_same_array_unchanged():
    audio = _sine()
    out = adjust_speed(audio, 1.0)
    assert out is audio


def test_adjust_speed_preserves_length():
    audio = _sine(2048)
    for factor in (SPEED_MIN, 0.75, 1.25, SPEED_MAX):
        out = adjust_speed(audio, factor)
        assert out.size == audio.size


def test_adjust_speed_faster_edge_replicates_the_tail():
    """factor > 1 compresses to fewer samples than the block; the remainder is
    edge-replicated (not zero-padded), per the module docstring."""
    audio = _sine(1000, freq=440.0)
    out = adjust_speed(audio, 2.0)
    n = audio.size
    m = round(n / 2.0)
    assert out.size == n
    tail = out[m:]
    assert tail.size == n - m
    assert np.all(tail == out[m - 1])


def test_adjust_speed_slower_stays_finite_and_full_length():
    audio = _sine(1000, freq=440.0)
    out = adjust_speed(audio, 0.5)
    assert out.size == audio.size
    assert np.isfinite(out).all()


def test_adjust_speed_changes_the_signal():
    audio = _sine()
    out = adjust_speed(audio, 1.5)
    assert not np.array_equal(out, audio)


def test_adjust_speed_empty_array_is_noop():
    audio = np.zeros(0, dtype=np.float32)
    assert adjust_speed(audio, 2.0) is audio


# ----- add_breathiness -----------------------------------------------------------


def test_add_breathiness_identity_returns_same_array_unchanged():
    audio = _sine()
    out = add_breathiness(audio, 0.0)
    assert out is audio


def test_add_breathiness_negative_amount_is_noop():
    audio = _sine()
    out = add_breathiness(audio, -0.5)
    assert out is audio


def test_add_breathiness_preserves_length():
    audio = _sine(2048)
    out = add_breathiness(audio, 0.5, rng=np.random.default_rng(1))
    assert out.size == audio.size


def test_add_breathiness_changes_the_signal_and_stays_bounded():
    audio = _sine(2048, amp=0.9)
    out = add_breathiness(audio, BREATHINESS_MAX, rng=np.random.default_rng(1))
    assert not np.array_equal(out, audio)
    assert np.isfinite(out).all()
    assert np.all(out >= -1.0) and np.all(out <= 1.0)


def test_add_breathiness_is_deterministic_with_a_seeded_generator():
    audio = _sine(2048)
    out1 = add_breathiness(audio, 0.5, rng=np.random.default_rng(42))
    out2 = add_breathiness(audio, 0.5, rng=np.random.default_rng(42))
    np.testing.assert_array_equal(out1, out2)


def test_add_breathiness_keeps_silence_silent():
    """The noise is loudness-enveloped to the block's own amplitude, so a
    silent block stays silent even at max breathiness (module docstring)."""
    audio = np.zeros(2048, dtype=np.float32)
    out = add_breathiness(audio, BREATHINESS_MAX, rng=np.random.default_rng(0))
    np.testing.assert_array_equal(out, audio)


def test_add_breathiness_empty_array_is_noop():
    audio = np.zeros(0, dtype=np.float32)
    assert add_breathiness(audio, 0.5) is audio


# ----- apply_tuning (full chain) --------------------------------------------------


def test_apply_tuning_identity_is_a_true_noop():
    audio = _sine()
    out = apply_tuning(audio)
    assert out is audio


def test_apply_tuning_preserves_length_for_every_stage_combined():
    audio = _sine(2048)
    out = apply_tuning(
        audio,
        pitch_offset=4.0,
        speed_factor=1.3,
        breathiness=0.4,
        rng=np.random.default_rng(7),
    )
    assert out.size == audio.size
    assert np.isfinite(out).all()


def test_apply_tuning_pitch_only_matches_shift_pitch_directly():
    audio = _sine()
    out = apply_tuning(audio, pitch_offset=6.0)
    expected = shift_pitch(audio, 6.0)
    np.testing.assert_allclose(out, expected, rtol=1e-6, atol=1e-6)


def test_apply_tuning_empty_array_is_noop():
    audio = np.zeros(0, dtype=np.float32)
    out = apply_tuning(audio, pitch_offset=5.0, speed_factor=1.5, breathiness=0.5)
    assert out is audio


def test_apply_tuning_extremes_stay_finite_and_length_preserving():
    audio = _sine(2048, amp=0.8)
    out_max = apply_tuning(
        audio,
        pitch_offset=PITCH_MAX,
        speed_factor=SPEED_MAX,
        breathiness=BREATHINESS_MAX,
        rng=np.random.default_rng(3),
    )
    assert out_max.size == audio.size
    assert np.isfinite(out_max).all()

    out_min = apply_tuning(
        audio,
        pitch_offset=PITCH_MIN,
        speed_factor=SPEED_MIN,
        breathiness=BREATHINESS_MIN,
        rng=np.random.default_rng(3),
    )
    assert out_min.size == audio.size
    assert np.isfinite(out_min).all()


def test_range_constants_match_product_spec():
    """Sanity pin: gateway/app/voices/routes.py's VoiceTuneRequest hardcodes
    these same bounds (separate service/venv, can't literally share the
    constant — see dsp.py's module comment); this just guards dsp.py's own
    copy from drifting silently.
    """
    assert (PITCH_MIN, PITCH_MAX) == (-12.0, 12.0)
    assert (SPEED_MIN, SPEED_MAX) == (0.5, 2.0)
    assert (BREATHINESS_MIN, BREATHINESS_MAX) == (0.0, 1.0)
