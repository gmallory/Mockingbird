# Mockingbird Inference

gRPC voice-conversion service. The gateway opens one bidirectional
`VoiceConversion.Convert` stream per session and proxies Int16 PCM frames here;
this service transforms each frame and streams it back 1:1.

The actual transform is chosen at startup by `INFERENCE_BACKEND`:

| Backend       | What it does                                         |
|---------------|------------------------------------------------------|
| `passthrough` | Returns audio unchanged. The M3 default; proves the gRPC hop and lets us measure loop latency with zero model cost. |
| `cartesia`    | Calls the Cartesia voice-changer API. Real transform, no GPU. Requires `CARTESIA_API_KEY`. |
| `self_hosted` | Reserved for the local GPU model (RVC/OpenVoice). Not implemented yet. |

## Run

```bash
uv sync
./scripts/gen_proto.sh                 # regenerate stubs after editing proto/audio.proto
INFERENCE_BACKEND=passthrough uv run uvicorn app.health:app --port 8001
```

`app.health:app` runs the FastAPI health endpoint on `:8001` **and** starts the
gRPC server on `:50051` (its lifespan owns the gRPC server).

## Test

```bash
uv run pytest
uv run ruff check
```

## Protocol

The contract lives in [`../proto/audio.proto`](../proto/audio.proto). Stubs are
generated into `app/proto_gen/` and committed; rerun `scripts/gen_proto.sh` when
the proto changes and keep it in sync with the WebSocket frame format in
`agents/AGENTS.md`.
