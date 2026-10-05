"""Assemble per-slide reports, evaluate configured thresholds, and write JSON.

Component `to_report()` functions define measurement sections. This module groups
them by stage, adds provenance and timing, and evaluates bounds. Ingestion data
is included under m1.ingestion.

Thresholds support lower and upper bounds and per-stain overrides. Shipped bounds
are null. Out-of-range values generate flags without stopping the pipeline or gating
M3. When no metric is checked, verdict.passed is None rather than a claimed pass.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Optional

from pathnd_qc import artifact_store as store
from pathnd_qc._logging import get_logger
from pathnd_qc.config.config import cfg, now_stamps, provenance, resolve_out_dir, thresholds

logger = get_logger(__name__)

DEFAULT_OUT_DIR = resolve_out_dir()      # same resolution pipeline.run() uses — one definition


def _dig(node: Any, dotted: str) -> Any:
    """Walk `"m2.folds.fold_area_fraction"` through the assembled report; None if absent."""
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def resolve_bounds(bounds: Optional[dict], stain_type: Optional[str]) -> tuple:
    """Select a metric’s bounds using the stain override, then the global bounds.

    Stain spellings are matched case-insensitively and through m4.stain_aliases.
    Returns (min, max, source), where source is "by_stain:<key>" or "global".
    Per-stain bounds allow stain-dependent metrics such as chroma to use separate bands.
    """
    if bounds is not None and not isinstance(bounds, dict):
        raise ValueError("threshold entry must be an object with min/max bounds")
    bounds = bounds or {}
    by_stain = bounds.get("by_stain", {})
    if by_stain is None:
        by_stain = {}
    if not isinstance(by_stain, dict) or any(not isinstance(k, str) for k in by_stain):
        raise ValueError("by_stain must map stain names to bound objects")
    if any(v is not None and not isinstance(v, dict) for v in by_stain.values()):
        raise ValueError("each by_stain entry must be a bound object or null")
    if by_stain and stain_type:
        raw = str(stain_type).strip()
        aliases = {k.strip().lower(): v for k, v in (cfg("m4.stain_aliases", {}) or {}).items()}
        lowered = {k.strip().lower(): k for k in by_stain}
        for candidate in (raw, aliases.get(raw.lower()) or ""):
            hit = lowered.get(candidate.strip().lower()) if candidate else None
            if hit is not None:
                entry = by_stain[hit] or {}
                return entry.get("min"), entry.get("max"), f"by_stain:{hit}"
    return bounds.get("min"), bounds.get("max"), "global"


def _finite_number(value) -> bool:
    return (not isinstance(value, bool) and
            (isinstance(value, int) or isinstance(value, float) and math.isfinite(value)))


def evaluate_thresholds(report: dict, table: Optional[dict] = None) -> dict:
    """Compare recorded metrics against the config thresholds. Returns the `verdict` block.

    A threshold entry is `{"min": x|null, "max": y|null}`, optionally with a `by_stain` override map
    (see `resolve_bounds`); a null bound is simply not checked, so an all-null table produces no flags
    and `passed` stays null. `checked` counts evaluated metrics, once per metric even with two
    bounds; non-finite numeric values count as checked and fail. Missing values and invalid
    bounds are flagged without increasing that count.
    """
    table = thresholds() if table is None else table
    stain_type = ((report.get("provenance") or {}).get("stain_type")
                  or ((report.get("m1") or {}).get("ingestion") or {}).get("source", {}).get("stain_type"))
    flags: list[dict] = []
    checked = 0

    if not isinstance(table, dict):
        return {"passed": None, "n_thresholds_checked": 0,
                "flags": [{"reason": "threshold table must be an object"}]}

    for metric, bounds in (table or {}).items():
        try:
            if not isinstance(metric, str):
                raise ValueError("threshold metric path must be a string")
            lo, hi, band = resolve_bounds(bounds, stain_type)
        except ValueError as exc:
            flags.append({"metric": str(metric), "value": None, "reason": str(exc)})
            continue
        if lo is None and hi is None:
            continue
        if any(b is not None and not _finite_number(b)
               for b in (lo, hi)):
            # Report invalid bounds as configuration flags without attempting a comparison.
            flags.append({"metric": metric, "value": None, "min": lo, "max": hi, "band": band,
                          "reason": f"threshold bound is not a finite number (min={lo!r}, max={hi!r}); "
                                    f"check the config"})
            continue
        if lo is not None and hi is not None and lo > hi:
            flags.append({"metric": metric, "value": None, "min": lo, "max": hi, "band": band,
                          "reason": "threshold minimum exceeds maximum; check the config"})
            continue
        value = _dig(report, metric)
        if value is None:
            flags.append({"metric": metric, "value": None, "min": lo, "max": hi, "band": band,
                          "reason": "metric not present in this report"})
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            # A metric path must resolve to a number; report nonnumeric values as flags.
            flags.append({"metric": metric, "value": None, "min": lo, "max": hi, "band": band,
                          "reason": f"metric is not a number ({type(value).__name__}); "
                                    f"check the threshold key"})
            continue
        checked += 1
        if not _finite_number(value):
            flags.append({"metric": metric, "value": None, "min": lo, "max": hi, "band": band,
                          "reason": f"metric is not finite ({value!r}); no valid measurement"})
            continue
        if lo is not None and value < lo:
            flags.append({"metric": metric, "value": value, "min": lo, "max": hi, "band": band,
                          "reason": f"{value} < min {lo}"})
        elif hi is not None and value > hi:
            flags.append({"metric": metric, "value": value, "min": lo, "max": hi, "band": band,
                          "reason": f"{value} > max {hi}"})

    # No evaluated bounds means no pass/fail verdict.
    passed = None if checked == 0 else (len(flags) == 0)
    return {"passed": passed, "flags": flags, "n_thresholds_checked": checked}


def collect_timing(report: dict, extra: Optional[dict] = None,
                   total_s: Optional[float] = None) -> dict:
    """Roll every component's `runtime_s` up into one `timing` block.

    Read OUT OF the assembled subsections rather than re-instrumented, so a component's own reported
    time and the rollup can never disagree. `extra` carries stage costs that belong to no component
    (localize, the slide open, the M2 plane read).

    The sum includes recorded extra costs such as localization. total_s covers the caller's
    measured interval; in the pipeline it excludes interpreter startup and final report writing.
    unattributed_s is the remaining difference, including uninstrumented work and rounding.
    These diagnostics do not enter the verdict.
    """
    components: dict = {}
    for stage in ("m1", "m2", "m3", "m4"):
        for name, sub in (report.get(stage) or {}).items():
            if not isinstance(sub, dict) or sub.get("runtime_s") is None:
                continue
            if str(sub.get("error", "")).startswith("skipped"):
                continue            # a skip has no runtime; a 0.0 here reads as "took no time"
            components[f"{stage}.{name}"] = round(float(sub["runtime_s"]), 4)
    for key, value in (extra or {}).items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"timing entry {key!r} must be a number of seconds, got {type(value).__name__}")
        components[key] = round(float(value), 4)

    stages = {}
    for stage in ("m1", "m2", "m3", "m4"):
        vals = [v for k, v in components.items() if k.startswith(f"{stage}.")]
        stages[stage] = round(sum(vals), 4) if vals else None
    summed = round(sum(v for v in stages.values() if v), 4)
    return {"components": dict(sorted(components.items(), key=lambda kv: -kv[1])),
            "stages": stages,
            "components_total_s": summed,
            "total_s": round(float(total_s), 4) if total_s is not None else None,
            "unattributed_s": (round(float(total_s) - summed, 4)
                               if total_s is not None else None),
            "note": ("seconds, wall-clock. components_total_s includes recorded acquisition/localization. "
                     "total_s covers the measured pipeline interval, excluding interpreter startup "
                     "and final report writing; unattributed_s is the remaining difference. "
                     "Machine- and network-dependent diagnostics; not thresholded.")}


def build_report(slide_id: str, slide_path: str, *, stain_type: Optional[str] = None,
                 m1: Optional[dict] = None, m2: Optional[dict] = None,
                 m3: Optional[dict] = None, m4: Optional[dict] = None,
                 generated_at: Optional[str] = None,
                 timing_extra: Optional[dict] = None,
                 total_s: Optional[float] = None,
                 run_provenance: Optional[dict] = None) -> dict:
    """Assemble the per-slide report. `m1`..`m4` are already-`to_report()`ed subsection dicts.

    `m4` (stain normalization) is optional and defaults to `{}` like the others, so a caller that
    does not run it — or a slide where it failed — still produces a complete, well-formed report.
    Pass `run_provenance` to preserve a snapshot collected before processing; otherwise identity
    and configuration provenance are collected here. The supplied snapshot is not mutated.
    """
    prov = provenance() if run_provenance is None else copy.deepcopy(run_provenance)
    prov["stain_type"] = stain_type
    if not stain_type:
        # Without a stain, tissue and folds use their default routes. Record that limitation
        # when neither metadata nor the explicit stain option supplies a value.
        prov["stain_type"] = None
        prov["stain_type_flag"] = ("stain_type not supplied (no --stain and none resolved from "
                                   "metadata) — tissue and fold routing both defaulted to the "
                                   "non-Hirano path")

    stamps = now_stamps()
    report = {
        "slide_id": slide_id,
        "slide_path": slide_path,
        "generated_at": generated_at or stamps[0],          # UTC, explicit offset
        "generated_at_local": stamps[1],                    # the same instant, local offset
        "provenance": prov,
        "verdict": None,                       # filled below, after the sections exist
        "m1": m1 or {},
        "m2": m2 or {},
        "m3": m3 or {},
        "m4": m4 or {},
    }
    report["verdict"] = evaluate_thresholds(report)
    report["timing"] = collect_timing(report, timing_extra, total_s)
    return report


def write_report(report: dict, out_dir: Optional[str] = None) -> Path:
    """Write `<out_dir>/<slide_id>_report.json`. Creates the directory if needed."""
    out_dir = resolve_out_dir(out_dir or os.environ.get("PATHND_REPORT_DIR"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{report.get('slide_id', 'slide')}_report.json"
    with store.atomic_write(path) as tmp:        # Publish the complete report with an atomic rename.
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_json_safe(report), f, indent=2, default=str, allow_nan=False)
    logger.info("Wrote %s", path)
    return path


def _json_safe(value):
    """Non-finite measurements serialize as missing; never emit invalid JSON tokens."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    demo = build_report("DEMO", "gs://bucket/demo.svs", stain_type=None,
                        m2={"tissue": {"tissue_coverage_score": 0.61, "error": None}})
    print(json.dumps(demo["verdict"], indent=2))
    print("stain flag:", demo["provenance"].get("stain_type_flag"))
    print("thresholds in config:", len(thresholds()), "| all null ->",
          demo["verdict"]["n_thresholds_checked"] == 0, "| passed:", demo["verdict"]["passed"])
