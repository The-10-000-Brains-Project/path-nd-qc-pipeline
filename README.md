# Path-ND QC

> **Status:** Early beta, for research use only, not validated for clinical use.

Path-ND QC is a whole-slide image quality-control and preprocessing pipeline for neuropathology
research in The 10,000 Brains Project. It helps researchers inspect slide quality, identify tissue
and artifacts, measure image tiles, and normalize stain appearance against chosen references.
Its reports bring measurements, visual outputs and execution details together for review.

[Features](#key-features) · [Choose an interface](#three-ways-to-run) ·
[Run the full pipeline](#run-the-full-pipeline) · [Repository overview](#repository-overview) · [License](#license) · [Long-term goal](#long-term-goal)

## Key features

- **Slide quality control:** tissue segmentation, fold and pen detection, staining measurements,
  and focus assessment.
- **Tile analysis and normalization:** tissue-based tile selection, tile measurements, GrandQC
  artifact detection, and stain normalization using configured reference targets.
- **Modular execution:** run the full pipeline or select the components needed for an analysis.
- **Local and cloud inputs:** read slides from local storage, GCS, S3 or Azure; process individual
  slides or resumable local batches.
- **Reviewable outputs:** browse masks, overlays, measurements and execution records through
  per-slide and batch result pages.

A completed run means the requested processing finished; scientific acceptance depends on reviewing
its measurements and using suitable thresholds and normalization references.

## Three ways to run

| Interface | Use it for | Guide |
|---|---|---|
| Python library | Integrating QC into scripts, notebooks and other analysis tools | [Library usage](src/USAGE_LIBRARY.md) |
| Command line | Processing one slide or a resumable batch from a terminal | [CLI usage](src/USAGE_CLI.md) |
| Verily workflows | Running the pipeline in a workflow workspace using the supplied WDLs | [Verily usage](src/USAGE_VERILY.md) |

All three use the same pipeline and component configuration. For an interactive walkthrough, open
[the demo notebook](demo.ipynb) at the repository root and follow its setup cells. Use the installed
Python environment as its kernel; the example needs access to its slide and uses provisional
normalization targets.

## Run the full pipeline

From the repository root, after [setting up dependencies](src/README.md#install):

```bash
python src/run.py --slide "/data/slides/example.svs" --out reports \
  --stain "AT8" --no_metadata
```

Replace the slide path and stain. This uses your configuration defaults; missing model assets
download automatically. See the [CLI guide](src/USAGE_CLI.md) for more options.

## Repository overview

The repository is organized as follows:

| Area | Purpose |
|---|---|
| [src/](src/README.md) | Standalone Python project, usage guides, runtime package and deployment files |
| [demo.ipynb](demo.ipynb) | Standalone interactive pipeline demonstration |
| [LICENSE](LICENSE) | Apache License 2.0 |
| [THIRD_PARTY_NOTICES.md](src/THIRD_PARTY_NOTICES.md) | Terms and attribution for bundled models and example images |

For individual source files, start with the [source inventory](src/README.md#project-files).
For pipeline internals, use the [package guide](src/pathnd_qc/README.md#guides).
Build and release instructions live in the [deployment guide](src/deploy/verily/README.md);
verification commands are in [release checks](src/README.md#release-checks).

## License

Path-ND QC is copyright 2026 The 10,000 Brains Project and licensed under the
[Apache License 2.0](LICENSE). The GrandQC compatibility patches are the exception: as
adaptations of GrandQC, they are licensed under CC BY-NC-SA 4.0 (see [NOTICE](src/NOTICE)).

Pen detection and GrandQC artifact detection use third-party models with their own terms.
GrandQC is distributed under a non-commercial license (CC BY-NC-SA 4.0 / CC BY-NC 4.0), and use is
subject to the terms of the original GrandQC license. The WSISegQC pen model publishes no license.
Example images in the guides and notebook come from the public SEA-AD dataset (image credit:
Allen Institute). See [third-party notices](src/THIRD_PARTY_NOTICES.md) for terms and citations.

## Long-term goal

The long-term goal is a reusable, reproducible QC and preprocessing foundation for large-scale
neuropathology research. Consistent measurements and traceable processing should help researchers
compare slide collections, assess whether images are suitable for downstream analyses, and extend
the pipeline as methods and validation evidence develop.

The component guides describe implemented behavior and its limits.
