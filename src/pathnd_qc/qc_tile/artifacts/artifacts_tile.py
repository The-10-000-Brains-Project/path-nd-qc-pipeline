"""Run GrandQC subprocesses and measure tile artifacts from their class mask.

The model checkpoints use 1.0, 1.5, or 2.0 microns per pixel. Select the requested
checkpoint when available, otherwise the finest available one. Run tissue detection
followed by artifact inference over the slide, then load the mask at model resolution.
Tile fractions and pen polygons are extracted by mapping tile coordinates to that mask.

Class IDs are 1 tissue, 2 folds, 3 darkspot/foreign object, 4 pen,
5 edge/air bubble, 6 out-of-focus, and 7 background.
Handled inference failures return class_mask=None and grandqc_error; subprocess
exceptions are handled by detect_slide_artifacts().

pathnd-qc setup grandqc prepares the checkout, compatibility bridge, and verified
weights. Registered user checkouts are not modified. The python argument selects
the backend interpreter. See external/README.md for setup.
"""

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # GrandQC masks at model MPP can exceed PIL's default limit

# 1-indexed GrandQC artifact classes used for tile labelling
ARTIFACT_CLASSES = {
    "fold": 2,
    "darkspot": 3,
    "pen": 4,
    "bubble": 5,   # edge & air bubble (also the closest channel to "tears", D-T)
    "oof": 6,
}
CLASS_BACKGROUND = 7
CLASS_PEN = 4

# Requested MPP -> GrandQC checkpoint file. Fallback order prefers higher res.
_MPP_TO_MODEL = {1.0: "GrandQC_MPP1.pth", 1.5: "GrandQC_MPP15.pth", 2.0: "GrandQC_MPP2.pth"}
_FALLBACK_ORDER = [1.0, 1.5, 2.0]


def resolve_model_mpp(grandqc_script_dir: str, requested_mpp: float) -> float | None:
    """Return an MPP whose checkpoint is present, preferring the requested one
    then the highest-resolution model available. None if no model is found."""
    qc_dir = Path(grandqc_script_dir) / "models" / "qc"
    if requested_mpp in _MPP_TO_MODEL and (qc_dir / _MPP_TO_MODEL[requested_mpp]).is_file():
        return requested_mpp
    for mpp in _FALLBACK_ORDER:
        if (qc_dir / _MPP_TO_MODEL[mpp]).is_file():
            logging.warning(
                f"GrandQC model for MPP {requested_mpp} not found; "
                f"falling back to MPP {mpp} ({_MPP_TO_MODEL[mpp]})."
            )
            return mpp
    return None


def run_grandqc_full_mask(wsi_path: str, grandqc_script_dir: str,
                          requested_mpp: float = 1.0, device: str = "cpu", python: str | None = None) -> dict:
    """Run GrandQC (tissue detect + artifact) and return the full class mask.

    Returns dict: class_mask (uint8 H×W at model MPP, or None), model_mpp,
    grandqc_error.
    """
    wsi_path = Path(wsi_path).resolve()
    script_dir = Path(grandqc_script_dir).resolve()
    python = python or sys.executable

    mpp_used = resolve_model_mpp(str(script_dir), requested_mpp)
    if mpp_used is None:
        return _error_result("No GrandQC artifact checkpoint found in models/qc/")

    with tempfile.TemporaryDirectory() as tmp_root:
        slide_dir = Path(tmp_root) / "slides"
        slide_dir.mkdir()
        (slide_dir / wsi_path.name).symlink_to(wsi_path)
        out_dir = Path(tmp_root) / "output"
        out_dir.mkdir()

        r1 = subprocess.run(
            [python, "-B", str(script_dir / "wsi_tis_detect.py"),
             "--slide_folder", str(slide_dir), "--output_dir", str(out_dir)],
            cwd=str(script_dir), capture_output=True, text=True, timeout=900,
        )
        if r1.returncode != 0:
            logging.error(f"GrandQC tissue detection failed:\n{r1.stderr[-1000:]}")
            return _error_result(r1.stderr[-500:], mpp_used)

        r2 = subprocess.run(
            [python, "-B", str(script_dir / "main.py"),
             "--slide_folder", str(slide_dir), "--output_dir", str(out_dir),
             "--mpp_model", str(mpp_used), "--create_geojson", "N",
             "--device", device],
            cwd=str(script_dir), capture_output=True, text=True, timeout=3600,
        )
        if r2.returncode != 0:
            logging.error(f"GrandQC artifact detection failed:\n{r2.stderr[-1000:]}")
            return _error_result(r2.stderr[-500:], mpp_used)

        mask_path = out_dir / "mask_qc" / f"{wsi_path.name}_mask.png"
        if not mask_path.exists():
            return _error_result(f"Mask not found: {mask_path}", mpp_used)

        class_mask = np.array(Image.open(mask_path).convert("L"))
        return {"class_mask": class_mask, "model_mpp": mpp_used, "grandqc_error": None}


def compute_tile_artifacts(class_mask: np.ndarray, model_mpp: float,
                           slide_mpp: float, geom: dict) -> dict:
    """Per-class artifact area fractions + refined pen polygons for one tile.

    Fractions are over the tile's total area at model MPP. Pen polygons are
    returned in the coordinate frame whose resolution is ``slide_mpp``; the pipeline uses
    its tile plane (normally 0.5 microns/pixel), not necessarily native level 0.
    """
    empty = {"artifact_fractions": {k: 0.0 for k in ARTIFACT_CLASSES},
             "background_fraction": 0.0, "pen_polygons": []}
    if class_mask is None:
        return empty

    mh, mw = class_mask.shape
    inv = slide_mpp / model_mpp          # level-0 px -> mask px
    fwd = model_mpp / slide_mpp          # mask px -> level-0 px

    mx0 = int(np.floor(geom["x"] * inv))
    my0 = int(np.floor(geom["y"] * inv))
    mx1 = int(np.ceil((geom["x"] + geom["width"]) * inv))
    my1 = int(np.ceil((geom["y"] + geom["height"]) * inv))
    mx0, my0 = max(0, mx0), max(0, my0)
    mx1, my1 = min(mw, max(mx1, mx0 + 1)), min(mh, max(my1, my0 + 1))

    sub = class_mask[my0:my1, mx0:mx1]
    if sub.size == 0:
        return empty

    total = float(sub.size)
    fractions = {name: float((sub == cid).sum() / total) for name, cid in ARTIFACT_CLASSES.items()}
    background_fraction = float((sub == CLASS_BACKGROUND).sum() / total)

    pen_polygons = []
    pen_sub = (sub == CLASS_PEN).astype(np.uint8)
    if pen_sub.any():
        contours, _ = cv2.findContours(pen_sub * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            pts = c.reshape(-1, 2)
            if len(pts) < 4:
                continue
            # offset into full mask frame, then scale to level-0 frame
            poly = [[float((px + mx0) * fwd), float((py + my0) * fwd)] for px, py in pts]
            if poly[0] != poly[-1]:
                poly.append(poly[0])
            pen_polygons.append(poly)

    return {"artifact_fractions": fractions,
            "background_fraction": background_fraction,
            "pen_polygons": pen_polygons}


def _error_result(error_msg: str, mpp_used: float | None = None) -> dict:
    return {"class_mask": None, "model_mpp": mpp_used, "grandqc_error": error_msg}
