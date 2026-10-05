# Staining Quality (`staining.py`)

[Package overview](../../README.md) · [CLI usage](../../../USAGE_CLI.md) · [Library usage](../../../USAGE_LIBRARY.md) · [Parent module](../README.md)

[What it does](#what-it-does) · [How it works](#how-it-works) · [Usage](#usage) · [Defaults](#defaults) · [Historical observations and limitations](#historical-observations-and-limitations) · [References](#references)

For setup, paths, and output folders, start with the [usage guide](../../../USAGE_CLI.md). Command examples below use the activated environment where the package is installed. Replace `/path/to/slide.svs` with your slide and mask paths with files from its earlier run.

Resolutions shown are the shipped defaults; µm/px means micrometres per pixel. Output artifacts are written inside the slide’s timestamped run folder unless `--no_save_artifacts` is used; the report is still written.

## What it does

Measures how strongly a slide is stained.

**In:** the 8.0 µm/px analysis image and the tissue mask, plus optional fold and pen masks to
subtract.
**Out:** `staining_metrics = {"chroma_mean": value}`, the same value as
`staining_quality_score`, support metadata, and an error field. `chroma_mean` is the only
staining metric; `return_debug=True` also returns the measured `support_mask`.

```
support = tissue_mask & ~fold_mask & ~pen_mask     (each exclusion applied only if supplied)
```

## How it works

**The score is mean colourfulness — `chroma_mean`.** Each tissue pixel is converted to CIE-Lab
(D65 / 2° observer) and its chroma — the distance from grey, `C* = √(a*² + b*²)` — is averaged over
the support. A vivid slide scores high, a washed-out one low. Chroma uses Lab’s colour channels; this does not make the score a calibrated measure of stain quality
or guarantee independence from scanning conditions.

**An empty support returns an error, never a number.** There is no sentinel value that could be
mistaken for a real score.
The metric dictionary remains `{"chroma_mean": None}` on failure.

## Usage

```bash
pathnd-qc --slide "/path/to/slide.svs" --run_tissue_segmentation --run_staining_quality
pathnd-qc --slide "/path/to/slide.svs" --run_staining_quality \
              --tissue_mask out/slide_tissue_mask.png            # reuse an earlier mask
```

The Python example is a contextual snippet: create an RGB PIL image named `plane` (or `thumb`)
and the masks/geometry it references first. Check returned error fields before using results.

```python
from pathnd_qc.qc_slide.staining.staining import compute_staining_metrics

res = compute_staining_metrics(plane, tissue_mask=tissue["tissue_mask"],
                               fold_mask=folds["fold_mask"], stain_type="LFB/H&E")
res["staining_quality_score"]
```

- **Flag:** `--run_staining_quality`. Also runs inside the `--run_thumbnail` M2 alias.
- **Needs a tissue mask.** `fold_mask` and `pen_mask` are optional; their absence is recorded in
  `provenance.degraded`.
- **Writes no file** — the score lands at `m2.staining` in `<slide_id>_report.json`.
- The pipeline supplies the available masks after resolving pen/fold overlap. Supplied masks must
  describe this slide and the same analysis image.

## Defaults

Values live in [`config/defaults.json`](../../config/defaults.json). Prefer a separate JSON override file selected with
`PATHND_CONFIG=/path/to/settings.json`; see the [configuration guide](../../config/README.md).
Where a Python function accepts an argument, an explicit value overrides its configured default.
Configuration keys are not automatically command-line flags.

| key | default | what it does |
|---|---|---|
| `thresholds` → `"m2.staining.staining_quality_score"` | `{"min": null, "max": null}` | the bounds the score is flagged against |

**Bounds are per-stain.** That entry also takes a `by_stain` map, resolved through `m4.stain_aliases`
so there is one stain vocabulary in the codebase rather than two. **These bounds ship `null`, so no staining-score threshold verdict is produced by default.**

`defaults.json` also carries an `_observed_chroma_mean` block — per-stain counts and percentiles from
a 171-slide cohort. **Nothing reads it.** It is there so whoever sets the bounds has numbers to hand,
and it carries its own warning: those percentiles include the cohort's bad slides, and were measured
at varying scale rather than the 8.0 µm/px the pipeline uses.

## Historical observations and limitations

The examples and measurements in this section come from earlier development runs. They have not
been rerun for this documentation update and may use older code or image resolutions. They are not
current benchmarks, accuracy guarantees, or recommended quality thresholds.

**The score tells you the stain type, not the staining quality.** That is measured, not suspected:

- **It is dominated by stain, roughly 11-fold.** Median chroma by stain: HE 40.00 · LFB-HE 37.09 ·
  Hirano 31.68 · GFAP 8.58 · AMYB 5.60 · ASYN 4.63 · AT8 4.28 · I6 3.84 · NEUN 3.61. A single global
  threshold across stains is meaningless, which is why per-stain bounds exist.
- **It does not separate the only staining ground truth available.** Within ASYN, the artefact slides
  score `[8.10, 9.62, 11.93, 15.81]` and the clean ones `[6.52, 8.54, 15.04, 25.15]` — fully
  interleaved, and the **highest-scoring slide is a clean one**.
- **Averaging hides what it should catch.** Over- and under-stained regions cancel. The four slides
  identified in review as visibly heterogeneous sit at the 59th, 71st, 76th and 88th percentile
  *within their own stain* — `Case_5_6_ASyn`, half blue-cast and half brown, lands dead middle.

**The per-stain machinery does work.** A score of 40.0 is flagged under NEUN's band (3–5) and passes
under HE's (30–50). But **no slide has yet been scored by a configured band** — all nine ship `null`,
so `n_thresholds_checked` is 0 with the shipped unset bounds and the failure mode above is unfalsified rather than
disproven.

Verified across 233 checks: neutral grey scores **0.0032**, a vivid patch **68.65**, and an empty
support errors rather than reporting 0.

### Against the specification

**The score is not the measure the specification asks for.** Staining quality is specified as the
**optical-density distribution**, reviewed per stain type by a neuropathologist. What ships is CIE-Lab
chroma.

## References

- **CIE 1976 L\*a\*b\*** and its chroma `C*ab = √(a*² + b*²)` — CIE Publication 15, *Colorimetry*.
- Implementation: `skimage.color.rgb2lab`, D65 / 2° observer —
  [scikit-image.org](https://scikit-image.org/docs/stable/api/skimage.color.html)
