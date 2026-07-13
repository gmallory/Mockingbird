#!/usr/bin/env bash
# Provision a rented GPU box (A10G/L4, Ubuntu 22.04+ w/ NVIDIA driver — the
# stock Lambda Labs / RunPod / EC2 g5|g6 images) as a Mockingbird `cloud_gpu`
# inference node, then measure the real per-block latency that PRODUCT_SPEC
# §4.1 wants locked ("measured-then-locked" 80ms GPU inference line).
#
# Run ON the GPU box:
#   git clone <repo> && cd Mockingbird && bash infrastructure/scripts/provision_cloud_gpu.sh
#
# What it does:
#   1. installs uv, syncs the inference service env
#   2. swaps onnxruntime for onnxruntime-gpu (CUDA execution provider)
#   3. exports the OpenVoice V2 converter (downloads the 131MB checkpoint)
#   4. runs scripts/self_hosted_bench.py on the real weights, CUDA vs CPU
#   5. prints the env the gateway needs to dial this box
#
# Afterwards, start serving with:
#   cd inference && INFERENCE_BACKEND=cloud_gpu DEVICE=cuda \
#     SELF_HOSTED_MODEL_SAMPLE_RATE=22050 uv run uvicorn app.health:app --port 8001
# and point the gateway's INFERENCE_GRPC_URL at this box's :50051.
set -euo pipefail

cd "$(dirname "$0")/../../inference"

if ! command -v nvidia-smi >/dev/null; then
  echo "nvidia-smi not found — this script expects a box with an NVIDIA driver" >&2
  exit 1
fi
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

if ! command -v uv >/dev/null; then
  # Pinned + checksum-verified installer (no blind `curl | sh`). To bump: set the
  # new version, then refresh the hash with
  #   curl -LsSf https://astral.sh/uv/<version>/install.sh | sha256sum
  UV_VERSION="0.11.28"
  UV_INSTALL_SHA256="b7b3fe80cad1142a2a5794050b7db7b3291d1bac1423b0732571dd9366e8ca8b"
  installer="$(mktemp)"
  curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" -o "$installer"
  echo "${UV_INSTALL_SHA256}  ${installer}" | sha256sum -c -
  sh "$installer"
  rm -f "$installer"
  export PATH="$HOME/.local/bin:$PATH"
fi

uv sync --group export

# onnxruntime-gpu replaces the CPU wheel (same import name, adds the CUDA EP).
# Kept out of pyproject: dev machines are Macs where the GPU wheel won't build.
uv pip install "onnxruntime-gpu>=1.27.0"

# Real weights: exports models/openvoice/* templates and verifies them.
uv run python scripts/export_openvoice_onnx.py --model-dir models \
  --voice bench-voice --clip "${BENCH_CLIP:-../target_gm_01_mockingbird_details.m4a}" ||
  uv run python scripts/export_openvoice_onnx.py --model-dir models

MODEL=$(ls models/ov2-*.onnx 2>/dev/null | head -1)
if [ -z "$MODEL" ]; then
  echo "no baked voice model (needs ffmpeg for non-WAV clips: apt install ffmpeg);" >&2
  echo "falling back to benchmarking the raw converter template" >&2
  MODEL=models/openvoice/openvoice_converter.onnx
fi

echo "=== bench: CUDA ==="
uv run python scripts/self_hosted_bench.py --model "$MODEL" \
  --model-sample-rate 22050 --device cuda --seconds 30
echo "=== bench: CPU (same box, for comparison) ==="
uv run python scripts/self_hosted_bench.py --model "$MODEL" \
  --model-sample-rate 22050 --device cpu --seconds 30

cat <<EOF

Record the CUDA numbers in docs/PRODUCT_SPEC.md §4.1 (the 80ms GPU inference
line is "measured-then-locked").

Gateway side (.env):
  INFERENCE_GRPC_URL=<this box's address>:50051
This box (.env or inline):
  INFERENCE_BACKEND=cloud_gpu  DEVICE=cuda  SELF_HOSTED_MODEL_SAMPLE_RATE=22050
EOF
