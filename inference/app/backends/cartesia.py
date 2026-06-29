"""Cartesia cloud backend: a real voice transform with no GPU.

This is the M3 "first real backend." It calls Cartesia's voice-changer endpoint,
which takes an audio clip and returns it re-voiced as the target voice.

Honest limitation: the endpoint is a per-clip HTTP call, so converting each 20ms
frame independently is functional but neither latency-optimal nor seam-free
across frame boundaries. It exists to prove the swappable interface end-to-end;
the latency-correct path (Cartesia's streaming WebSocket, frame batching) is a
later optimization. Quality/latency here are not the M3 success criteria —
passthrough is the measured path; cartesia just proves "a real transform plugs
in behind the same interface."
"""

import io
import wave

import httpx

from app.backends.base import InferenceBackend

_VOICE_CHANGER_PATH = "/voice-changer/bytes"


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap mono Int16 PCM in a minimal WAV container (the endpoint wants a file)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # Int16
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


class CartesiaBackend(InferenceBackend):
    def __init__(
        self,
        api_key: str,
        base_url: str,
        version: str,
        default_voice_id: str = "",
    ) -> None:
        self._default_voice_id = default_voice_id
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-API-Key": api_key, "Cartesia-Version": version},
            timeout=10.0,
        )

    async def convert(self, pcm: bytes, sample_rate: int, model_id: str) -> bytes:
        voice_id = model_id or self._default_voice_id
        if not voice_id:
            raise ValueError(
                "cartesia backend needs a target voice: send a modelId or set CARTESIA_VOICE_ID"
            )

        files = {"clip": ("frame.wav", _pcm_to_wav(pcm, sample_rate), "audio/wav")}
        data = {
            "voice[id]": voice_id,
            "output_format[container]": "raw",
            "output_format[encoding]": "pcm_s16le",
            "output_format[sample_rate]": str(sample_rate),
        }
        resp = await self._client.post(_VOICE_CHANGER_PATH, files=files, data=data)
        resp.raise_for_status()
        return resp.content

    async def aclose(self) -> None:
        await self._client.aclose()
