"""Load supplied artifacts and save generated masks, images and tile records.

Supplyable inputs are the thumbnail, tissue/fold/pen masks and tile list. Mask resizing uses
nearest-neighbour interpolation when aspect ratios agree within ASPECT_TOL (1%). Matching shape
cannot establish slide identity: callers must supply an artifact belonging to the intended slide.
Resize provenance records the direction and scale.

The fine tissue mask uses a packed in-memory plane and a tiled 1-bit LZW TIFF with four total
pyramid levels (the base plus three reduced levels). Memory is bounded for the packed plane, not for total process usage. File size
depends on mask content; MPP is recorded in TIFF resolution tags. Only successful outputs should
be reused. See USAGE.md and qc_tile/tile_metrics/README.md for mask coverage and failure semantics.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from pathnd_qc._fs import sha256_file
from pathnd_qc._logging import get_logger
from pathnd_qc._images import as_rgb_array
from pathnd_qc.config.config import cfg
logger = get_logger(__name__)

ASPECT_TOL = 0.01          # 1 % shape tolerance; does not prove slide identity
TIFF_TILE = 512
PYRAMID_LEVELS = 4


# --------------------------------------------------------------------------- loading

_hash_file = sha256_file        # Shared streamed file digest.


def load_mask(path, expected_hw: Optional[tuple] = None, *, allow_empty: bool = False) -> dict:
    """Load a `.png` or `.npy` mask as bool, resized to `expected_hw` when the aspect matches.

    Returns {mask, provenance, error} — never raises, so a bad input is a reported failure rather
    than a crash mid-run.
    `allow_empty=True` accepts a non-zero-sized mask with no detections (pen/folds); tissue
    support uses the default False because it must contain pixels to analyse.
    """
    out = {"mask": None, "provenance": None, "error": None}
    try:
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix == ".npy":
            arr = np.load(path)
        elif suffix == ".png":
            # Accept PNG/NPY analysis masks. Fine-resolution TIFF pyramids are not supplyable masks
            # and must not be decoded as whole analysis planes.
            arr = np.asarray(Image.open(path))
        else:
            raise ValueError(f"unsupported mask format {suffix!r}; expected .png or .npy")

        # Accept (H, W), (H, W, 1), and RGB/RGBA arrays; any nonzero channel denotes tissue.
        # Require both spatial dimensions to exceed one and reject channel-first layouts.
        if arr.ndim == 3 and arr.shape[2] in (1, 3, 4) and min(arr.shape[:2]) > 1:
            arr = arr[..., :3].any(axis=2)
        elif arr.ndim != 2:
            raise ValueError(f"mask must be (H, W), (H, W, 1) or (H, W, 3|4) with H, W > 1; "
                             f"got shape {arr.shape} (channel-first arrays are not accepted)")
        mask = np.asarray(arr).astype(bool)     # PNG convention: > 0 is True
        if mask.ndim != 2:
            raise ValueError(f"mask must be 2-D; got shape {mask.shape}")
        if min(mask.shape) <= 0:
            raise ValueError(f"mask has a zero-sized side: {mask.shape}")
        if not allow_empty and not mask.any():
            raise ValueError("supplied mask is empty (no True pixels)")

        prov = {"source": "supplied", "path": str(path), "sha256": _hash_file(path),
                "original_dims": [int(mask.shape[0]), int(mask.shape[1])],
                "resized": False, "direction": None, "scale": None, "method": None}

        if expected_hw is not None and tuple(mask.shape) != tuple(expected_hw):
            eh, ew = expected_hw
            mh, mw = mask.shape
            if min(eh, ew, mh, mw) <= 0:
                # Refuse invalid expected dimensions with a descriptive error.
                raise ValueError(f"cannot compare a {mw}x{mh} mask with a {ew}x{eh} target: "
                                 f"a side is zero")
            a_supplied, a_expected = mw / mh, ew / eh
            # Allow the aspect-ratio tolerance boundary despite floating-point roundoff.
            if abs(a_supplied - a_expected) / a_expected > ASPECT_TOL * (1 + 1e-9):
                raise ValueError(
                    f"aspect mismatch: supplied mask is {mw}x{mh} (aspect {a_supplied:.4f}) but the "
                    f"target is {ew}x{eh} (aspect {a_expected:.4f}). That is a different slide or a "
                    f"crop, not a scale difference — refusing rather than fabricating a fit.")
            scale = ew / mw
            mask = np.asarray(Image.fromarray(mask).resize((ew, eh), Image.NEAREST), dtype=bool)
            prov.update(resized=True, method="nearest", scale=round(float(scale), 6),
                        direction="up" if scale > 1 else "down",
                        note=(f"supplied mask was {mw}x{mh}, not the expected {ew}x{eh}; "
                              f"nearest-resized {'UP' if scale > 1 else 'DOWN'} by "
                              f"{scale:.4f}x"))
        prov["dims"] = [int(mask.shape[0]), int(mask.shape[1])]
        out["mask"], out["provenance"] = mask, prov
    except Exception as exc:                     # noqa: BLE001 - degrade, never raise
        out["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("load_mask(%s) failed: %s", path, out["error"])
    return out


def load_image(path, *, max_pixels: int | None = None) -> dict:
    """Load a supplied thumbnail as RGB after checking its dimensions before materialization.

    `max_pixels` defaults to `m2.read.max_plane_px`. NumPy files are opened as read-only memory
    maps so a large supplied array is refused from its shape without first loading its pixels.
    """
    out = {"image": None, "provenance": None, "error": None}
    try:
        path = Path(path)
        limit = cfg("m2.read.max_plane_px", 64 * 1024 * 1024) if max_pixels is None else max_pixels
        if isinstance(limit, bool) or not isinstance(limit, (int, np.integer)) or limit <= 0:
            raise ValueError("max_pixels must be a positive integer")

        def check_size(width, height):
            if width * height > limit:
                raise ValueError(f"supplied thumbnail {width}x{height} exceeds max_pixels={limit}; "
                                 "increase m2.read.max_plane_px only when enough memory is available")

        if path.suffix.lower() == ".npy":
            array = np.load(path, mmap_mode="r", allow_pickle=False)
            if array.ndim in (2, 3):
                check_size(array.shape[1], array.shape[0])
            img = Image.fromarray(as_rgb_array(array))
        else:
            with Image.open(path) as source:
                check_size(source.width, source.height)
                # Reject high-bit-depth and floating-point images whose RGB conversion would clip
                # intensity values instead of explicitly scaling them.
                if source.mode in {"I", "I;16", "I;16B", "I;16L", "I;16N", "F"} or source.mode.startswith("I;"):
                    raise ValueError(f"image file must be 8-bit per channel; got mode {source.mode}. "
                                     "Convert the intensity scale explicitly before supplying the image.")
                img = source.convert("RGB")
        out["image"] = img
        out["provenance"] = {"source": "supplied", "path": str(path), "sha256": _hash_file(path),
                             "dims": [img.height, img.width]}
    except Exception as exc:                     # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def load_json(path) -> dict:
    """Load a supplied tile list / tile-metrics JSON."""
    out = {"data": None, "provenance": None, "error": None}
    try:
        # `utf-8-sig`: a tile list saved by Excel or Notepad carries a BOM, like the manifests the
        # batch reader already accepts (B12); the hash is still over the bytes on disk.
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
        out["data"] = data
        out["provenance"] = {"source": "supplied", "path": str(path),
                             "sha256": _hash_file(path),
                             "n": len(data) if isinstance(data, list) else None}
    except Exception as exc:                     # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# --------------------------------------------------------------------------- writing

@contextmanager
def atomic_write(path):
    """Yield a temp path beside `path`; on success rename it INTO `path`, on failure remove it.

    Existing destination permissions are preserved. New artifacts follow the caller's umask,
    like an ordinary file opened for writing; this does not modify the process-wide umask.

    Artifact/report writers publish with os.replace after completing the temporary file.
    Atomic replacement and durability depend on the destination filesystem; this is not a
    guarantee for arbitrary bucket mounts or abrupt hardware failure.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    while True:
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o666 if mode is None else mode)
            break
        except FileExistsError:
            continue
    try:
        try:
            if mode is not None:
                os.fchmod(fd, mode)
        finally:
            os.close(fd)
        yield tmp
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def save_mask_png(mask: np.ndarray, path) -> dict:
    """Write a boolean analysis mask as a 1-bit PNG."""
    path = Path(path)
    with atomic_write(path) as tmp:
        Image.fromarray(np.asarray(mask, dtype=bool)).save(tmp, format="PNG", optimize=True)
    return {"source": "computed", "path": str(path), "sha256": _hash_file(path),
            "dims": [int(mask.shape[0]), int(mask.shape[1])], "format": "png_1bit"}


def save_json(data, path) -> dict:
    path = Path(path)
    with atomic_write(path) as tmp:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, default=str)
    n = (len(data) if isinstance(data, list)
         else len(data["tiles"]) if isinstance(data, dict) and isinstance(data.get("tiles"), list)
         else None)
    return {"source": "computed", "path": str(path), "sha256": _hash_file(path), "n": n}


def save_image_png(image, path) -> dict:
    """Write a PIL image (an overlay, the normalised thumbnail) atomically."""
    path = Path(path)
    with atomic_write(path) as tmp:
        image.save(tmp, format="PNG")
    return {"source": "computed", "path": str(path), "sha256": _hash_file(path),
            "dims": [int(image.height), int(image.width)], "format": "png"}


class TiledMaskWriter:
    """Accumulate mask tiles in a packed plane and write a tiled 1-bit LZW TIFF pyramid.

    Row-wise packing uses one bit per pixel. Check the allocation against
    m3.tile_metrics.max_mask_bytes, normally 512 MiB, before creating it. This budget
    limits the packed plane rather than total process memory. add() accepts tiles in
    any order and inserts them directly into the packed plane.
    """

    def __init__(self, plane_wh: tuple, mpp: float = 0.5, *, max_bytes: int | None = None):
        self.w, self.h = int(plane_wh[0]), int(plane_wh[1])
        self.mpp = float(mpp)
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"mask plane dimensions must be positive; got {plane_wh!r}")
        if not np.isfinite(self.mpp) or self.mpp <= 0:
            raise ValueError(f"mask mpp must be finite and positive; got {mpp!r}")
        if max_bytes is None:
            max_bytes = cfg("m3.tile_metrics.max_mask_bytes", 512 * 1024 * 1024)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, (int, np.integer)) or max_bytes <= 0:
            raise ValueError("max_mask_bytes must be a positive integer")
        packed_width = (self.w + 7) // 8
        required = self.h * packed_width
        if required > max_bytes:
            raise ValueError(f"packed mask requires {required} bytes, exceeding max_mask_bytes="
                             f"{max_bytes}; increase m3.tile_metrics.max_mask_bytes or disable "
                             "artifact saving")
        self._packed = np.zeros((self.h, packed_width), dtype=np.uint8)
        self.n_tiles = 0

    def add(self, x: int, y: int, tile_mask: np.ndarray) -> None:
        """Write one tile's boolean support into the plane at plane coords (x, y)."""
        m = np.asarray(tile_mask, dtype=bool)
        th, tw = m.shape
        x, y = int(x), int(y)
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + tw), min(self.h, y + th)
        if x1 <= x0 or y1 <= y0:
            return
        byte0, byte1 = x0 // 8, (x1 + 7) // 8
        row = np.zeros((y1 - y0, (byte1 - byte0) * 8), dtype=bool)
        offset = x0 - byte0 * 8
        row[:, offset:offset + x1 - x0] = m[y0 - y:y1 - y, x0 - x:x1 - x]
        # OR into the packed plane so overlapping tiles never erase a neighbour's pixels.
        self._packed[y0:y1, byte0:byte1] |= np.packbits(row, axis=-1)
        self.n_tiles += 1

    def write(self, path) -> dict:
        """Write tiled TIFF levels from packed masks without unpacking the whole plane.

        Unpack one row band at a time and yield tiles to tifffile. Build each pyramid level
        by nearest-neighbor decimation into another packed array. Peak mask memory is
        about 1.33 times the packed base plus one unpacked band.
        """
        import tifffile
        path = Path(path)
        opts = dict(tile=(TIFF_TILE, TIFF_TILE), compression="lzw", photometric="minisblack")
        res = 10000.0 / self.mpp                 # pixels per cm, so the file states its own scale
        packed, w, h = self._packed, self.w, self.h
        # Atomic: a partially written pyramid is the one truncated artifact that could still parse.
        with atomic_write(path) as tmp, tifffile.TiffWriter(tmp, bigtiff=True) as writer:
            writer.write(_packed_tiles(packed, w, h), shape=(h, w), dtype=bool,
                         subifds=PYRAMID_LEVELS - 1, resolution=(res, res),
                         resolutionunit="CENTIMETER", **opts)
            for _ in range(PYRAMID_LEVELS - 1):
                packed, w, h = _packed_halve(packed, w, h)
                writer.write(_packed_tiles(packed, w, h), shape=(h, w), dtype=bool,
                             subfiletype=1, **opts)
        return {"source": "computed", "path": str(path), "sha256": _hash_file(path),
                "dims": [self.h, self.w], "mpp": self.mpp, "n_tiles_written": self.n_tiles,
                "format": "tiff_1bit_lzw_tiled_pyramid", "pyramid_levels": PYRAMID_LEVELS,
                "bytes": path.stat().st_size}


def _packed_tiles(packed: np.ndarray, w: int, h: int):
    """Yield full TIFF_TILE x TIFF_TILE bool tiles in C order from a row-packed 1-bit plane.

    One row band (TIFF_TILE rows) is unpacked at a time, so the transient is one band, never the
    plane. Edge tiles are zero-padded to the full tile shape, as tifffile's tile iterator expects;
    readers crop to `shape`.
    """
    for y0 in range(0, h, TIFF_TILE):
        band = np.unpackbits(packed[y0:y0 + TIFF_TILE], axis=-1)[:, :w]
        bh = band.shape[0]
        for x0 in range(0, w, TIFF_TILE):
            chunk = band[:, x0:x0 + TIFF_TILE]
            tile = np.zeros((TIFF_TILE, TIFF_TILE), dtype=bool)
            tile[:bh, :chunk.shape[1]] = chunk
            yield tile


def _packed_halve(packed: np.ndarray, w: int, h: int) -> tuple:
    """Decimate a row-packed plane by two, band by band; return (packed, w, h) with ceil-sized dimensions."""
    w2, h2 = (w + 1) // 2, (h + 1) // 2
    out = np.zeros((h2, (w2 + 7) // 8), dtype=np.uint8)
    for y0 in range(0, h, TIFF_TILE):
        band = np.unpackbits(packed[y0:y0 + TIFF_TILE], axis=-1)[:, :w]
        small = band[::2, ::2]
        r0 = y0 // 2
        out[r0:r0 + small.shape[0]] = np.packbits(small, axis=-1)
    return out, w2, h2


def read_tiled_mask_window(path, x: int, y: int, w: int, h: int) -> np.ndarray:
    """Read one window from a tiled mask TIFF without materialising the whole plane."""
    import tifffile
    import zarr
    with tifffile.TiffFile(path) as handle:
        store = zarr.open(handle.aszarr(), mode="r")
        # A pyramidal TIFF's zarr view is a GROUP (one array per level); a flat one is an array.
        level0 = store["0"] if hasattr(store, "keys") else store   # zarr group keys are strings
        return np.asarray(level0[y:y + h, x:x + w]).astype(bool)
