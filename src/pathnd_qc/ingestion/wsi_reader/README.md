# WSI Reader (`wsi_reader.py`)

[Package overview](../../README.md) · [CLI usage](../../../USAGE_CLI.md) · [Library usage](../../../USAGE_LIBRARY.md) · [Parent module](../README.md)

[What it does](#what-it-does) · [How it works](#how-it-works) · [Usage](#usage) · [Defaults](#defaults) · [Historical observations](#historical-observations) · [References](#references) · [Resource limits and localization failures](#resource-limits-and-localization-failures)

## What it does

The pipeline's only door onto slide pixels.

**In:** a local slide path or GCS (`gs://`), S3 (`s3://`), Azure Blob/ADLS Gen2
(`az://`, `abfs://`, `abfss://`) object URI.
**Out:** an open slide, its acquisition metadata, and image data read at **a requested
microns-per-pixel scale** — a whole plane, a single window, or a thumbnail.

Read-only. Nothing here writes to a bucket.

| method | what it does |
|---|---|
| `open_slide` · `slide` · `open_from_row` | open a slide (`slide` is a context manager) |
| `get_slide_info` · `get_metadata` | dimensions, levels, resolution, magnification, vendor, scanner |
| `read_thumbnail` · `read_region` | reads at the slide's own pyramid levels |
| **`read_at_mpp`** | a whole plane at a requested resolution — M2's input |
| **`read_window_at_mpp`** | one window at a requested resolution — M3's input |
| **`localize`** | download the slide to a local temp file, verify it, delete on exit |
| `load_metadata(csv_path)` · `iter_slides` | Legacy helpers; a metadata path is required — see [`metadata/`](../metadata/README.md) instead |

## How it works

**Reading at a fixed resolution.** The reader picks the coarsest pyramid level close enough to the
target, then resamples toward the requested scale. Integer image dimensions can introduce rounding;
inspect `achieved_mpp` and `plane_dims` in the returned provenance. Unusable scales are refused.

**Windows and whole planes stay consistent.** Windows use coordinates in the target-resolution plane.
Shared geometry and resampling rules keep reads aligned; integer rounding and edge handling mean
this is not a promise of bit-for-bit equality with a cropped whole-plane read.

**`localize`** streams the slide to a local temp file, hands back the path, and deletes it on context
exit. Already-local paths pass straight through, and a download failure yields `None`. An `atexit`
hook also attempts cleanup on normal interpreter shutdown; a forced kill can leave temp files.

Each download attempt runs in an isolated process that owns the source and destination handles,
including source metadata lookup and checksum verification. The parent monitors increasing copied
or hashed byte counts. If progress stalls, it terminates and reaps the worker before retrying or
removing the file, so a timed-out worker cannot keep writing into a later attempt. A progressing
transfer may take longer than the inactivity limit. Process termination adds a cleanup grace period.

Direct streamed reads and metadata loading use native GCS/HTTP/S3 request timeouts. Azure receives
the driver's connection/read timeout options, but adlfs does not apply those to every streaming
or metadata operation. These options do not bound a complete operation or authentication.
The isolated download watchdog applies to all providers. Waiting for a download slot retains the batch
policy: the enclosing slide timeout bounds it when configured; standalone waits have no separate
deadline. Slot waits are logged periodically.

## Usage

```bash
pathnd-qc --slide /data/slides/example.svs --out reports \
  --run_tissue_segmentation --no_metadata
```

Run these examples after [setup](../../../USAGE_CLI.md). Replace the URI with an accessible slide
and check returned failures before using the image.

```python
from pathnd_qc import WSIReader

reader = WSIReader()

with reader.slide("gs://my-bucket/slides/slide.svs") as s:      # streams, no download
    info         = reader.get_slide_info(s)
    plane, prov  = reader.read_at_mpp(s, 8.0)             # M2's input

with reader.localize("gs://my-bucket/slides/slide.svs", verify=True) as local:
    if local is None:
        raise RuntimeError("Slide download failed; inspect reader.last_localize")
    with reader.slide(local) as s:                        # M3's input
        tile, prov = reader.read_window_at_mpp(s, 0.50, x=1024, y=512, w=512, h=512)
```

- Slide acquisition is planned from the selected components and supplied inputs.
- **Needs** credentials for the selected provider; see [cloud setup](../../../USAGE_CLI.md#cloud-slides-gcs-aws-s3-and-azure).
- `WSIReader` and the legacy `GCSWSIReader` name refer to the same implementation.
- **Both read-at-resolution methods return a `(image, provenance)` tuple.** The provenance dict holds
  `level · source_mpp · achieved_mpp · resample_ratio · direction · plane_dims · error`; `direction`
  is `"up"`, `"down"` or `"none"`. On failure the image is `None` and `error` says why.
- A supplied thumbnail can avoid the M2/M4 image read. Tile selection still needs slide information;
  tile metrics/artifacts need slide pixels. Metadata lookup runs only for explicitly supplied CSVs.

Transfer verification compares the size when available, plus GCS `md5Hash` or Azure blob
`content_md5` when present. S3 ETags are not treated as checksums: multipart uploads and encryption
can change their meaning. A size-only result is recorded as `check: "size"`, not as an MD5 check.
If source-info lookup fails, the copy may be returned with `verified: null` and
`check: "unavailable"`. If lookup succeeds but provides neither size nor MD5, the current
verifier returns `verified: false`, `check: "none"`, and rejects the download. An available
size/MD5 mismatch also fails it; inspect `last_localize` for the actual outcome.
See [S3 ETag semantics](https://docs.aws.amazon.com/AmazonS3/latest/API/API_Object.html) and
[Azure content MD5](https://learn.microsoft.com/en-us/python/api/azure-storage-blob/azure.storage.blob.contentsettings).

## Defaults

Values live in [`config/defaults.json`](../../config/defaults.json). Edit that file, point
`$PATHND_CONFIG` at your own JSON, or pass the argument directly — an explicit argument always wins.

| key | default | what it does |
|---|---|---|
| `m2.read.target_mpp` | `8.0` | the resolution the analysis image is read at |
| `m2.read.mpp_tolerance_rel` | `0.1` | how much upsampling is accepted before reading a finer level (the default `tol`; M3's tile reads pass their own, derived from M1's bound). Beyond the tolerance the resolver **refuses** — the caller decides, so a wrong-unit MPP never triggers a whole-level-0 read |
| `m2.read.max_read_px` | `67108864` | the size bound on `read_at_mpp` — `tol` bounds the level's *coarseness*, not its *size*. A level above the cap (a pyramid-less file's level 0) is read in horizontal bands with a scale-dependent source-row margin and resampled band by band; the provenance records `read_mode: "banded"` and `bands`. Pass `max_read_px` to override per call |
| `ingestion.localize_retries` / `localize_retry_backoff_s` | `3` / `2.0` | the download (and its transfer check) is retried, bounded, with exponential backoff; `last_localize.attempts` records the count |
| `ingestion.localize_stall_timeout_s` | `120.0` | no bytes for this long fails the attempt with `TimeoutError: download stalled …` instead of hanging |
| `ingestion.thumbnail_max_size` | `1024` | longest edge of `read_thumbnail`'s output |
| `ingestion.verify_localize` | `true` | attempt source-size/checksum verification. Unavailable verification is recorded separately from a mismatch; inspect `last_localize`. **A copy that fails an available check is a failed download**: `localize` yields `None`, the run fails at `m1.localize` naming the mismatch, and the bytes are never read  |

## Historical observations

The following measurements were recorded in earlier corpus runs and were not rerun for this
documentation update. They are examples, not speed, coverage or correctness guarantees.

**Resolution standardisation is verified on all three pyramid shapes in the corpus**, every target
landing within 0.2 %:

| cohort | source resolution | → 8.0 µm/px | direction |
|---|---|---|---|
| SEA-AD | 8.0265 | ×0.9967 | up |
| PART `42669` | 8.1074 | ×0.9868 | up |
| QSBB `Case_5_1_ASyn` | 3.6876 | ×2.1694 | **down** |

- **25,941 tiles decoded** across two codecs with **zero read failures**.
- **Downloading a slide costs 15.7–90.0 s**, depending on size.
- **Reads are driven by resolution, not by the magnification label**, because the two do not line up
  exactly across the corpus: SEA-AD reports 20× at 0.5016 µm/px, QSBB reports 40× at 0.2305 µm/px.
  Working from the resolution is what lets every slide be read on the same scale.

**Why some runs download and others don't.** Streaming individual tiles at full resolution is very
slow, so anything reading many tiles is faster downloading the slide once. Tile-reading M3 work therefore localizes remote inputs.
M2 and M4 normally share a streamed image read, but a remote source level above the configured
whole-read cap is also localized before banded reading. A supplied analysis image can avoid that read.
Already-local slides are not copied by `localize`.

## References

- TiffSlide — [github.com/Bayer-Group/tiffslide](https://github.com/Bayer-Group/tiffslide)
- fsspec / gcsfs — [github.com/fsspec/gcsfs](https://github.com/fsspec/gcsfs)

## Resource limits and localization failures

`read_at_mpp` checks the materialized output plane against `m2.read.max_plane_px`
(default 67,108,864 pixels) before decoding any pixels. Its separate `max_read_px` cap
continues to control source read bands. The public `max_plane_px=` argument can override
the output cap; this is a plane budget, not a process-memory guarantee.

`read_thumbnail` refuses to decode a smallest pyramid level larger than
`m2.read.max_read_px`, logs the reason and returns `None`. Use `read_at_mpp` to read
a bounded analysis plane from a pyramid-less WSI.

Localization retries transient transfer/verification errors up to `localize_retries`.
Missing objects, permissions and wrong file/directory kinds stop after one attempt.
On POSIX systems, `PATHND_LOCALIZE_SLOTS` must be `<directory>:<positive integer>`; invalid
settings and lock failures propagate, with the temporary download file removed. The lock uses
`fcntl`: on platforms without it (including Windows), this setting is ignored and there is no
shared download-slot limit. Limit batch workers to control concurrent downloads there.

Remote streams use explicit GCS read-ahead caching, with adaptive background prefetch disabled.
The slide owns its remote stream and closes it on normal exit and failed construction.
See the [GCS cache selection rules](https://gcsfs.readthedocs.io/en/latest/prefetcher.html).
