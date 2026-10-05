"""Smoke test — configuration layers reject malformed settings before component imports.

Contracts: config/README.md, config/defaults.json and validation error messages.
No slides, network, model weights or application settings are read or modified.
Run: python src/tests/smoke_config_validation.py
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from pathnd_qc.config.validation import validate  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + str(detail) if detail else ''}")


def layer(path, value):
    """Build a partial override; dotted threshold metric names are supplied as values."""
    result = value
    for key in reversed(path.split(".")):
        result = {key: result}
    return result


def validation_error(value, schema):
    try:
        validate(value, schema)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"UNEXPECTED {type(exc).__name__}: {exc}"
    return None


out = Path(tempfile.mkdtemp(prefix="config_validation_"))
try:
    schema = json.loads((HERE.parent / "pathnd_qc" / "config" / "defaults.json").read_text())
    original = copy.deepcopy(schema)
    print("\n[1] Valid partial layers and inclusive bounds")
    # README: omitted keys inherit defaults, threshold/reference names remain extensible,
    # and null can explicitly disable the documented drop/area policies.
    valid = [
        ("shipped defaults satisfy their own schema", schema),
        ("empty partial overrides are valid", {}),
        ("component selection accepts explicit booleans", layer("components.pen_detection", False)),
        ("partial overrides need not repeat the other component keys", layer("shared.tile_px", 256)),
        ("optional per-component tile and overlay overrides are accepted",
         {"m3": {"tiles": {"tile_px": 256}}, "m2": {"pen": {"alpha_blend": 0.3}, "folds": {"alpha_blend": 0.8}}}),
        ("zero spot checks, zero halo and zero reporting selections are nonnegative counts",
         {"ingestion": {"n_spot_regions": 0}, "m3": {"read": {"halo_out_px": 0}}, "report": {"focus_outlier_n": 0}}),
        ("overlay opacity accepts both physical endpoints",
         {"shared": {"alpha_blend": 0}, "m2": {"tissue": {"alpha_blend": 1}}}),
        ("percentiles accept zero and one hundred",
         {"m4": {"angular_percentile": 100}, "m2": {"folds": {"macenko_alpha": 0, "stain2_sweep_pctl": [0, 100]}}}),
        ("pen core and context accept aligned integer sizes", layer("m2.pen", {"tile_px": 32, "halo_px": 0})),
        ("null disables tile dropping", layer("m3.tile_metrics.drop_below", None)),
        ("null disables the line-fold area floor", layer("m2.folds.fline_min_area_um2", None)),
        ("artifact cutoff accepts null", layer("m3.artifacts.min_fraction", None)),
        ("artifact cutoff accepts a fraction", layer("m3.artifacts.min_fraction", 0.5)),
        ("artifact interpreter accepts null or an explicit path", layer("m3.artifacts.python", "/opt/qc/bin/python")),
        ("null interpreter uses the main Python", layer("m3.artifacts.python", None)),
        ("stain alias names are extensible", layer("m4.stain_aliases", {"new stain": "HE"})),
        ("normalization methods can be restricted", layer("m4.methods", ["reinhard"])),
        ("fold scales accept positive physical lengths", layer("m2.folds.fline_sigmas_um", [1, 2.5])),
        ("fold stain routing accepts explicit names", layer("m2.folds.d_path_stains", ["new stain"])),
        ("RGB overlay accepts integer channel endpoints", layer("m2.pen.color", [0, 128, 255])),
        ("equal plausibility limits are ordered intervals", layer("ingestion", {"mpp_min_plausible": 0.5, "mpp_max_plausible": 0.5})),
        ("empty threshold set and null metric disable comparison", {"thresholds": {"custom.metric": None}}),
        ("thresholds accept null endpoints and extensible per-stain bounds", {"thresholds": {
            "custom.metric": {"min": None, "max": 2.5, "by_stain": {"new stain": {"min": 0, "max": None}, "disabled stain": None}}}}),
        ("equal threshold endpoints are a valid closed interval", {"thresholds": {"custom.metric": {"min": 1, "max": 1}}}),
        ("reference names, bank suffixes and optional identity fields are extensible", {"m4": {"reference": {
            "NEW/bank": {"reference_slide_id": None, "dataset": "cohort", "fitted_at_mpp": 8.0,
                         "is_placeholder": True, "macenko": {"stain_matrix": [[1, 0, 0], [0, 1, 0]], "maxC": [1, 1]},
                         "reinhard": {"means": [50, 0, 0], "stds": [10, 5, 5]}}}}}),
    ]
    for name, value in valid:
        error = validation_error(value, schema)
        check(name, error is None, error or "")

    print("\n[2] Wrong types, numeric traps and unknown keys")
    # README: invalid layers are rejected with a key-specific diagnostic. Booleans are
    # not physical numbers, even though Python permits arithmetic on them.
    invalid = [
        ("root must be a configuration object", [], "must be an object"),
        ("component blocks cannot be scalar", layer("m2.pen", False), "m2.pen"),
        ("unknown top-level options are not silently ignored", {"typo": {}}, "typo"),
        ("unknown nested options are not silently ignored", layer("m2.pen.tile_size", 128), "m2.pen.tile_size"),
        ("component switches reject numeric truthiness", layer("components.pen_detection", 1), "components.pen_detection"),
        ("string fields reject null", layer("m2.pen.device", None), "m2.pen.device"),
        ("optional interpreter rejects numeric values", layer("m3.artifacts.python", 3), "m3.artifacts.python"),
        ("scalar numeric settings reject strings", layer("m2.read.target_mpp", "8"), "m2.read.target_mpp"),
        ("physical scale rejects booleans", layer("m2.read.target_mpp", True), "m2.read.target_mpp"),
        ("physical scale rejects NaN", layer("m2.read.target_mpp", float("nan")), "m2.read.target_mpp"),
        ("physical scale rejects infinity", layer("m2.read.target_mpp", float("inf")), "m2.read.target_mpp"),
        ("unrepresentable numeric settings have an actionable error", layer("m2.read.target_mpp", 10 ** 1000), "m2.read.target_mpp"),
        ("integer counts reject fractional values", layer("shared.tile_px", 32.5), "shared.tile_px"),
        ("positive read scale rejects zero", layer("m2.read.target_mpp", 0), "m2.read.target_mpp"),
        ("nonnegative read tolerance rejects negative values", layer("ingestion.mpp_tolerance_abs", -0.1), "ingestion.mpp_tolerance_abs"),
        ("fractional opacity rejects values over one", layer("shared.alpha_blend", 1.1), "shared.alpha_blend"),
        ("fractional tile cutoff rejects negative values", layer("m3.tile_metrics.drop_below", -0.1), "m3.tile_metrics.drop_below"),
        ("artifact fraction rejects booleans", layer("m3.artifacts.min_fraction", True), "m3.artifacts.min_fraction"),
        ("artifact fraction rejects values over one", layer("m3.artifacts.min_fraction", 1.1), "m3.artifacts.min_fraction"),
        ("percentiles reject values over one hundred", layer("m4.angular_percentile", 101), "m4.angular_percentile"),
        ("quantized fold threshold rejects values above 255", layer("m2.folds.fline_hard_index", 256), "m2.folds.fline_hard_index"),
        ("plausibility interval refuses inverted endpoints", layer("ingestion", {"mpp_min_plausible": 2, "mpp_max_plausible": 1}), "mpp_min_plausible"),
        ("MPP/objective interval refuses inverted endpoints", layer("ingestion", {"mpp_mag_product_min": 20, "mpp_mag_product_max": 10}), "mpp_mag_product_min"),
        ("unsupported tissue methods are refused", layer("m2.tissue.default_method", "invented"), "m2.tissue.default_method"),
        ("unsupported tissue retention policies are refused", layer("m2.tissue.keep", "random"), "m2.tissue.keep"),
        ("unsupported normalization methods are refused", layer("m4.method", "vahadane"), "m4.method"),
        ("pipeline version cannot be overridden", layer("shared.pipeline_version", "999"), "code-owned"),
        ("report version cannot be overridden", layer("report.report_version", "999"), "code-owned"),
        ("retired metadata source mapping directs callers to explicit inputs", layer("ingestion.metadata", {}), "--metadata"),
        ("retired metadata CSV directs callers to explicit inputs", layer("ingestion.metadata_csv", "metadata.csv"), "--metadata"),
    ]
    for name, path, value in [
        ("pen core requires 32-pixel alignment", "m2.pen.tile_px", 33),
        ("pen core rejects zero even though it is aligned", "m2.pen.tile_px", 0),
        ("pen context rejects negative aligned values", "m2.pen.halo_px", -32),
        ("pen sizes reject boolean counts", "m2.pen.tile_px", True),
        ("pen sizes reject integral floats", "m2.pen.halo_px", 32.0),
    ]:
        invalid.append((name, layer(path, value), path))
    for name, value, diagnostic in invalid:
        error = validation_error(value, schema)
        check(name, error is not None and not error.startswith("UNEXPECTED") and diagnostic in error, error or "accepted")

    print("\n[3] Lists, reference shapes and threshold schemas")
    malformed = [
        ("list options reject scalar strings", "m3.artifacts.classes", "pen"),
        ("list elements retain their declared types", "m3.artifacts.classes", [1]),
        ("fold scales require at least one scale", "m2.folds.fline_sigmas_um", []),
        ("fold scales reject zero lengths", "m2.folds.fline_sigmas_um", [0]),
        ("fold scales reject non-finite lengths", "m2.folds.fline_sigmas_um", [float("inf")]),
        ("stain routing rejects blank stain names", "m2.folds.d_path_stains", [" "]),
        ("stain routing rejects numeric stain names", "m2.folds.d_path_stains", [1]),
        ("normalization methods cannot be empty", "m4.methods", []),
        ("normalization method lists reject unsupported methods", "m4.methods", ["macenko", "vahadane"]),
        ("RGB colors need exactly three channels", "m2.pen.color", [0, 255]),
        ("RGB channels reject values above 255", "m2.pen.color", [0, 0, 256]),
        ("RGB channels reject boolean intensities", "m2.pen.color", [0, False, 255]),
        ("threshold ranges need two endpoints", "m2.tissue.thresh_range", [1]),
        ("threshold ranges reject inverted endpoints", "m2.tissue.thresh_range", [4, 1]),
        ("fold percentile ranges stay below one hundred", "m2.folds.stain2_sweep_pctl", [0, 101]),
        ("threshold range endpoints reject NaN", "m2.tissue.thresh_range", [0, float("nan")]),
        ("stain aliases must be mappings", "m4.stain_aliases", []),
        ("stain aliases reject empty targets", "m4.stain_aliases", {"new": ""}),
        ("stain aliases reject non-string targets", "m4.stain_aliases", {"new": 1}),
        ("reference collection must be an object", "m4.reference", []),
        ("reference entries must be objects", "m4.reference", {"NEW": 1}),
        ("reference coefficients reject unknown names", "m4.reference", {"NEW": {"typo": 1}}),
        ("Macenko needs two stain rows", "m4.reference", {"NEW": {"macenko": {"stain_matrix": [[1, 0, 0]]}}}),
        ("Macenko stain rows require RGB dimensionality", "m4.reference", {"NEW": {"macenko": {"stain_matrix": [[1, 0], [0, 1]]}}}),
        ("Macenko concentration scales need two values", "m4.reference", {"NEW": {"macenko": {"maxC": [1]}}}),
        ("Reinhard channel means need three values", "m4.reference", {"NEW": {"reinhard": {"means": [1, 2]}}}),
        ("reference coefficients reject non-finite numbers", "m4.reference", {"NEW": {"reinhard": {"stds": [1, float("nan"), 1]}}}),
        ("threshold collection must map metric names", "thresholds", []),
        ("threshold values must be objects or null", "thresholds", {"custom.metric": 0.5}),
        ("threshold bounds reject misspelled keys", "thresholds", {"custom.metric": {"minimum": 0}}),
        ("threshold bounds reject inverted endpoints", "thresholds", {"custom.metric": {"min": 1, "max": 0}}),
        ("threshold bounds reject booleans", "thresholds", {"custom.metric": {"min": False}}),
        ("threshold bounds reject NaN", "thresholds", {"custom.metric": {"max": float("nan")}}),
        ("per-stain thresholds require a mapping", "thresholds", {"custom.metric": {"by_stain": []}}),
        ("per-stain bounds reject invalid ranges", "thresholds", {"custom.metric": {"by_stain": {"HE": {"min": 5, "max": 1}}}}),
        ("per-stain bounds reject scalar values", "thresholds", {"custom.metric": {"by_stain": {"HE": 0.5}}}),
    ]
    for name, path, value in malformed:
        error = validation_error(layer(path, value), schema)
        check(name, error is not None and not error.startswith("UNEXPECTED") and path in error, error or "accepted")
    check("validation leaves the shipped schema unchanged", schema == original)
finally:
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{'=' * 70}\nCONFIG VALIDATION SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  - {name}")
sys.exit(1 if FAIL else 0)
