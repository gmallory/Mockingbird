"""Names of the exported OpenVoice template artifacts.

Shared between the torch export half (:mod:`app.export.openvoice_onnx`) and
the torch-free clone half (:mod:`app.export.clone`) — kept in a leaf module so
importing them never pulls torch into the service process.
"""

CONVERTER_TEMPLATE = "openvoice_converter.onnx"
SE_ENCODER = "openvoice_se_encoder.onnx"
