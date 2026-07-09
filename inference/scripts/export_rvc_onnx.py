"""Export the composed RVC-style HD graph to the M5a ONNX contract (M9).

This is the entrypoint for the *real* GPU fine-tune + export step
(PRODUCT_SPEC §4.2's Training -> Export stages) — M9's deferred tail (needs a
rented GPU + real pretrained HuBERT/F0/RVC checkpoints; see the module
docstring in ``app/export/rvc/compose.py`` for exactly what is still missing
and why this is the milestone's flagged risk item).

Today, with no real checkpoints wired in, this script exports the
placeholder-weight composed graph — genuinely runnable end to end (torch ->
ONNX -> ONNX Runtime), just not yet a trained voice — and verifies it. The
result is written to ``{model_dir}/rvc/rvc_converter.onnx``
(``app.export.rvc_templates.RVC_TEMPLATE``), the shared template
``app.export.hd_train.hd_train_local`` looks for: once it exists,
``POST /train_hd`` stops writing synthetic stand-ins and instead bakes each
clip's (currently torch-free, crude) speaker conditioning into a copy of this
template, mirroring how ``scripts/export_openvoice_onnx.py`` bakes ``tgt_se``.

Needs torch (kept out of the service deps):
    uv sync --group export
    uv run python scripts/export_rvc_onnx.py [--model-dir models]

The live inference service never imports this script or app/export/rvc/.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _verify(path: Path, sample_rate: int) -> None:
    import numpy as np
    import onnxruntime as ort

    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    names = [i.name for i in session.get_inputs()]
    assert names == ["audio"], f"expected a single baked 'audio' input, got {names}"
    audio = np.zeros((1, sample_rate), dtype=np.float32)
    (out,) = session.run(None, {"audio": audio})
    assert out.ndim == 2 and out.shape[0] == 1, f"unexpected output shape {out.shape}"
    assert np.isfinite(out).all(), "graph produced non-finite audio"
    print(f"verified: {audio.shape[1]} samples in -> {out.shape[1]} out ({sample_rate}Hz)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", default="models", help="SELF_HOSTED_MODEL_DIR")
    parser.add_argument(
        "--sample-rate", type=int, default=22050, help="must match SELF_HOSTED_MODEL_SAMPLE_RATE"
    )
    args = parser.parse_args()

    try:
        from app.export.rvc.compose import export_rvc
    except ImportError as exc:
        raise SystemExit(f"torch export deps missing ({exc}); run: uv sync --group export") from exc

    from app.export.rvc_templates import RVC_TEMPLATE

    out_path = Path(args.model_dir) / "rvc" / RVC_TEMPLATE
    export_rvc(out_path, sample_rate=args.sample_rate)
    _verify(out_path, args.sample_rate)
    print(
        "reminder: this graph has placeholder architecture weights, not a real "
        "fine-tune — see app/export/rvc/compose.py for what a production run on "
        "the rented GPU box needs (real HuBERT/F0/RVC checkpoints + an actual "
        "per-speaker fine-tune loop, PRODUCT_SPEC §4.2)"
    )


if __name__ == "__main__":
    main()
