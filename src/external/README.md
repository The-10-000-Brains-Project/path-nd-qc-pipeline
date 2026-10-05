# Checkout model assets — `src/external/`

[Source overview](../README.md) · [Automatic model setup](../pathnd_qc/external/README.md)

This folder holds checkout model assets, checksum records and a compatibility setup wrapper.
It is distinct from `src/pathnd_qc/external/`, which contains the packaged installer and catalog.
These weights and upstream checkouts are excluded from wheels and source distributions.

## Files in this folder

| Item | Purpose |
|---|---|
| [weights/](weights/) | Local GrandQC checkpoints; the pen model is downloaded from upstream, not stored here |
| [weights/SHA256SUMS](weights/SHA256SUMS) | Checksums for the local checkpoint files |
| [setup.sh](setup.sh) | Wrapper around the packaged GrandQC setup command |
| [grandqc/](grandqc/) | Checkout copies of the compatibility bridge and patches; managed setup uses the packaged copies |

## Model provenance

| File or backend | Size | Source | Identifier | Published | Pulled |
|---|---|---|---|---|---|
| `GrandQC_MPP1.pth` | 25.4 MB | [Zenodo 14041538](https://zenodo.org/records/14041538) | `10.5281/zenodo.14041538` | 2024-11-09 | 2026-08-17 |
| `GrandQC_MPP2.pth` | 25.4 MB | [Zenodo 14041538](https://zenodo.org/records/14041538) | `10.5281/zenodo.14041538` | 2024-11-09 | 2026-08-17 |
| `GrandQC_MPP15.pth` | 25.4 MB | [Zenodo 14041538](https://zenodo.org/records/14041538) | `10.5281/zenodo.14041538` | 2024-11-09 | 2026-07-13 |
| `Tissue_Detection_MPP10.pth` | 26.6 MB | [Zenodo 14507273](https://zenodo.org/records/14507273) | `10.5281/zenodo.14507273` | 2024-11-09 | 2026-08-17 |
| `pen.pt` | 104.5 MB | [WSISegQC Google Drive folder](https://drive.google.com/drive/folders/1P3E9kZDM7A7cM06RR47kywvQCL3X0HJz) | WSISegQC `e8a76b1` (source reference; see below) | 2024-10-14 | 2026-08-26 |
| GrandQC backend (cloned, not vendored) | — | [cpath-ukk/grandqc](https://github.com/cpath-ukk/grandqc) | `002688d7` (full pin below) | 2025-12-27 | 2026-08-17 |

**Published** is the recorded source release date; for the GrandQC backend it is the commit date.
**Pulled** is the recorded download date onto the original working machine. These describe the
original assets, not the date of a later automatic installation.

- **GrandQC source pin:** `002688d74a4ac86dfbb816a96df8461d6080f88a`, dated
  **2025-12-27T12:24:00Z**. Managed setup uses this commit and the compatibility patches identified
  by `patch_version` in the [packaged catalog](../pathnd_qc/external/catalog.json).
- **WSISegQC source reference:** `e8a76b1e653cd55eef30c2fe9fe0631ee3a64c8b`, dated
  **2025-05-23T06:05:06Z**. This is the repository revision whose README links the Drive weights,
  **not a commit or checksum of `pen.pt` itself**. The pen model's published date above,
  **2024-10-14**, is the recorded Drive-file date.
- **Checkpoint identity:** the four GrandQC files are identified by their SHA-256 values in
  [weights/SHA256SUMS](weights/SHA256SUMS); all five checkpoints, including `pen.pt`, are in the
  [download catalog](../pathnd_qc/external/catalog.json). Dates and source commits do not replace
  these checksums. The Zenodo DOIs identify the records; neither record supplied a version field
  in the recorded provenance check.

## Use these assets

Normal pipeline runs automatically obtain missing default assets in user data storage. To prohibit
model downloads, use `--no_model_download`, Python `download_models=False`, or
`PATHND_NO_MODEL_DOWNLOAD=1`. The model setup guide explains checks, locations and custom backends.

To explicitly reuse the files here, run from the repository root:

```bash
pathnd-qc setup grandqc --weights-dir "$PWD/src/external/weights"
```

The GrandQC command reuses these checkpoints but still needs Git and network access to fetch its
pinned source. For an existing compatible checkout, use `setup grandqc --repo /absolute/path/to/inference-directory`
instead; see [custom models](../pathnd_qc/external/README.md#use-your-own-version).

The pen model is not stored in this repository; `pathnd-qc setup pen` or the first pen run downloads
it from upstream and verifies its checksum. To use a copy you already have, register it with
`pathnd-qc setup pen --weights /absolute/path/to/pen.pt` or pass it as `--pen_weights`. See
[model locations](../pathnd_qc/external/README.md#model-locations) for relative defaults and managed storage.
`setup.sh` forwards to the package's GrandQC installer. Set `PATHND_PYTHON` to your environment's
Python if `python3` points elsewhere. The wrapper finds the package relative to its own location.

The authoritative catalog, downloader and patches are in the
[packaged installer](../pathnd_qc/external/README.md). Original checkpoint checksums are in
[weights/SHA256SUMS](weights/SHA256SUMS).

Upstream code/model terms apply to these assets: GrandQC is non-commercial (CC BY-NC-SA 4.0 /
CC BY-NC 4.0) and WSISegQC publishes no license. See [third-party notices](../THIRD_PARTY_NOTICES.md). Sources:

- GrandQC: https://github.com/cpath-ukk/grandqc
- Artifact checkpoints: https://zenodo.org/records/14041538
- Tissue checkpoint: https://zenodo.org/records/14507273
- Pen model: https://github.com/abhijeetptl5/wsisegqc
