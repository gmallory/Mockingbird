"""mu-law codec + 48k<->8k resampling (M8a): pure functions, no I/O."""

import math
import struct

from app.calls.telephony import (
    mulaw8_to_pcm48,
    mulaw_decode,
    mulaw_encode,
    pcm48_to_mulaw8,
)


def test_mulaw_silence_and_sign() -> None:
    # G.711: zero encodes to 0xFF and decodes back to exactly zero.
    assert mulaw_encode([0]) == b"\xff"
    assert mulaw_decode(b"\xff") == [0]
    # Sign is preserved through the roundtrip.
    for value in (100, 1000, 20000):
        (pos,) = mulaw_decode(mulaw_encode([value]))
        (neg,) = mulaw_decode(mulaw_encode([-value]))
        assert pos > 0 and neg < 0
        assert pos == -neg  # codec is symmetric


def test_mulaw_roundtrip_within_quantization_error() -> None:
    # mu-law is logarithmic: absolute error grows with amplitude, bounded by the
    # top-segment half step, plus the hard clip at 32635 — full-scale inputs
    # come back ~644 off, mid-range within a few counts.
    samples = [0, 1, -1, 7, 33, 128, 500, -500, 2000, -8191, 16000, 32767, -32768]
    decoded = mulaw_decode(mulaw_encode(samples))
    for original, back in zip(samples, decoded, strict=True):
        assert abs(back - original) <= 650, (original, back)


def test_pcm48_to_mulaw8_sizes() -> None:
    # A 20ms 48kHz frame (960 samples / 1920 bytes) -> 160 mu-law bytes (20ms @ 8kHz).
    frame = struct.pack("<960h", *([1000] * 960))
    assert len(pcm48_to_mulaw8(frame)) == 160


def test_mulaw8_to_pcm48_sizes() -> None:
    payload = mulaw_encode([500] * 160)
    pcm = mulaw8_to_pcm48(payload)
    assert len(pcm) == 960 * 2


def test_narrowband_sine_survives_roundtrip() -> None:
    # A 400Hz tone (well inside the 4kHz PSTN band) must come back strongly
    # correlated after 48k -> mu-law 8k -> 48k. HF content would not; a plain
    # voice fundamental should.
    n = 960
    original = [int(10000 * math.sin(2 * math.pi * 400 * i / 48000)) for i in range(n)]
    frame = struct.pack(f"<{n}h", *original)
    back = struct.unpack(f"<{n}h", mulaw8_to_pcm48(pcm48_to_mulaw8(frame)))

    mean_o = sum(original) / n
    mean_b = sum(back) / n
    cov = sum((o - mean_o) * (b - mean_b) for o, b in zip(original, back, strict=True))
    var_o = sum((o - mean_o) ** 2 for o in original)
    var_b = sum((b - mean_b) ** 2 for b in back)
    correlation = cov / math.sqrt(var_o * var_b)
    assert correlation > 0.95, correlation
