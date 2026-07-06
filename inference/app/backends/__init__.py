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
            frame_ms=settings.frame_ms,
            energy_threshold=settings.vad_energy_threshold,
            silence_ms=settings.vad_silence_ms,
            max_utterance_ms=settings.vad_max_utterance_ms,
            preroll_ms=settings.vad_preroll_ms,
        )

    if name in ("self_hosted", "cloud_gpu"):
        # cloud_gpu is the same stack as self_hosted, deployed on a rented GPU
        # box — the mode value exists so deploys are explicit about which shape
        # they are; the in-process backend is identical.
        from app.backends.self_hosted import SelfHostedBackend

        return SelfHostedBackend(
            model_dir=settings.self_hosted_model_dir,
            default_model=settings.self_hosted_default_model,
            model_sample_rate=settings.self_hosted_model_sample_rate,
            device=settings.device,
            frame_ms=settings.frame_ms,
            block_ms=settings.self_hosted_block_ms,
            context_ms=settings.self_hosted_context_ms,
            crossfade_ms=settings.self_hosted_crossfade_ms,
            max_loaded_models=settings.self_hosted_max_loaded_models,
            s3_endpoint=settings.s3_endpoint,
            s3_bucket=settings.s3_bucket,
        )

    if name == "elevenlabs":
        raise NotImplementedError("the elevenlabs backend is a placeholder mode (not yet built)")

    raise ValueError(f"unknown INFERENCE_BACKEND: {name!r}")
