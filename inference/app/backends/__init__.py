"""Backend selection.

``get_backend`` is called once at startup with the loaded settings and returns
the single backend instance the gRPC server uses for the process lifetime.
"""

from app.backends.base import InferenceBackend
from app.config import Settings


def get_backend(settings: Settings) -> InferenceBackend:
    name = settings.inference_backend

    if name == "passthrough":
        from app.backends.passthrough import PassthroughBackend

        return PassthroughBackend()

    if name == "cartesia":
        from app.backends.cartesia import CartesiaBackend

        if not settings.cartesia_api_key:
            raise RuntimeError("INFERENCE_BACKEND=cartesia requires CARTESIA_API_KEY")
        return CartesiaBackend(
            api_key=settings.cartesia_api_key,
            base_url=settings.cartesia_base_url,
            version=settings.cartesia_version,
            default_voice_id=settings.cartesia_voice_id,
        )

    if name == "self_hosted":
        raise NotImplementedError("the self_hosted GPU backend lands in a later milestone")

    raise ValueError(f"unknown INFERENCE_BACKEND: {name!r}")
