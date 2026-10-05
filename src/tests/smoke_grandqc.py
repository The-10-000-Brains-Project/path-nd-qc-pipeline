"""Smoke test — GrandQC adapter contracts with an executable synthetic backend.

Contracts: artifacts/README.md, adapter docstrings, physical area/coordinate invariants.
No trained model, credentials or network. Run: python src/tests/smoke_grandqc.py
"""

from pathlib import Path
import shutil
import sys
import tempfile
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pathnd_qc.qc_tile.artifacts import artifacts as A, artifacts_tile as B
from pathnd_qc.qc_tile.tiles import tiles as T

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        f"  {'PASS' if cond else 'FAIL'}  {name}{' — '+str(detail) if detail else ''}"
    )


out = Path(tempfile.mkdtemp(prefix="pathnd-grandqc-contract-"))
try:
    print("\n[1] Physical class fractions and tile geometry")
    mask = np.ones((8, 8), np.uint8)
    mask[:4, :4] = 2
    mask[4:, :4] = 4
    mask[:, 4:] = 7
    tile = {"col": 0, "row": 0, "x": 0, "y": 0, "w": 16, "h": 16}
    result = A.artifact_tile_metrics(
        mask, 1, [tile], (16, 16), classes=["fold", "pen"], min_fraction=0.25
    )
    rec = result["tiles"][0]
    check(
        "fractions equal labelled area at twice the tile-plane MPP",
        rec["artifact_fractions"] == {"fold": 0.25, "pen": 0.25}
        and rec["background_fraction"] == 0.5,
    )
    check(
        "threshold flags without discarding a tile",
        result["n_flagged"] == 1 and len(result["tiles"]) == 1 and rec["has_artifact"],
    )
    recorded = A.artifact_tile_metrics(
        mask, 1, [tile], (16, 16), classes=["fold", "pen"], min_fraction=None
    )
    check(
        "unconfigured threshold records measurements without a verdict",
        not recorded["tiles"][0]["has_artifact"]
        and recorded["tiles"][0]["artifact_label"] is None,
    )
    polygons = rec["pen_polygons"]
    check(
        "pen polygons are closed and in the tile-plane coordinate frame",
        bool(polygons)
        and all(
            poly[0] == poly[-1] and all(0 <= x <= 6 and 8 <= y <= 14 for x, y in poly)
            for poly in polygons
        ),
    )
    for classes in [[], ["tissue"], ["unknown"], [999]]:
        bad = A.artifact_tile_metrics(mask, 1, [tile], (16, 16), classes=classes)
        check(
            f"invalid artifact classes {classes} fail explicitly",
            bool(bad["artifact_error"]) and bad["tiles"] == [],
        )
    bad = A.artifact_tile_metrics(mask, 1, [tile], (32, 32))
    check(
        "a mask at the wrong physical scale is refused",
        bool(bad["artifact_error"]) and bad["tiles"] == [],
    )
    metrics = {"tiles": [dict(tile, kept=False), {"col": 1, "row": 0, "kept": True}]}
    merged = A.merge_tile_artifacts(metrics, result)
    check(
        "artifact merge preserves input records and kept decisions",
        "artifact_fractions" not in metrics["tiles"][0]
        and [r["kept"] for r in merged["tiles"]] == [False, True],
    )
    check(
        "unmatched tiles get null artifact measurements",
        merged["tiles"][1]["artifact_fractions"] is None,
    )
    image = Image.new("RGB", (16, 16), (100, 100, 100))
    overlay = np.asarray(A.generate_artifact_overlay(image, {"class_map": mask}))
    check(
        "default overlay leaves background pixels untinted",
        np.array_equal(overlay[:, 8:], np.asarray(image)[:, 8:]),
    )
    check(
        "missing artifact map leaves the image unchanged",
        np.array_equal(A.generate_artifact_overlay(image, {}), image),
    )
    grid = T.tile_grid((35, 19), tile_px=16)
    coverage = np.zeros((19, 35), int)
    for t in grid:
        coverage[t["y"] : t["y"] + t["h"], t["x"] : t["x"] + t["w"]] += 1
    check(
        "clipped grid cells cover a non-square plane exactly once",
        np.all(coverage == 1) and grid[-1]["w"] == 3 and grid[-1]["h"] == 3,
    )
    support = np.zeros((19, 35), bool)
    support[-1, -1] = True
    selected = T.select_tissue_tiles(support, (35, 19), tile_px=16)
    check(
        "one tissue pixel keeps its clipped edge tile only",
        len(selected) == 1 and (selected[0]["col"], selected[0]["row"]) == (2, 1),
    )
    check(
        "empty tissue selects no tiles",
        T.select_tissue_tiles(np.zeros_like(support), (35, 19)) == [],
    )
    view = T.plane_window(support, 32, 16, 3, 3, (35, 19))
    check(
        "plane window is a view with the requested physical footprint",
        view.shape == (3, 3) and np.shares_memory(view, support),
    )

    print("\n[2] Checkpoint selection and real subprocess boundaries")
    repo = out / "backend"
    (repo / "models/qc").mkdir(parents=True)
    (repo / "models/td").mkdir()
    (repo / "models/td/Tissue_Detection_MPP10.pth").touch()
    check(
        "missing checkpoints have no fabricated fallback",
        B.resolve_model_mpp(str(repo), 1) is None,
    )
    (repo / "models/qc/GrandQC_MPP2.pth").touch()
    (repo / "models/qc/GrandQC_MPP15.pth").touch()
    check(
        "requested checkpoint is preferred when present",
        B.resolve_model_mpp(str(repo), 2) == 2,
    )
    check(
        "fallback chooses the finest available checkpoint",
        B.resolve_model_mpp(str(repo), 1) == 1.5,
    )
    (repo / "models/qc/GrandQC_MPP1.pth").touch()
    (repo / "wsi_tis_detect.py").write_text(
        'import argparse\np=argparse.ArgumentParser();p.add_argument("--slide_folder");p.add_argument("--output_dir");p.parse_args()\n'
    )
    main = repo / "main.py"
    # A deterministic class mask stands in for neural inference; adapter execution remains real.
    main.write_text("""import argparse
from pathlib import Path
from PIL import Image
import numpy as np
p=argparse.ArgumentParser()
for key in ["slide_folder","output_dir","mpp_model","create_geojson","device"]: p.add_argument("--"+key)
a=p.parse_args();out=Path(a.output_dir)/"mask_qc";out.mkdir()
for slide in Path(a.slide_folder).iterdir():
 mask=np.ones((8,8),np.uint8);mask[:4,:4]=2;mask[4:,:4]=4;mask[:,4:]=7
 Image.fromarray(mask).save(out/(slide.name+"_mask.png"))
""")
    slide = out / "sample.svs"
    slide.touch()
    check(
        "complete synthetic backend passes structural validation",
        A.check_grandqc_repo(repo) is None,
    )
    result = A.detect_slide_artifacts(
        slide, [tile], (16, 16), grandqc_repo=repo, python=sys.executable
    )
    check(
        "adapter runs both scripts and loads their class mask",
        result["artifact_error"] is None
        and np.array_equal(result["artifact_map"]["class_map"], mask),
        result.get("artifact_error"),
    )
    check(
        "adapter records the actual backend and model scale",
        result["backend"] == "grandqc-subprocess" and result["model_mpp"] == 1,
    )
    main.write_text("raise SystemExit(7)\n")
    bad = A.detect_slide_artifacts(slide, [tile], (16, 16), grandqc_repo=repo)
    check(
        "backend failure does not become a zero artifact count",
        bool(bad["artifact_error"]) and A.to_report(bad)["n_flagged"] is None,
    )
    main.write_text("pass\n")
    bad = A.detect_slide_artifacts(slide, [tile], (16, 16), grandqc_repo=repo)
    check(
        "successful exit without the expected mask is an error",
        bool(bad["artifact_error"]) and "Mask not found" in bad["artifact_error"],
    )
    (repo / "wsi_tis_detect.py").write_text(
        'import sys\nsys.stderr.write("tissue failed")\nraise SystemExit(3)\n'
    )
    bad = A.detect_slide_artifacts(slide, [tile], (16, 16), grandqc_repo=repo)
    check(
        "tissue subprocess failure retains its reason",
        "tissue failed" in bad["artifact_error"],
    )
    bad = A.detect_slide_artifacts(
        slide, [tile], (16, 16), grandqc_repo=repo, python=str(out / "missing-python")
    )
    check(
        "unlaunchable interpreter is a reported adapter failure",
        bad["artifact_error_type"] == "FileNotFoundError",
    )
    for path, backend in [
        (slide, None),
        (out / "missing.svs", repo),
        (slide, out / "absent"),
    ]:
        bad = A.detect_slide_artifacts(path, [tile], (16, 16), grandqc_repo=backend)
        check(
            f"missing required input {path.name}/{backend} refuses inference",
            bool(bad["artifact_error"]) and bad["artifact_map"] is None,
        )
finally:
    shutil.rmtree(out, ignore_errors=True)
print(f"\n{'='*70}\nGRANDQC SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  - {name}")
sys.exit(bool(FAIL))
