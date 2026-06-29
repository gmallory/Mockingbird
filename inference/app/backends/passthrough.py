"""Passthrough backend: returns audio unchanged.

This is the M3 default. It proves the full path (browser -> gateway -> gRPC ->
inference -> back) and lets us measure loop latency *including the gRPC hop* with
zero model cost, before any real transform is swapped in.
"""

from app.backends.base import InferenceBackend


class PassthroughBackend(InferenceBackend):
    async def convert(self, pcm: bytes, sample_rate: int, model_id: str) -> bytes:
        return pcm
