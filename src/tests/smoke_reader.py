"""Smoke test — reader geometry, bounded reads, failure records and resource ownership.

No network or real slides. Contracts come from the reader README/docstrings and
physical coordinate invariants. The native-level tests use a lossless synthetic
TIFF so they exercise TiffSlide's actual rounding, not just the fake backend.
Run: .venv/bin/python src/tests/smoke_reader.py
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from fake_slide import FakeSlide, FakeReader, write_tiff
from pathnd_qc.ingestion.wsi_reader import wsi_reader as M

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + str(detail) if detail else ''}")


out = tempfile.mkdtemp(prefix="pathnd-reader-")
try:
    print("\n[1] Native coordinates, band boundaries and clipped windows")
    # Adapted from tests/review_pass/smoke_reader_review_new.py. Scanner dimensions
    # do not necessarily divide by a pyramid factor; use the SEA-AD 1/4/16 pyramid.
    source = FakeSlide(base_wh=(1025, 769), mpp=0.5016)
    path = write_tiff(source, os.path.join(out, "fixture.svs"), compression=None)
    reader = M.GCSWSIReader()
    with reader.slide(path) as slide:
        info = reader.get_slide_info(slide)
        raw = reader.get_metadata(slide)
        raw["tiffslide.vendor"] = "changed by caller"
        check("raw metadata is a copy, not mutable slide state",
              slide.properties.get("tiffslide.vendor") != "changed by caller")
        native_mpp = float(info["mpp_x"]) * slide.level_downsamples[2]
        whole, plan = reader.read_at_mpp(slide, native_mpp)
        for x, y, halo in [(0, 0, 0), (10, 17, 0), (10, 17, 4)]:
            tile, provenance = reader.read_window_at_mpp(slide, native_mpp, x, y, 16, 16,
                                                        halo_out=halo)
            expected = np.asarray(whole.crop((x, y, x + 16, y + 16)))
            check(f"native window ({x},{y}), halo {halo}, preserves source pixels",
                  provenance["error"] is None and np.array_equal(tile, expected), provenance["error"])
        banded, provenance = reader.read_at_mpp(slide, native_mpp, max_read_px=256)
        check("native bands preserve the full plane across rounded origins",
              provenance["error"] is None and provenance["bands"] > 1
              and np.array_equal(banded, whole), provenance)
        w, h = whole.size
        tile, provenance = reader.read_window_at_mpp(slide, native_mpp, w - 3, h - 2, 20, 20,
                                                    halo_out=4)
        check("edge window clips to real pixels without synthetic padding",
              tile.size == (3, 2) and np.array_equal(tile, np.asarray(whole)[-2:, -3:]), provenance)
        for x, y in [(w, 0), (0, h), (-20, 0), (0, -20)]:
            image, provenance = reader.read_window_at_mpp(slide, native_mpp, x, y, 10, 10)
            check(f"wholly outside window ({x},{y}) returns error provenance",
                  image is None and "outside" in provenance["error"], provenance["error"])
        thumbnail = reader.read_thumbnail(slide, max_size=20)
        check("thumbnail is RGB and respects its maximum edge",
              thumbnail.mode == "RGB" and max(thumbnail.size) <= 20)

    print("\n[2] Scale validation and allocation limits refuse before pixel decoding")
    tiny = FakeSlide(base_wh=(128, 96))
    fake = FakeReader(tiny)
    info = fake.get_slide_info(tiny)
    invalid_plans = [
        ("missing scale", {"mpp_x": None, "objective_power": None}, 8, .1),
        ("invalid fallback objective", {"mpp_x": None, "objective_power": -20}, 8, .1),
        ("non-numeric MPP and absent fallback", {"mpp_x": "bad", "objective_power": None}, 8, .1),
        ("infinite MPP and absent fallback", {"mpp_x": float("inf"), "objective_power": None}, 8, .1),
        ("zero downsample", {"level_downsamples": (0,)}, 8, .1),
        ("overflowed physical scale", {"mpp_x": 1e308, "level_downsamples": (1, 4, 16)}, 8, .1),
        ("missing pyramid", {"level_downsamples": ()}, 8, .1),
        ("invalid dimensions", {"level_dimensions": ((0, 96),)}, 8, .1),
        ("zero target", {}, 0, .1),
        ("infinite target", {}, float("inf"), .1),
        ("negative tolerance", {}, 8, -.1),
        ("infinite tolerance", {}, 8, float("inf")),
        ("no sufficiently fine source level", {}, .1, .1),
    ]
    for name, changes, target, tol in invalid_plans:
        before = tiny.n_reads
        results = [fake.read_at_mpp(tiny, target, info=dict(info, **changes), tol=tol),
                   fake.read_window_at_mpp(tiny, target, 0, 0, 8, 8,
                                           info=dict(info, **changes), tol=tol)]
        check(f"{name} refuses both read APIs without decoding",
              all(image is None and provenance["error"] for image, provenance in results)
              and tiny.n_reads == before, [p["error"] for _, p in results])
    for limits in [{"max_plane_px": 1}, {"max_plane_px": True}, {"max_plane_px": 1.5},
                   {"max_read_px": 0}, {"max_read_px": float("nan")}]:
        before = tiny.n_reads
        image, provenance = fake.read_at_mpp(tiny, 8, **limits)
        check(f"invalid or exceeded budget {limits} refuses before decoding",
              image is None and bool(provenance["error"]) and tiny.n_reads == before,
              provenance["error"])
    for max_size in [0, True, 1.5]:
        before = tiny.n_reads
        check(f"invalid thumbnail size {max_size!r} refuses before decoding",
              fake.read_thumbnail(tiny, max_size=max_size) is None and tiny.n_reads == before)
    with patch.object(M, "M2_MAX_READ_PX", 1):
        before = tiny.n_reads
        check("thumbnail refuses a smallest source level above its decode budget",
              fake.read_thumbnail(tiny) is None and tiny.n_reads == before)

    print("\n[3] Physical scale fallbacks and resampling")
    # Docstring: MPP takes priority; objective power is only a fallback.
    check("display magnification prefers physical spacing to an inconsistent label",
          M.base_magnification({"mpp_x": .5, "objective_power": 40}) == (20, "mpp_x"))
    check("display magnification falls back to numeric objective power",
          M.base_magnification({"objective_power": "40"}) == (40, "objective_power"))
    for op in [None, "", "bad"]:
        check(f"unavailable objective {op!r} remains explicitly unavailable",
              M.base_magnification({"objective_power": op}) == (None, "unavailable"))
    fallback = dict(info, mpp_x=None, objective_power=20)
    check("objective fallback scales every level using 10/objective",
          M.level_mpps(fallback) == [.5, 2, 8])
    for label, mpp, objective in [("SEA-AD upsample", .5016, 20), ("QSBB downsample", .2305, 40)]:
        sample = FakeSlide(base_wh=(512, 384), mpp=mpp, objective_power=objective)
        r = FakeReader(sample)
        image, p = r.read_at_mpp(sample, .5)
        # Pixel rounding permits at most half an output pixel of physical-width error.
        physical_width = 512 * mpp
        check(f"{label} preserves physical extent after rounding",
              p["error"] is None and abs(image.width * .5 - physical_width) <= .25
              and abs(p["achieved_mpp"] * image.width - physical_width) < 1e-9, p)
        tile, p = r.read_window_at_mpp(sample, .5, 3, 4, 32, 24, halo_out=4)
        check(f"{label} window has its requested physical grid",
              p["error"] is None and tile.size == (32, 24), p)

    print("\n[4] Corruption returns failure rather than a partial successful image")
    for mode in ["whole", "banded", "window", "thumbnail"]:
        bad = FakeSlide(base_wh=(128, 96), downsamples=(1,), fail_at=2 if mode == "banded" else 1)
        r = FakeReader(bad)
        if mode == "thumbnail":
            check("corrupt thumbnail returns None", r.read_thumbnail(bad) is None)
            continue
        if mode == "window":
            image, p = r.read_window_at_mpp(bad, .5016, 0, 0, 16, 16)
        else:
            image, p = r.read_at_mpp(bad, .5016, max_read_px=256 if mode == "banded" else None)
        check(f"corrupt {mode} decode yields no image and an explicit cause",
              image is None and "read_region returned None" in p["error"], p["error"])
    image, p = fake.read_window_at_mpp(tiny, .5016, 10, 10, 0, 10)
    check("zero-width window refuses a degenerate read", image is None and bool(p["error"]), p)

    print("\n[5] Handle ownership and metadata row failures")
    # Use the real context manager; FakeReader.slide deliberately bypasses ownership.
    for fail in [False, True]:
        handle = FakeSlide(base_wh=(32, 24))
        sentinel = RuntimeError("caller failed")
        caught = None
        with patch.object(reader, "open_slide", return_value=handle):
            try:
                with reader.slide(path) as opened:
                    assert opened is handle
                    if fail:
                        raise sentinel
            except RuntimeError as exc:
                caught = exc
        check(f"slide context closes after {'exception' if fail else 'success'}",
              handle.closed and (caught is sentinel if fail else caught is None))
    with patch.object(reader, "open_slide", return_value=None):
        with reader.slide("missing.svs") as handle:
            check("failed open can be handled as a None context result", handle is None)
    for value in [None, "", 42]:
        check(f"unusable metadata slide path {value!r} yields None",
              reader.open_from_row(pd.Series({"slide_paths": value})) is None)
    rows = pd.DataFrame({"slide_paths": [path, os.path.join(out, "missing.svs")]})
    handles = list(reader.iter_slides(rows))
    try:
        check("metadata iteration continues after unreadable rows",
              len(handles) == 2 and handles[0][2] is not None and handles[1][2] is None)
    finally:
        for _, _, handle in handles:
            if handle is not None:
                handle.close()

    print("\n[6] Localization preserves caller files and fails fast on permanent errors")
    with reader.localize(path) as localized:
        check("existing local slides pass through without a download",
              localized == path and reader.last_localize["check"] == "passthrough")
    check("localization never deletes a caller-owned slide", os.path.isfile(path))
    for error in [FileNotFoundError("missing object"), PermissionError("denied")]:
        cache = Path(out, type(error).__name__)
        with patch.dict(os.environ, {M.LOCALIZE_SLOTS_ENV: ""}), \
                patch.object(M, "download_attempt", side_effect=error) as download:
            with reader.localize("https://synthetic/slide.svs", cache_dir=str(cache)) as localized:
                check(f"{type(error).__name__} stops after one attempt and cleans its file",
                      localized is None and download.call_count == 1 and not list(cache.iterdir())
                      and type(error).__name__ in reader.last_localize["error"], reader.last_localize)
    # Resource cleanup must preserve the caller's exception, not report a download failure.
    cache = Path(out, "caller-failure")
    sentinel = RuntimeError("analysis failed after localization")
    with patch.dict(os.environ, {M.LOCALIZE_SLOTS_ENV: ""}), \
            patch.object(M, "download_attempt", return_value={"localized": True, "verified": True}):
        caught = None
        try:
            with reader.localize("https://synthetic/slide.svs", cache_dir=str(cache)):
                raise sentinel
        except RuntimeError as exc:
            caught = exc
        check("localized file is removed when analysis fails without relabeling the error",
              caught is sentinel and not list(cache.iterdir()) and reader.last_localize["localized"])

    print("\n[7] POSIX download slots enforce ownership and reject malformed limits")
    if os.name == "posix":
        import fcntl
        for spec in ["missing-count", f"{out}:0", ":2"]:
            caught = None
            with patch.dict(os.environ, {M.LOCALIZE_SLOTS_ENV: spec}):
                try:
                    with M.localize_slot():
                        pass
                except ValueError as exc:
                    caught = exc
            check(f"invalid slot setting {spec!r} fails with its setting name",
                  caught is not None and M.LOCALIZE_SLOTS_ENV in str(caught), caught)
        with patch.dict(os.environ, {M.LOCALIZE_SLOTS_ENV: f"{out}/slots:2"}):
            with M.localize_slot() as first:
                with M.localize_slot() as second:
                    check("simultaneous downloads occupy distinct lock slots",
                          first["slot"] != second["slot"] and second["queued_s"] >= 0)
            try:
                with M.localize_slot():
                    raise RuntimeError("download interrupted")
            except RuntimeError:
                pass
            with M.localize_slot() as recovered:
                check("slot ownership is released after an exception", recovered["slot"] == first["slot"])
            with patch.object(fcntl, "flock", side_effect=OSError(errno.EIO, "lock filesystem failed")):
                caught = None
                try:
                    with M.localize_slot():
                        pass
                except OSError as exc:
                    caught = exc
                check("unexpected lock errors propagate rather than waiting forever",
                      caught is not None and caught.errno == errno.EIO, caught)
    else:
        print("  NOT TESTABLE  POSIX flock cases: this platform has no POSIX locking")

    print("\n[8] Reader module CLI inspects a local CSV and refuses unusable inputs")
    metadata = Path(out, "slides.csv")
    env = dict(os.environ, PYTHONPATH=str(HERE.parent))
    for label, contents, succeeds in [
        ("valid slide", f"slide_paths\n{path}\n", True),
        ("empty CSV", "slide_paths\n", False),
        ("missing path column", "other\nvalue\n", False),
        ("unreadable slide", f"slide_paths\n{out}/missing.svs\n", False),
    ]:
        metadata.write_text(contents)
        proc = subprocess.run([sys.executable, "-P", "-m", M.__name__, "--metadata", str(metadata)],
                              env=env, capture_output=True, text=True, timeout=30)
        check(f"reader CLI {'prints geometry' if succeeds else 'fails loudly'} for {label}",
              proc.returncode == 0 and "dimensions:" in proc.stdout if succeeds else
              proc.returncode != 0 and "ERROR" in proc.stderr,
              {"returncode": proc.returncode, "stderr": proc.stderr[-500:]})
    check("unreadable metadata is None, not an empty successful table",
          reader.load_metadata(str(Path(out, "missing.csv"))) is None)
finally:
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{'=' * 70}\nREADER SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for n in FAIL: print(f"  - {n}")
sys.exit(1 if FAIL else 0)
