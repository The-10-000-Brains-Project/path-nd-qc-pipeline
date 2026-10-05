"""Detect tile artifacts with GrandQC using a local slide and its full class mask.

The subprocess backend selects the requested checkpoint or the finest available
fallback at 1.0, 1.5, or 2.0 microns per pixel. Per-tile fractions and pen polygons
come from the resulting mask. Tile coordinates use the analysis plane, normally
0.50 microns per pixel; the MPP ratio maps them into model pixels. Mask dimensions
are checked against the expected physical field before measurement.

Class IDs are 1 tissue, 2 folds, 3 darkspot/foreign object, 4 pen,
5 edge/air bubble, 6 out-of-focus, and 7 background. Configured fraction thresholds
flag tiles without dropping them. These outputs are not ground-truth validated
and do not gate pipeline stages. A requested component requires a configured
GrandQC checkout; setup or inference failures are reported as errors.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
from PIL import Image

from pathnd_qc.config.config import cfg, error_kind, resolve_path
from pathnd_qc.qc_tile.tiles.tiles import TILE_PX
from pathnd_qc.qc_tile.tile_metrics.tile_metrics import TILE_TARGET_MPP
from .artifacts_tile import (ARTIFACT_CLASSES as _TILE_CLASSES, compute_tile_artifacts,
                             resolve_model_mpp, run_grandqc_full_mask)

# ---------------------------------------------------------------------------- the class scheme
CLASSES = {"tissue": 1, "fold": 2, "darkspot": 3, "pen": 4, "bubble": 5, "oof": 6, "background": 7}
CLASS_NAMES = {v: k for k, v in CLASSES.items()}
NULL_CLASS = 0
BACKGROUND_CLASS = CLASSES["background"]

# ---------------------------------------------------------------------------- config
ARTIFACT_CLASSES = tuple(cfg("m3.artifacts.classes", ["fold", "pen", "bubble"]))
MODEL_MPP = float(cfg("m3.artifacts.model_mpp", 1.0))          # requested; the checkout decides
MIN_FRACTION = cfg("m3.artifacts.min_fraction", None)
DEFAULT_REPO_PATH = resolve_path(cfg("m3.artifacts.repo_path", None))
DEVICE = cfg("m3.artifacts.device", "cpu")
BACKEND = "grandqc-subprocess"
REQUIRED_SCRIPTS = ("wsi_tis_detect.py", "main.py")
MASK_DIMS_TOL = 0.02                       # mask vs plane-at-model-MPP size agreement, relative
ALPHA_BLEND = 0.5                          # overlay opacity, mirroring the M2 generate_*_overlay
# Default overlay classes exclude tissue/background; custom classes can include them.
CLASS_COLORS = {1: (60, 180, 75), 2: (230, 25, 75), 3: (145, 30, 180), 4: (0, 130, 200),
                5: (245, 130, 48), 6: (255, 225, 25), 7: (128, 128, 128)}


def _resolve_class(cls) -> tuple[str, int]:
    """Accept a class NAME or its ID; return (name, id). Refuse anything outside the scheme."""
    if isinstance(cls, str):
        if cls not in CLASSES:
            raise ValueError(f"unknown artifact class {cls!r}; valid: {sorted(CLASSES)}")
        return cls, CLASSES[cls]
    cid = int(cls)
    if cid not in CLASS_NAMES:
        raise ValueError(f"unknown artifact class id {cid}; valid: {sorted(CLASS_NAMES)}")
    return CLASS_NAMES[cid], cid


# ---------------------------------------------------------------------------- the checkout
def check_grandqc_repo(path) -> str | None:
    """Why `path` cannot serve as the GrandQC checkout, or None when it can. Pre-flight material."""
    if not path:
        return "no path given"
    p = Path(path)
    if not p.is_dir():
        return f"not a directory: {p}"
    missing = [s for s in REQUIRED_SCRIPTS if not (p / s).is_file()]
    if missing:
        return (f"{p} is not the GrandQC inference checkout: missing {', '.join(missing)} "
                f"(expected .../01_WSI_inference_OPENSLIDE_QC; run 'pathnd-qc setup grandqc')")
    if not (p / "models/td/Tissue_Detection_MPP10.pth").is_file():
        return f"{p} is missing models/td/Tissue_Detection_MPP10.pth"
    if resolve_model_mpp(str(p), MODEL_MPP) is None:
        return f"{p} has no artifact checkpoint under models/qc/ (GrandQC_MPP1/15/2.pth)"
    return None


def resolve_repo(flag_value) -> tuple[str | None, str | None]:
    """The flag wins over config. Preserve invalid paths so preflight names the setup error."""
    if flag_value:
        return str(flag_value), "flag"
    if DEFAULT_REPO_PATH:
        return str(DEFAULT_REPO_PATH), "config"
    return None, None


# ---------------------------------------------------------------------------- per-tile
def artifact_tile_metrics(class_mask: np.ndarray, model_mpp: float, tiles: list[dict],
                          plane_dims: tuple[int, int], *,
                          classes=ARTIFACT_CLASSES, min_fraction: float | None = MIN_FRACTION,
                          plane_mpp: float = TILE_TARGET_MPP) -> dict:
    """Per-tile area fraction of each requested class, from the full class mask. PURE.

    Each tile (plane px, `tiles.py` geometry) is cut from the mask by
    `artifacts_tile.compute_tile_artifacts` with the plane as the "slide" frame at `plane_mpp`.
    `min_fraction` (None = record only) sets `has_artifact` and `artifact_label`; there is no
    calibrated value for it, so the honest default records the fractions and judges nothing.
    The mask's size is checked against the plane first: a mask at the wrong scale is an error.

    Returns {tiles: [{col, row, x, y, w, h, artifact_fractions, background_fraction, has_artifact,
    artifact_label, pen_polygons}], classes, n_flagged, n_flagged_by_class, min_fraction,
    mask_dims, artifact_error}.
    """
    try:
        if not classes:
            raise ValueError("classes is empty — pass at least one class, or omit for the default")
        resolved = [_resolve_class(c) for c in classes]
        unknown = [n for n, _ in resolved if n not in _TILE_CLASSES]
        if unknown:
            raise ValueError(f"{unknown} are not artifact classes GrandQC reports per tile; "
                             f"valid: {sorted(_TILE_CLASSES)}")
        if class_mask is None:
            raise ValueError("no class mask")
        cmask = np.asarray(class_mask)
        pw, ph = plane_dims
        scale = plane_mpp / float(model_mpp)                    # plane px -> mask px
        exp_w, exp_h = pw * scale, ph * scale
        mh, mw = cmask.shape[:2]
        if abs(mw - exp_w) / max(exp_w, 1) > MASK_DIMS_TOL or abs(mh - exp_h) / max(exp_h, 1) > MASK_DIMS_TOL:
            raise ValueError(f"class mask is {mw}x{mh} but the {plane_mpp} um/px plane {list(plane_dims)} "
                             f"at model mpp {model_mpp} should be ~{exp_w:.0f}x{exp_h:.0f}: GrandQC "
                             f"ran at a different scale than recorded")
        out: list[dict] = []
        by_class = {name: 0 for name, _ in resolved}
        n_flagged = 0
        for t in tiles:
            w, h = int(t.get("w") or TILE_PX), int(t.get("h") or TILE_PX)
            geom = {"x": int(t["x"]), "y": int(t["y"]), "width": w, "height": h}
            per = compute_tile_artifacts(cmask, float(model_mpp), plane_mpp, geom)
            fracs = {name: round(float(per["artifact_fractions"].get(name, 0.0)), 4) for name, _ in resolved}
            if min_fraction is None:
                label, flagged = None, False
            else:
                over = {k: v for k, v in fracs.items() if v >= min_fraction}
                label = max(over, key=over.get) if over else None   # dominant flagged class
                flagged = label is not None
                for k, v in fracs.items():
                    if v >= min_fraction:
                        by_class[k] += 1
            n_flagged += int(flagged)
            out.append({"col": t.get("col"), "row": t.get("row"), "x": geom["x"], "y": geom["y"],
                        "w": w, "h": h, "artifact_fractions": fracs,
                        "background_fraction": round(float(per["background_fraction"]), 4),
                        "has_artifact": flagged, "artifact_label": label,
                        "pen_polygons": per["pen_polygons"]})
        return {"tiles": out, "classes": [n for n, _ in resolved], "n_flagged": n_flagged,
                "n_flagged_by_class": by_class, "min_fraction": min_fraction,
                "mask_dims": [int(mw), int(mh)], "artifact_error": None}
    except Exception as e:                     # noqa: BLE001
        return {"tiles": [], "classes": [], "n_flagged": 0, "n_flagged_by_class": {},
                "min_fraction": min_fraction, "mask_dims": None,
                "artifact_error": f"{type(e).__name__}: {e}", "artifact_error_type": type(e).__name__}


def merge_tile_artifacts(tile_metrics_result: dict, artifact_result: dict) -> dict:
    """Copy artifact fields into tile metrics by matching (col, row).

    The input records are not mutated and kept is unchanged. Consumers can filter on
    has_artifact independently of the tissue-fraction drop policy.
    """
    by_key = {(r.get("col"), r.get("row")): r for r in (artifact_result.get("tiles") or [])}
    merged = []
    for rec in (tile_metrics_result.get("tiles") or []):
        rec = dict(rec)
        a = by_key.get((rec.get("col"), rec.get("row")))
        rec["artifact_fractions"] = a["artifact_fractions"] if a else None
        rec["has_artifact"] = a["has_artifact"] if a else None
        rec["artifact_label"] = a["artifact_label"] if a else None
        merged.append(rec)
    out = dict(tile_metrics_result)
    out["tiles"] = merged if tile_metrics_result.get("tiles") is not None else None
    out["n_artifact_tiles"] = artifact_result.get("n_flagged", 0)
    out["n_artifact_by_class"] = artifact_result.get("n_flagged_by_class", {})
    out["artifact_classes"] = artifact_result.get("classes", [])
    out["artifact_min_fraction"] = artifact_result.get("min_fraction")
    out["artifact_error"] = artifact_result.get("artifact_error")
    return out


# ---------------------------------------------------------------------------- the entry point
def detect_slide_artifacts(local_path, tiles: list[dict], plane_dims: tuple[int, int], *,
                           grandqc_repo=None, classes=ARTIFACT_CLASSES,
                           model_mpp: float = MODEL_MPP, min_fraction: float | None = MIN_FRACTION,
                           device: str = DEVICE, plane_mpp: float = TILE_TARGET_MPP, python=None) -> dict:
    """PIPELINE ENTRY POINT for Step 3: GrandQC over the whole slide, then per-tile fractions.

    `local_path` must be a LOCAL file (GrandQC's scripts take a slide folder), which is why the
    planner localizes for this component. Direct callers must provide grandqc_repo themselves.
    Missing setup is an error, as it is for any requested component. Handled failures return
    an empty tiles list and an artifact_error naming the cause.

    Returns the `artifact_tile_metrics` dict plus `artifact_map` ({class_map, dims, target_mpp},
    for the overlay), `backend`, `model_mpp`, `requested_mpp`, `runtime_s`.
    """
    t0 = time.monotonic()
    skipped = {"tiles": [], "classes": [], "n_flagged": 0, "n_flagged_by_class": {},
               "min_fraction": min_fraction, "mask_dims": None, "artifact_map": None,
               "backend": None, "model_mpp": None, "requested_mpp": model_mpp,
               "runtime_s": 0.0, "artifact_error": None}
    try:
        if not grandqc_repo:
            return {**skipped, "artifact_error": "GrandQC checkout required: run 'pathnd-qc setup grandqc' "
                                                 "or pass grandqc_repo with a complete inference checkout"}
        problem = check_grandqc_repo(grandqc_repo)
        if problem:
            return {**skipped, "backend": BACKEND, "artifact_error": f"--grandqc_repo: {problem}"}
        if not local_path or not os.path.exists(str(local_path)):
            return {**skipped, "backend": BACKEND,
                    "artifact_error": f"GrandQC needs a local slide file; got {local_path!r}"}
        res = run_grandqc_full_mask(str(local_path), str(grandqc_repo), requested_mpp=model_mpp,
                                    device=device, python=python)
        if res.get("grandqc_error"):
            return {**skipped, "backend": BACKEND, "model_mpp": res.get("model_mpp"),
                    "runtime_s": round(time.monotonic() - t0, 3),
                    "artifact_error": f"GrandQC failed: {res['grandqc_error']}"}
        cm, used = res["class_mask"], float(res["model_mpp"])
        out = artifact_tile_metrics(cm, used, tiles, plane_dims, classes=classes,
                                    min_fraction=min_fraction, plane_mpp=plane_mpp)
        out.update({"artifact_map": {"class_map": cm, "dims": [int(cm.shape[1]), int(cm.shape[0])],
                                     "target_mpp": used},
                    "backend": BACKEND, "model_mpp": used, "requested_mpp": model_mpp,
                    "runtime_s": round(time.monotonic() - t0, 3)})
        return out
    except Exception as e:                     # noqa: BLE001 — graceful, like the M2 modules
        return {**skipped, "backend": BACKEND, "runtime_s": round(time.monotonic() - t0, 3),
                "artifact_error": f"{type(e).__name__}: {e}", "artifact_error_type": type(e).__name__}


def generate_artifact_overlay(thumbnail: Image.Image, artifact_map: dict,
                              classes=None) -> Image.Image:
    """Blend the class map over the thumbnail.

    classes=None renders artifact classes 2–6. An explicit selection can include tissue
    and background, which remain untinted under the default selection.
    """
    thumb = thumbnail.convert("RGB")
    cmap = (artifact_map or {}).get("class_map")
    if cmap is None:
        return thumb
    ids = ([cid for _, cid in (_resolve_class(c) for c in classes)] if classes
           else [c for c in CLASS_COLORS if c not in (CLASSES["tissue"], BACKGROUND_CLASS)])
    small = np.asarray(Image.fromarray(np.asarray(cmap, dtype=np.uint8))
                       .resize(thumb.size, Image.NEAREST))
    base = np.asarray(thumb, dtype=np.float32)
    tint = base.copy()
    hit = np.zeros(small.shape, dtype=bool)
    for cid in ids:
        sel = small == cid
        if sel.any():
            tint[sel] = np.asarray(CLASS_COLORS[cid], dtype=np.float32)
            hit |= sel
    blended = base.copy()
    blended[hit] = (1.0 - ALPHA_BLEND) * base[hit] + ALPHA_BLEND * tint[hit]
    return Image.fromarray(blended.clip(0, 255).astype(np.uint8))


def to_report(result: dict) -> dict:
    """Summarize artifact detection for report.json without arrays or polygons.

    Include not_gt_validated to distinguish diagnostic measurements from validated
    quality decisions. Artifact measurements do not gate pipeline stages.
    """
    error = result.get("artifact_error")
    return {"backend": result.get("backend"),
            "model_mpp": result.get("model_mpp"),
            "requested_mpp": result.get("requested_mpp"),
            "classes": list(result.get("classes") or []),
            "n_tiles": None if error else len(result.get("tiles") or []),
            # Use null counts when artifact detection fails.
            "n_flagged": None if error else result.get("n_flagged"),
            "n_flagged_by_class": {} if error else dict(result.get("n_flagged_by_class") or {}),
            "mask_dims": result.get("mask_dims"),
            "min_fraction": result.get("min_fraction"),
            "not_gt_validated": True,
            "runtime_s": result.get("runtime_s"),
            "error_type": error_kind(error, result.get("artifact_error_type")),
            "error": error}
