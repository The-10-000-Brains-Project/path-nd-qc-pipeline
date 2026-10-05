"""Construct tile geometry on the M3 analysis plane without reading pixels.

The plane uses a fixed MPP, normally 0.50 microns per pixel, so equal-sized tiles
cover the same physical area across slides. M1 materializes each window on demand.

plane_dims is (width, height). Tile x/y/w/h values are plane pixels; the reader
converts them to slide coordinates. tissue_mask is a whole-slide 2-D boolean array
in (height, width) order. Its shape determines the per-axis mapping to the tile plane.
Selection retains grid cells with any tissue overlap, including clipped edge cells.
"""
from __future__ import annotations

import math

import numpy as np

from pathnd_qc.config.config import cfg

TILE_PX = cfg("m3.tiles.tile_px", cfg("shared.tile_px", 512))  # tile size in PLANE pixels (512 @ 0.50 um/px = 256 um)


def tile_grid(plane_dims: tuple[int, int], tile_px: int = TILE_PX) -> list[dict]:
    """Non-overlapping `tile_px` grid over the analysis plane; edge tiles are clipped to < tile_px.

    `plane_dims` = (width, height) in PLANE pixels. Returns one dict per cell, row-major:
      {col, row, x, y, w, h}. Pure geometry; no I/O.
    """
    w0, h0 = int(plane_dims[0]), int(plane_dims[1])
    if w0 <= 0 or h0 <= 0:
        raise ValueError(f"plane_dims must be positive (w, h); got {plane_dims!r}")
    if tile_px <= 0:
        raise ValueError(f"tile_px must be positive; got {tile_px}")
    ncols, nrows = math.ceil(w0 / tile_px), math.ceil(h0 / tile_px)
    tiles: list[dict] = []
    for row in range(nrows):
        y = row * tile_px
        h = min(tile_px, h0 - y)
        for col in range(ncols):
            x = col * tile_px
            w = min(tile_px, w0 - x)
            tiles.append({"col": col, "row": row, "x": x, "y": y, "w": w, "h": h})
    return tiles


def _tile_to_mask_box(x: int, y: int, w: int, h: int, sx: float, sy: float,
                      mask_w: int, mask_h: int) -> tuple[int, int, int, int]:
    """Map a plane-space footprint (x, y, w, h) onto a mask index box.

    `sx, sy` = mask/plane scale per axis. Returns (mx0, mx1, my0, my1), clamped to the mask and
    guaranteed non-empty (>= 1 px each axis) so a tile never silently maps to nothing.
    """
    mx0 = min(max(int(math.floor(x * sx)), 0), mask_w - 1)
    my0 = min(max(int(math.floor(y * sy)), 0), mask_h - 1)
    mx1 = min(max(int(math.ceil((x + w) * sx)), mx0 + 1), mask_w)
    my1 = min(max(int(math.ceil((y + h) * sy)), my0 + 1), mask_h)
    return mx0, mx1, my0, my1


def plane_window(plane: np.ndarray, x: int, y: int, w: int, h: int,
                 plane_dims: tuple[int, int]) -> np.ndarray:
    """The slice of `plane` covering the footprint (x, y, w, h). A VIEW, not a copy.

    `plane` is any 2-D array indexed (row, col) spanning the whole slide — a tissue mask, an artifact
    class map, anything. Scale is derived from `plane.shape` against `plane_dims`, so the caller never
    states it and a coarser plane (e.g. the 2.0 um/px artifact map) needs no special handling.
    """
    plane = np.asarray(plane)
    if plane.ndim != 2:
        raise ValueError(f"plane must be 2-D (h, w); got shape {plane.shape}")
    ph, pw = plane.shape
    w0, h0 = int(plane_dims[0]), int(plane_dims[1])
    if w0 <= 0 or h0 <= 0:
        raise ValueError(f"plane_dims must be positive (w, h); got {plane_dims!r}")
    mx0, mx1, my0, my1 = _tile_to_mask_box(x, y, w, h, pw / w0, ph / h0, pw, ph)
    return plane[my0:my1, mx0:mx1]


def window_origin(start: int, size: int, extent: int) -> int:
    """Clamp a fixed-size window origin to keep it within the extent.

    Shifting the final window inward can overlap its neighbor and avoids synthetic
    padding. If extent is smaller than size, return zero; the caller must handle
    the resulting short window.
    """
    return max(0, min(int(start), int(extent) - int(size)))


def select_tissue_tiles(tissue_mask: np.ndarray, plane_dims: tuple[int, int],
                        tile_px: int = TILE_PX) -> list[dict]:
    """Select grid tiles whose footprints overlap a whole-slide tissue mask.

    The mask is a 2-D boolean array; plane_dims gives the tile plane’s width and height.
    Returns row-major records containing col, row, x, y, w, h, and tissue_overlap.
    Edge widths and heights are clipped to the plane. Any positive overlap selects a
    tile; compute_tile_metrics applies the tissue-fraction drop policy.
    """
    mask = np.asarray(tissue_mask, dtype=bool)
    if mask.ndim != 2:
        raise ValueError(f"tissue_mask must be 2-D (h, w); got shape {mask.shape}")
    mask_h, mask_w = mask.shape
    w0, h0 = int(plane_dims[0]), int(plane_dims[1])
    if w0 <= 0 or h0 <= 0:
        raise ValueError(f"plane_dims must be positive (w, h); got {plane_dims!r}")
    sx, sy = mask_w / w0, mask_h / h0

    selected: list[dict] = []
    for t in tile_grid((w0, h0), tile_px):
        mx0, mx1, my0, my1 = _tile_to_mask_box(t["x"], t["y"], t["w"], t["h"], sx, sy, mask_w, mask_h)
        overlap = float(mask[my0:my1, mx0:mx1].mean())
        if overlap > 0.0:
            selected.append({**t, "tissue_overlap": round(overlap, 4)})
    return selected


if __name__ == "__main__":
    # Check tile geometry using synthetic masks without slide I/O.
    W0, H0 = 2000, 1000
    grid = tile_grid((W0, H0), tile_px=512)
    assert len(grid) == math.ceil(W0 / 512) * math.ceil(H0 / 512) == 8
    assert any(t["w"] < 512 for t in grid) and any(t["h"] < 512 for t in grid)  # edge tiles clipped
    m = np.zeros((50, 100), dtype=bool)
    m[:25, :50] = True                                     # tissue in the top-left quadrant
    sel = select_tissue_tiles(m, (W0, H0), tile_px=512)
    empty = select_tissue_tiles(np.zeros((50, 100), bool), (W0, H0))
    assert all({"w", "h"} <= set(t) for t in sel), "w/h must be emitted"
    assert empty == []
    # plane_window derives its own scale: a 2x coarser plane halves the window
    pw = plane_window(np.ones((50, 100), bool), 0, 0, 512, 512, (W0, H0))
    print(f"grid={len(grid)} tiles; tissue-selected={len(sel)}; empty-mask-selected={len(empty)}")
    print("selected:", [(t["col"], t["row"], t["w"], t["h"], t["tissue_overlap"]) for t in sel])
    print("plane_window shape:", pw.shape)
    print("OK — geometry invariants hold")
