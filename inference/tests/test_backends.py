"""Backend unit tests: passthrough is a no-op; cartesia calls the API and maps bytes."""

from unittest.mock import AsyncMock

import pytest

from app.backends import get_backend
from app.backends.cartesia import CartesiaBackend
from app.backends.passthrough import PassthroughBackend
from app.config import Settings


async def test_passthrough_returns_identical_bytes():
    backend = PassthroughBackend()
    pcm = bytes(range(256)) * 8  # arbitrary frame
    assert await backend.convert(pcm, 48000, "") == pcm


async def test_cartesia_posts_and_returns_response_bytes():
    backend = CartesiaBackend(api_key="k", base_url="http://test", version="v")

    class _Resp:
        content = b"transformed-pcm"

        def raise_for_status(self):
            return None

    backend._client.post = AsyncMock(return_value=_Resp())
    out = await backend.convert(b"\x00\x01" * 960, 48000, "voice-123")

    assert out == b"transformed-pcm"
    # The target voice and raw pcm_s16le output format must be sent.
    _, kwargs = backend._client.post.call_args
    assert kwargs["data"]["voice[id]"] == "voice-123"
    assert kwargs["data"]["output_format[encoding]"] == "pcm_s16le"
    await backend.aclose()


async def test_cartesia_requires_a_voice():
    backend = CartesiaBackend(api_key="k", base_url="http://test", version="v")
    with pytest.raises(ValueError):
        await backend.convert(b"\x00\x01" * 960, 48000, "")  # no model_id, no default
    await backend.aclose()


def test_factory_passthrough_is_default():
    assert isinstance(get_backend(Settings(inference_backend="passthrough")), PassthroughBackend)


def test_factory_cartesia_without_key_raises():
    with pytest.raises(RuntimeError):
        get_backend(Settings(inference_backend="cartesia", cartesia_api_key=""))


def test_factory_self_hosted_not_implemented():
    with pytest.raises(NotImplementedError):
        get_backend(Settings(inference_backend="self_hosted"))
