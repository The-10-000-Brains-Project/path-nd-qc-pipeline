"""Smoke test — tile metrics: cascade fallbacks, two-pass drops and output failures.

No network, no real slide: uses the shared synthetic FakeSlide/FakeReader fixture.
Checks follow tile_metrics/README.md, function docstrings and mask/statistical invariants.
Run: .venv/bin/python src/tests/smoke_tile_metrics.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from unittest.mock import patch

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from fake_slide import FakeReader, FakeSlide  # noqa: E402
from pathnd_qc import artifact_store  # noqa: E402
from pathnd_qc.qc_tile.tile_metrics import tile_metrics as M  # noqa: E402
from pathnd_qc.qc_tile.tiles.tiles import tile_grid  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + str(detail) if detail else ''}")


def segmentation(mask):
    """Inject a known support to test orchestration independently of segmentation accuracy."""
    return {"tissue_mask": mask, "tissue_error": None}


out = tempfile.mkdtemp(prefix="pathnd_tile_metrics_")
try:
    slide = FakeSlide(base_wh=(256, 128), mpp=0.5016, seed=29)
    reader = FakeReader(slide)
    dims = slide.dimensions
    full = np.ones((128, 256), dtype=bool)
    empty = np.zeros_like(full)
    tiles = tile_grid(dims, tile_px=64)

    def metrics(mask=full, selected=None, **kwargs):
        return M.compute_tile_metrics(reader, slide, mask, tiles if selected is None else selected,
                                      dims, target_mpp=0.5016, **kwargs)

    def refine(mask, **kwargs):
        return M.refine_tissue_mask(reader, slide, mask, target_mpp=0.5016,
                                    tile_px=64, **kwargs)

    print("\n[1] Optional refinement: carried support, refinement and fallback")
    # Docstring: single-class chunks are carried without reads; nothing is dropped.
    before = slide.n_reads
    interior = refine(full)
    check("fully covered chunks retain every tissue pixel without reading",
          interior["refine_error"] is None and np.array_equal(interior["tissue_mask"], full)
          and interior["n_carried_interior"] == len(tiles) and slide.n_reads == before)
    sparse = refine(empty)
    check("empty chunks retain an empty mask without reading",
          sparse["refine_error"] is None and not sparse["tissue_mask"].any()
          and sparse["n_carried_sparse"] == len(tiles) and slide.n_reads == before)

    half = full.copy()
    half[:, 1::2] = False
    # An alternating binary support has exactly half coverage in every 64px chunk.
    with patch.object(M, "compute_tissue_mask", side_effect=lambda image, **kw:
                      segmentation(np.ones((image.height, image.width), bool))):
        refined = refine(half)
    check("successful chunk refinement stitches the returned supports into the plane",
          refined["refine_error"] is None and refined["tissue_mask"].all()
          and refined["n_resegmented"] == len(tiles)
          and refined["tissue_mask"].shape == full.shape)
    with patch.object(M, "compute_tissue_mask", return_value={"tissue_mask": None,
                                                            "tissue_error": "synthetic segmentation failure"}):
        fallback = refine(half)
    check("failed chunk segmentation preserves the incoming mask and counts failures",
          fallback["refine_error"] is None and np.array_equal(fallback["tissue_mask"], half)
          and fallback["n_reseg_failed"] == len(tiles))
    with patch.object(reader, "read_window_at_mpp", return_value=(None, {"error": "decode failed"})):
        unreadable = refine(half)
    check("unreadable chunks preserve incoming support instead of becoming glass",
          unreadable["refine_error"] is None and np.array_equal(unreadable["tissue_mask"], half)
          and unreadable["n_reseg_failed"] == len(tiles))
    bad_refine = refine(np.ones((2, 2, 3)))
    check("invalid refinement mask returns a typed error and no synthetic zero mask",
          bad_refine["tissue_mask"] is None and "2-D" in bad_refine["refine_error"]
          and M.refine_to_report(bad_refine)["error_type"] == "ValueError")
    bad_scale = refine(full, info={"mpp_x": None, "mpp_y": None,
                                  "level_dimensions": [dims], "level_downsamples": [1.0]})
    check("missing acquisition scale prevents refinement with an explicit error",
          bad_scale["tissue_mask"] is None and "cannot reach" in bad_scale["refine_error"])
    refinement_report = M.refine_to_report(refined)
    check("refinement report is JSON-safe and excludes the mask pixels",
          "tissue_mask" not in refinement_report and refinement_report["plane_dims"] == list(dims)
          and json.loads(json.dumps(refinement_report)) == refinement_report)

    print("\n[2] Two-pass dropping and mask accumulation")
    # README: pass 1 avoids reads; pass 2 tests refined support, after mask insertion.
    mask = full.copy()
    mask[:64, :64] = False
    mask[:64, 64:128] = half[:64, 64:128]
    thin = np.zeros((64, 64), bool)
    thin[:8] = True  # Known 1/8 area lies below the configured 20% drop threshold.
    writer = artifact_store.TiledMaskWriter(dims, mpp=0.5016)
    before = slide.n_reads
    with patch.object(M, "compute_tissue_mask", return_value=segmentation(thin)):
        dropped = metrics(mask, tiles[:3], mask_writer=writer)
    first, second, third = dropped["tiles"]
    check("pass-1 rejection avoids a slide read and never fabricates a focus measurement",
          first["dropped_pass1"] and not first["kept"] and first["focus"] is None
          and slide.n_reads - before == 2)
    check("pass-2 rejection uses refined tissue coverage while preserving its measurement",
          second["dropped_pass2"] and not second["kept"] and second["resegmented"]
          and second["tissue_fraction"] == 1 / 8 and second["focus"] is not None)
    check("fully tissue tile bypasses resegmentation and stays kept",
          third["kept"] and not third["resegmented"] and third["tissue_fraction"] == 1.0)
    saved = os.path.join(out, "tissue.tif")
    writer.write(saved)
    restored = artifact_store.read_tiled_mask_window(saved, 0, 0, *dims)
    check("saved mask includes pass-2 support and leaves pass-1 and unselected regions empty",
          dropped["mask_complete"] is True and np.array_equal(restored[:64, 64:128], thin)
          and not restored[:64, :64].any() and not restored[64:].any()
          and restored[:64, 128:192].all())
    no_drop = metrics(empty, tiles[:1], drop_below=None, resegment_edges=False)
    check("disabled dropping keeps decoded zero-support tiles with null focus",
          no_drop["n_kept"] == 1 and no_drop["tiles"][0]["focus"] is None
          and no_drop["mask_complete"] is None)
    # A threshold is a lower bound: exact equality must survive both passes.
    boundary = metrics(half, tiles[:1], drop_below=0.5, resegment_edges=False)
    check("coverage equal to the drop threshold survives both comparisons",
          boundary["n_kept"] == 1 and boundary["n_dropped_pass1"] == 0
          and boundary["n_dropped_pass2"] == 0)

    print("\n[3] Independent read, segmentation, focus and writer failures")
    with patch.object(reader, "read_window_at_mpp", return_value=(None, {})):
        read_failed = metrics(selected=tiles[:1], drop_below=None)
    check("disabled dropping never keeps an unreadable tile, even without a reader error string",
          read_failed["n_read_failed"] == 1 and read_failed["n_kept"] == 0
          and read_failed["tiles"][0]["focus"] is None and read_failed["tiles"][0]["read_error"])
    for label, value in (("reported error", {"tissue_error": "synthetic segmentation failure", "tissue_mask": None}),
                         ("missing mask", {"tissue_error": None, "tissue_mask": None})):
        with patch.object(M, "compute_tissue_mask", return_value=value):
            result = metrics(half, tiles[:1])
        record = result["tiles"][0]
        check(f"segmentation {label} retains coarse support with an explicit resegmentation error",
              record["reseg_error"] and record["tissue_fraction"] == 0.5
              and not record["resegmented"] and record["kept"], record["reseg_error"])
    with patch.object(M, "compute_tissue_mask", side_effect=RuntimeError("synthetic segmentation exception")):
        result = metrics(half, tiles[:1])
    check("raised segmentation exception keeps coarse measurements and records its cause",
          "RuntimeError" in result["tiles"][0]["reseg_error"]
          and result["tiles"][0]["tissue_fraction"] == 0.5)
    with patch.object(M, "compute_focus_score", return_value={"focus_score": None,
                                                             "focus_error": "synthetic focus failure"}):
        result = metrics(selected=tiles[:1])
    check("focus failure is null with its cause, rather than an apparently sharp zero",
          result["tiles"][0]["focus"] is None
          and result["tiles"][0]["focus_error"] == "synthetic focus failure")

    class FailedWriter:
        calls = 0

        def add(self, x, y, support):
            self.calls += 1
            if self.calls == 2:
                raise OSError("synthetic writer failure")

    failed_writer = FailedWriter()
    with patch.object(M, "PROGRESS_EVERY_S", 0):
        result = metrics(selected=tiles[:3], mask_writer=failed_writer)
    check("mask insertion failure stops writer calls while later tiles retain measurements",
          failed_writer.calls == 2 and result["mask_complete"] is False and result["n_kept"] == 3
          and all(t["focus"] is not None for t in result["tiles"])
          and [t["mask_written"] for t in result["tiles"]] == [True, False, False]
          and "synthetic writer failure" in result["mask_write_error"])
    # README: the reader clips overhangs; measurements must use the clipped shape.
    edge = {"x": 240, "y": 112, "w": 32, "h": 32, "col": 4, "row": 2}
    clipped = metrics(selected=[edge])
    check("an overhanging window measures clipped pixels without a support-shape failure",
          clipped["tile_error"] is None and clipped["tiles"][0]["focus_error"] is None
          and clipped["tiles"][0]["tissue_fraction"] == 1.0)
    for label, invalid in (("non-2D tissue mask", metrics(np.ones((2, 2, 3)))),
                           ("nonpositive plane", M.compute_tile_metrics(reader, slide, full, tiles, (0, 1)))):
        report = M.to_report(invalid)
        check(f"{label} yields an explicit typed error and null report counts",
              invalid["tiles"] is None and report["error_type"] == "ValueError"
              and report["n_tiles"] is None and report["focus_n"] is None, report["error"])

    print("\n[4] Statistical summaries and pure overlay rendering")
    # README: measured pass-2 drops participate in statistics; low-focus outliers use kept tiles.
    report = M.to_report(dropped, outlier_n=1)
    measured = [t["focus"] for t in dropped["tiles"] if t["focus"] is not None]
    check("focus statistics include measured pass-2 drops and report the mathematical median",
          report["focus_n"] == 2 and report["focus_median"] == float(np.median(measured))
          and report["kept_fraction"] == 1 / 3)
    check("low-focus outliers select kept tiles without changing their kept state",
          [t["col"] for t in report["outliers"] if t["why"] == "low_focus"] == [third["col"]]
          and third["kept"])
    error_records = [dict(tiles[i], focus=None, kept=False, read_error="decode failure",
                          reseg_error="segmentation failure") for i in range(3)]
    report = M.to_report({"tiles": error_records, "n_kept": 0}, outlier_n=1)
    check("read and segmentation outlier groups remain uncapped",
          sum(t["why"] == "read_error" for t in report["outliers"]) == 3
          and sum(t["why"] == "reseg_error" for t in report["outliers"]) == 3
          and report["focus_median"] is None)
    empty_report = M.to_report(metrics(selected=[]))
    check("empty direct-call statistics have null fractions and focus instead of dividing by zero",
          empty_report["kept_fraction"] is None and empty_report["focus_n"] == 0
          and empty_report["focus_p5"] is None and empty_report["outliers"] == [])

    thumb = Image.new("RGB", (30, 10), (233, 233, 233))  # Smooth glass, not noisy tissue.
    records = [{"x": 0, "y": 0, "w": 10, "h": 10, "focus": 0.0},
               {"x": 10, "y": 0, "w": 10, "h": 10, "focus": 10.0},
               {"x": 20, "y": 0, "w": 10, "h": 10, "focus": None}]
    base = np.asarray(thumb)
    for label, recs in (("no measured focus", []), ("one measured focus", records[:1]),
                        ("flat focus range", [records[0], dict(records[1], focus=0.0)])):
        overlay = M.generate_blur_overlay(thumb, {"tiles": recs}, thumb.size)
        check(f"{label} leaves the thumbnail untinted", np.array_equal(overlay, base))
    overlay = np.asarray(M.generate_blur_overlay(thumb, {"tiles": records}, thumb.size, alpha=1))
    check("overlay maps low to red and high to green, leaving missing focus untouched",
          np.all(overlay[:, :10] == (255, 0, 0)) and np.all(overlay[:, 10:20] == (0, 255, 0))
          and np.array_equal(overlay[:, 20:], base[:, 20:]))
    middle = np.asarray(M.generate_blur_overlay(thumb, {"tiles": records}, thumb.size,
                                                vmin=-10, vmax=10, alpha=1))
    check("explicit midpoint focus maps to the documented yellow waypoint",
          np.all(middle[:, :10] == (255, 255, 0)) and np.array_equal(np.asarray(thumb), base))
    error = None
    try:
        M.generate_blur_overlay(thumb, {"tiles": records}, (0, 10))
    except ValueError as exc:
        error = str(exc)
    check("overlay rejects nonpositive geometry with a usable message",
          error is not None and "positive" in error, error)
finally:
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{'=' * 70}\nTILE METRICS SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for n in FAIL:
    print(f"  - {n}")
sys.exit(1 if FAIL else 0)
