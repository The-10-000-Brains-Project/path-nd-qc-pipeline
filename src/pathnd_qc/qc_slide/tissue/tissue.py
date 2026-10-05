"""Segment tissue and background on the M2 analysis plane.

Methods:
  otsu: union of saturation and gray-level Otsu masks with morphology cleanup.
  entropy: grayscale texture entropy, which can retain pale tissue but miss smooth folds.
  union: otsu | entropy, combining chromatic and texture support.
  intersection: otsu & entropy, retaining agreement between the two methods.
  otsu_s: saturation Otsu without the gray-level branch.
  entropy_s: entropy on saturation, suited to separating vivid tissue from textured glass.
  union_s: otsu | entropy_s.
  adaptive: select union_s above the configured saturation percentile threshold, else union.

With no explicit method, stain metadata selects otsu_s for Hirano and DEFAULT_METHOD
for other or unknown stains. An explicit method bypasses that router.
All methods accept a PIL image or uint8 NumPy array and share input normalization.
The pipeline supplies a common plane at the configured M2 resolution, normally
8.0 microns per pixel; pixel-defined filters scale physically with that resolution.

Results include tissue_mask, tissue_coverage_score, runtime_s, and tissue_error.
The dispatcher records the method. Entropy results include threshold provenance,
and adaptive results retain the selected branch. return_debug adds intermediate
arrays. Fold detection consumes the mask; reporting evaluates configured coverage
bounds without rejecting tissue inside this module.

The entropy method follows Song, Glastonbury, van der Laan & Miller,
"An automatic entropy method to efficiently mask histology whole-slide images"
(Scientific Reports; PMC10017682; github.com/CirculatoryHealth/EntropyMasker).

LICENSE (entropy method): EntropyMasker is CC BY-NC-ND 4.0 (NonCommercial + NoDerivatives).
This is a clean-room reimplementation of the published algorithm, used for
INTERNAL EVALUATION ONLY. Carry attribution and resolve licensing before delivery.
The Otsu method is classical. Tissue segmentation is not ground-truth validated.

Morphology uses scikit-image’s max_size argument, which removes regions of area <= N.
Use Python 3.12+ with dependencies from src/pyproject.toml.
"""

import time

import cv2
import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from scipy.signal import argrelextrema
from skimage.filters.rank import entropy as _rank_entropy
from skimage.morphology import disk, remove_small_holes, remove_small_objects

from pathnd_qc.config.config import cfg, error_kind


from pathnd_qc._images import as_rgb_array as _as_rgb_array



METHODS = ("otsu", "otsu_s", "entropy", "entropy_s", "union", "union_s", "intersection", "adaptive")

# `compute_tissue_mask`'s DEFAULT is the metadata router `route_by_stain_type` (method=None), NOT a
# fixed method. DEFAULT_METHOD is that router's fallback for non-Hirano / unknown / no-stain slides.
DEFAULT_METHOD = cfg("m2.tissue.default_method", "union")

# Normalize configured Hirano spellings for case-insensitive stain routing.
HIRANO_STAINS = tuple(str(s).strip().lower() for s in cfg("m2.tissue.hirano_stains", ["hirano"]))


def route_by_stain_type(stain_type: str) -> str:
    """Select the tissue algorithm from normalized stain metadata.

    Configured Hirano spellings select otsu_s. Other, blank, and unknown stains use
    DEFAULT_METHOD, normally union. Image-only segmentation receives the selected method.
    """
    norm = (stain_type or "").strip().lower()
    return "otsu_s" if norm in HIRANO_STAINS else DEFAULT_METHOD
# Adaptive routing chooses union_s above this saturation threshold, otherwise union.
# Saturation does not uniquely identify a stain; both branches retain Otsu support
# and differ only in the channel used for entropy.
DEFAULT_SAT_P95_THRESHOLD = cfg("m2.tissue.sat_p95_threshold", 60.0)

COLOR_TISSUE = tuple(cfg("m2.tissue.color", [0, 180, 255]))  # cyan — distinct from fold/pen
ALPHA_BLEND = cfg("m2.tissue.alpha_blend", 0.40)


def _result(tissue_mask, t0, error=None, debug=None, extra=None) -> dict:
    """Build the common result fields and attach provenance and optional diagnostics.

    Failed measurements use tissue_coverage_score=None. extra contains small provenance
    fields, while debug holds intermediate arrays and other optional diagnostics.
    """
    out = {
        "tissue_mask": tissue_mask,
        "tissue_coverage_score": (
            float(tissue_mask.sum() / tissue_mask.size) if tissue_mask is not None else None
        ),
        "runtime_s": round(time.monotonic() - t0, 3),
        "tissue_error": error,
    }
    if extra:
        out.update(extra)
    if debug is not None:
        out.update(debug)
    return out


def _tag(result: dict, method: str) -> dict:
    """Record the selected segmentation method in its result."""
    result["method"] = method
    return result


# --------------------------------------------------------------------------- otsu
def compute_tissue_mask_otsu(
    thumbnail: Image.Image, return_debug: bool = False, saturation_only: bool = False
) -> dict:
    """Segment tissue using saturation and inverted-gray Otsu thresholds.

    Union the two masks, then remove small objects and fill small holes.
    saturation_only=True uses the saturation branch alone, as selected for Hirano
    metadata. It can omit dark unsaturated tissue, including fold boundaries.

    Returns tissue_mask, tissue_coverage_score, runtime_s, and tissue_error.
    return_debug adds thresholds and component masks.
    """
    t0 = time.monotonic()
    try:
        img = _as_rgb_array(thumbnail)

        # HSV saturation Otsu — tissue has higher saturation than glass background.
        hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV)
        S = hsv[:, :, 1]
        sat_thresh, s_mask = cv2.threshold(S, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        g_mask = None
        gray_thresh = None
        if saturation_only:
            tissue_mask = s_mask > 0
        else:
            # Inverted grayscale Otsu — catches dark, low-saturation haematoxylin-dense tissue.
            gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            gray_thresh, g_mask = cv2.threshold(
                gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
            )
            tissue_mask = (s_mask > 0) | (g_mask > 0)

        # Clean up: drop objects/holes of area at most ~ (1% of the shorter side)^2.
        H, W = tissue_mask.shape
        min_size = max(3, int(0.01 * min(H, W))) ** 2
        tissue_mask = remove_small_objects(tissue_mask, max_size=min_size)
        tissue_mask = remove_small_holes(tissue_mask, max_size=min_size)

        debug = None
        if return_debug:
            debug = {
                "otsu_saturation_threshold": float(sat_thresh),
                "otsu_gray_threshold": None if gray_thresh is None else float(gray_thresh),
                "otsu_saturation_mask": s_mask > 0,
                "otsu_gray_mask": None if g_mask is None else (g_mask > 0),
                "saturation_only": saturation_only,
                "min_size": min_size,
            }
        return _result(tissue_mask, t0, debug=debug)

    except Exception as e:  # degrade gracefully, like pen/fold
        return _result(None, t0, error=f"{type(e).__name__}: {e}")


# ------------------------------------------------------------------------ entropy
def _to_gray_u8(thumbnail: Image.Image) -> np.ndarray:
    """Grayscale uint8, matching cv2.imread(path, 0) luminosity weights."""
    return cv2.cvtColor(_as_rgb_array(thumbnail), cv2.COLOR_RGB2GRAY)


def _pick_threshold(ent: np.ndarray, hist_bins: int, lo: float, hi: float):
    """Return the last histogram minimum in (lo, hi), or a fallback threshold, with its source label."""
    counts, edges = np.histogram(ent, bins=hist_bins)
    minima = argrelextrema(counts, np.less)[0]
    chosen = None
    for idx in minima:
        val = edges[idx]
        if lo < val < hi:
            chosen = float(val)  # last one wins, per reference loop
    if chosen is not None:
        return chosen, "localmin"
    # If the bounded search finds no minimum, use the deepest histogram minimum
    # or the configured range midpoint.
    if minima.size:
        # deepest local minimum overall
        idx = minima[np.argmin(counts[minima])]
        return float(edges[idx]), "fallback_deepest_min"
    return float((lo + hi) / 2), "fallback_midpoint"


def _entropy_mask_from_channel(chan_u8, t0, channel_name, disk_radius, hist_bins,
                               thresh_range, keep, min_area_frac, return_debug):
    """Shared EntropyMasker core on a single uint8 channel: local Shannon-entropy map
    (disk(disk_radius)) -> histogram local-min threshold in `thresh_range` -> high-entropy =
    tissue -> cleanup. Both the grayscale and the saturation entropy functions are thin wrappers
    over this, so they stay structurally identical (only the input channel differs).
    """
    ent = _rank_entropy(chan_u8, disk(disk_radius))

    thresh, thresh_source = _pick_threshold(ent, hist_bins, *thresh_range)
    tissue = ent >= thresh  # high entropy = tissue

    H, W = tissue.shape
    if keep == "largest":
        lbl, n = ndi.label(tissue)
        if n:
            sizes = np.bincount(lbl.ravel())
            sizes[0] = 0  # ignore background label
            tissue = lbl == int(sizes.argmax())
    else:  # "all"
        min_size = max(1, int(min_area_frac * H * W))
        if tissue.any():
            tissue = remove_small_objects(tissue, max_size=min_size)

    debug = None
    if return_debug:
        debug = {
            "entropy_threshold": thresh,
            "threshold_source": thresh_source,
            "disk_radius": disk_radius,
            "keep": keep,
            "channel": channel_name,
            "entropy_map": ent,
        }
    # Include threshold_source in every result so a histogram fallback remains visible.
    return _result(tissue, t0, debug=debug,
                   extra={"threshold_source": thresh_source, "entropy_threshold": float(thresh)})


def compute_tissue_mask_entropy(
    thumbnail: Image.Image,
    disk_radius: int = cfg("m2.tissue.disk_radius", 5),
    hist_bins: int = cfg("m2.tissue.hist_bins", 30),
    thresh_range: tuple = tuple(cfg("m2.tissue.thresh_range", [1.0, 4.0])),
    keep: str = cfg("m2.tissue.keep", "all"),   # Retain all cleaned components or only the largest.
    min_area_frac: float = cfg("m2.tissue.min_area_frac", 1e-4),
    return_debug: bool = False,
) -> dict:
    """Compute grayscale local-entropy tissue support and clean the thresholded mask.

    Entropy uses a disk neighborhood. A local minimum of its histogram supplies the
    threshold; high-entropy pixels form tissue. keep="all" removes small specks while
    retaining disconnected pieces; keep="largest" selects one connected component.
    The same disk radius is used for threshold estimation and mask generation.

    Returns tissue_mask, tissue_coverage_score, runtime_s, and tissue_error, plus
    threshold provenance. return_debug adds intermediate diagnostics and the entropy map.
    The algorithm follows EntropyMasker’s grayscale entropy and histogram-minimum method.
    """
    t0 = time.monotonic()
    try:
        gray = _to_gray_u8(thumbnail)
        return _entropy_mask_from_channel(gray, t0, "gray", disk_radius, hist_bins,
                                          thresh_range, keep, min_area_frac, return_debug)
    except Exception as e:
        return _result(None, t0, error=f"{type(e).__name__}: {e}")


def compute_tissue_mask_entropy_S(
    thumbnail: Image.Image,
    disk_radius: int = cfg("m2.tissue.disk_radius", 5),
    hist_bins: int = cfg("m2.tissue.hist_bins", 30),
    thresh_range: tuple = tuple(cfg("m2.tissue.thresh_range", [1.0, 4.0])),
    keep: str = cfg("m2.tissue.keep", "all"),
    min_area_frac: float = cfg("m2.tissue.min_area_frac", 1e-4),
    return_debug: bool = False,
) -> dict:
    """Compute tissue support from local entropy on the HSV saturation channel.

    The threshold and cleanup steps match compute_tissue_mask_entropy. Saturation entropy
    can separate vivid tissue from unsaturated textured background, but pale stains with
    little saturation provide weak support. The adaptive method chooses its entropy channel
    using sat_p95. Return fields and debug controls match the grayscale variant.
    """
    t0 = time.monotonic()
    try:
        S = cv2.cvtColor(_as_rgb_array(thumbnail), cv2.COLOR_RGB2HSV)[:, :, 1]
        return _entropy_mask_from_channel(S, t0, "saturation", disk_radius, hist_bins,
                                          thresh_range, keep, min_area_frac, return_debug)
    except Exception as e:
        return _result(None, t0, error=f"{type(e).__name__}: {e}")


def _sat_p95(thumbnail: Image.Image) -> float:
    """95th-percentile of the HSV saturation channel (whole image, 0-255). Routing signal for the
    'adaptive' method: high sat_p95 => vivid tissue where S-entropy is reliable (Hirano, deep
    LFB/H&E); low => pale IHC where union is safer."""
    S = cv2.cvtColor(_as_rgb_array(thumbnail), cv2.COLOR_RGB2HSV)[:, :, 1]
    return float(np.percentile(S, 95))


# ------------------------------------------------------------------------ combine
def combine_masks(mask_a: np.ndarray, mask_b: np.ndarray, op: str = "union") -> np.ndarray:
    """Boolean OR ("union") / AND ("intersection") of two tissue masks.

    Public so a caller can compute the two masks itself and compose them directly; this is also
    what `compute_tissue_mask`'s union/intersection modes use internally.
    """
    a = np.asarray(mask_a, dtype=bool)
    b = np.asarray(mask_b, dtype=bool)
    if a.shape != b.shape:
        raise ValueError(f"mask shape mismatch: {a.shape} vs {b.shape}")
    if op == "union":
        return a | b
    if op == "intersection":
        return a & b
    raise ValueError(f"unknown op {op!r}; expected 'union' or 'intersection'")


# --------------------------------------------------------------------- dispatcher
def compute_tissue_mask(
    thumbnail: Image.Image,
    method: str | None = None,
    return_debug: bool = False,
    sat_p95_threshold: float = DEFAULT_SAT_P95_THRESHOLD,
    stain_type: str | None = None,
    **entropy_kwargs,
) -> dict:
    """Compute tissue support from an RGB analysis image.

    With method=None, stain metadata routes through route_by_stain_type: configured
    Hirano spellings use otsu_s, and other stains use DEFAULT_METHOD, normally union.
    An explicit method selects otsu, otsu_s, entropy, entropy_s, union, union_s,
    intersection, or adaptive.

    Combined methods run both constituent algorithms. union combines Otsu and grayscale
    entropy; union_s combines Otsu and saturation entropy. intersection requires agreement.
    A constituent error returns tissue_mask=None with a named error instead of a partial mask.

    adaptive selects union_s when sat_p95 exceeds sat_p95_threshold, otherwise union.
    Only that branch runs. entropy_kwargs configure entropy processing and are ignored
    by Otsu-only methods.

    Returns common mask, coverage, timing, and error fields, together with method and
    threshold provenance. return_debug adds per-method intermediates and nested results
    for combined methods.
    """
    t0 = time.monotonic()
    if method is None:                      # DEFAULT: metadata router (Hirano -> otsu_s, else -> union)
        method = route_by_stain_type(stain_type)
    if method not in METHODS:
        return _tag(_result(None, t0, error=f"ValueError: unknown method {method!r}; "
                                            f"expected one of {list(METHODS)}"), method)

    if method == "otsu":
        return _tag(compute_tissue_mask_otsu(thumbnail, return_debug=return_debug), method)
    if method == "otsu_s":
        return _tag(compute_tissue_mask_otsu(thumbnail, return_debug=return_debug,
                                             saturation_only=True), method)
    if method == "entropy":
        return _tag(compute_tissue_mask_entropy(thumbnail, return_debug=return_debug,
                                                **entropy_kwargs), method)
    if method == "entropy_s":
        return _tag(compute_tissue_mask_entropy_S(thumbnail, return_debug=return_debug,
                                                  **entropy_kwargs), method)

    if method == "adaptive":
        try:
            sp95 = _sat_p95(thumbnail)
        except Exception as e:
            return _tag(_result(None, t0, error=f"adaptive sat_p95 failed -> {type(e).__name__}: {e}"),
                        method)
        branch = "union_s" if sp95 > sat_p95_threshold else "union"
        res = compute_tissue_mask(thumbnail, method=branch, return_debug=return_debug,
                                  sat_p95_threshold=sat_p95_threshold, **entropy_kwargs)
        res["runtime_s"] = round(time.monotonic() - t0, 3)  # include the sat_p95 pass
        res["branch"] = branch                          # adaptive's own routing decision, recorded
        if return_debug:
            res["sat_p95"] = sp95
            res["sat_p95_threshold"] = sat_p95_threshold
        return _tag(res, method)

    # Combined modes require both Otsu and entropy results. union and intersection use
    # grayscale entropy; union_s uses saturation entropy. The operator combines their masks.
    use_s = method == "union_s"
    op = "union" if use_s else method
    ent_name = "entropy_s" if use_s else "entropy"
    ent_fn = compute_tissue_mask_entropy_S if use_s else compute_tissue_mask_entropy

    otsu = compute_tissue_mask_otsu(thumbnail, return_debug=return_debug)
    ent = ent_fn(thumbnail, return_debug=return_debug, **entropy_kwargs)

    legs = {"otsu": otsu, ent_name: ent}
    failed = [n for n, r in legs.items() if r["tissue_error"] is not None]
    if failed:
        errs = "; ".join(f"{n}: {legs[n]['tissue_error']}" for n in failed)
        return _tag(_result(None, t0, error=f"{method} leg failed -> {errs}"), method)

    try:
        combined = combine_masks(otsu["tissue_mask"], ent["tissue_mask"], op=op)
    except Exception as e:
        return _tag(_result(None, t0, error=f"{type(e).__name__}: {e}"), method)

    debug = {"parts": legs} if return_debug else None
    return _tag(_result(combined, t0, debug=debug,
                        extra={"threshold_source": ent.get("threshold_source"),
                               "entropy_threshold": ent.get("entropy_threshold")}), method)


# ------------------------------------------------------------------------ overlay
def generate_tissue_overlay(thumbnail: Image.Image, tissue_mask: np.ndarray) -> Image.Image:
    """Blend the tissue mask (cyan) onto the thumbnail."""
    overlay = _as_rgb_array(thumbnail).astype(np.float32)
    if tissue_mask is not None and tissue_mask.any():
        m = tissue_mask.astype(bool)
        for c, col in enumerate(COLOR_TISSUE):
            overlay[m, c] = overlay[m, c] * (1 - ALPHA_BLEND) + col * ALPHA_BLEND
    return Image.fromarray(np.clip(overlay, 0, 255).astype(np.uint8))


def to_report(result: dict) -> dict:
    """JSON-safe subsection of this component's result for `report.json` (masks/arrays dropped)."""
    return {"tissue_coverage_score": result.get("tissue_coverage_score"),
            "method": result.get("method"),                      # what the router chose
            "branch": result.get("branch"),                      # adaptive only, else None
            "threshold_source": result.get("threshold_source"),  # entropy legs only, else None
            "runtime_s": result.get("runtime_s"),
            "error_type": error_kind(result.get("tissue_error"), result.get("tissue_error_type")),
            "error": result.get("tissue_error")}
