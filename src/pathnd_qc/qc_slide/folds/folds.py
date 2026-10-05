"""Detect tissue folds on the M2 analysis plane with ConnSoftT and F_line.

ConnSoftT selects a feature from stain metadata. Configured histochemical stains,
including Hirano, LFB, and LFB/H&E, use d = saturation - intensity. Other stains use
Macenko stain2 concentration. Plain H&E uses stain2 unless added to d_path_stains.
Both variants apply connectivity thresholds, hysteresis, opening, an area gate,
despeckling, and boundary-contrast filtering. The method follows Kothari, Phan & Wang,
"Eliminating tissue-fold artifacts" (Journal of Pathology Informatics; PMC3779385).

F_line multiplies multiscale Frangi ridge response, structure-tensor coherence, and
positive local robust-z darkness on the optical-density norm. Tissue percentiles
scale the response to color indices; the configured index cutoff and connected-area
floor produce its mask. The final fold mask is the union of enabled branches.
No learned model or weights are needed. Outputs are not ground-truth validated.

Detection operates inside tissue, deriving a stain-routed tissue mask when needed.
A direct pen_mask argument excludes ink before thresholding. The pipeline instead
detects folds before computed pen, and subtracts any supplied pen mask afterward.
Fractions use the original tissue denominator.

The input targets 8.0 microns per pixel by default. thumb_mpp converts physical areas
and F_line scales to pixels; omission uses the configured fallback pixel gates and
8.0-micron F_line scale. Pixel-defined opening and contrast filters change physical
size when the analysis resolution changes.
"""

import time

import numpy as np
from PIL import Image

from pathnd_qc.config.config import cfg, error_kind
from scipy import ndimage as ndi
from skimage.feature import structure_tensor, structure_tensor_eigenvalues
from skimage.filters import frangi
from skimage.filters.rank import median as rank_median
from skimage.measure import label, regionprops
from skimage.morphology import dilation, disk, erosion, opening, remove_small_objects

# Paper: discard objects < 5% of a 512x512 high-res tile, expressed as physical area (0.5 um/px ref tile).
_REF_TILE_PX = 512
_REF_TILE_MPP = 0.5
DEFAULT_MIN_AREA_UM2 = cfg("m2.folds.min_area_um2", 0.05 * (_REF_TILE_PX * _REF_TILE_MPP) ** 2)  # ~=3276.8

# Post-filter defaults; supported None arguments disable their corresponding gates.
DEFAULT_OPENING_RADIUS = cfg("m2.folds.opening_radius", 3)              # Opening radius in analysis-plane pixels.
DEFAULT_SPECK_MAX_AREA_UM2 = cfg("m2.folds.speck_max_area_um2", 13144.0)    # ~200 px at thumb_mpp 8.107
DEFAULT_MIN_BOUNDARY_CONTRAST = cfg("m2.folds.min_boundary_contrast_d", 0.40)    # d = s-i units (the d/Hirano path); empty separation band 0.308->0.522
DEFAULT_CONTRAST_BAND_PX = cfg("m2.folds.contrast_band_px", 3)            # Erosion/dilation band radius in analysis-plane pixels.
# Fallback pixel gates correspond to the physical-area defaults at 8.0 microns per pixel.
_FALLBACK_MIN_AREA_PX = cfg("m2.folds.fallback_min_area_px", 51)
_FALLBACK_SPECK_MAX_AREA_PX = cfg("m2.folds.fallback_speck_max_area_px", 205)

# ---- d / Hirano path.
D_BETA = cfg("m2.folds.d_beta", 0.34)
# ---- stain2 / main path.
STAIN2_BETA = cfg("m2.folds.stain2_beta", 0.25)
STAIN2_MIN_BOUNDARY_CONTRAST = cfg("m2.folds.min_boundary_contrast_stain2", 0.10)     # boundary-contrast in stain2 concentration units
STAIN2_SWEEP_PCTL = tuple(cfg("m2.folds.stain2_sweep_pctl", [0.5, 99.5]))         # ranged sweep bounds (stain2 is unbounded; native [-1,1] is d-only)
STAIN2_SWEEP_LEVELS = cfg("m2.folds.stain2_sweep_levels", 41)                # matches the d grid granularity (41 levels)
MACENKO_BETA = cfg("m2.folds.macenko_beta", 0.15)                     # OD-norm floor: drop near-white pixels before stain estimation
MACENKO_ALPHA = cfg("m2.folds.macenko_alpha", 1.0)                     # robust angle percentile for the stain vectors

DEFAULT_ALPHA = cfg("m2.folds.alpha", 0.64)                    # shared by both paths
_AUTO = object()                        # sentinel: beta / min_boundary_contrast -> path-resolved default

# Route configured histochemical stain spellings to d and other stains to stain2.
D_PATH_STAINS = tuple(str(s).strip().lower() for s in
                      cfg("m2.folds.d_path_stains", ["hirano", "lfb", "lfb/h&e", "lfb-he", "lfb/he"]))

# F_line scales are specified in microns and converted using the analysis-plane MPP.
FLINE_ENABLED = bool(cfg("m2.folds.fline_enabled", True))
FLINE_SIGMAS_UM = tuple(cfg("m2.folds.fline_sigmas_um", [35.0, 70.0, 105.0]))   # Frangi scales ~ ridge half-widths
FLINE_BALL_UM = cfg("m2.folds.fline_ball_um", 520.0)              # Z_R reference-ball radius (>> fold width)
FLINE_ST_SIGMA_UM = cfg("m2.folds.fline_st_sigma_um", 70.0)       # structure-tensor integration scale
FLINE_EPS_FRAC = cfg("m2.folds.fline_eps_frac", 0.01)             # eps = frac x global tissue MAD of D
FLINE_HARD_INDEX = cfg("m2.folds.fline_hard_index", 200)          # colour index on the per-slide p1-p99 stretch (0..255)
FLINE_MIN_AREA_UM2 = cfg("m2.folds.fline_min_area_um2", 44800.0)  # Minimum line-fold area; 700 pixels at 8 microns per pixel.
_FALLBACK_MPP = 8.0                                               # the plane's design MPP, used when thumb_mpp is None

_T_STEP = 0.05                          # d native grid step, t in [-1, 1]
_EIGHT_CONN = np.ones((3, 3), dtype=bool)   # 8-connectivity
_HARD_NBR = np.ones((5, 5), dtype=bool)     # paper's 5x5 neighborhood gate

COLOR_FOLD = tuple(cfg("m2.folds.color", [255, 215, 0]))  # yellow — distinct from pen green
ALPHA_BLEND = cfg("m2.folds.alpha_blend", cfg("shared.alpha_blend", 0.45))


# ------------------------------------------------------------------ features

def rgb_to_hsi(img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """RGB uint8 -> (saturation, intensity), both float in [0, 1], HSI hexcone.

    i = (R + G + B) / 3 ; s = 1 - min(R,G,B) / i  (s = 0 where i = 0). NOT cv2 HSV (whose V = max).
    """
    rgb = img.astype(np.float32) / 255.0
    i = rgb.mean(axis=2)
    min_c = rgb.min(axis=2)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(i > 0, min_c / i, 1.0)
    s = np.clip(1.0 - ratio, 0.0, 1.0)
    return s, i


def _optical_density(img: np.ndarray) -> np.ndarray:
    """Beer-Lambert OD = -log10((I + 1) / 256), per channel; HxWx3 float."""
    return -np.log10((img.astype(np.float64) + 1.0) / 256.0)


def macenko_stain2(img: np.ndarray, mask: np.ndarray,
                   beta: float = MACENKO_BETA, alpha: float = MACENKO_ALPHA) -> tuple[np.ndarray, np.ndarray]:
    """Estimate Macenko stain vectors and return (stain2 concentration map, stain2 vector).

    Covariance eigenvectors define the optical-density plane and angular percentiles
    select its stain directions. Order by blue optical density: stain2 has the lower
    blue absorption. Close blue components can make this ordering sensitive to numerical
    noise. Too few above-floor tissue pixels raise ValueError for the caller to report.
    """
    OD = _optical_density(img)
    flat = OD.reshape(-1, 3)
    ODt = flat[np.asarray(mask, dtype=bool).reshape(-1)]
    ODt = ODt[np.linalg.norm(ODt, axis=1) > beta]
    if ODt.shape[0] < 3:
        raise ValueError("macenko_stain2: too few in-tissue OD pixels to estimate stain vectors")
    _, V = np.linalg.eigh(np.cov(ODt.T))
    plane = V[:, [2, 1]]                                    # top-2 eigenvectors
    proj = ODt @ plane
    phi = np.arctan2(proj[:, 1], proj[:, 0])
    lo, hi = np.percentile(phi, alpha), np.percentile(phi, 100 - alpha)
    v1 = plane @ np.array([np.cos(lo), np.sin(lo)]); v1 = v1 if v1.sum() >= 0 else -v1
    v2 = plane @ np.array([np.cos(hi), np.sin(hi)]); v2 = v2 if v2.sum() >= 0 else -v2
    # Order stain vectors by blue optical density; stain2 has lower blue absorption.
    # Near-equal blue components make the selected ordering sensitive to numerical noise.
    if v1[2] < v2[2]:
        v1, v2 = v2, v1
    S = np.array([v1, v2])
    S = S / np.linalg.norm(S, axis=1, keepdims=True)
    C = np.clip(flat @ np.linalg.pinv(S), 0, None)          # OD = C @ S  ->  C = OD @ pinv(S)
    stain2 = C.reshape(OD.shape[0], OD.shape[1], 2)[:, :, 1]
    return stain2, S[1]


# ------------------------------------------------ connectivity thresholding (feature-agnostic)

def _count_components(feat: np.ndarray, mask: np.ndarray, ts: np.ndarray) -> np.ndarray:
    """C(t) = # of 8-connected objects in {feat > t} within mask, over the given t grid."""
    counts = np.empty(ts.shape, dtype=np.int64)
    for k, t in enumerate(ts):
        binm = (feat > t) & mask
        counts[k] = ndi.label(binm, structure=_EIGHT_CONN)[1] if binm.any() else 0
    return counts


def connectivity_curve(d: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """d path: connectivity curve on the native grid t in [-1, 1] step 0.05."""
    ts = np.arange(-1.0, 1.0 + _T_STEP / 2, _T_STEP)
    return ts, _count_components(d, mask, ts)


def _ranged_ts(feat: np.ndarray, mask: np.ndarray, plo: float = STAIN2_SWEEP_PCTL[0],
               phi: float = STAIN2_SWEEP_PCTL[1], n: int = STAIN2_SWEEP_LEVELS) -> np.ndarray:
    """stain2 path: sweep grid over the feature's own [p_lo, p_hi] (native [-1,1] is d-only)."""
    lo, hi = np.percentile(feat[mask], [plo, phi])
    return np.linspace(lo, hi, n)


def _T(ts: np.ndarray, counts: np.ndarray, c: float) -> float:
    """T(c) = max{ t : C(t) >= c }. Falls back to min t if none qualify."""
    ok = ts[counts >= c]
    return float(ok.max()) if ok.size else float(ts.min())


def resolve_thresholds(ts, counts, alpha, beta) -> tuple[float, float]:
    """t_hard = T(alpha*maxC), t_soft = T(beta*maxC)."""
    max_c = float(counts.max()) if counts.size else 0.0
    t_hard = _T(ts, counts, alpha * max_c)
    t_soft = _T(ts, counts, beta * max_c)
    return t_hard, t_soft


def _hysteresis(feat, mask, t_hard, t_soft) -> np.ndarray:
    """Fold = soft pixel within a 5x5 neighborhood of a hard seed (paper's rule)."""
    hard = (feat > t_hard) & mask
    soft = (feat > t_soft) & mask
    hard_gated = ndi.binary_dilation(hard, structure=_HARD_NBR)
    return soft & hard_gated


# ------------------------------------------------------------------ post-filters

def _boundary_contrast(feat: np.ndarray, lbl: np.ndarray, region, r: int) -> float | None:
    """Mean ``feat`` on a blob's INSIDE edge band minus mean on its OUTSIDE band.

    A real fold is tissue DOUBLED OVER, so its edge is a step in the feature; a threshold artifact on a smooth
    gradient has a near-zero step. Returns None when either band is empty (tiny/edge blob) — caller then KEEPS
    the blob (absence of evidence must not delete one). Works for the d map (Hirano) or the stain2 map (main).
    """
    r0, c0, r1, c1 = region.bbox
    pad = r + 2
    R0, C0 = max(0, r0 - pad), max(0, c0 - pad)
    R1, C1 = min(lbl.shape[0], r1 + pad), min(lbl.shape[1], c1 + pad)
    sub = lbl[R0:R1, C0:C1] == region.label
    fsub = feat[R0:R1, C0:C1]
    inner = sub & ~erosion(sub, disk(r))
    outer = dilation(sub, disk(r)) & ~sub
    if not inner.any() or not outer.any():
        return None
    return float(fsub[inner].mean() - fsub[outer].mean())


def _postprocess(fold: np.ndarray, *, bc_feature: np.ndarray, min_boundary_contrast: float | None,
                 opening_radius: int | None, min_area_px: int, speck_max_area_px: float | None,
                 speck_min_circularity: float, contrast_band_px: int) -> np.ndarray:
    """The four post-filters in order: opening -> area -> despeckle -> boundary-contrast gate.

    Shared by both paths; the ONLY per-path difference is (bc_feature, min_boundary_contrast) — d @ 0.40 vs
    stain2 @ 0.10. Mutates a copy and returns it.
    """
    fold = fold.copy()
    # 1. opening — erase thin dendrites (< ~2r px) and sever thin bridges.
    if opening_radius and fold.any():
        fold = opening(fold, disk(opening_radius))
    # 2. area filter.
    if fold.any():
        fold = remove_small_objects(fold, max_size=min_area_px)
    # 3. despeckle — drop blobs that are BOTH small (< speck_max_area_px) AND round (circ > threshold).
    if speck_max_area_px is not None and fold.any():
        speck_lbl = label(fold, connectivity=2)
        for region in regionprops(speck_lbl):
            per = region.perimeter
            circ = min(4.0 * np.pi * region.area / (per ** 2), 1.0) if per > 0 else 0.0
            if region.area < speck_max_area_px and circ > speck_min_circularity:
                fold[speck_lbl == region.label] = False
    # 4. boundary-contrast gate — drop blobs whose edge is not a step in bc_feature (None -> keep, fail-safe).
    if min_boundary_contrast is not None and fold.any():
        bc_lbl = label(fold, connectivity=2)
        for region in regionprops(bc_lbl):
            bc = _boundary_contrast(bc_feature, bc_lbl, region, contrast_band_px)
            if bc is not None and bc < min_boundary_contrast:
                fold[bc_lbl == region.label] = False
    return fold


# ------------------------------------------------------------------ F_line line-fold branch

def _support(mask, shape, name):
    values = np.asarray(mask)
    if values.shape != shape or values.dtype.kind not in "buif" or not np.isfinite(values).all():
        raise ValueError(f"{name} must be a finite numeric/boolean mask with shape {shape}")
    return values.astype(bool)


def _positive_mpp(value):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)) or not np.isfinite(value) or value <= 0:
        raise ValueError("thumb_mpp/mpp must be finite and positive")
    return float(value)


def _fline_options(hard_index, min_area):
    if isinstance(hard_index, (bool, np.bool_)) or not isinstance(hard_index, (int, np.integer)) or not 0 <= hard_index <= 255:
        raise ValueError("fline_hard_index must be an integer in [0,255]")
    if min_area is not None and (isinstance(min_area, (bool, np.bool_)) or not isinstance(min_area, (int, float, np.number))
                                 or not np.isfinite(min_area) or min_area < 0):
        raise ValueError("fline minimum area must be finite and non-negative, or None")


def _local_median_mad(D: np.ndarray, mask: np.ndarray, radius: int) -> tuple[np.ndarray, np.ndarray]:
    """Estimate tissue-masked local median and MAD with 8-bit disk rank filters.

    Quantize D between its tissue p0.1 and p99.9. The median has that quantization error.
    MAD is approximate: each pixel’s deviation uses its own local median, rather than
    the center window’s median. Pixels outside mask do not contribute to neighborhoods.
    """
    v = D[mask]
    lo, hi = np.percentile(v, [0.1, 99.9])
    hi = hi if hi > lo else lo + 1e-6
    q = np.clip((D - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
    scale = (hi - lo) / 255.0
    fp = disk(int(radius)).astype(np.uint8)
    med_q = rank_median(q, fp, mask=mask)
    dev = np.abs(q.astype(np.int16) - med_q.astype(np.int16)).astype(np.uint8)
    mad_q = rank_median(dev, fp, mask=mask)
    return med_q.astype(np.float32) * scale + lo, mad_q.astype(np.float32) * scale


def fline_feature(img: np.ndarray, mask: np.ndarray, mpp: float) -> tuple[np.ndarray, dict]:
    """``F_line = R · C · [Z_R]+`` on ``D = ||OD||_2``, at physical scales converted with ``mpp``.

      Z_R = (D - median_ball D) / (MAD_ball D + eps)     ball = FLINE_BALL_UM, eps = FLINE_EPS_FRAC x global MAD
      R   = frangi(D, sigmas = FLINE_SIGMAS_UM / mpp, black_ridges=False)   bright ridges on the darkness map
      C   = (mu1 - mu2) / (mu1 + mu2 + eps_c)              structure-tensor coherence at FLINE_ST_SIGMA_UM
    Returns (F_line float32 HxW, params). Raises on a near-empty mask (caller records ``fline_error``).
    """
    img = np.asarray(img)
    if img.ndim != 3 or img.shape[2] != 3 or img.dtype != np.uint8:
        raise ValueError("fline_feature: expected HxWx3 uint8 RGB image")
    mask = _support(mask, img.shape[:2], "mask")
    mpp = _positive_mpp(mpp)
    if int(mask.sum()) < 10:
        raise ValueError("fline_feature: fewer than 10 tissue pixels")
    D = np.sqrt((_optical_density(img) ** 2).sum(2)).astype(np.float32)
    ball = max(int(round(FLINE_BALL_UM / mpp)), 1)
    sigmas = [max(float(s) / mpp, 0.5) for s in FLINE_SIGMAS_UM]
    st_sigma = max(float(FLINE_ST_SIGMA_UM) / mpp, 0.5)

    med, mad = _local_median_mad(D, mask, ball)
    gmad = float(np.median(np.abs(D[mask] - np.median(D[mask]))))
    eps = float(FLINE_EPS_FRAC) * gmad
    with np.errstate(divide="ignore", invalid="ignore"):
        Z = (D - med) / (mad + eps)
    Zp = np.maximum(np.where(np.isfinite(Z), Z, 0.0), 0.0).astype(np.float32)

    R = frangi(D, sigmas=sigmas, black_ridges=False).astype(np.float32)

    Arr, Arc, Acc = structure_tensor(D, sigma=st_sigma, order="rc")
    mu1, mu2 = structure_tensor_eigenvalues([Arr, Arc, Acc])
    eps_c = 1e-6 * float(np.mean((mu1 + mu2)[mask]))
    denominator = mu1 + mu2 + eps_c
    # A constant field has no orientation. Define its coherence as zero, not NaN.
    C = np.divide(mu1 - mu2, denominator, out=np.zeros_like(mu1), where=denominator > 0)
    C = np.clip(C, 0.0, 1.0).astype(np.float32)

    F = (R * C * Zp).astype(np.float32)
    return F, {"sigmas_px": [round(s, 4) for s in sigmas], "ball_px": ball, "st_sigma_px": round(st_sigma, 4),
               "eps": eps, "global_mad": gmad, "mpp_used": float(mpp)}


def fline_threshold(F: np.ndarray, mask: np.ndarray, hard_index: int = FLINE_HARD_INDEX,
                    min_area_px: int = 0) -> tuple[np.ndarray, dict]:
    """Per-slide p1–p99 stretch of ``F`` over tissue to colour indices 0..255; keep tissue pixels with index
    >= ``hard_index``; drop 8-connected blobs with area < ``min_area_px``. No other filter.

    Indices use ``np.rint`` (nearest integer, ties to even). ``t_raw`` is the nominal raw value for
    the requested index, not the rounding transition: at index 200, the boundary is 199.5/255
    of the p1–p99 span, including the tie. A p1 of zero is common, not guaranteed.
    """
    F = np.asarray(F)
    if F.ndim != 2 or F.dtype.kind not in "buif":
        raise ValueError("fline_threshold: expected a numeric 2D feature map")
    mask = _support(mask, F.shape, "mask")
    _fline_options(hard_index, min_area_px)
    if isinstance(min_area_px, (bool, np.bool_)) or not isinstance(min_area_px, (int, np.integer)):
        raise ValueError("min_area_px must be a non-negative integer")
    v = F[mask]
    v = v[np.isfinite(v)]
    if v.size < 10:
        return np.zeros_like(mask), {"vmin": None, "vmax": None, "t_raw": None,
                                     "n_blobs_before_area": 0, "n_blobs": 0, "degenerate": True}
    lo, hi = np.percentile(v, [1, 99])
    if hi <= lo:
        hi = lo + 1e-9
    scaled = np.where(np.isfinite(F), np.clip((F - lo) / (hi - lo), 0, 1), 0)
    idx = np.rint(255.0 * scaled).astype(np.int16)
    m = (idx >= int(hard_index)) & mask & np.isfinite(F)
    n_before = n_after = 0
    if m.any():
        lbl, n_before = ndi.label(m, structure=_EIGHT_CONN)
        if min_area_px and min_area_px > 1:
            areas = np.bincount(lbl.ravel())[1:]
            keep = np.where(areas >= int(min_area_px))[0] + 1
            m = np.isin(lbl, keep) if keep.size else np.zeros_like(m)
            n_after = int(keep.size)
        else:
            n_after = int(n_before)
    t_raw = float(lo + (int(hard_index) / 255.0) * (hi - lo))
    return m, {"vmin": float(lo), "vmax": float(hi), "t_raw": t_raw,
               "n_blobs_before_area": int(n_before), "n_blobs": n_after, "degenerate": False}


# ------------------------------------------------------------------ routing + orchestrator

def _uses_d_path(stain_type: str | None) -> bool:
    """Return whether the normalized stain selects the d = s-i feature.

    The accepted spellings combine m2.folds.d_path_stains and tissue’s configured Hirano
    set. Whitespace and case normalization match the tissue router.
    """
    from ..tissue.tissue import HIRANO_STAINS
    norm = (stain_type or "").strip().lower()
    return norm in D_PATH_STAINS or norm in HIRANO_STAINS


def _is_hirano(stain_type: str | None) -> bool:
    """Return whether the stain belongs to the configured Hirano set."""
    from ..tissue.tissue import HIRANO_STAINS
    return (stain_type or "").strip().lower() in HIRANO_STAINS


def detect_folds(
    thumbnail: Image.Image,
    tissue_mask: np.ndarray | None = None,
    thumb_mpp: float | None = None,
    stain_type: str | None = None,
    alpha: float = DEFAULT_ALPHA,
    beta: float = _AUTO,                                   # _AUTO -> path default (d 0.34, stain2 0.25)
    min_area_um2: float | None = None,
    opening_radius: int | None = DEFAULT_OPENING_RADIUS,
    speck_max_area_um2: float | None = DEFAULT_SPECK_MAX_AREA_UM2,
    speck_min_circularity: float = cfg("m2.folds.speck_min_circularity", 0.5),
    min_boundary_contrast: float | None = _AUTO,          # _AUTO -> path default (d 0.40, stain2 0.10)
    contrast_band_px: int = DEFAULT_CONTRAST_BAND_PX,
    return_debug: bool = False,
    *,
    pen_mask: np.ndarray | None = None,
    fline: bool = FLINE_ENABLED,                           # F_line line-fold branch, OR-ed in
    fline_hard_index: int = FLINE_HARD_INDEX,
    fline_min_area_um2: float | None = FLINE_MIN_AREA_UM2,  # None -> no area floor on the F_line blobs
) -> dict:
    """Detect tissue folds on the M2 plane. ConnSoftT routed on ``stain_type`` (histochemical -> d = s-i;
    else -> stain2), OR-ed with the F_line line-fold mask.

    ConnSoftT: connectivity curve -> adaptive (alpha/beta) thresholds -> hysteresis -> four post-filters
    (opening -> area -> despeckle -> boundary-contrast). ``beta`` and ``min_boundary_contrast`` default to the
    path-appropriate value (pass an explicit number to override, or ``None`` to disable the gate).
    F_line: ``fline_feature`` -> ``fline_threshold`` (index >= ``fline_hard_index``, blobs >= ``fline_min_area_um2``).
    ``fold_mask = connsoftt | fline``. Degrades gracefully: a ConnSoftT failure (incl. Macenko estimation on a
    degenerate slide) returns ``fold_error``; an F_line failure alone returns the ConnSoftT mask with
    ``fline_error`` set.
    """
    t0 = time.monotonic()
    use_d = _uses_d_path(stain_type) if isinstance(stain_type, (str, type(None))) else False
    method = "d" if use_d else "stain2"
    beta = (D_BETA if use_d else STAIN2_BETA) if beta is _AUTO else beta
    bc_thr = ((DEFAULT_MIN_BOUNDARY_CONTRAST if use_d else STAIN2_MIN_BOUNDARY_CONTRAST)
              if min_boundary_contrast is _AUTO else min_boundary_contrast)

    def _fail(exc):
        return {"pen_subtracted": False, "pen_overlap_fraction": None,
                "fold_area_fraction": None, "fold_mask": None, "t_hard": None, "t_soft": None,
                "n_seed_objects": 0, "min_area_px": None, "opening_radius": opening_radius,
                "speck_max_area_um2": speck_max_area_um2, "speck_min_circularity": speck_min_circularity,
                "min_boundary_contrast": bc_thr, "contrast_band_px": contrast_band_px, "alpha": alpha,
                "beta": beta, "method": method, "stain_type": stain_type, "stain2_vector": None,
                "connsoftt_area_fraction": None, "fline_enabled": bool(fline), "fline_area_fraction": None,
                "fline_n_blobs": None, "fline_t_raw": None, "fline_min_area_px": None, "fline_params": None,
                "fline_error": None, "fline_error_type": None, "fline_runtime_s": None,
                "runtime_s": round(time.monotonic() - t0, 3), "fold_error": f"{type(exc).__name__}: {exc}",
                "fold_error_type": type(exc).__name__}

    try:
        img = np.array(thumbnail.convert("RGB"))
        if stain_type is not None and not isinstance(stain_type, str):
            raise ValueError("stain_type must be a string or None")
        mpp = _positive_mpp(_FALLBACK_MPP if thumb_mpp is None else thumb_mpp)
        if not isinstance(fline, (bool, np.bool_)):
            raise ValueError("fline must be boolean")
        if fline:
            _fline_options(fline_hard_index, fline_min_area_um2)

        if tissue_mask is None:
            from ..tissue.tissue import compute_tissue_mask
            tissue = compute_tissue_mask(thumbnail, stain_type=stain_type)
            if tissue.get("tissue_error"):
                raise ValueError(f"tissue mask failed -> {tissue['tissue_error']}")
            tissue_mask = tissue.get("tissue_mask")
        tissue_mask = _support(tissue_mask, img.shape[:2], "tissue_mask")
        if pen_mask is not None:
            tissue_mask = tissue_mask & ~_support(pen_mask, img.shape[:2], "pen_mask")
        if not tissue_mask.any():
            raise ValueError("empty support: no tissue px to search for folds")

        # ---- feature + sweep grid (the per-path branch).
        stain2_vector = None
        if use_d:
            s, i = rgb_to_hsi(img)
            feature = s - i                                # in [-1, 1]
            ts, counts = connectivity_curve(feature, tissue_mask)
        else:
            feature, stain2_vector = macenko_stain2(img, tissue_mask)
            ts = _ranged_ts(feature, tissue_mask)
            counts = _count_components(feature, tissue_mask, ts)

        # ---- shared core: adaptive thresholds -> hysteresis -> post-filters.
        t_hard, t_soft = resolve_thresholds(ts, counts, alpha, beta)
        fold = _hysteresis(feature, tissue_mask, t_hard, t_soft)

        if min_area_um2 is None:
            min_area_um2 = DEFAULT_MIN_AREA_UM2
        min_area_px = max(int(round(min_area_um2 / (thumb_mpp ** 2))) if thumb_mpp else _FALLBACK_MIN_AREA_PX, 1)
        # Convert physical area thresholds to pixel counts using thumb_mpp.
        speck_max_area_px = (None if speck_max_area_um2 is None
                             else (speck_max_area_um2 / (thumb_mpp ** 2) if thumb_mpp
                                   else _FALLBACK_SPECK_MAX_AREA_PX))

        fold = _postprocess(fold, bc_feature=feature, min_boundary_contrast=bc_thr,
                            opening_radius=opening_radius, min_area_px=min_area_px,
                            speck_max_area_px=speck_max_area_px, speck_min_circularity=speck_min_circularity,
                            contrast_band_px=contrast_band_px)
        connsoftt = fold

        # Compute F_line independently and union it with the ConnSoftT mask.
        fline_mask = None
        fline_map = None
        fline_info = {"fline_enabled": bool(fline), "fline_area_fraction": None, "fline_n_blobs": None,
                      "fline_t_raw": None, "fline_min_area_px": None, "fline_params": None,
                      "fline_error": None, "fline_error_type": None, "fline_runtime_s": None}
        if fline:
            t1 = time.monotonic()
            try:
                fl_min_px = (0 if fline_min_area_um2 is None
                             else max(int(round(float(fline_min_area_um2) / (mpp ** 2))), 1))
                fline_map, fparams = fline_feature(img, tissue_mask, mpp)
                fline_mask, fdiag = fline_threshold(fline_map, tissue_mask, fline_hard_index, fl_min_px)
                fold = connsoftt | fline_mask
                fline_info.update({"fline_n_blobs": fdiag["n_blobs"], "fline_t_raw": fdiag["t_raw"],
                                   "fline_min_area_px": fl_min_px,
                                   "fline_params": {**fparams, "hard_index": int(fline_hard_index),
                                                    "n_blobs_before_area": fdiag["n_blobs_before_area"]}})
            except Exception as exc:  # noqa: BLE001 — degrade to ConnSoftT-only, never lose the slide
                fline_mask = None
                fline_info["fline_error"] = f"{type(exc).__name__}: {exc}"
                fline_info["fline_error_type"] = type(exc).__name__
            fline_info["fline_runtime_s"] = round(time.monotonic() - t1, 3)

        _, n_seeds = ndi.label((feature > t_hard) & tissue_mask, structure=_EIGHT_CONN)
        tissue_px = int(tissue_mask.sum())
        frac = float(fold.sum() / tissue_px) if tissue_px else 0.0
        fline_info["fline_area_fraction"] = (float(fline_mask.sum() / tissue_px)
                                             if (fline_mask is not None and tissue_px) else None)

        result = {
            "fold_area_fraction": frac,
            "fold_mask": fold,
            "tissue_px": tissue_px,
            "pen_subtracted": False,
            "pen_overlap_fraction": None,
            # Retain branch masks so supplied-pen subtraction updates all area measurements.
            "_connsoftt_mask": connsoftt,
            "_fline_mask": fline_mask,
            "t_hard": t_hard,
            "t_soft": t_soft,
            "n_seed_objects": int(n_seeds),
            "min_area_px": min_area_px,
            "opening_radius": opening_radius,
            "speck_max_area_um2": speck_max_area_um2,
            "speck_min_circularity": speck_min_circularity,
            "min_boundary_contrast": bc_thr,
            "contrast_band_px": contrast_band_px,
            "alpha": alpha,
            "beta": beta,
            "method": f"{method}+fline" if fline_mask is not None else method,
            "stain_type": stain_type,
            "stain2_vector": None if stain2_vector is None else stain2_vector.tolist(),
            "connsoftt_area_fraction": float(connsoftt.sum() / tissue_px) if tissue_px else 0.0,
            **fline_info,
            "runtime_s": round(time.monotonic() - t0, 3),
            "fold_error": None,
            "fold_error_type": None,
        }
        if return_debug:
            result["feature_map"] = feature
            result["tissue_mask"] = tissue_mask
            result["C_curve"] = (ts, counts)
            result["connsoftt_mask"] = connsoftt
            result["fline_mask"] = fline_mask
            result["fline_map"] = fline_map
            fl = label(fold, connectivity=2)
            result["blob_stats"] = [
                {"area": int(r.area),
                 "circularity": (round(min(4.0 * np.pi * r.area / (r.perimeter ** 2), 1.0), 3)
                                 if r.perimeter > 0 else 0.0),
                 "boundary_contrast": (
                     None if (bc := _boundary_contrast(feature, fl, r, contrast_band_px)) is None
                     else round(bc, 4))}
                for r in regionprops(fl)
            ]
        return result

    except Exception as e:  # degrade gracefully, like pen/tissue (incl. Macenko-estimation failure)
        return _fail(e)


def generate_fold_overlay(thumbnail: Image.Image, fold_mask: np.ndarray) -> Image.Image:
    """Blend the fold mask (yellow) onto the thumbnail — matches pen/tissue overlay style."""
    overlay = np.array(thumbnail.convert("RGB")).astype(np.float32)
    if fold_mask is not None and fold_mask.any():
        m = fold_mask.astype(bool)
        for c, col in enumerate(COLOR_FOLD):
            overlay[m, c] = overlay[m, c] * (1 - ALPHA_BLEND) + col * ALPHA_BLEND
    return Image.fromarray(np.clip(overlay, 0, 255).astype(np.uint8))


def subtract_pen(result: dict, pen_mask: np.ndarray) -> dict:
    """Subtract a supplied pen mask from computed folds in place.

    Recompute fold fractions with the original tissue denominator. pen_overlap_fraction
    records the excluded overlap as a fraction of tissue.
    """
    fold = result.get("fold_mask")
    if fold is None:
        return result
    pen = np.asarray(pen_mask, dtype=bool)
    if pen.shape != fold.shape:
        result["note"] = f"pen mask {pen.shape} does not match the fold mask {fold.shape}; nothing subtracted"
        return result
    fold = np.asarray(fold, dtype=bool)
    tissue_px = int(result.get("tissue_px") or 0)
    overlap = fold & pen
    kept = fold & ~pen
    for name in ("connsoftt", "fline"):
        branch = result.get("_" + name + "_mask")
        if branch is not None:
            branch = np.asarray(branch, dtype=bool) & ~pen
            result["_" + name + "_mask"] = branch
            result[name + "_area_fraction"] = float(branch.sum() / tissue_px) if tissue_px else None
            if name + "_mask" in result:
                result[name + "_mask"] = branch
            if name == "fline":
                result["fline_n_blobs"] = int(ndi.label(branch, structure=_EIGHT_CONN)[1])
    result.update(fold_mask=kept,
                  fold_area_fraction=float(kept.sum() / tissue_px) if tissue_px else 0.0,
                  pen_overlap_fraction=float(overlap.sum() / tissue_px) if tissue_px else 0.0,
                  pen_subtracted=True)
    return result


def to_report(result: dict) -> dict:
    """JSON-safe subsection of this component's result for `report.json` (masks/arrays dropped)."""
    v = result.get("stain2_vector")
    error = result.get("fold_error")
    if not error and result.get("fline_error"):
        error = f"F_line failed; ConnSoftT retained: {result['fline_error']}"
    return {"fold_area_fraction": result.get("fold_area_fraction"),
            "pen_subtracted": result.get("pen_subtracted"),
            "pen_overlap_fraction": result.get("pen_overlap_fraction"),
            "method": result.get("method"),
            "stain_type": result.get("stain_type"),
            "t_hard": result.get("t_hard"), "t_soft": result.get("t_soft"),
            "min_area_px": result.get("min_area_px"),
            "alpha": result.get("alpha"), "beta": result.get("beta"),
            "min_boundary_contrast": result.get("min_boundary_contrast"),
            "stain2_vector": list(v) if v is not None else None,
            "connsoftt_area_fraction": result.get("connsoftt_area_fraction"),
            "fline_enabled": result.get("fline_enabled"),
            "fline_area_fraction": result.get("fline_area_fraction"),
            "fline_n_blobs": result.get("fline_n_blobs"),
            "fline_t_raw": result.get("fline_t_raw"),
            "fline_min_area_px": result.get("fline_min_area_px"),
            "fline_params": result.get("fline_params"),
            "fline_error": result.get("fline_error"),
            "fline_error_type": error_kind(result.get("fline_error"), result.get("fline_error_type")),
            "fline_runtime_s": result.get("fline_runtime_s"),
            "runtime_s": result.get("runtime_s"),
            "error_type": error_kind(error, result.get("fold_error_type") or result.get("fline_error_type")),
            "error": error}
