"""The backend interface every transform implements.

A backend opens one :class:`BackendSession` per gRPC ``Convert`` stream. The
session is fed 20ms Int16 PCM frames via :meth:`BackendSession.push` and returns
zero or more output frames that are ready to send back **now**.

Conversion is deliberately **not** required to be 1:1. A frame-level transform
(passthrough, and later self-hosted RVC) returns one output frame per input
frame. A clip-based cloud transform (Cartesia) buffers many input frames
silently while a speaker talks, then emits a burst of output frames once the
utterance completes. :meth:`BackendSession.flush` drains any audio still buffered
when the stream ends.
"""

from abc import ABC, abstractmethod


class BackendSession(ABC):
    """Per-stream conversion state. One instance per gRPC ``Convert`` stream."""

    @abstractmethod
    async def push(self, pcm: bytes, sample_rate: int, model_id: str) -> list[bytes]:
        """Feed one input frame; return 0+ output frames ready to send now.

        ``model_id`` is the target voice ("" means no model / passthrough).
        """
        ...

    async def flush(self) -> list[bytes]:  # noqa: B027 - optional, default drains nothing
        """Emit any audio still buffered at end of stream. Optional."""
        return []

    async def aclose(self) -> None:  # noqa: B027 - optional teardown hook, no-op by default
        """Release per-session resources. Optional."""


class InferenceBackend(ABC):
    @abstractmethod
    def open_session(self) -> BackendSession:
        """Create a fresh per-stream session (one per gRPC ``Convert`` stream)."""
        ...

    async def aclose(self) -> None:  # noqa: B027 - optional teardown hook, no-op by default
        """Release process-level resources (shared clients, GPU memory). Optional."""
