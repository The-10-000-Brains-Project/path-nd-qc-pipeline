# Fold detection: ConnSoftT + F_line

[Package overview](../../README.md) · [CLI usage](../../../USAGE_CLI.md) · [Library usage](../../../USAGE_LIBRARY.md) · [Parent module](../README.md)

[What it does](#what-it-does) · [How it works](#how-it-works) · [Usage](#usage) · [Defaults](#defaults) · [Outputs and failures](#outputs-and-failures) · [Historical observations and limitations](#historical-observations-and-limitations) · [References](#references)

For installation and output navigation, start with the [usage guide](../../../USAGE_CLI.md).
This component runs on the M2 analysis image, targeting **8.0 µm/pixel** by default. It needs no model
weights. The September 2026 update combines a tissue-fold body detector with a line-fold detector.

## What it does

**Inputs:** an RGB PIL image, tissue mask, achieved `thumb_mpp`, and actual `stain_type`. The standalone
function can compute tissue if omitted; a modular CLI run must select tissue segmentation or supply
its mask. **Output:** a boolean fold mask and its area as a fraction of tissue, with per-branch diagnostics.
Computed pen, staining and focus consume the combined fold mask through the existing pipeline.

The pipeline keeps its mask precedence: folds run on tissue, then computed pen yields to folds.
A supplied pen mask is authoritative and is subtracted afterwards from computed folds and both branch
masks. Their fractions use the same original tissue denominator. For a direct Python call only,
`pen_mask=...` optionally excludes ink before thresholding; its fractions use that reduced support.
Supplied fold masks retain their pixels.

## How it works

1. **Route ConnSoftT by stain.** `Hirano`, `LFB`, `LFB/H&E`, `LFB-HE`, and `LFB/HE` use saturation minus
   intensity (`d = s − i`). The configured `m2.folds.d_path_stains` list is combined with tissue's Hirano
   spellings, using stripped lowercase matching. Other stains, including plain H&E and IHC, use the
   per-slide Macenko stain-2 concentration. Missing stain also uses stain-2.
2. **ConnSoftT body mask.** Count connected components across thresholds; choose hard/soft thresholds;
   apply neighbourhood hysteresis; then opening, area, round-speck and boundary-contrast filters.
3. **F_line mask, enabled on every stain by default.** On optical-density magnitude, multiply multiscale
   Frangi ridge response, structure-tensor orientation coherence and positive local robust-z darkness:
   `F_line = R × C × max(Z_R, 0)`. The local median/MAD use tissue-masked, quantized rank filters. MAD is
   approximate: deviations use each pixel's local median, not the centre window's nested median.
4. **Threshold F_line.** Stretch tissue values from p1 to p99 to indices 0–255, round with `numpy.rint`,
   retain indices ≥ 200, then retain 8-connected blobs meeting the area floor. No ConnSoftT post-filters
   are applied to this branch. `fline_t_raw` is the nominal value at index 200; rounding means the actual
   transition is at 199.5/255 of the p1–p99 span (the tie rounds to 200).
5. **Combine:** `fold_mask = connsoftt_mask | fline_mask`. The two fractions can overlap, so their sum
   is not the combined fraction. Reports name successful methods `d+fline` or `stain2+fline`.

Frangi and structure-tensor scales are converted from micrometres to pixels using achieved MPP;
local-ball radii and area floors are rounded to integer pixels. A missing MPP assumes 8.0. Explicit
nonpositive/nonfinite MPP and mismatched/nonfinite masks are errors. On constant fields, orientation
coherence is zero. F_line requires at least ten support pixels.

## Usage

```bash
pathnd-qc --slide /path/to/slide.svs --out reports --stain LFB \
  --run_tissue_segmentation --run_fold_detection

pathnd-qc --slide /path/to/slide.svs --out reports --stain AT8 \
  --run_fold_detection --tissue_mask /path/to/earlier/images/slide_tissue_mask.png
```

With an RGB PIL image `thumb`, its tissue mask `tissue`, and achieved scale `mpp`:

```python
from pathnd_qc.qc_slide.folds import detect_folds, generate_fold_overlay

result = detect_folds(thumb, tissue_mask=tissue, thumb_mpp=mpp, stain_type="LFB/H&E")
if result["fold_error"] or result["fline_error"]:
    print(result["fold_error"] or result["fline_error"])
if result["fold_mask"] is not None:
    overlay = generate_fold_overlay(thumb, result["fold_mask"])
```

The prior positional arguments retain their meaning: third is `thumb_mpp`, fourth is `stain_type`.
New `pen_mask`, `fline`, `fline_hard_index` and `fline_min_area_um2` parameters are keyword-only.
`fline=False` disables the added branch; it does not change stain routing. `return_debug=True` adds
ConnSoftT/F_line masks and feature maps, tissue support, connectivity curve and blob statistics.
`fline_feature` and `fline_threshold` are also available from the folds package for diagnostic use.

## Defaults

Use [`config/defaults.json`](../../config/defaults.json) as the source of defaults. Supply overrides
through `PATHND_CONFIG` before starting the single-slide or batch process; workflow/Compose requests
use `config_file`. See [configuration](../../config/README.md). For example, save this partial
override as `folds.json`; omitted settings keep their defaults:

```json
{
  "m2": {
    "folds": {
      "fline_enabled": true,
      "fline_hard_index": 200,
      "fline_min_area_um2": 44800.0
    }
  }
}
```

Use `PATHND_CONFIG=/absolute/path/to/folds.json` for the CLI, or pass the file as the workflow's
`config_file` input. A full workflow that selects normalization also needs reference settings
in that same configuration file.

| Key under `m2.folds` | Default | Meaning |
|---|---|---|
| `d_path_stains` | `hirano`, `lfb`, `lfb/h&e`, `lfb-he`, `lfb/he` | ConnSoftT d-route spellings, plus tissue's Hirano set |
| `fline_enabled` | `true` | Union the line-fold mask with ConnSoftT |
| `fline_sigmas_um` | `[35, 70, 105]` | Frangi scales in µm |
| `fline_ball_um` | `520` | Local median/MAD disk radius in µm |
| `fline_st_sigma_um` | `70` | Structure-tensor integration scale in µm |
| `fline_eps_frac` | `0.01` | Stabilizer fraction of global tissue MAD |
| `fline_hard_index` | `200` | Rounded display index threshold, integer 0–255 |
| `fline_min_area_um2` | `44800` | Line-fold area floor; null disables it; 700 px at 8 µm/px |
| `alpha`, `d_beta`, `stain2_beta` | `0.64`, `0.34`, `0.25` | ConnSoftT thresholds relative to connectivity peak |
| `opening_radius` | `3` px | ConnSoftT opening radius; 24 µm at default scale |
| `min_area_um2` | `3276.8` | ConnSoftT area filter |
| `speck_max_area_um2`, `speck_min_circularity` | `13144`, `0.5` | Small-round-speck filter |
| `min_boundary_contrast_d`, `min_boundary_contrast_stain2` | `0.4`, `0.1` | ConnSoftT edge contrast gates |
| `contrast_band_px` | `3` | Edge contrast band radius |
| `macenko_beta`, `macenko_alpha` | `0.15`, `1.0` | OD floor and angular percentile |
| `stain2_sweep_pctl`, `stain2_sweep_levels` | `[0.5, 99.5]`, `41` | Stain-2 threshold sweep |
| `fallback_min_area_px`, `fallback_speck_max_area_px` | `51`, `205` | ConnSoftT areas when scale is omitted |
| `color` | `[255, 215, 0]` | Yellow overlay |

Overlay opacity inherits `shared.alpha_blend` (0.45); optional `m2.folds.alpha_blend` overrides it.
It is intentionally absent from defaults so a shared override still takes effect. The new F_line
settings are validated: positive scales/stabilizer, nonnegative area (or null), boolean enable flag,
nonempty scale list and integer index 0–255. Changing M2 scale changes the physical size of the older
pixel-defined ConnSoftT filters; the new physical-scale conversion does not recalibrate those filters.

## Outputs and failures

The usual `images/<slide_id>_fold_mask.png` now contains the union. No extra image files are required.
`m2.folds` adds `connsoftt_area_fraction`, `fline_enabled`, `fline_area_fraction`, `fline_n_blobs`,
`fline_t_raw`, `fline_min_area_px`, `fline_params`, `fline_error`, `fline_error_type` and `fline_runtime_s`.
The report also retains pen-subtraction fields, thresholds, stain vector, method, timing and errors.

A whole-detector failure returns a null mask and null fractions. An F_line-only failure retains the
ConnSoftT mask/fraction, sets `fline_error`, and leaves its uncomputed F_line metrics null. The report
marks fold detection failed/partial and `provenance.execution.complete=false`; downstream components
can still use the retained mask. The pipeline raises `RunFailed`, records failed status, and
the CLI and workflows exit nonzero. A disabled
F_line branch has `fline_enabled=false`, null branch measurements, and no error. Successful zero area
means the branch ran and found no folds.

These are the user's visually selected settings from a 13-slide corpus, not newly fitted thresholds.
Synthetic smoke/integration tests check code behavior and plumbing; they do not establish sensitivity,
specificity or clinical calibration. Re-run slides to compare algorithms: the source/config fingerprints
and `method` identify which detector produced a measurement. Older saved fold masks are not regenerated
when supplied explicitly to a modular run.

## Historical observations and limitations

The examples and measurements in this section come from earlier development runs. They have not
been rerun for this documentation update and may use older code or image resolutions. They are not
current benchmarks, accuracy guarantees, or recommended quality thresholds.

**The historical examples include both detected and missed folds; they do not establish accuracy.** Thresholds were tuned on
about 12 slides by eye. Sensitivity is unproven, and heavily stained fold *bodies* are
under-covered.

**`42669`** (LFB/H&E, four confirmed folds) — the stain-2 path marked all four folded wedges along
the right-hand edges: 113,268 px, **3.22 % of tissue**.

**`42030`** (AT8, pale) — the case that needs stain-2. The fold is a thin, low-contrast band along
the left edge; the `d` feature returns **nothing at all** on this slide, while stain-2 marked
7,221 px, **0.44 % of tissue**.

**`42702`** (LFB/H&E) — ⚠️ **a false positive, and a miss.** Same path, same defaults marked
1,533 px — **none of it on a real fold**.

This slide is in the fold ground truth, so the result can be scored — and it scores zero both ways.
The **1,533 marked pixels and the 687 annotated fold pixels do not share a single pixel**: everything
detected is a false positive, and the real fold is missed entirely. The two regions sit 15 px apart
with different centroids, so this is a genuine disagreement rather than a misaligned mask. What was
marked is a cluster of blobs on uniform interior tissue, with none of the step edge a fold produces.

**Two features are needed because neither finds everything.** `d = s − i` cannot see pale folds — on
`42030` the same slide returns **0.00 % on the `d` path and 0.44 % on stain-2**, because that fold's
`d` value sits at the tissue median. Stain-2 reaches those, and on a 10-stain transfer set was
markedly more specific. Conversely `d` fills the body of dark Hirano folds that stain-2 catches only
at the crease.

**Shape alone does not work.** Four geometric measures — solidity, perimeter²/area, closing-porosity
and one more — failed to separate real folds from threshold artifacts. The boundary-contrast step
test separated them cleanly, which is why the gate is photometric.

**The pen mask changes the answer by 42×.** On `42669`, folds computed with pen exclusion give
**0.0748 %** against **3.1304 %** without. The pen model false-positives on exactly the dark folded
tissue this component is looking for, so exclusion removed those folds in that earlier experiment.
The current default computes folds before pen and does not exclude computed pen from folds. Only
a supplied pen mask is subtracted after fold detection; inspect input provenance for that case.

**On Hirano, some folds are unreachable.** The tissue mask that Hirano slides route to leaves the
dark unsaturated rim uncovered, and **66 ground truth fold pixels sit outside the mask** on those
slides. Nothing running inside the mask can recover them.

## References

- Kothari, Phan & Wang, "Eliminating tissue-fold artifacts in histopathological whole-slide images…,"
  *J Pathol Inform* (PMC3779385) — the connectivity-thresholding method and its post-filters.
- Macenko et al., "A method for normalizing histology slides for quantitative analysis," *ISBI* 2009 —
  the stain-vector estimation behind the stain-2 feature.
