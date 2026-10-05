# M3 — Tile-Level QC

[Package overview](../README.md) · [CLI usage](../../USAGE_CLI.md) · [Library usage](../../USAGE_LIBRARY.md)

[Components](#components) · [How tiles are kept or dropped](#how-tiles-are-kept-or-dropped) · [Running it](#running-it) · [Configuration](#configuration) · [Structure](#structure)

For setup, paths, and output folders, start with the [usage guide](../../USAGE_CLI.md). Command examples below use the activated environment where the package is installed. Replace `/path/to/slide.svs` with your slide and mask paths with files from its earlier run.

Resolutions shown are the shipped defaults; µm/px means micrometres per pixel. Output artifacts are written inside the slide’s timestamped run folder unless `--no_save_artifacts` is used; the report is still written.

Per-tile quality control at a configured physical resolution, driven by the M2 tissue mask.

- **Tiles** the slide at 0.50 µm/px and keeps the tiles containing tissue.
- **Measures** each tile's tissue fraction and focus at the tile resolution, dropping mostly-glass tiles.
- **Refines** partly covered tiles when configured, building a mask at the tile resolution; fully covered tiles retain coarse support.
- **Renders** a per-tile focus heatmap.
- Labels artifacts per tile with the GrandQC model.

## Components

| Component | What it does | Docs |
|---|---|---|
| **tiles** | tile geometry and tissue-tile selection — pure, reads no pixels | [tiles/](tiles/README.md) |
| **tile_metrics** | per-tile tissue fraction and focus, the mask at the tile resolution, the heatmap | [tile_metrics/](tile_metrics/README.md) |
| **artifacts** | GrandQC fold / pen / bubble per tile — needs external models; unvalidated on this data | [artifacts/](artifacts/README.md) |

## How tiles are kept or dropped

At the defaults, the M2 mask is 16× coarser per pixel than the tile plane, so a 512-pixel tile spans roughly 32×32 mask cells — a
staircase edge, and an edge decision made on that staircase. `tile_metrics` handles this in two passes:

| pass | when | what happens |
|---|---|---|
| **1** | before the tile is read | drop it if the coarse mask says under 20 % tissue — costs no full-resolution read |
| **2** | after the tile is read | re-segment anything not already fully tissue; drop it if the refined fraction is under 20 % |

A separate chunk-level refinement pass at 2.0 µm/px, `refine_tissue_mask`, is built and callable but
**the pipeline does not use it** — `pipeline.run()` selects tiles straight from the M2 mask, so `m3.cascade` in
the report is always an explicit skip.

## Running it

```bash
pathnd-qc --slide "/path/to/slide.svs" --run_tissue_segmentation --run_tiles          # all of M3
pathnd-qc --slide "/path/to/slide.svs" --run_tiles --tissue_mask out/slide_tissue_mask.png
pathnd-qc --slide "/path/to/slide.svs" --run_tile_metrics \
              --tissue_mask out/m.png --tile_list out/slide_tile_list.json
```

`--run_tiles` requests tile selection, tile metrics and GrandQC artifact detection, then applies
the `components.<name>` config switches. Every remaining component must complete. Missing default
GrandQC assets are installed automatically before slide I/O. Invalid custom paths, failed setup,
or missing assets with `--no_model_download` are errors. See [model setup](../external/README.md).
Choose a subset through config or explicit flags.

`tile_metrics` and `artifacts` require a local slide file. Remote slides are localized
(downloaded to a temporary file; there is no persistent localization cache); local slides need no download. Size and transfer time depend on
the slide. Thumbnail analysis can also localize a remote slide when its read-size limit requires it.

**The gate is M1's, not M3's.** M1's `check_mpp` passes a slide whose level 0 is no coarser than
**0.55 µm/px**; M3 then selects the coarsest eligible pyramid level and resamples it to 0.50
(at most a 10 % upsample — it resolves with the
same bound, `tile_metrics.M3_MPP_TOL_REL`). A plausible but too-coarse scale skips M3 while
allowing M2/M4. An implausible scale also prevents the M2 image read unless an analysis image
is supplied. M1's reason is recorded at `m3.reason` with `m3.gate: "m1.checks.mpp"`.
M3 has no resolution bound of its own.

## Configuration

M3 reads the **`m3.*`** prefix in [`config/defaults.json`](../config/defaults.json) — `m3.read` for the
resolutions (2.0 µm/px chunks, 0.50 µm/px tiles), then one section per
component. Each component README lists its own keys; the full table is in
[`config/`](../config/README.md).

## Structure

```
qc_tile/
├── README.md                     ← you are here
├── tiles/
│   ├── tiles.py                  tile geometry and tissue-tile selection — reads no pixels
│   ├── README.md
│   └── assets/
├── tile_metrics/
│   ├── tile_metrics.py           per-tile tissue fraction and focus · mask at the tile resolution · heatmap
│   ├── README.md
│   └── assets/
└── artifacts/
    ├── artifacts.py              GrandQC artifact classes per tile: checkout → GrandQC once → every tile
    ├── artifacts_tile.py         runs GrandQC's own scripts over the whole slide; cuts one tile from the mask
    ├── README.md
    └── assets/
```

`tiles` performs geometry only; the pipeline still opens the slide to obtain its dimensions and
resolution. `tile_metrics` and `artifacts` require local access to slide pixels.
