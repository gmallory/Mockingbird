"""Latency benchmark for the self-hosted (ONNX Runtime) streaming backend.

Measures the "measured-then-locked" numbers ROADMAP M5 asks for: per-block
conversion latency of the self_hosted path, and the effective added latency
(block buffering + conversion) that sits inside the ~172ms end-to-end budget.

With no --model argument it generates a trivial identity graph, which measures
the **streaming-pipeline overhead floor** (blocking, float conversion, resample,
ORT session call) — not real voice-conversion inference. Point --model at real
exported RVC/OpenVoice weights (M5b) to measure the actual engine.

Usage:
    uv run python scripts/self_hosted_bench.py [--device auto|cuda|coreml|cpu]
        [--model path.onnx] [--seconds 30] [--block-ms 100] [--context-ms 200]
        [--model-sample-rate 48000]
"""

import argparse
import asyncio
import statistics
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backends.self_hosted import SelfHostedBackend  # noqa: E402

SAMPLE_RATE = 48000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


def _make_identity_model(path: Path) -> None:
    import onnx
    from onnx import TensorProto, helper

    inp = helper.make_tensor_value_info("audio", TensorProto.FLOAT, [1, None])
    out = helper.make_tensor_value_info("out", TensorProto.FLOAT, [1, None])
    node = helper.make_node("Identity", ["audio"], ["out"])
    graph = helper.make_graph([node], "identity", [inp], [out])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    onnx.save(model, str(path))


def _speechlike_frame(i: int) -> bytes:
    """20ms of a wandering tone — nonzero, non-constant, cheap to generate."""
    import math

    samples = [
        int(12000 * math.sin(2 * math.pi * (180 + 40 * math.sin(i / 7)) * t / SAMPLE_RATE))
        for t in range(i * FRAME_SAMPLES, (i + 1) * FRAME_SAMPLES)
    ]
    return struct.pack(f"<{FRAME_SAMPLES}h", *samples)


async def run(args: argparse.Namespace) -> None:
    tmp = None
    if args.model:
        model_path = Path(args.model)
        model_dir, model_id = model_path.parent, model_path.stem
    else:
        tmp = tempfile.TemporaryDirectory()
        model_dir = Path(tmp.name)
        model_id = "bench-identity"
        _make_identity_model(model_dir / f"{model_id}.onnx")
        print("no --model given: benchmarking pipeline overhead with an identity graph")

    backend = SelfHostedBackend(
        model_dir=str(model_dir),
        default_model=model_id,
        model_sample_rate=args.model_sample_rate,
        device=args.device,
        frame_ms=FRAME_MS,
        block_ms=args.block_ms,
        context_ms=args.context_ms,
    )
    session = backend.open_session()
    n_frames = args.seconds * 1000 // FRAME_MS
    block_frames = backend.block_frames

    # Warm up: first block includes ORT session creation / provider compile.
    for i in range(block_frames):
        await session.push(_speechlike_frame(i), SAMPLE_RATE, model_id)

    block_ms: list[float] = []
    out_frames = 0
    for i in range(block_frames, n_frames):
        frame = _speechlike_frame(i)
        t0 = time.perf_counter()
        out = await session.push(frame, SAMPLE_RATE, model_id)
        elapsed = (time.perf_counter() - t0) * 1000
        out_frames += len(out)
        if out:  # this push completed a block and ran inference
            block_ms.append(elapsed)
    out_frames += len(await session.flush())

    if tmp:
        tmp.cleanup()

    p50 = statistics.median(block_ms)
    p95 = statistics.quantiles(block_ms, n=20)[18]
    audio_s = (n_frames - block_frames) * FRAME_MS / 1000
    busy_s = sum(block_ms) / 1000
    print(
        f"\ndevice={args.device}  model={model_id}  block={args.block_ms}ms  "
        f"context={args.context_ms}ms  model_sr={args.model_sample_rate}"
    )
    print(f"blocks converted: {len(block_ms)}   output frames: {out_frames}")
    print(
        f"per-block conversion latency: p50={p50:.2f}ms  p95={p95:.2f}ms  max={max(block_ms):.2f}ms"
    )
    print(
        f"real-time factor: {busy_s / audio_s:.4f}x  "
        f"({busy_s * 1000:.0f}ms busy for {audio_s:.0f}s of audio)"
    )
    print(f"effective added latency (block buffer + p95 conversion): {args.block_ms + p95:.1f}ms")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="path to a .onnx voice model (default: identity graph)")
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "coreml", "cpu"])
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--block-ms", type=int, default=100)
    parser.add_argument("--context-ms", type=int, default=200)
    parser.add_argument("--model-sample-rate", type=int, default=48000)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
