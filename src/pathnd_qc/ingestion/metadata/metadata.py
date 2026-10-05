"""Read only user-supplied slide-metadata CSVs into the standard 35-column record.

Lookup joins on the full slide path and records which file supplied the values. Missing
columns remain null; explicit stain arguments override metadata for routing. No source registry,
default brain-bank paths or shared default index is consulted. Without supplied sources, lookup
is skipped and slide analysis can continue using explicit values and embedded scanner metadata.
"""

from __future__ import annotations

import logging
import time
from threading import local
from typing import Optional

from pathnd_qc._logging import get_logger
from pathnd_qc._fs import slide_id
from pathnd_qc.config.config import cfg
from pathnd_qc.ingestion._download import open_binary

logger = get_logger(__name__)

# Define the 35-field standard export schema in file order. Additional dataset
# columns remain in extra, including QSBB artifact_type labels.
STANDARD_COLUMNS = (
    "study", "brain_bank_id", "participant_id", "other_case_identifiers", "institution",
    "stain_type", "slide_paths", "sex", "center_of_sequencing", "genotyping_chip",
    "primary_id", "rep_no", "instance_id",
    "_submission_id", "_version", "_source_row", "_accepted_at", "_accepted_by",
    "brain_region", "age_at_death", "highest_level_of_education", "education_years",
    "apoe_status", "cog_status", "last_casi_score", "brain_weight_grams", "nia_aa_adnc_score",
    "path_thal", "path_braak_neurofibrillary_tangle_stage", "path_cerad", "path_mckeith",
    "vascular_pathology_severity", "arteriolosclerosis_severity", "late_tdp43_score", "primary_dx",
)

# The six source/provenance fields derived from the standard record.
SOURCE_FIELDS = ("dataset", "slide_id", "participant_id", "brain_region", "stain_type",
                 "slide_reference")

# Diagnostics belong to the explicitly supplied files in this caller's last lookup.
_LAST_BUILD = local()  # compatibility accessor: last build in this caller's thread


def slide_key(path: str) -> str:
    """Match full references, preserving cloud object names and local directory identity."""
    from pathlib import Path
    path = str(path).strip()
    if path.startswith("gcs://"):
        path = "gs://" + path[len("gcs://"):]
    return path if "://" in path else str(Path(path).expanduser().resolve())


def _region_from_path(path: str) -> Optional[str]:
    """Parse a PART brain region from the slide’s parent directory.

    For example, slides/Hippocampus_AT8_stain/42312.svs yields Hippocampus.
    Split the folder name at the first underscore and report the region as derived.
    """
    parts = str(path).rstrip("/").split("/")
    return parts[-2].split("_")[0] if len(parts) >= 2 else None


def _blank_record() -> dict:
    return {c: None for c in STANDARD_COLUMNS}


def _read_csv(path: str):
    """Read one explicitly supplied local/cloud metadata CSV; return rows or raise."""
    import pandas as pd
    with open_binary(path, cfg("ingestion.localize_stall_timeout_s", 120.0)) as handle:
        frame = pd.read_csv(handle, low_memory=False)
    frame = frame.where(frame.notna(), None)
    return frame.to_dict("records")


def _coerce(value):
    """NaN/empty -> None; numpy scalars -> Python. Keeps the record JSON-safe."""
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (AttributeError, ValueError):
            pass
    if isinstance(value, float) and value != value:          # NaN
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


# Recognized slide-reference column names, tried in order for a supplied CSV.
KEY_CANDIDATES = ("slide_paths", "gs_file_path", "slide_path", "file_path", "path")


def sources_from_paths(paths, key: Optional[str] = None) -> list:
    """Build source specs from bare paths supplied on the command line.

    Used by `pathnd-qc --metadata` and `run(metadata_paths=[...])`. These are the only files read.

    `dataset` is left None — a bare path does not declare one, and inventing a label would put a
    guess into `metadata_source.dataset`, which exists precisely so a null is always attributable.
    Key detection happens during the index read, so each source is opened once and a failed read
    keeps its original exception in the diagnostics.
    """
    return [{"dataset": None, "path": p, "key": key, "from_cli": True}
            for p in (paths or [])]


def build_index(sources) -> dict:
    """Read the explicitly supplied sources and index them by slide key.

    Degrades per source: an unreachable or malformed file is recorded in `index_errors()` and the
    others still load. A source that fails must never take the pipeline with it -- metadata is an
    enrichment, and its absence leaves fields unresolved unless explicit arguments supply them.
    """
    index, errors = _load_index(sources)
    _LAST_BUILD.errors = errors
    return index


def _load_index(sources) -> tuple[dict, list]:
    """Read only this call's sources and return their rows and diagnostics together."""
    index: dict = {}
    errors: list = []
    for spec in (sources or []):
        path = spec.get("path")
        dataset = spec.get("dataset")
        if not path:
            continue
        try:
            rows = _read_csv(path)
        except Exception as exc:                              # noqa: BLE001 - degrade, never raise
            errors.append({"dataset": dataset, "path": path,
                           "error": f"{type(exc).__name__}: {exc}"})
            logger.warning("metadata source unreadable (%s): %s", path, exc)
            continue

        key_col = spec.get("key", "slide_paths")
        if not key_col and spec.get("from_cli"):
            header = set(rows[0]) if rows else set()
            key_col = next((c for c in KEY_CANDIDATES if c in header), None)
        if not key_col:
            errors.append({"dataset": dataset, "path": path,
                           "error": "no slide-key column found (tried "
                                    f"{', '.join(KEY_CANDIDATES)}); pass --metadata_key"})
            logger.warning("no slide-key column in %s", path)
            continue
        if rows and key_col not in rows[0]:
            # Report a missing declared key column as a metadata-file error.
            errors.append({"dataset": dataset, "path": path,
                           "error": f"key column {key_col!r} is not in the header of {path} "
                                    f"(columns: {', '.join(list(rows[0])[:8])}...); pass "
                                    f"--metadata_key with a column that exists"})
            logger.warning("key column %r not in %s", key_col, path)
            continue
        colmap = spec.get("columns") or {}                    # standard column -> this file's column
        extra_cols = spec.get("extra") or []
        for row in rows:
            reference = _coerce(row.get(key_col))
            if reference is None:
                continue
            key = slide_key(reference)
            record = _blank_record()
            for standard in STANDARD_COLUMNS:
                record[standard] = _coerce(row.get(colmap.get(standard, standard)))
            record["slide_paths"] = reference                 # the key column is authoritative here
            parsed = {}
            if spec.get("region_from_path") and not record.get("brain_region"):
                region = _region_from_path(reference)
                if region:
                    record["brain_region"] = region
                    parsed["brain_region"] = "parsed from the slide path folder, not read from a column"
            entry = {
                "metadata": record,
                "extra": {c: _coerce(row.get(c)) for c in extra_cols},
                "metadata_source": {"dataset": dataset, "path": path, "key_column": key_col,
                                    "matched_key": key, "parsed_fields": parsed},
            }
            if key in index:
                # Use the first matching row and report duplicate slide keys.
                prior = index[key]["metadata_source"]
                errors.append({"dataset": dataset, "path": path, "duplicate_key": key,
                               "error": f"duplicate slide key {key!r}; kept the row from "
                                        f"{prior['path']}, ignored this one"})
                logger.warning("duplicate metadata key %r — keeping the first", key)
                continue
            index[key] = entry

    return index, errors


def index_errors() -> list:
    """Failures and collisions from this thread's last build or lookup."""
    return [dict(error) for error in getattr(_LAST_BUILD, "errors", [])]


def to_source(entry: dict, slide_path: str) -> dict:
    """Convert a standard metadata record into the six-field source block.

    Preserve raw stain spellings for the tissue/fold routers and normalization alias
    lookup. Preserve participant_id verbatim: dataset-specific identifiers can represent
    a slide composite rather than a person, so callers must interpret grouping accordingly.
    """
    record = (entry or {}).get("metadata") or {}
    return {"dataset": (entry or {}).get("metadata_source", {}).get("dataset"),
            "slide_id": slide_id(slide_path),
            "participant_id": record.get("participant_id"),
            "brain_region": record.get("brain_region"),
            "stain_type": record.get("stain_type"),
            "slide_reference": slide_path}


def lookup(slide_path: str, sources=None, enabled: Optional[bool] = None) -> dict:
    """Look up a slide in explicitly supplied metadata sources, or skip when none are given.

    Return result, params, runtime_s, and error. A match contains metadata, extra,
    metadata_source, and source. An absent match returns result=None without rejecting
    the slide; the caller preserves missing fields and reports an unspecified stain.
    """
    started = time.monotonic()
    _LAST_BUILD.errors = []
    # Lookup requires an explicit source path even when enabled=True.
    use = bool(sources) and enabled is not False
    out = {"result": None,
           "params": {"slide_key": slide_key(slide_path), "enabled": use,
                      "skipped_by_request": enabled is False, "skipped": not use,
                      "skip_reason": ("disabled_by_request" if enabled is False else
                                      "no_metadata_path" if not sources else None)},
           "runtime_s": 0.0, "error": None, "error_type": None, "index_errors": []}
    try:
        if not use:
            out["error"] = ("metadata lookup skipped on request (--no_metadata)" if enabled is False
                            else "metadata lookup skipped: no metadata path supplied; use --metadata PATH")
        else:
            index, errors = _load_index(sources)
            _LAST_BUILD.errors = errors
            out["index_errors"] = [dict(error) for error in errors]
            entry = index.get(slide_key(slide_path))
            if entry is None:
                out["error"] = (f"slide {slide_key(slide_path)!r} not present in any supplied "
                                f"metadata source ({len(index)} slides indexed)")
                if out["index_errors"]:
                    # Include indexing failures so a missing match is not mistaken for absent slide metadata.
                    out["error"] += (f"; {len(out['index_errors'])} source problem(s): "
                                     + "; ".join(e["error"] for e in out["index_errors"][:3]))
            else:
                out["result"] = dict(entry, source=to_source(entry, slide_path))
                out["params"].update(dataset=entry["metadata_source"]["dataset"],
                                     metadata_path=entry["metadata_source"]["path"],
                                     n_indexed=len(index))
    except Exception as exc:                                  # noqa: BLE001 - degrade, never raise
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["error_type"] = type(exc).__name__
        logger.warning("metadata lookup failed: %s", out["error"])
    out["runtime_s"] = round(time.monotonic() - started, 4)
    return out


def merge_source(resolved: Optional[dict], slide_path: str, **explicit) -> tuple:
    """Merge metadata with explicit arguments, giving explicit arguments precedence.

    Return (source, resolved_from), with each field’s origin labeled cli, metadata, or none.
    """
    from_metadata = (resolved or {}).get("source") or {}
    source, resolved_from = {}, {}
    for field in SOURCE_FIELDS:
        value = explicit.get(field)
        if value is not None:
            source[field], resolved_from[field] = value, "cli"
        elif from_metadata.get(field) is not None:
            source[field], resolved_from[field] = from_metadata[field], "metadata"
        else:
            source[field], resolved_from[field] = None, "none"
    source["slide_id"] = source["slide_id"] or slide_id(slide_path)
    source["slide_reference"] = source["slide_reference"] or slide_path
    return source, resolved_from


def to_report(resolved: Optional[dict], lookup_result: Optional[dict] = None) -> dict:
    """Build the JSON-safe m1.ingestion.metadata subsection.

    Preserve the standard record, including flat underscore-prefixed export fields,
    to retain the link to the submitted metadata.
    """
    if not resolved:
        params = (lookup_result or {}).get("params") or {}
        skipped = bool(params.get("skipped") or params.get("skipped_by_request"))
        return {"found": False, "skipped": skipped, "skip_reason": params.get("skip_reason"),
                "record": None, "extra": None, "metadata_source": None,
                "standard_columns": len(STANDARD_COLUMNS),
                "runtime_s": (lookup_result or {}).get("runtime_s"),
                "error": (lookup_result or {}).get("error"),
                "error_type": (lookup_result or {}).get("error_type"),
                "index_errors": list((lookup_result or {}).get("index_errors") or []),
                # `found: false` alone conflates "looked and it is not there" with "never looked".
                # `skipped` separates them, so an unresolved slide is never mistaken for a skipped run.
                "note": ("metadata lookup was skipped for this run — the slide was NOT checked "
                         "against any source" if skipped else
                         "no metadata resolved from the supplied files — the pipeline uses "
                         "explicit arguments and nulls")}
    return {"found": True, "skipped": False,
            "record": dict(resolved.get("metadata") or {}),
            "extra": dict(resolved.get("extra") or {}),
            "metadata_source": dict(resolved.get("metadata_source") or {}),
            "standard_columns": len(STANDARD_COLUMNS),
            "runtime_s": (lookup_result or {}).get("runtime_s"),
            "error": None,
            "error_type": None,
            "index_errors": list((lookup_result or {}).get("index_errors") or []),
            "note": ("fields absent from this slide's source file are null BY CONSTRUCTION, not by "
                     "failure — see metadata_source.path for which file answered")}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("slide", help="slide path or URI to match")
    parser.add_argument("--metadata", action="append", required=True, metavar="PATH")
    parser.add_argument("--metadata_key", metavar="COL")
    args = parser.parse_args()
    print(lookup(args.slide, sources=sources_from_paths(args.metadata, key=args.metadata_key)))
