"""gRPC server: bidirectional ``VoiceConversion.Convert`` stream.

One stream per WebSocket session (the gateway owns that mapping). For each
inbound frame we call the selected backend and yield the transformed frame back
in order, 1:1.
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
        async for frame in request_iterator:
            pcm = await self._backend.convert(frame.pcm, frame.sample_rate, frame.model_id)
            yield audio_pb2.AudioFrame(
                pcm=pcm,
                sample_rate=frame.sample_rate,
                model_id=frame.model_id,
            )


async def create_server(backend: InferenceBackend, host: str, port: int) -> grpc.aio.Server:
    server = grpc.aio.server()
    audio_pb2_grpc.add_VoiceConversionServicer_to_server(VoiceConversionServicer(backend), server)
    server.add_insecure_port(f"{host}:{port}")
    log.info("inference.grpc_bound", host=host, port=port)
    return server
