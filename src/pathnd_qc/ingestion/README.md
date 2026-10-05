# M1 — slide ingestion

[Package overview](../README.md) · [CLI usage](../../USAGE_CLI.md) · [Library usage](../../USAGE_LIBRARY.md)

Ingestion connects the pipeline to local slides or GCS, S3 and Azure objects. It resolves optional metadata,
reads acquisition information, checks resolution and integrity, and records the source of the data.
See the [usage guide](../../USAGE_CLI.md) for installation and complete commands.

| Component | What it does |
|---|---|
| [WSI reader](wsi_reader/README.md) | Open slides, read images at a target scale, and localize remote slides |
| [Metadata lookup](metadata/README.md) | Match full slide paths to explicitly supplied CSV records |
| [Ingestion checks](ingestion_checks/README.md) | Record magnification, effective resolution, integrity and a decision |
| [Legacy adapters](adapters/README.md) | Older dataset conversion helpers; not called by the main pipeline |

## When ingestion runs

There is no `--run_m1` flag. The planner requests slide access when selected components need image
pixels or acquisition information. A run using only supplied images/masks for M2/M4 can skip slide
acquisition. A supplied thumbnail alone does not guarantee this: tile selection still needs slide
information, while tile metrics and tile artifacts need access to slide pixels.

Metadata lookup is separate and runs only when a CSV path is supplied using `--metadata`, including
when slide acquisition is skipped. Without a path, lookup is skipped. `--stain` overrides the stain resolved from
metadata. `--bank` selects normalization reference parameters and supplies the source dataset
label when metadata has not provided one. It preserves an existing dataset label and does not
select metadata files.

After installing the package:

```bash
pathnd-qc --slide /data/slides/example.svs --out reports \
  --run_tissue_segmentation --no_metadata
```

Replace the slide path with an existing supported file. Add its actual stain if known.

## How to interpret ingestion results

Results appear under `m1` in `<slide_id>_report.json`. A failed acquisition check is distinct from a
fatal process error. The main pipeline gates tile work using effective MPP, and refuses an M2 image
read when no plausible scale is available. The integrity decision can recommend quarantine; the
main pipeline records that recommendation and does not automatically stop solely because of it.
Inspect checks, decision, component errors and `provenance.execution` together.

Most settings are under `ingestion.*` in [defaults.json](../config/defaults.json). The tile gate
uses `m3.read.tile_target_mpp`; M2/M4 reads use `m2.read.target_mpp`.
