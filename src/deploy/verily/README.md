# Path-ND QC — deployment and release guide

Both WDLs call the same single-slide pipeline as the local CLI. They support all nine components,
explicit metadata lists, reusable artifacts, GCS/S3/Azure slide URIs, the current output layout,
and pipeline/Git provenance. Both default to requesting all nine components. Set any
`components.<name>` to `false` in `config_file` to exclude it, including from explicit selections.
Only the remaining components require model assets and must complete. Missing default assets are
installed automatically by the pipeline. The bucket bootstrap applies the same selection when
checking inputs, then installs the package before invoking the pipeline.

The standard image includes GrandQC; pen is downloaded on first use unless supplied. The bucket
route obtains both when selected. Set `PathNDQC.no_model_download` to `true` in WDL inputs
(`"no_model_download": true` in an adapter/Compose request) to require existing assets.
Host files in `src/external/` are not bundled in the release or image; supply custom files through
workflow inputs or the Compose input mount. See [model inputs](../../USAGE_VERILY.md#model-and-reference-inputs).

The shipped package is recorded in [`package-freeze.json`](../../package-freeze.json). Release
preparation verifies every shipped Python module, default, model catalog, patch, image asset and
package guide against this manifest. Intentional changes require corresponding hash updates
after review; each prepared release retains its own immutable snapshot.
A release keeps package version **0.5.0** and records checksums that distinguish its exact contents.


[Source overview](../../README.md) · [Run a Verily workflow](../../USAGE_VERILY.md) · [Validation status](deployment-status.md)

For workflow inputs, models, batches and outputs, start with the **[Verily usage guide](../../USAGE_VERILY.md)**.
This page covers preparing distributions, building containers and validating a release.

Pen uses a single whole-image forward pass by default, so memory requirements grow with the
analysis image. For smaller memory allocations, supply `m2.pen.tile_px: 1024` or `2048` through
`config_file`; see [pen inference](../../pathnd_qc/qc_slide/pen/README.md#how-it-works).
The standard image and bucket bootstrap set OpenMP, OpenBLAS and MKL thread limits to one.
The [local batch worker budget](../../pathnd_qc/batch/README.md#scheduling-and-batch-outputs)
applies to the batch runner; WDL tasks invoke the single-slide adapter.

- [Build the frozen release](#build-the-frozen-release)
- [Local verification](#local-verification)
- [Files in this directory](#files-in-this-directory)

## Build the frozen release

From the project root (`src/` in the development workspace), using Python 3.12+ and setuptools 77+:

```bash
python -m pip install 'setuptools>=77'
python deploy/verily/prepare_release.py --check
python deploy/verily/prepare_release.py --out dist/deployment/0.5.0-release
```

Use a new destination for each snapshot; existing releases are never overwritten. This creates:

```text
dist/deployment/0.5.0-release/
├── pathnd_qc-0.5.0.tar.gz          # source archive for the bucket workflow
├── pathnd_qc-0.5.0.tar.gz.sha256
├── pathnd_qc-0.5.0-py3-none-any.whl
├── pathnd_qc-0.5.0-py3-none-any.whl.sha256
├── release.json                  # distribution hashes, package files and workflow identity
├── inputs.example.json           # image request with the source fingerprint
├── inputs.models-check.example.json # focused pen + GrandQC run
├── inputs.models.example.json
├── inputs.bucket.example.json    # source fingerprint and archive checksum filled in
├── request.compose.example.json
└── source/                       # extracted source; Docker/Cloud Build context
```

The helper embeds the Git commit, dirty state and pipeline source fingerprint. A dirty
checkout remains marked dirty; preparing a snapshot does not commit it. A copied project with
neither Git nor embedded build identity reports an unknown commit. The archive hash
(`archive_sha256` in `release.json`) and pipeline fingerprint (`source_sha256` there) are different:
use the former for the bucket WDL's **`source_sha256`**, and the latter for either WDL's optional
**`expected_source_sha256`** guard. The helper sets that guard's default in both prepared WDLs to
the captured pipeline fingerprint. Use the WDLs from `source/deploy/verily/` for release: even an older
image/archive labelled 0.5.0 will then be rejected. The repository templates leave the guard optional.
Both WDLs require package version **0.5.0** by default.

The wheel is built from the extracted archive and checked against the same freeze manifest.
Install it in a Python 3.12+ environment with `python -m pip install /path/to/pathnd_qc-0.5.0-py3-none-any.whl`.
The wheel contains the runtime package and installed commands; the source archive also contains
container/workflow definitions. Both have adjacent `.sha256` checksum files. Dependencies resolve
from `pyproject.toml`; this source freeze is not a dependency lock.

For the bucket route, upload the prepared archive and WDL to your workflow bucket using your usual
release process. Use `inputs.bucket.example.json` at the release directory's root and fill in the archive URI,
slide and model/reference paths. Its archive checksum and pipeline fingerprint are already set. There is no default pointing to an old release. Standard setuptools
source archives with one project root are accepted if they contain the adapter and build hooks;
use the preparation helper to capture the workflow files and identity together.

For an image, build from the prepared context:

```bash
docker build --platform linux/amd64 --target pipeline \
  -f dist/deployment/0.5.0-release/source/deploy/verily/Dockerfile \
  -t pathnd-qc:0.5.0-release dist/deployment/0.5.0-release/source
```

There is one image target for the entire pipeline. Push/tagging and workflow registration are
separate release actions. [deploy_from_workbench.sh](deploy_from_workbench.sh) is an explicit publishing
helper for a configured Workbench environment: it verifies the frozen package, builds through Cloud Build, resolves the image digest and uploads
both WDLs, input examples, wheel, source archive, checksums and release records. The bucket input
example receives the uploaded archive URI. Configure its target in `deploy.env`.

To publish from a Verily Workbench terminal, copy [deploy.env.example](deploy.env.example) to
`deploy.env` (git-ignored, never packaged), fill in your workspace values, then run `bash deploy/verily/deploy_from_workbench.sh` from `src/`. The helper loads that settings
file itself and uses the authenticated `wb` command to build and upload the release.

For local Compose, run these commands from `src/` after preparing the example release above:

```bash
mkdir -p inputs outputs
cp dist/deployment/0.5.0-release/request.compose.example.json inputs/request.json
```

Place your slide and reference configuration in `inputs/`, edit `inputs/request.json`,
and use [compose.yaml](../../compose.yaml) with the prepared context. Paths in that request are
container paths (`/inputs/...`), not host paths. Supply a normalization reference configuration under the input mount for a full run.
Pen weights may be supplied or downloaded automatically; `no_model_download` disables that download. Each adapter run requires a fresh output directory without `results/`. Compose
defaults to `dist/deployment/0.5.0-frozen/source` and image tag `0.5.0-frozen`; set
`PATHND_RELEASE_DIR` for a different prepared snapshot and `PATHND_IMAGE` for its image.
Use `docker compose build` before running the new snapshot; existing containers/images are unchanged.
Results appear under the host `outputs/` directory. Use `PATHND_INPUT_DIR` and `PATHND_OUTPUT_DIR`
to select different existing host directories; choose a fresh output directory for each adapter run.


To select the prepared release explicitly with Compose (from `src/`):

```bash
export PATHND_RELEASE_DIR=dist/deployment/0.5.0-release
export PATHND_IMAGE=pathnd-qc:0.5.0-release
docker compose build
docker compose run --rm qc
```

The directory name above is an example for a new build; choose another if it already exists.
The earlier locally model-tested bundle is `dist/deployment/0.5.0-frozen-models`, with image
`pathnd-qc:0.5.0-model-check`. Those artifacts preserve their own documentation and release identity.
See [validation status](deployment-status.md) for their exact test scope.

## Local verification

From the project root (`src/` in a repository checkout), using its installed environment:

```bash
python deploy/verily/prepare_release.py --check
python -m pip install miniwdl==1.15.0
miniwdl check deploy/verily/pathnd_qc.wdl
miniwdl check deploy/verily/pathnd_qc_bucket.wdl
```

These commands work from a standalone source project. Source-freeze failures stop release preparation
before building; WDL checks validate syntax and types. Neither check submits cloud jobs or runs models.
WDL input/output semantics follow the [WDL 1.0 specification](https://github.com/openwdl/wdl/blob/legacy/versions/1.0/SPEC.md).

The `test_workflow.py` and `test_bucket_workflow.py` suites exercise request handling and synthetic
TIFF processing. Both use the shared `tests/fake_slide.py` under the source project (`src/tests/`
in the repository). Run them from a checkout with the package dependencies installed; no separate
development workspace or custom `PYTHONPATH` is needed. The fixture is excluded from release
distributions, so run the suites from the checkout even when testing a built archive.

From the project root (`src/`):

```bash
python -m pip install "setuptools>=77" wheel
python deploy/verily/test_workflow.py
python deploy/verily/prepare_release.py --out dist/workflow-test-release
PATHND_TEST_SOURCE_ARCHIVE="$PWD/dist/workflow-test-release/pathnd_qc-0.5.0.tar.gz" \
  python deploy/verily/test_bucket_workflow.py
```

Choose a fresh release directory if that output already exists. `PATHND_TEST_SOURCE_ARCHIVE`
enables the archived-source integration case; without it, that one case is skipped. These suites
do not submit cloud jobs or download models. See [deployment status](deployment-status.md) for
recorded Docker/model results and the limits of cloud verification.

## Files in this directory

| File | Purpose |
|---|---|
| [pathnd_qc.wdl](pathnd_qc.wdl) | Workflow using a prepared inference image |
| [pathnd_qc_bucket.wdl](pathnd_qc_bucket.wdl) | Workflow installing a checksum-pinned source archive |
| [run_workflow.py](run_workflow.py) | Shared request validation and single-slide adapter |
| [Dockerfile](Dockerfile) | Linux CPU image with inference dependencies and GrandQC |
| [Dockerfile.dockerignore](Dockerfile.dockerignore), [gcloudignore](gcloudignore) | Files excluded from container/cloud build contexts |
| [cloudbuild.yaml](cloudbuild.yaml) | Build and offline setup check for the image |
| [prepare_release.py](prepare_release.py) | Verify the freeze and build matching release artifacts |
| [deploy_from_workbench.sh](deploy_from_workbench.sh) | Explicit cloud publishing helper |
| [deploy.env.example](deploy.env.example) | Template for the git-ignored `deploy.env` deployment target settings |
| [inputs.example.json](inputs.example.json), [inputs.models.example.json](inputs.models.example.json) | Full-run image inputs, without/with metadata |
| [inputs.models-check.example.json](inputs.models-check.example.json) | Focused pen and GrandQC check |
| [inputs.bucket.example.json](inputs.bucket.example.json) | Full-run source-archive inputs |
| [request.compose.example.json](request.compose.example.json) | Local adapter request using container paths |
| [config.folds.example.json](config.folds.example.json) | Fold detector configuration example |
| [batch.example.csv](batch.example.csv), [column_mapping.json](column_mapping.json) | Image-workflow table and input mapping |
| [batch.bucket.example.csv](batch.bucket.example.csv), [column_mapping.bucket.json](column_mapping.bucket.json) | Source-workflow table and input mapping |
| [test_workflow.py](test_workflow.py), [test_bucket_workflow.py](test_bucket_workflow.py) | Workflow contracts and synthetic TIFF tests using the checkout's shared `src/tests/fake_slide.py` |
| [deployment-status.md](deployment-status.md) | Scope and limits of deployment validation |
