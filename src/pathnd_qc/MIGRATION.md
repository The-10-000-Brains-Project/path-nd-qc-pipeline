# Report schema 2.0 and input changes

## Automatic model setup

Pipeline runs obtain missing default pen and GrandQC assets automatically for selected components.
Use CLI/batch `--no_model_download`, Python `download_models=False`, or
`PATHND_NO_MODEL_DOWNLOAD=1` for the previous existing-assets-only behavior. WDL requests accept
`no_model_download`. Invalid explicit/custom model paths remain errors. Direct low-level component
functions still require usable model paths; automatic setup belongs to the pipeline entry point.

The workspace asset folder is `src/external/`. Update explicit paths that pointed at the former
root `external/` folder; relative configuration paths are still resolved against the working directory.
The packaged installer remains separate; see [model locations](external/README.md#model-locations).


## Staining metric simplification (26 September 2026)

- `staining_metrics` now contains only `chroma_mean`, including a null value on failure.
  `staining_quality_score` remains an alias for that value, so its existing per-stain thresholds work.
- Deconvolution, brightness, contrast, chroma dispersion, and luminosity metrics are removed.
  Remove `m2.staining.stain_matrix`, `m2.staining.clip_percentile`, and thresholds for retired
  metrics from configuration overrides. The `stain_matrix` function argument and report field
  are removed; `return_debug=True` returns the support mask without a deconvolution stack.
- Previously saved reports describe their original run and are not rewritten.

## Component selection and completion (23 September 2026)

- No component selection means all enabled components; the shipped defaults enable all nine.
- Every component has a `components.<name>` boolean. `false` excludes it from any run, including
  explicit flags, aliases, Python selections and WDL inputs. Replace the retired `m2.pen.enabled`
  with `components.pen_detection`. No component has a special selection policy.
- An empty effective selection is rejected. Disabled producers are not re-enabled automatically;
  supply required artifacts for their consumers. Reports record `components_disabled_config`.
- Standard installation includes all inference dependencies. Model weights/code are still obtained
  or registered through `pathnd-qc setup pen` and `pathnd-qc setup grandqc`.
- Missing selected model files are preflight errors. Runtime component errors, scientific gates
  that prevent requested work, and incomplete artifacts preserve the available report but raise
  `RunFailed`, write status `failed`, and return CLI exit `1`.
- Both WDL defaults request all nine, then apply config exclusions. The standard Docker target is `pipeline`; `core`/`models`
  targets and `PATHND_IMAGE_TARGET` are removed. Workflow completion is always required;
  remove `require_complete` from WDL inputs. Set pen weights and normalization references.

Earlier entries below describe previous behavior where noted.

Reports now use code-owned `provenance.report_version: "2.0"`. The implementation version comes
from `pathnd_qc.__version__`; remove `shared.pipeline_version` and `report.report_version` from
configuration overrides. Unknown keys and invalid values are reported through `config_error`
and execution diagnostics; an invalid configuration layer is rejected as a whole.

## Report consumers

- New reports add `provenance.git_commit`, `git_dirty`, `git_source` and `source_sha256` alongside
  the existing package version and configuration fingerprint. These are additive schema `2.0`
  fields, captured before processing. Consumers must accept absent fields in historical reports
  and null Git identity when unavailable. Batch summaries expose these fields plus `pipeline_version`
  and `config_sha256`; they preserve each saved report's identity. See
  [pipeline revision details](reporting/README.md#pipeline-revision-and-reproducibility).
- Read `provenance.execution.complete` for tracked computational completion and
  `provenance.execution.reasons` for observations and omissions. This replaces historical
  `provenance.trustworthiness` interpretations. Completion is not scientific acceptance.
- Ingestion quarantine, metadata errors, scale disagreements and unavailable transfer verification
  now appear in execution reasons as well as their detailed records. An observation can coexist
  with `complete: true` when all requested computations finished.
- Normalization blocked by a failed tissue mask is gated, with zero runtime; it was not attempted.
- A failed packed-mask write preserves measured tile records, sets `m3.tiles.mask_complete: false`
  and `mask_write_error`, and makes execution incomplete. No completed mask is advertised.
- Non-finite numbers serialize as JSON `null`. Active threshold checks reject non-finite
  measurements; malformed or non-finite bounds generate flags without counting as applied checks.
  A rejected non-finite measurement counts as one checked metric. Null metrics are not zeroes.
- Standalone interruption after a run folder is created normally writes a failure report and final
  status. A forced kill or unusable filesystem can still prevent publication. Batch cancellation
  records known run folders and reconciles their status, including timeouts.

## Output folders in 0.5.0

New batch runs live under `<out>/batch_<batch_id>/<relative input parent>/<slidename>_output/<UTC date>/`
for folder inputs. Paths are relative to `--slide_dir`; the input root's own name is not repeated.
Slide lists and CSV/TSV manifests put `<slidename>_output` directly inside `batch_<batch_id>/`.
Single-slide commands still put it directly under `--out`.
There is no added `slides/` directory or source hash in the folder name. Reruns retain dated
subfolders. A `slide.json` in the slide folder records its owner; a different source with the same
name is refused rather than mixed in. Flat lists with duplicate names need renamed slides or a
folder input that separates them.

Reports and status JSON stay at the dated run root. All generated masks and visual outputs now
share `images/`; tile geometry and records use `data/`. Historical `masks/` and flat-file artifacts
remain discoverable. `{latest_artifact}` and older `{latest_run}/masks/<filename>` templates can
resolve the new location without moving previous results.
Open `index.html` for links and version identity. Batch records, summaries and slide folders now
share `<out>/batch_<batch_id>/`. Its summary covers only this batch's runs. `summarize --out <out>`
still builds a combined table across all batches.

When `--batch_id` is omitted, the ID is the current UTC date/time (`YYYYMMDD_HHMMSS`), without
hyphens or a timezone label in the folder name.
Occupied names receive `_2`, `_3`, etc.; random suffixes are no longer added. Automatic allocation
always creates a fresh folder, including simultaneous starts. Pass the generated ID explicitly to resume.

Existing folders are not moved. Batch discovery and summaries read both historical layouts and
new mirrored runs at any depth. Resume now requires the same explicit `--batch_id` as well as a
matching invocation fingerprint, including output placement. A new ID runs independently; older
unwrapped results remain available for summaries and artifact reuse. Use returned `report_path` /
`out_dir` or `provenance.outputs`, instead
of constructing paths or assuming a fixed directory depth. For artifact reuse, prefer
`--tissue_mask "{latest_artifact}"`; old `{latest_run}/{slide_id}_tissue_mask.png` templates also work.

Workflow archives include a top-level `index.html`; extract the complete archive to preserve its
relative links. Existing WDL output names and the report schema are unchanged; WDLs also expose
`provenance` as a separate JSON File output. Standalone
`reporting.write_report(report, out_dir)` retains its existing flat-file API.
See the [complete layout](../USAGE_CLI.md#8-output-files).

## Metadata inputs in 0.5.0

Automatic metadata-bank lookup has been removed. Pass `--metadata PATH` (repeatable) or
`run(metadata_paths=[...])` to read your chosen files. Without a path, analysis continues and
`m1.ingestion.metadata` records `skipped: true` with `skip_reason: "no_metadata_path"`.
`--no_metadata` remains an explicit opt-out, incompatible with a metadata path or key.

Remove `ingestion.metadata` and `ingestion.metadata_csv` from old configuration overrides; they
now produce a configuration error with migration instructions. As with any invalid configuration
layer, the whole layer is rejected. Pass its metadata paths as run inputs instead.

The low-level `build_index(sources)` API requires an explicit source list and no longer has a
default-source cache or `force` argument. `GCSWSIReader.load_metadata(csv_path)` also requires a
path. Source specs with column maps remain available through the
[explicit metadata API](ingestion/metadata/README.md). `--bank` still selects normalization
references; it does not search for metadata files.

## Cloud inputs in 0.5.0

The base package now installs `s3fs` and `adlfs` alongside `gcsfs`. Reinstall the project in
existing environments to add them. `--slide` and explicit `--metadata` files accept GCS, AWS S3
and Azure Blob/ADLS Gen2 object URIs, including mixed-provider batch lists. `--slide_dir` remains
local-only. See [cloud setup](../USAGE_CLI.md#cloud-slides-gcs-aws-s3-and-azure).

`from pathnd_qc import WSIReader` is the provider-neutral API name; `GCSWSIReader` remains an
alias for the same implementation. Transfer checks now recognize Azure content MD5; S3 ETags
are not treated as MD5. A size-only check is reported as `check: "size"`.

## Inputs and memory

NumPy analysis images must be nonempty `uint8` grayscale, RGB or RGBA arrays. PIL images are
converted to RGB. Float, signed and higher-bit-depth arrays now fail explicitly: convert them using
your known intensity scale before passing them to tissue/normalization APIs or `.npy` thumbnails.
Image *files* follow the same rule: a 16-bit, 32-bit-integer or float PNG/TIFF thumbnail is refused
by name instead of being clipped to 255 (18 September 2026).
Masks keep their separate boolean/nonzero-pixel contract, and their accepted layouts are now
explicit: nonempty 2D `(H, W)` arrays (including a one-pixel side), or `(H, W, 1)` / RGB/RGBA
`(H, W, 3|4)` with both spatial sides larger than 1. Multichannel masks select pixels with any
nonzero RGB channel; alpha is ignored.
A channel-first `(C, H, W)` array, which used to be silently read as a one-pixel-tall image, is
refused with a message naming the accepted layouts (18 September 2026).
Supplied tile-list JSON may carry a UTF-8 BOM, like batch manifests already could.

Supplied tile widths/heights cannot exceed `m3.tiles.tile_px` (512 by default). Declared `plane_mpp`
must be positive, finite and match the resolved plane; declared dimensions must also match.
Large custom rectangles that previously bypassed bounded reads are refused.

`m2.read.max_plane_px` limits the full analysis image to 67,108,864 pixels by default.
`m3.tile_metrics.max_mask_bytes` limits the packed mask to 536,870,912 bytes before allocation.
These bounds complement the source-read cap; they do not estimate all intermediate algorithm
arrays or the combined RAM of batch workers. Size worker counts for your machine.

## Batch resume and artifact permissions

Resume is scoped to `<out>/batch_<batch_id>/`; repeat the same `--batch_id` to continue there.
Concurrent writers to the same batch are refused. A new ID starts independent work. Existing
batch bookkeeping is refreshed on resume, logs are appended, and dated slide runs remain intact.
Resume requires a completed run with a matching request fingerprint, including component flags,
resolved configuration, implementation identity, stain/bank and local input size/modification time.
Older status files without a fingerprint run again. A completed but computationally incomplete run
with the same fingerprint is still skipped; inspect its report or use `--force`.

Atomic replacement preserves an existing destination's permissions. New artifacts use normal file
creation permissions filtered by the caller's umask. This can permit group readers where the
previous temporary-file implementation produced owner-only files.

## Commands and dependencies

Install through `pyproject.toml` with Python 3.12 or newer; scikit-image 0.26 is required by the
current morphology API. `requirements.txt` installs this project using the dependencies declared in `pyproject.toml`; it is not a pinned environment.
Use `pathnd-qc`, `pathnd-qc-batch`, or `python -m pathnd_qc`. The `run.py` / `batch_run.py`
files remain compatibility shims; CamelCase imports are obsolete. The HTML manual was refreshed
for 0.5.0; dated design plans remain historical. See [CLI usage](../USAGE_CLI.md).

## Batch `summarize --to`

`--to` names the directory the summary is written into. Naming an existing *file* is refused by name
with exit code 2 (18 September 2026); it used to fail with a bare `[Errno 17] File exists`.

## Batch results rows: `state` is now `outcome`

`results.json` / `results.csv` rows name the batch's verdict on a job `outcome`
(`completed`, `failed`, `refused`, `timeout`, `error`, `skipped`, `interrupted`). The word `state`
is reserved for a run's lifecycle in its `<slide_id>_status.json` (`running`, `completed`, `failed`).
The module that reads run history is `pathnd_qc.batch.runs` (formerly `batch.state`).

## Fold detector update within 0.5.0 (23 September 2026)

Fold detection now adds F_line by default and routes LFB/LFB-H&E alongside Hirano to the d branch.
Successful `m2.folds.method` values become `d+fline` / `stain2+fline`; reports add branch diagnostics.
`fline_enabled=false` selects ConnSoftT only. The existing third positional Python argument remains
`thumb_mpp`; new detector options are keyword-only. Public imports, masks and folder layouts stay
compatible. Supplied-pen precedence and null failure metrics are preserved.

Measurements can change. Compare `source_sha256`, `config_sha256`, method and branch parameters,
not just package version 0.5.0. A new source/config fingerprint invalidates batch reuse. Prepared WDLs
now default their source guard to the prepared pipeline fingerprint, so a same-version older image
or archive is rejected. Rebuild/register from the new source snapshot to use this detector.
