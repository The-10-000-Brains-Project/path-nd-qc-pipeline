"""Aggregate saved run status, completion, timing, and headline metrics into a table.

Each row comes from a run’s status and report without recomputing measurements.
Runs with running status and no final report remain visible as incomplete work.
"""
from __future__ import annotations

import csv
import json
import math
from collections import Counter
from pathlib import Path

from pathnd_qc.batch import runs
from pathnd_qc import artifact_store as store, output_layout

COLUMNS = ["slide_id", "slide_path", "run_dir", "state", "started_at", "finished_at", "exit_stage",
           "pipeline_version", "git_commit", "git_dirty", "git_source", "source_sha256", "config_sha256",
           "exit_type", "exit_message", "complete", "execution_reasons", "warnings", "total_s",
           "dataset", "stain_type", "mpp_effective", "mpp_source", "mpp_base", "mpp_base_source", "components_run",
           "components_gated", "tissue_coverage", "fold_area_fraction", "focus_score", "n_tiles",
           "n_kept", "n_read_failed", "read_mode", "localized", "attempts", "error_types", "read_error"]


def _get(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


def _error_types(report: dict) -> list[str]:
    found: list[str] = []
    for stage in ("m2", "m3", "m4"):
        sections = report.get(stage) or {}
        if not isinstance(sections, dict):
            raise ValueError(f"report {stage} must be an object")
        for name, sec in sections.items():
            if isinstance(sec, dict) and sec.get("error_type"):
                found.append(f"{stage}.{name}:{sec['error_type']}")
    return found


def row_for(run_dir: Path) -> dict | None:
    """The summary row of one run folder, or None when it holds no status file."""
    run_dir = Path(run_dir)
    status_files = sorted(run_dir.glob("*_status.json"))
    if not status_files:
        return None
    slide_id = status_files[0].name[: -len("_status.json")]
    row = {c: None for c in COLUMNS}
    row.update(slide_id=slide_id, run_dir=str(run_dir))
    try:
        if len(status_files) != 1:
            raise ValueError("run folder contains multiple status files")
        _fill_row(run_dir, status_files[0], row)
        for key in ("total_s", "mpp_effective", "mpp_base", "tissue_coverage", "fold_area_fraction", "focus_score",
                    "n_tiles", "n_kept", "n_read_failed", "attempts"):
            value = row[key]
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value)):
                raise ValueError(f"report {key} must be a finite number or null")
        for key in ("complete", "localized", "git_dirty"):
            if row[key] is not None and not isinstance(row[key], bool):
                raise ValueError(f"report {key} must be a boolean or null")
        for key in ("state", "exit_stage", "exit_type", "exit_message", "dataset", "stain_type", "mpp_source", "mpp_base_source",
                    "pipeline_version", "git_commit", "git_source", "source_sha256", "config_sha256"):
            if row[key] is not None and not isinstance(row[key], str):
                raise ValueError(f"report {key} must be a string or null")
    except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        # Treat saved reports as untrusted inputs. Retain validated identity and status when
        # a report is damaged, without aggregating its partially decoded metrics.
        identity = {k: row[k] for k in ("slide_id", "slide_path", "run_dir", "state")
                    if isinstance(row[k], str)}
        row = dict.fromkeys(COLUMNS)
        row.update(identity, read_error=f"{type(exc).__name__}: {exc}")
    return row


def _fill_row(run_dir: Path, status_path: Path, row: dict) -> None:
    st = runs.read_status(status_path)
    slide_id = row["slide_id"]
    row.update(slide_id=slide_id, slide_path=st.get("slide_path"), run_dir=str(run_dir),
               state=st.get("state"), started_at=st.get("started_at"), finished_at=st.get("finished_at"),
               exit_stage=st.get("stage"), exit_type=st.get("type"), exit_message=st.get("message"))
    report_path = run_dir / (st.get("report") or f"{slide_id}_report.json")
    if report_path.exists():
        with open(report_path, encoding="utf-8") as f:
            rep = json.load(f)
        if not isinstance(rep, dict):
            raise ValueError("report must be a JSON object")
        prov = rep.get("provenance") or {}
        for key in ("pipeline_version", "git_commit", "git_dirty", "git_source", "source_sha256", "config_sha256"):
            row[key] = prov.get(key)
        execution = prov.get("execution") or prov.get("trust") or {}  # Read the completion field when the report lacks structured execution metadata.
        row.update(complete=execution.get("complete"),
                   execution_reasons="; ".join(execution.get("reasons") or []) or None,
                   warnings="; ".join(prov.get("warnings") or []) or None,
                   total_s=_get(rep, "timing", "total_s"),
                   dataset=_get(rep, "m1", "ingestion", "source", "dataset") or prov.get("bank"),
                   stain_type=prov.get("stain_type"),
                   mpp_effective=_get(rep, "m2", "read", "achieved_mpp",
                                      default=_get(rep, "m2", "read", "assumed_mpp")),
                   mpp_source=_get(rep, "m2", "read", "source"),
                   mpp_base=_get(rep, "m1", "ingestion", "acquisition", "mpp_effective"),
                   mpp_base_source=_get(rep, "m1", "ingestion", "acquisition", "mpp_source"),
                   components_run=",".join(prov.get("components_run") or []) or None,
                   components_gated=",".join(prov.get("components_gated") or []) or None,
                   tissue_coverage=_get(rep, "m2", "tissue", "tissue_coverage_score"),
                   fold_area_fraction=_get(rep, "m2", "folds", "fold_area_fraction"),
                   focus_score=_get(rep, "m2", "focus", "focus_score"),
                   n_tiles=_get(rep, "m3", "tiles", "n_tiles"), n_kept=_get(rep, "m3", "tiles", "n_kept"),
                   n_read_failed=_get(rep, "m3", "tiles", "n_read_failed"),
                   read_mode=_get(rep, "m2", "read", "read_mode"),
                   localized=_get(rep, "m1", "localize", "localized"),
                   attempts=_get(rep, "m1", "localize", "attempts"),
                   error_types=";".join(_error_types(rep)) or None)
        if row["exit_stage"] is None and rep.get("error"):
            row.update(exit_stage=_get(rep, "error", "stage"), exit_type=_get(rep, "error", "type"),
                       exit_message=_get(rep, "error", "message"))
    elif st["state"] in {"completed", "failed"}:
        raise ValueError(f"finished run has no report: {report_path.name}")


def summarize(out_dir) -> dict:
    """-> {"rows": [...], "counts": {...}} over every run folder under `out_dir`."""
    root = Path(out_dir)
    rows: list[dict] = []
    for d in output_layout.iter_run_dirs(root):
        row = row_for(d)
        if row:
            rows.append(row)
    counts = {
        "runs": len(rows),
        "by_state": dict(Counter(r["state"] for r in rows)),
        "complete": sum(1 for r in rows if r["complete"] is True),
        "incomplete": sum(1 for r in rows if r["complete"] is False),
        "read_errors": sum(bool(r["read_error"]) for r in rows),
        "by_dataset": dict(Counter(str(r["dataset"]) for r in rows)),
        "by_exit_type": dict(Counter(r["exit_type"] for r in rows if r["exit_type"])),
        "by_error_type": dict(Counter(e for r in rows for e in (r["error_types"] or "").split(";") if e)),
        "by_mpp_source": dict(Counter(str(r["mpp_source"]).split(" ")[0] for r in rows if r["mpp_source"])),
        "total_compute_s": round(sum(r["total_s"] or 0 for r in rows), 1),
    }
    return {"rows": rows, "counts": counts}


def write_summary(summary: dict, dest_dir) -> dict:
    """summary.csv + summary.json under `dest_dir`; returns their paths."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    csv_path, json_path = dest / "summary.csv", dest / "summary.json"
    with store.atomic_write(csv_path) as tmp:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(summary["rows"])
    runs.write_json(json_path, summary)
    return {"csv": str(csv_path), "json": str(json_path)}
