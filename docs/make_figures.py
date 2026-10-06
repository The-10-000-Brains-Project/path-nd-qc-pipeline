"""Regenerate the manual's figures from one completed pipeline run.

The pipeline saves masks, the blur heatmap, the artifact overlay and the normalized image. This
script reads the slide's 8.0 um/px analysis image again and draws the remaining overlays with the
pipeline's own overlay functions, so colours match the code. Run it from the repository root:

    python docs/make_figures.py --slide /data/slides/42669.svs \
        --run reports/42669_output/<UTC date> --out docs/images

Pass --reinhard with the normalized image from a second run using --norm_method reinhard.
"""

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from pathnd_qc import WSIReader
from pathnd_qc.qc_slide.folds.folds import generate_fold_overlay
from pathnd_qc.qc_slide.pen.pen import generate_pen_overlay
from pathnd_qc.qc_slide.tissue.tissue import generate_tissue_overlay

WIDTH = 800                     # figure width in pixels
KEPT, DROPPED = (0, 160, 80), (220, 50, 50)


def _save(image, path):
    image = image.convert("RGB")
    if image.width > WIDTH:
        image = image.resize((WIDTH, round(image.height * WIDTH / image.width)), Image.LANCZOS)
    image.save(path, quality=88, optimize=True)
    print(f"wrote {path} {image.size}")


def _mask(path, size):
    mask = np.array(Image.open(path).convert("L")) > 0
    if mask.shape[::-1] != size:
        raise ValueError(f"{path} is {mask.shape[::-1]}, expected {size}")
    return mask


def _tiles(image, run, stem):
    """Outline kept tiles in green and dropped tiles in red on the analysis image."""
    tile_list = json.loads((run / "data" / f"{stem}_tile_list.json").read_text())
    records = json.loads((run / "data" / f"{stem}_tile_records.json").read_text())
    if isinstance(records, dict):
        records = records.get("tiles") or records.get("records") or []
    dropped = {(r["x"], r["y"]) for r in records
               if r.get("dropped_pass1") or r.get("dropped_pass2") or r.get("dropped")}
    scale = image.width / tile_list["plane_dims"][0]
    canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    for t in tile_list["tiles"]:
        box = [t["x"] * scale, t["y"] * scale, (t["x"] + t["w"]) * scale, (t["y"] + t["h"]) * scale]
        draw.rectangle(box, outline=DROPPED if (t["x"], t["y"]) in dropped else KEPT)
    return canvas


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slide", required=True, help="the slide the run analysed")
    parser.add_argument("--run", required=True, type=Path, help="the dated run folder")
    parser.add_argument("--out", required=True, type=Path, help="figure output folder")
    parser.add_argument("--reinhard", type=Path, help="normalized image from a Reinhard run")
    args = parser.parse_args()

    report = next(args.run.glob("*_report.json"))
    stem = report.name.removesuffix("_report.json")
    images = args.run / "images"
    args.out.mkdir(parents=True, exist_ok=True)

    reader = WSIReader()
    with reader.slide(args.slide) as slide:
        plane, geometry = reader.read_at_mpp(slide, 8.0)
    if plane is None:
        raise SystemExit(f"could not read the analysis image: {geometry}")

    _save(plane, args.out / f"{stem}_slide.jpg")
    _save(generate_tissue_overlay(plane, _mask(images / f"{stem}_tissue_mask.png", plane.size)),
          args.out / f"{stem}_tissue.jpg")
    _save(generate_fold_overlay(plane, _mask(images / f"{stem}_fold_mask.png", plane.size)),
          args.out / f"{stem}_folds.jpg")
    _save(generate_pen_overlay(plane, _mask(images / f"{stem}_pen_mask.png", plane.size)),
          args.out / f"{stem}_pen.jpg")
    _save(_tiles(plane, args.run, stem), args.out / f"{stem}_tiles.jpg")
    for name in ("blur_overlay", "artifact_overlay", "normalized"):
        source = images / f"{stem}_{name}.png"
        if source.exists():
            _save(Image.open(source), args.out / f"{stem}_{name}.jpg")
    if args.reinhard:
        _save(Image.open(args.reinhard), args.out / f"{stem}_reinhard.jpg")


if __name__ == "__main__":
    main()
