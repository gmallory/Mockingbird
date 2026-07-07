"""Prometheus metrics for the inference service (M7).

Served by ``GET /metrics`` on the health app. The conversion histogram is the
primary signal on the Grafana latency dashboard: its buckets bracket the 80ms
GPU-inference line from the ~172ms end-to-end budget (PRODUCT_SPEC §4).
"""

from prometheus_client import Counter, Gauge, Histogram

ACTIVE_STREAMS = Gauge(
    "mockingbird_inference_active_streams",
    "Currently open gRPC Convert streams.",
)

FRAMES_TOTAL = Counter(
    "mockingbird_inference_frames_total",
    "Audio frames through the Convert stream.",
    ["direction"],  # in (pushed by the gateway) | out (converted frames emitted)
)

# Observed only for push/flush calls that *emitted* audio — i.e. one sample per
# converted block (self_hosted) or per converted utterance (cartesia). Buffering
# pushes that return nothing are skipped so the histogram measures conversion
# latency, not queueing no-ops. Labelled by the backend class actually serving
# the stream (`self_hosted` and `cloud_gpu` both report SelfHostedBackend; the
# scrape target's job label tells the deployments apart).
CONVERT_SECONDS = Histogram(
    "mockingbird_inference_convert_seconds",
    "Wall time of a push/flush that emitted converted audio.",
    ["backend"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.08, 0.1, 0.15, 0.25, 0.5, 1.0, 2.5, 5.0),
)
