"""Read local, GCS, S3 and Azure Blob/ADLS Gen2 slides with TiffSlide over fsspec.

Cloud drivers use ambient provider credentials. Reads use explicit object URIs;
the pipeline does not discover slides by listing cloud buckets or containers.
WSIReader is the provider-neutral alias; GCSWSIReader remains compatible with
existing callers. Integrity/QC checks are separate Module-1 steps.
"""

from __future__ import annotations

import atexit
import errno
import logging
import math
import os
import random
import tempfile
import time
from contextlib import contextmanager, suppress
from typing import Iterator, Optional

import pandas as pd
from PIL import Image
from tiffslide import TiffSlide

from pathnd_qc._logging import get_logger
from pathnd_qc.config.config import cfg
from pathnd_qc.ingestion._download import DownloadError, download_attempt, open_binary

logger = get_logger(__name__)

# Use microns per pixel for physical resolution. The configured relative tolerance
# controls how much upsampling is permitted when selecting a pyramid level.
DEFAULT_MPP_TOL = cfg("m2.read.mpp_tolerance_rel", 0.1)


# --- localized-slide temp-file bookkeeping (used by GCSWSIReader.localize) ---
_LOCALIZED_TEMP_FILES: set[str] = set()


def _remove_quietly(path: str) -> None:
    """Delete a localized temp file, swallowing errors (best-effort cleanup)."""
    _LOCALIZED_TEMP_FILES.discard(path)
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError as exc:  # noqa: BLE001
        logger.warning("Could not remove localized temp %s: %s", path, exc)


@atexit.register
def _cleanup_localized_temp_files() -> None:
    """Safety net: remove any localized temp files still around at interpreter exit."""
    for path in list(_LOCALIZED_TEMP_FILES):
        _remove_quietly(path)


class _RemoteSlide(TiffSlide):
    """Own the remote stream as well as the TIFF reader, including failed opens."""

    def __init__(self, path):
        self._remote_stream = open_binary(path, LOCALIZE_STALL_TIMEOUT_S)
        try:
            super().__init__(self._remote_stream)
        except BaseException:
            self._remote_stream.close()
            raise

    def close(self):
        try:
            super().close()
        finally:
            self._remote_stream.close()


class GCSWSIReader:
    """Read WSIs and metadata from local files, GCS, S3 or Azure.

    Methods accept an explicit path or open slide handle. The reader keeps no collection
    of open slides; last_localize records the most recent localization outcome.
    """

    #: Outcome of the most recent `localize` call (verified by `_download.verify_local_copy`). The context manager has
    #: to keep yielding a plain path, so the verification result is recorded here instead.
    last_localize: dict = {}

    # ------------------------------------------------------------------ metadata
    def load_metadata(self, csv_path: str) -> Optional[pd.DataFrame]:
        """Load a metadata CSV at an explicitly supplied local path or URI."""
        try:
            with open_binary(csv_path, LOCALIZE_STALL_TIMEOUT_S) as handle:
                df = pd.read_csv(handle)
            logger.info("Loaded metadata %s: %d rows x %d cols",
                        csv_path, df.shape[0], df.shape[1])
            return df
        except Exception as exc:  # noqa: BLE001 - report and keep a batch alive
            logger.error("Failed to load metadata %s: %s", csv_path, exc)
            return None

    # --------------------------------------------------------------------- slide
    def open_slide(self, slide_path: str) -> Optional[TiffSlide]:
        """Open one local or cloud WSI with TiffSlide over fsspec.

        Returns None on failure (corrupt/unreadable/permission) rather than raising,
        so one bad slide does not halt a batch. TiffSlide is used instead of
        OpenSlide because it can read remote object URIs directly.
        """
        slide = None
        try:
            slide = (_RemoteSlide(slide_path) if "://" in str(slide_path) else TiffSlide(slide_path))
            logger.info("Opened slide %s: dims=%s levels=%d",
                        slide_path, slide.dimensions, slide.level_count)
            return slide
        except Exception as exc:  # noqa: BLE001
            if slide is not None:
                with suppress(Exception):
                    slide.close()
            logger.error("Failed to open slide %s: %s", slide_path, exc)
            return None

    def open_from_row(self, row: pd.Series,
                      path_col: str = "slide_paths") -> Optional[TiffSlide]:
        """Convenience: open the slide referenced by a metadata-DataFrame row."""
        slide_path = row.get(path_col)
        if not isinstance(slide_path, str) or not slide_path:
            logger.error("Row has no usable '%s' value: %r", path_col, slide_path)
            return None
        return self.open_slide(slide_path)

    @contextmanager
    def slide(self, slide_path: str) -> Iterator[Optional[TiffSlide]]:
        """Context manager that opens a slide and guarantees it is closed."""
        handle = self.open_slide(slide_path)
        try:
            yield handle
        finally:
            if handle is not None:
                handle.close()

    @contextmanager
    def localize(self, slide_path: str, cache_dir: Optional[str] = None,
                 verify: Optional[bool] = None) -> Iterator[Optional[str]]:
        """Yield a local slide path for the duration of a context.

        Existing local files pass through without deletion. Cloud objects are downloaded
        with ambient credentials into cache_dir or the system temporary directory, then
        removed on context exit. Normal interpreter shutdown also attempts cleanup;
        forced termination can leave temporary files.

        When verify is enabled, check available object size and GCS/Azure MD5 metadata.
        S3 ETags are not used as MD5 checks. Transfer verification does not decode pixels.
        Failed downloads or verification are retried with backoff up to localize_retries
        attempts. Lack of byte progress for localize_stall_timeout_s stops an attempt.
        The isolated download worker is reaped before retry or cleanup.

        Yield None on failure and record the cause and attempt count in last_localize.
        Exceptions from the caller’s context body propagate without becoming download errors.
        """
        if verify is None:
            verify = bool(cfg("ingestion.verify_localize", True))
        self.last_localize = {"localized": False, "verified": None, "check": "not_started",
                              "error": None}

        # Already local -> pass through untouched (never delete a caller's own file).
        if os.path.exists(slide_path):
            self.last_localize = {"localized": False, "verified": None, "check": "passthrough",
                                  "error": None}
            yield slide_path
            return

        cache_dir = cache_dir or tempfile.gettempdir()
        os.makedirs(cache_dir, exist_ok=True)
        suffix = os.path.splitext(slide_path)[1] or ".svs"
        fd, local_path = tempfile.mkstemp(prefix="pathnd_wsi_", suffix=suffix, dir=cache_dir)
        os.close(fd)
        _LOCALIZED_TEMP_FILES.add(local_path)
        # Retry download and transfer verification together. Keep yield outside the download
        # exception handler so errors in the caller’s context body propagate as their own failures.
        try:
            with localize_slot() as slot:
                if slot and slot["queued_s"] > 1.0:
                    logger.info("Localize slot %d for %s after %.0f s in the queue", slot["slot"],
                                slide_path, slot["queued_s"])
                self._download_with_retries(slide_path, local_path, verify, slot)
            # Caller exceptions and slot-acquisition errors propagate unchanged; cleanup covers both.
            if self.last_localize.get("localized"):
                yield local_path
            else:
                _remove_quietly(local_path)
                yield None
        finally:
            _remove_quietly(local_path)

    def _download_with_retries(self, slide_path: str, local_path: str, verify: bool, slot) -> None:
        """The retry loop; leaves the outcome on `self.last_localize` (`localized` True or False)."""
        logger.info("Localizing %s -> %s", slide_path, local_path)
        attempts, t_start = 0, time.monotonic()
        while attempts < LOCALIZE_RETRIES:
            attempts += 1
            try:
                info = download_attempt(slide_path, local_path, verify=verify,
                                        stall_timeout=LOCALIZE_STALL_TIMEOUT_S,
                                        progress_every=LOCALIZE_PROGRESS_EVERY_S)
                info["attempts"] = attempts
                if slot:
                    info["queued_s"] = slot["queued_s"]
                if info.get("verified") is False:
                    raise TransferVerificationError(info)
                self.last_localize = info
                logger.info("Localized %s -> %s (%.2f GB, %.0f s, attempt %d/%d, check: %s)",
                            slide_path, local_path, os.path.getsize(local_path) / 1e9,
                            time.monotonic() - t_start, attempts, LOCALIZE_RETRIES, info.get("check"))
                break
            except Exception as exc:  # noqa: BLE001 - recorded; retried while attempts remain
                base = getattr(exc, "info", None) or {"localized": False, "verified": False,
                                                       "check": "download"}
                self.last_localize = dict(base, localized=False, attempts=attempts,
                                          error=f"{type(exc).__name__}: {exc}")
                error_type = exc.error_type if isinstance(exc, DownloadError) else type(exc).__name__
                if (error_type in {"FileNotFoundError", "PermissionError", "IsADirectoryError",
                                   "NotADirectoryError"}
                        or getattr(exc, "status_code", None) in {401, 403}):
                    logger.error("Failed to localize %s: %s (not retryable)",
                                 slide_path, self.last_localize["error"])
                    break
                if attempts < LOCALIZE_RETRIES:
                    delay = LOCALIZE_RETRY_BACKOFF_S * (2 ** (attempts - 1)) * (1 + 0.25 * random.random())
                    logger.warning("Localize attempt %d/%d failed for %s (%s); retrying in %.1f s",
                                   attempts, LOCALIZE_RETRIES, slide_path, self.last_localize["error"],
                                   delay)
                    if delay > 0:
                        time.sleep(delay)
        else:
            self.last_localize["error"] += f" (after {attempts} attempt(s))"
            self.last_localize["localized"] = False
            logger.error("Failed to localize %s: %s", slide_path, self.last_localize["error"])

    # --------------------------------------------------------------- inspection
    def get_slide_info(self, slide: TiffSlide) -> dict:
        """Extract the pyramid geometry and key acquisition metadata for a slide.

        Pulls the fields Module 1 needs downstream (magnification, MPP, vendor,
        scanner) from the standardized `tiffslide.*` / `aperio.*` property keys.
        """
        props = slide.properties
        return {
            "dimensions": slide.dimensions,
            "level_count": slide.level_count,
            "level_dimensions": slide.level_dimensions,
            "level_downsamples": tuple(slide.level_downsamples),
            "vendor": props.get("tiffslide.vendor"),
            "objective_power": props.get("tiffslide.objective-power"),
            "mpp_x": props.get("tiffslide.mpp-x"),
            "mpp_y": props.get("tiffslide.mpp-y"),
            "scanner_id": props.get("aperio.ScanScope ID"),
            "aperio_filename": props.get("aperio.Filename"),
            "scan_date": props.get("aperio.Date"),
        }

    def get_metadata(self, slide: TiffSlide) -> dict:
        """Return the full raw property dict embedded in the slide file."""
        return dict(slide.properties)

    # ---------------------------------------------------------------- rendering
    def read_thumbnail(self, slide: TiffSlide,
                       max_size: int = cfg("ingestion.thumbnail_max_size", 1024)) -> Optional[Image.Image]:
        """Return an RGB thumbnail; refuse a smallest level above m2.read.max_read_px.

        A pyramid-less WSI can have a multigigabyte smallest level. It must use the bounded
        `read_at_mpp` path, rather than decoding that level just to make a preview.
        """
        try:
            if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size <= 0:
                raise ValueError("max_size must be a positive integer")
            best_level = slide.level_count - 1
            width, height = slide.level_dimensions[best_level]
            if width * height > M2_MAX_READ_PX:
                raise ValueError("smallest thumbnail level exceeds m2.read.max_read_px; "
                                 "use read_at_mpp for a bounded plane read")
            thumb = slide.read_region(
                (0, 0), best_level, slide.level_dimensions[best_level]
            ).convert("RGB")
            thumb.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            return thumb
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to extract thumbnail: %s", exc)
            return None

    def read_region(self, slide: TiffSlide, location: tuple[int, int],
                    level: int, size: tuple[int, int]) -> Optional[Image.Image]:
        """Thin RGB wrapper over TiffSlide.read_region (level in pyramid coords)."""
        try:
            return slide.read_region(location, level, size).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to read region loc=%s level=%s size=%s: %s",
                         location, level, size, exc)
            return None


    # --------------------------------------------------- MPP-standardised reading
    def read_at_mpp(self, slide: TiffSlide, target_mpp: float,
                    info: Optional[dict] = None,
                    tol: float = DEFAULT_MPP_TOL,
                    max_read_px: Optional[float] = None,
                    max_plane_px: Optional[int] = None) -> tuple[Optional[Image.Image], dict]:
        """Read a whole-slide plane at target_mpp and return (image, provenance).

        The resolver selects the source level and rounds output dimensions; achieved_mpp
        records the resulting scale. Source levels within max_read_px are read whole.
        Larger levels use horizontal bands, resampled and pasted into the output plane.
        Provenance records read_mode, bands, level_px, and the read cap.

        max_plane_px separately limits output allocation before decoding, normally to
        64 Mpx. Failures return image=None with an error in provenance. Tile analysis
        uses read_window_at_mpp to avoid allocating its full-resolution plane.
        """
        info = info or self.get_slide_info(slide)
        plan = resolve_mpp_level(info, target_mpp, tol)
        if plan["error"]:
            return None, plan
        try:
            cap = M2_MAX_READ_PX if max_read_px is None else float(max_read_px)
            plane_cap = M2_MAX_PLANE_PX if max_plane_px is None else max_plane_px
            if not math.isfinite(cap) or cap <= 0:
                raise ValueError("max_read_px must be finite and positive")
            if isinstance(plane_cap, bool) or not isinstance(plane_cap, int) or plane_cap <= 0:
                raise ValueError("max_plane_px must be a positive integer")
            level = plan["level"]
            lw, lh = info["level_dimensions"][level]
            pw, ph = plan["plane_dims"]
            plan.update(level_px=int(lw) * int(lh), max_read_px=cap, max_plane_px=plane_cap)
            if pw * ph > plane_cap:
                raise ValueError(f"output plane {pw}x{ph} exceeds max_plane_px={plane_cap}; "
                                 "increase m2.read.max_plane_px only when enough memory is available")
            if lw * lh <= cap:
                plan.update(read_mode="whole", bands=1)
                img = self.read_region(slide, (0, 0), level, (lw, lh))
                if img is None:
                    plan["error"] = "read_region returned None"
                    return None, plan
                if (pw, ph) != (lw, lh):
                    img = img.resize((pw, ph), _resample_filter(max(lw / pw, lh / ph)))
                return img, plan
            ds = float(info["level_downsamples"][level])
            img, n = self._read_banded(slide, level, ds, (lw, lh), (pw, ph), cap)
            plan.update(read_mode="banded", bands=n)
            return img, plan
        except Exception as exc:  # noqa: BLE001
            plan["error"] = f"{type(exc).__name__}: {exc}"
            return None, plan

    def _read_banded(self, slide, level: int, ds: float, level_wh, plane_wh, cap: float):
        """Read `level` in horizontal bands of at most ~`cap` px, resample each to the plane, paste.

        Each band is resampled through PIL's float `box`, i.e. the EXACT source span the whole-
        image resize would map that band's output rows onto, with a margin of source rows read
        on either side so the filter sees the same neighbourhood at a band edge as it would in a
        whole read. Floating-point coordinates avoid the scale shifts caused by rounding each
        source span independently; filter rounding can still differ at boundaries.
        The cap is approximate: source-row margins and a minimum of one output row can exceed it.
        """
        lw, lh = level_wh
        pw, ph = plane_wh
        ry = lh / ph                                     # level rows per output row (float)
        band_out = max(1, int(cap / (lw * ry)))          # output rows per band within the cap
        margin = int(math.ceil(max(ry, 1.0))) + 2        # source rows: the filter support, plus slack
        filt = _resample_filter(max(lw / pw, ry))
        out = Image.new("RGB", (pw, ph))
        n = 0
        for y0 in range(0, ph, band_out):
            y1 = min(ph, y0 + band_out)
            src_y0, src_y1 = y0 * ry, y1 * ry            # the exact source span of these output rows
            sy0 = max(0, int(math.floor(src_y0)) - margin)
            sy1 = min(lh, int(math.ceil(src_y1)) + margin)
            # TiffSlide floors level-0 coordinates / downsample; round() can select the prior row.
            region = self.read_region(slide, (0, math.ceil(sy0 * ds)), level, (lw, sy1 - sy0))
            if region is None:
                raise RuntimeError(f"read_region returned None for band {n} (rows {sy0}-{sy1})")
            band = region.resize((pw, y1 - y0), filt,
                                 box=(0.0, src_y0 - sy0, float(lw), src_y1 - sy0))
            out.paste(band, (0, y0))
            n += 1
        return out, n

    def read_window_at_mpp(self, slide: TiffSlide, target_mpp: float, x: int, y: int,
                           w: int, h: int, info: Optional[dict] = None,
                           tol: float = DEFAULT_MPP_TOL,
                           halo_out: int = 0) -> tuple[Optional[Image.Image], dict]:
        """Read a (w, h) window in target-resolution plane coordinates.

        Map the window to a floating-point source rectangle using the achieved per-axis
        scale. Read its enclosing integer region and pass the exact rectangle to Pillow’s
        resize box, preserving the whole-plane sampling phase. Convert the read origin
        to level-0 coordinates using TiffSlide’s floor-based mapping.

        halo_out adds neighboring output pixels for filter support. Clamp the halo to the
        slide boundaries and crop through the resize box, without synthetic padding.
        Returns (image, provenance); failed reads return image=None with an error.
        """
        info = info or self.get_slide_info(slide)
        plan = resolve_mpp_level(info, target_mpp, tol)
        if plan["error"]:
            return None, plan
        try:
            pw, ph = plan["plane_dims"]
            lw, lh = info["level_dimensions"][plan["level"]]
            ds0 = float(info["level_downsamples"][plan["level"]])
            rx, ry = lw / pw, lh / ph                      # ACHIEVED level px per plane px

            # exact source rectangle for this window, in level pixels (floats)
            sx0, sx1 = x * rx, (x + w) * rx
            sy0, sy1 = y * ry, (y + h) * ry
            if sx0 >= lw or sy0 >= lh or sx1 <= 0 or sy1 <= 0:
                plan["error"] = f"window ({x},{y},{w},{h}) falls outside plane {pw}x{ph}"
                return None, plan
            sx1, sy1 = min(sx1, float(lw)), min(sy1, float(lh))

            # integer read box, widened by the halo and clamped to the level (never padded)
            hx, hy = halo_out * rx, halo_out * ry
            lx0 = max(0, int(math.floor(sx0 - hx)))
            ly0 = max(0, int(math.floor(sy0 - hy)))
            lx1 = min(lw, int(math.ceil(sx1 + hx)))
            ly1 = min(lh, int(math.ceil(sy1 + hy)))
            rw, rh = lx1 - lx0, ly1 - ly0
            if rw <= 0 or rh <= 0:
                plan["error"] = f"degenerate read window {rw}x{rh} at level {plan['level']}"
                return None, plan

            # Round upward so TiffSlide's floor division returns the intended level pixel.
            img = self.read_region(slide, (math.ceil(lx0 * ds0), math.ceil(ly0 * ds0)),
                                   plan["level"], (rw, rh))
            if img is None:
                plan["error"] = "read_region returned None"
                return None, plan

            # resample the exact float rectangle straight to the output size -- no separate crop,
            # so no second rounding.
            out_w = max(1, min(w, int(round((sx1 - sx0) / rx))))
            out_h = max(1, min(h, int(round((sy1 - sy0) / ry))))
            box = (sx0 - lx0, sy0 - ly0, sx1 - lx0, sy1 - ly0)
            img = img.resize((out_w, out_h), _resample_filter(max(rx, ry)), box=box)

            plan = dict(plan, window={"x": x, "y": y, "w": out_w, "h": out_h,
                                      "halo_out": halo_out,
                                      "halo_used": [round(sx0 - lx0, 3), round(sy0 - ly0, 3)],
                                      "source_box": [round(v, 3) for v in box]})
            return img, plan
        except Exception as exc:  # noqa: BLE001
            plan["error"] = f"{type(exc).__name__}: {exc}"
            return None, plan

    # ------------------------------------------------------------------- batch
    def iter_slides(self, df: pd.DataFrame, path_col: str = "slide_paths"
                    ) -> Iterator[tuple[int, pd.Series, Optional[TiffSlide]]]:
        """Yield (index, row, open_slide) for each row, opening one at a time.

        The caller is responsible for closing each yielded slide. Rows whose slide
        cannot be opened yield a None handle so the batch can record and continue.
        """
        for idx, row in df.iterrows():
            yield idx, row, self.open_from_row(row, path_col=path_col)


WSIReader = GCSWSIReader  # Provider-neutral public name; preserve existing imports/subclasses.


# MPP and magnification helpers operate on metadata; reader methods perform pixel I/O.

def slide_mpp(info: dict) -> Optional[float]:
    """Level-0 microns-per-pixel, or None. The authoritative scale field."""
    mpp = info.get("mpp_x")
    if mpp in (None, ""):
        return None
    try:
        v = float(mpp)
        return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError):
        return None


def base_magnification(info: dict) -> tuple[Optional[float], str]:
    """Level-0 objective magnification and where it came from.

    Prefer 10 / MPP for this display estimate, falling back to stated objective power.
    Separately, ingestion checks record objective power and can use it to derive an effective
    MPP when stated spacing is unusable; this helper does not perform that validation.
    """
    mpp = slide_mpp(info)
    if mpp is not None:
        return 10.0 / mpp, "mpp_x"
    op = info.get("objective_power")
    if op not in (None, ""):
        try:
            return float(op), "objective_power"
        except (TypeError, ValueError):
            pass
    return None, "unavailable"


def level_mpps(info: dict) -> Optional[list]:
    """Per-level MPP, or None when slide scale or pyramid downsamples are invalid."""
    try:
        mpp = slide_mpp(info)
        if mpp is None:
            op = float(info.get("objective_power"))
            if not math.isfinite(op) or op <= 0:
                return None
            mpp = 10.0 / op
        downsamples = [float(d) for d in info["level_downsamples"]]
        if not downsamples or any(not math.isfinite(d) or d < 1 for d in downsamples):
            return None
        mpps = [mpp * d for d in downsamples]
        return mpps if all(math.isfinite(m) and m > 0 for m in mpps) else None
    except (TypeError, ValueError, KeyError, OverflowError):
        return None


# Read source levels above this pixel budget in bands; cap output allocation separately.
M2_MAX_READ_PX = float(cfg("m2.read.max_read_px", 64 * 1024 * 1024))
M2_MAX_PLANE_PX = cfg("m2.read.max_plane_px", 64 * 1024 * 1024)

# Bound download attempts with backoff and a byte-progress stall timeout.
LOCALIZE_RETRIES = max(1, int(cfg("ingestion.localize_retries", 3)))
LOCALIZE_RETRY_BACKOFF_S = float(cfg("ingestion.localize_retry_backoff_s", 2.0))
LOCALIZE_STALL_TIMEOUT_S = float(cfg("ingestion.localize_stall_timeout_s", 120.0))
LOCALIZE_PROGRESS_EVERY_S = float(cfg("ingestion.localize_progress_every_s", 30.0))


LOCALIZE_SLOTS_ENV = "PATHND_LOCALIZE_SLOTS"      # "<dir>:<k>" -- at most k concurrent downloads


@contextmanager
def localize_slot():
    """Hold one of k cross-process download slots while set (`PATHND_LOCALIZE_SLOTS=<dir>:<k>`).

    The batch runner's work units are separate `pathnd-qc` processes, so a plain semaphore cannot
    bound their concurrent downloads; k lock files under a shared directory can (`flock`, POSIX).
    Yields {slot, queued_s}, or None when no limit is configured. Only the DOWNLOAD is held, never
    the compute that follows it.
    """
    spec = os.environ.get(LOCALIZE_SLOTS_ENV)
    if not spec:
        yield None
        return
    try:
        import fcntl
    except ImportError:                                    # not POSIX: no limit, not an error
        yield None
        return
    try:
        slot_dir, count = spec.rsplit(":", 1)
        k = int(count)
        if not slot_dir or k < 1:
            raise ValueError("directory and positive slot count required")
    except ValueError as exc:
        raise ValueError(f"{LOCALIZE_SLOTS_ENV} must be '<directory>:<positive integer>'") from exc
    os.makedirs(slot_dir, exist_ok=True)
    t0 = t_log = time.monotonic()
    while True:
        for i in range(k):
            handle = open(os.path.join(slot_dir, f"slot_{i}.lock"), "a+")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                continue
            try:
                yield {"slot": i, "queued_s": round(time.monotonic() - t0, 3)}
            finally:
                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                finally:
                    handle.close()
            return
        now = time.monotonic()
        if LOCALIZE_PROGRESS_EVERY_S and now - t_log >= LOCALIZE_PROGRESS_EVERY_S:
            logger.info("Waiting for a download slot: %.0f s queued", now - t0)
            t_log = now
        time.sleep(0.5)


class TransferVerificationError(RuntimeError):
    """The downloaded bytes do not match the remote object. Carries the verification record."""

    def __init__(self, info: dict):
        super().__init__(info.get("error") or "transfer verification failed")
        self.info = info


def _resample_filter(ratio: float):
    """Use BOX for downsampling and BILINEAR for upsampling to avoid ringing in texture measurements."""
    return Image.BOX if ratio > 1.0 else Image.BILINEAR


def resolve_mpp_level(info: dict, target_mpp: float,
                      tol: float = DEFAULT_MPP_TOL) -> dict:
    """Select the coarsest pyramid level within target_mpp * (1 + tol).

    Resample that level toward the target, rounding output dimensions to whole pixels.
    tol limits permitted upsampling; it does not disable resampling. M3 supplies the
    tolerance derived from M1’s scale gate. Invalid scales, dimensions, or unavailable
    levels return error provenance without reading pixels.

    Returns level, source_mpp, achieved_mpp, resample_ratio, direction, plane_dims,
    target_mpp, tol, and error. resample_ratio is source-level pixels per output pixel.
    """
    out = {"target_mpp": target_mpp, "tol": tol, "level": None, "source_mpp": None,
           "achieved_mpp": None, "resample_ratio": None, "direction": None, "plane_dims": None,
           "error": None}
    try:
        target_mpp, tol = float(target_mpp), float(tol)
        if not math.isfinite(target_mpp) or target_mpp <= 0:
            raise ValueError("target_mpp must be finite and positive")
        if not math.isfinite(tol) or tol < 0:
            raise ValueError("tol must be finite and non-negative")
        out.update(target_mpp=target_mpp, tol=tol)
        mpps = level_mpps(info)
        if not mpps:
            raise ValueError("image has missing or invalid mpp_x, objective_power or pyramid downsamples")
        dimensions = info["level_dimensions"]
        if len(dimensions) != len(mpps) or any(
            len(size) != 2 or any(not math.isfinite(n) or n <= 0 or int(n) != n for n in size)
            for size in dimensions
        ):
            raise ValueError("image has invalid pyramid dimensions")

        bound = target_mpp * (1.0 + tol)
        eligible = [(m, i) for i, m in enumerate(mpps) if m <= bound]
        if not eligible:
            raise ValueError(f"no level at or finer than {target_mpp} um/px "
                             f"(+{tol:.0%} = {bound:.4f}); finest is {min(mpps):.4f}")

        source_mpp, level = max(eligible)  # coarsest eligible = least data to read
        ratio = target_mpp / source_mpp
        lw, lh = dimensions[level]
        pw, ph = max(1, int(round(lw / ratio))), max(1, int(round(lh / ratio)))
        out.update(level=level, source_mpp=source_mpp,
                   achieved_mpp=lw * source_mpp / pw,
                   resample_ratio=ratio,
                   direction=("down" if ratio > 1.0 else "up" if ratio < 1.0 else "none"),
                   plane_dims=(pw, ph))
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, ZeroDivisionError) as exc:
        out["error"] = f"Cannot read image at requested scale: {exc}"
    return out


if __name__ == "__main__":
    # Minimal example: open the first slide from a user-supplied CSV and print its info.
    # (No plotting here — visualization lives in the reference notebook.)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    import argparse
    parser = argparse.ArgumentParser(description="Inspect the first slide in a supplied metadata CSV")
    parser.add_argument("--metadata", required=True, metavar="PATH")
    args = parser.parse_args()
    reader = GCSWSIReader()
    meta = reader.load_metadata(args.metadata)
    if meta is None or meta.empty or "slide_paths" not in meta.columns:
        logger.error("Metadata must be readable and contain at least one slide_paths row")
        raise SystemExit(1)
    case = meta.iloc[0]
    with reader.slide(case["slide_paths"]) as s:
        if s is None:
            raise SystemExit(1)
        info = reader.get_slide_info(s)
        print(f"stain_type (from CSV): {case.get('stain_type')}")
        for key, value in info.items():
            print(f"{key}: {value}")
