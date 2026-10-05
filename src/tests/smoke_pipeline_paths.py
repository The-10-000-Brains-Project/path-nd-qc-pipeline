"""Smoke test — orchestration paths for supplied inputs, tile planes and failures.

Hermetic: synthetic slide pixels, local metadata and no network/model downloads.
Assertions follow pipeline.run/main docstrings, the supplied-artifact contract and
physical tile geometry. No production code or shared fixtures are modified.
Run: .venv/bin/python src/tests/smoke_pipeline_paths.py
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from unittest.mock import patch

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from fake_slide import FakeSlide, FakeReader
from pathnd_qc import pipeline as M

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + str(detail) if detail else ''}")


def caught(call):
    try:
        return call(), None
    except Exception as exc:
        return None, exc


out = tempfile.mkdtemp(prefix="pathnd-pipeline-paths-")
root = Path(out)
slide_path = root / "slide.svs"
slide_path.touch()
slide = FakeSlide(base_wh=(512, 384), downsamples=(1, 4), mpp=.5)
reader = FakeReader(slide)
image = slide._levels[1]
image.save(root / "thumbnail.png")
Image.fromarray(np.full((96, 128), 255, np.uint8)).save(root / "tissue.png")
Image.fromarray(np.zeros((96, 128), np.uint8)).save(root / "empty.png")
supplied = {"thumbnail": str(root / "thumbnail.png"), "tissue_mask": str(root / "tissue.png"),
            "fold_mask": str(root / "empty.png"), "pen_mask": str(root / "empty.png")}
tile = {"x": 128, "y": 128, "w": 128, "h": 128}


def run(name, *, source=slide, **kwargs):
    options = {"out_dir": str(root / name), "metadata_enabled": False, "stain_type": "AT8",
               "download_models": False, "write": False, "save_artifacts": False}
    options.update(kwargs)
    with patch.object(M, "GCSWSIReader", return_value=FakeReader(source)):
        return M.run(str(slide_path), **options)


def write_tiles(name, data):
    path = root / f"{name}.json"
    path.write_text(json.dumps(data))
    return str(path)


try:
    print("\n[1] Supplied-image analysis and explicit publication controls")
    # pipeline.run: complete supplied inputs permit analysis without opening the slide.
    offline_reader = FakeReader(slide)
    with patch.object(M, "GCSWSIReader", return_value=offline_reader), \
            patch.object(offline_reader, "slide", side_effect=AssertionError("must not open slide")):
        result = M.run(str(slide_path), components={"stain_normalization"}, supplied=supplied,
                       norm_method="reinhard", stain_type="AT8", out_dir=str(root / "offline"),
                       metadata_enabled=False, download_models=False, write=False, save_artifacts=False)
    rep = result["report"]
    check("supplied image normalization completes without acquisition facts or slide I/O",
          rep["m4"]["stain_norm"]["error"] is None and result["checks"] is None
          and rep["m1"]["ingestion"]["decision"]["status"] == "not_checked")
    check("supplied thumbnail scale is an assumption rather than a measured resolution",
          rep["m2"]["read"]["source"] == "supplied"
          and rep["m2"]["read"]["achieved_mpp"] is None
          and rep["m2"]["read"]["assumed_mpp"] == M.M2_TARGET_MPP, rep["m2"]["read"])
    check("write=False and save_artifacts=False return results without creating a run",
          result["report_path"] is None and result["out_dir"] is None
          and not list((root / "offline").iterdir()))

    result = run("normalize_save", components={"stain_normalization"}, supplied=supplied,
                 norm_method="reinhard", write=False, save_artifacts=True)
    normalized = result["outputs"].get("normalized_image") or {}
    check("write=False still permits explicitly enabled image artifacts",
          result["report_path"] is None and Path(normalized.get("path", "")).is_file(), normalized)
    check("supplied masks are not copied as generated artifacts", set(result["outputs"]) == {"normalized_image"})

    # Explicit CSV lookup must still run in the supplied-image branch.
    metadata = root / "metadata.csv"
    metadata.write_text(f"slide_paths,stain_type,study\n{slide_path},AT8,Synthetic cohort\n")
    result = run("offline_metadata", components={"staining_quality"}, supplied=supplied,
                 metadata_enabled=True, metadata_paths=[str(metadata)], stain_type=None)
    src = result["report"]["m1"]["ingestion"]["source"]
    check("offline metadata supplies stain and preserves the standard study field",
          src["stain_type"] == "AT8" and result["report"]["m1"]["ingestion"]["metadata"]["record"]["study"]
          == "Synthetic cohort", src)
    warnings = []
    result = run("unknown_stain", components={"staining_quality"}, supplied=supplied,
                 stain_type="not-a-configured-stain", warn=warnings.append)
    check("unknown stain is reported to the warning callback and report without a false failure",
          any("vocabulary" in str(w) for w in warnings)
          and result["report"]["provenance"]["warnings"]
          and result["report"]["provenance"]["execution"]["complete"])

    print("\n[2] Tile-list JSON validates before slide I/O")
    invalid = [
        ("corrupt JSON", None), ("scalar", 4), ("empty tiles", []),
        ("missing coordinate", [{"x": 0, "y": 0, "w": 12}]),
        ("boolean coordinate", [dict(tile, x=True)]),
        ("fractional coordinate", [dict(tile, y=1.2)]),
        ("negative coordinate", [dict(tile, x=-1)]),
        ("oversized tile", [dict(tile, w=M.m3_tiles.TILE_PX + 1)]),
        ("fractional row", [dict(tile, row=1.2)]),
        ("missing plane dimensions", {"tiles": [tile]}),
        ("non-numeric plane MPP", {"plane_dims": [512, 384], "plane_mpp": "0.5", "tiles": [tile]}),
        ("non-positive plane MPP", {"plane_dims": [512, 384], "plane_mpp": 0, "tiles": [tile]}),
    ]
    for name, data in invalid:
        path = write_tiles(name, data)
        if name == "corrupt JSON":
            Path(path).write_text("{not-json")
        with patch.object(M, "GCSWSIReader", side_effect=AssertionError("preflight must precede reader")):
            _, exc = caught(lambda: M.run(str(slide_path), components={"tile_metrics"},
                supplied=dict(supplied, tile_list=path), out_dir=str(root / name),
                download_models=False, metadata_enabled=False))
        check(f"{name} refuses with --tile_list before reader construction",
              isinstance(exc, ValueError) and "--tile_list" in str(exc), exc)

    warnings = []
    tile_path = write_tiles("duplicate_tiles", [dict(tile, x=128.0, col=1.0), tile])
    result = run("deduplicated", components={"tile_metrics"},
                 supplied=dict(supplied, tile_list=tile_path), warn=warnings.append)
    check("duplicate physical windows are measured once with a visible warning",
          result["report"]["m3"]["tiles"]["n_tiles"] == 1
          and any("duplicate" in str(w) for w in warnings), warnings)
    for name, data, cause in [
        ("wrong MPP", {"plane_dims": [512, 384], "plane_mpp": 2, "tiles": [tile]}, "uses 2"),
        ("wrong dimensions", {"plane_dims": [256, 192], "plane_mpp": .5, "tiles": [tile]}, "plane"),
        ("outside plane", [dict(tile, x=500)], "outside"),
    ]:
        tile_path = write_tiles(name, data)
        _, exc = caught(lambda: run(name, components={"tile_metrics"},
                                   supplied=dict(supplied, tile_list=tile_path)))
        rep = exc.report if isinstance(exc, M.RunFailed) else {}
        check(f"{name} refuses incompatible geometry with an in-memory failure report",
              isinstance(exc, M.RunFailed) and cause in str(rep.get("m3", {}).get("tiles", {}).get("error"))
              and not rep["provenance"]["execution"]["complete"], exc)

    print("\n[3] Tile work: selected geometry, measured support, and save/no-save")
    for save in [False, True]:
        result = run(f"tiles_save_{save}", components={"tile_selection", "tile_metrics"},
                     supplied=supplied, save_artifacts=save, write=True)
        rep = result["report"]
        check(f"tile selection and metrics complete with save_artifacts={save}",
              rep["provenance"]["execution"]["complete"] and rep["m3"]["n_tiles_selected"] > 0
              and rep["m3"]["tiles"]["n_tiles"] == rep["m3"]["n_tiles_selected"])
        if save:
            outputs = result["outputs"]
            check("saved tile analysis includes geometry, records, fine mask and blur overlay",
                  {"tile_list", "tile_records", "tissue_mask_20x", "blur_overlay"} <= set(outputs)
                  and all(Path(p["path"]).is_file() for p in outputs.values()), sorted(outputs))
        else:
            check("save_artifacts=False retains report and status while omitting masks and tile files",
                  result["outputs"] == {} and Path(result["report_path"]).is_file()
                  and (result["out_dir"] / "slide_status.json").is_file())

    coarse = FakeSlide(base_wh=(512, 384), downsamples=(1, 4), mpp=.7, objective_power=10)
    _, exc = caught(lambda: run("coarse_gate", source=coarse,
                                components={"staining_quality", "tile_selection", "tile_metrics"},
                                supplied=supplied))
    rep = exc.report if isinstance(exc, M.RunFailed) else {}
    check("coarse valid scale permits thumbnail staining while gating required tile work",
          isinstance(exc, M.RunFailed) and rep["m2"]["staining"]["error"] is None
          and rep["m3"]["status"] == "rejected"
          and {"tile_selection", "tile_metrics"} <= set(rep["provenance"]["components_gated"]), exc)

    implausible = FakeSlide(base_wh=(128, 96), mpp=20, objective_power=None)
    _, exc = caught(lambda: run("implausible_scale", source=implausible,
                                components={"tissue_segmentation", "stain_normalization"}))
    rep = exc.report if isinstance(exc, M.RunFailed) else {}
    check("physically implausible scale refuses both analysis read and normalization",
          isinstance(exc, M.RunFailed) and rep["m2"]["read"]["source"] == "refused"
          and "refused" in rep["m4"]["stain_norm"]["error"], exc)

    print("\n[4] Model/setup refusal and component failures remain explicit")
    for name, changes, required in [
        ("missing pen weights", {"components": {"pen_detection"}, "pen_weights": str(root / "missing.pt")}, "pen"),
        ("invalid GrandQC checkout", {"components": {"tile_artifacts"},
                                     "grandqc_repo": str(root / "not-grandqc"),
                                     "supplied": dict(supplied, tile_list=write_tiles("valid", [tile]))}, "GrandQC"),
        ("invalid normalization method", {"components": {"stain_normalization"},
                                          "supplied": supplied, "norm_method": "unsupported"}, "norm_method"),
    ]:
        _, exc = caught(lambda: run(name, **changes))
        check(f"{name} refuses before producing a run",
              isinstance(exc, ValueError) and required.lower() in str(exc).lower()
              and not list((root / name).glob("**/*_report.json")), exc)

    tissue_failure = {"tissue_mask": None, "tissue_error": "synthetic segmentation failure",
                      "tissue_coverage_score": None, "runtime_s": 0}
    with patch.object(M.m2_tissue, "compute_tissue_mask", return_value=tissue_failure):
        _, exc = caught(lambda: run("tissue_failure", components={"tissue_segmentation", "fold_detection",
            "staining_quality", "focus", "stain_normalization"}, supplied={"thumbnail": supplied["thumbnail"]}))
    rep = exc.report if isinstance(exc, M.RunFailed) else {}
    sections = [rep.get("m2", {}).get(k, {}) for k in ("folds", "staining", "focus")]
    sections.append(rep.get("m4", {}).get("stain_norm", {}))
    check("tissue failure prevents dependent algorithms from silently measuring without support",
          isinstance(exc, M.RunFailed) and all("tissue mask failed" in s.get("error", "") for s in sections), exc)

    weights = root / "synthetic.pt"
    weights.write_bytes(b"no inference: detector patched in this case")
    with patch.object(M.m2_pen, "detect_pen", return_value={"pen_mask": None, "pen_error": "synthetic model failure",
                                                          "pen_area_fraction": None, "runtime_s": 0}):
        _, exc = caught(lambda: run("pen_fallback", components={"pen_detection", "focus"},
                                    supplied=supplied, pen_weights=str(weights)))
    rep = exc.report if isinstance(exc, M.RunFailed) else {}
    check("failed pen inference uses supplied exclusion for focus but keeps the requested model failure",
          isinstance(exc, M.RunFailed) and rep["m2"]["focus"]["error"] is None
          and "supplied" in rep["m2"]["pen"]["note"]
          and "pen_detection" in rep["provenance"]["components_failed"], exc)

    # A raised component exception preserves earlier measurements and identifies unreached work.
    with patch.object(M.m2_staining, "compute_staining_metrics", side_effect=RuntimeError("staining crashed")):
        _, exc = caught(lambda: run("raised_component", components={"staining_quality", "stain_normalization"},
                                    supplied=supplied))
    rep = exc.report if isinstance(exc, M.RunFailed) else {}
    check("component exception retains stage and marks later selected work as not reached",
          isinstance(exc, M.RunFailed) and exc.failure["stage"] == "m2"
          and "stain_normalization" in rep["provenance"]["components_not_reached"]
          and exc.report_path is None, exc)

    print("\n[5] Remote acquisition decisions with hermetic reader injection")
    from contextlib import contextmanager
    remote_reader = FakeReader(slide)
    opened, localized = [], []
    @contextmanager
    def stream_unavailable(path):
        opened.append(path)
        yield None if len(opened) == 1 else slide
    @contextmanager
    def downloaded(path, **kwargs):
        localized.append(path)
        yield "/synthetic/downloaded.svs"
    remote_reader.slide = stream_unavailable
    remote_reader.localize = downloaded
    with patch.object(M, "GCSWSIReader", return_value=remote_reader):
        result = M.run("https://synthetic/slide.svs", components={"tile_metrics"},
                       supplied=dict(supplied, tile_list=write_tiles("remote_valid", [tile])),
                       out_dir=str(root / "remote_fallback"), metadata_enabled=False,
                       stain_type="AT8", download_models=False, write=False, save_artifacts=False)
    check("unavailable streamed header falls back to one local download for required tiles",
          result["report"]["provenance"]["execution"]["complete"]
          and len(localized) == 1 and opened == ["https://synthetic/slide.svs", "/synthetic/downloaded.svs"],
          {"opened": opened, "localized": localized})

    remote_reader = FakeReader(slide)
    localized.clear()
    remote_reader.localize = downloaded
    with patch.object(M, "GCSWSIReader", return_value=remote_reader), \
            patch.object(M.m1_wsi, "M2_MAX_READ_PX", 100):
        result = M.run("https://synthetic/large-level.svs", components={"tissue_segmentation"},
                       out_dir=str(root / "band_localization"), metadata_enabled=False,
                       stain_type="AT8", download_models=False, write=False, save_artifacts=False)
    check("remote analysis level over the read cap localizes before bounded band reading",
          result["report"]["provenance"]["execution"]["complete"]
          and len(localized) == 1 and result["read"]["read_mode"] == "banded", result["read"])

    print("\n[6] CLI outcomes distinguish usage, processing, completion and interruption")
    def cli(args):
        stdout, stderr = io.StringIO(), io.StringIO()
        before = signal.getsignal(signal.SIGTERM)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = M.main(args)
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue(), signal.getsignal(signal.SIGTERM) == before

    base = ["--slide", str(slide_path), "--no_metadata", "--no_model_download", "--quiet"]
    for name, args in [("missing slide argument", []),
                       ("conflicting metadata flags", base + ["--metadata", str(metadata)]),
                       ("metadata key without metadata", ["--slide", str(slide_path), "--metadata_key", "path"]),
                       ("missing required mask", base + ["--run_focus"])]:
        code, _, err, restored = cli(args)
        check(f"CLI {name} exits 2 and restores signal handling", code == 2 and restored and bool(err), err[-300:])
    code, stdout, _, restored = cli(["--list_components"])
    check("CLI component listing needs no slide", code == 0 and "tissue" in stdout and restored)
    for name, opens, expected in [("successful read", True, 0), ("unreadable slide", False, 1)]:
        local_reader = FakeReader(slide)
        if not opens:
            from contextlib import contextmanager
            @contextmanager
            def unavailable(*args, **kwargs):
                yield None
            local_reader.slide = unavailable
        with patch.object(M, "GCSWSIReader", return_value=local_reader):
            code, stdout, err, restored = cli(base + ["--run_tissue_segmentation", "--no_save_artifacts",
                                                    "--out", str(root / name)])
        check(f"CLI {name} exits {expected} with an explicit report location",
              code == expected and "report" in stdout + err and restored, err[-300:])
    with patch.object(M, "run", side_effect=KeyboardInterrupt):
        code, _, _, restored = cli(base + ["--run_tissue_segmentation"])
    check("CLI interruption returns 130 and restores caller signal handling", code == 130 and restored)
finally:
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{'=' * 70}\nPIPELINE PATHS SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL: print(f"  - {name}")
sys.exit(1 if FAIL else 0)
