# Path-ND QC — source project

This directory is the standalone Python project. It contains everything needed to build the
library and its workflow deployment files; the parent workspace is not required.

## Choose your guide

| Use case | Guide |
|---|---|
| Python scripts and notebooks | [Library usage](USAGE_LIBRARY.md) |
| Terminal commands and local batches | [CLI usage](USAGE_CLI.md) |
| Verily workflow jobs | [Verily usage](USAGE_VERILY.md) |
| Package and algorithm overview | [Package README](pathnd_qc/README.md) |
| Container builds and release preparation | [Deployment guide](deploy/verily/README.md) |

[Install](#install) · [Find a component](#find-a-component) · [Project files](#project-files) ·
[Build and freeze](#build-and-freeze) · [Release checks](#release-checks)

## Install

With Python 3.12+ in an activated environment, run from this directory:

```bash
python -m pip install -e .
pathnd-qc --help
pathnd-qc-batch --help
```

From the repository root, use `python -m pip install -e ./src`. Editable installation makes
source changes available without reinstalling. For CPU-only Linux/Windows environments, follow
the [Torch/torchvision installation steps](USAGE_CLI.md#cpu-only-linux-and-windows) after installing the package.
For native Windows, Python 3.13+ and further restrictions apply; see the
[platform requirements](USAGE_CLI.md#platform-support). Native Windows has not been
validated end to end. Local checks cover macOS on Apple Silicon and the Linux amd64 image.

All nine analysis components are enabled by default, including pen and GrandQC. Pip installs their
Python dependencies; the first run automatically obtains missing selected model assets.
Use `--no_model_download` to require existing assets only. See [model setup](pathnd_qc/external/README.md).
Normalization uses configured references; the shipped targets are placeholders. For a first run
without model setup, use the [tissue-only example](USAGE_CLI.md#first-run).

## Find a component

| Area | Guide |
|---|---|
| Sources, metadata, reader and integrity checks | [Ingestion](pathnd_qc/ingestion/README.md) |
| Tissue, folds, pen, staining and focus | [Slide QC](pathnd_qc/qc_slide/README.md) |
| Tiles, per-tile measurements and GrandQC | [Tile QC](pathnd_qc/qc_tile/README.md) |
| Stain normalization and reference fitting | [Normalization](pathnd_qc/normalization/README.md) |
| Settings and component switches | [Configuration](pathnd_qc/config/README.md) |
| Batch scheduling and resume | [Batch](pathnd_qc/batch/README.md) |
| Report fields, thresholds and identity | [Reporting](pathnd_qc/reporting/README.md) |

Slides and explicit metadata can be local or on GCS, S3 or Azure; see
[cloud inputs](USAGE_CLI.md#cloud-slides-gcs-aws-s3-and-azure).
Open each run or batch's `index.html` to browse its results. Reuse the same output directory and
`--batch_id` to resume a batch; omitting the ID creates a new one. See
[output layout](USAGE_CLI.md#8-output-files).

## Project files

Paths below are relative to `src/` in the repository, or to the project root when this directory
is supplied on its own. The runtime's individual modules are mapped in
[package files](pathnd_qc/README.md#package-files).

| Item | Purpose |
|---|---|
| [README.md](README.md) | Source overview, file inventory, installation and development instructions |
| [USAGE_LIBRARY.md](USAGE_LIBRARY.md), [USAGE_CLI.md](USAGE_CLI.md), [USAGE_VERILY.md](USAGE_VERILY.md) | Usage guides for the three interfaces |
| [pathnd_qc/](pathnd_qc/README.md) | Runtime package, component guides, defaults and model installer |
| `external/` (`src/external/` in the workspace; development checkout only) | Optional local model assets and compatibility wrapper; see [model locations](pathnd_qc/external/README.md#model-locations). Excluded from distributions |
| [deploy/verily/](deploy/verily/README.md#files-in-this-directory) | WDLs, containers, deployment helpers and input examples |
| `tests/` (repository checkout only) | Regression suites and synthetic fixtures; see the [test guide](https://github.com/The-10-000-Brains-Project/path-nd-qc-pipeline/blob/main/src/tests/README.md) |
| [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) | Terms and attribution for GrandQC, WSISegQC and SEA-AD material |
| [pyproject.toml](pyproject.toml) | Build metadata, dependencies and installed commands |
| [requirements.txt](requirements.txt) | Installs this project with the dependencies in `pyproject.toml`; not a lockfile |
| [MANIFEST.in](MANIFEST.in) | Source-distribution contents |
| [build_hooks.py](build_hooks.py) | Embeds implementation provenance in distributions |
| [package-freeze.json](package-freeze.json) | Hashes of shipped runtime files and documentation |
| [compose.yaml](compose.yaml) | Local container execution using a prepared snapshot |
| `run.py` (development checkout) | Checkout entry point for the single-slide CLI |
| `batch_run.py` (development checkout) | Checkout entry point for the batch CLI |
| `.gitignore` (development checkout) | Project version-control exclusions |
| `dist/` | Generated wheels, source archives and prepared release bundles |
| `build/`, `pathnd_qc.egg-info/`, `__pycache__/` | Generated packaging intermediates, metadata and Python caches |

Generated directories may be absent in a clean checkout. They are outputs, not files to edit.
Items marked development checkout are excluded from release distributions.

## Build and freeze

The freeze manifest records the shipped package, including its guides. Intentional changes to
tracked files require corresponding hash updates after review. Preparing a release verifies the
complete inventory before building:

```bash
python -m pip install 'setuptools>=77'
python deploy/verily/prepare_release.py --check
python deploy/verily/prepare_release.py --out dist/deployment/0.5.0-release
```

Choose a fresh output directory; existing snapshots are never overwritten. The helper creates a
matching wheel, source archive, pinned WDLs, input examples and container context. See the
[deployment guide](deploy/verily/README.md) for image builds, Compose and publishing.
Existing prepared releases retain their original documentation and checksums.

For ordinary distribution builds, use `python -m pip install build`, then `python -m build`.
Artifacts are written to `dist/`. Building does not publish to a package index. A separate
`setup.py` is unnecessary. Builds embed the checkout's source identity; see
[report provenance](pathnd_qc/reporting/README.md#pipeline-revision-and-reproducibility).

## Release checks

Run `python deploy/verily/prepare_release.py --check` from this directory to verify the shipped
package against its freeze manifest. The [deployment guide](deploy/verily/README.md#local-verification)
also covers WDL validation. These checks work from the standalone source project.
For checkout regression suites, follow the [test guide](https://github.com/The-10-000-Brains-Project/path-nd-qc-pipeline/blob/main/src/tests/README.md).

The CLI, library and workflows do not need the development workspace's tests, review records or
research fixtures. See [deployment validation](deploy/verily/deployment-status.md) for recorded
test scope and limits.
