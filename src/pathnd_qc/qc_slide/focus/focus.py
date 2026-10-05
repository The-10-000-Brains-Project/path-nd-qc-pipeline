"""Measure Laplacian variance over tissue support for the M2 focus diagnostic.

The pipeline supplies tissue, fold, and pen masks. Standalone calls can derive tissue
with the stain router. Support excludes available folds and pen. Pixels outside it
are filled with the support’s mean gray value before filtering; variance is measured
only inside support. This reduces, but does not eliminate, boundary effects.

The metric follows the Laplacian-variance method described by Pech-Pacheco et al.,
"Diatom autofocusing in brightfield microscopy: a comparative study" (ICPR).
Resolution, texture, and support affect the score. At the default 8.0 microns per
pixel it is a coarse diagnostic, not a calibrated blur verdict. Reporting applies
thresholds. Empty support and processing failures return a null score and an error.
"""

import time

import cv2
import numpy as np
from PIL import Image
from pathnd_qc.config.config import error_kind


def compute_focus_score(
    thumbnail: Image.Image,
    tissue_mask: np.ndarray | None = None,
    fold_mask: np.ndarray | None = None,
    pen_mask: np.ndarray | None = None,
    stain_type: str | None = None,
    return_debug: bool = False,
) -> dict:
    """Laplacian-variance focus score over tissue minus folds, on the M2 analysis plane.

    `stain_type` routes the tissue mask when one is not supplied (Hirano -> otsu_s, else union).

    See the module docstring for the wiring, the background handling, and why the score carries
    no threshold.

    Returns a dict shaped like the pen/tissue/fold/staining plumbing:
      {focus_score, support_fraction, tissue_coverage_score, runtime_s, focus_error}
    `return_debug` adds the support mask and the Laplacian map. Degrades gracefully: an empty
    support returns `focus_score=None` plus a `focus_error`, NEVER 0.0 — zero is a legitimate
    score for a perfectly uniform (i.e. maximally blurred) region, so returning it for "no data"
    would silently read as the worst possible focus.
    """
    t0 = time.monotonic()

    def fail(msg, coverage=None):
        # No measurement gives null support_fraction. Preserve mask coverage when a mask exists.
        return {
            "focus_score": None,
            "support_fraction": None,
            "tissue_coverage_score": coverage,
            "stain_type": stain_type,
            "runtime_s": round(time.monotonic() - t0, 3),
            "focus_error": msg,
        }

    try:
        rgb = np.array(thumbnail.convert("RGB"))

        if tissue_mask is None:
            # Metadata-routed, matching folds.py / staining.py: Hirano -> otsu_s, else union.
            from ..tissue.tissue import compute_tissue_mask
            t = compute_tissue_mask(thumbnail, stain_type=stain_type)
            if t["tissue_error"] is not None:
                return fail(f"tissue mask failed -> {t['tissue_error']}")
            tissue_mask = t["tissue_mask"]
        tissue_mask = np.asarray(tissue_mask, dtype=bool)

        # Support = tissue, minus folds (smooth smears -> DEFLATE the variance), minus pen if given.
        support = tissue_mask
        if fold_mask is not None:
            support = support & ~np.asarray(fold_mask, dtype=bool)
        if pen_mask is not None:
            support = support & ~np.asarray(pen_mask, dtype=bool)

        if not support.any():
            return fail("empty support: no tissue px remain after fold/pen exclusion",
                        coverage=float(tissue_mask.mean()))

        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float64)

        # Flatten everything outside the support to the support mean, so the tissue/glass border
        # (and any excluded fold/pen interior) cannot fire the edge detector.
        filled = gray.copy()
        filled[~support] = gray[support].mean()

        lap = cv2.Laplacian(filled, cv2.CV_64F)
        focus_score = float(lap[support].var())

        result = {
            "focus_score": focus_score,
            "support_fraction": float(support.mean()),
            "tissue_coverage_score": float(tissue_mask.mean()),
            "fold_excluded": fold_mask is not None,     # Record whether fold exclusion contributes to the support.
            "pen_excluded": pen_mask is not None,
            "stain_type": stain_type,
            "runtime_s": round(time.monotonic() - t0, 3),
            "focus_error": None,
        }
        if return_debug:
            result["support_mask"] = support
            result["laplacian"] = lap
        return result

    except Exception as e:
        return fail(f"{type(e).__name__}: {e}")


def to_report(result: dict) -> dict:
    """JSON-safe subsection of this component's result for `report.json` (masks/arrays dropped)."""
    return {"focus_score": result.get("focus_score"),
            "support_fraction": result.get("support_fraction"),
            "fold_excluded": result.get("fold_excluded"),
            "pen_excluded": result.get("pen_excluded"),
            "tissue_coverage_score": result.get("tissue_coverage_score"),
            "stain_type": result.get("stain_type"),
            "runtime_s": result.get("runtime_s"),
            "error_type": error_kind(result.get("focus_error"), result.get("focus_error_type")),
            "error": result.get("focus_error")}
