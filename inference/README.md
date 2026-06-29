# Mockingbird Inference

gRPC voice-conversion service. The gateway opens one bidirectional
`VoiceConversion.Convert` stream per session and proxies Int16 PCM frames here.
Each stream gets one per-stream `BackendSession`: input frames are pushed in and
output frames are streamed back. Conversion is **not** required to be 1:1 — a
clip-based backend buffers a whole utterance and emits a burst of output frames
once the speaker pauses.

The actual transform is chosen at startup by `INFERENCE_BACKEND`:

| Backend       | What it does                                         |
|---------------|------------------------------------------------------|
| `passthrough` | Returns audio unchanged, one frame out per frame in. The default; proves the gRPC hop and lets us measure loop latency with zero model cost. |
| `cartesia`    | Real cloud voice changer, no GPU. Cartesia's voice changer is clip-based, so this backend groups frames into utterances with a simple energy VAD and converts each on a trailing pause via `/voice-changer/sse` (walkie-talkie feel, not per-frame streaming). Requires `CARTESIA_API_KEY` and a target voice (`CARTESIA_VOICE_ID` or a per-session `modelId`). |
| `self_hosted` | Reserved for the local GPU model (RVC/OpenVoice), the true low-latency per-frame path. Not implemented yet. |

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
