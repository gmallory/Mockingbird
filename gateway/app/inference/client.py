"""gRPC client for the inference service.

One ``InferenceSession`` wraps a single bidirectional ``VoiceConversion.Convert``
stream for the lifetime of a WebSocket session. The stream is **decoupled**:
input frames are written with :meth:`send` and converted frames are read with the
:meth:`outputs` async iterator, running concurrently. This is required because
conversion is no longer 1:1 — a clip-based backend buffers a whole utterance and
then emits a burst of output frames, so reads do not line up with writes.

If the inference service is unreachable — at :meth:`open` time or mid-session —
the call raises ``InferenceUnavailable``; the WS handler catches it and degrades
to passthrough rather than dropping the session.
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
        self._call = None

    async def open(self) -> None:
        """Establish the channel and open the Convert stream.

        Bounds connection establishment by ``timeout_s`` so an unreachable
        inference service surfaces promptly as ``InferenceUnavailable`` instead
        of leaving the session waiting forever. Called lazily on the first audio
        frame so control-only sessions never touch inference.
        """
        try:
            await asyncio.wait_for(self._channel.channel_ready(), self._timeout_s)
        except (TimeoutError, grpc.aio.AioRpcError) as exc:
            raise InferenceUnavailable(f"inference unreachable: {exc}") from exc
        self._call = self._stub.Convert()

    def _require_call(self):
        if self._call is None:
            raise InferenceUnavailable("inference stream not open")
        return self._call

    async def send(self, pcm: bytes, sample_rate: int, model_id: str) -> None:
        call = self._require_call()
        frame = audio_pb2.AudioFrame(pcm=pcm, sample_rate=sample_rate, model_id=model_id or "")
        try:
            # Bound the write: gRPC HTTP/2 flow control blocks the write when the
            # server stops reading, and send() runs on the WS receive loop, so an
            # unbounded write would freeze the whole session instead of degrading.
            await asyncio.wait_for(call.write(frame), self._timeout_s)
        except TimeoutError as exc:
            raise InferenceUnavailable(
                f"inference write timed out after {self._timeout_s}s"
            ) from exc
        except grpc.aio.AioRpcError as exc:
            raise InferenceUnavailable(str(exc)) from exc

    async def outputs(self):
        """Yield converted frames as they arrive, until the stream ends."""
        call = self._require_call()
        try:
            while True:
                response = await call.read()
                if response is grpc.aio.EOF:
                    break
                yield response.pcm
        except grpc.aio.AioRpcError as exc:
            raise InferenceUnavailable(str(exc)) from exc

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
