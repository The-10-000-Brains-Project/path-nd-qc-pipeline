"""Smoke test — damaged or incomplete saved runs remain visible without invented metrics.

Contracts: batch/README.md summary/resume rules and summary.py row_for docstring.
All files are synthetic local JSON; no slides, models or network are required.
Run: python src/tests/smoke_saved_failures.py
"""

from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from pathnd_qc.batch import summary

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + str(detail) if detail else ''}"
    )


out = Path(tempfile.mkdtemp(prefix="pathnd-saved-failures-"))
base = {
    "provenance": {"execution": {"complete": True}},
    "timing": {"total_s": 2.5},
    "m2": {"tissue": {"tissue_coverage_score": 0.5}},
}


def saved(name, report=None, state="completed"):
    directory = out / "runs" / name
    directory.mkdir(parents=True)
    status = {
        "slide_path": f"/{name}.svs",
        "state": state,
        "report": "slide_report.json",
    }
    (directory / "slide_status.json").write_text(json.dumps(status))
    if report is not None:
        (directory / "slide_report.json").write_text(json.dumps(report))
    return directory


try:
    print("\n[1] Corrupt reports cannot contribute partially decoded measurements")
    # Saved files are untrusted: validation failure must clear all measurements even
    # when earlier fields in the same report looked plausible.
    cases = [
        ("nonfinite timing", ("timing", "total_s"), float("nan")),
        ("infinite scale", ("m2", "read", "achieved_mpp"), float("inf")),
        ("boolean measurement", ("m2", "tissue", "tissue_coverage_score"), True),
        ("nonboolean completion", ("provenance", "execution", "complete"), "true"),
        ("nonboolean provenance", ("provenance", "git_dirty"), 1),
        ("nonstring dataset", ("m1", "ingestion", "source", "dataset"), ["bank"]),
        ("nonobject stage", ("m3",), ["broken"]),
        ("nonstring failure reason", ("provenance", "execution", "reasons"), [3]),
    ]
    for index, (name, keys, value) in enumerate(cases):
        report = copy.deepcopy(base)
        obj = report
        for key in keys[:-1]:
            obj = obj.setdefault(key, {})
        obj[keys[-1]] = value
        folder = saved(f"damaged{index}", report)
        row = summary.row_for(folder)
        check(
            f"{name} keeps identity but excludes all measurements",
            bool(row["read_error"])
            and row["slide_path"] == f"/damaged{index}.svs"
            and row["state"] == "completed"
            and all(
                row[key] is None
                for key in ("total_s", "complete", "tissue_coverage", "dataset")
            ),
            row,
        )

    print("\n[2] Incomplete files and explicit errors retain their meaning")
    running = summary.row_for(saved("running", state="running"))
    check(
        "running job without report is visible without invented completion or duration",
        running["state"] == "running"
        and running["read_error"] is None
        and running["complete"] is None
        and running["total_s"] is None,
    )
    for state in ("completed", "failed"):
        row = summary.row_for(saved(f"missing_{state}", state=state))
        check(
            f"{state} job without final report records a read error",
            row["state"] == state
            and bool(row["read_error"])
            and row["complete"] is None,
            row,
        )
    multiple = saved("ambiguous", base)
    shutil.copyfile(multiple / "slide_status.json", multiple / "other_status.json")
    check(
        "multiple status files cannot choose an arbitrary run",
        bool(summary.row_for(multiple)["read_error"]),
    )
    orphan = out / "orphan"
    orphan.mkdir()
    (orphan / "slide_report.json").write_text(json.dumps(base))
    check(
        "report without status is not treated as a completed run",
        summary.row_for(orphan) is None,
    )

    failed = copy.deepcopy(base)
    failed["provenance"]["execution"] = {
        "complete": False,
        "reasons": ["tile decode failed"],
    }
    failed["error"] = {
        "stage": "m3",
        "type": "OSError",
        "message": "tile decode failed",
    }
    failed["m3"] = {"tiles": {"error_type": "OSError", "n_read_failed": 1}}
    row = summary.row_for(saved("explicit_failure", failed, state="failed"))
    check(
        "partial failed run keeps valid earlier measurements and exposes component error",
        row["complete"] is False
        and row["tissue_coverage"] == 0.5
        and row["exit_stage"] == "m3"
        and row["exit_type"] == "OSError"
        and row["error_types"] == "m3.tiles:OSError"
        and row["n_read_failed"] == 1,
        row,
    )

    print("\n[3] Aggregate exports retain failures and survive failed replacement")
    saved("healthy", base)
    result = summary.summarize(out / "runs")
    check(
        "aggregate duration excludes corrupted reports and preserves actual partial work",
        result["counts"]["total_compute_s"] == 5.0
        and result["counts"]["complete"] == 1
        and result["counts"]["incomplete"] == 1
        and result["counts"]["read_errors"] == len(cases) + 3,
        result["counts"],
    )
    paths = summary.write_summary(result, out / "export")
    with open(paths["csv"], newline="") as handle:
        rows = list(csv.DictReader(handle))
    check(
        "CSV keeps incomplete measurements blank and failed measurements explicit",
        next(row for row in rows if row["slide_path"] == "/running.svs")["total_s"]
        == ""
        and next(row for row in rows if row["slide_path"] == "/explicit_failure.svs")[
            "n_read_failed"
        ]
        == "1",
    )
    before = {name: Path(path).read_bytes() for name, path in paths.items()}
    error = None
    with patch.object(
        csv.DictWriter, "writerows", side_effect=OSError("disk write failed")
    ):
        try:
            summary.write_summary(result, out / "export")
        except OSError as exc:
            error = exc
    check(
        "failed CSV replacement raises and preserves prior CSV and JSON bytes",
        error is not None
        and all(
            Path(paths[name]).read_bytes() == value for name, value in before.items()
        ),
    )
    check(
        "failed export leaves no temporary file for another reader to mistake for output",
        {p.name for p in (out / "export").iterdir()} == {"summary.csv", "summary.json"},
    )
finally:
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{'=' * 70}\nSAVED FAILURES SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  - {name}")
sys.exit(1 if FAIL else 0)
