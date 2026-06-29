"""The backend interface every transform implements.

A backend converts one frame of Int16 PCM to another frame of Int16 PCM. The
gRPC server calls ``convert`` once per inbound frame and streams the result back
1:1. Keep implementations stateless per-call where possible; per-session state
(loaded models, warm connections) belongs on the backend instance.
"""

from abc import ABC, abstractmethod


class InferenceBackend(ABC):
    @abstractmethod
    async def convert(self, pcm: bytes, sample_rate: int, model_id: str) -> bytes:
        """Transform one frame of Int16 PCM and return Int16 PCM of the same shape.

        ``model_id`` is the target voice ("" means no model / passthrough).
        """
        ...

    async def aclose(self) -> None:  # noqa: B027 - optional teardown hook, no-op by default
        """Release any held resources (connections, GPU memory). Optional."""
