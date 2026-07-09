"""Fine-tune DSP transforms (M10): pitch, speed, and breathiness.

Pure, allocation-cheap numpy functions applied to one already-converted audio
**block** (not per-20ms frame — see ``app/backends/self_hosted.py``'s
``_convert_block``, which is the only caller in the streaming path). Every
function here is a **length-preserving** transform: given an ``N``-sample
block, it returns an ``N``-sample block, regardless of the parameter value.
That invariant is what lets ``apply_tuning`` slot into the streaming session
without touching any block/frame chunking math — the DSP stage is a drop-in
``ndarray -> ndarray`` step between the ONNX conversion (+ seam crossfade) and
PCM re-encoding, and the stream's frame-for-frame 1:1 cadence (input frames in
== output frames out, the invariant M5b/M9 already protect) is never at risk.

Each transform is a no-op (returns the input array unchanged, no allocation)
at its identity parameter value (``pitch_offset=0``, ``speed_factor=1.0``,
``breathiness=0``), so a voice nobody has tuned costs nothing extra on the hot
path.

Techniques, and why they're honest rather than broadcast-quality DSP:

- **Pitch** (``shift_pitch``): a single-frame spectral resample — take the
  block's real FFT, remap bin ``j`` from source bin ``j / ratio``, inverse-FFT
  back to exactly the input length. This shifts spectral content without
  changing the sample count (unlike the classic "resample twice" trick, which
  necessarily trades pitch for duration — see the module history/PR
  discussion). It has no phase vocoder / overlap-add smoothing, so very
  low-frequency blocks can show minor artifacts at block boundaries; the
  existing seam crossfade in ``self_hosted.py`` already runs on the *pre-tune*
  audio and is unaffected since tuning is applied after it.
- **Speed** (``adjust_speed``): a naive playback-rate change — resample the
  block's own content to ``round(N / factor)`` samples (compressing for
  faster, expanding for slower), then pad (edge-replicate) or truncate back to
  exactly ``N`` samples so the block's duration is unaffected downstream. Like
  changing a tape's speed, this ties a small pitch shift to the tempo change;
  documented, accepted simplification for a real-time per-block effect where
  true tempo-only stretching (WSOLA/phase vocoder) is out of scope.
- **Breathiness** (``add_breathiness``): shaped (high-pass-tilted) noise,
  amplitude-enveloped to the block's own loudness so silence stays silent, and
  mixed in proportion to ``amount``.

None of this claims broadcast/studio quality — see the M10 spike note in
``app/tuning.py`` for the full out-of-band wiring story. Correctness (exact
length preservation), testability (pure functions, deterministic when a seeded
``rng`` is passed to ``add_breathiness``), and not disturbing the streaming
cadence are the bar; perceptual quality is a nice-to-have.
"""

import numpy as np

# Shared with gateway/app/voices/routes.py's PATCH validation and
# PRODUCT_SPEC §6 (VoiceModel.pitch_offset/speed_factor/breathiness) — the two
# services can't literally share a constant (separate uv projects/venvs), so
# keep both copies in sync with the spec if these ever change.
PITCH_MIN, PITCH_MAX = -12.0, 12.0
SPEED_MIN, SPEED_MAX = 0.5, 2.0
BREATHINESS_MIN, BREATHINESS_MAX = 0.0, 1.0


def shift_pitch(audio: np.ndarray, semitones: float) -> np.ndarray:
    """Shift pitch by ``semitones`` (positive = up), preserving ``audio.size``.

    Identity (returns ``audio`` unchanged, no allocation) at ``semitones=0``.
    """
    n = audio.size
    if semitones == 0.0 or n == 0:
        return audio
    ratio = 2.0 ** (semitones / 12.0)
    spectrum = np.fft.rfft(audio)
    n_bins = spectrum.size
    bins = np.arange(n_bins, dtype=np.float64)
    src_idx = bins / ratio
    # Interpolate real/imag separately rather than magnitude/phase: phase
    # wraps at +/-pi, so linearly interpolating it across a wrap point
    # produces garbage. Real/imag interpolation has no such discontinuity.
    real = np.interp(src_idx, bins, spectrum.real, left=0.0, right=0.0)
    imag = np.interp(src_idx, bins, spectrum.imag, left=0.0, right=0.0)
    shifted = real + 1j * imag
    return np.fft.irfft(shifted, n=n).astype(np.float32)


def adjust_speed(audio: np.ndarray, factor: float) -> np.ndarray:
    """Naive playback-rate change; always returns exactly ``audio.size`` samples.

    Identity (returns ``audio`` unchanged, no allocation) at ``factor=1.0``.
    """
    n = audio.size
    if factor == 1.0 or n == 0:
        return audio
    m = max(1, round(n / factor))
    src_idx = np.linspace(0.0, n - 1, m)
    resampled = np.interp(src_idx, np.arange(n), audio).astype(np.float32)
    if m == n:
        return resampled
    if m > n:
        # Slower: the block stretched past its own boundary; keep the front
        # (this block's content starts where the previous one left off) and
        # drop the tail rather than resample again (which would cancel the
        # speed change back out — see module docstring).
        return resampled[:n]
    # Faster: fewer samples than the block needs. Edge-replicate the last
    # sample rather than zero-pad, so the tail doesn't click to silence.
    pad = np.full(n - m, resampled[-1], dtype=np.float32)
    return np.concatenate([resampled, pad])


def add_breathiness(
    audio: np.ndarray, amount: float, rng: np.random.Generator | None = None
) -> np.ndarray:
    """Blend in loudness-enveloped, high-pass-shaped noise. ``amount`` in [0, 1].

    Identity (returns ``audio`` unchanged, no allocation) at ``amount<=0``.
    ``rng`` defaults to a fresh ``np.random.default_rng()`` (real variety in
    production); pass a seeded generator for deterministic tests.
    """
    if amount <= 0.0 or audio.size == 0:
        return audio
    gen = rng if rng is not None else np.random.default_rng()
    noise = gen.standard_normal(audio.size).astype(np.float32)
    # First-difference high-pass: tilts flat white noise toward higher
    # frequencies, closer to breath/aspiration noise than a flat spectrum.
    shaped = np.diff(noise, prepend=noise[:1])
    peak = np.max(np.abs(shaped))
    if peak > 0:
        shaped = shaped / peak
    envelope = np.abs(audio)
    window = max(1, audio.size // 100)
    if window > 1:
        kernel = np.ones(window, dtype=np.float32) / window
        envelope = np.convolve(envelope, kernel, mode="same")
    mixed = audio + shaped * envelope * amount
    return np.clip(mixed, -1.0, 1.0).astype(np.float32)


def apply_tuning(
    audio: np.ndarray,
    pitch_offset: float = 0.0,
    speed_factor: float = 1.0,
    breathiness: float = 0.0,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Run the fine-tune DSP chain in order: pitch -> speed -> breathiness.

    Every stage preserves ``audio.size`` and no-ops at its identity value, so
    this is always safe to call with a block fresh off the ONNX model/seam
    crossfade — callers never need to special-case "no tuning set".
    """
    if audio.size == 0:
        return audio
    out = audio
    if pitch_offset:
        out = shift_pitch(out, pitch_offset)
    if speed_factor != 1.0:
        out = adjust_speed(out, speed_factor)
    if breathiness > 0.0:
        out = add_breathiness(out, breathiness, rng=rng)
    return out
