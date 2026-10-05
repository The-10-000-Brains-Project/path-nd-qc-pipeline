"""Registry of selectable components, artifact requirements and output descriptions.

The five ARTIFACTS entries are supplyable inputs: producers or a file can satisfy them.
Generated-only files are listed separately in SIDE_OUTPUTS. The CLI, planner and report share
these declarations. Argument/dependency checks run before localization; geometry, file decoding
and backend checks also occur elsewhere. This registry does not establish scientific validity.
"""

from __future__ import annotations

from typing import Optional

from pathnd_qc.config.config import cfg
from pathnd_qc.output_layout import ARTIFACT_FILES

# Artifact scales use the same configuration keys as component reads, so supplied
# images and computed images share the declared MPP.
M2_MPP = float(cfg("m2.read.target_mpp", 8.0))
M3_MPP = float(cfg("m3.read.tile_target_mpp", 0.5))

# --------------------------------------------------------------------------- artifacts
# `produced_by` is a COMPONENT KEY or None for the root read. `formats` are what a SUPPLIED file may
# be. The 20x tissue mask is the only one that also writes a tiled TIFF -- everything else is small
# enough that PNG is both smaller and inspectable.
ARTIFACTS = {
    "thumbnail": {
        "flag": "--thumbnail", "kind": "image", "produced_by": None,
        "formats": (".png", ".npy"), "mpp": M2_MPP,
        "help": f"the {M2_MPP} um/px analysis image every M2/M4 component consumes",
    },
    "pen_mask": {
        "flag": "--pen_mask", "kind": "mask", "produced_by": "pen_detection",
        "formats": (".png", ".npy"), "mpp": M2_MPP,
        "help": f"pen-ink mask at {M2_MPP} um/px",
    },
    "tissue_mask": {
        "flag": "--tissue_mask", "kind": "mask", "produced_by": "tissue_segmentation",
        "formats": (".png", ".npy"), "mpp": M2_MPP,
        "help": f"tissue mask at {M2_MPP} um/px",
    },
    "fold_mask": {
        "flag": "--fold_mask", "kind": "mask", "produced_by": "fold_detection",
        "formats": (".png", ".npy"), "mpp": M2_MPP,
        "help": f"tissue-fold mask at {M2_MPP} um/px",
    },
    "tile_list": {
        "flag": "--tile_list", "kind": "json", "produced_by": "tile_selection",
        "formats": (".json",), "mpp": M3_MPP,
        "help": f"selected tile geometry (col/row/x/y/w/h) on the {M3_MPP} um/px plane",
    },
}
# Per-tile measurement records are outputs, not supplyable component inputs.

# requires names mandatory artifacts; missing entries prevent execution.
# optional names artifacts that affect results when available; missing entries
# are recorded as degraded support. needs_slide identifies components needing
# a slide handle in addition to their artifact inputs.
COMPONENTS = {
    "tissue_segmentation": {
        "flag": "--run_tissue_segmentation", "stage": "m2", "file": "tissue.py",
        "requires": ("thumbnail",), "optional": (), "produces": ("tissue_mask",),
        "needs_slide": False, "report_key": "tissue",
    },
    "fold_detection": {
        "flag": "--run_fold_detection", "stage": "m2", "file": "folds.py",
        "requires": ("thumbnail", "tissue_mask"), "optional": (),
        "supplied_optional": ("pen_mask",),  # only a supplied pen is an exclusion input for folds
        "produces": ("fold_mask",), "needs_slide": False, "report_key": "folds",
    },
    "pen_detection": {
        "flag": "--run_pen_detection", "stage": "m2", "file": "pen.py",
        "requires": ("thumbnail",), "optional": ("fold_mask",), "produces": ("pen_mask",),
        "needs_slide": False, "report_key": "pen",
    },
    "staining_quality": {
        "flag": "--run_staining_quality", "stage": "m2", "file": "staining.py",
        "requires": ("thumbnail", "tissue_mask"), "optional": ("fold_mask", "pen_mask"),
        "produces": (), "needs_slide": False, "report_key": "staining",
    },
    "focus": {
        "flag": "--run_focus", "stage": "m2", "file": "focus.py",
        "requires": ("thumbnail", "tissue_mask"), "optional": ("fold_mask", "pen_mask"),
        "produces": (), "needs_slide": False, "report_key": "focus",
    },
    "stain_normalization": {
        "flag": "--run_stain_normalization", "stage": "m4", "file": "stain_norm.py",
        "requires": ("thumbnail", "tissue_mask"), "optional": (),
        "produces": (), "needs_slide": False, "report_key": "stain_norm",
    },
    "tile_selection": {
        "flag": "--run_tile_selection", "stage": "m3", "file": "tiles.py",
        "requires": ("tissue_mask",), "optional": (), "produces": ("tile_list",),
        # Tile selection reads no pixels but needs the slide header to resolve its analysis plane.
        "needs_slide": False, "needs_info": True, "report_key": "tile_selection",
    },
    "tile_metrics": {
        "flag": "--run_tile_metrics", "stage": "m3", "file": "tile_metrics.py",
        "requires": ("tissue_mask", "tile_list"), "optional": (),
        "produces": (), "needs_slide": True, "report_key": "tiles",
    },
    "tile_artifacts": {
        "flag": "--run_tile_artifacts", "stage": "m3", "file": "artifacts.py",
        "requires": ("tissue_mask", "tile_list"), "optional": (),
        "produces": (), "needs_slide": True, "report_key": "artifacts",
    },
}

# Files written for reporting or inspection, separate from artifacts consumed by components.
SIDE_OUTPUTS = {
    "tissue_segmentation": (("tissue_mask", ARTIFACT_FILES["tissue_mask"].format(slide_id="<slide_id>"),
                             f"tissue mask at {M2_MPP} um/px, 1-bit PNG"),),
    "fold_detection":      (("fold_mask", ARTIFACT_FILES["fold_mask"].format(slide_id="<slide_id>"),
                             f"fold mask at {M2_MPP} um/px, 1-bit PNG"),),
    "pen_detection":       (("pen_mask", ARTIFACT_FILES["pen_mask"].format(slide_id="<slide_id>"),
                             f"pen mask at {M2_MPP} um/px, 1-bit PNG"),),
    "tile_selection":      (("tile_list", ARTIFACT_FILES["tile_list"].format(slide_id="<slide_id>"),
                             f"selected tile geometry on the {M3_MPP} um/px plane, as "
                             f"{{plane_mpp, plane_dims, tiles}} so a supplied list declares its plane"),),
    "tile_metrics":        (("tissue_mask_20x", ARTIFACT_FILES["tissue_mask_20x"].format(slide_id="<slide_id>"),
                             f"FULL-RESOLUTION tissue mask at {M3_MPP} um/px — tiled 1-bit LZW TIFF "
                             "with a 4-level pyramid; file size depends on mask content"),
                            ("tile_records", ARTIFACT_FILES["tile_records"].format(slide_id="<slide_id>"),
                             "per-tile fraction/focus/kept for every selected tile"),
                            ("blur_overlay", ARTIFACT_FILES["blur_overlay"].format(slide_id="<slide_id>"),
                             "PER-TILE FOCUS HEATMAP — relative Laplacian variance, green=high to red=low, "
                             f"alpha-blended over the {M2_MPP} um/px thumbnail; tiles with focus=None "
                             "are left unpainted.")),
    "stain_normalization": (("normalized_image", ARTIFACT_FILES["normalized_image"].format(slide_id="<slide_id>"),
                             f"the normalized {M2_MPP} um/px image; background byte-identical to the "
                             "input"),),
    "tile_artifacts":      (("artifact_overlay", ARTIFACT_FILES["artifact_overlay"].format(slide_id="<slide_id>"),
                             f"GrandQC class map over the {M2_MPP} um/px image, tinting only the "
                             "consumed classes (m3.artifacts.classes); tissue and background stay "
                             "untinted"),),
}

# Module aliases group their component flags. Stain normalization has its own M4 flag.
ALIASES = {
    "--run_thumbnail": ("pen_detection", "tissue_segmentation", "fold_detection",
                        "staining_quality", "focus"),
    "--run_tiles": ("tile_selection", "tile_metrics", "tile_artifacts"),
}

# The thumbnail has no producing COMPONENT -- it comes from the reader. It is still an artifact, so it
# is still supplyable; this names the pseudo-producer for error messages.
THUMBNAIL_PRODUCER = "the slide read (automatic)"


def flag_to_component(flag: str) -> Optional[str]:
    for key, spec in COMPONENTS.items():
        if spec["flag"] == flag:
            return key
    return None


def select_run_set(requested: set | None = None) -> set:
    """Apply the same config switch to every requested component; None requests all.

    A false `components.<name>` excludes that component even when explicitly requested.
    This never adds dependencies or re-enables a disabled producer. An empty result is
    left for plan_run to reject before any slide I/O.
    """
    chosen = set(COMPONENTS) if requested is None else set(requested)
    unknown = chosen - set(COMPONENTS)
    if unknown:
        raise ValueError(f"unknown component(s) {sorted(unknown)}; valid: {sorted(COMPONENTS)}")
    return {name for name in chosen if cfg(f"components.{name}", True)}


def default_run_set() -> set:
    """Select every component enabled in configuration (all nine in shipped defaults)."""
    return select_run_set()


def resolve_run_set(selected: set, aliases_given: set) -> set:
    """Resolve component flags and module aliases into a selected set.

    With no flags, select all enabled components. Otherwise union the requested
    components and aliases, then apply configuration exclusions.
    """
    if not selected and not aliases_given:
        return default_run_set()
    chosen = set(selected)
    for alias in aliases_given:
        chosen.update(ALIASES[alias])
    return select_run_set(chosen)


def plan_run(run_set: set, supplied: dict, artifact_backend: bool = True) -> dict:
    """Plan component order, available artifacts, warnings, errors, and slide access.

    supplied maps artifact names to paths. Planning performs no file or slide I/O.
    A requested tile_artifacts component without a backend produces a setup error.
    """
    errors: list = []
    warnings: list = []
    degraded: dict = {}

    # Validate programmatic component names as well as names accepted by the CLI parser.
    unknown = set(run_set) - set(COMPONENTS)
    if unknown:
        raise ValueError(f"unknown component(s) {sorted(unknown)}; valid: {sorted(COMPONENTS)}")
    if not run_set:
        raise ValueError("nothing to run: the component set is empty; request at least one "
                         "component enabled by components.<name> in configuration")
    unknown_art = set(supplied) - set(ARTIFACTS)
    if unknown_art:
        raise ValueError(f"unknown supplied artifact(s) {sorted(unknown_art)}; "
                         f"valid: {sorted(ARTIFACTS)}")
    if "tile_artifacts" in run_set and not artifact_backend:
        errors.append({"component": "tile_artifacts", "artifact": "grandqc_backend",
                       "message": "tile_artifacts requires GrandQC. Run 'pathnd-qc setup grandqc' "
                                  "or supply --grandqc_repo with a complete inference checkout."})

    # Every artifact that will exist: supplied outright, or produced by something in the run set.
    available = set(supplied)
    for key in run_set:
        available.update(COMPONENTS[key]["produces"])
    if "thumbnail" not in supplied:
        available.add("thumbnail")          # the reader always makes it when a slide is given

    # When an artifact is both supplied and computed, warn that the computed value takes precedence.
    for key in sorted(run_set):
        for art in COMPONENTS[key]["produces"]:
            if art in supplied:
                consequence = ("The computed value wins when successful; on pen failure the supplied "
                               "mask is used and recorded as degraded." if key == "pen_detection" else
                               "The computed value wins; the supplied file is ignored.")
                warnings.append(
                    f"{COMPONENTS[key]['flag']} will COMPUTE {art}, but {ARTIFACTS[art]['flag']} "
                    f"was also supplied ({supplied[art]}). {consequence}")

    # Rule 5 — a REQUIRED artifact with neither a producer in the run set nor a supplied file.
    for key in sorted(run_set):
        spec = COMPONENTS[key]
        for art in spec["requires"]:
            if art in available:
                continue
            errors.append({"component": key, "artifact": art,
                           "message": _missing_message(key, art)})
        # SOFT misses degrade rather than fail, but are recorded per component.
        missing_soft = [a for a in spec["optional"] if a not in available]
        if missing_soft:
            degraded[key] = (f"ran without {', '.join(missing_soft)} "
                             f"(not computed in this run and not supplied)")

    # Warn when no selected component consumes a supplied artifact.
    consumed = set()
    for key in run_set:
        consumed.update(COMPONENTS[key]["requires"]); consumed.update(COMPONENTS[key]["optional"])
        consumed.update(a for a in COMPONENTS[key].get("supplied_optional", ()) if a in supplied)
    consumed.add("thumbnail")                              # every M2/M4 component reads it
    for art in sorted(supplied):
        if art not in consumed and not any(art in COMPONENTS[k]["produces"] for k in run_set):
            warnings.append(f"{ARTIFACTS[art]['flag']} was supplied but no requested component "
                            f"consumes {art}; it is ignored.")

    order = [k for k in COMPONENTS if k in run_set]      # COMPONENTS is in dependency order

    # needs_open requests a slide handle for header or pixel reads.
    # needs_localize requests a local copy for components that read tiles.
    # A single analysis-plane read can stream from the remote slide.
    needs_localize = any(COMPONENTS[k]["needs_slide"] for k in run_set)
    needs_open = (needs_localize or "thumbnail" not in supplied
                  or any(COMPONENTS[k].get("needs_info") for k in run_set))
    return {"order": order, "available": available, "errors": errors, "warnings": warnings,
            "degraded": degraded, "needs_open": needs_open, "needs_localize": needs_localize}


def validate_supplied(supplied: dict) -> list:
    """Check that supplied files exist and use supported extensions before slide I/O."""
    import os
    problems = []
    for art, path in sorted(supplied.items()):
        exts = ARTIFACTS[art]["formats"]
        if not os.path.exists(path):
            problems.append(f"{ARTIFACTS[art]['flag']}: file not found: {path}\n"
                            f"  expected: an existing {' or '.join(exts)} file\n"
                            f"  {art} is {ARTIFACTS[art]['help']}")
        elif os.path.splitext(path)[1].lower() not in exts:
            problems.append(
                f"{ARTIFACTS[art]['flag']}: unsupported format "
                f"{os.path.splitext(path)[1] or '(none)'!r} for {path}\n"
                f"  expected: {' or '.join(exts)}\n"
                f"  masks are read as boolean (PNG: any non-zero pixel is True); "
                f"lists are JSON arrays of tile dicts with col/row/x/y/w/h")
    return problems


def _missing_message(component: str, artifact: str) -> str:
    """The error contract: name the missing artifact and BOTH remedies, with a runnable example."""
    spec = COMPONENTS[component]
    art = ARTIFACTS[artifact]
    producer = COMPONENTS.get(art["produced_by"] or "", {}).get("flag") or THUMBNAIL_PRODUCER
    lines = [f"{spec['flag']} needs {artifact.upper().replace('_', ' ')}, and none is available."]
    if art["produced_by"]:
        lines.append(f"  compute it:  pathnd-qc --slide <slide> {producer} {spec['flag']}")
        lines.append(f"               requires components.{art['produced_by']}=true in configuration")
    lines.append(f"  supply it :  pathnd-qc --slide <slide> {art['flag']} <path> {spec['flag']}")
    return "\n".join(lines)


def usage_summary() -> str:
    """Human-readable map of the whole surface — used by `--list_components`."""
    out = ["Components (one flag per file):"]
    for key, spec in COMPONENTS.items():
        req = ", ".join(spec["requires"]) or "-"
        opt = ", ".join(spec["optional"]) or "-"
        if spec.get("supplied_optional"):
            opt += " (supplied only: " + ", ".join(spec["supplied_optional"]) + ")"
        out.append(f"  {spec['flag']:<28} {spec['file']:<17} [{spec['stage']}] "
                   f"needs: {req}   optional: {opt}")
        for _, fname, desc in SIDE_OUTPUTS.get(key, ()):
            out.append(f"  {'':<28} writes {fname:<32} {desc[:60]}")
    out.append("\nModule aliases:")
    for alias, members in ALIASES.items():
        out.append(f"  {alias:<28} = {', '.join(members)}")
    out.append("\nSupplyable artifacts (.png / .npy for masks, .json for lists):")
    for name, art in ARTIFACTS.items():
        out.append(f"  {art['flag']:<28} {art['help']}")
    out.append("\nNo --run_* flags selects all configured components (all nine by default). "
               "components.<name>=false excludes any component, even with an explicit flag or alias. "
               "Every remaining component must complete; missing models are setup errors.")
    return "\n".join(out)
