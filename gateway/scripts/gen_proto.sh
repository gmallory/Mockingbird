#!/usr/bin/env bash
# Regenerate gRPC stubs from the shared contract into app/proto_gen/.
# Run from anywhere; paths are resolved relative to this script.
set -euo pipefail

SVC_DIR="$(cd "$(dirname "$0")/.." && pwd)"   # gateway/
REPO_ROOT="$(cd "$SVC_DIR/.." && pwd)"
OUT_DIR="$SVC_DIR/app/proto_gen"

mkdir -p "$OUT_DIR"
touch "$OUT_DIR/__init__.py"

uv run python -m grpc_tools.protoc \
  -I "$REPO_ROOT/proto" \
  --python_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$REPO_ROOT/proto/audio.proto"

# protoc emits an absolute `import audio_pb2`; rewrite it to a package-relative
# import so the stubs work inside app.proto_gen. perl is portable across macOS/Linux.
perl -i -pe 's/^import audio_pb2/from . import audio_pb2/' "$OUT_DIR/audio_pb2_grpc.py"

echo "stubs written to $OUT_DIR"
