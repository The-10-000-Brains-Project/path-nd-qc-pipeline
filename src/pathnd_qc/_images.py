"""Shared RGB input boundary for standalone components and supplied thumbnails."""
from __future__ import annotations

import numpy as np
from PIL import Image


def as_rgb_array(image) -> np.ndarray:
    """Return RGB uint8 pixels; refuse array encodings whose intensity scale is ambiguous.

    PIL images use Pillow's explicit RGB conversion. NumPy inputs must be uint8;
    callers with normalized floats or high-bit-depth data must convert deliberately.
    Grayscale arrays are replicated and RGBA arrays retain their RGB channels.
    """
    if isinstance(image, Image.Image):
        arr = np.asarray(image.convert("RGB"))
    else:
        arr = np.asarray(image)
        if arr.dtype != np.uint8:
            raise ValueError(f"image array must have dtype uint8 in [0, 255]; got {arr.dtype}. "
                             "Convert the intensity scale explicitly before supplying the image.")
        if arr.ndim == 2:
            arr = np.repeat(arr[:, :, None], 3, axis=2)
        elif arr.ndim == 3 and arr.shape[2] == 4:
            arr = arr[:, :, :3]
    if arr.ndim != 3 or arr.shape[2] != 3 or min(arr.shape[:2]) <= 0:
        raise ValueError(f"expected a nonempty PIL image or RGB array; got shape {arr.shape}")
    return arr
