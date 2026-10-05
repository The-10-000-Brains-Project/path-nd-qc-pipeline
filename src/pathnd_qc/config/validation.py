"""Validate configuration layers before import-time component defaults can consume them."""
from __future__ import annotations

import math


_INTEGER_KEYS = {
    "shared.tile_px", "ingestion.integrity_max_read_px", "ingestion.n_spot_regions",
    "ingestion.spot_size", "ingestion.localize_retries", "ingestion.thumbnail_max_size",
    "m2.read.max_plane_px", "m2.tissue.disk_radius", "m2.tissue.hist_bins",
    "m2.folds.opening_radius", "m2.folds.contrast_band_px", "m2.folds.fallback_min_area_px",
    "m2.folds.fallback_speck_max_area_px", "m2.folds.stain2_sweep_levels", "m2.pen.pen_class",
    "m2.folds.fline_hard_index", "m2.pen.tile_px", "m2.pen.halo_px",
    "m3.read.halo_out_px", "m3.tiles.tile_px", "m3.tile_metrics.max_mask_bytes",
    "m4.min_tissue_px", "report.focus_outlier_n",
}
_NONNEGATIVE_KEYS = {
    "ingestion.mpp_tolerance_abs", "ingestion.localize_retry_backoff_s",
    "ingestion.localize_progress_every_s", "ingestion.n_spot_regions",
    "m2.read.mpp_tolerance_rel", "m2.tissue.disk_radius", "m2.folds.opening_radius",
    "m2.folds.contrast_band_px", "m2.folds.fallback_min_area_px",
    "m2.folds.fallback_speck_max_area_px", "m2.pen.pen_class", "m2.pen.halo_px", "m3.read.halo_out_px",
    "m2.folds.fline_hard_index", "m2.folds.fline_min_area_um2",
    "m3.tile_metrics.progress_every_s", "report.focus_outlier_n",
}
_POSITIVE_KEYS = {
    "ingestion.target_magnification", "ingestion.mpp_min_plausible", "ingestion.mpp_max_plausible",
    "ingestion.mpp_mag_product_min", "ingestion.mpp_mag_product_max",
    "ingestion.localize_stall_timeout_s", "m2.read.target_mpp", "m2.read.max_read_px", "m3.read.tile_target_mpp",
    "m3.read.chunk_target_mpp", "m3.artifacts.model_mpp", "m4.od_eps", "m4.eps",
    "m2.folds.fline_ball_um", "m2.folds.fline_st_sigma_um", "m2.folds.fline_eps_frac",
} | (_INTEGER_KEYS - _NONNEGATIVE_KEYS)
_FRACTION_KEYS = {
    "shared.alpha_blend", "m2.tissue.alpha_blend", "m2.tissue.min_area_frac",
    "m2.folds.alpha_blend", "m2.folds.speck_min_circularity", "m2.pen.alpha_blend",
    "m3.tile_metrics.chunk_reseg_min", "m3.tile_metrics.drop_below",
    "m3.tile_metrics.alpha_blend", "m3.artifacts.min_fraction",
}
_PERCENTILE_KEYS = {"m2.folds.macenko_alpha", "m4.angular_percentile"}
_ENUMS = {
    "m2.tissue.default_method": {"otsu", "otsu_s", "entropy", "entropy_s", "union", "union_s",
                                 "intersection", "adaptive"},
    "m2.tissue.keep": {"all", "largest"},
    "m4.method": {"macenko", "reinhard"},
}


def _fail(path, message):
    raise ValueError(f"{path}: {message}")


def _number(value, path):
    try:
        finite = math.isfinite(value) if isinstance(value, (int, float)) else False
    except OverflowError:
        finite = False
    if isinstance(value, bool) or not finite:
        _fail(path, "must be a finite number")


def _bounds(value, path):
    if value is None:
        return
    if not isinstance(value, dict):
        _fail(path, "must be an object with min/max and optional by_stain")
    for key, bound in value.items():
        child = f"{path}.{key}"
        if key in {"min", "max"}:
            if bound is not None:
                _number(bound, child)
        elif key == "by_stain":
            if not isinstance(bound, dict):
                _fail(child, "must map stain names to threshold objects")
            for stain, band in bound.items():
                _bounds(band, f"{child}.{stain}")
        else:
            _fail(child, "unknown threshold key")
    low, high = value.get("min"), value.get("max")
    if low is not None and high is not None and low > high:
        _fail(path, "min must be <= max")


def validate(value, schema, path=""):
    """Validate a partial layer or resolved mapping against shipped types and physical bounds.

    Stain names, references and threshold metrics remain extensible.
    Unknown ordinary keys are rejected instead of silently becoming unused configuration.
    """
    if path == "m3.tile_metrics.drop_below" and value is None:
        return  # Explicitly disable the tile-area drop policy.
    if path == "m2.folds.fline_min_area_um2" and value is None:
        return  # Explicitly disable the line-fold area floor.
    if path in {"m2.pen.tile_px", "m2.pen.halo_px"}:
        if path == "m2.pen.tile_px" and value is None:
            return  # Whole-image inference.
        minimum = 32 if path == "m2.pen.tile_px" else 0
        if type(value) is not int or value < minimum or value % 32:
            _fail(path, f"must be an integer multiple of 32 and >= {minimum}")
        return
    if path in {"shared.pipeline_version", "report.report_version"}:
        _fail(path, "software/report identity is code-owned; remove this override")
    if path == "thresholds":
        if not isinstance(value, dict):
            _fail(path, "must map dotted metric names to threshold objects")
        for metric, band in value.items():
            _bounds(band, f"{path}.{metric}")
        return
    if path == "m4.stain_aliases":
        if not isinstance(value, dict) or not all(isinstance(v, str) and v for v in value.values()):
            _fail(path, "must map names to nonempty strings")
        return
    if path == "m4.reference":
        if not isinstance(value, dict):
            _fail(path, "must map stain or stain/bank names to reference objects")
        reference = next(iter(schema.values()), {})
        reference = dict(reference, reference_slide_id=None, dataset=None)
        for name, item in value.items():
            validate(item, reference, f"{path}.{name}")
        return
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            _fail(path, "must be an object")
        # Component-specific fallbacks are optional, so overriding shared defaults still works.
        optional = {"m2.folds": {"alpha_blend": 0.45}, "m2.pen": {"alpha_blend": 0.45},
                    "m3.tiles": {"tile_px": 512}}
        known = dict(schema, **optional.get(path, {}))
        for key, item in value.items():
            child = f"{path}.{key}" if path else key
            if child in {"shared.pipeline_version", "report.report_version"}:
                _fail(child, "software/report identity is code-owned; remove this override")
            if child in {"ingestion.metadata", "ingestion.metadata_csv"}:
                _fail(child, "automatic metadata sources were removed; use --metadata PATH or "
                            "run(metadata_paths=[...]); omit metadata to skip lookup")
            if key not in known:
                _fail(child, "unknown configuration key")
            validate(item, known[key], child)
        for low, high in (("mpp_min_plausible", "mpp_max_plausible"),
                          ("mpp_mag_product_min", "mpp_mag_product_max")):
            if low in value and high in value and value[low] > value[high]:
                _fail(path, f"{low} must be <= {high}")
        return
    if isinstance(schema, list):
        if not isinstance(value, list):
            _fail(path, "must be a list")
        if path == "m2.folds.fline_sigmas_um":
            if not value:
                _fail(path, "must contain at least one positive physical scale")
            for item in value:
                _number(item, path)
                if item <= 0:
                    _fail(path, "scales must be positive")
            return
        if path == "m2.folds.d_path_stains":
            if any(not isinstance(item, str) or not item.strip() for item in value):
                _fail(path, "must contain nonempty stain names")
            return
        if path == "m4.methods" and (not value or any(v not in ("macenko", "reinhard") for v in value)):
            _fail(path, "must list supported methods: macenko, reinhard")
        if path.endswith(".color"):
            if len(value) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in value):
                _fail(path, "must contain three integer RGB values in [0,255]")
        elif path.endswith((".thresh_range", ".stain2_sweep_pctl")):
            if len(value) != 2:
                _fail(path, "must contain [lower, upper]")
            for item in value:
                _number(item, path)
            if value[0] > value[1]:
                _fail(path, "lower must be <= upper")
            if path.endswith(".stain2_sweep_pctl") and not (0 <= value[0] <= value[1] <= 100):
                _fail(path, "percentiles must lie in [0,100]")
        else:
            template = schema[0] if schema else ""
            if path.startswith("m4.reference.") and len(value) != len(schema):
                _fail(path, f"must contain {len(schema)} values")
            for i, item in enumerate(value):
                validate(item, template, f"{path}[{i}]")
        return
    if schema is None:
        if value is None:
            return
        if path == "m3.artifacts.min_fraction":
            _number(value, path)
            if not 0 <= value <= 1:
                _fail(path, "must lie in [0,1]")
        elif not isinstance(value, str):
            _fail(path, "must be a string or null")
        return
    if isinstance(schema, bool):
        if not isinstance(value, bool):
            _fail(path, "must be true or false")
        return
    if isinstance(schema, str):
        if not isinstance(value, str):
            _fail(path, "must be a string")
        if path in _ENUMS and value not in _ENUMS[path]:
            _fail(path, f"must be one of {sorted(_ENUMS[path])}")
        return
    _number(value, path)
    if path in _INTEGER_KEYS and type(value) is not int:
        _fail(path, "must be an integer")
    if path in _POSITIVE_KEYS and value <= 0:
        _fail(path, "must be positive")
    if path in _NONNEGATIVE_KEYS and value < 0:
        _fail(path, "must be non-negative")
    if path in _FRACTION_KEYS and not 0 <= value <= 1:
        _fail(path, "must lie in [0,1]")
    if path in _PERCENTILE_KEYS and not 0 <= value <= 100:
        _fail(path, "must lie in [0,100]")
    if path == "m2.folds.fline_hard_index" and not 0 <= value <= 255:
        _fail(path, "must lie in [0,255]")
