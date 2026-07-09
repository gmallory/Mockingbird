# Mockingbird Inference

gRPC voice-conversion service. The gateway opens one bidirectional
`VoiceConversion.Convert` stream per session and proxies Int16 PCM frames here.
Each stream gets one per-stream `BackendSession`: input frames are pushed in and
output frames are streamed back. Conversion is **not** required to be 1:1 — a
clip-based backend buffers a whole utterance and emits a burst of output frames
once the speaker pauses.

The actual transform is chosen at startup by `INFERENCE_BACKEND`:

| Backend       | What it does                                                                                                                                                                                                                                                                                                                                                    |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `passthrough` | Returns audio unchanged, one frame out per frame in. The default; proves the gRPC hop and lets us measure loop latency with zero model cost.                                                                                                                                                                                                                    |
| `cartesia`    | Real cloud voice changer, no GPU. Cartesia's voice changer is clip-based, so this backend groups frames into utterances with a simple energy VAD and converts each on a trailing pause via `/voice-changer/sse` (walkie-talkie feel, not per-frame streaming). Requires `CARTESIA_API_KEY` and a target voice (`CARTESIA_VOICE_ID` or a per-session `modelId`). |
| `self_hosted` | **Primary engine.** Block-streaming ONNX Runtime voice conversion (M5a) running exported OpenVoice V2 weights (M5b): 60ms blocks + 140ms left context per inference, 5ms seam crossfade, per-voice `{model_id}.onnx` files from `SELF_HOSTED_MODEL_DIR` or S3.                                                                                                  |
| `cloud_gpu`   | The exact same backend, deployed on a rented GPU box (see `../infrastructure/scripts/provision_cloud_gpu.sh`); the gateway dials that box's gRPC endpoint.                                                                                                                                                                                                      |

## Run

```bash
uv sync
./scripts/gen_proto.sh                 # regenerate stubs after editing proto/audio.proto
INFERENCE_BACKEND=passthrough uv run uvicorn app.health:app --port 8001
```

## Real voice models (M5b)

One-time export of the OpenVoice V2 converter (torch stays out of the service —
it lives in the `export` dependency group):

```bash
uv sync --group export
uv run python scripts/export_openvoice_onnx.py --model-dir models
```

That writes `models/openvoice/openvoice_converter.onnx` (voice template) and
`models/openvoice/openvoice_se_encoder.onnx`. Then serve:

```bash
INFERENCE_BACKEND=self_hosted SELF_HOSTED_MODEL_SAMPLE_RATE=22050 \
  uv run uvicorn app.health:app --port 8001
```

`POST /voices` (multipart `clip` + `name` + `language`) instant-clones a voice:
it runs the SE encoder over the clip and bakes the speaker embedding into a copy
of the template as `models/{model_id}.onnx`; the returned `voice_id` is that
`model_id`, selectable per-stream via `switch_model`. Non-WAV clips (browser
webm/opus, m4a) need `ffmpeg` on PATH — the Dockerfile installs it.

Benchmark real weights (`scripts/self_hosted_bench.py --model
models/<id>.onnx --model-sample-rate 22050`); measured numbers live in
`../docs/PRODUCT_SPEC.md` §4.1.

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
