"""Torch → ONNX export of the composed RVC-style HD graph (M9).

Produces ``rvc_converter.onnx`` (:data:`app.export.rvc_templates.RVC_TEMPLATE`):
float32 mono audio ``[1, N]`` + a speaker conditioning tensor ``[1, 1, 1]`` in,
converted audio ``[1, M]`` out — satisfying the M5a self-hosted model contract
the same way ``app.export.openvoice_onnx`` does for the instant-clone tier.
``speaker`` is a graph *input* at export time (blocks constant folding from
smearing it into downstream weights, same reasoning as OpenVoice's
``tgt_se``); :func:`app.export.clone.bake_tgt_se` (generalized in M9 to take
any initializer name) then demotes it to a patchable initializer.

**This module composes real, exportable architecture with placeholder
weights — it is not a trained voice.** Three stages stand in for the real
building blocks a production RVC HD tier needs:

- :class:`ContentEncoderStub` stands in for a pretrained **HuBERT** content
  encoder (real: e.g. ``facebook/hubert-base-ls960`` via ``fairseq`` or
  ``transformers``, frozen, feeding a learned or FAISS-retrieved content
  sequence).
- :class:`F0EstimatorStub` stands in for real pitch tracking (e.g. CREPE,
  parselmouth/RMVPE), which upstream RVC uses to condition the synthesizer so
  pitch is preserved independent of the source speaker's range.
- :class:`SynthesizerStub` stands in for the RVC/VITS decoder — and, per
  PRODUCT_SPEC §4.2, **this is the piece that actually needs fine-tuning**:
  real HD quality comes from gradient descent on the target speaker's 10-30
  minute clip (a 30min-2hr GPU job), not from conditioning a shared decoder
  on a cheap embedding the way OpenVoice's zero-shot SE bake does. FAISS
  retrieval (steering content units toward the target speaker's timbre) is
  also part of a real RVC pipeline and is not implemented here.

None of the above is exportable to ONNX "for free" (no vendored checkpoint,
no GPU, no fine-tune loop) — which is exactly why this was flagged the
milestone's risk item, and why the real fine-tune run is M9's deferred tail.
What this module *does* prove: the three-stage composition really does export
to one ONNX graph, load in ONNX Runtime, and satisfy the streaming contract —
the architecural risk, if not yet the quality bar.

Requires torch (``uv sync --group export``). The live service never imports
this module — the torch-free stand-in it actually runs is
``app.export.hd_train``.
"""

from pathlib import Path

import torch
from torch import nn
from torch.nn import functional as F  # noqa: N812 - torch idiom

OPSET = 17
CONTENT_CHANNELS = 32
COND_CHANNELS = 16
SPEAKER_NAME = "speaker"


class ContentEncoderStub(nn.Module):
    """Placeholder for a HuBERT content encoder: audio [1, N] -> content [1, C, T]."""

    def __init__(self, channels: int = CONTENT_CHANNELS) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(1, channels, kernel_size=9, stride=4, padding=4),
            nn.GELU(),
            nn.Conv1d(channels, channels, kernel_size=9, stride=4, padding=4),
            nn.GELU(),
        )

    def forward(self, audio: torch.Tensor) -> torch.Tensor:
        return self.net(audio.unsqueeze(1))


class F0EstimatorStub(nn.Module):
    """Placeholder for pitch tracking: audio [1, N] -> pitch contour [1, 1, T]."""

    def __init__(self) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(1, 8, kernel_size=9, stride=4, padding=4),
            nn.GELU(),
            nn.Conv1d(8, 1, kernel_size=9, stride=4, padding=4),
            nn.Softplus(),  # pitch is non-negative
        )

    def forward(self, audio: torch.Tensor) -> torch.Tensor:
        return self.net(audio.unsqueeze(1))


class SynthesizerStub(nn.Module):
    """Placeholder for the RVC/VITS decoder: (content, f0, speaker) -> audio.

    Real HD quality comes from fine-tuning *this* stage on the target
    speaker — see the module docstring.
    """

    def __init__(
        self, content_channels: int = CONTENT_CHANNELS, cond_channels: int = COND_CHANNELS
    ) -> None:
        super().__init__()
        self.speaker = nn.Conv1d(1, cond_channels, kernel_size=1)
        self.pre = nn.Conv1d(content_channels + cond_channels + 1, 32, kernel_size=3, padding=1)
        self.up = nn.Sequential(
            nn.ConvTranspose1d(32, 16, kernel_size=8, stride=4, padding=2),
            nn.GELU(),
            nn.ConvTranspose1d(16, 1, kernel_size=8, stride=4, padding=2),
            nn.Tanh(),
        )

    def forward(
        self, content: torch.Tensor, f0: torch.Tensor, speaker: torch.Tensor
    ) -> torch.Tensor:
        spk = self.speaker(speaker).expand(-1, -1, content.shape[-1])
        f0 = F.interpolate(f0, size=content.shape[-1], mode="linear", align_corners=False)
        x = torch.cat([content, f0, spk], dim=1)
        return self.up(F.gelu(self.pre(x)))


class RVCComposedGraph(nn.Module):
    """The single exportable graph: audio [1, N], speaker [1, 1, 1] -> audio [1, M]."""

    def __init__(self) -> None:
        super().__init__()
        self.content_encoder = ContentEncoderStub()
        self.f0_estimator = F0EstimatorStub()
        self.synthesizer = SynthesizerStub()

    def forward(self, audio: torch.Tensor, speaker: torch.Tensor) -> torch.Tensor:
        content = self.content_encoder(audio)
        f0 = self.f0_estimator(audio)
        out = self.synthesizer(content, f0, speaker)
        return out.squeeze(1)


def export_rvc(out_path: Path, sample_seconds: float = 1.0, sample_rate: int = 22050) -> Path:
    """Export a placeholder-weight :class:`RVCComposedGraph` to ONNX.

    Proves the composed-graph shape satisfies the M5a contract end to end
    (torch -> ONNX -> ORT). Also demotes ``speaker`` to a patchable
    initializer (mirroring ``export_openvoice``'s ``tgt_se`` handling) so the
    exported file is immediately usable by
    ``app.export.clone.bake_tgt_se(..., tensor_name="speaker")``.
    """
    from app.export.clone import bake_tgt_se

    model = RVCComposedGraph()
    model.eval()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dummy_audio = torch.zeros(1, int(sample_rate * sample_seconds), dtype=torch.float32)
    dummy_audio[0, :: max(1, sample_rate // 100)] = 0.5  # impulses: exercise every op
    dummy_speaker = torch.zeros(1, 1, 1, dtype=torch.float32)
    with torch.no_grad():
        torch.onnx.export(
            model,
            (dummy_audio, dummy_speaker),
            str(out_path),
            input_names=["audio", SPEAKER_NAME],
            output_names=["audio_out"],
            dynamic_axes={"audio": {1: "n_samples"}, "audio_out": {1: "n_out"}},
            opset_version=OPSET,
            do_constant_folding=True,
            dynamo=False,
        )
    import numpy as np

    bake_tgt_se(out_path, np.zeros((1, 1, 1), dtype=np.float32), out_path, tensor_name=SPEAKER_NAME)
    return out_path
