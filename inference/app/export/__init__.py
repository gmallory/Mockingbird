"""Voice-model export + instant-clone tooling (M5b).

Two halves, split by dependency weight:

- ``app.export.openvoice`` + ``app.export.openvoice_onnx`` need **torch** (the
  ``export`` uv dependency group) and are only imported by the offline export
  script. They turn the OpenVoice V2 converter checkpoint into ONNX artifacts
  that satisfy the M5a self-hosted model contract.
- ``app.export.clone`` is **torch-free** (numpy + onnx + onnxruntime, all
  service deps) and runs inside the inference service: it turns an uploaded
  reference clip into a per-voice ``{model_id}.onnx`` by patching the target
  speaker embedding into the exported converter template.
"""
