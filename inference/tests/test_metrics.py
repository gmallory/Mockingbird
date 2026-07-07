"""Prometheus metrics (M7): /metrics endpoint + Convert stream instrumentation.

Metrics live on the process-global default registry, so assertions are deltas
around the action under test, never absolute values. Reads go through the
public ``REGISTRY.get_sample_value`` API rather than child internals.
"""

import httpx
from prometheus_client import REGISTRY

from app.backends.passthrough import PassthroughBackend
from app.health import app
from app.proto_gen import audio_pb2
from app.server import VoiceConversionServicer


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


async def test_metrics_endpoint_serves_prometheus_exposition():
    # No lifespan on purpose: the route needs no backend or gRPC server.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "mockingbird_inference_convert_seconds" in resp.text
    assert "mockingbird_inference_active_streams" in resp.text


async def test_convert_stream_records_frames_and_latency():
    servicer = VoiceConversionServicer(PassthroughBackend())
    frames = [bytes([i]) * 1920 for i in range(3)]

    async def _requests():
        for pcm in frames:
            yield audio_pb2.AudioFrame(pcm=pcm, sample_rate=48000, model_id="")

    in_before = _sample("mockingbird_inference_frames_total", direction="in")
    out_before = _sample("mockingbird_inference_frames_total", direction="out")
    observed_before = _sample(
        "mockingbird_inference_convert_seconds_count", backend="PassthroughBackend"
    )
    active_before = _sample("mockingbird_inference_active_streams")

    out = [resp.pcm async for resp in servicer.Convert(_requests(), context=None)]

    assert out == frames
    assert _sample("mockingbird_inference_frames_total", direction="in") == in_before + 3
    assert _sample("mockingbird_inference_frames_total", direction="out") == out_before + 3
    # Passthrough emits on every push: one histogram observation per frame.
    assert (
        _sample("mockingbird_inference_convert_seconds_count", backend="PassthroughBackend")
        == observed_before + 3
    )
    # Stream finished; the gauge nets back to where it started.
    assert _sample("mockingbird_inference_active_streams") == active_before
