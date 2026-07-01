"""Cartesia cloud backend: a real voice transform with no GPU.

Cartesia's Voice Changer is **clip-based** — `/voice-changer/bytes` and
`/voice-changer/sse` both take a whole audio clip and return it re-voiced as the
target voice. There is no streaming-input voice changer (the realtime WebSocket
is TTS-only), so true per-frame live morphing is not possible here; that latency
profile belongs to the self-hosted RVC GPU path. With Cartesia the honest model
is **utterance-segmented**.

This backend therefore buffers the 20ms input frames with a simple energy VAD
and, when the speaker pauses, sends the whole utterance to `/voice-changer/sse`,
decodes the streamed output, and re-chunks it back into 20ms frames. The result
is a walkie-talkie feel: speak a phrase, hear it back in the cloned voice. VAD
state lives on the per-stream session; the network call lives on the backend so
the httpx client is shared across sessions.

The energy VAD is intentionally minimal (no torch/onnxruntime dependency). Silero
VAD is the planned hardening swap behind the same session interface.
"""

import base64
import io
import json
import wave
from collections import deque

import httpx
import numpy as np
import structlog

from app.backends.base import BackendSession, InferenceBackend

log = structlog.get_logger(__name__)

_VOICE_CHANGER_SSE_PATH = "/voice-changer/sse"


def cartesia_client(
    *,
    api_key: str,
    base_url: str,
    version: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Build the Cartesia httpx client (bearer auth, version header, timeout).

    Shared by the long-lived voice-changer client here and the per-request clone
    client in ``app/voices.py`` — both talk to the same Cartesia auth surface.
    """
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}", "Cartesia-Version": version},
        transport=transport,
        timeout=httpx.Timeout(30.0, connect=10.0),
    )


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap mono Int16 PCM in a minimal WAV container (the endpoint wants a file)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # Int16
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def _rms_normalized(pcm: bytes) -> float:
    """Root-mean-square level of an Int16 PCM frame, normalized to 0..1."""
    if len(pcm) < 2:
        return 0.0
    if len(pcm) % 2:  # defensive: frombuffer needs whole Int16 samples
        pcm = pcm[:-1]
    # float32 is ample for a 0..1 energy gate and halves the per-frame array
    # allocation this runs on every 20ms frame.
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples)))) / 32768.0


def _rechunk(pcm: bytes, frame_bytes: int) -> list[bytes]:
    """Split a blob of PCM into fixed-size frames, zero-padding the final frame."""
    if frame_bytes <= 0 or not pcm:
        return []
    frames = [pcm[i : i + frame_bytes] for i in range(0, len(pcm), frame_bytes)]
    last = frames[-1]
    if len(last) < frame_bytes:
        frames[-1] = last + b"\x00" * (frame_bytes - len(last))
    return frames


def _frame_bytes(sample_rate: int, frame_ms: int) -> int:
    return int(sample_rate * frame_ms / 1000) * 2  # *2 for Int16


class _CartesiaSession(BackendSession):
    """Groups input frames into utterances and converts each on a trailing pause."""

    def __init__(self, backend: CartesiaBackend) -> None:
        self._backend = backend
        self._threshold = backend.energy_threshold
        self._silence_limit = backend.silence_frames
        self._max_frames = backend.max_utterance_frames
        self._preroll: deque[bytes] = deque(maxlen=backend.preroll_frames)
        self._buf: list[bytes] = []
        self._in_speech = False
        self._silence_run = 0
        self._sample_rate = 48000
        self._model_id = ""

    async def push(self, pcm: bytes, sample_rate: int, model_id: str) -> list[bytes]:
        self._sample_rate = sample_rate
        self._model_id = model_id
        speech = _rms_normalized(pcm) >= self._threshold

        if not self._in_speech:
            if speech:
                # Start the utterance with the buffered pre-roll plus this onset
                # frame, so the very first word is not clipped.
                self._in_speech = True
                self._buf = list(self._preroll)
                self._buf.append(pcm)
                self._preroll.clear()
                self._silence_run = 0
            else:
                # Keep a rolling pre-roll of recent silence ahead of any onset.
                self._preroll.append(pcm)
            return []

        self._buf.append(pcm)
        if speech:
            self._silence_run = 0
        else:
            self._silence_run += 1

        if self._silence_run >= self._silence_limit or len(self._buf) >= self._max_frames:
            return await self._end_utterance()
        return []

    async def flush(self) -> list[bytes]:
        if self._in_speech and self._buf:
            return await self._end_utterance()
        return []

    async def _end_utterance(self) -> list[bytes]:
        utterance = b"".join(self._buf)
        self._buf = []
        self._in_speech = False
        self._silence_run = 0
        self._preroll.clear()
        return await self._backend.convert_utterance(utterance, self._sample_rate, self._model_id)


class CartesiaBackend(InferenceBackend):
    def __init__(
        self,
        api_key: str,
        base_url: str,
        version: str,
        default_voice_id: str = "",
        frame_ms: int = 20,
        energy_threshold: float = 0.02,
        silence_ms: int = 500,
        max_utterance_ms: int = 15000,
        preroll_ms: int = 200,
    ) -> None:
        self._default_voice_id = default_voice_id
        self._frame_ms = frame_ms
        self.energy_threshold = energy_threshold
        # Convert the ms-based VAD config into frame counts once, up front.
        self.silence_frames = max(1, round(silence_ms / frame_ms))
        self.max_utterance_frames = max(1, round(max_utterance_ms / frame_ms))
        self.preroll_frames = max(0, round(preroll_ms / frame_ms))
        self._client = cartesia_client(api_key=api_key, base_url=base_url, version=version)

    def open_session(self) -> BackendSession:
        return _CartesiaSession(self)

    async def convert_utterance(self, pcm: bytes, sample_rate: int, model_id: str) -> list[bytes]:
        """Convert one whole utterance via /voice-changer/sse; return 20ms frames."""
        frame_bytes = _frame_bytes(sample_rate, self._frame_ms)
        voice_id = model_id or self._default_voice_id
        if not voice_id:
            # Misconfigured (no target voice). Don't abort the stream — hand the
            # original audio back so the speaker still hears themselves.
            log.warning("cartesia.no_voice_id", note="set CARTESIA_VOICE_ID or send a modelId")
            return _rechunk(pcm, frame_bytes)

        files = {"clip": ("utterance.wav", _pcm_to_wav(pcm, sample_rate), "audio/wav")}
        data = {
            "voice[id]": voice_id,
            "output_format[container]": "raw",
            "output_format[encoding]": "pcm_s16le",
            "output_format[sample_rate]": str(sample_rate),
        }

        try:
            out = bytearray()
            async with self._client.stream(
                "POST", _VOICE_CHANGER_SSE_PATH, files=files, data=data
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[len("data:") :].strip())
                    except json.JSONDecodeError:
                        continue  # skip keep-alives / non-JSON data: lines
                    # Read the chunk before checking `done`: a terminal event may
                    # carry the final audio alongside done=true.
                    chunk = event.get("data")
                    if chunk:
                        out += base64.b64decode(chunk)
                    if event.get("done"):
                        break
        except (httpx.HTTPError, ValueError) as exc:
            # One bad clip must not kill the session. Log and echo this utterance
            # back unchanged so conversion resumes on the next utterance, instead
            # of aborting the gRPC stream and degrading the whole session.
            log.warning("cartesia.convert_failed", error=str(exc))
            return _rechunk(pcm, frame_bytes)

        return _rechunk(bytes(out), frame_bytes)

    async def aclose(self) -> None:
        await self._client.aclose()
