"""Export the OpenVoice V2 tone-color converter to the M5a ONNX contract.

Downloads the published converter checkpoint (myshell-ai/OpenVoiceV2, MIT) on
first run, exports the two template artifacts the instant-clone path needs
into ``{model_dir}/openvoice/``, and verifies both with ONNX Runtime:

- ``openvoice_converter.onnx``  — audio [1, N] @22050Hz → audio [1, M]; the
  target voice lives in the patchable ``tgt_se`` initializer
- ``openvoice_se_encoder.onnx`` — reference audio [1, N] → SE [1, 256, 1]

Optionally bakes a ready-to-stream voice from a reference clip.

Needs torch (kept out of the service deps):
    uv sync --group export
    uv run python scripts/export_openvoice_onnx.py [--model-dir models]
        [--voice my-voice --clip ref.wav]

Set SELF_HOSTED_MODEL_SAMPLE_RATE=22050 when serving these models.
"""

import argparse
import hashlib
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HF_BASE = "https://huggingface.co/myshell-ai/OpenVoiceV2/resolve/main/converter"
CACHE_DIR = Path.home() / ".cache" / "mockingbird" / "openvoice"
# Pinned sha256 of the published artifacts: a checkpoint swapped upstream (or a
# tampered download) fails loudly instead of exporting silently. If myshell-ai
# ever republishes, recompute — the checkpoint hash is in its LFS pointer:
#   curl -s https://huggingface.co/myshell-ai/OpenVoiceV2/raw/main/converter/checkpoint.pth
ARTIFACT_SHA256 = {
    "config.json": "9dfff60350b8c63f2c664efd92a61b2516efb22671466960f0e5dfebd881fa47",
    "checkpoint.pth": "9652c27e92b6b2a91632590ac9962ef7ae2b712e5c5b7f4c34ec55ee2b37ab9e",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path, sha256: str) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        if _sha256(dest) == sha256:
            return dest
        print(f"cached {dest} fails its checksum; re-downloading")
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url} -> {dest}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
    actual = _sha256(tmp)
    if actual != sha256:
        tmp.unlink()
        raise SystemExit(
            f"{url}: sha256 mismatch (expected {sha256}, got {actual}) — upstream "
            "changed or the download was tampered with; refusing to use it"
        )
    tmp.rename(dest)
    return dest


def _verify(model_dir: Path, sample_rate: int) -> None:
    """Run both exported graphs through ONNX Runtime and check the contract."""
    import numpy as np
    import onnxruntime as ort

    from app.export.openvoice_templates import CONVERTER_TEMPLATE, SE_ENCODER

    t = np.arange(sample_rate, dtype=np.float32) / sample_rate
    audio = (0.3 * np.sin(2 * np.pi * 220 * t)).reshape(1, -1).astype(np.float32)

    se_sess = ort.InferenceSession(str(model_dir / SE_ENCODER), providers=["CPUExecutionProvider"])
    (se,) = se_sess.run(None, {se_sess.get_inputs()[0].name: audio})
    assert se.shape == (1, 256, 1), f"unexpected SE shape {se.shape}"

    conv_sess = ort.InferenceSession(
        str(model_dir / CONVERTER_TEMPLATE), providers=["CPUExecutionProvider"]
    )
    inputs = [i.name for i in conv_sess.get_inputs()]
    assert inputs == ["audio"], f"converter must take only audio, got {inputs}"
    (out,) = conv_sess.run(None, {"audio": audio})
    assert out.ndim == 2 and out.shape[0] == 1, f"unexpected output shape {out.shape}"
    assert np.isfinite(out).all(), "converter produced non-finite audio"
    print(
        f"verified: se {se.shape}, converter {audio.shape[1]} samples in -> "
        f"{out.shape[1]} out ({sample_rate}Hz)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", default="models", help="SELF_HOSTED_MODEL_DIR")
    parser.add_argument("--checkpoint", help="local converter checkpoint.pth (skips download)")
    parser.add_argument("--config", help="local converter config.json (skips download)")
    parser.add_argument("--voice", help="also bake a voice with this label from --clip")
    parser.add_argument("--clip", help="reference clip for --voice (WAV, or anything ffmpeg reads)")
    args = parser.parse_args()
    if bool(args.voice) != bool(args.clip):
        parser.error("--voice and --clip must be given together")

    try:
        from app.export.openvoice_onnx import export_openvoice
    except ImportError as exc:
        raise SystemExit(f"torch export deps missing ({exc}); run: uv sync --group export") from exc

    # Local --config/--checkpoint paths are deliberate operator overrides and
    # skip the pin; the downloaded artifacts are always checksum-verified.
    config = (
        Path(args.config)
        if args.config
        else _download(
            f"{HF_BASE}/config.json", CACHE_DIR / "config.json", ARTIFACT_SHA256["config.json"]
        )
    )
    checkpoint = (
        Path(args.checkpoint)
        if args.checkpoint
        else _download(
            f"{HF_BASE}/checkpoint.pth",
            CACHE_DIR / "checkpoint.pth",
            ARTIFACT_SHA256["checkpoint.pth"],
        )
    )

    template_dir = Path(args.model_dir) / "openvoice"
    conv_path, se_path = export_openvoice(config, checkpoint, template_dir)
    print(f"exported {conv_path} and {se_path}")

    import json

    sample_rate = json.loads(config.read_text())["data"]["sampling_rate"]
    _verify(template_dir, sample_rate)

    if args.voice:
        from app.export.clone import clone_voice_local

        model_id = clone_voice_local(Path(args.clip).read_bytes(), args.voice, args.model_dir)
        print(f"baked voice model: {Path(args.model_dir) / (model_id + '.onnx')}")
        print(f"select it with model_id / voice_id = {model_id}")

    print(f"reminder: serve with SELF_HOSTED_MODEL_SAMPLE_RATE={sample_rate}")


if __name__ == "__main__":
    main()
