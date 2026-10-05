"""Measure tissue fraction and Laplacian-variance focus for selected tiles.

Tiles are read from a local slide on the configured MPP plane, normally 0.50
microns per pixel. M1 supplies the pipeline’s scale gate and the reader resolves
each window with that tolerance.

Tissue support starts from the whole-slide mask. Fully covered tiles retain that
support to avoid thresholding within a single tissue class. Edge tiles can be
resegmented with the stain-routed M2 algorithm. Its pixel-sized cleanup filters
have a different physical footprint at tile resolution, which limits interpretation
of pale-stain results.

compute_tile_metrics drops tiles below the tissue threshold before reading, then
checks refined coverage again. Read failures are recorded per tile and processing
continues. Focus summaries and overlays use measured values; artifacts are separate.
refine_tissue_mask provides optional chunk refinement for direct callers and is
not part of the pipeline’s tile sequence.
"""
from __future__ import annotations

import time

import numpy as np
from PIL import Image

from pathnd_qc._logging import get_logger
from pathnd_qc.config.config import cfg, error_kind
from pathnd_qc.ingestion.wsi_reader.wsi_reader import resolve_mpp_level
from pathnd_qc.ingestion.ingestion_checks.ingestion_checks import DEFAULT_MPP_TOL as _M1_MPP_TOL_ABS
from pathnd_qc.qc_slide.focus.focus import compute_focus_score
from pathnd_qc.qc_slide.tissue.tissue import compute_tissue_mask

from ..tiles.tiles import TILE_PX, _tile_to_mask_box

ALPHA_BLEND = cfg("m3.tile_metrics.alpha_blend", cfg("shared.alpha_blend", 0.5))
RESEGMENT_EDGES = cfg("m3.tile_metrics.resegment_edges", True)   # Resegment partly covered tiles when enabled.

# A chunk spans tile_px pixels at CHUNK_TARGET_MPP; a tile spans tile_px pixels
# at TILE_TARGET_MPP. With default scales, one chunk covers a 4-by-4 tile region.
logger = get_logger(__name__)
PROGRESS_EVERY_S = float(cfg("m3.tile_metrics.progress_every_s", 30.0))


def _log_progress(out: list, n_total: int, t_start: float) -> None:
    kept = sum(1 for r in out if r.get("kept"))
    failed = sum(1 for r in out if r.get("read_error"))
    logger.info("tiles %d/%d (%d kept, %d read failures) %.0fs", len(out), n_total, kept, failed,
                time.monotonic() - t_start)

TILE_TARGET_MPP = cfg("m3.read.tile_target_mpp", 0.5)
CHUNK_TARGET_MPP = cfg("m3.read.chunk_target_mpp", 2.0)
HALO_OUT_PX = cfg("m3.read.halo_out_px", 4)
# Express M1’s absolute MPP tolerance relative to the tile target, so its scale gate
# and the tile reader accept the same acquisition resolution.
M3_MPP_TOL_REL = _M1_MPP_TOL_ABS / TILE_TARGET_MPP

CHUNK_RESEG_MIN = cfg("m3.tile_metrics.chunk_reseg_min", 0.10)  # Stage A band: reseg only inside it
TILE_DROP_MIN = cfg("m3.tile_metrics.drop_below", 0.20)         # Stage B: drop below this, BOTH passes


def _mask_patch(tissue_mask: np.ndarray, x: int, y: int, w: int, h: int, sx: float, sy: float,
                mask_w: int, mask_h: int, out_wh: tuple[int, int]) -> np.ndarray:
    """Boolean mask patch under a PLANE footprint, nearest-upsampled to `out_wh` pixels.

    The output size is explicit because a chunk's footprint on the tile plane and its own pixel size
    differ by the chunk/tile MPP ratio; a tile's happen to coincide.
    """
    mx0, mx1, my0, my1 = _tile_to_mask_box(x, y, w, h, sx, sy, mask_w, mask_h)
    patch = tissue_mask[my0:my1, mx0:mx1]
    up = Image.fromarray((patch.astype(np.uint8) * 255)).resize(out_wh, Image.NEAREST)  # (w, h)
    return np.asarray(up) > 127


def _tile_support(tissue_mask: np.ndarray, x: int, y: int, w: int, h: int,
                  sx: float, sy: float, mask_w: int, mask_h: int) -> np.ndarray:
    """Boolean tissue support for a (w x h) tile, from the slide mask upsampled to tile size."""
    return _mask_patch(tissue_mask, x, y, w, h, sx, sy, mask_w, mask_h, (w, h))


def refine_tissue_mask(reader, slide, tissue_mask: np.ndarray, *,
                       target_mpp: float = CHUNK_TARGET_MPP, tile_px: int = TILE_PX,
                       stain_type: str | None = None, reseg_min: float = CHUNK_RESEG_MIN,
                       halo_out: int = HALO_OUT_PX, info: dict | None = None) -> dict:
    """Refine a whole-slide tissue mask in chunks at target_mpp, normally 2.0 microns per pixel.

    Resegment only chunks with reseg_min < coverage < 1.0, where both glass and tissue
    are present. Carry sparse and fully covered chunks forward to avoid thresholding
    within a single class. No tissue is dropped here; tile metrics apply that policy.
    Failed resegmentation falls back to the incoming support and increments n_reseg_failed.

    Returns the stitched tissue_mask, plane_dims, read provenance, chunk counters,
    runtime_s, and refine_error. On a whole-pass failure, tissue_mask is None and the
    caller can retain its input mask.
    """
    t0 = time.monotonic()
    base = {"tissue_mask": None, "plane_dims": None, "read": None, "n_chunks": 0,
            "n_resegmented": 0, "n_carried_sparse": 0, "n_carried_interior": 0, "n_reseg_failed": 0}
    try:
        mask = np.asarray(tissue_mask, dtype=bool)
        if mask.ndim != 2:
            raise ValueError(f"tissue_mask must be 2-D (h, w); got shape {mask.shape}")
        mask_h, mask_w = mask.shape
        if info is None:
            info = reader.get_slide_info(slide)

        plan = resolve_mpp_level(info, target_mpp)
        if plan["error"]:
            raise ValueError(f"cannot reach {target_mpp} um/px: {plan['error']}")
        pw, ph = plan["plane_dims"]
        sx, sy = mask_w / pw, mask_h / ph          # incoming mask per chunk-plane pixel

        fine = np.zeros((ph, pw), dtype=bool)
        n_reseg = n_sparse = n_interior = n_failed = n_chunks = 0
        for cy in range(0, ph, tile_px):
            for cx in range(0, pw, tile_px):
                cw, ch = min(tile_px, pw - cx), min(tile_px, ph - cy)
                n_chunks += 1
                coarse = _mask_patch(mask, cx, cy, cw, ch, sx, sy, mask_w, mask_h, (cw, ch))
                cov = float(coarse.mean())

                in_band = reseg_min < cov < 1.0            # both classes present -> refine
                refined_ok = False
                if in_band:
                    img, _ = reader.read_window_at_mpp(slide, target_mpp, cx, cy, cw, ch,
                                                       info=info, halo_out=halo_out)
                    rs = compute_tissue_mask(img, stain_type=stain_type) if img is not None else None
                    if rs is not None and rs["tissue_error"] is None and rs["tissue_mask"] is not None:
                        rm = np.asarray(rs["tissue_mask"], dtype=bool)
                        fine[cy:cy + ch, cx:cx + cw] = rm[:ch, :cw]
                        n_reseg += 1
                        refined_ok = True
                    else:
                        n_failed += 1                      # falls back to the carried-forward mask
                if not refined_ok:
                    fine[cy:cy + ch, cx:cx + cw] = coarse
                    if cov >= 1.0:
                        n_interior += 1
                    elif not in_band:
                        n_sparse += 1                      # in-band failures counted by n_failed

        return {**base, "tissue_mask": fine, "plane_dims": (pw, ph), "read": plan,
                "n_chunks": n_chunks, "n_resegmented": n_reseg, "n_carried_sparse": n_sparse,
                "n_carried_interior": n_interior, "n_reseg_failed": n_failed,
                "runtime_s": round(time.monotonic() - t0, 3), "refine_error": None}
    except Exception as e:  # noqa: BLE001 — graceful, like the M2 modules
        return {**base, "runtime_s": round(time.monotonic() - t0, 3),
                "refine_error": f"{type(e).__name__}: {e}", "refine_error_type": type(e).__name__}


def compute_tile_metrics(reader, slide, tissue_mask: np.ndarray, tiles: list[dict],
                         plane_dims: tuple[int, int], *, info: dict | None = None,
                         target_mpp: float = TILE_TARGET_MPP, tile_px: int = TILE_PX,
                         stain_type: str | None = None, resegment_edges: bool = RESEGMENT_EDGES,
                         mask_writer=None,
                         drop_below: float | None = TILE_DROP_MIN,
                         halo_out: int = HALO_OUT_PX) -> dict:
    """Measure selected tiles from an open, preferably localized slide.

    reader provides read_window_at_mpp. tissue_mask is a whole-slide 2-D boolean mask;
    tiles contains plane-space x/y/w/h records, and plane_dims is the tile plane’s
    (width, height). stain_type routes optional edge resegmentation.

    The first drop pass rejects low coarse coverage without reading pixels. Surviving
    edge tiles can be resegmented; the second pass checks their refined fraction.
    drop_below=None disables fraction-based drops. Read failures still set kept=False
    and focus=None, record the cause, and allow processing to continue.

    Returns tile records, drop/read/resegmentation counts, runtime_s, and tile_error.
    Records include both coarse and refined coverage, focus, error fields, and kept.
    An optional mask_writer receives support after measurement. A write failure stops
    further insertions while preserving metrics, sets mask_complete=False, and records
    mask_write_error. No writer gives mask_complete=None. Callers must not publish an
    incomplete mask.
    """
    t0 = time.monotonic()
    base = {"tiles": None, "n_tiles": 0, "n_kept": 0, "n_resegmented": 0, "n_read_failed": 0,
            "n_dropped_pass1": 0, "n_dropped_pass2": 0, "drop_below": drop_below,
            "resegment_edges": resegment_edges, "mask_write_error": None,
            "mask_complete": None if mask_writer is None else False}
    try:
        mask = np.asarray(tissue_mask, dtype=bool)
        if mask.ndim != 2:
            raise ValueError(f"tissue_mask must be 2-D (h, w); got shape {mask.shape}")
        mask_h, mask_w = mask.shape
        w0, h0 = int(plane_dims[0]), int(plane_dims[1])
        if w0 <= 0 or h0 <= 0:
            raise ValueError(f"plane_dims must be positive (w, h); got {plane_dims!r}")
        sx, sy = mask_w / w0, mask_h / h0
        if info is None:
            info = reader.get_slide_info(slide)

        out: list[dict] = []
        mask_write_error = None
        # Log tile-loop progress at the configured interval.
        n_total, t_loop, t_log = len(tiles), time.monotonic(), time.monotonic()
        for t in tiles:
            if out and PROGRESS_EVERY_S is not None and time.monotonic() - t_log >= PROGRESS_EVERY_S:
                _log_progress(out, n_total, t_loop)
                t_log = time.monotonic()
            x, y = int(t["x"]), int(t["y"])
            w = int(t.get("w") or min(tile_px, w0 - x))
            h = int(t.get("h") or min(tile_px, h0 - y))
            support = _tile_support(mask, x, y, w, h, sx, sy, mask_w, mask_h)
            coarse_frac = float(support.mean())

            rec = dict(t)
            rec.update(tissue_fraction=round(coarse_frac, 4),
                       tissue_fraction_coarse=round(coarse_frac, 4), resegmented=False,
                       reseg_error=None, read_error=None, focus=None, focus_error=None,
                       dropped_pass1=False, dropped_pass2=False, kept=True,
                       mask_write_error=None, mask_written=None if mask_writer is None else False)

            # PASS 1 — decided from the INCOMING mask alone, so a hopeless tile costs no full-res I/O.
            if drop_below is not None and coarse_frac < drop_below:
                rec.update(dropped_pass1=True, kept=False)
                out.append(rec)
                continue

            tile, rplan = reader.read_window_at_mpp(slide, target_mpp, x, y, w, h,
                                                    info=info, tol=M3_MPP_TOL_REL, halo_out=halo_out)
            if tile is None:
                # Preserve the reader’s error message in the tile record.
                rec.update(read_error=(rplan or {}).get("error") or "read_window_at_mpp returned None",
                           kept=False)
                out.append(rec)
                continue
            # The reader clips overhanging windows; trim support to match the returned image.
            tw, th = tile.size
            if support.shape != (th, tw):
                support = support[:th, :tw]

            # EDGE tile -> re-segment full-res; the refined mask drives fraction AND focus support.
            if resegment_edges and coarse_frac < 1.0:
                try:
                    rs = compute_tissue_mask(tile, stain_type=stain_type)
                    if rs.get("tissue_error") is None and rs.get("tissue_mask") is not None:
                        support = np.asarray(rs["tissue_mask"], dtype=bool)
                        rec["resegmented"] = True
                    else:
                        rec["reseg_error"] = rs.get("tissue_error") or "no tissue_mask returned"
                except Exception as e:  # noqa: BLE001 — keep coarse support on failure
                    rec["reseg_error"] = f"{type(e).__name__}: {e}"

            frac = float(support.mean())
            focus, focus_err = None, None
            if support.any():
                fres = compute_focus_score(tile, tissue_mask=support)
                focus, focus_err = fres["focus_score"], fres["focus_error"]   # error READ, not dropped
            rec["focus_error"] = focus_err

            # Insert each tile’s support into the packed mask as it is measured.
            if mask_writer is not None and mask_write_error is None:
                try:
                    mask_writer.add(x, y, support)
                    rec["mask_written"] = True
                except Exception as exc:  # noqa: BLE001 — optional output must not erase measurements
                    mask_write_error = f"{type(exc).__name__}: {exc}"
                    rec["mask_write_error"] = mask_write_error
                    logger.warning("mask accumulation failed; continuing tile metrics: %s",
                                   mask_write_error)

            # PASS 2 — the authoritative check, on the refined full-res fraction.
            dropped2 = bool(drop_below is not None and frac < drop_below)
            rec.update(tissue_fraction=round(frac, 4), focus=focus,
                       dropped_pass2=dropped2, kept=not dropped2)
            out.append(rec)

        _log_progress(out, n_total, t_loop)

        return {**base, "tiles": out, "n_tiles": len(out),
                "n_kept": sum(1 for r in out if r["kept"]),
                "n_resegmented": sum(1 for r in out if r["resegmented"]),
                "n_read_failed": sum(1 for r in out if r["read_error"]),
                "n_dropped_pass1": sum(1 for r in out if r["dropped_pass1"]),
                "n_dropped_pass2": sum(1 for r in out if r["dropped_pass2"]),
                "mask_write_error": mask_write_error,
                "mask_complete": None if mask_writer is None else mask_write_error is None,
                "runtime_s": round(time.monotonic() - t0, 3), "tile_error": None}
    except Exception as e:  # noqa: BLE001 — graceful, like the M2 modules
        return {**base, "runtime_s": round(time.monotonic() - t0, 3),
                "tile_error": f"{type(e).__name__}: {e}", "tile_error_type": type(e).__name__}


def _blur_color(t: float) -> tuple[float, float, float]:
    """Normalised focus t in [0, 1] -> RGB on a red->yellow->green ramp.

    t<=0.5: red (255,0,0) -> yellow (255,255,0);  t>0.5: yellow -> green (0,255,0). The yellow waypoint
    is explicit so the midtone reads yellow (mimicking matplotlib 'RdYlGn'); a 2-stop red->green ramp
    would pass through muddy olive there.
    """
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    if t <= 0.5:
        return (255.0, 510.0 * t, 0.0)            # red -> yellow
    return (255.0 * (2.0 - 2.0 * t), 255.0, 0.0)  # yellow -> green


def generate_blur_overlay(thumbnail: Image.Image, tile_metrics_result: dict,
                          plane_dims: tuple[int, int], *, tile_px: int = TILE_PX,
                          vmin: float | None = None, vmax: float | None = None,
                          alpha: float = ALPHA_BLEND) -> Image.Image:
    """Colour each tile by its focus (Laplacian variance) onto the thumbnail — a per-tile blur map.

    red = low LV (blurry / low-texture) -> green = high LV (sharp). PURE renderer over an existing
    `compute_tile_metrics` result: no slide I/O, no recompute.

    `thumbnail`   — the M2 analysis-plane image the mask was built on.
    `plane_dims`  — (width, height) of the TILE plane (the frame the tile x,y live in).
    `vmin`/`vmax` — focus range mapped to red..green; default = p5/p95 of the tiles' focus values
                    (self-normalised, so colours are NOT comparable across slides).

    Tiles with focus=None (empty support, a pass-1 drop, or a read failure) are left unpainted. With
    <2 valid tiles or a degenerate range the thumbnail is returned unchanged.
    """
    base = np.array(thumbnail.convert("RGB")).astype(np.float32)
    recs = (tile_metrics_result or {}).get("tiles") or []
    foci = [r["focus"] for r in recs if r.get("focus") is not None]
    if len(foci) < 2:
        return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))

    if vmin is None:
        vmin = float(np.percentile(foci, 5))
    if vmax is None:
        vmax = float(np.percentile(foci, 95))
    if vmax <= vmin:
        return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))

    H, W = base.shape[:2]
    w0, h0 = int(plane_dims[0]), int(plane_dims[1])
    if w0 <= 0 or h0 <= 0:
        raise ValueError(f"plane_dims must be positive (w, h); got {plane_dims!r}")
    sx, sy = W / w0, H / h0

    for r in recs:
        f = r.get("focus")
        if f is None:
            continue
        x, y = int(r["x"]), int(r["y"])
        w = int(r.get("w") or min(tile_px, w0 - x))
        h = int(r.get("h") or min(tile_px, h0 - y))
        mx0, mx1, my0, my1 = _tile_to_mask_box(x, y, w, h, sx, sy, W, H)
        col = _blur_color((f - vmin) / (vmax - vmin))
        for c in range(3):
            base[my0:my1, mx0:mx1, c] = base[my0:my1, mx0:mx1, c] * (1 - alpha) + col[c] * alpha
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))


def refine_to_report(result: dict) -> dict:
    """JSON-safe Stage-A subsection for `report.json` (the stitched mask plane is dropped)."""
    return {"plane_dims": list(result["plane_dims"]) if result.get("plane_dims") else None,
            "n_chunks": result.get("n_chunks"),
            "n_resegmented": result.get("n_resegmented"),
            "n_carried_sparse": result.get("n_carried_sparse"),
            "n_carried_interior": result.get("n_carried_interior"),
            "n_reseg_failed": result.get("n_reseg_failed"),
            "runtime_s": result.get("runtime_s"),
            "error_type": error_kind(result.get("refine_error"), result.get("refine_error_type")),
            "error": result.get("refine_error")}


def to_report(result: dict, outlier_n: int | None = None) -> dict:
    """Summarize tile counts and focus measurements for report.json.

    The report contains aggregate statistics and selected outliers; full tile records
    are saved separately. Low-focus outliers are a reporting selection and remain kept.
    """
    if outlier_n is None:
        outlier_n = cfg("report.focus_outlier_n", 20)
    if result.get("tile_error"):
        # Return null counters when tile measurement fails, rather than suggesting measured zero counts.
        return {"n_tiles": None, "n_kept": None, "kept_fraction": None, "n_resegmented": None,
                "n_read_failed": None, "n_dropped_pass1": None, "n_dropped_pass2": None,
                "drop_below": result.get("drop_below"), "focus_median": None, "focus_p5": None,
                "focus_p95": None, "focus_n": None, "outliers": [],
                "runtime_s": result.get("runtime_s"),
                "mask_write_error": result.get("mask_write_error"),
                "mask_complete": result.get("mask_complete"),
                "error_type": error_kind(result.get("tile_error"), result.get("tile_error_type")),
                "error": result.get("tile_error")}
    recs = result.get("tiles") or []
    foci = sorted(r["focus"] for r in recs if r.get("focus") is not None)
    n = len(recs)

    def _pct(p):
        return float(np.percentile(foci, p)) if foci else None

    def _slim(r, why):
        return {"col": r.get("col"), "row": r.get("row"), "why": why,
                "tissue_fraction": r.get("tissue_fraction"), "focus": r.get("focus")}

    outliers = ([_slim(r, "read_error") for r in recs if r.get("read_error")]
                + [_slim(r, "reseg_error") for r in recs if r.get("reseg_error")]
                + [_slim(r, "dropped_pass1") for r in recs if r.get("dropped_pass1")][:outlier_n]
                + [_slim(r, "dropped_pass2") for r in recs if r.get("dropped_pass2")][:outlier_n]
                + [_slim(r, "low_focus") for r in
                   sorted((r for r in recs if r.get("focus") is not None and r.get("kept")),
                          key=lambda r: r["focus"])[:outlier_n]])

    return {"n_tiles": n, "n_kept": result.get("n_kept"),
            "kept_fraction": (result.get("n_kept") / n) if n else None,
            "n_resegmented": result.get("n_resegmented"),
            "n_read_failed": result.get("n_read_failed"),
            "n_dropped_pass1": result.get("n_dropped_pass1"),
            "n_dropped_pass2": result.get("n_dropped_pass2"),
            "mask_write_error": result.get("mask_write_error"),
            "mask_complete": result.get("mask_complete"),
            "drop_below": result.get("drop_below"),
            "focus_median": _pct(50), "focus_p5": _pct(5), "focus_p95": _pct(95),
            "focus_n": len(foci),
            "outliers": outliers,
            "runtime_s": result.get("runtime_s"),
            "error_type": error_kind(result.get("tile_error"), result.get("tile_error_type")),
            "error": result.get("tile_error")}
