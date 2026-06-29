"""Passthrough backend: returns audio unchanged.

The default backend. It proves the full path (browser -> gateway -> gRPC ->
inference -> back) and lets us measure loop latency *including the gRPC hop* with
zero model cost. Its session is 1:1 — one output frame per input frame — so the
echo loop behaves exactly as it did before the per-stream session abstraction.
"""

from app.backends.base import BackendSession, InferenceBackend


class _PassthroughSession(BackendSession):
    async def push(self, pcm: bytes, sample_rate: int, model_id: str) -> list[bytes]:
        return [pcm]


class PassthroughBackend(InferenceBackend):
    def open_session(self) -> BackendSession:
        return _PassthroughSession()
