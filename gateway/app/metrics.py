"""Prometheus metrics for the gateway (M7).

All metrics live on the default registry and are served by ``GET /metrics``
(see ``app.main``). Naming follows prometheus conventions:
``mockingbird_gateway_<subsystem>_<what>_<unit>``.

The latency budget context (PRODUCT_SPEC §4): the end-to-end target is ~172ms,
so the first-output histogram buckets bracket that line — the p95 panel on the
Grafana latency dashboard reads straight off these buckets.
"""

from prometheus_client import Counter, Gauge, Histogram

# Session lifecycle. `outcome` is decided at the auth/limit gate:
#   accepted     — session entered the receive loop
#   rejected     — bad/missing token, closed 4001
#   rate_limited — over the plan cap, closed 4029
WS_SESSIONS_TOTAL = Counter(
    "mockingbird_gateway_ws_sessions_total",
    "WebSocket /ws/voice sessions by gate outcome.",
    ["outcome"],
)

WS_SESSIONS_ACTIVE = Gauge(
    "mockingbird_gateway_ws_sessions_active",
    "Currently open /ws/voice sessions.",
    ["mode"],  # authenticated | anonymous
)

WS_SESSION_SECONDS = Histogram(
    "mockingbird_gateway_ws_session_seconds",
    "Duration of a /ws/voice session from accept to close.",
    buckets=(1, 5, 15, 60, 300, 900, 1800, 3600),
)

WS_FRAMES_TOTAL = Counter(
    "mockingbird_gateway_ws_audio_frames_total",
    "Binary audio frames through /ws/voice.",
    ["direction"],  # in (browser->gateway) | out (gateway->browser)
)

WS_DEGRADED_TOTAL = Counter(
    "mockingbird_gateway_ws_degraded_sessions_total",
    "Sessions that fell back to passthrough echo because inference was unreachable.",
)

# Startup latency of the conversion path: first inbound audio frame (which
# triggers the lazy inference dial) to the first *converted* frame back from
# the reader. Passthrough echoes after degrade never count.
WS_FIRST_OUTPUT_SECONDS = Histogram(
    "mockingbird_gateway_ws_first_output_seconds",
    "Time from the session's first inbound audio frame to its first converted output frame.",
    buckets=(0.025, 0.05, 0.075, 0.1, 0.172, 0.25, 0.5, 1.0, 2.5, 5.0),
)
