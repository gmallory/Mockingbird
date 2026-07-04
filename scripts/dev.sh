#!/usr/bin/env bash
# Run the full Mockingbird stack for local dev / the M4c demo in one command.
#
#   ./scripts/dev.sh
#
# Brings up Postgres + Redis (docker compose), applies gateway migrations, then
# starts the three app services and streams their logs. Ctrl-C stops everything.
#
#   inference  :8001 HTTP (clone) + :50051 gRPC (audio)   uvicorn app.health:app
#   gateway    :3001 WS + /voices                          uvicorn app.main:app
#   frontend   :3000 Studio + Monitor                      uvicorn app.main:app
#
# Inference uses Cartesia by default; CARTESIA_API_KEY is read from inference/.env.
# Override the backend with INFERENCE_BACKEND=passthrough ./scripts/dev.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export INFERENCE_BACKEND="${INFERENCE_BACKEND:-cartesia}"

if [[ "$INFERENCE_BACKEND" == "cartesia" && ! -f inference/.env ]]; then
  echo "warning: inference/.env not found — cartesia needs CARTESIA_API_KEY there" >&2
fi

echo "==> infra: postgres + redis"
docker compose -f infrastructure/docker-compose.yml up -d --wait

echo "==> gateway: alembic upgrade head"
uv run --directory gateway alembic upgrade head

pids=()
cleanup() {
  echo
  echo "==> stopping services"
  kill "${pids[@]}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> inference (:8001 HTTP + :50051 gRPC, backend=$INFERENCE_BACKEND)"
uv run --directory inference uvicorn app.health:app --port 8001 &
pids+=("$!")

echo "==> gateway (:3001)"
uv run --directory gateway uvicorn app.main:app --port 3001 &
pids+=("$!")

echo "==> frontend (:3000)"
uv run --directory frontend uvicorn app.main:app --port 3000 &
pids+=("$!")

echo
echo "Stack up — open:"
echo "  Studio:  http://localhost:3000/studio   (record -> clone your voice)"
echo "  Monitor: http://localhost:3000/          (pick the voice -> speak -> hear it)"
echo "  Ctrl-C to stop everything."
wait
