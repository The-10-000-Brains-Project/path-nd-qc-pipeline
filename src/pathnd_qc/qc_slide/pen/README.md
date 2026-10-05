# Pen Detection (`pen.py`)

[Package overview](../../README.md) · [CLI usage](../../../USAGE_CLI.md) · [Library usage](../../../USAGE_LIBRARY.md) · [Parent module](../README.md)

[What it does](#what-it-does) · [How it works](#how-it-works) · [Usage](#usage) · [Defaults](#defaults) · [Historical observations and limitations](#historical-observations-and-limitations) · [References](#references)

For setup, paths, and output folders, start with the [usage guide](../../../USAGE_CLI.md). Command examples below use the activated environment where the package is installed. Replace `/path/to/slide.svs` with your slide and mask paths with files from its earlier run.

Resolutions shown are the shipped defaults; µm/px means micrometres per pixel. Output artifacts are written inside the slide’s timestamped run folder unless `--no_save_artifacts` is used; the report is still written.

## What it does

Finds pen and ink annotation marks on the slide, so they can be excluded from everything measured
afterwards.

**In:** the 8.0 µm/px analysis image, plus the pen model's weights.
**Out:** a boolean pen mask and `pen_area_fraction`, measured over the entire analysis image
(not just tissue support).

The mask matters mostly because other components consume it: [`staining`](../staining/README.md)
and [`focus`](../focus/README.md) exclude it from their support. Fold detection runs on tissue,
and `detect_pen` takes the fold mask as an **optional** input — where the two overlap the pixel is fold, so `pen_mask = raw & ~fold`, with
`pen_area_fraction_raw` and `fold_overlap_fraction` kept beside the reported fraction and
`fold_subtracted` saying whether it happened. The rule for every combination: a **supplied** mask is
treated as authoritative and is never altered (a supplied pen makes the computed fold yield instead); both
computed → fold wins; no fold mask at all (folds not run, or failed) → the pen mask stays as it is
and `fold_subtracted` is false.

## How it works

A neural segmentation model (WSISegQC) runs one forward pass over the analysis image:

1. Load `UnetPlusPlus` with a `resnet34` encoder and two output classes from `pen.pt`.
2. Pad the image to the model's multiple-of-32 canvas plus its 64 px border.
3. Scale pixels as `(img / 255) − 0.5` — no ImageNet normalization.
4. Take the `argmax` and crop back to the image dimensions. Class 1 is pen.

Whole-image inference avoids repeated context computation; activation memory grows with image
size. For a memory-constrained machine, set `m2.pen.tile_px` to `1024` or `2048` in a configuration
override. Tiled inference retains each core and discards its context halo. Tiled predictions can
differ from whole-image predictions and need validation on the intended slides. `null` disables
tiling. Changing resolution can also change predictions.

## Usage

Pipeline runs automatically install missing default pen assets. Use `--no_model_download`
to require an existing checkpoint. Direct component calls need a usable weights path.

```bash
pathnd-qc --slide "/path/to/slide.svs" --run_pen_detection                     # runs on its own
```

The Python example is a contextual snippet: create an RGB PIL image named `plane` (or `thumb`)
and the masks/geometry it references first. Check returned error fields before using results.

```python
from pathnd_qc.qc_slide.pen.pen import detect_pen, generate_pen_overlay

res     = detect_pen(plane, weights_path="/path/to/pen.pt")
overlay = generate_pen_overlay(plane, res["pen_mask"])
```

```bash
pathnd-qc --slide "/path/to/slide.svs" --run_pen_detection --pen_weights /path/to/pen.pt
```

- **Flag:** `--run_pen_detection`. Also runs inside the `--run_thumbnail` M2 alias.
- **Needs:** the analysis image, installed model dependencies, and readable compatible weights.
  It can run without a tissue mask; folds are optional.
- **Writes:** `images/<slide_id>_pen_mask.png`. Results land at `m2.pen` in `<slide_id>_report.json`.
- **Returns:** `{pen_mask, pen_area_fraction, runtime_s, pen_error}`.

**If the model did not run, `pen_area_fraction` is `None`, never `0.0`** — a zero would read as "no
pen found" on a slide where nothing happened. Check `pen_error`. Missing weights is treated as a
configuration state, not a crash: you get a message naming both ways to fix it.

## Defaults

Values live in [`config/defaults.json`](../../config/defaults.json). Prefer a separate JSON override file selected with
`PATHND_CONFIG=/path/to/settings.json`; see the [configuration guide](../../config/README.md).
Where a Python function accepts an argument, an explicit value overrides its configured default.
Configuration keys are not automatically command-line flags.

| key | default | what it does |
|---|---|---|
| `m2.pen.weights_path` | `external/weights/pen.pt` | the model file; `--pen_weights` overrides it |
| `m2.pen.tile_px` | `null` | whole image; optional core size is a positive multiple of 32 |
| `m2.pen.halo_px` | `64` | context size for tiled inference only; non-negative multiple of 32 |
| `m2.pen.device` | `"cpu"` | torch device |
| `m2.pen.pen_class` | `1` | which output class means pen |
| `m2.pen.color` | `[0, 200, 0]` | overlay colour |
| `shared.alpha_blend` | `0.45` | overlay opacity |

The table shows the literal shipped fallback; registered model paths override it.
See [model locations](../../external/README.md#model-locations) for relative paths and managed downloads.

**Selection.** Pen follows the same rules as every component: it is enabled by default and can be
excluded with `components.pen_detection=false`, even from `--run_thumbnail` or an explicit flag.
Replace the retired `m2.pen.enabled` with this shared component switch.

## Historical observations and limitations

The examples and measurements in this section come from earlier development runs. They have not
been rerun for this documentation update and may use older code or image resolutions. They are not
current benchmarks, accuracy guarantees, or recommended quality thresholds.

**The earlier examples did not establish sensitivity to real pen marks.** They documented false
positives on folds and image borders. Treat the mask as an unvalidated model prediction.

| slide | stain | pen fraction | what it actually found |
|---|---|---|---|
| PART `42669` | LFB/H&E, dark, 4 real folds | **2.762 %** | ⚠️ false positive — the mask sits on folded tissue |
| PART `42053` | LFB/H&E, light | ~0.17 % | no blue-stain flooding |
| SEA-AD ×4 | GFAP / NEUN / LFB | ~0.25–1.3 % | all border artifact — a padding frame, no ink |

**The false positives used to be expensive because folds depended on this mask.** `folds.py`
subtracted pen before thresholding, so a pen mask that was really fold deleted the folds the
pipeline should be finding: **0.0748 % with pen exclusion against 3.1304 % without**, on the same
slide. That is why the precedence was reversed on 2026-09-09: folds are found first, on the tissue
alone, and the computed pen mask yields to them.

**Two artifacts of the inference path.** A thin padding-boundary frame inflates the fraction on clean
slides — a clean slide can read ~1.3 % from the frame alone. And the model floods at low resolution:
a slide scoring ~25 % at 512 px read ~0.25 % at native scale, so low-resolution pen screening is not
valid.

**In an earlier timing run it was the most expensive M2 component.** 73.3 s of an 89.5 s per-slide median — 82 % of the module's
total, in a module that is otherwise 2.1–2.9 % of a full run. Current runtime depends on image size, hardware, and whether model inference succeeds.

**Three candidate positives exist and have not been run**: QSBB `Case_2_1_ABeta` and `Case_2_6_AT8`
carry hand-drawn ink circling the artefact, and `Case_1_18_HE` shows an apparent blue pen loop.

## References

- Patil et al., "Semantic Segmentation Based Quality Control of Histopathology Whole Slide
  Images," arXiv 2410.03289 — [github.com/abhijeetptl5/wsisegqc](https://github.com/abhijeetptl5/wsisegqc).
  The repo's "Pen Marker Segmentation" link is the pen **dataset**, not the model; the weights are in
  a Drive folder linked from its README.
- License: WSISegQC publishes no license; all rights remain with its authors. See
  [third-party notices](../../../THIRD_PARTY_NOTICES.md).
