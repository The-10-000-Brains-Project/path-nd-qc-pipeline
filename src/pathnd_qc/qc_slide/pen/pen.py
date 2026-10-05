"""M2 pen-mark segmentation with the WSISegQC two-class UNet++ model.

Based on Puttagunta et al., "Semantic Segmentation Based Quality Control of Histopathology
Whole Slide Images" (arXiv:2410.03289; github.com/abhijeetptl5/wsisegqc). Inference uses
smp.UnetPlusPlus(resnet34, classes=2), padding to /32 plus a border, (img/255)-0.5 normalization,
argmax and cropping. Whole-image inference is the default; context tiling is optional. Image dimensions are flexible; predictions are not guaranteed scale-invariant.

The pipeline runs tissue, folds, then pen. A computed pen mask yields to an available fold mask;
supplied masks retain their pixels. Staining and focus exclude the union of the resulting masks.
The default analysis target is 8.0 microns/pixel. Pen detection is included in every full run
and the thumbnail alias, with the same selection and completion rules as other components.

The model needs its inference dependencies and weights resolved by the external-backend manager.
Weights are not included in the Python distribution; see external/README.md for setup and model
terms. pen_area_fraction is measured over the entire image, and is None on inference failure.
Predictions and their scientific suitability require validation on the intended tissue and stains.
"""

import time

import numpy as np
from PIL import Image

from pathnd_qc.config.config import cfg, error_kind, resolve_path

# Read the pen weights path from configuration. Missing weights produce pen_error.
DEFAULT_WEIGHTS = resolve_path(cfg("m2.pen.weights_path", None))

PEN_CLASS = cfg("m2.pen.pen_class", 1)  # 2-class model: 0 = background, 1 = pen
COLOR_PEN = tuple(cfg("m2.pen.color", [0, 200, 0]))  # green — distinct from the fold yellow
ALPHA_BLEND = cfg("m2.pen.alpha_blend", cfg("shared.alpha_blend", 0.45))

_MODEL_CACHE: dict = {}


def build_pen_model(weights_path: str = DEFAULT_WEIGHTS, device: str = "cpu"):
    """smp UNet++/ResNet34, 2 classes, loaded from the released state_dict."""
    try:
        import torch
        import segmentation_models_pytorch as smp
    except ImportError as exc:
        raise ImportError("Pen detection dependencies are missing: reinstall 'pathnd-qc' "
                          "or run 'pathnd-qc setup pen --install-deps'") from exc
    except RuntimeError as exc:
        if "torchvision::nms" not in str(exc):
            raise
        raise RuntimeError("Torch and torchvision builds are incompatible. Install both from "
                           "the same PyTorch index, or run 'pathnd-qc setup pen --install-deps' "
                           "to install a matching CPU pair.") from exc
    model = smp.UnetPlusPlus(
        encoder_name="resnet34", encoder_weights=None, in_channels=3, classes=2,
    )
    state = torch.load(weights_path, map_location=device)
    model.load_state_dict(state)  # strict — verified 0 missing/unexpected
    return model.eval().to(device)


def _get_model(weights_path: str, device: str):
    key = (weights_path, device)
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = build_pen_model(weights_path, device)
    return _MODEL_CACHE[key]


def _pred(image: Image.Image, model, device: str) -> np.ndarray:
    """Verbatim WSISegQC `pred`: pad to /32 + 64px border, center-normalize,
    argmax over classes, crop back to original size. Returns class-id map (uint8)."""
    import torch
    with torch.no_grad():
        w_, h_ = image.size
        wa, ha = w_ % 32, h_ % 32
        canvas = Image.new("RGB", (w_ + (32 - wa) + 128, h_ + (32 - ha) + 128))
        canvas.paste(image, (64, 64, w_ + 64, h_ + 64))
        arr = np.moveaxis(np.array(canvas), -1, 0)
        x = (torch.Tensor(arr) / 255) - 0.5
        x = x.unsqueeze(0).to(device)
        out = torch.argmax(model(x)[0], 0).cpu().numpy()
        return out[64:64 + h_, 64:64 + w_].astype("uint8")


def _pred_tiled(image: Image.Image, model, device: str, tile_px: int, halo_px: int) -> np.ndarray:
    """Predict bounded patches, retaining only each core and discarding its context halo.

    Multiples of 32 keep patch origins aligned to the encoder stride. A plane that
    fits one tile uses one forward pass. Halos reduce boundary effects but tiled
    predictions need not be identical to a whole-plane forward pass.
    """
    for name, value, minimum in (("tile_px", tile_px, 32), ("halo_px", halo_px, 0)):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value % 32:
            raise ValueError(f"{name} must be a multiple of 32 and >= {minimum}")
    width, height = image.size
    if not width or not height:
        raise ValueError("pen image must have positive dimensions")
    classes = np.empty((height, width), dtype=np.uint8)
    for y in range(0, height, tile_px):
        for x in range(0, width, tile_px):
            right, bottom = min(x + tile_px, width), min(y + tile_px, height)
            left, top = max(x - halo_px, 0), max(y - halo_px, 0)
            patch = image.crop((left, top, min(right + halo_px, width), min(bottom + halo_px, height)))
            predicted = _pred(patch, model, device)
            classes[y:bottom, x:right] = predicted[y - top:bottom - top, x - left:right - left]
    return classes


_NO_SUBTRACTION = {"pen_area_fraction_raw": None, "fold_overlap_fraction": None, "fold_subtracted": False}


def detect_pen(
    thumbnail: Image.Image,
    weights_path: str | None = DEFAULT_WEIGHTS,
    device: str = cfg("m2.pen.device", "cpu"),
    fold_mask: np.ndarray | None = None,
    tile_px: int | None = cfg("m2.pen.tile_px", None),
    halo_px: int = cfg("m2.pen.halo_px", 64),
) -> dict:
    """Detect pen marks as a boolean mask matching the thumbnail dimensions.

    tile_px=None uses one whole-image forward pass; a positive tile_px enables tiling.
    When fold_mask is supplied, subtract it from the computed pen mask and record the
    overlap. Without it, retain the prediction and set fold_subtracted=False.

    Returns pen_mask, pen_area_fraction, pen_area_fraction_raw, fold_overlap_fraction,
    fold_subtracted, runtime_s, and pen_error. Failed inference returns null measurements.
    """
    t0 = time.monotonic()
    if not weights_path:
        # Not an exception: the model simply is not configured. Say so, and say how to fix it —
        # a caller who asked for pen detection needs the remedy, not a stack-trace string.
        return {
            "pen_mask": None,
            "pen_area_fraction": None,          # NOT 0.0 — see below
            **_NO_SUBTRACTION,
            "runtime_s": 0.0,
            "pen_error": ("no pen weights configured. Supply them with --pen_weights <path/to.pt>, "
                          "or set m2.pen.weights_path in config/defaults.json. Expected: a torch "
                          "state_dict for the WSISegQC UNet++ pen model."),
        }
    try:
        thumbnail = thumbnail.convert("RGB")
        model = _get_model(weights_path, device)
        class_map = (_pred(thumbnail, model, device) if tile_px is None else
                     _pred_tiled(thumbnail, model, device, tile_px, halo_px))
        pen_mask = class_map == PEN_CLASS
        frac = float(pen_mask.mean())  # fraction of thumbnail area
        result = {
            "pen_mask": pen_mask,
            "pen_area_fraction": frac,
            "pen_area_fraction_raw": frac,
            "fold_overlap_fraction": None,
            "fold_subtracted": False,
            "runtime_s": round(time.monotonic() - t0, 3),
            "pen_error": None,
            "tile_px": tile_px, "halo_px": halo_px,
        }
        if fold_mask is not None:
            subtract_folds(result, fold_mask)
        result["runtime_s"] = round(time.monotonic() - t0, 3)
        return result
    except Exception as e:
        return {
            "pen_mask": None,
            # A failed inference has no measured pen fraction; use None rather than zero.
            "pen_area_fraction": None,
            **_NO_SUBTRACTION,
            "runtime_s": round(time.monotonic() - t0, 3),
            "pen_error": f"{type(e).__name__}: {e}", "pen_error_type": type(e).__name__,
        }


def subtract_folds(result: dict, fold_mask: np.ndarray) -> dict:
    """Subtract folds from a computed pen mask in place and retain raw and overlap fractions.

    Computed folds take precedence over computed pen. Supplied pen masks bypass this
    helper and take precedence over computed folds in the orchestrator.
    """
    pen = result.get("pen_mask")
    if pen is None:
        return result
    fold = np.asarray(fold_mask, dtype=bool)
    if fold.shape != pen.shape:
        result["note"] = (f"fold mask {fold.shape} does not match the pen mask {pen.shape}; "
                          f"nothing subtracted")
        return result
    raw = np.asarray(pen, dtype=bool)
    overlap = raw & fold
    kept = raw & ~fold
    result.update(pen_mask=kept, pen_area_fraction=float(kept.mean()),
                  pen_area_fraction_raw=float(raw.mean()), fold_overlap_fraction=float(overlap.mean()),
                  fold_subtracted=True)
    return result


def generate_pen_overlay(thumbnail: Image.Image, pen_mask: np.ndarray) -> Image.Image:
    """Blend the pen mask (green) onto the thumbnail."""
    overlay = np.array(thumbnail.convert("RGB")).astype(np.float32)
    if pen_mask is not None and pen_mask.any():
        m = pen_mask.astype(bool)
        for c, col in enumerate(COLOR_PEN):
            overlay[m, c] = overlay[m, c] * (1 - ALPHA_BLEND) + col * ALPHA_BLEND
    return Image.fromarray(np.clip(overlay, 0, 255).astype(np.uint8))


def to_report(result: dict) -> dict:
    """JSON-safe subsection for `report.json`.

    `pen_area_fraction` is **None** on the error path, so it can never be mistaken for "no pen
    found". `error` is what a consumer must read: a 0.0 from
    a model that never loaded is indistinguishable from a genuinely pen-free slide.
    """
    return {"pen_area_fraction": result.get("pen_area_fraction"),
            "pen_area_fraction_raw": result.get("pen_area_fraction_raw"),
            "fold_overlap_fraction": result.get("fold_overlap_fraction"),
            "fold_subtracted": result.get("fold_subtracted"),
            "note": result.get("note"),
            "tile_px": result.get("tile_px"), "halo_px": result.get("halo_px"),
            "runtime_s": result.get("runtime_s"),
            "error_type": error_kind(result.get("pen_error"), result.get("pen_error_type")),
            "error": result.get("pen_error")}
