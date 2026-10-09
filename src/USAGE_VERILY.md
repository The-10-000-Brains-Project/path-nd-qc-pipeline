# Path-ND QC — Verily workflow usage

[Source overview](README.md) · [Deployment and builds](deploy/verily/README.md) · [Validation status](deploy/verily/deployment-status.md)

Use this guide to run a supplied Path-ND workflow on Verily. One workflow invocation processes one
slide. To prepare or publish a new workflow version, use the [deployment guide](deploy/verily/README.md).

- [Before you start](#before-you-start)
- [Choose a workflow](#choose-a-workflow)
- [First job](#first-job)
- [Inputs](#inputs-slide-metadata-and-modular-analysis)
- [Pen, GrandQC and normalization](#model-and-reference-inputs)
- [Fold settings](#connsoftt--f_line-fold-settings)
- [Cloud reads and caching](#cloud-reads-and-workflow-caching)
- [Results](#access-results)
- [Troubleshooting](#troubleshooting)

## Before you start

You need a Verily workspace with permission to run workflows, access to the slide and optional
metadata/model/configuration objects, and a prepared Path-ND release. For the image route, the
execution environment also needs permission to pull its container image. The bucket route needs
network access to install dependencies and selected model assets.
The image route also needs network access when a selected model, such as pen, is not already
included or supplied. `no_model_download` prohibits automatic model downloads; it does not prevent
the bucket bootstrap from installing Python dependencies.

A local Docker tag is not a registry image available to Verily. Ask your deployment maintainer for
the registered workflow/release and its matching image digest or source archive/checksum. Use the
prepared release's WDL and example inputs together. The [validation status](deploy/verily/deployment-status.md)
distinguishes local model checks from cloud execution; no Verily job has been verified by those checks.

## Choose a workflow

| Route | Required deployment input | External models |
|---|---|---|
| [Image WDL](deploy/verily/pathnd_qc.wdl) | `docker_image`, preferably a registry digest | The standard `pipeline` image includes all inference dependencies and GrandQC code/checkpoints |
| [Bucket-source WDL](deploy/verily/pathnd_qc_bucket.wdl) | `source_archive` File and its full `source_sha256` | Bootstrap installs the complete package; the pipeline installs missing selected model assets automatically unless downloads are disabled |

The bucket route uses a pinned official Python 3.12 Linux image by default. Debian/Python packages
still resolve at task startup, so this route needs network access and does not freeze dependencies.
It records installed Python versions in `environment.txt`. Source and image checks identify the
implementation; they do not make external services or slide decoding infallible.

## First job

1. Choose the prepared `pathnd_qc.wdl` (image route) or `pathnd_qc_bucket.wdl` (bucket route).
   Register/select that WDL through your workspace's workflow submission process.
2. Start with a small component set and one accessible, self-contained slide. Replace every
   placeholder below and use the slide's real stain.
3. Supply the inputs using the workflow's input form or JSON submission mechanism. Input names
   in JSON have the `PathNDQC.` prefix; the UI may display the shorter names.
4. Submit one job, inspect task logs if it fails, and [download its results](#access-results)
   after it completes. Increase resource allocations if your slide requires them.

For an image workflow, a first tissue-only request is:

```json
{
  "PathNDQC.slide": "gs://YOUR_SLIDE_BUCKET/example.svs",
  "PathNDQC.stain": "HE",
  "PathNDQC.components": ["tissue_segmentation"],
  "PathNDQC.no_metadata": true,
  "PathNDQC.docker_image": "REGION-docker.pkg.dev/PROJECT/REPOSITORY/pathnd-qc@sha256:IMAGE_DIGEST",
  "PathNDQC.expected_pipeline_version": "0.5.0"
}
```

For the bucket workflow, omit `docker_image` to use its default Python image, and add:

```json
{
  "PathNDQC.source_archive": "gs://YOUR_WORKFLOW_BUCKET/RELEASE/pathnd_qc-0.5.0.tar.gz",
  "PathNDQC.source_sha256": "REPLACE_WITH_ARCHIVE_SHA256"
}
```

Merge those fields into the request; this second object is not a complete request by itself.
Take the archive checksum from the release's `.tar.gz.sha256` file or `archive_sha256` in
`release.json`. It is different from the pipeline fingerprint `expected_source_sha256`.
Prepared WDLs pin that fingerprint in their defaults; keep it matched to your release.

### Add the models or run all nine

| Run | Example | What to provide |
|---|---|---|
| Tissue + pen + tile selection + GrandQC | [Model check inputs](deploy/verily/inputs.models-check.example.json) | Slide, stain and matching image; optional custom pen checkpoint |
| All nine components | [Full inputs](deploy/verily/inputs.example.json) | Slide and stain; optional `config_file` to override the shipped placeholder targets |
| All nine with metadata | [Metadata inputs](deploy/verily/inputs.models.example.json) | The above plus explicit metadata files |
| All nine from a source archive | [Bucket inputs](deploy/verily/inputs.bucket.example.json) | Source archive/checksum and slide; optional reference configuration and custom pen checkpoint |

Use copies emitted into the prepared release when available: their identity/checksum fields are
filled in. The source-tree templates contain placeholders. Full runs include normalization;
[create a reference configuration](pathnd_qc/normalization/README.md#create-a-reference-configuration)
before relying on its output.

### Multiple slides

Use [batch.example.csv](deploy/verily/batch.example.csv) with [column_mapping.json](deploy/verily/column_mapping.json), or
[bucket CSV](deploy/verily/batch.bucket.example.csv) with [bucket mapping](deploy/verily/column_mapping.bucket.json), as templates
for your workspace's table submission. Set deployment, model and reference inputs shared by all rows;
put slide-specific paths and stains in the table. Inspect the mapping before submission.
Each row is an independent workflow job. This does not use local `--batch_id` resume semantics.

## Inputs: slide, metadata and modular analysis

Use workflow-prefixed names, such as `PathNDQC.metadata_files`, in WDL input JSON. The adapter's
standalone JSON uses the same names without the prefix. Start from
[inputs.example.json](deploy/verily/inputs.example.json) or [inputs.models.example.json](deploy/verily/inputs.models.example.json).

| Input | Meaning |
|---|---|
| `slide` **or** `slide_uri` | Supply exactly one. `slide` is a WDL File localized by the engine (usually GCS on Verily). `slide_uri` is an explicit `gs://`, `s3://`, `az://`, `abfs://` or `abfss://` object read by the pipeline |
| `stain` | Optional actual stain override; empty uses supplied metadata if available |
| `components` | Nonempty requested list; default all nine. `config_file` exclusions apply last; at least one component must remain |
| `metadata_files` | Explicit list of CSV Files, localized by the workflow engine and searched in order |
| `metadata_uris` | Explicit cloud CSV URIs, searched after localized files; task needs provider credentials |
| `metadata` | Compatibility single File, searched before both lists |
| `metadata_key` | Optional common key column for the metadata files; requires at least one metadata path |
| `no_metadata` | Explicit opt-out. Omission of all metadata paths also continues without metadata. Cannot combine opt-out with metadata paths/key |
| `thumbnail`, `tissue_mask`, `fold_mask`, `pen_mask`, `tile_list` | Optional WDL Files passed to the corresponding pipeline artifact inputs; the normal validation/geometry rules apply |
| `config_file` | JSON-object override File. Relative/absolute paths **inside** this JSON are not automatically localized |
| `bank`, `norm_method` | Normalization reference selection and optional `macenko`/`reinhard` override; `bank` does not locate metadata |
| `pen_weights` | Optional compatible pen checkpoint File; missing default assets are installed automatically |
| `grandqc_repo`, `grandqc_python` | Optional inference directory and interpreter already present in the task/image. Empty uses managed configuration |
| `no_model_download` | Default false; use existing assets only and fail if a selected model is missing |
| `no_save_artifacts`, `quiet` | Forward the corresponding pipeline flags; report/completion output remains available |
| `expected_pipeline_version` | Exact version guard, default `0.5.0` |
| `expected_source_sha256` | Optional exact pipeline source fingerprint guard |
| `cpu`, `memory_gb`, `disk_gb` | Defaults 4 CPUs, 50 GB RAM, 100 GB task disk (see below) |

**Memory for Verily jobs:** the tested full-pipeline run failed with **16 GB RAM** and succeeded
with **50 GB RAM**, so both WDLs default to `memory_gb` 50. Allocations between 16 and 50 GB were
**not tested**, so the minimum required memory has not been established; 50 GB is the observed
successful allocation. Lighter component selections may run with less: set `PathNDQC.memory_gb`
explicitly after checking a pilot slide.

No metadata directory or bank is searched automatically. The first matching explicit metadata file
wins. Producers are not automatically selected for modular runs: select them or supply the required
artifacts. See [component dependencies](USAGE_CLI.md#3-selecting-components).

A minimal standalone adapter request is:

```json
{
  "slide": "/inputs/example.svs",
  "stain": "HE",
  "components": ["tissue_segmentation"],
  "expected_pipeline_version": "0.5.0"
}
```

Run from a fresh result directory, after installing the package:

```bash
python /path/to/project/deploy/verily/run_workflow.py --request /path/to/request.json
```

## Model and reference inputs

Both WDL routes execute `pen_detection` and `tile_artifacts` through the installed pipeline.
Use [inputs.models-check.example.json](deploy/verily/inputs.models-check.example.json) to run those models
with their tissue/tiling prerequisites, without requiring a normalization reference. Supply
the slide and image digest; optionally supply a compatible pen checkpoint. For the bucket route, omit that image
input to use its Python base image, then add the prepared source archive and checksum.

**Pen:** included in a full run. A missing default checkpoint is downloaded and registered
automatically. Supply `pen_weights` to use your own checkpoint. The adapter rejects invalid
explicit checkpoint paths and missing Python dependencies. It uses the
pipeline's existing UNet++/ResNet34 inference implementation.

**GrandQC:** included in a full run, with tissue segmentation and tile selection. The standard
image contains managed GrandQC; the bucket workflow sets it up when selected. Managed setup
pins upstream code, applies packaged compatibility patches, and verifies the four checkpoint
checksums. Patch version 2 avoids downloading redundant ImageNet initialization weights at inference;
the full tissue checkpoint supplies those parameters, with ImageNet preprocessing retained. Existing
custom checkouts are used as supplied and are not modified by the workflow.
The bucket workflow reuses an existing configured inference directory, as well as an explicit
`grandqc_repo` input. It checks that a supplied pen checkpoint exists before installing dependencies.
Cloud Build checks the image's GrandQC registration, checkpoints and CLI imports with networking
disabled. Pen weights may be supplied as a workflow input or obtained on first use.
Set `PathNDQC.no_model_download=true` to prohibit missing-model downloads; selected components
still require working assets. Provider failures are setup errors, not successful skips.

Both the [standard example](deploy/verily/inputs.example.json) and [metadata example](deploy/verily/inputs.models.example.json)
select all nine components, including normalization. Completion is always required. To use it with the bucket WDL, replace `docker_image` with the bucket
WDL's default or omit it, and add `source_archive` plus its checksum. Startup downloads make that
route slower than a prebuilt inference image. Both provided routes use CPU inference; GPU resources
are not configured by these WDLs.

**Normalization:** without `config_file`, the ninth component uses the shipped `m4.reference`
defaults and logs a warning that they are placeholders. A supplied `config_file` overrides the
defaults as usual; the selected stain must have a matching reference. Provide suitable targets
before treating the normalized output as calibrated. Supplying an arbitrary config file is not calibration. See [model setup](pathnd_qc/external/README.md) and
[normalization](pathnd_qc/normalization/README.md) for the underlying contracts.

## ConnSoftT + F_line fold settings

`fold_detection` runs the detector from the installed package; neither WDL reimplements it.
The standard image includes it. NumPy, SciPy and scikit-image are installed with the package;
fold detection needs no checkpoint download. The image build checks that
F_line helpers and defaults are present, so a stale source context fails the build.

The defaults are recorded in [config.folds.example.json](deploy/verily/config.folds.example.json):
M2 at 8 µm/pixel; Frangi scales 35/70/105 µm; local-ball radius 520 µm; tensor scale 70 µm; stabilizer
fraction 0.01; rounded index threshold 200; area floor 44,800 µm². Hirano/LFB/LFB-H&E spellings use
the d branch, while other stains use stain-2; F_line is unioned with both. The source package owns
these defaults. Supply this JSON as `PathNDQC.config_file`, or use a partial override when changing
only selected settings. For Compose, copy it into the input mount and set request
`"config_file": "/inputs/config.folds.example.json"`.

Use actual stains, including per-slide metadata in mixed batches. Reports retain the method, branch
measurements and physical parameters. If F_line fails while ConnSoftT succeeds, its mask remains available downstream, but incomplete execution always fails the workflow.
A deliberate `fline_enabled=false` is a successful ConnSoftT-only selection.

The CPU/RAM defaults are 4 CPUs and 50 GB, matching the tested full-pipeline workload described
above. Neither that successful run nor a host synthetic resource probe
establishes a safe allocation for arbitrary WSI sizes. F_line adds arrays and filtering on the M2 plane. Increase RAM
for large planes and choose batch worker counts accordingly. See the [detector contract](pathnd_qc/qc_slide/folds/README.md) for algorithm details and limitations.

## Cloud reads and workflow caching

For S3 or Azure reads, use `slide_uri` and provide the normal provider credentials in the task's
execution environment. Cloud permissions used to stage GCS Files do not grant AWS/Azure access.
For public S3, set `s3_anonymous=true`; for public Azure, set `azure_anonymous=true` and supply its
account configuration through the task environment. See the
[credential guide](USAGE_CLI.md#cloud-slides-gcs-aws-s3-and-azure). Do not put credentials
in the request JSON, which is retained with results.

`slide_uri` and `metadata_uris` are WDL Strings: the engine does not localize or content-track them.
Use immutable object names or disable call caching for mutable URI inputs to avoid reusing results
after an object changes. WDL File inputs are localized by the engine according to its own storage
support. Both WDLs replace JSON File values with command-localized paths before bootstrap or
pipeline execution, including the source archive, config, metadata, weights and supplied artifacts.
Use filenames without line breaks. Paths embedded inside config JSON remain your responsibility;
only declared WDL File inputs are localized. The pipeline's range-read/localization behavior applies
when it reads a URI directly.

Both wrappers accept only self-contained slide files. `.mrxs`, `.vms` and `.vmu` are rejected because
companion-file staging is not implemented. One invocation processes one slide. CSV submissions run
one job per row; they do not reproduce a local folder hierarchy or batch resume history. For folder
mirroring and local batch bookkeeping, use [pathnd-qc-batch](pathnd_qc/batch/README.md).
For bucket CSV runs, set `source_archive` and `source_sha256` as shared workflow inputs, alongside the
slide/stain columns in [batch.bucket.example.csv](deploy/verily/batch.bucket.example.csv) and
[column_mapping.bucket.json](deploy/verily/column_mapping.bucket.json).

## Access results

Both WDLs expose **report, summary, provenance, results_archive, log and complete**.
The bucket route additionally exposes **setup_log, environment_file and bootstrap_info**.
`complete` describes execution, not whether the slide passes neuropathology QC.

Download and extract `results_archive`, then open `index.html`:

```text
index.html
summary.json
provenance.json                   # pipeline/Git/source/config identity and model hashes
workflow_inputs.json
pipeline.log
results/
└── <slidename>_output/
    └── <UTC run timestamp>/
        ├── index.html
        ├── <slidename>_report.json
        ├── <slidename>_status.json
        ├── images/               # masks, overlays and normalized image
        └── data/
```

Artifact folders appear when artifacts are saved. Relative links continue working after extraction.
The standalone `report.json` output copies the nested slide report. When available, the environment
inventory is included at `results/environment.txt`. Setup errors before analysis can prevent result
creation; inspect the workflow engine's task logs in that case. Existing uploaded WDLs and source
archives do not change when these local files change: register/upload the new version to use it.

## Troubleshooting

| Symptom | Action |
|---|---|
| Version or source mismatch | Use the WDL, image/archive and inputs from the same prepared release |
| Archive checksum mismatch | Verify the uploaded bytes and use the archive hash, not the pipeline fingerprint |
| Pen download/setup fails | Check task network/provider access, or supply `pen_weights` as an accessible compatible checkpoint File |
| GrandQC setup fails | Check the bootstrap log and network access; custom `grandqc_repo` must already exist inside the task |
| Placeholder normalization warning | The run uses shipped defaults because no `config_file` was supplied. Provide suitable targets to override them, or select components without normalization |
| Missing mask or tiles | Select the producer or supply its WDL File input; dependencies are not added automatically |
| Permission or download failure | Check task identity access to slide, metadata, archive, model and image resources |
| No report output | Setup may have failed before analysis; inspect workflow task logs and bucket setup logs |
| Failed with partial measurements | Read component errors and execution reasons; a selected component must finish for workflow success |
| Out of memory or disk | Increase the task's `memory_gb`/`disk_gb`; defaults are not limits proven safe for every slide |

For component behavior, see the [package overview](pathnd_qc/README.md).
For exact validation coverage, see [deployment status](deploy/verily/deployment-status.md).
