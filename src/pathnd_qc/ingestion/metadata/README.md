# Slide metadata

[Package overview](../../README.md) · [CLI usage](../../../USAGE_CLI.md) · [Library usage](../../../USAGE_LIBRARY.md) · [Parent module](../README.md)

[Supply a metadata path](#supply-a-metadata-path) · [Matching and field rules](#matching-and-field-rules) · [Explicit source specs in Python](#explicit-source-specs-in-python) · [Reports and diagnostics](#reports-and-diagnostics)

Metadata describes a slide: its stain, participant and brain region. The reader matches the
full slide path to a row in a **user-supplied CSV** and records which file supplied the values in
`m1.ingestion.metadata`. It does not read slide pixels.

There is no metadata-bank registry, automatic search, or built-in cloud metadata path. Without a
metadata path, lookup is skipped and analysis continues using explicit arguments and the slide's
embedded scanner information.

## Supply a metadata path

After [setup](../../../USAGE_CLI.md#1-setup-and-first-run), create `my_metadata.csv`:

```csv
slide_paths,stain_type,brain_region
/data/slides/example.svs,Hirano,Frontal
```

Use the actual slide path, stain and region:

```bash
pathnd-qc --slide /data/slides/example.svs --out reports \
  --metadata my_metadata.csv --run_tissue_segmentation
```

Repeat `--metadata PATH` to select several files; only those files are read, in the supplied order.
Local paths and accessible GCS, S3 and Azure URIs are supported, using the same
[cloud credentials](../../../USAGE_CLI.md#cloud-slides-gcs-aws-s3-and-azure) as slide reads.
The key column is detected from `slide_paths`,
`gs_file_path`, `slide_path`, `file_path`, or `path`, in that order. For another column name, add
`--metadata_key column_name`; it applies to all supplied files and requires `--metadata`.

`--no_metadata` is optional when no path is supplied. It explicitly skips lookup and cannot be
combined with `--metadata` or `--metadata_key`. `--stain` overrides a CSV's stain for analysis.

For the Python pipeline:

```python
from pathnd_qc import run

result = run(
    "/data/slides/example.svs", out_dir="reports",
    components={"tissue_segmentation"}, metadata_paths=["my_metadata.csv"],
)
```

The same `--metadata` options work in the batch runner. A batch manifest chooses slides to run;
it is separate from a metadata CSV that describes them.

## Matching and field rules

- Matching uses the full cloud URI or resolved local path, including its extension.
  `/a/slide.svs` and `/b/slide.svs` are distinct. Local relative paths resolve against the
  caller’s working directory; use absolute paths in shared CSVs. `gcs://` and `gs://` are aliases.
  Filename-only keys do not match slides in another directory.
- Duplicate keys keep the first row and record a warning in `index_errors`. Unreadable files or
  missing key columns also appear there; other explicitly supplied files can still load.
- The standard record contains 35 columns (`STANDARD_COLUMNS` in [metadata.py](metadata.py)).
  Absent values are null. Additional columns are retained only when named in an explicit source
  spec's `extra` list.
- The metadata layer does not deliberately remap stain labels or participant IDs. CSV loading uses
  pandas type inference, so numeric-looking identifiers can lose formatting such as leading zeros.
  Each analysis stage applies its own stain routing. Participant identifiers do not necessarily
  group people across datasets.
- A bare path does not declare a dataset label. A CSV column named `dataset` does not set it.
  The low-level source spec can declare `dataset`. `--bank` chooses normalization references and
  supplies the report's dataset label when metadata has not provided one. It preserves an existing
  dataset label and does not select metadata files.

## Explicit source specs in Python

For custom column names or extra fields, pass source specs directly to the metadata API:

```python
from pathnd_qc.ingestion.metadata import metadata as md

sources = [{
    "path": "my_metadata.csv", "dataset": "my_dataset", "key": "slide_paths",
    "columns": {"stain_type": "stain_type"}, "extra": ["reviewer_note"],
}]
result = md.lookup("/data/slides/example.svs", sources=sources)
source, origins = md.merge_source(result["result"], "/data/slides/example.svs")
```

`columns` maps standard field names to CSV names. The optional `region_from_path` setting parses
text before the first `_` in the containing folder when the region column is missing; use it only
if that convention matches your files. These specs are explicit arguments, not configuration
sources or a persistent registry. `md.sources_from_paths([...])` creates specs for ordinary paths.
`md.build_index(sources)` indexes the supplied list afresh; no default-source cache is retained.

## Reports and diagnostics

A lookup returns `result`, `params`, `runtime_s`, `error`, `error_type` and `index_errors`.
`index_errors()` describes the latest lookup/build in the calling thread. One call's sources and
errors cannot become another call's fallback.

When no path is supplied, the report records `found: false`, `skipped: true`, and
`skip_reason: "no_metadata_path"`. An explicit opt-out uses `"disabled_by_request"`. A supplied file
that was searched but contained no matching row has `found: false` and `skipped: false`.
Resolved values appear in `record`, `extra`, and `metadata_source`; `source_resolved_from` records
the origin of routing values separately.

Old `ingestion.metadata` and `ingestion.metadata_csv` settings are no longer accepted. See the
[0.5.0 migration notes](../../MIGRATION.md#metadata-inputs-in-050).
