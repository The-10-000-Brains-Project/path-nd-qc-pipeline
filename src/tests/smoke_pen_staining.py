"""Smoke test — pen inference plumbing and staining-support invariants.

Contracts: pen/README.md and staining/README.md. A pointwise Torch model replaces learned
inference; no weights are downloaded and no model-accuracy claims are made.
Run: python src/tests/smoke_pen_staining.py
"""

from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch
import numpy as np
from PIL import Image
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pathnd_qc.qc_slide.pen import pen as P
from pathnd_qc.qc_slide.staining import staining as S
from pathnd_qc.qc_slide.tissue import tissue as T
from fake_slide import _render

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        f"  {'PASS' if cond else 'FAIL'}  {name}{' — '+str(detail) if detail else ''}"
    )


class Pointwise(torch.nn.Module):
    """Class 1 iff normalized red exceeds zero; has no context or learned parameters."""

    def forward(self, x):
        return torch.stack((-x[:, 0], x[:, 0]), dim=1)


out = Path(tempfile.mkdtemp(prefix="pathnd-pen-staining-"))
try:
    print("\n[1] Actual Torch normalization, padding, argmax and cropping")
    # Discrete threshold has an analytic oracle, independent of patch boundaries and padding.
    pixels = np.zeros((67, 193, 3), np.uint8)
    pixels[:, ::3, 0] = 255
    image = Image.fromarray(pixels)
    model = Pointwise()
    single = P._pred(image, model, "cpu")
    tiled = P._pred_tiled(image, model, "cpu", 64, 32)
    check(
        "padding and cropping preserve the pointwise class map",
        np.array_equal(single, pixels[:, :, 0] > 127),
    )
    check(
        "context tiling preserves a context-free model at odd edges",
        np.array_equal(tiled, single),
    )
    fold = np.zeros(single.shape, bool)
    fold[:20] = True
    with patch.object(P, "_get_model", return_value=model):
        result = P.detect_pen(
            image, weights_path="injected", fold_mask=fold, tile_px=64, halo_px=32
        )
    check(
        "computed folds are removed from pen support",
        result["pen_error"] is None
        and np.array_equal(result["pen_mask"], single.astype(bool) & ~fold),
    )
    check(
        "raw pen area partitions into kept area and fold overlap",
        np.isclose(
            result["pen_area_fraction_raw"],
            result["pen_area_fraction"] + result["fold_overlap_fraction"],
        ),
    )
    check(
        "report keeps inference settings and omits the mask",
        P.to_report(result)["tile_px"] == 64 and "pen_mask" not in P.to_report(result),
    )
    unchanged = result["pen_mask"].copy()
    P.subtract_folds(result, np.ones((2, 3), bool))
    check(
        "mismatched exclusion shape leaves pen unchanged and explains why",
        np.array_equal(result["pen_mask"], unchanged) and bool(result["note"]),
    )
    overlay = np.asarray(P.generate_pen_overlay(image, unchanged))
    check(
        "pen overlay alters detected pixels only",
        np.array_equal(overlay[~unchanged], pixels[~unchanged])
        and np.any(overlay[unchanged] != pixels[unchanged]),
    )
    missing = P.detect_pen(image, weights_path=None)
    check(
        "unconfigured pen has null measurements and actionable error",
        missing["pen_area_fraction"] is None and bool(missing["pen_error"]),
    )
    # Build/load through real torch state_dict validation, replacing only the model architecture.
    import segmentation_models_pytorch as smp

    weights = out / "weights.pt"
    torch.save(torch.nn.Conv2d(3, 2, 1).state_dict(), weights)
    with patch.object(
        smp, "UnetPlusPlus", side_effect=lambda **kwargs: torch.nn.Conv2d(3, 2, 1)
    ):
        loaded = P.build_pen_model(str(weights))
        check("checkpoint is loaded in evaluation mode", not loaded.training)
        P._MODEL_CACHE.clear()
        first = P._get_model(str(weights), "cpu")
        second = P._get_model(str(weights), "cpu")
        check(
            "model cache reuses a loaded checkpoint in the same process",
            first is second,
        )
        corrupt = out / "corrupt.pt"
        torch.save({"wrong": torch.ones(1)}, corrupt)
        failed = P.detect_pen(image, weights_path=str(corrupt))
        check(
            "incompatible checkpoint fails without fabricated pen area",
            failed["pen_mask"] is None
            and failed["pen_area_fraction"] is None
            and bool(failed["pen_error"]),
        )
    P._MODEL_CACHE.clear()

    print("\n[2] Stain colourfulness over the exact contributing support")
    thumbnail = Image.fromarray(_render(128, 96, seed=29))
    tissue = np.ones((96, 128), bool)
    fold = np.zeros_like(tissue)
    fold[:16] = True
    ink = np.zeros_like(tissue)
    ink[:, 16:24] = True
    result = S.compute_staining_metrics(
        thumbnail, tissue, fold, ink, stain_type="AT8", return_debug=True
    )
    expected = tissue & ~fold & ~ink
    check(
        "staining support excludes the union of fold and pen masks",
        np.array_equal(result["support_mask"], expected),
    )
    check(
        "support fraction equals the contributing pixel fraction",
        result["support_fraction"] == float(expected.mean()),
    )
    check(
        "score aliases the measured finite nonnegative chroma",
        result["staining_quality_score"] == result["staining_metrics"]["chroma_mean"]
        and np.isfinite(result["staining_quality_score"])
        and result["staining_quality_score"] >= 0,
    )
    neutral = S.compute_staining_metrics(
        Image.new("RGB", (128, 96), (128, 128, 128)), tissue
    )
    check(
        "neutral gray has negligible chroma",
        neutral["staining_error"] is None and neutral["staining_quality_score"] < 0.01,
    )
    for label, tm, fm in [
        ("no tissue", np.zeros_like(tissue), None),
        ("all excluded", tissue, tissue),
    ]:
        r = S.compute_staining_metrics(thumbnail, tm, fm)
        check(
            f"{label} has no fabricated stain measurement",
            bool(r["staining_error"])
            and r["staining_quality_score"] is None
            and r["staining_metrics"]["chroma_mean"] is None,
        )
    r = S.compute_staining_metrics(thumbnail, np.ones((3, 5), bool))
    check(
        "incompatible support shape reports an error",
        bool(r["staining_error"]) and r["staining_quality_score"] is None,
    )
    inferred = S.compute_staining_metrics(thumbnail, stain_type="AT8")
    check(
        "missing support invokes real tissue segmentation",
        inferred["staining_error"] is None and 0 < inferred["support_fraction"] <= 1,
    )
    with patch.object(
        T, "compute_tissue_mask", return_value={"tissue_error": "injected failure"}
    ):
        r = S.compute_staining_metrics(thumbnail)
    check(
        "tissue segmentation failure cannot become a staining score",
        "injected failure" in r["staining_error"]
        and r["staining_quality_score"] is None,
    )
    report = S.to_report(result)
    check(
        "staining report retains exclusions and is JSON safe",
        report["fold_excluded"]
        and report["pen_excluded"]
        and "support_mask" not in report,
    )
finally:
    P._MODEL_CACHE.clear()
    shutil.rmtree(out, ignore_errors=True)
print(f"\n{'='*70}\nPEN STAINING SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  - {name}")
sys.exit(bool(FAIL))
