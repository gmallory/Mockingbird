"""gRPC client for the inference service.

One ``InferenceSession`` wraps a single bidirectional ``VoiceConversion.Convert``
stream for the lifetime of a WebSocket session. The browser/WS contract is 1:1
and ordered, so each ``convert`` writes one frame and reads one back.

If the inference service is unreachable — at connect time or mid-session — the
write/read raises ``InferenceUnavailable``; the WS handler catches it and
degrades to passthrough rather than dropping the session.
"""

import asyncio

import grpc

from app.proto_gen import audio_pb2, audio_pb2_grpc


class InferenceUnavailable(Exception):
    """The inference stream failed; caller should degrade to passthrough."""


class InferenceSession:
    def __init__(self, grpc_url: str, timeout_s: float = 2.0) -> None:
        self._channel = grpc.aio.insecure_channel(grpc_url)
        self._stub = audio_pb2_grpc.VoiceConversionStub(self._channel)
        self._timeout_s = timeout_s
        # The Convert stream is opened lazily on the first frame, so a
        # control-only session never starts (or has to tear down) a stream.
        self._call = None

    async def _exchange(self, frame: audio_pb2.AudioFrame):
        await self._call.write(frame)
        return await self._call.read()

    async def convert(self, pcm: bytes, sample_rate: int, model_id: str) -> bytes:
        if self._call is None:
            self._call = self._stub.Convert()
        frame = audio_pb2.AudioFrame(pcm=pcm, sample_rate=sample_rate, model_id=model_id or "")
        try:
            # Bound the round-trip: a stalled stream (connect never completes, read
            # never returns) must surface as InferenceUnavailable, not hang the loop.
            response = await asyncio.wait_for(self._exchange(frame), self._timeout_s)
        except TimeoutError as exc:
            raise InferenceUnavailable(f"inference timed out after {self._timeout_s}s") from exc
        except grpc.aio.AioRpcError as exc:
            raise InferenceUnavailable(str(exc)) from exc

        if response is grpc.aio.EOF:
            raise InferenceUnavailable("inference stream closed")
        return response.pcm

    async def aclose(self) -> None:
        # Best-effort: teardown must never raise into the WS disconnect path. A
        # half-open or failed stream can throw low-level gRPC errors on close.
        try:
            if self._call is not None:
                await self._call.done_writing()
        except Exception:  # noqa: BLE001 - cleanup only
            pass
        finally:
            await self._channel.close()
