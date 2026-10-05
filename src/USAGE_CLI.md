# Path-ND QC — CLI usage

[Package overview](pathnd_qc/README.md) · [Python library guide](USAGE_LIBRARY.md)

Run one slide or a batch from a terminal. Start with [installation](#1-setup-and-first-run),
then choose a [first run](#first-run), [full analysis](#full-analysis-with-models), or
[resumable batch](#10-batch-processing). The remaining sections are an option and output reference.
Examples are not performance promises or clinical validation.

Every registered component is part of the full pipeline, including pen detection and GrandQC
artifact detection. With no component flags, all configured components are selected (all nine by
default). Configuration can disable any component. Missing default model assets are installed automatically
before slide I/O. Failed setup, invalid custom paths, or missing assets with downloads disabled are errors;
they never turn a requested analysis into a successful skip. A selected component that fails or
cannot complete makes the run fail, while preserving the available measurements and report.
Use configuration, explicit component flags or Python functions to run a subset. Every algorithm
uses the same `components.<name>` configuration switch and completion rules.

- [1. Setup and first run](#1-setup-and-first-run)
- [2. Models and metadata](#2-models-and-metadata)
- [3. Selecting components](#3-selecting-components)
- [4. Whole-slide and tile examples](#4-whole-slide-and-tile-examples)
- [5. Reusing masks and tile lists](#5-reusing-masks-and-tile-lists)
- [6. Single-slide options](#6-single-slide-options)
- [7. Resolution and downloads](#7-resolution-and-downloads)
- [8. Output files](#8-output-files)
- [9. Reading a report](#9-reading-a-report)
- [10. Batch processing](#10-batch-processing)
- [11. Troubleshooting](#11-troubleshooting)

## 1. Setup and first run

Requires Python 3.12 or newer (3.13+ on native Windows; see [platform support](#platform-support)).
If the package is already installed, continue to [First run](#first-run).
Otherwise create an environment in a folder of your choice:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows, create the environment with `py -m venv .venv` and activate it with
`.venv\Scripts\Activate.ps1` in PowerShell. Use the Python interpreter from that environment.

With Git installed, install directly from GitHub without a manual checkout:

```bash
python -m pip install "git+https://github.com/The-10-000-Brains-Project/path-nd-qc-pipeline.git#subdirectory=src"
```

This installs the default branch; private repository access requires GitHub authentication.
For an existing checkout, run `python -m pip install .` from the directory containing
`pyproject.toml` and `pathnd_qc/` (`src/` in this workspace). The parent workspace is not required.

If someone has given you the built wheel, install it from any folder instead of `pip install .`:

```bash
python -m pip install /path/to/pathnd_qc-0.5.0-py3-none-any.whl
```

The distribution is pip-installable locally; this change does not publish it to PyPI. For development,
`python -m pip install -e .` installs an editable checkout. Runtime dependencies are declared in
`pyproject.toml`; `python -m pip install -r requirements.txt` installs this same project from its root.
The standard installation includes Python dependencies for every component, including both models.
Selected default checkpoints are obtained automatically on first use; pip does not download weights.

### CPU-only Linux and Windows

After installing Path-ND, install **both** Torch and torchvision together from the CPU index,
before running the pipeline. This also repairs an environment with mixed CPU/CUDA wheels:

```bash
python -m pip install --upgrade --force-reinstall "torch>=2.6,<3" "torchvision>=0.21,<1" --index-url https://download.pytorch.org/whl/cpu
python -m pip check
python -c "import torch, torchvision; print(torch.__version__, torchvision.__version__); print(torchvision.ops.nms(torch.tensor([[0.,0.,1.,1.]]), torch.tensor([1.]), 0.5))"
```

Installing CPU Torch alone is insufficient: torchvision must come from the same index with a
compatible version. The operator check must print `tensor([0])`. Repeat this check after dependency
upgrades. On macOS use the normal PyPI installation; do not use the CPU wheel index.
See [PyTorch's matching releases](https://docs.pytorch.org/get-started/previous-versions/).

### Platform support

The package requires Python 3.12+. Local checks cover macOS on Apple Silicon and the Linux
amd64 deployment image. Native Windows has not been validated end to end.

On Windows, use Python **3.13+**: replacing existing output files calls `os.fchmod`, which is
unavailable on Windows in Python 3.12. GrandQC creates a symbolic link to the slide, so enable
Windows Developer Mode or use an account with symlink privileges. Batch download-slot limits
are not enforced on Windows; limit `--workers` instead. These requirements do not establish
complete Windows support. The supplied WDLs and container run Linux.
See Python's [file-permission](https://docs.python.org/3.13/library/os.html#os.fchmod) and
[symbolic-link](https://docs.python.org/3.13/library/os.html#os.symlink) documentation.

After installation, commands and imports work from any folder. Quote paths containing spaces:

```bash
pathnd-qc --help
pathnd-qc --list_components
pathnd-qc-batch --help
pathnd-qc setup --help
```

### First run

Start with tissue segmentation, which needs no external model weights. Replace both the slide
path and stain with your data:

```bash
pathnd-qc --slide "/absolute/path/to/slide.svs" --out reports \
  --run_tissue_segmentation --no_metadata --stain "Hirano"
```

Open the run's `index.html` at the `browse -> ...` path printed by the command.

### Full analysis with models

Run all nine components on one local slide; missing default model assets are downloaded and checked automatically:

```bash
pathnd-qc \
  --slide "/absolute/path/to/slide.svs" \
  --out "./reports" \
  --no_metadata --stain "Hirano"
```

Replace the path and `Hirano` with your slide's real values. Full runs include normalization;
review the configured reference for that stain and [fit an approved target](pathnd_qc/normalization/README.md#create-a-reference-configuration). Section 3 explains explicit component subsets.

The terminal prints `report -> ...` with the output location. Open that JSON report in a text editor,
or use the batch summary described below for a spreadsheet-friendly overview.

### Cloud slides: GCS, AWS S3 and Azure

The base installation includes all three storage drivers.
Use an explicit object URI wherever `--slide` accepts a local WSI:

| Storage | Slide URI | Authentication |
|---|---|---|
| Google Cloud Storage | `gs://bucket/folder/slide.svs` | Existing application-default or service-account credentials |
| AWS S3 | `s3://bucket/folder/slide.svs` | AWS credential/profile files, environment credentials or an assigned IAM role |
| Azure Blob / ADLS Gen2 | `az://container/folder/slide.svs` | Account name plus Azure credentials, or a connection string |

For AWS, a configured profile can be selected before running:

```bash
export AWS_PROFILE=my-profile
pathnd-qc --slide 's3://my-bucket/slides/slide.svs' --out reports \
  --run_tissue_segmentation --stain AT8
```

The SDK also accepts `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and, for temporary credentials,
`AWS_SESSION_TOKEN`. For intentionally public S3 objects without credentials, set
`FSSPEC_S3='{"anon": true}'` before starting Python. See [S3 authentication](https://s3fs.readthedocs.io/en/latest/#credentials).

For streaming over a slow link, smaller S3 read-ahead blocks can reduce unnecessary transfers;
for example, `FSSPEC_S3='{"anon":true,"default_block_size":262144}'` requests 256 KiB blocks.
Smaller blocks may increase request counts; the best setting depends on the slide and connection.

For Azure with an existing Azure CLI login, managed identity or service principal:

```bash
export AZURE_STORAGE_ACCOUNT_NAME=myaccount
export AZURE_STORAGE_ANON=false
pathnd-qc --slide 'az://my-container/slides/slide.svs' --out reports \
  --run_tissue_segmentation --stain AT8
```

Azure uses its default credential chain when no explicit storage credential is configured.
Alternatively, supply `AZURE_STORAGE_CONNECTION_STRING`, or the account name with
`AZURE_STORAGE_ACCOUNT_KEY` or `AZURE_STORAGE_SAS_TOKEN`. A fully qualified URI such as
`abfss://my-container@myaccount.dfs.core.windows.net/slides/slide.svs` supplies the account name;
`abfs://` is also accepted. For intentionally public blobs, set `AZURE_STORAGE_ANON=true`.
See [Azure storage authentication and URI forms](https://github.com/fsspec/adlfs).

Keep credentials in the provider environment/configuration rather than in slide URIs or metadata
files: input URIs are retained in logs and reports. The process needs read access to the objects.
Outputs and temporary downloads are local; the selected QC components determine whether a slide
is streamed or downloaded before processing. Files must still be supported by TiffSlide.

For a batch, put any mixture of GCS, S3, Azure and local slide paths into a local text list or
manifest and pass `--slides`. `--slide_dir` still walks local folders; it does not enumerate cloud
buckets. Each listed slide gets its own `<slidename>_output` folder. Distinct slides with the same
filename stem in a list receive separate path-derived output parents.
Explicit `--metadata` CSV paths support the same providers and may be repeated. Omitting metadata
continues without a lookup.

## 2. Models and metadata

### External models

| Analysis | What must be available | If unavailable |
|---|---|---|
| Pen detection | WSISegQC `pen.pt`, configured by `m2.pen.weights_path` or `--pen_weights` | Missing default weights are installed automatically; invalid custom paths are refused; inference failure fails the run with a report |
| Tile artifacts | Patched GrandQC inference checkout and model checkpoints, configured by `m3.artifacts.repo_path` or `--grandqc_repo` | Missing default assets are installed automatically; invalid custom checkouts are refused; inference failure fails the run with a report |
| Stain normalization | Reference parameters for the stain in `m4.reference` | Missing/unusable references fail the run with a normalization error |

The full run includes pen, GrandQC and normalization. Missing default model assets are downloaded,
checked and registered automatically before analysis. Later runs reuse them. To prohibit automatic
model downloads, add `--no_model_download` to a single-slide or batch command, or set
`PATHND_NO_MODEL_DOWNLOAD=1` in the environment. Missing required assets then fail loudly.
The option does not disable the selected analyses or suppress slide downloads.

`pathnd-qc setup --check` is an optional diagnostic. Explicit `setup pen` and `setup grandqc`
commands remain available for pre-downloading or registering custom installations.
Pen's Google Drive source can require downloading through a browser and registering with
`pathnd-qc setup pen --weights /path/to/pen.pt`. No model downloads occur during pip installation.

To register your own GrandQC version without modifying it:

```bash
pathnd-qc setup grandqc --repo /my/grandqc/inference-directory --python /my/env/bin/python
```

The remaining setup options: `--weights-dir DIR` reuses checksum-verified official GrandQC weights
already present in that directory instead of downloading them;
`--url HTTPS_URL --sha256 HEX` fetches pen weights from a URL you trust, refusing anything but
HTTPS and anything whose bytes do not hash to `--sha256`. `pathnd-qc setup --help` lists them all.

Custom versions must satisfy the pipeline's interface. For details, checksums, inference dependencies,
manual downloads and user storage paths, see [model setup](pathnd_qc/external/README.md). You can override
one run with `--grandqc_repo`, `--grandqc_python` or `--pen_weights`; those also work in batch mode.
Registered paths override shipped defaults; an explicit config file or run flag can override them.
Normal installed use needs no repository asset folder. To reuse checkout assets, see
[model locations and registration](pathnd_qc/external/README.md#model-locations).

Bundled normalization references are marked `is_placeholder: true`. They demonstrate normalization;
they are not validated calibration targets. See [normalization](pathnd_qc/normalization/README.md).

### Where the stain name comes from

Metadata is read only when you supply `--metadata PATH`. Without a path, lookup is skipped and
analysis continues. There is no built-in metadata bank or automatic search of accessible files.
`--stain` overrides the stain in a supplied CSV but does not disable reading that CSV.

For a run without metadata, provide the actual stain with `--stain`; `--no_metadata` is optional.
For mixed-stain batches, put each slide's stain in the [batch manifest](pathnd_qc/batch/README.md#choose-the-slides).
Missing or unrecognized stains are recorded in the report; they can change algorithm routing or
prevent normalization. Metadata names are preserved rather than rewritten into one universal name.

To use your own metadata CSV:

```bash
pathnd-qc \
  --slide "/absolute/path/to/slide.svs" --out "./reports" \
  --run_tissue_segmentation \
  --metadata "/absolute/path/to/metadata.csv" --metadata_key slide_paths
```

A minimal metadata file for that example is:

```csv
slide_paths,stain_type
/absolute/path/to/slide.svs,Hirano
```

`--metadata` may be repeated; only those files are read. `--metadata_key` names the
slide-path column; when omitted, a supported column name is detected from the header. This metadata
file is different from a batch manifest: it describes slides for lookup rather than choosing which
slides to run. Lookup matches full slide paths. Use full cloud URIs or absolute local paths in metadata
sources; identical filenames in different folders remain distinct. See [metadata details](pathnd_qc/ingestion/metadata/README.md).

`--bank` selects the bank used for normalization reference lookup. It does not rewrite the dataset
stored in the source metadata record, but supplies it when no dataset was resolved.

## 3. Selecting components

**With no `--run_*` flag, the default component set is selected.** With one or more component flags,
their combined set is requested. In both cases, configuration exclusions apply last.
Required producers are not automatically added for you.

An *artifact* is a reusable input or output, such as a tissue mask or tile list. A component needs
its input artifacts, but they can come from selected producers or files you supply.

| Component flag | What it measures or creates | Required artifacts |
|---|---|---|
| `--run_tissue_segmentation` | Tissue mask | Analysis image |
| `--run_fold_detection` | Fold mask | Analysis image, tissue mask |
| `--run_pen_detection` | Pen-ink mask | Analysis image; also needs model weights |
| `--run_staining_quality` | Mean CIE-Lab chroma (`chroma_mean`) | Analysis image, tissue mask |
| `--run_focus` | Coarse whole-slide focus score | Analysis image, tissue mask |
| `--run_tile_selection` | Tile positions | Tissue mask; also opens the slide for geometry |
| `--run_tile_metrics` | Tissue fraction and focus for each selected tile | Tissue mask, tile list; reads the slide |
| `--run_tile_artifacts` | GrandQC artifact fractions and pen polygons for selected tiles | Tissue mask, tile list; needs the slide and GrandQC |
| `--run_stain_normalization` | Normalized analysis image | Analysis image, tissue mask; needs a reference |

The analysis image is normally read from the slide at 8.0 µm/pixel. It can also be supplied using
`--thumbnail`. Although the flag says thumbnail, it must be the analysis image at the configured
scale, not an arbitrary small preview.

Two flags select groups:

| Group flag | Components selected |
|---|---|
| `--run_thumbnail` | M2: tissue, folds, pen, staining, focus |
| `--run_tiles` | M3: tile selection, tile metrics, tile artifacts |

Neither group includes normalization. `--run_tiles` does not include tissue segmentation: add
`--run_tissue_segmentation` or supply `--tissue_mask`.

Configuration can exclude **any** component, including from explicit flags, Python `components=`
selections and aliases. For example, a `PATHND_CONFIG` file containing:

```json
{"components": {"fold_detection": false, "tile_metrics": false}}
```

removes those two analyses from any requested set. All components default to `true`; omitted keys
keep their defaults. An empty effective selection is an error. If an enabled component needs an
excluded producer's output, supply that artifact or re-enable the producer.
Batch runs inherit the same config; WDLs accept it as `config_file`.
See the [complete switch list](pathnd_qc/config/README.md#enable-or-disable-components).
Replace the retired `m2.pen.enabled` with `components.pen_detection`.

### Fold detector

`fold_detection` unions ConnSoftT with F_line line-fold detection. Hirano/LFB/LFB-H&E spellings
use the d feature; plain H&E/IHC use stain-2. The default F_line threshold is rounded index 200 with
an area floor of 44,800 µm² on the 8 µm/pixel analysis plane. Configure `m2.folds.fline_*` and
`d_path_stains` through `PATHND_CONFIG`; the same file is inherited by batch children or supplied as
workflow `config_file`. No new component flag or model weights are needed.

Reports include branch fractions and diagnostics. An F_line-only failure retains the ConnSoftT mask
and partial measurements, but fails the run with incomplete execution. See [fold detection](pathnd_qc/qc_slide/folds/README.md) for configuration,
API compatibility and validation limits.

### Mask precedence and optional inputs

Computed masks run in the order tissue → folds → pen. Folds are detected from tissue without first
excluding computed pen. Where computed fold and computed pen masks overlap, the fold wins. When one
mask is supplied and the other computed, the supplied mask wins; two supplied masks retain their
overlap. Staining and focus exclude the union of available fold and pen masks.

Missing optional masks can change measurements and are recorded as degradation. A supplied mask is
treated as authoritative for precedence, but that is not a validation of its accuracy.

Avoid selecting a producer and supplying its output in the same command. This produces a warning:
the computed output normally wins. Pen detection can use a supplied pen mask as a recorded degraded
fallback if computing pen fails. Supplied files are still checked even if their results are not used.

## 4. Whole-slide and tile examples

All examples below use the installed commands in your activated environment. Replace `Hirano` with
the actual stain, or replace `--no_metadata --stain ...` with `--metadata /path/to/metadata.csv`.

### Whole-slide QC only

With the default config, this selects all five thumbnail components:

```bash
pathnd-qc \
  --slide "/absolute/path/to/slide.svs" --out "./reports" \
  --run_thumbnail --no_metadata --stain "Hirano"
```

### Tissue and tile measurements without GrandQC

```bash
pathnd-qc \
  --slide "/absolute/path/to/slide.svs" --out "./reports" \
  --run_tissue_segmentation --run_tile_selection --run_tile_metrics \
  --no_metadata --stain "Hirano"
```

### Tile QC including GrandQC

```bash
pathnd-qc \
  --slide "/absolute/path/to/slide.svs" --out "./reports" \
  --run_tissue_segmentation --run_tiles \
  --no_metadata --stain "Hirano"
```

Missing default GrandQC assets are installed automatically unless downloads are disabled.

### All default components

```bash
pathnd-qc \
  --slide "/absolute/path/to/slide.svs" --out "./reports" \
  --no_metadata --stain "Hirano"
```

Review the reference settings; default model assets are prepared automatically.

## 5. Reusing masks and tile lists

There are **five supplyable artifacts**:

| Flag | Accepted format | Meaning |
|---|---|---|
| `--thumbnail` | `.png` or `.npy` | Analysis image, assumed to be at the configured M2 scale |
| `--tissue_mask` | `.png` or `.npy` | Nonempty tissue support mask |
| `--fold_mask` | `.png` or `.npy` | Fold mask; an all-zero mask is valid |
| `--pen_mask` | `.png` or `.npy` | Pen mask; an all-zero mask is valid |
| `--tile_list` | `.json` | Nonempty tile list on the configured tile plane |

Use local files for these inputs. Masks become boolean arrays: nonzero pixels count as selected.
NumPy thumbnails must use `uint8` pixels; floating-point and higher-bit-depth arrays are refused.
Convert using the source's known intensity scale before supplying them. Grayscale and RGBA
thumbnails are converted to RGB. The array contract also applies to the tissue and normalization
APIs. Direct PIL inputs to those APIs are converted to RGB; higher-bit-depth PIL data can be clipped,
so convert deliberately using the source intensity scale before calling them.
A mask with a zero-sized side is invalid. Masks can be resized with nearest-neighbor interpolation
when their aspect ratio matches within the loader's tolerance; the report records the resize.
An incompatible aspect ratio is refused. Reuse masks from the **same slide and full image extent**:
an aspect-ratio match does not establish identity or alignment. Do not supply the 0.50 µm TIFF
output as an 8.0 µm tissue mask; TIFF is not an accepted supplied-mask format.

Reuse a previous tissue mask for fold detection:

```bash
pathnd-qc \
  --slide "/absolute/path/to/slide.svs" --out "./reports" \
  --run_fold_detection \
  --tissue_mask "/absolute/path/to/previous_run/images/slide_tissue_mask.png" \
  --no_metadata --stain "Hirano"
```

To avoid reading the slide pixels for this example, also supply `--thumbnail` with an analysis image
at the configured M2 scale. The CLI still needs `--slide`; a local slide path must exist even on the
supplied-image route. Tile selection or tile reading still requires the slide, despite a supplied
thumbnail. Metadata lookup occurs only for explicitly supplied `--metadata` CSVs.

The pipeline's tile-list JSON includes `plane_mpp`, `plane_dims: [width, height]`, and `tiles`.
For supplied object-form JSON, `plane_dims` is required; `plane_mpp` may be absent or null, in
which case no declared-MPP consistency check is possible.
Each tile has integer `x`, `y`, `w`, and `h`; `col` and `row` are optional integer indices. Coordinates
are on the resampled tile plane, not necessarily scanner level 0. Bounds must fit that plane and
declared geometry is checked against the resolved plane. Duplicate rectangles are removed with a
warning. Older bare arrays are accepted but cannot provide the same scale-consistency check.
Tile width and height cannot exceed the configured tile size (512 pixels by default).

Supplying artifacts **without any component flag still selects the default pipeline**. Supply flags
do not choose the component set.

## 6. Single-slide options

The nine component flags and two groups are listed above. These are the remaining options:

| Option | Meaning |
|---|---|
| `--slide PATH` | Required for a processing run; local WSI or accessible GCS, S3 or Azure object URI |
| `--out DIR` | Parent output directory; default is `report.out_dir`, shipped as `./reports` |
| `--stain NAME` | Stain used for analysis; overrides metadata |
| `--metadata PATH` | Read only these supplied CSVs; repeat for several files; omission skips lookup |
| `--metadata_key COLUMN` | Slide-key column for the supplied metadata CSVs |
| `--no_metadata` | Disable metadata lookup |
| `--bank NAME` | Bank for normalization reference lookup; defaults to metadata dataset |
| `--norm_method macenko` or `--norm_method reinhard` | Normalization method; default from `m4.method` |
| `--pen_weights PATH` | Override the pen model file |
| `--grandqc_repo DIR` | Override the GrandQC inference directory |
| `--grandqc_python PATH` | Python executable for GrandQC; overrides registration/config |
| `--no_model_download` | Require existing model assets; prohibit automatic model downloads |
| `--no_save_artifacts` | Suppress the pipeline's saved masks, tile records, and images; keep the report and status. External backends may still create working files |
| `-q`, `--quiet` | Reduce logging to warnings/errors; final CLI summaries and explicit warning messages still print |
| `--list_components` | Show component inputs and declared outputs, then exit; no slide needed |
| `-h`, `--help` | Show help, then exit |
| `--version` | Print the package version when used alone as `pathnd-qc --version` |

The five supply flags are listed in section 5. There is no single-slide `--force` flag. A repeated
single-slide command creates a new run; `--force` belongs to the batch CLI.

`--metadata_key` requires `--metadata`. `--no_metadata` cannot be combined with either of them.
Configuration overrides use the `PATHND_CONFIG` environment variable, not a `--config` option:

```bash
PATHND_CONFIG="/absolute/path/to/settings.json" \
  pathnd-qc \
  --slide "/absolute/path/to/slide.svs" --out "./reports" \
  --run_tissue_segmentation --no_metadata --stain "Hirano"
```

The settings file must already exist. See [configuration](pathnd_qc/config/README.md) for its format.

## 7. Resolution and downloads

M1 decides one effective physical scale for a slide. With the default settings:

- Stated horizontal and vertical pixel scales must both be usable. Sub-floor scales can be accepted
  when both are corroborated by the objective-power check.
- Otherwise, a usable scale derived from `10 / objective_power` is used and recorded as derived.
- No usable scale means the M2 plane read is refused; M3 cannot proceed. A supplied analysis image
  can still support M2/M4 because it bypasses that read.
- A usable scale coarser than 0.55 µm/pixel skips M3. M2/M4 can still run.

These acquisition decisions are separate from optional report-quality thresholds. Setting every
report threshold to `null` does **not** disable resolution checks or tile tissue filtering.

M2/M4 use an image targeting 8.0 µm/pixel; M3 tiles target 0.50 µm/pixel. Image dimensions are integers,
so achieved scales can differ slightly from targets. The report records the actual read plan.

For remote slides, tile-reading components normally download one local copy before reading tiles.
An M2/M4 run normally streams a single selected pyramid level. If that level exceeds
`m2.read.max_read_px` (67,108,864 pixels by default), the pipeline downloads first and reads it in
bands. This includes some shallow-pyramid and single-level slides. Local slides use the same banded
read when needed, without a download. The cap is a source-read setting, not a total process-RAM limit.

Before tile localization, the pipeline tries to inspect the streamed header and apply the MPP gate.
A remote header that cannot be streamed falls back to localization for requested tile reading.
Analysis output planes also have a `m2.read.max_plane_px` cap (67,108,864 pixels). Packed tissue masks
have a `m3.tile_metrics.max_mask_bytes` cap (536,870,912 bytes). Exceeding the mask cap preserves tile
measurements and reports the missing artifact. These limits do not cover all working arrays or the
sum of concurrent batch workers; choose worker counts for the machine's memory.

Downloads have bounded retries and an inactivity timeout. A detected size/checksum mismatch fails
verification and is never used. If source metadata is unavailable, verification can be explicitly
unavailable rather than successful. See [reader behavior](pathnd_qc/ingestion/wsi_reader/README.md).

## 8. Output files

The single-slide default destination is `./reports` relative to the launch directory. Batch commands
require `--out`. Open the run's `index.html` in a browser; the CLI prints its `browse` path.

All batch outputs live together in `<out>/batch_<batch_id>/`. With `--slide_dir /data/slides`,
paths are mirrored **relative to that input folder**, inside the batch:

```text
Input: /data/slides/                    Output: /qc/batch_<batch_id>/
├── case_1/                            ├── case_1/
│   ├── slide_A.svs                     │   ├── slide_A_output/
│   └── region/                        │   └── region/
│       └── slide_B.tif                 │       └── slide_B_output/
└── case_2/                            └── case_2/
    └── slide_C.svs                        └── slide_C_output/
```

With `--slides slides.txt` (or a CSV/TSV manifest), each slide's folder goes directly inside its
batch, regardless of where the source slide lives:

```text
out/
├── README.txt
└── batch_<batch_id>/
    ├── index.html                    # open this to browse the batch
    ├── summary.csv                   # measurements from this batch's saved runs
    ├── summary.json
    ├── results.csv                   # one outcome per job in the latest invocation
    ├── results.json
    ├── batch.json
    ├── progress.json
    ├── batch.log
    ├── README.txt
    ├── slide_A_output/
    │   ├── slide.json                # source identity; prevents mixing different slides
    │   ├── 2026-09-18_14-30-00_UTC/   # all results from one run
    │   │   ├── index.html            # links, execution notes, version and Git commit
    │   │   ├── slide_A_report.json
    │   │   ├── slide_A_status.json
    │   │   ├── images/               # masks, review overlays and normalized image
    │   │   └── data/                 # tile geometry and measurements
    │   └── 2026-09-19_09-00-00_UTC/   # reruns preserve earlier results
    ├── slide_B_output/
    └── slide_C_output/
```

Single-slide commands retain `<out>/<slidename>_output/<UTC date>/` without a batch wrapper.
Give `--batch_id study01` to create `batch_study01`; reuse that ID to resume inside it. Omitting the
ID uses the current UTC date/time in compact form, for example `batch_20260923_143000`; same-second name
collisions add `_2`, `_3`, etc. A new ID starts an independent batch. Batch summaries exclude other
batches; `summarize --out <out>` can
still produce a combined table across all batches. A `.batch.lock` and per-job run tracking JSON
files also live inside the batch directory.

`<slidename>` is the filename without its extension. Only branches containing discovered slides
are mirrored, and only artifact categories with saved files are created. When directory and list
inputs are combined, a slide found in the directory keeps its mirrored location; list-only slides
use `sources/<path-sha256>/` beneath the batch root. This SHA-256 is derived from each source's
full canonical path or cloud URI, so same-named slides stay separate and subset reruns keep their
output locations. Slide IDs and report filenames retain their original names. Existing
`<slidename>_output` folders reject a different source path; do not remove `slide.json`.

Dates are UTC and same-second collisions get a numeric suffix. Inspect returned paths rather
than predicting a timestamp. Changing between mirrored directory discovery and a list changes
placement and the resume fingerprint. An output folder inside the input tree is excluded
from CLI discovery; `--out` cannot equal or be an ancestor of `--slide_dir`.

Relative links in the HTML pages work after copying the batch folder or extracting a workflow
archive. Historical flat runs, the earlier `slides/<id>__<hash>/<date>` layout and unwrapped mirrored
runs remain readable by summaries and artifact reuse; they are not moved into new batches or used
to skip work there. Filesystem errors can prevent report or status
writing; the program cannot guarantee a report when the output location is unusable.

| File in the run folder | Contents / when written |
|---|---|
| `index.html` | Offline contents page, execution notes and version identity; best effort |
| `<slide_id>_report.json` | Measurements, errors, skipped sections, settings, and provenance |
| `<slide_id>_status.json` | Best-effort lifecycle marker: `running`, then `completed` or `failed` |
| `images/<slide_id>_tissue_mask.png` | Saved computed tissue mask |
| `images/<slide_id>_fold_mask.png` | Saved computed fold mask |
| `images/<slide_id>_pen_mask.png` | Saved computed pen mask |
| `data/<slide_id>_tile_list.json` | Selected tile rectangles and plane geometry |
| `data/<slide_id>_tile_records.json` | Detailed records for the selected tiles |
| `images/<slide_id>_tissue_mask_20x.tif` | Tiled, pyramidal tissue mask on the tile plane |
| `images/<slide_id>_blur_overlay.png` | Tile focus heatmap; unmeasured tiles are left unpainted |
| `images/<slide_id>_artifact_overlay.png` | GrandQC class overlay, when an artifact map is available |
| `images/<slide_id>_normalized.png` | Normalized analysis image |

Artifacts depend on the requested components and successful computation/saving; requesting a
component does not guarantee its file exists. The `20x` filename is historical: the configured tile
MPP defines the physical scale. Higher focus scores indicate stronger measured image variation;
heatmap colors are not validated clinical blur labels. No standalone thumbnail is automatically saved.

A successful full run with selected tiles normally writes two files in `data/`: the tile list
and the tile records. Empty or skipped tile passes may write fewer files. All generated masks,
overlays and normalized images share `images/`. Earlier `masks/` and flat artifact locations remain
supported for reuse; existing results are not moved.

The pipeline's artifact/report writers use temporary sibling files and replacement. Independent
artifact saves continue after one fails; successful files are retained and save failures reach the
report when it can be written. Failed runs may therefore still have useful reusable outputs.

## 9. Reading a report

Read these fields together:

| Field | How to use it |
|---|---|
| `error` | A run-level failure, or `null` when none was recorded |
| `provenance.execution.complete` | Whether all requested work finished without recorded incompleteness |
| `provenance.execution.reasons` | Recorded execution observations and omissions |
| `provenance.components_failed` | Reached components with recorded errors |
| `verdict` | Comparisons against the configured report thresholds |
| `m1.ingestion.checks` and `.decision` | Acquisition and sampled integrity results, including any quarantine recommendation |

A handled component error or incomplete requested work fails the run: the report has a run-level
error, status is `failed`, the CLI exits `1`, and Python raises `RunFailed`. Partial outputs remain
available. Batch requests can be retried with `--retry_failed`. A quarantine
recommendation in the ingestion record is not an automatic filesystem move or whole-pipeline stop;
the orchestrator separately applies the MPP decisions described in section 7.

Schema `2.0` promotes ingestion quarantine, metadata-source failures, scale disagreements and
unavailable transfer verification into `execution.reasons`. Observations can coexist with completed
computation. The implementation and schema versions are code-owned; see [migration notes](pathnd_qc/MIGRATION.md).

### Which pipeline revision produced these values?

Read `provenance.git_commit` for the full commit hash. It is collected automatically from the
pipeline checkout before slide processing, including when the clone lives on another machine or
the command is launched elsewhere. Installed wheels retain the revision recorded during their build.

Also compare `git_dirty`, `source_sha256` and `config_sha256`: local edits or different settings can
produce different results at the same commit. `git_source` identifies whether the revision came from
the checkout or package build; an unavailable commit is `null`. These fields also appear in batch
summaries. See [pipeline revision details](pathnd_qc/reporting/README.md#pipeline-revision-and-reproducibility)
for the fingerprint scope and handling of source copies without Git history.

### Empty values and skipped work

`null` means no value is available; it does not mean zero. A component that did not run keeps a
section with `runtime_s: 0.0` and `error: "skipped: ..."`. Skipped sections need not include
`error_type`. Reached component reports generally include `error` and `error_type`; the latter is an
exception class or a general failure label, not a complete transient/permanent error taxonomy.

`components_run` means a component produced a non-skipped result, even if that result contains an
error. `components_not_reached` names work prevented by an earlier run failure; `components_gated`
names requested work skipped by a decision. `components_skipped` names components not requested.

### Threshold verdict

With the shipped null bounds:

```json
{"passed": null, "flags": [], "n_thresholds_checked": 0}
```

After you configure bounds, `flags` can identify out-of-range or unavailable metrics and invalid
bounds. `n_thresholds_checked` counts evaluated numeric metrics, including rejected non-finite
measurements. Invalid bounds are not applied. `passed` remains null if no metric was evaluated;
it can be false when evaluated metrics fail or have flags. Report thresholds flag results;
they do not themselves reject slides. Bounds require validation for the intended stain and use case.

### Where to find measurements

| Section | Main contents |
|---|---|
| `m1.ingestion.source` | Dataset, slide ID, participant, region, stain, source path |
| `m1.ingestion.source_resolved_from` | Origin of each resolved source field |
| `m1.ingestion.metadata` | Standard metadata, additional fields, source-file details |
| `m1.ingestion.acquisition` | Raw scanner fields and effective MPP/source |
| `m1.localize` | Download attempts and available verification details |
| `m2.read` | Computed, supplied, or refused image-read information |
| `m2.tissue`, `.folds`, `.pen`, `.staining`, `.focus` | Whole-slide component results |
| `m3.status`, `.reason`, `.tile_plane` | Tile-resolution decision and geometry |
| `m3.tiles` | Tile counts, tissue/focus aggregates, read failures, outliers |
| `m3.artifacts` | Artifact backend, classes, counts and status |
| `m3.cascade` | An explicit skip for the separate chunk-refinement pass, which is not in the current pipeline flow |
| `m4.stain_norm` | Normalization method, reference, fitted parameters, placeholder/error status |
| `provenance.inputs`, `.outputs` | Artifact paths and available hashes/shape details |
| `provenance.external_backends` | Selected backend paths/interpreter, available commit, and code/weight hashes |
| `provenance.config_sources`, `.config_sha256` | Loaded settings files and resolved configuration hash |
| `provenance.components_disabled_config` | Components excluded by config, even if explicitly requested |
| `provenance.stain_routing`, `.degraded`, `.warnings` | Routing and conditions that affect interpretation |

Detailed tissue/focus tile records live in their own JSON file. Per-tile GrandQC fractions and
polygons are available in Python return values but are not saved by the CLI; its artifact outputs
are the aggregate report and overlay. The outlier list combines limited groups of low-focus and
dropped tiles with all tile read/resegmentation errors, so it can exceed the configured per-group
focus-outlier count.

Dimension order currently differs between fields:

| Field | Order |
|---|---|
| Supplied image/mask and saved image/mask `dims` | `[height, width]` |
| Computed `provenance.inputs.thumbnail.dims` | `[width, height]` — a current implementation inconsistency |
| Reader/tile-list `plane_dims` | `[width, height]` |

Check the field and the thumbnail's `source` before interpreting nonsquare dimensions; do not assume
all `dims` arrays share one order.

Generated and ingested timestamps have UTC and local-offset forms. Status/batch timestamps are not
all accompanied by local twins. `timing.components` includes component durations plus explicitly
recorded ingestion/read costs; `timing.stages` groups them. `total_s` is measured inside `run()` before
final report assembly/writing, so it excludes initial Python imports and final output overhead.
`unattributed_s` is the difference within that measured interval, not total shell startup time.

Reports retain diagnostic tracebacks, input paths, and metadata. No separate BDSA export or redaction
view is implemented. Further detail is in the [reporting guide](pathnd_qc/reporting/README.md).

## 10. Batch processing

Run up to four slides concurrently:

```bash
pathnd-qc-batch run \
  --slide_dir "/absolute/path/to/slides" --out "./reports" --batch_id study01 --workers 4 \
  --run_tissue_segmentation --no_metadata --stain "Hirano"
```

Choose the actual stain for a same-stain folder. For mixed stains use a manifest or metadata lookup.
The folder is searched recursively. Each worker gets at most available CPUs divided by workers
(minimum one), with smaller caller thread limits preserved. For 32 CPUs and eight workers this
is four threads each. The chosen limit is saved in `batch.json`. More workers also require more
memory and disk; four slides do not require four workers.

Repeat the same command, including `--out` and `--batch_id study01`, to resume. Matching completed
requests are skipped; add `--retry_failed` to retry failed ones or `--force` to repeat completed
work. Omitting `--batch_id` creates a fresh batch every time. A failed slide is recorded and the
batch continues with other slides.

Preview discovery without running slide analysis:

```bash
pathnd-qc-batch list \
  --slide_dir "/absolute/path/to/slides" --out "./reports" \
  --run_tissue_segmentation --no_metadata --stain "Hirano"
```

`list` shows jobs, template problems, and the first command. It is **not full preflight validation**
of every child command or slide.

For a second tile pass using masks from completed runs in the same output directory:

```bash
pathnd-qc-batch run \
  --slide_dir "/absolute/path/to/slides" --out "./reports" --workers 2 --force \
  --run_tiles --tissue_mask "{latest_artifact}" \
  --no_metadata --stain "Hirano"
```

This needs a saved tissue mask for each slide in its newest completed run. `{latest_artifact}` does not
search backwards for a particular artifact. GrandQC uses automatic setup when its default assets are missing. For only tile
measurements, replace `--run_tiles` with `--run_tile_selection --run_tile_metrics`.

Batch commands share the component flags, aliases, and documented forwarded options, but not every
single-slide option. Use `--quiet` rather than `-q` in batch commands, `--slide_dir`/`--slides` rather
than `--slide`. Batch `list` previews jobs; use the single-slide `--list_components` command to
list available analyses. Batch `--out` is required.
See the [batch guide](pathnd_qc/batch/README.md) for the complete option table, manifests, and resume behavior.

## 11. Troubleshooting

| What you see | What to check |
|---|---|
| `No module named pathnd_qc` | Install the wheel or checkout into the interpreter you are using; activate that environment |
| `No module named ...` for a dependency | Install the complete package into the interpreter used by the command |
| Missing required tissue mask or tile list | Select its producer or supply that artifact explicitly; a component flag does not add its producers |
| Pen model error | Check the configured weights path; use an absolute `--pen_weights` path when needed |
| GrandQC setup error | Run `pathnd-qc setup grandqc`, or provide a complete checkout and checkpoints |
| Stain unresolved or not recognized | Check spelling and metadata access; provide the actual stain for a local slide |
| M3 rejected by MPP | Read `m1.ingestion.checks.mpp`; do not assume an objective label makes the pixel scale valid |
| A second batch does no work | Only matching request fingerprints skip completed slides. Changed components/settings schedule new work; use `--force` to repeat an unchanged request |
| Conflicting manifest values | Resolve inconsistent metadata for the same slide, including symlink aliases |
| No `{latest_run}` or missing mask | Check the same output directory and canonical slide path; the newest completed run must contain that artifact |
| Run failed with partial results | Inspect execution reasons and component errors; retained measurements remain accessible |
| `--force` rejected by `pathnd-qc` | That option is batch-only |

Single-slide exit codes are 0 when all selected work completes, 2 for a refused command, and 1 for a run or
unexpected failure. Preflight failures do not create a slide report, although an empty output parent
may already have been created. After processing begins, report writing is attempted even on failure.
Batch outcomes and exit codes are explained in the [batch guide](pathnd_qc/batch/README.md#scheduling-and-batch-outputs).
