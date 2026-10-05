# Reading and writing reports

[Package overview](../README.md) · [CLI usage](../../USAGE_CLI.md) · [Library usage](../../USAGE_LIBRARY.md)

[Execution facts and SME interpretation](#execution-facts-and-sme-interpretation) · [What is in the file](#what-is-in-the-file) · [Pipeline revision and reproducibility](#pipeline-revision-and-reproducibility) · [Thresholds](#thresholds) · [Python functions](#python-functions)

Each pipeline run normally produces `<slide_id>_report.json` inside its run folder.
The pipeline also writes `index.html` with links to saved outputs and version identity. Runs are
grouped under `batch_<batch_id>/<relative input parent>/<slidename>_output/<UTC date>/` for folder
batches, or `batch_<batch_id>/sources/<path-sha256>/<slidename>_output/<UTC date>/` for lists. Single-slide commands use
`<slidename>_output/<UTC date>/` directly under `--out`; see the
[output layout](../../USAGE_CLI.md#8-output-files). Standalone `write_report()` still writes only the JSON
directly into the caller's directory.
Start with [the report guide](../../USAGE_CLI.md#9-reading-a-report) for field descriptions and
examples of interpreting results. This module assembles component outputs, compares configured
report thresholds, and writes JSON; the pipeline adds orchestration and artifact provenance.

## Execution facts and SME interpretation

| Field | Meaning |
|---|---|
| Top-level `error` | Fatal run failure, or `null` if none was recorded |
| `provenance.execution.complete` | Whether requested work completed without tracked omissions/failures |
| `provenance.warnings` | Acquisition observations, scale and stain routing notices |
| `provenance.execution.reasons` | Execution failures and omissions |
| `verdict` | Results of configured report-threshold comparisons |
| `m1.ingestion.checks` and `m1.ingestion.decision` | Acquisition/integrity checks and the ingestion decision |

Every selected component must complete. A handled component error, rejected analysis, or partial
tile read makes execution incomplete, records a top-level `IncompleteRun` error, writes status
`failed`, raises `RunFailed` in Python, and exits `1` in the CLI. Usable partial outputs are retained.
Inspect component `error` fields, `components_failed`, and execution reasons for the cause.
Scientific interpretation and assessment of these results belong to SMEs. Inspect the acquisition, integrity, localization and resampling details for your use case.

Fatal errors after preflight normally produce an error block with `stage`, `type`, `message`, and
`traceback` when an exception was caught, and exit `1`. Unreached components are marked skipped. Saving each artifact is attempted
independently; artifact save failures make the run fail. A fine-resolution TIFF writer failure
retains measured tiles, omits the mask and fails the run with incomplete execution. Report writing is best effort
when the filesystem itself fails. Preflight refusal exits `2` and creates no slide report, although the parent
output directory may already exist.

## What is in the file

The report contains slide identity, timestamps, configuration provenance, `m1` ingestion,
`m2` slide QC, `m3` tile QC, `m4` normalization, timing, verdict and run error information.
Ingestion metadata is embedded under `m1.ingestion`; the main pipeline does not write a separate
ingestion record. Resampling provenance records requested and achieved scales for reads performed.
A skipped read cannot supply measured resampling provenance.

`components_run` means the component was reached, not necessarily successful. Skipped sections carry
an `error` explaining the skip, generally zero runtime, and missing or null measurement fields. Do not interpret a null
metric as a measured zero. Not every skipped section has `error_type`; handle missing fields.

Reports can include local paths and tracebacks. They are local analysis records, not a BDSA export
format. The [usage guide](../../USAGE_CLI.md#9-reading-a-report) explains dimensions, tile outliers,
execution status, and timing conventions.

## Pipeline revision and reproducibility

`provenance.components_disabled_config` lists components excluded by configuration. These are
also in `components_skipped` and do not make execution incomplete. Every enabled, selected
component must complete; required artifacts must be supplied if their producer is disabled.

Every new slide report includes these fields under `provenance`, including failure reports when
publication succeeds. The pipeline captures them before slide processing begins; a later checkout
or commit cannot relabel that run. No user-supplied hash or configuration is needed.

| Field | Meaning |
|---|---|
| `pipeline_version` | Installed package version, such as `0.5.0` |
| `git_commit` | Full Git commit of the pipeline checkout, or the checkout used to build the installed package |
| `git_dirty` | `true` for local package/build-file changes or detected edits to an installed build; `false` for a known clean source; `null` if unknown |
| `git_source` | `checkout`, `build`, or `unavailable`: where the Git identity came from |
| `source_sha256` | Fingerprint of runtime Python files, default configuration and model catalog; excludes tests and generated outputs |
| `config_sha256` | Fingerprint of the resolved configuration for this run |

Git discovery follows the pipeline's source location, regardless of the directory from which the
command is launched. Moving or cloning the same revision preserves its identity. Dirty status is
scoped to the package and neighboring build files; unrelated workspace edits do not change it.
The source fingerprint distinguishes local code changes that share the same commit.

Normal wheel and source-archive builds embed the identity, so an installation can report the build's
commit without Git or a `.git` directory. A plain source copy with neither Git history nor embedded
build metadata reports `git_commit: null` and `git_dirty: null`; it never borrows the caller's Git
revision. Git lookup failure does not stop slide processing.

For comparisons, inspect the commit, dirty flag, source fingerprint and configuration fingerprint
together. Matching commits alone do not establish equivalent settings, models or input artifacts;
their details remain in the existing provenance sections.

Batch `summary.csv` and `summary.json` expose all six fields from each saved report. Rebuilding a
summary keeps the original identity. Historical reports without these fields remain readable and
show unknown values. Standalone `build_report()` captures provenance when called; an orchestrator
can pass an earlier snapshot through its optional `run_provenance` keyword.

## Thresholds

The shipped `thresholds` entries in [defaults.json](../config/defaults.json) have null bounds.
With no numeric metrics compared against active bounds, `verdict.passed` is `null` and
`n_thresholds_checked` is zero. This does not mean the slide passed QC.

Set lower and/or upper bounds for selected metrics through a [configuration override](../config/README.md).
Metric names are flat dotted keys, for example `m2.focus.focus_score`. Entries may have `by_stain`
overrides resolved through stain aliases. A value outside a bound adds a verdict flag; this report
comparison does not remove tiles or stop the run. Separate acquisition gates and tile-selection/drop
rules still operate even with every report threshold null.

## Python functions

| Function | Responsibility |
|---|---|
| `build_report(slide_id, slide_path, ...)` | Assemble supplied stage sections, config provenance, timing and verdict |
| `resolve_bounds(bounds, stain_type)` | Select general or per-stain bounds |
| `evaluate_thresholds(report, table=None)` | Compare available numeric metrics against bounds |
| `collect_timing(report, extra=None, ...)` | Collect stage/component timings and supplied timing extras |
| `write_report(report, out_dir=None)` | Atomically write `<slide_id>_report.json` |

A minimal standalone example after package installation:

```python
from pathnd_qc.reporting.reporting import build_report, write_report

report = build_report("example", "/data/slides/example.svs")
path = write_report(report, "reports/example")
```

This only assembles a report; it does not analyze the slide or supply the full pipeline's
execution/artifact tracking. Use `pathnd_qc.run(...)` to execute analysis.

`write_report()` resolves an explicit output directory first, then `PATHND_REPORT_DIR`, then
`report.out_dir` (default `reports`). The main pipeline passes its own run directory, so its normal
output location is controlled by `--out` or `report.out_dir`. Relative paths use the caller's working
directory. Reports identify schema `2.0` through code-owned `provenance.report_version`.
See the [migration guide](../MIGRATION.md) for consumer changes; configuration cannot override this identity.

`report.focus_outlier_n` defaults to 20. The report also includes dropped or errored tiles, so this
is not a promise that the combined outlier list contains at most 20 entries. Timing covers the
pipeline's measured interval, including recorded acquisition/localization work; it is not the entire
CLI process duration, and final serialization is outside the measured run total.
