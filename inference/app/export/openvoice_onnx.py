"""Torch → ONNX export of the OpenVoice V2 tone-color converter (M5b).

Produces the two artifacts the torch-free instant-clone path
(:mod:`app.export.clone`) consumes:

- ``openvoice_converter.onnx`` — the **voice template**: float32 mono audio
  ``[1, N]`` at 22.05kHz in, converted audio ``[1, M]`` out, satisfying the
  M5a self-hosted model contract. The *source* speaker embedding is computed
  in-graph from the incoming audio itself; the *target* speaker embedding is
  the ``tgt_se`` initializer, which cloning overwrites per voice.
- ``openvoice_se_encoder.onnx`` — reference clip ``[1, N]`` → speaker
  embedding ``[1, 256, 1]`` (the converter's own reference encoder), used to
  compute the ``tgt_se`` that gets baked into a cloned voice.

Deliberate deviations from upstream inference, both required for a single
deterministic streaming graph: the posterior encoder runs at ``tau=0`` (mean,
no sampling — upstream uses ``tau=0.3``), and the source SE is re-derived per
block from the ~260ms window the backend feeds the model rather than from a
long reference recording.

Requires torch (``uv sync --group export``). The live service never imports
this module.
"""

import json
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F  # noqa: N812 - torch idiom

from app.export.openvoice.models import SynthesizerTrn
from app.export.openvoice_templates import CONVERTER_TEMPLATE, SE_ENCODER

TGT_SE_NAME = "tgt_se"
GIN_CHANNELS = 256
OPSET = 17


class ConvSTFT(nn.Module):
    """Linear-magnitude STFT as a strided conv1d (exportable; torch.stft is not).

    Mirrors upstream ``mel_processing.spectrogram_torch_conv`` (which asserts
    equality with ``torch.stft``): hann window, reflect padding of
    ``(n_fft - hop) / 2`` per side, ``sqrt(re² + im² + 1e-6)`` magnitude.

    Reflect padding requires the input to be longer than one pad span
    (``(n_fft - hop) / 2`` samples ≈ 17ms at 22050Hz) — shorter windows fail
    inside ORT. The streaming backend's smallest window (one 20ms frame) and
    the clone path's 1s minimum clip both clear it.
    """

    def __init__(self, n_fft: int, hop_length: int, win_length: int) -> None:
        super().__init__()
        if win_length != n_fft:
            raise NotImplementedError("win_length != n_fft is not needed for OpenVoice V2")
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.pad = (n_fft - hop_length) // 2
        freq_cutoff = n_fft // 2 + 1
        fourier_basis = torch.view_as_real(torch.fft.fft(torch.eye(n_fft)))
        forward_basis = (
            fourier_basis[:freq_cutoff].permute(2, 0, 1).reshape(-1, 1, n_fft)
        ) * torch.hann_window(win_length)
        self.register_buffer("basis", forward_basis.float())
        self.freq_cutoff = freq_cutoff

    def forward(self, audio: torch.Tensor) -> torch.Tensor:
        """[1, N] audio → [1, n_fft//2+1, T] linear magnitude spectrogram."""
        y = F.pad(audio.unsqueeze(1), (self.pad, self.pad), mode="reflect")
        proj = F.conv1d(y, self.basis, stride=self.hop_length)
        real, imag = proj[:, : self.freq_cutoff, :], proj[:, self.freq_cutoff :, :]
        return torch.sqrt(real * real + imag * imag + 1e-6)


class SpeakerEncoderModule(nn.Module):
    """Audio [1, N] → speaker embedding [1, 256, 1] via the converter's ref_enc."""

    def __init__(self, model: SynthesizerTrn, stft: ConvSTFT) -> None:
        super().__init__()
        self.model = model
        self.stft = stft

    def forward(self, audio: torch.Tensor) -> torch.Tensor:
        spec = self.stft(audio)
        return self.model.ref_enc(spec.transpose(1, 2)).unsqueeze(-1)


class ConverterModule(nn.Module):
    """Audio [1, N] → converted audio [1, M], target voice given by ``tgt_se``.

    ``tgt_se`` is a graph *input* at export time (so constant folding cannot
    smear it into downstream weights); :func:`app.export.clone.bake_tgt_se`
    then turns it into a patchable initializer.

    The masked-length machinery from upstream collapses to all-ones masks here
    (the backend always sends fully valid blocks), so the posterior encoder is
    inlined with ``x_mask = 1`` instead of calling ``voice_conversion`` with a
    dynamic length tensor.
    """

    def __init__(self, model: SynthesizerTrn, stft: ConvSTFT) -> None:
        super().__init__()
        self.model = model
        self.stft = stft

    def forward(self, audio: torch.Tensor, tgt_se: torch.Tensor) -> torch.Tensor:
        m = self.model
        spec = self.stft(audio)
        x_mask = torch.ones_like(spec[:, :1, :])

        g_src = m.ref_enc(spec.transpose(1, 2)).unsqueeze(-1)
        g_zero = torch.zeros_like(g_src)
        g_enc_dec = g_zero if m.zero_g else g_src

        # enc_q at tau=0 (deterministic): z = mean of the posterior.
        h = m.enc_q.pre(spec) * x_mask
        h = m.enc_q.enc(h, x_mask, g=g_enc_dec)
        stats = m.enc_q.proj(h) * x_mask
        z, _logs = torch.split(stats, m.enc_q.out_channels, dim=1)

        z_p = m.flow(z, x_mask, g=g_src)
        z_hat = m.flow(z_p, x_mask, g=tgt_se, reverse=True)
        o = m.dec(z_hat * x_mask, g=g_zero if m.zero_g else tgt_se)
        return o.squeeze(1)


def load_converter(config_path: Path, checkpoint_path: Path) -> tuple[SynthesizerTrn, dict]:
    """Build the converter from its config and load the checkpoint weights."""
    hps = json.loads(config_path.read_text())
    model = SynthesizerTrn(
        0,
        hps["data"]["filter_length"] // 2 + 1,
        n_speakers=hps["data"]["n_speakers"],
        **hps["model"],
    )
    model.eval()
    state = torch.load(checkpoint_path, map_location="cpu", weights_only=True)["model"]
    missing, unexpected = model.load_state_dict(state, strict=False)
    if missing:
        raise RuntimeError(f"checkpoint is missing converter weights: {missing[:8]}...")
    if unexpected:
        # TTS-side weights we deliberately dropped (enc_p, duration predictors).
        print(f"ignoring {len(unexpected)} non-converter checkpoint keys")
    return model, hps


def _export(
    module: nn.Module,
    args: tuple,
    path: Path,
    input_names: list[str],
    output_name: str,
    dynamic_axes: dict,
) -> None:
    torch.onnx.export(
        module,
        args,
        str(path),
        input_names=input_names,
        output_names=[output_name],
        dynamic_axes=dynamic_axes,
        opset_version=OPSET,
        do_constant_folding=True,
        dynamo=False,
    )


def export_openvoice(
    config_path: Path, checkpoint_path: Path, out_dir: Path, sample_seconds: float = 1.0
) -> tuple[Path, Path]:
    """Export converter template + SE encoder; returns their paths."""
    # Torch-free half of the pipeline; imported here so this module stays the
    # only place with a hard torch dependency.
    from app.export.clone import bake_tgt_se

    model, hps = load_converter(config_path, checkpoint_path)
    sr = hps["data"]["sampling_rate"]
    stft = ConvSTFT(
        hps["data"]["filter_length"], hps["data"]["hop_length"], hps["data"]["win_length"]
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    dummy = torch.zeros(1, int(sr * sample_seconds), dtype=torch.float32)
    dummy[0, :: sr // 100] = 0.5  # impulses: keep every op on a non-trivial path

    se_path = out_dir / SE_ENCODER
    with torch.no_grad():
        _export(
            SpeakerEncoderModule(model, stft),
            (dummy,),
            se_path,
            ["audio"],
            "se",
            {"audio": {1: "n_samples"}},
        )

    conv_path = out_dir / CONVERTER_TEMPLATE
    dummy_se = torch.zeros(1, GIN_CHANNELS, 1, dtype=torch.float32)
    with torch.no_grad():
        _export(
            ConverterModule(model, stft),
            (dummy, dummy_se),
            conv_path,
            ["audio", TGT_SE_NAME],
            "audio_out",
            {"audio": {1: "n_samples"}, "audio_out": {1: "n_out"}},
        )

    # Turn tgt_se from a required input into a patchable initializer default.
    bake_tgt_se(conv_path, np.zeros((1, GIN_CHANNELS, 1), dtype=np.float32), conv_path)
    return conv_path, se_path
