# Focus (`focus.py`)

[Package overview](../../README.md) · [CLI usage](../../../USAGE_CLI.md) · [Library usage](../../../USAGE_LIBRARY.md) · [Parent module](../README.md)

[What it does](#what-it-does) · [How it works](#how-it-works) · [Usage](#usage) · [Defaults](#defaults) · [Historical observations and limitations](#historical-observations-and-limitations) · [References](#references)

For setup, paths, and output folders, start with the [usage guide](../../../USAGE_CLI.md). Command examples below use the activated environment where the package is installed. Replace `/path/to/slide.svs` with your slide and mask paths with files from its earlier run.

Resolutions shown are the shipped defaults; µm/px means micrometres per pixel. Output artifacts are written inside the slide’s timestamped run folder unless `--no_save_artifacts` is used; the report is still written.

## What it does

Reports how sharp a slide looks.

**In:** the 8.0 µm/px analysis image and the tissue mask, plus optional fold and pen masks.
**Out:** a `focus_score` and the fraction of the image it was measured over.

```
support = tissue_mask & ~fold_mask & ~pen_mask     (each exclusion applied only if supplied)
```

## How it works

- **Variance of the Laplacian.** A sharp image has strong fine detail, so its second derivative swings
  widely and its variance is high. A blurred image has little left, and the variance collapses.
- **The background is flattened first.** The tissue-to-glass border is a huge intensity step that the
  Laplacian fires on, which can inflate the sharpness estimate. Everything outside
  the support is set to the support's mean grey before measuring; this does not eliminate every boundary effect.
- **The score is a variance, not a sum.** It is not multiplied by tissue area, but changing the
  selected tissue, its boundaries, or resolution can change the score.
- **An empty support returns `None` and an error, never `0.0`.** Zero is a legitimate score — a
  spatially uniform region can measure zero, without establishing why it lacks texture. Returning
  zero for "no data" would conflate a failed measurement with a measured value.

## Usage

```bash
pathnd-qc --slide "/path/to/slide.svs" --run_tissue_segmentation --run_focus
pathnd-qc --slide "/path/to/slide.svs" --run_focus \
              --tissue_mask out/slide_tissue_mask.png            # reuse an earlier mask
```

The Python example is a contextual snippet: create an RGB PIL image named `plane` (or `thumb`)
and the masks/geometry it references first. Check returned error fields before using results.

```python
from pathnd_qc.qc_slide.focus.focus import compute_focus_score

res = compute_focus_score(plane, tissue_mask=tissue["tissue_mask"],
                          fold_mask=folds["fold_mask"], stain_type="LFB/H&E")
```

- **Flag:** `--run_focus`. Also runs inside the `--run_thumbnail` M2 alias.
- **Needs a tissue mask.** `fold_mask` and `pen_mask` are optional; their absence is recorded in
  `provenance.degraded`.
- **Writes no file** — the score lands at `m2.focus` in `<slide_id>_report.json`.
- **Returns:** `{focus_score, support_fraction, tissue_coverage_score, runtime_s, focus_error}`.
- **No verdict.** The score is emitted, never judged — bounds are checked in
  [`reporting`](../../reporting/README.md).
- The pipeline supplies whichever fold and pen masks are available after its overlap rule.
  A missing optional mask does not stop the measurement, but changes its support.

## Defaults

**No tunables.** `m2.focus` is an empty section in
[`config/defaults.json`](../../config/defaults.json), marking that the stage has nothing to configure.

| key | default | what it does |
|---|---|---|
| `thresholds` → `"m2.focus.focus_score"` | `{"min": null, "max": null}` | bounds the score is flagged against — unset, so nothing is flagged |

## Historical observations and limitations

The examples and measurements in this section come from earlier development runs. They have not
been rerun for this documentation update and may use older code or image resolutions. They are not
current benchmarks, accuracy guarantees, or recommended quality thresholds.

**It measures image content, not focus.** That is the headline, and it is measured rather than
suspected:

- **On 18 slides independently labelled out-of-focus, the score spans 160 to 1752 — about 11× — with
  no pattern.** The highest is a cerebellum section, naturally textured; the lowest are sparse pale
  ones.
- **Scores are not comparable across slides.** The palest slide tested scores **highest** (4055) and
  the dark LFB slide **lowest** (2995) — the ranking tracks stain and content.
- **It can say blurred or not blurred, but cannot grade severity.** A 100-step synthetic blur sweep is
  flat to σ = 0.30, spends ~90 % of its range between σ 0.3 and 1.0, then flattens past σ ≈ 1.5. The
  usable band is **roughly 0.7 px wide**.
- **Real defocus is regional; this is a global average**, so a small blurred patch barely moves it.
  Per-tile focus is [`tile_metrics`](../../qc_tile/tile_metrics/README.md), which does separate
  global from regional defocus.

**It inherits the tissue mask's failures.** Slide `45226` returned 0.9890 coverage — the known Hirano
over-segmentation — so its score was computed partly over background: **3117.79 on the flooded mask
against 4817.23 on a correct one, +54.5 %**. That also inverts the ranking against the other two
slides. Changing the mask moved the score about as much as 0.45 px of blur, which sits inside the
metric's only sensitive band.

**Folds are excluded because they lower the score, not raise it.** At this scale a fold is a broad,
smooth, dark smear with little fine texture, so it carries below-average variance. Removing folds
moved `42669` **2905.22 → 2994.89**, upward.

| slide | stain | tissue coverage | fold % | focus | Δ from excluding folds |
|---|---|---|---|---|---|
| 42669 | LFB/H&E — dark, real folds | 0.6362 | 3.222 % | 2994.89 | **+3.09 %** |
| 42025 | AT8 — pale, fold-free | 0.5052 | 0.019 % | 4055.15 | +0.05 % |
| 45226 | Hirano — dark, vivid | 0.9890 | 0.000 % | 3117.79 | 0.00 % |

## References

- Pech-Pacheco, Cristóbal, Chamorro-Martínez & Fernández-Valdivia, "Diatom autofocusing in
  brightfield microscopy: a comparative study," *ICPR* 2000 — variance of the Laplacian.
