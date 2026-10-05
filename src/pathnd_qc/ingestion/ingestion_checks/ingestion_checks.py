"""Check acquisition scale and sampled integrity for local, GCS, S3 and Azure slides.

Uses GCSWSIReader for all supported providers. Magnification is recorded; effective
MPP governs the tile-resolution gate. Integrity checks sample bounded regions.
Records include checks, source metadata, and software provenance, without BDSA write-back.

decide records recommendations unless enforce=True and a policy is active. The pipeline
uses scale plausibility to decide whether image and tile work can run. These checks
do not establish diagnostic or model accuracy.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Optional

import pandas as pd
from tiffslide import TiffSlide

from pathnd_qc._logging import get_logger
from pathnd_qc import __version__
from pathnd_qc.config.config import cfg, now_stamps

from ..adapters.adapters import DEFAULT_PART_GCS_PREFIX, adapt
from ..wsi_reader.wsi_reader import GCSWSIReader

logger = get_logger(__name__)

# Use the package version to identify the implementation in ingestion records.
LIBRARY_VERSION = __version__

# Policy defaults (config-backed; explicit call-site arguments still win).
DEFAULT_TARGET_MAGNIFICATION = cfg("ingestion.target_magnification", 20)
# Use the tile-plane target for M1’s MPP gate so the bound and analysis resolution agree.
DEFAULT_TARGET_MPP = cfg("m3.read.tile_target_mpp", 0.5)
DEFAULT_MPP_TOL = cfg("ingestion.mpp_tolerance_abs", 0.05)
# Below ~0.2 um/px is the diffraction limit of light microscopy; the finest scanner in the corpus
# is 0.2305 (QSBB 40x). Anything under 0.1 is a unit error, not a resolution.
MIN_PLAUSIBLE_MPP = cfg("ingestion.mpp_min_plausible", 0.1)
# The upper plausibility bound rejects scale metadata outside the supported acquisition range.
MAX_PLAUSIBLE_MPP = cfg("ingestion.mpp_max_plausible", 10.0)
# An MPP/objective product within the configured range can confirm a sub-floor MPP.
# Record out-of-range pairs as observations; plausible stated spacing takes precedence.
MPP_MAG_PRODUCT_MIN = cfg("ingestion.mpp_mag_product_min", 5.0)
MPP_MAG_PRODUCT_MAX = cfg("ingestion.mpp_mag_product_max", 15.0)
# Cap the smallest-level decode so single-level slides cannot force an unbounded integrity read.
INTEGRITY_MAX_READ_PX = cfg("ingestion.integrity_max_read_px", 2048 * 2048)
DEFAULT_N_SPOT_REGIONS = cfg("ingestion.n_spot_regions", 3)
DEFAULT_SPOT_SIZE = cfg("ingestion.spot_size", 256)

# Library defaults never write into the package directory.
DEFAULT_OUT_DIR = Path("reports") / "ingestion"


# --------------------------------------------------------------------- policy
def check_magnification(info: dict,
                        target_objective_power: int = DEFAULT_TARGET_MAGNIFICATION) -> dict:
    """Pass when objective power meets or exceeds the target, normally 20x.

    Higher magnifications are accepted because the reader can downsample to the target.
    """
    observed = info.get("objective_power")
    numeric = _num(observed)
    passed = numeric is not None and numeric >= target_objective_power
    return {
        "check": "magnification",
        "passed": bool(passed),
        "observed": observed,
        "expected": target_objective_power,
        "reason": (
            f"objective_power={observed} >= {target_objective_power}"
            if passed
            else f"objective_power={observed} < target {target_objective_power}"
        ),
    }


def _num(v):
    """A finite float, or None."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _plausible(v) -> bool:
    return v is not None and MIN_PLAUSIBLE_MPP <= v <= MAX_PLAUSIBLE_MPP


def check_mpp(info: dict, target_mpp: float = DEFAULT_TARGET_MPP,
              tol: float = DEFAULT_MPP_TOL,
              mag_product_min: float = MPP_MAG_PRODUCT_MIN,
              mag_product_max: float = MPP_MAG_PRODUCT_MAX) -> dict:
    """Resolve effective level-0 MPP and check it against target + tol.

    Use stated mpp_x/mpp_y when both lie within the plausibility window, or when a
    sub-floor value is confirmed by the configured MPP/objective product range.
    Otherwise try 10/objective_power within the same plausibility window. If neither
    is usable, set implausible and leave effective_mpp unavailable. A plausible stated
    scale takes precedence over an objective-derived estimate.

    Defaults are target=0.5 and tol=0.05 microns per pixel. Finer scans pass; coarser
    scans fail the tile gate but can still support lower-resolution analysis.
    The result records effective_mpp and scale_source for apply_scale. Both axes are
    checked; downstream resolution uses mpp_x, with mpp_y serving as a consistency check.
    """
    mx, my, op = info.get("mpp_x"), info.get("mpp_y"), info.get("objective_power")
    sx, sy, opf = _num(mx), _num(my), _num(op)
    stated = sx if (sx is not None and sy is not None) else None
    derived = round(10.0 / opf, 6) if (opf is not None and opf > 0) else None
    product = round(sx * opf, 4) if (sx is not None and opf is not None) else None
    window = f"[{MIN_PLAUSIBLE_MPP}, {MAX_PLAUSIBLE_MPP}] um/px"
    effective = scale_source = None
    stated_note = None
    observations = []
    if sx is not None and sy is not None and sx > 0 and sy > 0 and not math.isclose(
            sx, sy, rel_tol=1e-6, abs_tol=1e-9):
        observations.append({"code": "mpp_axis_disagreement",
                             "message": f"stated mpp_x={sx} differs from mpp_y={sy}; "
                                        "the pipeline assumes square pixels and uses one scale"})
    if product is not None and opf is not None and opf > 0 and sx > 0 and not (
            mag_product_min <= product <= mag_product_max):
        observations.append({"code": "scale_disagreement",
                             "message": f"stated mpp_x x objective_power = {product} is outside "
                                        f"[{mag_product_min}, {mag_product_max}]; "
                                        "this heuristic does not override a plausible stated scale"})
    if stated is not None and _plausible(sx) and _plausible(sy):
        effective, scale_source = stated, "stated"
    elif (stated is not None and product is not None and opf is not None
            and 0 < sx < MIN_PLAUSIBLE_MPP and 0 < sy < MIN_PLAUSIBLE_MPP
            and mag_product_min <= product <= mag_product_max
            and mag_product_min <= sy * opf <= mag_product_max):
        effective = stated
        scale_source = (f"stated (below the plausible minimum {MIN_PLAUSIBLE_MPP}, but confirmed by "
                        f"objective_power {op}: mpp_x x objective = {product} lies in "
                        f"[{mag_product_min}, {mag_product_max}])")
    else:
        if sx is None and sy is None:
            stated_note = "mpp not provided"
        elif stated is None:
            stated_note = f"mpp ({mx},{my}) not numeric"
        else:
            stated_note = f"stated mpp ({mx},{my}) rejected as implausible (outside {window}"
            if product is not None and not mag_product_min <= product <= mag_product_max:
                stated_note += (f"; mpp_x x objective_power {op} = {product} is outside "
                                f"[{mag_product_min}, {mag_product_max}]")
            stated_note += ")"
        if _plausible(derived):
            effective = derived
            scale_source = (f"derived from objective_power {op} (10 / {op} = {derived} um/px); "
                            f"{stated_note}")
    implausible = effective is None
    passed = (not implausible) and effective <= target_mpp + tol
    if passed:
        reason = (f"effective mpp {effective} <= {target_mpp}+{tol} (no coarser than target); "
                  f"scale: {scale_source}")
    elif implausible:
        reason = f"no usable scale: {stated_note}"
        if derived is not None:
            reason += f"; objective_power {op} gives {derived} um/px, outside {window}"
        else:
            reason += "; no objective_power to derive one from"
    else:
        reason = (f"effective mpp {effective} coarser than {target_mpp}+{tol}; "
                  f"scale: {scale_source}")
    return {
        "check": "mpp",
        "passed": bool(passed),
        "implausible": bool(implausible),
        "effective_mpp": effective,
        "scale_source": scale_source,
        "observations": observations,
        "observed": {"mpp_x": mx, "mpp_y": my, "objective_power": op,
                     "mpp_x_objective_product": product, "derived_mpp": derived},
        "expected": {"target_mpp": target_mpp, "tol": tol, "min_plausible": MIN_PLAUSIBLE_MPP,
                     "max_plausible": MAX_PLAUSIBLE_MPP,
                     "mag_product_window": [mag_product_min, mag_product_max],
                     "bound": "effective mpp <= target_mpp + tol, where effective = stated mpp if "
                              "plausible (or sub-floor but mpp_x * objective_power inside "
                              "mag_product_window), else 10 / objective_power if plausible"},
        "reason": reason,
    }


def apply_scale(info: dict, mpp_check: dict) -> dict:
    """Copy slide metadata and apply the effective scale selected by check_mpp.

    For objective-derived scale, replace mpp_x/mpp_y and retain the stated values in
    mpp_stated_x/y. mpp_source identifies the decision. Acquisition records retain raw info.
    """
    out = dict(info)
    src = mpp_check.get("scale_source") or None
    eff = mpp_check.get("effective_mpp")
    out["mpp_source"] = src
    if eff is not None and str(src).startswith("derived"):
        out["mpp_stated_x"], out["mpp_stated_y"] = info.get("mpp_x"), info.get("mpp_y")
        out["mpp_x"] = out["mpp_y"] = eff
    return out


# ------------------------------------------------------------------ integrity
def check_integrity(slide: TiffSlide, info: dict,
                    n_spot_regions: int = DEFAULT_N_SPOT_REGIONS,
                    spot_size: int = DEFAULT_SPOT_SIZE) -> dict:
    """Check pyramid structure and sample decodability with bounded region reads.

    Require at least one level, matching level-0 dimensions, and increasing downsamples.
    Read the smallest level when it fits the cap, plus sampled base-level regions.
    Corruption outside sampled regions can remain undetected. Tile read failures and
    localization transfer verification provide separate integrity signals.
    """
    problems: list[str] = []

    # --- structure ---
    lc = info.get("level_count", 0)
    if not lc or lc < 1:
        problems.append(f"level_count={lc}")
    dims = info.get("dimensions")
    ldims = info.get("level_dimensions") or ()
    if not ldims or tuple(ldims[0]) != tuple(dims):
        problems.append(f"base level_dimensions {ldims[:1]} != dimensions {dims}")
    ds = info.get("level_downsamples") or ()
    if list(ds) != sorted(ds) or len(set(ds)) != len(ds):
        problems.append(f"non-monotonic downsamples {ds}")

    # --- decodability spot-check ---
    try:
        best = lc - 1
        bw, bh = slide.level_dimensions[best]
        if bw * bh <= INTEGRITY_MAX_READ_PX:
            slide.read_region((0, 0), best, (bw, bh))             # top of pyramid, whole
        else:
            # Top level too big to decode whole: a bounded centre window of it instead.
            side = int(INTEGRITY_MAX_READ_PX ** 0.5)
            cw, ch = min(side, bw), min(side, bh)
            ds = float(slide.level_downsamples[best])
            slide.read_region((int(((bw - cw) // 2) * ds), int(((bh - ch) // 2) * ds)), best, (cw, ch))
        W, H = dims
        size = min(spot_size, W, H)
        locs = [(0, 0)]
        if W > size and H > size:
            locs.append((W - size, H - size))
            locs.append(((W - size) // 2, (H - size) // 2))
        for loc in locs[:n_spot_regions]:
            slide.read_region(loc, 0, (size, size))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"decode failure: {exc}")

    passed = not problems
    return {
        "check": "integrity",
        "passed": passed,
        "observed": {"level_count": lc, "dimensions": dims,
                     "n_spot_regions": n_spot_regions},
        "reason": "structure + spot-decode OK" if passed else "; ".join(problems),
    }


# --------------------------------------------------------------------- record
def build_ingestion_record(source: dict, info: dict, checks: dict,
                           metadata: Optional[dict] = None,
                           source_resolved_from: Optional[dict] = None) -> dict:
    """Assemble the versioned, structured ingestion record for one slide.

    `source` is the six-field dataset-agnostic block the pipeline routes on.
    `metadata` is the **standard 35-column record** as read (M1 Step 6), kept BESIDE `source` rather
    than replacing it: `metadata` is what the file says, `source` is the derived view that actually
    drove routing, and `source_resolved_from` names the origin of each field (`cli`/`metadata`/`none`)
    so a routing decision is always distinguishable from a raw field.
    """
    _stamps = now_stamps()
    return {
        "source": source,
        "source_resolved_from": source_resolved_from or {},
        "metadata": metadata,
        "acquisition": {
            "vendor": info.get("vendor"),
            "scanner_id": info.get("scanner_id"),
            "objective_power": info.get("objective_power"),
            "mpp_x": info.get("mpp_x"),
            "mpp_y": info.get("mpp_y"),
            # Record the effective scale alongside the raw acquisition fields.
            "mpp_effective": (checks.get("mpp") or {}).get("effective_mpp"),
            "mpp_source": (checks.get("mpp") or {}).get("scale_source"),
            "dimensions": info.get("dimensions"),
            "level_count": info.get("level_count"),
        },
        "checks": checks,
        "integrity_passed": checks.get("integrity", {}).get("passed"),
        "library_version": LIBRARY_VERSION,
        "ingested_at": _stamps[0],
        "ingested_at_local": _stamps[1],
    }


def decide(record: dict, enforce: bool = False,
           active_policy: Optional[str] = None) -> dict:
    """Pass / quarantine decision.

    Hard rule: an integrity failure always quarantines. Policy (magnification/MPP)
    failures are only quarantine-worthy when enforce=True AND an active_policy is named
    (the one the neuropathologist selected). Otherwise they are recorded as flags.
    Returns {status, quarantine_reason, flags}.
    """
    checks = record.get("checks", {})
    flags = [name for name, c in checks.items() if not c.get("passed")]

    if not checks.get("integrity", {}).get("passed", False):
        return {"status": "quarantine", "flags": flags,
                "quarantine_reason": f"integrity: {checks['integrity']['reason']}"}

    if enforce and active_policy:
        pc = checks.get(active_policy)
        if pc is not None and not pc.get("passed"):
            return {"status": "quarantine", "flags": flags,
                    "quarantine_reason": f"{active_policy}: {pc['reason']}"}

    return {"status": "pass", "flags": flags, "quarantine_reason": None}


def write_record(record: dict, out_dir: Path = DEFAULT_OUT_DIR) -> Path:
    """Write one JSON record per slide into out_dir (created if needed)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = record.get("source", {})
    stem = src.get("slide_id") or Path(str(src.get("slide_reference", "slide"))).stem
    path = out_dir / f"{stem}.json"
    with open(path, "w") as f:
        json.dump(record, f, indent=2, default=str)
    return path


# ---------------------------------------------------------------- orchestrate
def ingest_slide(reader: GCSWSIReader, row: pd.Series, dataset: str,
                 policy_targets: Optional[dict] = None,
                 out_dir: Path = DEFAULT_OUT_DIR,
                 enforce: bool = False,
                 active_policy: Optional[str] = None,
                 gcs_prefix: str = DEFAULT_PART_GCS_PREFIX,
                 read_path: Optional[str] = None) -> Optional[dict]:
    """Run adapter + checks + record + decision + JSON write for one metadata row.

    `dataset` selects the adapter (e.g. "SEA-AD", "PART"). The record's canonical
    `source.slide_reference` comes from the adapter; the slide is opened from
    `read_path` if given (e.g. a local demo file), else from that reference.
    policy_targets keys: target_objective_power, target_mpp, tol.
    Returns the written record (with a 'decision' block).
    """
    pt = policy_targets or {}
    source = adapt(dataset, row, gcs_prefix=gcs_prefix)
    open_path = read_path or source.get("slide_reference")
    slide = reader.open_slide(open_path) if open_path else None

    if slide is None:
        record = build_ingestion_record(
            source, {},
            {"integrity": {"check": "integrity", "passed": False,
                           "observed": None, "reason": "slide could not be opened"}},
        )
        record["decision"] = {"status": "quarantine", "flags": ["integrity"],
                              "quarantine_reason": "slide could not be opened"}
        write_record(record, out_dir)
        logger.warning("QUARANTINE (unopenable): %s", open_path)
        return record

    try:
        info = reader.get_slide_info(slide)
        checks = {
            "magnification": check_magnification(
                info, pt.get("target_objective_power", 20)),
            "mpp": check_mpp(info, pt.get("target_mpp", 0.5), pt.get("tol", 0.05)),
            "integrity": check_integrity(slide, info),
        }
        record = build_ingestion_record(source, info, checks)
        record["decision"] = decide(record, enforce=enforce, active_policy=active_policy)
        write_record(record, out_dir)
        logger.info("%s %s (flags=%s)", record["decision"]["status"].upper(),
                    source.get("slide_id"), record["decision"]["flags"])
        return record
    finally:
        slide.close()


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Run ingestion checks on three supplied metadata rows")
    parser.add_argument("--metadata", required=True, metavar="PATH")
    parser.add_argument("--dataset", default=None)
    args = parser.parse_args()
    reader = GCSWSIReader()
    df = reader.load_metadata(args.metadata)
    if df is not None:
        sample = df.head(3)  # small live sample
        for _, r in sample.iterrows():
            rec = ingest_slide(reader, r, dataset=args.dataset)
            d = rec["decision"]
            s = rec["source"]
            print(f"{s.get('slide_id')} [{s.get('stain_type')}] -> "
                  f"{d['status']} flags={d['flags']}")
        print(f"records written to {DEFAULT_OUT_DIR}")


def to_report(record: dict) -> dict:
    """Convert acquisition, source, checks, and decision into the report’s ingestion subsection."""
    return {"source": dict(record.get("source") or {}),
            "source_resolved_from": dict(record.get("source_resolved_from") or {}),
            "metadata": record.get("metadata"),
            "acquisition": dict(record.get("acquisition") or {}),
            "checks": {k: {"passed": v.get("passed"), "observed": v.get("observed"),
                           "expected": v.get("expected"), "reason": v.get("reason"),
                           **{f: v[f] for f in ("implausible", "effective_mpp", "scale_source")
                              if f in v}}
                       for k, v in (record.get("checks") or {}).items()},
            "decision": dict(record.get("decision") or {}),
            "integrity_passed": record.get("integrity_passed"),
            "library_version": record.get("library_version"),
            "ingested_at": record.get("ingested_at"),
            "ingested_at_local": record.get("ingested_at_local")}
