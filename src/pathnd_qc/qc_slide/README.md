# M2 — WSI-Level QC

[Package overview](../README.md) · [CLI usage](../../USAGE_CLI.md) · [Library usage](../../USAGE_LIBRARY.md)

[Components](#components) · [Stain routing](#stain-routing) · [Running it](#running-it) · [Configuration](#configuration) · [Structure](#structure)

For setup, paths, and output folders, start with the [usage guide](../../USAGE_CLI.md). Command examples below use the activated environment where the package is installed. Replace `/path/to/slide.svs` with your slide and mask paths with files from its earlier run.

Resolutions shown are the shipped defaults; µm/px means micrometres per pixel. Output artifacts are written inside the slide’s timestamped run folder unless `--no_save_artifacts` is used; the report is still written.

Whole-slide quality signals, all computed from the same 8.0 µm/px analysis image, in the order
**tissue → folds → pen → staining → focus**.

- **Detects pen** and ink marks.
- **Segments tissue** from glass — support for folds, staining, focus and tile selection. Pen can run independently.
- **Detects folds**, where the section is doubled over.
- **Measures staining** and **focus** over tissue, excluding available fold and pen masks.

## Components

| Component | What it does | Docs |
|---|---|---|
| **pen** | pen-mark mask — requires weights; predictions need validation | [pen/](pen/README.md) |
| **tissue** | tissue-versus-glass mask, routed on stain type | [tissue/](tissue/README.md) |
| **folds** | ConnSoftT body detection plus F_line creases | [folds/](folds/README.md) |
| **staining** | mean CIE-Lab chroma (`chroma_mean`) | [staining/](staining/README.md) |
| **focus** | a Laplacian-variance sharpness score | [focus/](focus/README.md) |

## Stain routing

Two components change algorithm based on the slide's stain type, and both fall back to their
non-Hirano path when it is missing:

| stain | tissue | folds |
|---|---|---|
| Hirano | `otsu_s` — saturation-Otsu alone | `d = s − i` + F_line |
| LFB / LFB-H&E spellings | `union` — `otsu \| entropy` | `d = s − i` + F_line |
| everything else, or unknown | `union` — `otsu \| entropy` | Macenko stain-2 + F_line |

⚠️ **A missing stain type is therefore not cosmetic** — a Hirano slide would be analysed by the wrong
two algorithms. Supply `--stain`, or supply a CSV with `--metadata` for
[metadata lookup](../ingestion/metadata/README.md). No metadata files are searched automatically;
the report records an unresolved-stain flag when no stain is available.

## Running it

```bash
pathnd-qc --slide "/path/to/slide.svs" --run_thumbnail                 # all enabled M2 components
pathnd-qc --slide "/path/to/slide.svs" --run_tissue_segmentation --run_fold_detection --run_focus
pathnd-qc --slide "/path/to/slide.svs" --run_fold_detection --tissue_mask out/slide_tissue_mask.png
```

`--run_thumbnail` requests all five M2 components, then applies the `components.<name>` config
switches. Every remaining component must complete.
Missing default pen weights are installed automatically. Invalid custom paths, failed setup,
or missing weights with `--no_model_download` are errors. See [model setup](../external/README.md).
Use explicit component flags to run a subset.

M2 components share one analysis image. Remote slides are normally read without downloading the
whole file, but the reader can localize a remote slide when the selected pyramid level exceeds
`m2.read.max_read_px`. A local slide needs no download. A supplied `--thumbnail` avoids opening the
slide only when no selected component needs slide information or tile pixels. Its physical scale is
assumed from configuration, so it must be prepared at the configured analysis resolution.

When both pen and fold masks are computed, folds take precedence in overlap. A supplied mask is
preserved; a computed counterpart yields to it. See [pen detection](pen/README.md).

## Configuration

M2 reads the **`m2.*`** prefix in [`config/defaults.json`](../config/defaults.json) — `m2.read` for
the analysis resolution, then one section per component. Each component README lists its own keys; the
full table is in [`config/`](../config/README.md).

## Structure

```
qc_slide/
├── README.md                     ← you are here
├── pen/
│   ├── pen.py                    pen-mark mask
│   └── README.md
├── tissue/
│   ├── tissue.py                 tissue vs. glass mask — analysis support
│   ├── README.md
│   └── assets/
├── folds/
│   ├── folds.py                  fold detection
│   ├── README.md
│   └── assets/                   result images
├── staining/
│   ├── staining.py               mean CIE-Lab chroma
│   └── README.md
└── focus/
    ├── focus.py                  Laplacian-variance sharpness score
    └── README.md
```

Components run in the order **tissue → folds → pen → staining → focus**, so staining and focus receive the final available masks.
