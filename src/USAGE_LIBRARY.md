# Path-ND QC — Python library usage

[Package overview](pathnd_qc/README.md) · [CLI guide](USAGE_CLI.md)

Use this guide for Python scripts and notebooks. `run()` processes one slide and returns its
measurements and report. Component functions let you work directly with images, masks and tiles.

- [Install](#install)
- [Run one slide](#run-one-slide)
- [Choose components and models](#choose-components-and-models)
- [Configure before importing](#configure-before-importing)
- [Supply metadata or saved artifacts](#supply-metadata-or-saved-artifacts)
- [Read results and handle failures](#read-results-and-handle-failures)
- [Read images and call components](#read-images-and-call-components)
- [Run a batch](#run-a-batch)
- [API reference map](#api-reference-map)

## Install

### Directly from GitHub

With Python, pip and Git installed, run this from any directory in your chosen Python environment:

```bash
python -m pip install "git+https://github.com/The-10-000-Brains-Project/path-nd-qc-pipeline.git#subdirectory=src"
pathnd-qc --help
```

Pip downloads the repository and installs the package and its declared Python dependencies.
You do not need to clone the repository manually. `#subdirectory=src` tells pip where the Python
project lives. A private repository requires GitHub access and authentication. The command uses
the default branch on GitHub; unpushed local changes are not included.

This does not require PyPI publication. A bare `pip install pathnd-qc` requires the package to be
published to your chosen package index first.

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

### From a checkout or wheel

If you already have a checkout, run from the directory containing `pyproject.toml`
(`src/` in this workspace), in your chosen environment:

```bash
python -m pip install .
```

Or install a supplied wheel from any directory:

```bash
python -m pip install /path/to/pathnd_qc-0.5.0-py3-none-any.whl
```

For notebooks, select the same interpreter as your kernel; install `ipykernel` into it if needed.
The package requires Python 3.12+. Native Windows needs Python 3.13+ and additional symlink
permissions for GrandQC, and has not been validated end to end. Read the
[platform requirements](USAGE_CLI.md#platform-support) before choosing an environment.

Pip installs inference dependencies but does not download model checkpoints. The first example
uses tissue segmentation, which needs no external model weights. Selected pen and GrandQC assets are installed automatically on first use.

## Run one slide

Replace the path and stain with your actual data:

```python
from pathnd_qc import run

result = run(
    "/data/slides/example.svs",
    out_dir="reports",
    stain_type="Hirano",
    metadata_enabled=False,
    components={"tissue_segmentation"},
)
print(result["report_path"])
print(result["report"]["provenance"]["execution"])
```

Open `index.html` inside `result["out_dir"]`. Each saved run gets a new dated folder under
`reports/<slidename>_output/`; use returned paths instead of constructing them yourself.

`slide_path` also accepts `gs://`, `s3://`, `az://`, `abfs://` and `abfss://` object URIs.
Set provider credentials in the process environment before opening a slide; see the
[cloud reference](USAGE_CLI.md#cloud-slides-gcs-aws-s3-and-azure). Output folders are local.

## Choose components and models

`components=None` selects every enabled component: all nine by default. A nonempty set requests
only those names. Config exclusions apply last; an empty effective selection is an error.
Input producers are not added automatically.

| Component name | Inputs beyond the slide/analysis image |
|---|---|
| `tissue_segmentation` | None |
| `fold_detection` | Tissue mask |
| `pen_detection` | Compatible pen checkpoint |
| `staining_quality` | Tissue mask; available folds/pen are excluded |
| `focus` | Tissue mask; available folds/pen are excluded |
| `tile_selection` | Tissue mask and slide geometry |
| `tile_metrics` | Tissue mask, tile list and slide pixels |
| `tile_artifacts` | Tissue mask, tile list, slide and GrandQC |
| `stain_normalization` | Tissue mask and normalization reference |

For tissue and tile measurements, set:

```python
components = {"tissue_segmentation", "tile_selection", "tile_metrics"}
```

Pass that set to `run(components=components, ...)`. For all nine, omit `components`.
Missing default pen and GrandQC assets are downloaded, checked and registered automatically;
components you do not select need no setup. Later calls reuse the managed files.

To prohibit automatic model downloads, pass `download_models=False` to `run()`, or set
`os.environ["PATHND_NO_MODEL_DOWNLOAD"] = "1"` before the call. Existing models still run;
missing required assets raise an error. This does not disable analyses or slide downloads.

If the pen download needs a browser, obtain a compatible checkpoint and register it with
`pathnd-qc setup pen --weights /absolute/path/to/pen.pt`.
Review the stain's normalization target first:
[create a reference configuration](pathnd_qc/normalization/README.md#create-a-reference-configuration).
The bundled targets are placeholders. See [model setup](pathnd_qc/external/README.md) for custom paths,
checksums and separate inference environments. Per-call overrides are `pen_weights`,
`grandqc_repo` and `grandqc_python`.

## Configure before importing

Save a partial override, for example `settings.json`:

```json
{
  "components": {"stain_normalization": false},
  "m2": {"read": {"target_mpp": 8.0}}
}
```

Set the environment variable **before importing `run` or any component modules**:

```python
import os
os.environ["PATHND_CONFIG"] = "/absolute/path/to/settings.json"

from pathnd_qc import run
```

Several algorithm defaults are bound during import. In a notebook, restart the kernel after
changing configuration and run this setup first. Reloading the configuration alone does not
rebind those imported defaults. There is no `config=` argument to `run()`.
See [configuration precedence and validation](pathnd_qc/config/README.md).

## Supply metadata or saved artifacts

For metadata lookup, supply only the files you want read:

```python
from pathnd_qc import run

result = run(
    "/data/slides/example.svs", out_dir="reports",
    components={"tissue_segmentation"},
    metadata_paths=["/data/metadata.csv"], metadata_key="slide_paths",
)
```

A minimal CSV contains `slide_paths,stain_type`. Lookup uses full slide paths, so same-named slides in separate folders stay distinct.
Use full cloud URIs or absolute local paths in the CSV. `stain_type=` overrides a metadata stain. Omitting metadata paths skips lookup;
`metadata_enabled=False` is an explicit opt-out and cannot be combined with metadata paths/key.
`bank=` selects a normalization reference and fills the dataset when metadata leaves it empty.

To reuse a tissue mask from this same slide:

```python
result = run(
    "/data/slides/example.svs", out_dir="reports", stain_type="Hirano",
    components={"fold_detection"},
    supplied={"tissue_mask": "/data/previous_run/images/example_tissue_mask.png"},
)
```

`supplied` maps `thumbnail`, `tissue_mask`, `fold_mask`, `pen_mask`, or `tile_list` to file paths,
not in-memory arrays. Use `.png`/`.npy` for images and masks, and `.json` for tiles. The analysis
image must have the configured scale; masks must represent the same slide and full image extent.
See the [complete artifact contract](USAGE_CLI.md#5-reusing-masks-and-tile-lists).

## Read results and handle failures

| Return key | Contents |
|---|---|
| `report` | JSON-compatible measurements, execution status, settings and provenance |
| `report_path`, `out_dir` | Saved report and run folder, or `None` when not written/created |
| `outputs` | Saved artifact paths |
| `plan` | Selected work, inputs and dependency planning |
| `thumbnail` | Analysis image when available |
| Component keys such as `tissue`, `folds`, `artifacts`, `stain_norm` | In-memory component results, including arrays/images where applicable |

For a report without saved masks/images, use `save_artifacts=False`. To suppress pipeline report
and artifact publication, use **both** `write=False, save_artifacts=False`. Slide downloads and
external inference may still use temporary files. The full return value contains Python/image
objects; serialize `result["report"]` rather than the entire return value.

Selected work must complete. Handle setup errors separately from a processing failure:

```python
from pathnd_qc import RunFailed, run

try:
    result = run(
        "/data/slides/example.svs", out_dir="reports", stain_type="Hirano",
        components={"tissue_segmentation"},
    )
except RunFailed as exc:
    print("Slide failed:", exc.failure["message"])
    print("Report destination:", exc.report_path)
    partial_report = exc.report
except ValueError as exc:
    print("Request or setup error:", exc)
```

`RunFailed` retains the in-memory report and attempted report path. Disk errors can prevent that
file being written. Preflight errors have no slide report. A successful return means selected
work completed; it does not establish scientific acceptance. Read `provenance.execution`,
component errors and configured threshold flags together. A missing value is not zero.
See [report fields](USAGE_CLI.md#9-reading-a-report) and [report provenance](pathnd_qc/reporting/README.md).

## Read images and call components

Direct calls are useful for notebooks and custom processing. They do not create the pipeline's
complete report or perform all of its orchestration, acquisition gates and mask precedence.
Automatic model setup belongs to `run()`; direct pen and GrandQC calls need usable model paths
as described in the [pen](pathnd_qc/qc_slide/pen/README.md#usage) and
[artifact](pathnd_qc/qc_tile/artifacts/README.md#usage) guides.
Check each function's returned error field before using its result:

```python
from pathnd_qc import WSIReader
from pathnd_qc.qc_slide.tissue.tissue import compute_tissue_mask

reader = WSIReader()
with reader.slide("/data/slides/example.svs") as slide:
    image, read_info = reader.read_at_mpp(slide, 8.0)
if image is None:
    raise RuntimeError(f"Analysis image unavailable: {read_info}")
tissue = compute_tissue_mask(image, stain_type="Hirano")
if tissue.get("tissue_error"):
    raise RuntimeError(tissue["tissue_error"])
mask = tissue["tissue_mask"]
```

Use uint8 RGB for NumPy image inputs. Check the [reader contract](pathnd_qc/ingestion/wsi_reader/README.md)
for physical scale, streaming and downloads, and each module guide for its masks and return fields.
For normalization, [fit and save a target](pathnd_qc/normalization/README.md#create-a-reference-configuration)
or call `freeze_slide()` once and reuse those source parameters across your own tiles.

## Run a batch

The batch runner starts a separate interpreter for each slide, so the installed package and model
assets must be accessible to that interpreter; missing defaults are set up automatically.
Include `"--no_model_download"` in `run_args` to prohibit model downloads. It records failed slides and continues with others.

```python
from pathnd_qc import run_batch
from pathnd_qc.batch import manifest

rows = manifest.discover(slide_dir="/data/slides", exclude_dir="reports")
jobs = manifest.build_jobs(rows, out_dir="reports")
record = run_batch(
    jobs, "reports", workers=2, batch_id="study01",
    run_args=["--run_tissue_segmentation", "--no_metadata", "--stain", "Hirano"],
)
print(record["summary_paths"])
```

Repeat the same `out_dir`, `batch_id` and request to skip matching completed runs. Use
`retry_failed=True` for failures or `force=True` to repeat completed work. Omitting `batch_id`
creates a new batch. Use a manifest with per-slide stains for mixed inputs. Python callers own
their signal policy; see [batch scheduling and recovery](pathnd_qc/batch/README.md#scheduling-and-batch-outputs).

## API reference map

| Area | Detailed guide |
|---|---|
| Slide sources, metadata and acquisition | [Ingestion](pathnd_qc/ingestion/README.md) |
| Tissue, folds, pen, staining and focus | [Slide QC](pathnd_qc/qc_slide/README.md) |
| Tile selection, measurements and GrandQC | [Tile QC](pathnd_qc/qc_tile/README.md) |
| Reference fitting and normalization | [Normalization](pathnd_qc/normalization/README.md) |
| Reports and thresholds | [Reporting](pathnd_qc/reporting/README.md) |
| Discovery, manifests and scheduling | [Batch](pathnd_qc/batch/README.md) |

For settings, assets and compatibility changes, see [configuration](pathnd_qc/config/README.md),
[model setup](pathnd_qc/external/README.md), and [migration notes](pathnd_qc/MIGRATION.md).
