"""A synthetic multi-level WSI that quacks like TiffSlide, so the pipeline can be smoke-tested
end-to-end with no network and no real slide.

Mirrors the three real pyramid shapes in the corpus so the MPP machinery is exercised for real:
  20x  mpp 0.5016  levels 1/4/16   (SEA-AD-like: M2 upsamples slightly, M3 tiles ~1:1)
  40x  mpp 0.2305  levels 1/4/16   (QSBB-like:  M2 downsamples 2.17x, M3 tiles downsample 2.17x)
  10x  mpp 0.7000                  (fails M1's MPP check -> M3 skipped; M2/M4 still run)
  20x  mpp 0.5254  levels 1/4      (Aperio-like: passes M1, M3 upsamples level 0 by 1.05x)
"""
from __future__ import annotations

import numpy as np
from PIL import Image


# Macenko's canonical optical-density vectors (haematoxylin, eosin). Tissue colour is built with
# Beer-Lambert from these, so stain deconvolution finds real vectors and a "fold" is a real fold.
OD_H = np.array([0.65, 0.70, 0.29], dtype=np.float32)
OD_E = np.array([0.07, 0.99, 0.11], dtype=np.float32)


def _render(w: int, h: int, seed: int = 7) -> np.ndarray:
    """Render smooth glass, H&E tissue, a dense fold band and a glassy hole.

    Spatially correlated dye concentrations produce RGB through Beer-Lambert absorption.
    The fold band multiplies both dye concentrations by 2.5.
    """
    rng = np.random.default_rng(seed)
    # Glass is SMOOTH in reality; keep the noise sub-LSB-ish and do the arithmetic in int16 --
    # `uint8 += negative.astype(uint8)` wraps (233 + (-4 -> 252) = 229) and fabricates texture that
    # gray-entropy then reads as tissue, which is the union-oversegmentation failure mode.
    img = np.clip(np.full((h, w, 3), 233, np.int16)
                  + rng.integers(-1, 2, (h, w, 3), dtype=np.int16), 0, 255).astype(np.uint8)
    yy, xx = np.mgrid[0:h, 0:w]
    cy, cx = h * 0.5, w * 0.5
    blob = (((yy - cy) / (h * 0.34)) ** 2 + ((xx - cx) / (w * 0.36)) ** 2) < 1.0
    # Spatially correlated concentration fields (box-blurred noise), not per-pixel white noise --
    # real tissue is correlated, and white noise makes every resampling comparison look catastrophic.
    from scipy.ndimage import uniform_filter
    c_h = uniform_filter(rng.uniform(0.3, 0.9, (h, w)).astype(np.float32), size=3)
    c_e = uniform_filter(rng.uniform(0.2, 0.7, (h, w)).astype(np.float32), size=3)
    band = blob & (np.abs(yy - cy) < max(2, h * 0.05))                      # doubled-over band
    c_h = np.where(band, c_h * 2.5, c_h)
    c_e = np.where(band, c_e * 2.5, c_e)
    od = c_h[:, :, None] * OD_H[None, None, :] + c_e[:, :, None] * OD_E[None, None, :]
    tissue = np.clip(255.0 * np.exp(-od), 0, 255).astype(np.uint8)
    img[blob] = tissue[blob]
    hole = (((yy - cy * 1.35) / (h * 0.07)) ** 2 + ((xx - cx * 1.3) / (w * 0.07)) ** 2) < 1.0
    img[hole] = 233                                                        # sub-mask glass
    return img


class FakeSlide:
    """TiffSlide-shaped: .dimensions, .level_count, .level_dimensions, .level_downsamples,
    .properties, .read_region(location_level0, level, size_in_level_px), .close()."""

    def __init__(self, base_wh=(4096, 3072), downsamples=(1, 4, 16), mpp=0.5016,
                 objective_power=20, seed=7, fail_at=None):
        w0, h0 = base_wh
        self.dimensions = (w0, h0)
        self.level_downsamples = tuple(float(d) for d in downsamples)
        self.level_dimensions = tuple((max(1, int(w0 // d)), max(1, int(h0 // d))) for d in downsamples)
        self.level_count = len(downsamples)
        self.properties = {
            "tiffslide.vendor": "synthetic", "tiffslide.objective-power": objective_power,
            "tiffslide.mpp-x": mpp, "tiffslide.mpp-y": mpp,
            "aperio.ScanScope ID": "FAKE01", "aperio.Filename": "fake", "aperio.Date": "2026-08-24",
        }
        self._levels = [Image.fromarray(_render(w, h, seed)) for (w, h) in self.level_dimensions]
        self.fail_at = fail_at            # (level, col, row)-agnostic: fail the Nth read
        self.n_reads = 0
        self.closed = False

    def read_region(self, location, level, size):
        self.n_reads += 1
        if self.fail_at is not None and self.n_reads == self.fail_at:
            raise OSError("synthetic decode failure (corrupt tile block)")
        ds = self.level_downsamples[level]
        lx, ly = int(round(location[0] / ds)), int(round(location[1] / ds))
        w, h = int(size[0]), int(size[1])
        return self._levels[level].crop((lx, ly, lx + w, ly + h)).convert("RGB")

    def close(self):
        self.closed = True


def write_tiff(slide: "FakeSlide", path: str, compression: str = "jpeg") -> str:
    """Write a FakeSlide to disk as a REAL Aperio-style pyramidal TIFF that tiffslide opens.

    Level 0 plus SubIFD pyramid levels, tiled, with the MPP in the resolution tags AND an Aperio
    ImageDescription (`AppMag`, `MPP`) so `get_slide_info` sees vendor, objective and mpp exactly
    as it does on a scanner file. This is what lets a subprocess `run.py` -- the batch runner's
    work unit -- and an end-to-end integration test run on a slide with no network.
    """
    import tifffile
    mpp = float(slide.properties["tiffslide.mpp-x"])
    op = slide.properties.get("tiffslide.objective-power")
    res = 10000.0 / mpp                                   # pixels per centimetre
    levels = [np.asarray(im.convert("RGB")) for im in slide._levels]
    desc = f"Aperio Image Library Fake v1.0 \n|AppMag = {op}|MPP = {mpp}|"
    with tifffile.TiffWriter(path, bigtiff=False) as tw:
        tw.write(levels[0], tile=(256, 256), compression=compression, photometric="rgb",
                 subifds=len(levels) - 1, resolution=(res, res), resolutionunit="CENTIMETER",
                 description=desc)
        for lv in levels[1:]:
            tw.write(lv, tile=(256, 256), compression=compression, photometric="rgb", subfiletype=1)
    return path


class FakeReader:
    """GCSWSIReader with the real MPP/read logic but a synthetic slide underneath.

    Subclasses the real reader, so `read_at_mpp` / `read_window_at_mpp` / `get_slide_info` are the
    SHIPPED implementations -- only `localize`/`open_slide` are stubbed.
    """

    def __new__(cls, slide: FakeSlide):
        from pathnd_qc.ingestion.wsi_reader.wsi_reader import GCSWSIReader
        from contextlib import contextmanager

        class _R(GCSWSIReader):
            def __init__(self, s):
                self._slide = s
                self.last_localize = {"localized": False, "verified": True, "check": "synthetic",
                                      "error": None}

            @contextmanager
            def localize(self, slide_path, cache_dir=None, verify=None):
                yield "/synthetic/" + str(slide_path).split("/")[-1]

            def open_slide(self, slide_path):
                return self._slide

            @contextmanager
            def slide(self, slide_path):
                yield self._slide

        return _R(slide)
