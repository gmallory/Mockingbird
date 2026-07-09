"""RVC-style HD voice-conversion graph composition (M9).

Composes three conceptual stages — a HuBERT-style content encoder, an F0
(pitch) estimator, and an RVC/VITS-style synthesizer — into a single
audio-in -> audio-out ONNX graph satisfying the M5a self-hosted model
contract, the same shape ``app.export.openvoice_onnx`` produces for the
instant-clone tier.

**This package ships placeholder-weight architecture, not a trained voice.**
See :mod:`app.export.rvc.compose` for exactly what real pretrained weights
the GPU-quality tail needs — that GPU fine-tune run is M9's deferred tail
(PRODUCT_SPEC §15 criterion 4), same posture as M5b's rented-GPU bench and
M8a's live-Twilio run.

Requires torch — install the ``export`` dependency group. The live inference
service never imports this package; the torch-free stand-in it actually runs
is :mod:`app.export.hd_train`.
"""
