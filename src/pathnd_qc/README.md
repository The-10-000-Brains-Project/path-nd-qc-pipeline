# Path-ND QC

[CLI usage](../USAGE_CLI.md) · [Library usage](../USAGE_LIBRARY.md) · [Component guides](#guides)

[Install](#install) · [First run](#first-run) · [Browse your results](#browse-your-results) · [Model setup](#model-setup) · [Guides](#guides) · [Package files](#package-files) · [Python and alternate commands](#python-and-alternate-commands) · [Distribution contents](#distribution-contents)

Path-ND QC measures quality in whole-slide microscopy images. It processes one slide or a folder,
saves tissue masks and measurements, and produces reports for review. Python imports use `pathnd_qc`;
the distribution name is `pathnd-qc`.

A completed run is not automatically a QC pass. Review component errors, completeness, and flags.
Report thresholds are unset by default, and bundled normalization references are placeholders.

## Install

Work from a repository checkout so you can inspect and edit the code. If the package is already
installed, continue to [First run](#first-run).

Requires Python 3.12 or newer. Start in the directory containing `pyproject.toml` and `pathnd_qc/`
(`src/` in the development workspace). This directory can be supplied on its own. Create and activate
an environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

For native Windows, use Python 3.13+ and read the [platform requirements](../USAGE_CLI.md#platform-support),
including GrandQC symlink permissions. Windows has not been validated end to end. Local checks
cover macOS on Apple Silicon and the Linux amd64 deployment image.

From that project directory, install the library:

```bash
python -m pip install -e .
```

Editable installation uses your source changes without reinstalling. For CPU-only Linux/Windows,
follow the [Torch/torchvision installation steps](../USAGE_CLI.md#cpu-only-linux-and-windows)
after installing the package.

To use a prepared release instead, install its wheel from any directory:

```bash
python -m pip install /path/to/pathnd_qc-0.5.0-py3-none-any.whl
```

The project is packaged for pip installation; these instructions install from a checkout or a prepared wheel.
A bare `pip install pathnd-qc` requires publication to your chosen package index first.

## First run

Replace the slide path and stain with your actual values. Missing default model assets are
installed automatically for selected components and reused on later runs:

```bash
pathnd-qc --slide "/data/slides/example.svs" --out reports \
  --run_tissue_segmentation --no_metadata --stain "Hirano"
```

This first example selects tissue segmentation, which needs no external model. To run all enabled
components (all nine by default), omit `--run_tissue_segmentation` and first
[configure a normalization reference](normalization/README.md#create-a-reference-configuration).
Selected components must complete. Metadata lookup is skipped unless
you provide `--metadata PATH`; `--no_metadata` makes that choice explicit. Results go into a new
slide-specific folder under `reports`. Use config switches or explicit component flags for a smaller analysis.

For a folder of slides with the same stain:

```bash
pathnd-qc-batch run --slide_dir "/data/slides" --out reports \
  --batch_id study01 --workers 2 --run_tissue_segmentation --no_metadata --stain "Hirano"
```

Two slides run concurrently; another starts as each finishes. For mixed stains, use a
[manifest](batch/README.md#choose-the-slides) or your own metadata CSV. Repeating the same `--out` and `--batch_id` skips
completed slides with the same request fingerprint; changed inputs, settings, or component flags
run again. Omitting `--batch_id` starts a fresh batch. Use `--force` to repeat a completed request
in the same batch, or `--retry_failed` to retry failures.

Slides and explicit metadata files can be local or on GCS, AWS S3 or Azure Blob/ADLS Gen2.
Cloud batches use a list/manifest of object URIs. See
[cloud setup and examples](../USAGE_CLI.md#cloud-slides-gcs-aws-s3-and-azure).

## Browse your results

Open the `index.html` inside a dated slide run, or `batch_<batch_id>/index.html` for a batch.
The CLI prints this as `browse -> ...`. Batch slide folders, summaries and logs live together under
`batch_<batch_id>/`. Folder inputs mirror subfolders inside it; list entries use `sources/<path-sha256>/` parents. Single-slide
commands use `--out` directly. Each slide has `<slidename>_output/` with dated runs. Each run separates
`images/` (masks, overlays and normalized image) and `data/` (tile geometry and records), alongside
its full report. Contents pages include execution notes,
pipeline version and Git commit. See the [folder layout](../USAGE_CLI.md#8-output-files).

## Model setup

Normal runs prepare missing default model assets automatically and reuse them from your user data
directory. No separate setup command is required. To prohibit model downloads, use
`--no_model_download` or `PATHND_NO_MODEL_DOWNLOAD=1`; existing assets still work.

`pathnd-qc setup --check` is an optional diagnostic. Explicit setup commands can pre-download assets
or register custom versions and separate GrandQC environments. Pen's Google Drive download may
require a manual browser download when the provider blocks access. See [model setup](external/README.md).
Pip installs the Python dependencies; model downloads happen on first use.

| Analysis | Flags | Required inputs |
|---|---|---|
| Tissue mask | `--run_tissue_segmentation` | Installed package |
| Whole-slide QC | `--run_thumbnail` | Pen weights |
| Tissue and tile measurements | `--run_tissue_segmentation --run_tile_selection --run_tile_metrics` | Installed package |
| Tile QC including artifacts | `--run_tissue_segmentation --run_tiles` | GrandQC setup for artifact analysis |
| Normalization | `--run_tissue_segmentation --run_stain_normalization` | A suitable stain reference |
| Default full pipeline | No `--run_*` flags | Both models and normalization reference |

Required input producers are not automatically selected for a subset. Configuration
`components.<name>=false` excludes any component, including from explicit flags and aliases.
See [component settings](config/README.md#enable-or-disable-components).
Missing default model assets are set up before slide processing. `--no_model_download` disables
automatic downloads; a missing required model or failed setup is an error; incomplete selected work fails
the run and retains its report and available outputs.

## Guides

Use `--no_model_download` for CLI/batch runs, `download_models=False` in Python, or
`PATHND_NO_MODEL_DOWNLOAD=1` to prohibit automatic model downloads.

- [CLI usage](../USAGE_CLI.md): installation, single slides, batches, flags and troubleshooting.
- [Library usage](../USAGE_LIBRARY.md): Python calls, configuration, results and errors.
- [Batch processing](batch/README.md): lists, manifests, concurrency and resume.
- [Configuration](config/README.md): settings and report thresholds.
- [External models](external/README.md): downloads, custom versions, checks and separate environments.
- [Migration guide](MIGRATION.md): report schema 2.0 and stricter input contracts.

| Stage | Role | Default working resolution |
|---|---|---|
| [M1 ingestion](ingestion/README.md) | Read metadata and check acquisition/integrity | Scanner information and sampled reads |
| [M2 slide QC](qc_slide/README.md) | Tissue → folds → pen → staining/focus | 8.0 µm/pixel |
| [M3 tile QC](qc_tile/README.md) | Select tiles, measure tissue/focus, detect artifacts | 0.50 µm/pixel; GrandQC uses its own model scale |
| [M4 normalization](normalization/README.md) | Normalize the analysis image using a reference | Same image as M2 |

In fold/pen overlaps, computed folds take precedence over computed pen; when only one of those
masks is supplied, it takes precedence over its computed counterpart. Supplying a mask while also
selecting its own producer is a different case: see [mask precedence](../USAGE_CLI.md#mask-precedence-and-optional-inputs).
With default settings, a usable scale coarser than 0.55 µm/pixel skips M3. See the usage guide
for supplied-image exceptions, derived scales and interpretation limits.

The [original design diagram](assets/pipeline_diagram.png) includes planned BDSA integration and
other behavior not implemented in this checkout; it is historical context.

## Package files

Paths in this table are relative to `pathnd_qc/`. Component and module READMEs describe their own
files and behavior; the [usage guides](#guides) explain how to run them.

| Item | General purpose |
|---|---|
| [README.md](README.md) | Package overview, installation and stage map |
| [MIGRATION.md](MIGRATION.md) | Changes relevant to existing callers and saved reports |
| [__init__.py](__init__.py) | Public Python exports and package/report versions |
| [__main__.py](__main__.py), [cli.py](cli.py) | Installed command dispatch |
| [pipeline.py](pipeline.py) | Coordinates one slide's analysis and publication |
| [pipeline_spec.py](pipeline_spec.py) | Component names, dependencies and selections |
| [artifact_store.py](artifact_store.py) | Reads and writes reusable masks, images and JSON |
| [output_layout.py](output_layout.py) | Run-folder layout and artifact discovery |
| [result_browser.py](result_browser.py) | Generates HTML contents pages for slide and batch results |
| [_fs.py](_fs.py), [_images.py](_images.py) | Shared filesystem and image conversion helpers |
| [_logging.py](_logging.py), [_provenance.py](_provenance.py) | Logging and implementation identity |
| [batch/](batch/README.md) | Input discovery, scheduling, resume and batch summaries |
| [config/](config/README.md) | Defaults, overrides, component switches and validation |
| [external/](external/README.md) | Packaged model installer, asset catalog and compatibility patches |
| [ingestion/](ingestion/README.md) | Slide readers, metadata, acquisition and integrity checks |
| [qc_slide/](qc_slide/README.md) | Tissue, folds, pen, staining and focus components |
| [qc_tile/](qc_tile/README.md) | Tile selection, measurements and GrandQC artifacts |
| [normalization/](normalization/README.md) | Reference fitting and stain normalization |
| [reporting/](reporting/README.md) | Report assembly, thresholds and provenance |
| [assets/](assets) | Package documentation images, including the historical design diagram |

## Python and alternate commands

See the [library guide](../USAGE_LIBRARY.md) for complete examples and failure handling.

```python
from pathnd_qc import run

result = run(
    "/data/slides/example.svs", out_dir="reports",
    components={"tissue_segmentation"}, stain_type="Hirano", metadata_enabled=False,
)
report = result["report"]
```

After installation, imports work from any directory. `python -m pathnd_qc` supports the same commands
as `pathnd-qc`. Batch can also run through `pathnd-qc batch run` or `python -m pathnd_qc.batch run`.
From the repository root, `python src/run.py` and `python src/batch_run.py` run the checkout
with the same single-slide and batch arguments. The installed commands also use your checkout
when installed with `pip install -e ./src`.

## Distribution contents

The wheel includes the `pathnd_qc` runtime modules, batch runner, configuration defaults, user guides,
example images, installer catalog and compatibility patches. Development plans, tests, review docs,
model weights and upstream repositories are excluded. Managed models live in user data storage;
input slides and reports stay in the locations you select.

To build a wheel and source distribution, run these commands from the directory containing
`pyproject.toml` (`src/` in the development workspace):

```bash
python -m pip install build
python -m build
```

Artifacts appear in `dist/`. Install the wheel to use or share the library without the source tree.
