"""Smoke test — concurrent batch failures, real corrupt TIFFs and resumable outputs.

No network, model weights or real slides. Eight workers run the actual CLI over synthetic
TIFFs and damaged files. Assertions follow batch/README.md's isolation, resume and output
contracts, never observed QC scores.
Run: PYTHONDONTWRITEBYTECODE=1 python src/tests/smoke_batch_failures.py
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import tifffile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from fake_slide import FakeSlide, write_tiff  # noqa: E402
from pathnd_qc.batch import runs  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + str(detail) if detail else ''}"
    )


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def invoke(*extra):
    # Library and CLI docs: selected tissue segmentation requires no model or metadata.
    command = [
        sys.executable,
        "-P",
        "-m",
        "pathnd_qc.batch",
        "run",
        "--slide_dir",
        str(slides),
        "--out",
        str(output),
        "--batch_id",
        "mixed",
        "--workers",
        "8",
        "--timeout_s",
        "120",
        "--run_tissue_segmentation",
        "--no_metadata",
        "--no_model_download",
        "--quiet",
        *extra,
    ]
    return subprocess.run(
        command, env=environment, capture_output=True, text=True, timeout=180
    )


def report_for(row):
    folder = Path(row["run_dir"])
    status_path = next(folder.glob("*_status.json"))
    status = read(status_path)
    return status, read(folder / status["report"])


def saved_reports():
    return {str(path): path.read_bytes() for path in batch.rglob("*_report.json")}


def rows_by_slide():
    return {row["slide"]: row for row in read(batch / "results.json")}


def check_final_progress(label, total):
    progress = read(batch / "progress.json")
    check(
        f"{label} finalizes every job with no pending or active work",
        progress["final"]
        and progress["total"] == total
        and progress["done"] == total
        and progress["pending"] == 0
        and progress["in_flight"] == [],
        progress,
    )


out = Path(tempfile.mkdtemp(prefix="pathnd_batch_failures_")).resolve()
try:
    slides = out / "slides"
    slides.mkdir()
    # Nested output exercises the documented discovery exclusion on every subsequent invocation.
    output = slides / "reports"
    batch = output / "batch_mixed"
    environment = dict(
        os.environ,
        PYTHONPATH=str(HERE.parent),
        PYTHONDONTWRITEBYTECODE="1",
        PATHND_CONFIG="",
        PATHND_NO_MODEL_DOWNLOAD="1",
        PATHND_DATA_DIR=str(out / "models"),
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
    )
    healthy = []
    for index in range(8):
        folder = slides / f"case_{index}"
        folder.mkdir()
        # Equal stems in mirrored subfolders must retain independent output identities.
        path = folder / "same_name.tif"
        write_tiff(FakeSlide(base_wh=(512, 384), seed=31 + index), str(path))
        healthy.append(str(path))
    header = slides / "00_bad_header.tif"
    header.write_bytes(b"This is not a TIFF image.")
    pixels = slides / "01_bad_pixels.tif"
    write_tiff(FakeSlide(base_wh=(512, 384), seed=77), str(pixels))
    # Retain a valid TIFF directory/scale but invalidate every compressed tile. This reaches
    # a decoder failure after open/metadata, unlike the independent corrupt-header case.
    with tifffile.TiffFile(pixels) as image:
        segments = [
            (offset, length)
            for level in image.series[0].levels
            for page in level.pages
            for offset, length in zip(page.dataoffsets, page.databytecounts)
        ]
    with pixels.open("r+b") as stream:
        for offset, length in segments:
            stream.seek(offset)
            stream.write(b"\0" * length)
    bad = {str(header), str(pixels)}
    all_slides = set(healthy) | bad

    print(
        "\n[1] Eight real workers isolate corrupt headers and compressed tile payloads"
    )
    result = invoke()
    check(
        "a mixed successful/failed batch CLI returns failure",
        result.returncode == 1,
        result.stdout + result.stderr,
    )
    rows = rows_by_slide()
    check(
        "every discovered slide has exactly one outcome despite worker failures",
        len(read(batch / "results.json")) == len(all_slides)
        and set(rows) == all_slides,
        {key: row["outcome"] for key, row in rows.items()},
    )
    check(
        "all eight healthy slides finish despite corrupt peers and a second scheduling wave",
        all(
            rows[path]["outcome"] == "completed" and rows[path]["exit_code"] == 0
            for path in healthy
        ),
    )
    for path, label in (
        (str(header), "corrupt header"),
        (str(pixels), "corrupt compressed pixels"),
    ):
        row = rows[path]
        status, report = report_for(row)
        check(
            f"{label} records a failed run and a nonempty diagnostic",
            row["outcome"] == "failed"
            and row["exit_code"] == 1
            and status["state"] == "failed"
            and bool(report.get("error", {}).get("message")),
            report.get("error"),
        )
        check(
            f"{label} keeps report sections and marks unexecuted tissue as unavailable",
            {"m1", "m2", "m3", "m4", "provenance"} <= set(report)
            and report["provenance"]["execution"]["complete"] is False
            and report["m2"]["tissue"].get("tissue_coverage_score") is None
            and bool(report["m2"]["tissue"]["error"]),
            report["m2"]["tissue"],
        )
    check(
        "equal filenames retain separate mirrored run folders",
        len({rows[path]["run_dir"] for path in healthy}) == len(healthy)
        and all(
            Path(rows[path]["run_dir"]).is_relative_to(batch / Path(path).parent.name)
            for path in healthy
        ),
    )
    reports = [report_for(rows[path])[1] for path in healthy]
    check(
        "successful peers retain complete reports with physical tissue fractions",
        all(
            report["provenance"]["execution"]["complete"] is True
            and report["m2"]["tissue"]["error"] is None
            and 0 <= report["m2"]["tissue"]["tissue_coverage_score"] <= 1
            for report in reports
        ),
    )
    initial_summary = read(batch / "summary.json")
    check(
        "the summary counts every healthy and failed run without parse errors",
        initial_summary["counts"]["runs"] == 10
        and initial_summary["counts"]["complete"] == 8
        and initial_summary["counts"]["incomplete"] == 2
        and initial_summary["counts"]["read_errors"] == 0,
        initial_summary["counts"],
    )
    with (batch / "results.csv").open(newline="", encoding="utf-8") as stream:
        csv_rows = list(csv.DictReader(stream))
    check(
        "CSV and JSON expose the same per-slide outcomes",
        {row["slide"]: row["outcome"] for row in csv_rows}
        == {path: row["outcome"] for path, row in rows.items()},
    )
    check_final_progress("mixed batch", 10)

    print("\n[2] A concurrent invocation cannot modify a batch held by another owner")
    before = {
        path.name: path.read_bytes()
        for path in batch.iterdir()
        if path.is_file() and path.name != ".batch.lock"
    }
    with runs.open_batch_dir(output, "mixed"):
        contender = invoke()
    check(
        "a second process receives the documented already-running refusal",
        contender.returncode == 2 and "already running" in contender.stderr,
        contender.stderr,
    )
    check(
        "the refused contender leaves all batch bookkeeping intact",
        before
        == {
            path.name: path.read_bytes()
            for path in batch.iterdir()
            if path.is_file() and path.name != ".batch.lock"
        },
    )

    print(
        "\n[3] Resume preserves healthy outputs and requires explicit retry of unchanged failures"
    )
    original_reports = saved_reports()
    resumed = invoke()
    resumed_rows = rows_by_slide()
    check(
        "default resume skips matching completed and failed runs without reprocessing",
        resumed.returncode == 0
        and len(resumed_rows) == 10
        and all(row["outcome"] == "skipped" for row in resumed_rows.values()),
        resumed.stderr,
    )
    check(
        "resume keeps all previous report bytes and run identities",
        saved_reports() == original_reports
        and all(
            resumed_rows[path]["run_dir"] == rows[path]["run_dir"]
            for path in all_slides
        ),
    )
    check_final_progress("resumed batch", 10)
    retry = invoke("--retry_failed")
    retry_rows = rows_by_slide()
    check(
        "retry_failed reruns corrupt slides while skipping completed peers",
        retry.returncode == 1
        and all(retry_rows[path]["outcome"] == "failed" for path in bad)
        and all(retry_rows[path]["outcome"] == "skipped" for path in healthy),
        retry.stderr,
    )
    check(
        "failed retries get separate runs and preserve every earlier report",
        all(retry_rows[path]["run_dir"] != rows[path]["run_dir"] for path in bad)
        and all(
            Path(path).read_bytes() == content
            for path, content in original_reports.items()
        )
        and len(saved_reports()) == 12,
    )
    check(
        "retry summary includes earlier attempts rather than replacing failures",
        read(batch / "summary.json")["counts"]["by_state"]
        == {"completed": 8, "failed": 4},
        read(batch / "summary.json")["counts"],
    )
    check_final_progress("failed retry", 10)

    print("\n[4] Repairing a slide invalidates its fingerprint without rerunning peers")
    write_tiff(FakeSlide(base_wh=(512, 384), seed=91), str(header))
    repaired = invoke()
    repaired_rows = rows_by_slide()
    check(
        "changed input reruns automatically and replaces its failed latest outcome with success",
        repaired.returncode == 0
        and repaired_rows[str(header)]["outcome"] == "completed"
        and repaired_rows[str(header)]["run_dir"] != retry_rows[str(header)]["run_dir"],
        repaired.stderr,
    )
    check(
        "repair leaves unchanged healthy and still-corrupt inputs skipped",
        all(
            row["outcome"] == "skipped"
            for path, row in repaired_rows.items()
            if path != str(header)
        ),
    )
    final_summary = read(batch / "summary.json")
    check(
        "repair retains failure history and adds one complete run without read errors",
        final_summary["counts"]["runs"] == 13
        and final_summary["counts"]["complete"] == 9
        and final_summary["counts"]["incomplete"] == 4
        and final_summary["counts"]["read_errors"] == 0,
        final_summary["counts"],
    )
    check(
        "generated output masks never enter subsequent slide discovery",
        set(repaired_rows) == all_slides
        and read(batch / "batch.json")["n_jobs"] == len(all_slides),
    )
    check_final_progress("repaired batch", 10)
finally:
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{'=' * 70}\nBATCH FAILURES SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  - {name}")
sys.exit(1 if FAIL else 0)
