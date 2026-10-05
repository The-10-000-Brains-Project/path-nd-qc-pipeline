"""Normalize tissue color using frozen slide parameters and a reference target.

Methods are Macenko stain deconvolution and Reinhard LAB mean/std matching.
The method is selected per call and recorded with its output; the default is Macenko.
Unsupported methods, including Vahadane, return an error rather than falling back.

fit_reference estimates target parameters from a reference image. freeze_slide
estimates source parameters once per slide. normalize applies the frozen parameters
to images or tiles without re-estimation. Estimators use a dense bag of masked pixels;
normalized pixels are scattered into a copy, leaving background byte-identical.

The pipeline supplies the M2 analysis image, normally at 8.0 microns per pixel.
Direct callers can supply an image independently. Shipped reference targets are
placeholders with is_placeholder and fitted_at_mpp metadata carried into reports;
they are not neuropathologist-approved calibration targets. The not_two_dye note
marks conservative stain-model limitations and does not establish the biology of
an individual sample. Biological interpretation requires validation.

The local math follows TIAToolbox tools/stainextract, tools/stainnorm, and
utils/transforms, cited at the relevant functions. TIAToolbox is not a dependency.
Use Python 3.12+ with dependencies from src/pyproject.toml.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from pathnd_qc._logging import get_logger
from pathnd_qc.config.config import cfg, error_kind

logger = get_logger(__name__)

METHODS = tuple(cfg("m4.methods", ["macenko", "reinhard"]))
DEFAULT_METHOD = cfg("m4.method", "macenko")

# TIAToolbox constants [DOC]
OD_EPS = cfg("m4.od_eps", 1e-6)               # utils/transforms: floor on both rgb2od and od2rgb
ANGULAR_PERCENTILE = cfg("m4.angular_percentile", 99.0)   # MacenkoExtractor default
LAB_L_SCALE = 2.55                            # stainnorm.ReinhardNormalizer.lab_split: L /= 2.55
LAB_AB_OFFSET = 128.0                         # stainnorm.ReinhardNormalizer.lab_split: a,b -= 128

EPS = cfg("m4.eps", 1e-8)
MIN_TISSUE_PX = cfg("m4.min_tissue_px", 3)
# Map raw stain spellings to normalization reference keys, including aliases
# such as LFB/H&E -> LFB-HE and Beta-Amy -> AMYB.
STAIN_ALIASES = {k.strip().lower(): v for k, v in (cfg("m4.stain_aliases", {}) or {}).items()}
PROVENANCE_DIR = cfg("m4.provenance_dir", "reports")
from pathnd_qc import __version__

MODULE_VERSION = __version__

# Attach conservative two-dye interpretation notes for these stain labels.
NOT_TWO_DYE = {
    "hirano": "silver-type stain — not a two-chromogen system",
    "lfb-he": "LFB + H&E — three dyes, not two",
    "lfb/h&e": "LFB + H&E — three dyes, not two",
    "lfb": "LFB + H&E — three dyes, not two",
    "he": "haematoxylin + eosin — two dyes, but no DAB channel",
}

VAHADANE_REASON = (
    "vahadane is not implemented: faithfully reproduced it returns physically impossible NEGATIVE "
    "optical-density components on every slide tested (30/30, log.md 2026-08-17), because TIAToolbox "
    "applies three of the paper's four structural constraints to the wrong matrix factor. Use "
    "'macenko' or 'reinhard'."
)


# --------------------------------------------------------------------------- input normalisation

from pathnd_qc._images import as_rgb_array as _as_rgb_array



def _check_method(method: str) -> str:
    """Validate and normalise a method name. `vahadane` raises WITH THE REASON, never falls back."""
    norm = (method or "").strip().lower()
    if norm == "vahadane":
        raise ValueError(VAHADANE_REASON)
    if norm not in METHODS:
        raise ValueError(f"unknown method {method!r}; expected one of {list(METHODS)}")
    return norm


def _bag(rgb: np.ndarray, tissue_mask=None) -> np.ndarray:
    """Masked pixels as a dense `(1, N, 3)` uint8 bag; the whole image when no mask is given.

    This is the step that makes tissue-only estimation EXACT rather than approximate — see the module
    docstring. The bag keeps C order, which is what lets `_scatter` put the result back correctly.
    """
    if tissue_mask is None:
        return rgb.reshape((1, -1, 3)).astype(np.uint8)
    mask = np.asarray(tissue_mask, dtype=bool)
    if mask.shape != rgb.shape[:2]:
        raise ValueError(f"tissue_mask shape {mask.shape} != image shape {rgb.shape[:2]}")
    if not mask.any():
        raise ValueError("empty tissue mask: no pixels to estimate from")
    return rgb[mask].reshape((1, -1, 3)).astype(np.uint8)


def _scatter(original_rgb: np.ndarray, tissue_mask, normalized_bag: np.ndarray) -> np.ndarray:
    """Scatter the normalized pixels back into a COPY of the original.

    Only `mask == True` positions are written, so the background is left **byte-identical** to the
    input — which is what "normalize the tissue and not the background" means concretely.
    """
    out = original_rgb.copy()
    flat = normalized_bag.reshape((-1, 3))
    if tissue_mask is None:
        return flat.reshape(original_rgb.shape)
    out[np.asarray(tissue_mask, dtype=bool)] = flat
    return out


# --------------------------------------------------------------------------- optical density [DOC]

def rgb2od(img: np.ndarray) -> np.ndarray:
    """Convert RGB to OD = max(-ln(I / 255), OD_EPS), mapping zero intensity to one.

    Copy the input before replacing zeros. This uses natural-log optical density;
    fold detection uses a separate log10 formulation with intensity offsets.
    Reference: TIAToolbox utils/transforms.rgb2od.
    """
    out = np.asarray(img, dtype=np.float64).copy()
    out[out == 0] = 1.0
    return np.maximum(-np.log(out / 255.0), OD_EPS)


def od2rgb(od: np.ndarray) -> np.ndarray:
    """Convert optical density to uint8 RGB with 255 * exp(-max(OD, OD_EPS)).

    Render stain vectors as color swatches. Reference: TIAToolbox utils/transforms.od2rgb.
    """
    return (255.0 * np.exp(-np.maximum(np.asarray(od, dtype=np.float64), OD_EPS))).astype(np.uint8)


def _vectors_in_correct_direction(e_vectors: np.ndarray) -> np.ndarray:
    """Orient each eigenvector so its first component is nonnegative (TIAToolbox stainextract)."""
    out = np.asarray(e_vectors, dtype=np.float64).copy()
    if out[0, 0] < 0:
        out[:, 0] *= -1
    if out[0, 1] < 0:
        out[:, 1] *= -1
    return out


def _h_and_e_in_right_order(v1: np.ndarray, v2: np.ndarray) -> np.ndarray:
    """Order stain vectors by descending red-channel optical density.

    Reference: TIAToolbox stainextract.h_and_e_in_right_order. This is a heuristic for
    a hematoxylin plus chromogen model; interpretation is limited for silver and
    multidye stains. Fold detection uses blue-channel ordering for its own feature.
    """
    return np.array([v1, v2]) if v1[0] > v2[0] else np.array([v2, v1])


def _l2_normalise_rows(matrix: np.ndarray) -> np.ndarray:
    """Normalize each matrix row to unit L2 norm."""
    return matrix / np.linalg.norm(matrix, axis=1)[:, None]


def _as_od_rows(bag: np.ndarray, minimum: int = MIN_TISSUE_PX) -> np.ndarray:
    """Bag -> (N, 3) OD rows, with the too-few-pixels guard the callers degrade on."""
    od = rgb2od(bag).reshape((-1, 3))
    if od.shape[0] < minimum:
        raise ValueError(f"too few tissue pixels to estimate stain vectors: {od.shape[0]} < {minimum}")
    return od


def _swatches(stain_matrix: np.ndarray) -> list:
    """Each stain vector rendered as its own colour, via TIAToolbox's own back-conversion."""
    return [[int(c) for c in od2rgb(row)] for row in stain_matrix]


# --------------------------------------------------------------------------- Macenko [DOC]

def macenko_stain_matrix(bag: np.ndarray,
                         angular_percentile: float = ANGULAR_PERCENTILE) -> np.ndarray:
    """Estimate a two-row Macenko stain matrix from the supplied tissue pixels.

    Compute OD covariance and retain its two largest eigenvectors. Orient the basis,
    project OD into it, and use angular percentiles to choose two stain directions.
    Order by red optical density and normalize each row. The supplied tissue support
    is authoritative; no extra luminosity mask is applied.
    Reference: TIAToolbox stainextract.MacenkoExtractor.get_stain_matrix.
    """
    od = _as_od_rows(bag)
    _, eigen_vectors = np.linalg.eigh(np.cov(od, rowvar=False))
    eigen_vectors = _vectors_in_correct_direction(eigen_vectors[:, [2, 1]])

    projection = od @ eigen_vectors
    phi = np.arctan2(projection[:, 1], projection[:, 0])
    min_phi = float(np.percentile(phi, 100.0 - angular_percentile))
    max_phi = float(np.percentile(phi, angular_percentile))

    v1 = eigen_vectors @ np.array([np.cos(min_phi), np.sin(min_phi)])
    v2 = eigen_vectors @ np.array([np.cos(max_phi), np.sin(max_phi)])
    return _l2_normalise_rows(_h_and_e_in_right_order(v1, v2))


def get_concentrations(bag: np.ndarray, stain_matrix: np.ndarray) -> np.ndarray:
    """Solve S.T * concentrations.T = OD.T by unconstrained least squares.

    Return an (N, 2) concentration array. Negative concentrations are permitted;
    RGB reconstruction clips values before conversion to uint8.
    Reference: TIAToolbox stainnorm.StainNormalizer.get_concentrations.
    """
    od = rgb2od(bag).reshape((-1, 3))
    concentrations, *_ = np.linalg.lstsq(stain_matrix.T, od.T, rcond=-1)
    return concentrations.T


def _fit_macenko(bag: np.ndarray) -> dict:
    """[DOC — stainnorm.StainNormalizer.fit] stain matrix + the **99th percentile** of the fitted
    slide's concentrations (`maxC`), which is what a source slide is rescaled onto."""
    stain_matrix = macenko_stain_matrix(bag)
    concentrations = get_concentrations(bag, stain_matrix)
    max_c = np.percentile(concentrations, 99, axis=0)
    return {"method": "macenko",
            "stain_matrix": [[float(v) for v in row] for row in stain_matrix],
            "maxC": [float(v) for v in max_c],
            "swatch_rgb": _swatches(stain_matrix),
            "n_px": int(bag.shape[1])}


def _apply_macenko(bag: np.ndarray, target: dict, source: dict) -> np.ndarray:
    """Normalize with frozen source and target Macenko parameters.

    Deconvolve with the source matrix, scale concentrations by target/source maxC,
    then reconstruct through the target matrix. Clip RGB to [0, 255] before the uint8
    cast so negative least-squares concentrations cannot wrap bright pixels to dark.
    Reference: TIAToolbox stainnorm.StainNormalizer.transform.
    """
    source_matrix = np.asarray(source["stain_matrix"], dtype=np.float64)
    target_matrix = np.asarray(target["stain_matrix"], dtype=np.float64)
    max_c_source = np.asarray(source["maxC"], dtype=np.float64)
    max_c_target = np.asarray(target["maxC"], dtype=np.float64)

    concentrations = get_concentrations(bag, source_matrix)
    concentrations = concentrations * (max_c_target / np.maximum(max_c_source, EPS))

    trans = 255.0 * np.exp(-concentrations @ target_matrix)
    return np.clip(trans, 0, 255).astype(np.uint8).reshape(bag.shape)


# --------------------------------------------------------------------------- Reinhard [DOC]

def _lab_split(bag: np.ndarray):
    """[DOC — stainnorm.ReinhardNormalizer.lab_split] cv2 RGB2LAB -> float32 -> L/2.55, a-128, b-128.

    The numbers therefore live in **OpenCV LAB rescaled to L in [0, 100], a,b in [-128, 127]**;
    skimage's LAB would NOT be comparable, which is why the convention is pinned in the params.
    """
    lab = cv2.cvtColor(np.ascontiguousarray(bag, dtype=np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
    chan1, chan2, chan3 = cv2.split(lab)
    return (chan1 / np.float32(LAB_L_SCALE),
            chan2 - np.float32(LAB_AB_OFFSET),
            chan3 - np.float32(LAB_AB_OFFSET))


def _merge_back(chan1, chan2, chan3) -> np.ndarray:
    """[DOC — stainnorm.ReinhardNormalizer.merge_back] undo the rescaling, clip, LAB2RGB."""
    merged = cv2.merge((chan1 * np.float32(LAB_L_SCALE),
                        chan2 + np.float32(LAB_AB_OFFSET),
                        chan3 + np.float32(LAB_AB_OFFSET)))
    return cv2.cvtColor(np.clip(merged, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)


def _fit_reinhard(bag: np.ndarray) -> dict:
    """[DOC — stainnorm.ReinhardNormalizer.get_mean_std] per-channel LAB mean/std over the bag.

    Unlike the deconvolution methods this has no stain vectors; the "swatch" is the mean colour,
    obtained by pushing the mean LAB triple back through upstream's `merge_back`.
    """
    channels = _lab_split(bag)
    means, stds = [], []
    for channel in channels:
        mean, std = cv2.meanStdDev(np.asarray(channel))
        means.append(float(mean[0][0]))
        stds.append(float(std[0][0]))
    swatch = _merge_back(np.float32([[means[0]]]), np.float32([[means[1]]]), np.float32([[means[2]]]))
    return {"method": "reinhard", "means": means, "stds": stds,
            "mean_swatch_rgb": [int(c) for c in swatch[0, 0]],
            "lab_convention": "opencv_rgb2lab_rescaled_L0-100_ab-128-127",
            "n_px": int(bag.shape[1])}


def _apply_reinhard(bag: np.ndarray, target: dict, source: dict) -> np.ndarray:
    """[DOC — stainnorm.ReinhardNormalizer.transform] per channel:
    `((x - mean_src) * (std_tgt / std_src)) + mean_tgt`.

    A zero source std (a channel with no variation at all) would divide by zero; it is floored, which
    leaves that channel a flat shift instead of exploding.
    """
    means, stds = source["means"], source["stds"]
    channels = _lab_split(bag)
    normalized = [((channels[i] - means[i]) * (target["stds"][i] / max(float(stds[i]), EPS)))
                  + target["means"][i] for i in range(3)]
    return _merge_back(*normalized)


_FIT = {"macenko": _fit_macenko, "reinhard": _fit_reinhard}
_APPLY = {"macenko": _apply_macenko, "reinhard": _apply_reinhard}


# --------------------------------------------------------------------------- public API

def freeze_slide(image, tissue_mask=None, *, method: str = DEFAULT_METHOD,
                 stain_type: Optional[str] = None) -> dict:
    """Estimate source stain parameters once for reuse by normalize().

    Returns result, params, runtime_s, and error; result is None on failure.
    """
    started = time.monotonic()
    out = {"result": None, "params": {"method": method, "stain_type": stain_type},
           "runtime_s": 0.0, "error": None}
    try:
        norm = _check_method(method)
        rgb = _as_rgb_array(image)
        params = _FIT[norm](_bag(rgb, tissue_mask))
        params["stain_type"] = stain_type
        params["tissue_mask_used"] = tissue_mask is not None
        # Use the reference lookup’s alias table to annotate equivalent stain spellings consistently.
        raw = (stain_type or "").strip().lower()
        canon = str(STAIN_ALIASES.get(raw) or raw).strip().lower()
        if stain_type and canon in NOT_TWO_DYE and norm == "macenko":
            params["not_two_dye"] = NOT_TWO_DYE[canon]
        out["result"] = params
        out["params"] = {"method": norm, "stain_type": stain_type,
                         "n_px": params["n_px"], "tissue_mask_used": tissue_mask is not None}
    except Exception as exc:                                     # noqa: BLE001 - degrade, never raise
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["error_type"] = type(exc).__name__
        logger.warning("freeze_slide failed: %s", out["error"])
    out["runtime_s"] = round(time.monotonic() - started, 4)
    return out


def fit_reference(image, tissue_mask=None, *, method: str = DEFAULT_METHOD,
                  stain_type: Optional[str] = None, reference_slide_id: Optional[str] = None,
                  is_placeholder: bool = True, fitted_at_mpp: Optional[float] = None) -> dict:
    """Fit target parameters from a reference image using the source-fitting math.

    is_placeholder defaults to True and is included in reporting. Pass False only
    for an approved reference; fitted_at_mpp records the reference image’s scale.
    """
    out = freeze_slide(image, tissue_mask, method=method, stain_type=stain_type)
    if out["result"] is not None:
        out["result"].update(reference_slide_id=reference_slide_id,
                             is_placeholder=bool(is_placeholder),
                             fitted_at_mpp=fitted_at_mpp)
    return out


def load_reference(stain_type: Optional[str], *, method: str = DEFAULT_METHOD,
                   bank: Optional[str] = None, spec=None) -> dict:
    """Resolve frozen target parameters from configuration or a supplied reference spec.

    Try stain/bank keys before stain-only keys. Matching ignores case and consults
    m4.stain_aliases, such as LFB/H&E -> LFB-HE and Beta-Amy -> AMYB.

    spec accepts a stain-keyed mapping or a single entry containing macenko. Wrap a
    Reinhard-only entry in a stain-keyed mapping. Frozen target parameters go directly
    to normalize(). Returns result, params, runtime_s, and error. Reference metadata,
    including is_placeholder and fitted_at_mpp, accompanies the selected parameters.
    """
    started = time.monotonic()
    key = (stain_type or "").strip()
    out = {"result": None, "params": {"method": method, "stain_type": stain_type, "bank": bank,
                                      "reference_key": None}, "runtime_s": 0.0, "error": None}
    try:
        norm = _check_method(method)
        table = spec if isinstance(spec, dict) and "macenko" not in spec else None
        if table is None and isinstance(spec, dict):
            entry, matched = spec, key                       # Use a directly supplied reference entry.
        else:
            table = table if table is not None else (cfg("m4.reference", {}) or {})
            entry = matched = None
            lowered = {k.strip().lower(): k for k in table}
            alias = STAIN_ALIASES.get(key.lower())
            names = [key] + ([alias] if alias and alias != key else [])
            candidates = ([f"{n}/{bank}" for n in names] if bank else []) + names
            for candidate in candidates:
                hit = lowered.get(candidate.strip().lower())
                if hit is not None:
                    entry, matched = table[hit], hit
                    break
            if entry is None:
                raise KeyError(f"no reference for stain_type {stain_type!r}"
                               + (f" / bank {bank!r}" if bank else "")
                               + f"; tried {candidates}; configured keys: {sorted(table)}")
        if norm not in entry:
            raise KeyError(f"reference {matched!r} has no {norm!r} parameters")

        target = dict(entry[norm])
        target.update(method=norm, stain_type=stain_type,
                      reference_slide_id=entry.get("reference_slide_id"),
                      dataset=entry.get("dataset"),
                      fitted_at_mpp=entry.get("fitted_at_mpp"),
                      is_placeholder=bool(entry.get("is_placeholder", True)))
        if norm == "macenko":
            target.setdefault("swatch_rgb",
                              _swatches(np.asarray(target["stain_matrix"], dtype=np.float64)))
        out["result"] = target
        out["params"] = {"method": norm, "stain_type": stain_type, "bank": bank,
                         "reference_key": matched,
                         "reference_slide_id": target.get("reference_slide_id"),
                         "is_placeholder": target["is_placeholder"],
                         "fitted_at_mpp": target.get("fitted_at_mpp")}
    except Exception as exc:                                     # noqa: BLE001 - degrade, never raise
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["error_type"] = type(exc).__name__
        logger.warning("load_reference failed: %s", out["error"])
    out["runtime_s"] = round(time.monotonic() - started, 4)
    return out


def normalize(image, target: dict, source: dict, *, method: str = DEFAULT_METHOD,
              tissue_mask=None) -> dict:
    """Normalize an image onto `target` using ALREADY-FROZEN `source` params. NO re-estimation.

    Only masked pixels are transformed; the background is copied through byte-identical. Safe to call
    per tile — neither `target` nor `source` is mutated.

    Returns {result (uint8 RGB array), params, runtime_s, error}.
    """
    started = time.monotonic()
    out = {"result": None, "params": {"method": method}, "runtime_s": 0.0, "error": None}
    try:
        norm = _check_method(method)
        for name, prm in (("target", target), ("source", source)):
            if not isinstance(prm, dict):
                raise TypeError(f"{name} params must be a dict, got {type(prm).__name__}")
            if prm.get("method") not in (None, norm):
                raise ValueError(f"{name} params were fitted with method {prm['method']!r}, "
                                 f"but normalize was called with {norm!r}")
        rgb = _as_rgb_array(image)
        bag = _bag(rgb, tissue_mask)
        out["result"] = _scatter(rgb, tissue_mask, _APPLY[norm](bag, target, source))
        out["params"] = {"method": norm,
                         "stain_type": source.get("stain_type") or target.get("stain_type"),
                         "reference_slide_id": target.get("reference_slide_id"),
                         "is_placeholder": bool(target.get("is_placeholder", True)),
                         "reference_fitted_at_mpp": target.get("fitted_at_mpp"),
                         "n_tissue_px": int(bag.shape[1]),
                         "tissue_mask_used": tissue_mask is not None}
    except Exception as exc:                                     # noqa: BLE001 - degrade, never raise
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["error_type"] = type(exc).__name__
        logger.warning("normalize failed: %s", out["error"])
    out["runtime_s"] = round(time.monotonic() - started, 4)
    return out


def normalize_slide(image, target: dict, *, method: str = DEFAULT_METHOD, tissue_mask=None,
                    stain_type: Optional[str] = None) -> dict:
    """`freeze_slide` + `normalize` in one call — the whole-slide convenience path.

    The frozen source params are returned under `params["source"]` so a caller can reuse them for
    that slide's tiles without re-estimating.
    """
    started = time.monotonic()
    frozen = freeze_slide(image, tissue_mask, method=method, stain_type=stain_type)
    if frozen["error"]:
        return {"result": None, "params": {"method": method, "stain_type": stain_type},
                "runtime_s": frozen["runtime_s"], "error": frozen["error"],
                "error_type": error_kind(frozen["error"], frozen.get("error_type"))}
    out = normalize(image, target, frozen["result"], method=method, tissue_mask=tissue_mask)
    out["params"]["source"] = frozen["result"]
    out["params"]["target"] = target
    out["runtime_s"] = round(time.monotonic() - started, 4)
    return out


# --------------------------------------------------------------------------- provenance

def _params_hash(params: dict) -> str:
    """SHA-256 over the canonical JSON of a params dict — stable across key order."""
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()


def slide_record(slide_id: str, stain_type: Optional[str], target: Optional[dict],
                 source: Optional[dict], *, method: str = DEFAULT_METHOD,
                 extra: Optional[dict] = None) -> dict:
    """Build slide-level normalization provenance referenced by per-tile records."""
    record = {
        "slide_id": slide_id,
        "stain_type": stain_type,
        "method": method,
        "module_version": MODULE_VERSION,
        "reference_slide_id": (target or {}).get("reference_slide_id"),
        "reference_hash": _params_hash(target) if target else None,
        "is_placeholder": bool((target or {}).get("is_placeholder", True)),
        "reference_fitted_at_mpp": (target or {}).get("fitted_at_mpp"),
        "target_params": target,
        "source_params": source,
        "tissue_mask_used": bool((source or {}).get("tissue_mask_used")),
        "n_tissue_px": (source or {}).get("n_px"),
    }
    if extra:
        record.update(extra)
    return record


def tile_record(tile_id, source_xywh, normalized_path, provenance_ref: str) -> dict:
    """Per-tile record pointing at a slide record's `reference_hash`.

    This standalone helper is not called by the pipeline. Its record is separate from M3's
    saved tile list and measurement records; callers assemble this normalization provenance.
    """
    return {"tile_id": tile_id,
            "source_xywh": list(source_xywh) if source_xywh is not None else None,
            "normalized_path": str(normalized_path) if normalized_path is not None else None,
            "provenance_ref": provenance_ref}


def write_provenance(out_dir: Optional[str], record: dict, tile_records=()) -> Path:
    """Write `<out_dir>/<slide_id>_stainnorm.json`. Creates the directory if needed."""
    out_dir = Path(out_dir or os.environ.get("PATHND_REPORT_DIR") or PROVENANCE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{record.get('slide_id', 'slide')}_stainnorm.json"
    payload = dict(record)
    payload["tiles"] = list(tile_records)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
    logger.info("Wrote %s", path)
    return path


# --------------------------------------------------------------------------- reporting

def to_report(result: dict) -> dict:
    """JSON-safe subsection of this component's result for `report.json` (the image is dropped).

    `is_placeholder` and `reference_fitted_at_mpp` are surfaced deliberately and near the top: with
    no approved reference (ES-1) and placeholder targets fitted at 3.69-16.16 um/px against an
    8.0 um/px analysis plane, a reader who cannot see those two fields would have no way to know the
    output is not calibrated.
    """
    params = dict((result or {}).get("params") or {})
    source = params.get("source") or {}
    target = params.get("target") or {}
    report = {
        "method": params.get("method"),
        "stain_type": params.get("stain_type"),
        "normalized": (result or {}).get("result") is not None,
        "is_placeholder": params.get("is_placeholder", target.get("is_placeholder")),
        "reference_slide_id": params.get("reference_slide_id"),
        "reference_key": params.get("reference_key"),   # Selected configuration reference key.
        "bank": params.get("bank"),                     # the bank requested; a fallback is visible
        "reference_fitted_at_mpp": params.get("reference_fitted_at_mpp"),
        "n_tissue_px": params.get("n_tissue_px"),
        "tissue_mask_used": params.get("tissue_mask_used"),
        "not_validated": ("no neuropathologist-approved reference exists (ES-1) and the shipped "
                          "targets are PLACEHOLDERS fitted at a different MPP from the analysis "
                          "plane — these outputs are diagnostics, not calibrated normalization"),
        "runtime_s": (result or {}).get("runtime_s"),
        "error": (result or {}).get("error"),
        "error_type": error_kind((result or {}).get("error"), (result or {}).get("error_type")),
    }
    for name, prm in (("source_params", source), ("target_params", target)):
        if prm:
            report[name] = {k: v for k, v in prm.items()
                            if k in ("method", "stain_matrix", "maxC", "means", "stds",
                                     "swatch_rgb", "mean_swatch_rgb", "n_px", "not_two_dye")}
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rng = np.random.default_rng(0)
    demo = np.clip(np.full((64, 64, 3), 200, dtype=np.int16)
                   + rng.integers(-30, 30, (64, 64, 3)), 0, 255).astype(np.uint8)
    mask = np.zeros((64, 64), dtype=bool)
    mask[8:56, 8:56] = True
    for m in METHODS:
        ref = load_reference("LFB-HE", method=m)
        res = normalize_slide(demo, ref["result"], method=m, tissue_mask=mask,
                              stain_type="LFB/H&E")
        print(f"{m:9s} error={res['error']} "
              f"placeholder={res['params'].get('is_placeholder')} "
              f"fitted_at_mpp={res['params'].get('reference_fitted_at_mpp')} "
              f"bg_unchanged={np.array_equal(res['result'][~mask], demo[~mask])}")
