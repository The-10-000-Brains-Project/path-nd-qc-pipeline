# Pipeline models and automatic setup

[Package overview](../README.md) · [CLI usage](../../USAGE_CLI.md) · [Library usage](../../USAGE_LIBRARY.md)

[Model locations](#model-locations) · [Managed setup](#managed-setup) · [Use your own version](#use-your-own-version) · [Give GrandQC its own Python environment](#give-grandqc-its-own-python-environment) · [Compatibility contract and provenance](#compatibility-contract-and-provenance)

Path-ND QC installs Python dependencies through pip. A pipeline run automatically obtains,
checks and registers missing default assets for selected pen and GrandQC components. Existing
installations are reused. Imports and pip installation do not download model weights.

## Model locations

Normal installed use needs no repository asset folder. The pipeline downloads missing selected
models to [user data storage](#managed-storage) and remembers their paths.

| Location | Purpose |
|---|---|
| `src/external/` in a development checkout | Optional local checkpoints, their checksum record, and a setup wrapper; omitted from wheels and source archives |
| `src/pathnd_qc/external/` in the repository; `pathnd_qc/external/` after installation | This packaged installer, download catalog and compatibility patches; contains no model weights |
| User data directory, or `PATHND_DATA_DIR` | Downloaded models, managed GrandQC checkouts and registration used by installed runs |

The literal configuration defaults `external/weights/pen.pt` and
`external/grandqc/repo/01_WSI_inference_OPENSLIDE_QC` are resolved against the working directory.
From `src/`, they point into the checkout asset folder. A GrandQC inference checkout is created
by setup; its fallback path need not exist in a fresh clone. Registered model paths override these
defaults. For local assets, registering absolute paths avoids depending on the launch directory.
See [custom registration](#use-your-own-version); checkout-specific commands are in
`src/external/README.md` when working from the repository.

## Managed setup

No separate setup command is needed for a normal CLI, Python `run()` or batch run. The first
model-dependent run needs network access and Git for GrandQC. Downloads and registration share a
setup lock and atomic publication so concurrent runs do not publish competing installations.
A batch prepares shared models once in the parent before starting timed slide workers. If that
setup fails, no workers start; fix the reported setup problem and rerun the batch.

To prohibit automatic model downloads:

| Interface | Control |
|---|---|
| CLI or batch | `--no_model_download` |
| Python `run()` | `download_models=False` |
| Process environment | `PATHND_NO_MODEL_DOWNLOAD=1` |
| WDL/adapter request | `no_model_download: true` |

These controls still allow existing assets and do not disable analyses or slide downloads.
A missing required model is an error. Invalid custom paths remain errors; they are not replaced
with managed downloads. Disabled/unselected components do not trigger setup.

The following commands are optional tools for preparing assets ahead of time or checking them:

```bash
pathnd-qc setup pen
pathnd-qc setup grandqc
pathnd-qc setup --check
```

`--install-deps` runs pip for the backend's Python dependencies and reinstalls Torch and
torchvision together: CPU wheels on Linux/Windows, PyPI wheels on macOS. For CUDA, install a
matching pair yourself and omit `--install-deps`. To configure or repair a CPU-only environment
after package installation, follow the [CPU installation steps](../../USAGE_CLI.md#cpu-only-linux-and-windows).
A local wheel includes the same dependency requirements:

```bash
python -m pip install "/path/to/pathnd_qc-0.5.0-py3-none-any.whl"
```

GrandQC setup requires Git. It clones commit `002688d74a4ac86dfbb816a96df8461d6080f88a`, applies the
packaged compatibility patches, downloads the four catalogued checkpoints, verifies their SHA-256
checksums, and checks the scripts' command-line interfaces. Dependency installation includes the
OpenSlide Python bindings and binary distribution; a platform without a compatible binary needs a
working native OpenSlide installation. Setup reports dependency/import failures before registering
an unusable checkout.

Managed patch version 2 loads GrandQC tissue inference parameters from the full tissue checkpoint
without downloading separate ImageNet weights. ImageNet preprocessing is retained. Managed setup
selects the patched checkout; custom `--repo` checkouts remain untouched.

Pen setup uses `gdown` to retrieve the upstream Google Drive folder and verifies `pen.pt`. Drive
access can require authentication or hit provider limits. If automatic retrieval fails, download the
file in your browser from the [upstream folder](https://drive.google.com/drive/folders/1P3E9kZDM7A7cM06RR47kywvQCL3X0HJz),
then register it:

```bash
pathnd-qc setup pen --weights "/path/to/pen.pt" --install-deps
```

Pen checkpoints must load into the pipeline's UNet++/ResNet34 architecture with two classes. Setup
checks that loading contract. GrandQC setup checks script imports and required CLI options; it does
not establish scientific accuracy or guarantee that every slide will decode or infer successfully.

### Managed storage

Managed downloads are stored outside the installed package:

| Platform | Default directory |
|---|---|
| Linux | `~/.local/share/pathnd-qc` (or `$XDG_DATA_HOME/pathnd-qc`) |
| macOS | `~/Library/Application Support/pathnd-qc` |
| Windows | `%LOCALAPPDATA%\pathnd-qc` |

Set `PATHND_DATA_DIR` to select another location. Keep that environment variable set for both setup
and later runs; it selects where registration is read. Settings are saved as `settings.json`,
GrandQC versions in separate commit/patch-version folders, and pen weights in checksum-named folders.
The setup lock prevents concurrent installers from modifying the same registration. Failed downloads
are not published as complete assets. Setup reuses verified managed files and refuses modified ones.

## Use your own version

Download or prepare the version you want, then register it:

```bash
pathnd-qc setup grandqc --repo "/my/grandqc/01_WSI_inference_OPENSLIDE_QC"
pathnd-qc setup pen --weights "/my/models/pen.pt"
```

Registration remembers absolute paths and hashes. It does not copy, patch, or replace your files.
A stock upstream GrandQC checkout may not accept the required `--device` option; a compatible
custom checkout must implement the interface below. Choosing a different version does not make an
incompatible interface or checkpoint architecture work automatically.

You can instead override a single run, without changing registration:

```bash
pathnd-qc --slide "/data/slide.svs" --out reports --no_metadata \
  --run_tissue_segmentation --run_tiles \
  --grandqc_repo "/my/grandqc/01_WSI_inference_OPENSLIDE_QC"

pathnd-qc --slide "/data/slide.svs" --out reports --no_metadata \
  --run_pen_detection --pen_weights "/my/models/pen.pt"
```

These model flags also work with `pathnd-qc-batch run`. `--check` can inspect an override without
registering it, for example `pathnd-qc setup grandqc --check --repo /my/inference-directory`.
Run `--check` after changing a registered checkout; altered hashes are reported. Re-registering is
an explicit choice to accept the changed files. Ordinary pipeline runs record current hashes and
fail if selected work cannot complete. They check required model files before reading the slide;
they do not repeat the installer’s full checksum and inference-interface validation.

A direct HTTPS pen checkpoint download is also supported:

```bash
pathnd-qc setup pen --url "https://your-server.example/pen.pt" --sha256 YOUR_64_CHARACTER_SHA256
```

Supply the actual URL and checksum. Authentication for private servers is not configured by this
option; download those files yourself and use `--weights`. To avoid downloading official GrandQC
weights you already have, use `setup grandqc --weights-dir /path/to/weights`; all four catalogued
files must exist and match their expected hashes. This option still clones the pinned source.

## Give GrandQC its own Python environment

Create an environment with your chosen Python, then point setup at its executable:

```bash
python3 -m venv /path/to/grandqc-env
pathnd-qc setup grandqc --python /path/to/grandqc-env/bin/python --install-deps
```

For an existing custom checkout, add `--repo /path/to/inference-directory`. `--install-deps` installs
Path-ND QC's supported backend dependencies into that environment. If your custom version needs its
own dependency versions, install those yourself and omit `--install-deps`.

The selected interpreter is remembered. A per-run `--grandqc_python /path/to/python` overrides it;
this flag also works in batch mode and does not change the Python used by the main pipeline.
Pen executes inside the main process and therefore uses the main environment.

## Compatibility contract and provenance

GrandQC must provide `wsi_tis_detect.py` and `main.py`, a tissue checkpoint at
`models/td/Tissue_Detection_MPP10.pth`, and at least one supported checkpoint under `models/qc/`.
Both scripts must accept `--slide_folder` and `--output_dir`; `main.py` must additionally accept
`--mpp_model`, `--create_geojson`, and `--device`. It must write `mask_qc/<slide-filename>_mask.png`
using the class IDs and physical scale expected by [the artifact adapter](../qc_tile/artifacts/README.md).
CLI help checks cannot prove this output contract; a custom version needs an inference check too.

Reports record selected backend paths, the GrandQC interpreter and Git commit when available,
Python-script/checkpoint hashes, and pen checkpoint hashes under `provenance.external_backends`.
The configuration hash includes remembered paths. Explicit flags override configuration; precedence
is shipped defaults → managed registration → `PATHND_CONFIG` → explicit configuration/function flags.

No upstream checkout or model weights are bundled in the wheel or source distribution. Their terms
remain those of the upstream providers. Only Path-ND QC's installer, patches, compatibility bridge
and download catalog are packaged.
