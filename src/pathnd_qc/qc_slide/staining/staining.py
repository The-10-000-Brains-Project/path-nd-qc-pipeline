"""Measure mean CIE-Lab chroma on the analysis image, normally at 8.0 microns per pixel.

chroma_mean is mean sqrt(a*a + b*b) over tissue excluding available fold and pen
masks. staining_quality_score exposes the same value for reports and thresholds.
Support metadata describes contributing pixels; return_debug adds the support mask.

Chroma depends on stain and tissue content and can hide regional differences.
It is not a calibrated quality verdict. Reporting applies configured bounds;
all shipped bounds are null.
"""

import time

import numpy as np
from PIL import Image
from skimage.color import rgb2lab

from pathnd_qc.config.config import error_kind


def compute_staining_metrics(
    thumbnail: Image.Image,
    tissue_mask: np.ndarray | None = None,
    fold_mask: np.ndarray | None = None,
    pen_mask: np.ndarray | None = None,
    stain_type: str | None = None,
    *,
    return_debug: bool = False,
) -> dict:
    """Return chroma_mean over tissue minus folds and pen, with support metadata.

    staining_metrics contains only chroma_mean; staining_quality_score is its
    existing score alias. Both are None on failure, including empty support.
    stain_type routes tissue segmentation when no tissue mask is supplied.
    return_debug adds the boolean support_mask on success.
    """
    t0 = time.monotonic()

    def fail(msg, coverage=None):
        # Use null support_fraction on failure. Preserve tissue coverage when a mask is available.
        return {
            "staining_quality_score": None,
            "staining_metrics": {"chroma_mean": None},
            "support_fraction": None,
            "tissue_coverage_score": coverage,
            "stain_type": stain_type,
            "runtime_s": round(time.monotonic() - t0, 3),
            "staining_error": msg,
        }

    try:
        rgb = np.array(thumbnail.convert("RGB"))

        if tissue_mask is None:
            # Metadata-routed, matching folds.py: Hirano -> otsu_s, everything else -> union.
            from ..tissue.tissue import compute_tissue_mask
            t = compute_tissue_mask(thumbnail, stain_type=stain_type)
            if t["tissue_error"] is not None:
                return fail(f"tissue mask failed -> {t['tissue_error']}")
            tissue_mask = t["tissue_mask"]
        tissue_mask = np.asarray(tissue_mask, dtype=bool)

        # Support = tissue, minus folds (dark/saturated -> bias the stain stats), minus pen if given.
        support = tissue_mask
        if fold_mask is not None:
            support = support & ~np.asarray(fold_mask, dtype=bool)
        if pen_mask is not None:
            support = support & ~np.asarray(pen_mask, dtype=bool)

        if not support.any():
            return fail("empty support: no tissue px remain after fold/pen exclusion",
                        coverage=float(tissue_mask.mean()))

        lab = rgb2lab(rgb)
        a, b = lab[:, :, 1][support], lab[:, :, 2][support]
        chroma_mean = float(np.sqrt(a ** 2 + b ** 2).mean())

        result = {
            "staining_quality_score": chroma_mean,
            "staining_metrics": {"chroma_mean": chroma_mean},
            "support_fraction": float(support.mean()),
            "tissue_coverage_score": float(tissue_mask.mean()),
            # Record which exclusions define the measured support.
            "fold_excluded": fold_mask is not None,
            "pen_excluded": pen_mask is not None,
            "stain_type": stain_type,
            "runtime_s": round(time.monotonic() - t0, 3),
            "staining_error": None,
        }
        if return_debug:
            result["support_mask"] = support
        return result

    except Exception as e:
        return fail(f"{type(e).__name__}: {e}")


def to_report(result: dict) -> dict:
    """JSON-safe subsection of this component's result for `report.json` (masks/arrays dropped)."""
    metrics = result.get("staining_metrics") or {}
    return {"staining_quality_score": result.get("staining_quality_score"),  # = chroma_mean
            "staining_metrics": {"chroma_mean": metrics.get("chroma_mean")},
            "support_fraction": result.get("support_fraction"),
            "fold_excluded": result.get("fold_excluded"),
            "pen_excluded": result.get("pen_excluded"),
            "stain_type": result.get("stain_type"),
            "runtime_s": result.get("runtime_s"),
            "error_type": error_kind(result.get("staining_error"), result.get("staining_error_type")),
            "error": result.get("staining_error")}
