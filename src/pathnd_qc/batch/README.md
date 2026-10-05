# Batch processing

[Package overview](../README.md) · [CLI usage](../../USAGE_CLI.md) · [Library usage](../../USAGE_LIBRARY.md)

[Run a folder of slides](#run-a-folder-of-slides) · [Choose the analysis](#choose-the-analysis) · [Choose the slides](#choose-the-slides) · [Preview before running](#preview-before-running) · [Resume or run a second analysis](#resume-or-run-a-second-analysis) · [Scheduling and batch outputs](#scheduling-and-batch-outputs) · [Python API](#python-api)

Run the same analysis on several slides, with one separate process and result folder per slide.
The batch runner schedules the work, records progress, and can resume after an interruption.
Start with the [installation and single-slide guide](../../USAGE_CLI.md) if you have not run a slide yet.

## Run a folder of slides

Use the installed commands in your activated Python environment. Replace `/data/slides` with your folder.
This first example performs tissue segmentation without requiring model weights or cloud metadata:

```bash
pathnd-qc-batch run --slide_dir /data/slides --out reports \
  --workers 2 --no_metadata --run_tissue_segmentation
```

Add `--stain "Hirano"` only if that is your slides' actual stain. For mixed stains, use a manifest
or a metadata file. Folder discovery is recursive. Start with one or two workers and increase only
when memory, disk space and runtime measurements on your slides support it.

The package entry point is equivalent after package installation:

```bash
python -m pathnd_qc.batch run --slide_dir /data/slides --out reports \
  --workers 2 --no_metadata --run_tissue_segmentation
```

Here `run` is a subcommand. The other subcommands are `list` and `summarize`.

## Choose the analysis

Use the same component flags as for a single slide. No component flags means the default full
component set, subject to model availability, configuration and input requirements.
See [the component table](../../USAGE_CLI.md#3-selecting-components) before choosing a subset.
The `components.<name>` config switches filter every slide's requested components, including
explicit flags and aliases. A disabled model needs no checkpoint; selected components must complete.

Missing default pen and GrandQC assets are prepared once in the parent before timed slide workers
start, then reused by workers and later runs. Add `--no_model_download` to require existing assets.
Missing required models, invalid shared model paths or failed shared setup stop the batch before
workers start. Per-slide read or inference failures are recorded while other jobs continue.
See [model setup](../external/README.md) for storage locations and custom models.

```bash
# Thumbnail-level analysis: includes pen detection, which needs pen weights.
pathnd-qc-batch run --slide_dir /data/slides --out reports \
  --workers 2 --run_thumbnail --no_metadata

# Tissue segmentation, tile selection and tile metrics; no GrandQC required.
pathnd-qc-batch run --slide_dir /data/slides --out reports \
  --workers 2 --run_tissue_segmentation --run_tile_selection --run_tile_metrics --no_metadata
```

The batch parser accepts all nine component flags, `--run_thumbnail`, `--run_tiles`, the five
artifact flags, `--stain`, `--metadata` (repeatable), `--metadata_key`, `--no_metadata`, `--bank`,
`--norm_method`, `--pen_weights`, `--grandqc_repo`, `--grandqc_python`, `--no_model_download`,
`--no_save_artifacts`, and `--quiet`.
Use `--slides` or `--slide_dir` instead of single-slide `--slide`. Batch does not accept `-q` or
`--list_components`; use `--quiet` and the single-slide component listing instead.

## Choose the slides

### Use several metadata files

Repeat `--metadata` for each CSV you want checked:

```bash
pathnd-qc-batch run --slide_dir /data/slides --out reports \
  --workers 2 --run_tissue_segmentation \
  --metadata /data/metadata/collection_a.csv \
  --metadata /data/metadata/collection_b.csv
```

Every slide is looked up across **all explicitly supplied files**. Each file's slide-key column is
detected independently unless `--metadata_key COLUMN` supplies a common column name. Each report
records the file and row key that matched. Only the named files are checked.

If the same slide key occurs more than once, the first row in the supplied file order wins and
the duplicate is reported in `m1.ingestion.metadata.index_errors`; rows are not merged across files.
A slide absent from all supplied files continues with unresolved metadata. Without any `--metadata`
option, lookup is skipped. Local paths and accessible GCS, S3 and Azure paths can be mixed.

### Slide inputs

| Input | How to use it |
|---|---|
| Local folder | `--slide_dir /data/slides`; walks subfolders |
| Text list | `--slides slides.txt`; one local path or GCS/S3/Azure URI per line; blank lines and `#` comments ignored |
| CSV or TSV manifest | `--slides slides.csv`; a `slide` column plus optional `stain`, `bank`, `thumbnail`, `tissue_mask`, `pen_mask`, `fold_mask`, `tile_list` |

Example manifest (change all paths and stain labels to your own):

```csv
slide,stain,bank
/data/slides/slide_a.svs,Hirano,my_bank
/data/slides/slide_b.svs,GFAP,my_bank
```

This batch manifest assigns slides and per-slide options. It is different from `--metadata`, which
supplies biological metadata for lookup. Manifest stain/bank values override the global values.
Metadata is optional: only files named by `--metadata PATH` are read. Without this option, each slide
records metadata lookup as skipped; there is no automatic metadata-bank search.
Relative slide and artifact paths are resolved from your working directory, not the manifest folder.
Use absolute paths when sharing a manifest.

You can combine `--slides` and `--slide_dir`. Local paths are canonicalized, including symlinks;
existing files are also matched by filesystem identity, so hard links and case aliases merge. Cloud URIs are retained as supplied. Identical or complementary rows merge;
conflicting values refuse that slide and identify the field. Other jobs continue. Two distinct slides
with the same filename receive distinct batch job keys and separate path-derived output parents.
Metadata lookup joins by full slide path, so the two rows retain their own stains and other metadata.

`--slide_dir` cannot list a cloud bucket. Put remote slide URIs in a text list or manifest.
Providers can be mixed in one batch; the worker processes inherit their
[cloud credentials](../../USAGE_CLI.md#cloud-slides-gcs-aws-s3-and-azure) from the launch environment.

Folder extensions are discovery filters, not a promise that TiffSlide decodes every scanner
format. Files needing companion directories may require separate staging or conversion.

## Preview before running

```bash
pathnd-qc-batch list --slide_dir /data/slides --out reports \
  --run_tissue_segmentation --no_metadata
```

`--out` is required even for `list`, because artifact templates can refer to existing results.
This prints discovered jobs, manifest/artifact problems, and a shell-quoted first command.
Common option errors are refused before launching any child. Empty input is refused with exit 2;
a preview containing refused jobs also exits 2. It does not open slides or perform the complete
single-slide validation; a successful preview is not a successful run. UTF-8 manifests and text
lists may include a byte-order mark (BOM), including Excel's UTF-8 CSV exports.

## Resume or run a second analysis

Resume uses the latest valid status for the slide path inside the same `--out` and `--batch_id`, only when its
`batch_fingerprint` matches this invocation. The fingerprint includes forwarded options,
per-slide stain/bank, supplied input paths (including explicit metadata) and local size/modification
time, configured model resources (including nested GrandQC code/checkpoints), interpreter, resolved configuration,
and the installed Python/configuration source digest. A changed fingerprint or an older status
without this field schedules new work automatically.

If passing `--grandqc_python`, supply an absolute path to an existing executable
(for example `/opt/grandqc/bin/python`). Bare executable names are refused before launching
the batch so resume cannot track the wrong interpreter. Omit the option to use the configured
or default interpreter.

For a matching fingerprint:

| Latest status | Default behavior |
|---|---|
| `completed` | Skip the slide |
| `failed` | Skip unless `--retry_failed` or `--force` |
| `running`, or no valid status | Launch it again |

`--force` repeats all jobs. Use it when replacing remote content at the same URI, modifying files
while preserving their size and modification time, or repeating partial completed work. Fingerprints
are conservative invocation identifiers, not content hashes of large slides or remote objects; they
do not certify artifact completeness. A refusal before a status file exists can be attempted again.

```bash
# Retry slides whose latest recorded run failed in batch_study01.
pathnd-qc-batch run --slide_dir /data/slides --out reports --batch_id study01 \
  --retry_failed --run_tissue_segmentation --no_metadata

# Second pass: reuse tissue masks to compute tile selection and tile metrics.
pathnd-qc-batch run --slide_dir /data/slides --out reports \
  --force --workers 2 --run_tile_selection --run_tile_metrics --no_metadata \
  --tissue_mask "{latest_artifact}"
```

Artifact paths accept `{slide_id}`, `{stem}`, `{slide_dir}`, `{latest_run}` and `{latest_artifact}`.
`{latest_artifact}` resolves the matching artifact flag's file in the newest completed run,
across the current `images/`/`data/` layout, older `masks/` folders and historical flat folders.
It never searches earlier runs
when that newest completed run lacks the artifact. Historical templates such as
`{latest_run}/{slide_id}_tissue_mask.png` and `{latest_run}/masks/{slide_id}_tissue_mask.png`
also find masks now saved under `images/`.
`{latest_run}` selects
the newest completed run for that slide, not the newest run containing the requested artifact.
Keep the first-pass masks, and check the preview before a second pass. Repeating the second-pass
command may select a different latest run. An explicit per-slide artifact path avoids that ambiguity.

A selected component error or incomplete result makes the slide job fail. Available partial results
remain in its report. Use `--retry_failed` to retry failed requests; `--force` repeats completed ones.

## Scheduling and batch outputs

| Option | Default | Meaning |
|---|---|---|
| `--no_model_download` | false | Prohibit automatic model downloads; selected models must already exist |
| `--workers` | CPU cores minus one, minimum one | Concurrent slide processes |
| `--timeout_s` | `3600` | Per-slide subprocess deadline in seconds; `0` disables it |
| `--download_slots` | `4` | Concurrent localizations across workers on POSIX (`fcntl`); `0` disables it. On Windows it is not enforced; limit `--workers` instead |
| `--ext` | `.svs,.tif,.tiff,.ndpi,.scn,.mrxs,.bif,.vms,.vmu` | Comma-separated folder-discovery extensions |
| `--batch_id` | Current UTC date/time: `YYYYMMDD_HHMMSS` | Writes `<out>/batch_<batch_id>/`; reuse the same ID to resume that batch |
| `--python` | Current interpreter | Python executable used for each slide |

With `--slide_dir`, the output mirrors the input's relative subfolders:
`input/case/region/A.svs` becomes `<out>/batch_<batch_id>/case/region/A_output/<UTC date>/`.
With `--slides` (text list or CSV/TSV), it becomes
`<out>/batch_<batch_id>/sources/<path-sha256>/A_output/<UTC date>/`.
Every list-only source gets a SHA-256 parent derived from its canonical full path or cloud URI.
The location stays the same when input order changes or only a subset is rerun. The slide ID and
report filenames retain the original stem. Dated runs preserve earlier results.

Combining directory and list inputs preserves the mirrored location of slides found in the
directory. Explicitly colliding mirrored locations are still refused. Use `list` to preview paths.

Worker count is capped at the number of jobs. Each slide child receives at most
`available CPUs // workers` numerical threads (minimum one).
The CPU budget respects process affinity and the cgroup v2 quota when available. Smaller positive
`PATHND_CPU_THREADS`, `OMP_NUM_THREADS`, `MKL_NUM_THREADS` or `OPENBLAS_NUM_THREADS` values are
preserved as a lower shared limit. Torch uses that intra-op limit and one inter-op thread;
OpenMP, MKL, OpenBLAS and NumExpr inherit the same budget, including in backend subprocesses.
`batch.json` records `threads_per_worker` and the batch log prints it. The parent library process
is not reconfigured. For 32 available CPUs and eight workers, the automatic limit is four.

The CLI excludes `--out` from input discovery when it is inside `--slide_dir`, so saved TIFF masks
never become input slides on a subsequent batch. Do not use the input folder itself, or its ancestor,
as `--out`. Python callers can pass `discover(..., exclude_dir=out_dir)` for the same behavior.
`discover(slide_dir=...)` rows carry the relative parent through `build_jobs()` and `run_batch()`;
manually constructed `Job`s default to the batch root and may set a safe relative `output_subdir`.
Output placement is part of the resume fingerprint.

Each slide has its normal [run folder and report](../../USAGE_CLI.md#8-output-files).
The batch writes its bookkeeping alongside the slide folders in `<out>/batch_<batch_id>/`:

Open `index.html` to navigate this batch's slides. Its relative links remain usable when the
batch folder is copied or moved. Refused jobs have no run; interrupted jobs may have only a status file.

A new ID creates an independent batch even if those slides were processed elsewhere under `--out`.
When `--batch_id` is omitted, a run starting at 14:30:00 UTC on 23 September 2026 creates
`batch_20260923_143000`. The name omits hyphens and the timezone label; the clock remains UTC.
If that name is occupied, allocation adds `_2`, `_3`, and so on;
an automatic ID never resumes an existing batch, including when two processes start together.
To resume, repeat `--out` and `--batch_id` from the original command. Only matching runs inside that
batch are eligible for skipping; `--retry_failed` and `--force` apply within it. Slide run folders are
retained, the batch log is appended, and the bookkeeping files describe the latest invocation.
An exclusive `.batch.lock` prevents two invocations from writing the same batch concurrently.
Existing unrelated folders are refused. `list --batch_id ID` previews the exact destination without
creating it; without an ID it displays `batch_<batch_id>` as a placeholder.

| File | What to look for |
|---|---|
| `index.html` | This batch's jobs, outcomes, notes and links to saved slide results |
| `batch.log` | Child output and per-job outcomes prefixed by job key; child lines are logged at batch INFO level |
| `progress.json` | Counts, in-flight jobs, pending jobs, rate and estimated time remaining |
| `results.csv`, `results.json` | One outcome per job, including skipped/refused jobs |
| `batch.json` | Batch options, manifest/config/source hashes, owned Git commit and dirty status, execution context |
| `run-<job-token>.json` | Early per-job run-folder identity for timeout/cancellation recovery |
| `summary.csv`, `summary.json` | One row per run folder in this batch, including earlier attempts; other batches are excluded |

Progress is updated while jobs run. Download slots bound downloading, not computation or total RAM.
Timeouts and cancellation terminate subprocess groups on POSIX; Windows uses `taskkill /T`.
Ctrl-C and SIGTERM to the CLI terminate all live children and record interruptions. Child groups
receive TERM together, share a grace deadline, then receive KILL if needed. The CLI restores its
previous signal handler after returning; Python library callers own their signal policy.

Each child publishes its run folder to an atomic `run-<job-token>.json` file in the batch folder
before processing. Timeouts and cancellation retain this identity in results and change an existing
`running` status to `failed`, with `batch_outcome` and a cause. Thus a timed-out matching invocation
needs `--retry_failed` or `--force`. A forced kill of the batch itself or inability to write output
can prevent final files from being saved. Ordinary per-slide failures are recorded so
other jobs can continue; this is not protection against failure of the batch process itself.

CLI exit `0` means jobs completed or were skipped, and the summary had no read errors. Exit `1`
indicates a failed, refused, timed-out, interrupted or errored job, a summary read error, or an I/O
failure. Invalid batch arguments can return `2`. Exit `0` does not certify slide quality or completeness.

Rebuild a summary without running slides:

```bash
pathnd-qc-batch summarize --out reports --to reports_summary
```

Malformed historical reports/status files become `read_error` rows. This command returns `1` if
there are read errors. It summarizes all discovered run folders, not just the most recent batch.

Each summary row includes `pipeline_version`, `git_commit`, `git_dirty`, `git_source`,
`source_sha256` and `config_sha256` from that run's saved report. These identify the pipeline and
settings used for each slide, even when the output directory contains runs from different revisions.
Re-summarizing does not substitute the currently installed revision. Missing fields in historical
reports remain `null` in JSON and empty in CSV. See the
[report provenance guide](../reporting/README.md#pipeline-revision-and-reproducibility).

Summary discovery requires a valid `<slide_id>_status.json`. A report without its status file
is not included. Keep the whole run folder when moving results.

## Python API

After installing the package:

```python
from pathnd_qc.batch import manifest, runner

jobs = manifest.build_jobs(manifest.discover(slide_dir="/data/slides"), out_dir="reports")
record = runner.run_batch(
    jobs, "reports", workers=2,
    run_args=["--run_tissue_segmentation", "--no_metadata"],
)
paths = record["summary_paths"]
```

`run_batch()` writes bookkeeping, batch-scoped summaries and navigation before releasing the batch
lock. `manifest.py` handles discovery and inputs, `runs.py` handles run history,
`runner.py` manages processes, and `summary.py` collects existing reports.

## Unattended execution

The parent resolves missing selected model assets once before launching timed slide workers.
Managed setup holds a cross-process lock and publishes verified weights by atomic rename.
`--no_model_download` and `PATHND_NO_MODEL_DOWNLOAD=1` disable automatic downloads.
Python children use `-P`, preventing implicit imports from the caller’s working directory;
explicit `PYTHONPATH` entries still apply.

In `summary.csv`, `warnings` contains observations and routing notices; `execution_reasons`
contains execution failures or incomplete work. `mpp_effective` is the thumbnail’s achieved
resolution (or assumed resolution for a supplied thumbnail), with `mpp_source` identifying
computed/supplied input. `mpp_base` and `mpp_base_source` retain the acquisition scale.
Thumbnail resolution stays empty when no thumbnail was read. `dataset` falls back to `--bank`
when metadata does not provide one.
