"""G.711 mu-law codec + 48kHz<->8kHz resampling for the Twilio media bridge (M8a).

Twilio Media Streams speak 8kHz mono G.711 mu-law (base64 in JSON); the browser
loop speaks 48kHz mono Int16 PCM in 20ms frames (960 samples / 1920 bytes). This
module converts between the two — pure stdlib, because ``audioop`` was removed in
Python 3.13 and numpy is not a gateway dependency. At telephony sizes (160
samples per 20ms) the per-sample Python loops are far off the hot-path budget.

The rate conversion is deliberately simple: decimation averages each group of 6
samples (a crude low-pass), interpolation is linear. The PSTN leg is 8kHz
narrowband regardless, so a proper polyphase filter would be inaudible here.
Both directions are stateless per frame; the one-sample seam this leaves at
frame edges is below the mu-law quantization floor.
"""

import struct

PCM_RATE = 48000
PSTN_RATE = 8000
RATIO = PCM_RATE // PSTN_RATE  # 6

_BIAS = 0x84
_CLIP = 32635


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _decode_sample(byte: int) -> int:
    byte = ~byte & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = ((mantissa << 3) + _BIAS) << exponent
    sample -= _BIAS
    return -sample if sign else sample


# 256-entry decode table; encode stays a function (a full 64K table buys nothing
# at 160 samples per frame).
_DECODE = [_decode_sample(b) for b in range(256)]


def mulaw_encode(samples: list[int]) -> bytes:
    """Int16 samples -> one mu-law byte each."""
    return bytes(_encode_sample(s) for s in samples)


def mulaw_decode(data: bytes) -> list[int]:
    """mu-law bytes -> Int16 samples."""
    return [_DECODE[b] for b in data]


def pcm48_to_mulaw8(frame: bytes) -> bytes:
    """A 48kHz Int16 PCM frame -> 8kHz mu-law payload (1/12 the byte count)."""
    n = len(frame) // 2
    samples = struct.unpack(f"<{n}h", frame[: n * 2])
    out: list[int] = []
    for i in range(0, n - RATIO + 1, RATIO):
        out.append(sum(samples[i : i + RATIO]) // RATIO)
    return mulaw_encode(out)


def mulaw8_to_pcm48(payload: bytes) -> bytes:
    """An 8kHz mu-law payload -> 48kHz Int16 PCM frame (12x the byte count)."""
    samples = mulaw_decode(payload)
    n = len(samples)
    out: list[int] = []
    for i in range(n):
        cur = samples[i]
        nxt = samples[i + 1] if i + 1 < n else cur
        for k in range(RATIO):
            out.append(cur + (nxt - cur) * k // RATIO)
    return struct.pack(f"<{len(out)}h", *out)
