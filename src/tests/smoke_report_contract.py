"""Smoke test — failure reports, partial outputs and mask dependencies.

Incomplete requested analyses raise RunFailed and retain usable partial outputs.
Normalization reports preserve structured errors; empty exclusion masks remain valid.
No network, model weights or real slides. The synthetic fixture models SEA-AD pixels.
Run: PYTHONDONTWRITEBYTECODE=1 python src/tests/smoke_report_contract.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from contextlib import contextmanager

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.dont_write_bytecode = True
out = tempfile.mkdtemp(prefix="report_contract_", dir=HERE)
os.environ["TMPDIR"] = out
os.environ["MPLCONFIGDIR"] = os.path.join(out, "matplotlib")
tempfile.tempdir = out

import numpy as np
from PIL import Image

from fake_slide import FakeReader, FakeSlide
from pathnd_qc import pipeline
from pathnd_qc.ingestion.ingestion_checks import ingestion_checks as ingestion
from pathnd_qc.normalization.stain_norm import stain_norm as normalization

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + str(detail) if detail else ''}")


@contextmanager
def patched(obj, name, replacement):
    original = getattr(obj, name)
    setattr(obj, name, replacement)
    try:
        yield
    finally:
        setattr(obj, name, original)


def raises(fn):
    try:
        fn()
    except Exception as exc:
        return exc
    return None


def run_on(slide, name, **kwargs):
    with patched(pipeline, "GCSWSIReader", lambda *a, **k: FakeReader(slide)):
        return pipeline.run(str(Path(out) / "slide.svs"),
                            out_dir=str(Path(out) / name), metadata_enabled=False,
                            stain_type="AT8", download_models=False, **kwargs)


try:
    Path(out, "slide.svs").touch()  # Reader injection supplies the pixels; no file decoding.
    slide = FakeSlide(base_wh=(2048, 1536), downsamples=(1, 4), mpp=0.5016)
    reader = FakeReader(slide)
    thumb, _ = reader.read_at_mpp(slide, 8.0, info=reader.get_slide_info(slide))
    hw = (thumb.height, thumb.width)
    tissue = np.ones(hw, dtype=bool)
    empty = np.zeros(hw, dtype=bool)
    thumb.save(Path(out, "thumbnail.png"))
    for name, mask in (("tissue", tissue), ("empty", empty), ("full", tissue)):
        Image.fromarray(mask.astype(np.uint8) * 255).save(Path(out, name + ".png"))
    supplied = {"thumbnail": str(Path(out, "thumbnail.png")),
                "tissue_mask": str(Path(out, "tissue.png")),
                "fold_mask": str(Path(out, "empty.png")),
                "pen_mask": str(Path(out, "empty.png"))}

    print("\n[1] normalization errors retain their type through reporting")
    missing = normalization.load_reference("absent", spec={})
    report = normalization.to_report(missing)
    check("missing reference is a structured KeyError", report.get("error_type") == "KeyError"
          and report.get("normalized") is False, report.get("error"))
    frozen = normalization.normalize_slide(thumb, {}, method="invalid-method")
    report = normalization.to_report(frozen)
    check("failed source fit remains a structured ValueError in normalization report",
          report.get("error_type") == "ValueError" and report.get("normalized") is False,
          report.get("error"))
    check("normalization callers receive the original source-fit error type",
          frozen.get("error_type") == "ValueError", frozen.get("error_type"))
    with patched(normalization, "load_reference", lambda *a, **k: missing):
        result = pipeline._run_m4(thumb, tissue, "AT8", None, None, {"stain_normalization"})
    report = normalization.to_report(result["stain_norm"])
    check("orchestration preserves the reference error type despite its message prefix",
          report.get("error_type") == "KeyError", report.get("error"))
    clean = normalization.to_report({"result": np.asarray(thumb), "params": {}, "error": None})
    check("normalization success explicitly reports a null error type",
          "error_type" in clean and clean["error_type"] is None)

    print("\n[2] both stated scale axes must be valid")
    for bad_y in (-0.5, 0.0, None, float("nan")):
        result = ingestion.check_mpp({"mpp_x": 0.5, "mpp_y": bad_y, "objective_power": 20})
        check(f"invalid mpp_y={bad_y} cannot qualify as objective-confirmed stated scale",
              not str(result.get("scale_source")).startswith("stated"), result.get("scale_source"))
    confirmed = ingestion.check_mpp({"mpp_x": 0.05, "mpp_y": 0.05, "objective_power": 200})
    check("documented below-floor objective confirmation remains usable",
          str(confirmed.get("scale_source")).startswith("stated")
          and confirmed.get("effective_mpp") == 0.05, confirmed.get("scale_source"))
    invalid_y = ingestion.check_mpp({"mpp_x": 0.5, "mpp_y": -0.5, "objective_power": 20})
    # Mathematical invariant: 0.5 * 20 = 10 lies within the documented [5, 15] interval.
    check("invalid Y explanation does not mislabel the valid X/objective product",
          "10.0 is outside" not in str(invalid_y.get("reason")), invalid_y.get("reason"))

    print("\n[3] Mask semantics and requested-component completion")
    # Empty exclusions mean no detected artifacts; empty tissue has no analysis support.
    result = None
    exc = raises(lambda: run_on(slide, "empty_tissue", components={"focus"},
                               supplied=dict(supplied, tissue_mask=supplied["pen_mask"])))
    check("empty supplied tissue is refused before execution", isinstance(exc, ValueError)
          and "tissue_mask" in str(exc), exc)
    exc = None
    try:
        result = run_on(slide, "clean_focus", components={"focus"}, supplied=supplied)
    except Exception as exc:
        check("all-zero supplied pen and fold masks are accepted", False, exc)
    else:
        report = result["report"]
        check("all-zero supplied pen and fold masks are accepted",
              report["m2"]["focus"].get("error") is None, report["m2"]["focus"])
        trust = report["provenance"]["execution"]
        check("no automated trust verdict in report", "trust" not in report["provenance"]
              and "trustworthy" not in trust, trust)
        check("ordinary clean supplied focus is complete", trust.get("complete") is True, trust)

    exc = raises(lambda: run_on(slide, "excluded_focus", components={"focus"},
                                supplied=dict(supplied, fold_mask=str(Path(out, "full.png")))))
    report = exc.report if isinstance(exc, pipeline.RunFailed) else {}
    execution = report.get("provenance", {}).get("execution", {})
    check("handled focus failure raises RunFailed and makes execution incomplete",
          isinstance(exc, pipeline.RunFailed)
          and bool(report.get("m2", {}).get("focus", {}).get("error"))
          and execution.get("complete") is False, exc)
    check("no automated trust verdict in failed report", bool(report)
          and "trust" not in report.get("provenance", {}) and "trustworthy" not in execution)

    print("\n[4] Requested tile read failure remains visible at the report level")
    tiles_path = Path(out, "tiles.json")
    tiles_path.write_text(json.dumps([{"x": 0, "y": 0, "w": 512, "h": 512}]))

    corrupt = FakeSlide(base_wh=(2048, 1536), downsamples=(1, 4), mpp=0.5016)
    original_integrity = pipeline.check_integrity

    def fail_next_read(slide, info):
        checked = original_integrity(slide, info)
        # M1's probe is healthy; the next real decode is the supplied M3 tile.
        slide.fail_at = slide.n_reads + 1
        return checked

    with patched(pipeline, "check_integrity", fail_next_read):
        exc = raises(lambda: run_on(corrupt, "tile_read_failure", components={"tile_metrics"},
                                    supplied=dict(supplied, tile_list=str(tiles_path)), save_artifacts=False))
    report = exc.report if isinstance(exc, pipeline.RunFailed) else {}
    execution = report.get("provenance", {}).get("execution", {})
    check("tile read failures raise RunFailed and make requested metrics incomplete",
          isinstance(exc, pipeline.RunFailed)
          and report.get("m3", {}).get("tiles", {}).get("n_read_failed") == 1
          and execution.get("complete") is False and "trustworthy" not in execution, exc)

    print("\n[5] A failed artifact save preserves earlier provenance and later outputs")
    original_save = pipeline.store.save_mask_png
    attempted = []

    def fail_fold_save(mask, path):
        attempted.append(Path(path).name)
        if str(path).endswith("_fold_mask.png"):
            raise OSError("synthetic fold save failure")
        return original_save(mask, path)

    def tissue_result(*a, **k):
        return {"tissue_mask": tissue, "tissue_coverage_score": 1.0,
                "tissue_error": None, "runtime_s": 0.0}

    def fold_result(*a, **k):
        return {"fold_mask": empty, "fold_area_fraction": 0.0,
                "fold_error": None, "runtime_s": 0.0}

    def pen_result(*a, **k):
        return {"pen_mask": empty, "pen_area_fraction": 0.0,
                "pen_error": None, "runtime_s": 0.0}

    weights = Path(out, "pen.pt")
    weights.write_bytes(b"synthetic checkpoint; detector inference is stubbed")
    with patched(pipeline.store, "save_mask_png", fail_fold_save), \
            patched(pipeline.m2_tissue, "compute_tissue_mask", tissue_result), \
            patched(pipeline.m2_folds, "detect_folds", fold_result), \
            patched(pipeline.m2_pen, "detect_pen", pen_result):
        exc = raises(lambda: run_on(slide, "partial_save",
                     components={"tissue_segmentation", "fold_detection", "pen_detection"},
                     pen_weights=str(weights), supplied={"thumbnail": supplied["thumbnail"]}))
    report = exc.report if isinstance(exc, pipeline.RunFailed) else {}
    outputs = report.get("provenance", {}).get("outputs", {})
    check("failed fold save records failure while retaining successful tissue provenance",
          isinstance(exc, pipeline.RunFailed) and "tissue_mask" in outputs
          and "fold_mask" not in outputs, (str(exc), outputs))
    check("pen save after failed fold save is attempted and recorded",
          any(p.endswith("_pen_mask.png") for p in attempted) and "pen_mask" in outputs,
          attempted)
    check("partial save failure still writes a report for the caller",
          isinstance(exc, pipeline.RunFailed) and bool(exc.report_path)
          and Path(exc.report_path).is_file(), getattr(exc, "report_path", None))

    print("\n[6] Registry agrees with documented pen/fold precedence")
    # pipeline._run_m2: computed pen yields to folds; only supplied pen modifies computed folds.
    plan = pipeline.spec.plan_run({"pen_detection"},
                                 {"thumbnail": "thumb.png", "fold_mask": "fold.png"})
    check("supplied fold mask is consumed by requested pen detection",
          not plan["errors"] and not plan["warnings"], plan)
    plan = pipeline.spec.plan_run({"fold_detection"},
                                 {"thumbnail": "thumb.png", "tissue_mask": "tissue.png"})
    check("fold detection on tissue does not falsely degrade for absent pen",
          not plan["errors"] and "fold_detection" not in plan["degraded"], plan)
    plan = pipeline.spec.plan_run({"fold_detection"},
                                 {"thumbnail": "thumb.png", "tissue_mask": "tissue.png",
                                  "pen_mask": "pen.png"})
    check("supplied pen mask is consumed when folds are computed",
          not plan["errors"] and not plan["warnings"], plan)
    plan = pipeline.spec.plan_run({"fold_detection", "pen_detection"},
                                 {"thumbnail": "thumb.png", "tissue_mask": "tissue.png"})
    check("computing both masks produces no false circular dependency or degradation",
          not plan["errors"] and not plan["degraded"], plan)
finally:
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{'=' * 70}\nREPORT CONTRACT SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for n in FAIL:
    print(f"  - {n}")
sys.exit(1 if FAIL else 0)
