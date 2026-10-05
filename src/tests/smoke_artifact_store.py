"""Smoke test — supplied artifacts and packed mask persistence.

Contracts: artifact_store docstrings, USAGE_LIBRARY supplied inputs, and lossless pixel/area
invariants. No network, model weights or real slides. Run: python src/tests/smoke_artifact_store.py
"""

from pathlib import Path
import hashlib
import json
import shutil
import sys
import tempfile
import numpy as np
from PIL import Image
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pathnd_qc import artifact_store as store

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        f"  {'PASS' if cond else 'FAIL'}  {name}{' — '+str(detail) if detail else ''}"
    )


def raises(call):
    try:
        call()
    except ValueError:
        return True
    return False


out = Path(tempfile.mkdtemp(prefix="pathnd-artifact-contract-"))
try:
    print("\n[1] Supplied masks: representation, resizing and refusal")
    mask = np.zeros((12, 16), bool)
    mask[2:9, 3:11] = True
    for suffix in ("npy", "png"):
        path = out / f"mask.{suffix}"
        if suffix == "npy":
            np.save(path, mask)
        else:
            Image.fromarray(mask).save(path)
        r = store.load_mask(path)
        check(
            f"{suffix} mask round-trips without changing pixels",
            r["error"] is None and np.array_equal(r["mask"], mask),
        )
        r = store.load_mask(path, (24, 32))
        check(
            f"{suffix} scale change uses nearest-neighbour and records provenance",
            r["error"] is None
            and np.array_equal(r["mask"], mask.repeat(2, 0).repeat(2, 1))
            and r["provenance"]["direction"] == "up"
            and r["provenance"]["method"] == "nearest",
        )
        check(
            f"{suffix} incompatible aspect is refused",
            bool(store.load_mask(path, (24, 48))["error"]),
        )
        check(
            f"{suffix} zero target side is refused",
            bool(store.load_mask(path, (0, 32))["error"]),
        )
    for shape in [(12, 16, 1), (12, 16, 3), (12, 16, 4)]:
        arr = np.zeros(shape, np.uint8)
        arr[2:9, 3:11, 0] = 255
        path = out / "channels.npy"
        np.save(path, arr)
        check(
            f"{shape[-1]}-channel mask uses nonzero image channels",
            np.array_equal(store.load_mask(path)["mask"], mask),
        )
    for name, arr in [
        ("empty", np.zeros((12, 16), bool)),
        ("zero_side", np.empty((0, 16))),
        ("channel_first", np.ones((3, 12, 16))),
    ]:
        path = out / f"{name}.npy"
        np.save(path, arr)
        r = store.load_mask(path)
        check(
            f"{name} tissue mask has an error and no usable mask",
            bool(r["error"]) and r["mask"] is None,
        )
    check(
        "empty detection mask is explicitly allowed",
        store.load_mask(out / "empty.npy", allow_empty=True)["error"] is None,
    )
    check(
        "unsupported mask format is rejected",
        bool(store.load_mask(out / "mask.tiff")["error"]),
    )

    print("\n[2] Images and JSON: conversion, budgets and corrupt input")
    rgb = np.stack([mask * 210, mask * 100, mask * 50], axis=-1).astype(np.uint8)
    for suffix in ("npy", "png"):
        path = out / f"image.{suffix}"
        if suffix == "npy":
            np.save(path, rgb)
        else:
            Image.fromarray(rgb).save(path)
        r = store.load_image(path, max_pixels=192)
        check(
            f"{suffix} RGB image preserves pixels and content hash",
            r["error"] is None
            and np.array_equal(np.asarray(r["image"]), rgb)
            and r["provenance"]["sha256"]
            == hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        check(
            f"{suffix} over-budget image is refused",
            store.load_image(path, max_pixels=191)["image"] is None,
        )
    for budget in (0, -1, True, 1.5):
        check(
            f"invalid image budget {budget!r} is refused",
            bool(store.load_image(out / "image.png", max_pixels=budget)["error"]),
        )
    Image.fromarray(np.ones((12, 16), np.uint16) * 65535).save(out / "deep.png")
    check(
        "high-bit-depth image is refused instead of clipped",
        bool(store.load_image(out / "deep.png")["error"]),
    )
    path = out / "tiles.json"
    path.write_text("\ufeff" + json.dumps([{"x": 0, "y": 0}]), encoding="utf-8")
    check(
        "JSON with a UTF-8 BOM remains readable",
        store.load_json(path)["data"] == [{"x": 0, "y": 0}],
    )
    path.write_text("{broken")
    check(
        "corrupt JSON has no usable data",
        store.load_json(path)["data"] is None and bool(store.load_json(path)["error"]),
    )

    print("\n[3] Failed atomic writes preserve the old artifact")
    path = out / "atomic.txt"
    path.write_text("original")
    path.chmod(0o640)
    try:
        with store.atomic_write(path) as tmp:
            tmp.write_text("partial")
            raise OSError("injected disk failure")
    except OSError:
        pass
    check(
        "interrupted publication preserves previous bytes and removes temporary file",
        path.read_text() == "original" and not tmp.exists(),
    )
    with store.atomic_write(path) as tmp:
        tmp.write_text("replacement")
    check(
        "successful replacement preserves permissions",
        path.read_text() == "replacement" and path.stat().st_mode & 0o777 == 0o640,
    )

    print("\n[4] Packed TIFF: overlaps, clipping, scale and pyramid geometry")
    # Non-byte-aligned width and offsets exercise packed storage, not just aligned writes.
    expected = np.zeros((533, 541), bool)
    writer = store.TiledMaskWriter((541, 533), mpp=0.5)
    for x, y, w, h in [
        (3, 5, 31, 29),
        (19, 15, 28, 29),
        (-3, -2, 9, 8),
        (530, 525, 20, 20),
    ]:
        writer.add(x, y, np.ones((h, w), bool))
        expected[max(y, 0) : min(y + h, 533), max(x, 0) : min(x + w, 541)] = True
    writer.add(3, 5, np.zeros((29, 31), bool))
    writer.add(999, 999, np.ones((3, 3), bool))
    path = out / "mask.tif"
    record = writer.write(path)
    with tifffile.TiffFile(path) as tif:
        arrays = [level.asarray() for level in tif.series[0].levels]
        check(
            "overlapping and clipped tiles preserve their union exactly",
            np.array_equal(arrays[0], expected),
        )
        check(
            "four pyramid levels use nearest-neighbour decimation",
            len(arrays) == 4
            and all(
                np.array_equal(arr, expected[:: 2**i, :: 2**i])
                for i, arr in enumerate(arrays)
            ),
        )
        check(
            "TIFF resolution records 0.5 microns per pixel",
            tif.pages[0].tags["XResolution"].value == (20000, 1),
        )
    check(
        "window reads match the original mask without a whole-plane decode",
        np.array_equal(
            store.read_tiled_mask_window(path, 2, 4, 39, 37), expected[4:41, 2:41]
        ),
    )
    check(
        "persisted mask provenance hashes the file",
        record["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    for args in [
        ((0, 5), {}),
        ((10, 10), {"mpp": 0}),
        ((10, 10), {"max_bytes": 1}),
        ((10, 10), {"max_bytes": True}),
    ]:
        check(
            f"invalid or over-budget packed plane {args} is refused",
            raises(lambda args=args: store.TiledMaskWriter(args[0], **args[1])),
        )
finally:
    shutil.rmtree(out, ignore_errors=True)
print(f"\n{'='*70}\nARTIFACT STORE SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  - {name}")
sys.exit(bool(FAIL))
