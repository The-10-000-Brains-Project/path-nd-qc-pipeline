# Dataset Adapters (`adapters.py`)

[Package overview](../../README.md) · [CLI usage](../../../USAGE_CLI.md) · [Library usage](../../../USAGE_LIBRARY.md) · [Parent module](../README.md)

[What it does](#what-it-does) · [How it works](#how-it-works) · [Usage](#usage) · [Defaults](#defaults) · [Performance status](#performance-status)

> ## ⚠️ A placeholder — not part of the running pipeline
>
> The pipeline (`pipeline.run()`) never calls `adapt()`. Reading metadata *is* pipeline work, and that is
> [`metadata/`](../metadata/README.md), which replaces this module in practice. The gaps below are
> known and deliberately left alone.

## What it does

Maps one dataset's raw metadata row onto the common `source` block of an ingestion record, so a PART
slide and a SEA-AD slide come out the same shape.

```
dataset · slide_id · participant_id · brain_region · stain_type · slide_reference
```

## How it works

A column lookup per dataset, plus cleanup that turns empty and not-a-number values into `None`.
`adapt_part` additionally rewrites a Windows file path onto a cloud storage prefix.

`adapt_seaad` and `adapt_part` are registered in an `ADAPTERS` table, and `adapt(dataset, row)`
dispatches on an explicit dataset name. Adding a dataset means writing an `adapt_<name>` and
registering it.

**The dataset is passed in, never sniffed.** SEA-AD, ACT and PART share an identical 35-column schema
— same names, same order — so no adapter could tell them apart from the columns alone.

## Usage

```python
from pathnd_qc.ingestion.adapters.adapters import adapt
source = adapt("SEA-AD", row)      # -> the 6-key source block
```

Not reachable from `pipeline.run()`. `ingest_slide` in
[`ingestion_checks`](../ingestion_checks/README.md) is the only caller, and the pipeline does not use
that path either.

## Defaults

**No config keys.** These legacy module-level defaults are not validated storage locations:

| constant | value | what it does |
|---|---|---|
| `DEFAULT_PART_GCS_PREFIX` | `gs://pathnd/part/slides` | prefix that PART paths are rewritten onto |
| `PART_WIN_ROOT` | `W:\Collection_PART_DATA_MINERVA` | the Windows root that gets stripped first |

`adapt_part` takes `gcs_prefix` as an argument, so the prefix can be overridden per call.

## Performance status

**It does not work on real PART data, and that is knowingly uncorrected** — the module is a
placeholder, and [`metadata/`](../metadata/README.md) does the job instead.

1. **`adapt_part` reads the wrong column names.** It looks for uppercase `SLIDE_IDS`, `PWG_ID`,
   `BLOCK_IDS`, `STAINS` and `SLIDE_PATHS`; the real PART export carries lowercase SEA-AD-style
   columns and no uppercase variant at all. For the lowercase export schema described in the source, these uppercase lookups cannot
   populate the intended fields. `adapt_seaad` on the *same* row extracts all of them
   correctly — so the field logic is fine and only the names are wrong. It is not a drop-in
   substitute, though, because it stamps the dataset as `SEA-AD`.
2. **The PART storage prefix is an unverified placeholder.** `gs://pathnd/part/slides` is marked as an assumption in
   the source; the real slides live under a different prefix entirely.
3. **There is no QSBB adapter**, and QSBB's slide-level artifact labels — the only artifact ground
   truth in the corpus — have no slot in this schema. `metadata/` carries them in an `extra` block.
4. **`brain_region` is unreliable for PART.** The column it reads holds tissue-block letters, not
   regions, and the same letter spans different regions. The reliable signal is the file path, which
   is what `metadata/` parses.

These limitations are documented legacy assumptions. The main pipeline uses metadata lookup instead;
calling `adapt_part` or the older `ingest_slide` helper directly can still exercise this code.
