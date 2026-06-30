"""gRPC server: bidirectional ``VoiceConversion.Convert`` stream.

One stream per WebSocket session (the gateway owns that mapping). Each stream
opens one :class:`~app.backends.base.BackendSession`; inbound frames are pushed
into it and any output frames it yields are streamed back in order.

The stream is intentionally **not** 1:1. A frame-level backend (passthrough)
yields one frame per input frame, so the loop behaves exactly as before. A
clip-based backend (Cartesia) buffers a whole utterance silently and then emits
a burst of output frames once the speaker pauses — so output frames are not
aligned to input frames. ``flush`` drains any trailing buffered audio when the
stream ends.
"""

import grpc
import structlog

from app.backends.base import InferenceBackend
from app.proto_gen import audio_pb2, audio_pb2_grpc

log = structlog.get_logger(__name__)


class VoiceConversionServicer(audio_pb2_grpc.VoiceConversionServicer):
    def __init__(self, backend: InferenceBackend) -> None:
        self._backend = backend

    async def Convert(self, request_iterator, context):  # noqa: N802 - gRPC method name
        session = self._backend.open_session()
        # Track the last frame's metadata so flushed frames (which arrive after
        # the input stream has ended) carry a sensible sample_rate / model_id.
        sample_rate = 48000
        model_id = ""
        try:
            async for frame in request_iterator:
                sample_rate = frame.sample_rate
                model_id = frame.model_id
                for pcm in await session.push(frame.pcm, frame.sample_rate, frame.model_id):
                    yield audio_pb2.AudioFrame(pcm=pcm, sample_rate=sample_rate, model_id=model_id)
            for pcm in await session.flush():
                yield audio_pb2.AudioFrame(pcm=pcm, sample_rate=sample_rate, model_id=model_id)
        finally:
            await session.aclose()


async def create_server(backend: InferenceBackend, host: str, port: int) -> grpc.aio.Server:
    server = grpc.aio.server()
    audio_pb2_grpc.add_VoiceConversionServicer_to_server(VoiceConversionServicer(backend), server)
    server.add_insecure_port(f"{host}:{port}")
    log.info("inference.grpc_bound", host=host, port=port)
    return server
