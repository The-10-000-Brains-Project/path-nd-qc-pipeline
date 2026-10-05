"""Standalone dataset adapters for the six-field ingestion source schema.

adapt maps a raw row to dataset, slide_id, participant_id, brain_region, stain_type,
and slide_reference. The caller supplies the dataset name explicitly. Add adapters
by registering adapt_<name> functions in ADAPTERS.

The pipeline uses metadata lookup and does not call these adapters. adapt_part
expects uppercase PART column names; lowercase exports require a different mapping.
No QSBB adapter or artifact_type field is provided by this schema.
"""

from __future__ import annotations

import pandas as pd
from pathnd_qc._logging import get_logger

logger = get_logger(__name__)

# Remap PART Windows path tails onto this placeholder GCS prefix.
# Callers must configure a usable destination before relying on adapted paths.
DEFAULT_PART_GCS_PREFIX = "gs://pathnd/part/slides"
PART_WIN_ROOT = r"W:\Collection_PART_DATA_MINERVA"


def _clean(v):
    """Normalize a raw cell: NaN/empty → None; unwrap numpy scalars to Python types."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass  # non-scalar (e.g. list) — leave as-is
    if hasattr(v, "item"):
        try:
            v = v.item()
        except Exception as exc:  # noqa: BLE001 -- preserve unsupported cells, disclose failed coercion
            logger.warning("Could not unwrap metadata scalar (%s): %s", type(v).__name__, exc)
    return v


def _part_path_to_gcs(win_path, gcs_prefix: str):
    """Remap a PART Windows SLIDE_PATHS value onto the assumed GCS prefix."""
    if not isinstance(win_path, str) or not win_path:
        return None
    p = win_path.replace("\\", "/")
    root = PART_WIN_ROOT.replace("\\", "/")
    if p.startswith(root):
        tail = p[len(root):].lstrip("/")
    elif ":/" in p:  # fallback: drop drive letter
        tail = p.split(":/", 1)[-1].lstrip("/")
    else:
        tail = p.lstrip("/")
    return f"{gcs_prefix.rstrip('/')}/{tail}"


def adapt_seaad(row: pd.Series, **_) -> dict:
    """SEA-AD metadata row → normalized source block."""
    return {
        "dataset": "SEA-AD",
        "slide_id": _clean(row.get("instance_id")),
        "participant_id": _clean(row.get("participant_id")),
        "brain_region": _clean(row.get("brain_region")),
        "stain_type": _clean(row.get("stain_type")),  # raw
        "slide_reference": _clean(row.get("slide_paths")),  # already gs://
    }


def adapt_part(row: pd.Series, gcs_prefix: str = DEFAULT_PART_GCS_PREFIX, **_) -> dict:
    """PART metadata row → normalized source block.

    Region = BLOCK_IDS verbatim (Data_Dictionary defines it as the neuroanatomical
    region); stain = STAINS verbatim; slide_reference = SLIDE_PATHS remapped to GCS.
    """
    return {
        "dataset": "PART",
        "slide_id": _clean(row.get("SLIDE_IDS")),
        "participant_id": _clean(row.get("PWG_ID")),
        "brain_region": _clean(row.get("BLOCK_IDS")),  # raw code, e.g. "F"/"H"
        "stain_type": _clean(row.get("STAINS")),        # raw, e.g. "LFB/H&E"
        "slide_reference": _part_path_to_gcs(row.get("SLIDE_PATHS"), gcs_prefix),
    }


ADAPTERS = {
    "SEA-AD": adapt_seaad,
    "PART": adapt_part,
}


def adapt(dataset: str, row: pd.Series, **kwargs) -> dict:
    """Dispatch to the adapter for `dataset`. Raises on an unknown dataset."""
    if dataset not in ADAPTERS:
        raise ValueError(f"unknown dataset {dataset!r}; known: {list(ADAPTERS)}")
    return ADAPTERS[dataset](row, **kwargs)
