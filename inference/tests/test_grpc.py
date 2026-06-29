"""End-to-end gRPC test: a bidi Convert stream against a real in-process server.

With the passthrough backend, frames must come back byte-identical and in order —
this proves the gRPC hop itself, not just the backend.
"""

import socket

import grpc

from app.backends.passthrough import PassthroughBackend
from app.proto_gen import audio_pb2, audio_pb2_grpc
from app.server import create_server


def _free_port() -> int:
    s = socket.socket()
    s.bind(("localhost", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_convert_stream_roundtrip_passthrough():
    port = _free_port()
    server = await create_server(PassthroughBackend(), "localhost", port)
    await server.start()

    frames = [bytes([i]) * 1920 for i in range(4)]

    async def _requests():
        for pcm in frames:
            yield audio_pb2.AudioFrame(pcm=pcm, sample_rate=48000, model_id="")

    try:
        async with grpc.aio.insecure_channel(f"localhost:{port}") as channel:
            stub = audio_pb2_grpc.VoiceConversionStub(channel)
            out = [resp.pcm async for resp in stub.Convert(_requests())]
    finally:
        await server.stop(grace=None)

    assert out == frames
