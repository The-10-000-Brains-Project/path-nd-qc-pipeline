# `config` — every tunable value, and all QC thresholds

[Package overview](../README.md) · [CLI usage](../../USAGE_CLI.md) · [Library usage](../../USAGE_LIBRARY.md)

[How to change a value](#how-to-change-a-value) · [Enable or disable components](#enable-or-disable-components) · [Fold detection settings](#fold-detection-settings) · [How components read it](#how-components-read-it) · [Functions](#functions) · [Selected settings](#selected-settings)

The shared configuration for resolution targets, algorithm parameters,
component selection, model weight paths, normalization references, and QC thresholds. Metadata paths are explicit run inputs.

- `defaults.json` — the values. Plain JSON, no extra dependency.
- `config.py` — finds, parses, merges and exposes them.

## How to change a value

Configuration layers, later ones winning:

```
defaults.json  →  managed settings.json  →  $PATHND_CONFIG  →  load(path=...)
```

For most users, keep the shipped defaults and create a small override file, such as `my_config.json`:

```json
{"shared": {"tile_px": 512}}
```

In your activated environment, after [setup](../../USAGE_CLI.md#1-setup-and-first-run):

```bash
PATHND_CONFIG="$PWD/my_config.json" pathnd-qc \
  --slide /data/slides/example.svs --out reports --run_thumbnail --no_metadata
```

This example explicitly keeps the default tile size. With the shipped component settings, all five
thumbnail components remain selected. Missing default pen assets are installed automatically;
replace the slide path with your file. Use `--no_model_download` to require existing models.
There is no `--config` CLI flag. The `PATHND_` environment-variable prefix is retained after the
package rename.

Automatic model setup and the optional [setup commands](../external/README.md) remember model paths and the GrandQC interpreter in
`settings.json` under the user data directory selected by `PATHND_DATA_DIR`. This registration is
loaded before explicit overrides.

Override objects merge recursively; lists replace the existing list. `load(path=...)` applies an
additional explicit override after the environment file. Arguments override configuration where the
called API supports them; the CLI does not expose every configuration key.

Configuration is cached, but `load(path=...)` and `load(force=True)` can rebuild it. Modules also
capture defaults at import time, so reloading config does not reliably update already-imported
component defaults. Set the environment before starting a fresh process for reproducible runs.

Unreadable, invalid JSON, or non-object override files are reported and skipped. Overrides are
validated before merging: unknown keys, wrong types, non-finite numbers and invalid physical ranges
reject the **entire layer**, retaining the preceding valid settings. Diagnostics name the key and
appear in `config_error()` and report provenance. Correct the file and start a fresh process.
Stain aliases/reference names and threshold metric/stain names are extensible;
their values still have checked types. Component-specific overlay opacity and tile size overrides
are supported even when the defaults inherit their value from `shared`.

Package and report versions are code-owned. Remove old `shared.pipeline_version` and
`report.report_version` overrides; reports use `pathnd_qc.__version__` and `REPORT_VERSION` (2.0).
Schema 2.0 explicitly identifies factual `provenance.execution` instead of the retired trust verdict.
New MPP observations describe inconsistent metadata without changing the chosen scale.

## Enable or disable components

Every component has the same boolean switch, enabled by default:

```json
{
  "components": {
    "tissue_segmentation": true,
    "fold_detection": true,
    "pen_detection": true,
    "staining_quality": true,
    "focus": true,
    "stain_normalization": true,
    "tile_selection": true,
    "tile_metrics": true,
    "tile_artifacts": true
  }
}
```

Set any entry to `false` to exclude it; omitted entries retain their earlier/default setting.
This applies to `run()`, the single-slide CLI, batch children and both WDLs (`config_file`).
It filters the requested components **after** explicit selections or aliases, so an explicit flag
cannot re-enable a component disabled in config. With no selection, all enabled components run.
An empty effective selection is an error before slide processing.

Disabled components need no model files and do not make execution incomplete. Enabled, selected
components must complete. If you disable a required producer, supply its artifact (for example
`--tissue_mask`) or enable it again; dependencies are never silently added.
Reports list exclusions in `provenance.components_disabled_config` and skipped sections.

Direct library functions such as `detect_pen(image)` run when called; these switches select work
for the pipeline orchestrator. Replace the retired `m2.pen.enabled` with `components.pen_detection`.

## Fold detection settings

`m2.folds` includes `d_path_stains` and the `fline_*` controls for the ConnSoftT/F_line union.
[Defaults, units and validation](../qc_slide/folds/README.md#defaults) are documented together.
Use the same override file for local, batch and workflow runs; workflow requests call it `config_file`.
Changing `fline_enabled` to false restores ConnSoftT-only detection while retaining configured stain routing.

## How components read it

Modules read config at import time to seed their own module-level defaults:

```python
from ..config.config import cfg
DEFAULT_OPENING_RADIUS = cfg("m2.folds.opening_radius", 3)
```

Explicit supported function arguments override those defaults. Read the component API before
changing parameters; not every function accepts the same configuration keys as keyword arguments.

## Functions

| function | what it does |
|---|---|
| `cfg(key, default)` | read one dotted key, e.g. `cfg("m2.read.target_mpp")` |
| `thresholds()` | the whole `thresholds` block |
| `sources()` | which files the resolved config came from |
| `load(path=, force=)` | load or reload the configuration |
| `resolve_path(value)` | a path string resolved against the caller's working directory |
| `resolve_out_dir(out_dir)` | where output goes — see below |
| `resolved_sha256()` | a hash of the resolved values — see below |
| `provenance()` | the block that lands in every report |
| `config_error()` | why config failed to load, if it did |

**`resolved_sha256()` hashes the merged values, not the file.** An override has to change the recorded
hash, or the provenance in every report is wrong. It lands in each report as
`provenance.config_sha256`. The hash describes resolved configuration values; it does not include arbitrary Python call arguments.
Inspect recorded component parameters too, and avoid reloading configuration after importing components.

**`resolve_out_dir()` is the single definition of where output goes.** `--out` wins, accepting `~` and
relative paths; otherwise `report.out_dir` is resolved against the **caller's working directory**.
Relative model/checkpoint paths follow the same rule. Use absolute configured paths when the same
configuration is used from different working directories. Installed package directories are never
used as output or external-model roots.

## Selected settings

The complete key list is in [defaults.json](defaults.json). Values below describe the shipped defaults.

### `shared` — fallbacks a component key can override

| key | default | what it does |
|---|---|---|
| `shared.tile_px` | `512` | tile size in pixels |
| `shared.alpha_blend` | `0.45` | default overlay opacity |

### `ingestion` — M1

| key | default | what it does |
|---|---|---|
| `ingestion.target_magnification` | `20` | minimum objective power |
| *(target resolution)* | — | M1 gates on **`m3.read.tile_target_mpp`** — the plane it protects; there is no separate `ingestion.target_mpp` |
| `ingestion.mpp_tolerance_abs` | `0.05` | slack on the resolution bound, giving ≤ 0.55; M3 resolves with the same slack (`0.05 / 0.5 = 10 %`) |
| `ingestion.mpp_min_plausible` / `mpp_max_plausible` | `0.1` / `10.0` | the ordinary plausibility window; consistent positive sub-floor X/Y values may also be accepted using objective power. Otherwise the code tries `10 / objective_power`; no usable scale marks the check implausible. See [MPP rules](../ingestion/ingestion_checks/README.md) |
| `ingestion.localize_retries` | `3` | maximum attempts for transient download or verification failures; missing files, permission errors and wrong path kinds stop after one attempt |
| `ingestion.localize_retry_backoff_s` | `2.0` | base of the exponential backoff between attempts (×1–1.25 jitter) |
| `ingestion.localize_stall_timeout_s` | `120.0` | a transfer that delivers **no bytes** for this long is a recorded failure rather than a hung process. A stall guard, not a total deadline: a 0.2–1.8 GB transfer's total time varies too much for one number |
| `ingestion.localize_progress_every_s` | `30.0` | how often the download logs bytes so far and rate |
| `m3.tile_metrics.progress_every_s` | `30.0` | how often the tile loop logs `tiles i/n (kept, read failures)` |
| `ingestion.mpp_mag_product_min` / `_max` | `5.0` / `15.0` | confirmation window for sub-floor MPP; also records an out-of-window stated MPP/objective observation. A plausible stated scale still wins. This is a heuristic, not scanner calibration |
| `ingestion.integrity_max_read_px` | `4194304` | the largest single decode `check_integrity` will do (2048²); a bigger top level is spot-read at its centre |
| `ingestion.n_spot_regions` | `3` | regions the integrity check decodes |
| `ingestion.spot_size` | `256` | size of each of those regions |
| `ingestion.thumbnail_max_size` | `1024` | longest edge of a thumbnail read |
| `ingestion.verify_localize` | `true` | verify a downloaded slide against the source |

Metadata files are run inputs: pass `--metadata PATH` or `run(metadata_paths=[...])`. No path means
no lookup. The retired `ingestion.metadata` and `ingestion.metadata_csv` configuration keys are
rejected with migration instructions; remove them from old overrides. Use the
[metadata API](../ingestion/metadata/README.md) for explicit source specs and column mappings.

### `m2` — whole-slide QC

The tables show literal shipped defaults. Relative model paths are resolved from the working
directory; registered paths override them. See [model locations](../external/README.md#model-locations)
for checkout assets and managed storage.

| key | default | what it does |
|---|---|---|
| `m2.read.target_mpp` | `8.0` | the resolution M2 and M4 analyse at |
| `m2.read.mpp_tolerance_rel` | `0.1` | upsampling accepted before reading a finer level (M2's 8.0 µm/px plane and the 2.0 µm/px chunk plane; M3's 0.50 tile plane uses M1's bound instead). 10 % because the Aperio 20× top level is 8.4065 = 5.08 % over — at 5 % every Aperio slide read a 16× larger level |
| `m2.read.max_read_px` | `67108864` | the largest level M2 reads whole (64 Mpx, 192 MB as RGB). A larger level — a pyramid-less file, whose only level is level 0 — is read in horizontal bands and resampled band by band, which bounds source reads but is not a total-memory cap; the output image and other arrays also consume memory. A *remote* slide over the cap is downloaded first and banded from disk, never streamed band by band (2026-09-08) |
| `m2.read.max_plane_px` | `67108864` | maximum materialized analysis-plane pixels; larger outputs fail before decoding or allocation. Raise only when sufficient memory is available for the plane and intermediate arrays |
| `m2.pen.tile_px` | `null` | whole-image pen inference; optional tile size is a positive multiple of 32 |
| `m2.pen.halo_px` | `64` | context pixels for tiled pen inference only; non-negative multiple of 32 |
| `m2.pen.weights_path` | `external/weights/pen.pt` | the pen model |
| `m2.pen.device` · `.pen_class` · `.color` | `"cpu"` · `1` · `[0,200,0]` | device, output class, overlay colour |
| `m2.tissue.default_method` | `"union"` | segmentation method when the stain router finds no match |
| `m2.tissue.hirano_stains` | `["hirano"]` | which stains route to `otsu_s` |
| `m2.tissue.sat_p95_threshold` | `60.0` | saturation cut used by the `adaptive` method |
| `m2.tissue.disk_radius` · `.hist_bins` · `.thresh_range` | `5` · `30` · `[1.0, 4.0]` | entropy neighbourhood, histogram bins, valley band |
| `m2.tissue.keep` · `.min_area_frac` | `"all"` · `0.0001` | component cleanup |
| `m2.tissue.color` · `.alpha_blend` | `[0,180,255]` · `0.4` | overlay |
| `m2.folds.*` | 25 keys | thresholds, filters and the stain-vector settings — see [folds/](../qc_slide/folds/README.md) |
| `m2.staining` | *(empty)* | computes only mean Lab chroma; score bounds belong in `thresholds` |
| `m2.focus` | *(empty)* | the stage has no tunables yet |

### `m3` — tile-level QC

| key | default | what it does |
|---|---|---|
| `m3.read.tile_target_mpp` | `0.5` | the resolution tiles are read at |
| `m3.read.chunk_target_mpp` | `2.0` | the resolution chunk refinement reads at |
| `m3.read.halo_out_px` | `4` | pixels read past a window edge, to avoid resampling seams |
| `m3.tiles.tile_px` | `shared.tile_px`, default `512` (not present in defaults JSON) | tile edge length in pixels on the tile plane |
| `m3.tile_metrics.drop_below` | `0.2` | tissue fraction below which a tile is dropped; `null` disables both area-drop passes |
| `m3.tile_metrics.chunk_reseg_min` | `0.1` | lower bound for chunk re-segmentation |
| `m3.tile_metrics.resegment_edges` | `true` | re-segment partly-covered tiles |
| `m3.tile_metrics.alpha_blend` | `0.5` | heatmap opacity |
| `m3.tile_metrics.max_mask_bytes` | `536870912` | maximum packed tissue-mask bytes before allocating its backing storage |
| `m3.artifacts.classes` | `["fold","pen","bubble"]` | which artifact classes are reported |
| `m3.artifacts.min_fraction` | `null` | area fraction at or above which a class counts as present |
| `m3.artifacts.model_mpp` | `1.0` | the GrandQC model resolution requested; the checkout decides (1.0 → 1.5 → 2.0 checkpoints) |
| `m3.artifacts.python` | `null` | GrandQC Python executable; null uses the main interpreter; setup can register a separate one |
| `m3.artifacts.device` | `"cpu"` | passed to GrandQC's `main.py --device` |
| `m3.artifacts.repo_path` | `external/grandqc/repo/01_WSI_inference_OPENSLIDE_QC` | the checkout used when `--grandqc_repo` is not given; missing default assets are installed automatically when selected. Invalid custom paths or missing models with downloads disabled are errors |

### `m4` — stain normalization

| key | default | what it does |
|---|---|---|
| `m4.method` | `"macenko"` | default method |
| `m4.methods` | `["macenko","reinhard"]` | selectable methods; an override can restrict this list |
| `m4.min_tissue_px` · `.angular_percentile` | `3` · `99.0` | minimum retained pixels and angular percentile for Macenko fitting; this minimum is not applied to Reinhard |
| `m4.od_eps` · `.eps` | `1e-06` · `1e-08` | numerical floors |
| `m4.provenance_dir` | `"reports"` | fallback directory for the standalone `write_provenance()` helper; the pipeline embeds parameters in its report |
| `m4.reference` | 9 stains | the target parameters per stain class |
| `m4.stain_aliases` | 15 entries | raw metadata spellings mapped onto stain classes |

### `report` and `thresholds`

| key | default | what it does |
|---|---|---|
| `report.out_dir` | `"reports"` | where output goes, resolved against the caller's working directory |
| `report.focus_outlier_n` | `20` | number requested per outlier selection; dropped/errored tiles can make the combined list exceed this |
| `thresholds.*` | all `null` | the QC bounds — a **top-level** section, not part of `report` |

**The `thresholds` block uses flat dotted keys**, one per metric — `"m2.focus.focus_score"`,
`"m3.tiles.kept_fraction"` — not nested objects.

**Thresholds are two-sided** — `{"min": null, "max": null}` — because staining needs both ends.
Comparison happens in [`reporting`](../reporting/README.md), never inside a component. A metric outside
its bounds raises a report flag; these comparisons do not themselves reject slides or remove tiles.
Independent MPP gates and tile-drop rules still apply. An entry may also carry a `by_stain` map,
resolved through `m4.stain_aliases`.
