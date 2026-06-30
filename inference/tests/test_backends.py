"""Backend unit tests.

Passthrough is 1:1 (one output frame per input frame). Cartesia groups input
frames into utterances with the energy VAD and converts each on a trailing pause
via /voice-changer/sse — mocked here so no network or API key is needed.
"""

import base64
import json
import struct
from unittest.mock import MagicMock

import httpx
import pytest

from app.backends import get_backend
from app.backends.cartesia import CartesiaBackend, _rechunk, _rms_normalized
from app.backends.passthrough import PassthroughBackend
from app.config import Settings

_FRAME_SAMPLES = 960  # 20ms @ 48kHz


def _frame(value: int, n: int = _FRAME_SAMPLES) -> bytes:
    """A 20ms Int16 PCM frame of constant amplitude (loud if nonzero, silent if 0)."""
    return struct.pack(f"<{n}h", *([value] * n))


class _FakeStream:
    """Async context manager mimicking httpx's client.stream() for the SSE call."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock(return_value=None)

        async def _aiter():
            for line in self._lines:
                yield line

        resp.aiter_lines = _aiter
        return resp

    async def __aexit__(self, *exc):
        return False


class _FakeErrorStream:
    """client.stream() whose response raises HTTPStatusError on raise_for_status."""

    async def __aenter__(self):
        resp = MagicMock()
        request = httpx.Request("POST", "http://test/voice-changer/sse")
        response = httpx.Response(500, request=request)
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("server error", request=request, response=response)
        )
        return resp

    async def __aexit__(self, *exc):
        return False


def _sse_lines(pcm_out: bytes, sample_rate: int = 48000) -> list[str]:
    chunk = json.dumps(
        {
            "done": False,
            "status_code": 206,
            "data": base64.b64encode(pcm_out).decode(),
            "sample_rate": sample_rate,
            "step_time": 1,
        }
    )
    done = json.dumps({"done": True, "status_code": 200})
    return [f"data: {chunk}", "", f"data: {done}", ""]


def _cartesia(**overrides) -> CartesiaBackend:
    params = {
        "api_key": "k",
        "base_url": "http://test",
        "version": "v",
        "default_voice_id": "voice-1",
        "frame_ms": 20,
        "energy_threshold": 0.02,
        "silence_ms": 40,  # -> 2 silent frames end an utterance
        "max_utterance_ms": 15000,
        "preroll_ms": 0,
    }
    params.update(overrides)
    return CartesiaBackend(**params)


# ----- passthrough ----------------------------------------------------------


async def test_passthrough_session_is_1to1():
    session = PassthroughBackend().open_session()
    pcm = bytes(range(256)) * 8
    assert await session.push(pcm, 48000, "") == [pcm]
    assert await session.flush() == []


# ----- helpers --------------------------------------------------------------


def test_rms_normalized_silence_vs_loud():
    assert _rms_normalized(_frame(0)) == 0.0
    assert _rms_normalized(_frame(10000)) > 0.2


def test_rechunk_pads_final_partial_frame():
    out = _rechunk(b"\x01" * 1920 + b"\x02" * 100, 1920)
    assert [len(f) for f in out] == [1920, 1920]
    assert out[1].endswith(b"\x00")


# ----- cartesia VAD + SSE ---------------------------------------------------


async def test_cartesia_buffers_then_converts_on_silence():
    backend = _cartesia()
    pcm_out = b"\x07\x00" * _FRAME_SAMPLES  # one 20ms frame of output
    backend._client.stream = MagicMock(return_value=_FakeStream(_sse_lines(pcm_out)))

    session = backend.open_session()
    loud, quiet = _frame(10000), _frame(0)

    assert await session.push(loud, 48000, "") == []  # onset, buffering
    assert await session.push(loud, 48000, "") == []
    assert await session.push(quiet, 48000, "") == []  # 1 silent frame, not yet
    out = await session.push(quiet, 48000, "")  # 2nd silent frame ends the utterance

    assert out == [pcm_out]
    backend._client.stream.assert_called_once()
    await backend.aclose()


async def test_cartesia_flush_converts_trailing_speech():
    backend = _cartesia()
    pcm_out = b"\x07\x00" * _FRAME_SAMPLES
    backend._client.stream = MagicMock(return_value=_FakeStream(_sse_lines(pcm_out)))

    session = backend.open_session()
    await session.push(_frame(10000), 48000, "")
    assert await session.flush() == [pcm_out]
    await backend.aclose()


async def test_cartesia_without_voice_passes_through_without_calling_api():
    backend = _cartesia(default_voice_id="")
    backend._client.stream = MagicMock()  # must not be called

    session = backend.open_session()
    await session.push(_frame(10000), 48000, "")  # onset
    await session.push(_frame(0), 48000, "")  # silence 1
    out = await session.push(_frame(0), 48000, "")  # silence 2 -> end utterance

    # 3 buffered frames (loud + 2 quiet) handed back unchanged as 3 frames.
    assert len(out) == 3
    backend._client.stream.assert_not_called()
    await backend.aclose()


async def test_cartesia_uses_per_frame_model_id_as_voice():
    backend = _cartesia(default_voice_id="")
    captured = {}

    def _stream(method, path, files=None, data=None):
        captured["data"] = data
        return _FakeStream(_sse_lines(b"\x00\x00" * _FRAME_SAMPLES))

    backend._client.stream = MagicMock(side_effect=_stream)

    session = backend.open_session()
    await session.push(_frame(10000), 48000, "voice-xyz")
    await session.push(_frame(0), 48000, "voice-xyz")
    await session.push(_frame(0), 48000, "voice-xyz")

    assert captured["data"]["voice[id]"] == "voice-xyz"
    assert captured["data"]["output_format[encoding]"] == "pcm_s16le"
    await backend.aclose()


async def test_cartesia_api_error_echoes_utterance_instead_of_raising():
    # A failing clip must not raise out of push(): that would abort the gRPC
    # stream and degrade the whole session. The utterance is echoed back instead.
    backend = _cartesia()
    backend._client.stream = MagicMock(return_value=_FakeErrorStream())

    session = backend.open_session()
    loud = _frame(10000)
    await session.push(loud, 48000, "")  # onset
    await session.push(_frame(0), 48000, "")  # silence 1
    out = await session.push(_frame(0), 48000, "")  # silence 2 -> convert (API 500)

    assert out, "errored clip should be echoed back, not dropped"
    assert b"".join(out).startswith(loud)
    await backend.aclose()


async def test_cartesia_converts_on_max_utterance_without_pause():
    # max_utterance_ms=60 / frame_ms=20 -> cut and convert after 3 buffered frames,
    # even with no trailing silence (continuous speech).
    backend = _cartesia(max_utterance_ms=60)
    pcm_out = b"\x07\x00" * _FRAME_SAMPLES
    backend._client.stream = MagicMock(return_value=_FakeStream(_sse_lines(pcm_out)))

    session = backend.open_session()
    loud = _frame(10000)
    assert await session.push(loud, 48000, "") == []  # 1 buffered
    assert await session.push(loud, 48000, "") == []  # 2 buffered
    out = await session.push(loud, 48000, "")  # 3rd hits the max -> convert

    assert out == [pcm_out]
    backend._client.stream.assert_called_once()
    await backend.aclose()


# ----- factory --------------------------------------------------------------


def test_factory_passthrough_is_default():
    assert isinstance(get_backend(Settings(inference_backend="passthrough")), PassthroughBackend)


def test_factory_cartesia_without_key_raises():
    with pytest.raises(RuntimeError):
        get_backend(Settings(inference_backend="cartesia", cartesia_api_key=""))


def test_factory_self_hosted_not_implemented():
    with pytest.raises(NotImplementedError):
        get_backend(Settings(inference_backend="self_hosted"))
