"""Name of the exported RVC placeholder-architecture artifact (M9).

Mirrors ``openvoice_templates.py``: shared between the torch export half
(:mod:`app.export.rvc.compose`) and the torch-free training half
(:mod:`app.export.hd_train`), kept in a leaf module so importing it never
pulls torch into the service process.
"""

RVC_TEMPLATE = "rvc_converter.onnx"
