"""Smoke test — normalization: frozen transforms, reference selection and failure states.

No network or real slides: uses the shared synthetic FakeSlide/FakeReader fixture.
Assertions follow normalization/README.md, function docstrings and mathematical invariants.
Run: python src/tests/smoke_normalization.py
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from fake_slide import FakeReader, FakeSlide  # noqa: E402
from pathnd_qc.normalization.stain_norm import stain_norm as M  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + str(detail) if detail else ''}")


def failed(result, message):
    """A failed public operation carries no usable result and an actionable error."""
    return (result["result"] is None and message in str(result["error"])
            and bool(result.get("error_type")) and result["runtime_s"] >= 0)


def refuse_refit(*args, **kwargs):
    raise AssertionError("normalize must reuse frozen parameters")


out = Path(tempfile.mkdtemp(prefix="normalization_"))
try:
    slide = FakeSlide(base_wh=(2048, 1536), seed=31)
    reader = FakeReader(slide)
    image, geometry = reader.read_at_mpp(slide, 8.0)
    rgb = np.asarray(image)
    # A central rectangular support excludes the fixture's smooth glass. Its exact geometry
    # also provides an independent cropped-image comparison for tissue-only fitting.
    h, w = rgb.shape[:2]
    mask = np.zeros((h, w), dtype=bool)
    mask[h // 3:2 * h // 3, w // 3:2 * w // 3] = True
    crop = rgb[h // 3:2 * h // 3, w // 3:2 * w // 3]

    print("\n[1] Frozen transforms and reference fitting")
    targets, sources, normalized = {}, {}, {}
    for method in M.METHODS:
        # README: both methods fit masked pixels exactly and reuse frozen parameters per tile.
        fitted = M.fit_reference(image, mask, method=method, stain_type="AT8",
                                 reference_slide_id="synthetic-reference",
                                 fitted_at_mpp=geometry["achieved_mpp"])
        source = M.freeze_slide(image, mask, method=method, stain_type="AT8")
        target = fitted["result"]
        targets[method], sources[method] = target, source["result"]
        check(f"{method}: reference fitting records its identity, scale and placeholder status",
              fitted["error"] is None and target["reference_slide_id"] == "synthetic-reference"
              and target["fitted_at_mpp"] == geometry["achieved_mpp"]
              and target["is_placeholder"] is True and target["n_px"] == int(mask.sum()), fitted["error"])
        cropped = M.freeze_slide(crop, method=method)["result"]
        fields = ("stain_matrix", "maxC") if method == "macenko" else ("means", "stds")
        check(f"{method}: fitting the selected pixels equals fitting their rectangular crop",
              all(np.allclose(source["result"][key], cropped[key]) for key in fields))
        before = copy.deepcopy((target, source["result"]))
        with patch.dict(M._FIT, {method: refuse_refit}):
            result = M.normalize(image, target, source["result"], method=method, tissue_mask=mask)
        normalized[method] = result
        pixels = result["result"]
        check(f"{method}: normalization uses frozen parameters without refitting or mutation",
              result["error"] is None and (target, source["result"]) == before, result["error"])
        check(f"{method}: masked output is uint8 RGB with byte-identical background and input",
              pixels.shape == rgb.shape and pixels.dtype == np.uint8
              and np.array_equal(pixels[~mask], rgb[~mask])
              and np.array_equal(np.asarray(image), rgb))
        full = M.normalize(rgb, target, source["result"], method=method)["result"]
        cut = w // 2
        tiles = [M.normalize(part, target, source["result"], method=method)["result"]
                 for part in (rgb[:, :cut], rgb[:, cut:])]
        # The transform is pixelwise; tolerate one quantization unit for floating-point BLAS.
        delta = np.abs(full.astype(int) - np.concatenate(tiles, axis=1).astype(int)).max()
        check(f"{method}: tile transforms agree with the same frozen whole-image transform",
              delta <= 1, f"maximum difference {delta}/255")
        whole = M.normalize_slide(image, target, method=method, tissue_mask=mask, stain_type="AT8")
        check(f"{method}: convenience normalization returns reusable source and target parameters",
              whole["error"] is None and whole["params"]["target"] == target
              and whole["params"]["source"] == source["result"])
        report = M.to_report(whole)
        check(f"{method}: report is JSON-safe, omits pixels and preserves reference limitations",
              isinstance(json.dumps(report, allow_nan=False), str) and "result" not in report
              and report["normalized"] is True and report["is_placeholder"] is True
              and report["reference_fitted_at_mpp"] == geometry["achieved_mpp"]
              and "not calibrated" in report["not_validated"]
              and all(key in report["source_params"] for key in fields))

    approved = M.fit_reference(image, mask, method="reinhard", is_placeholder=False)
    check("reference approval is preserved only when explicitly supplied",
          approved["result"]["is_placeholder"] is False)
    matrix = np.asarray(sources["macenko"]["stain_matrix"])
    check("Macenko stain directions have unit length and descending red optical density",
          np.allclose(np.linalg.norm(matrix, axis=1), 1) and matrix[0, 0] >= matrix[1, 0])
    for label in ("Hirano", "LFB/H&E", "HE"):
        note = M.freeze_slide(image, mask, stain_type=label)
        check(f"{label}: conservative stain-model note does not refuse normalization",
              note["error"] is None and bool(note["result"].get("not_two_dye")))

    print("\n[2] Reference routing")
    entry = {"macenko": targets["macenko"], "reinhard": targets["reinhard"],
             "reference_slide_id": "generic", "fitted_at_mpp": 8.0, "is_placeholder": False}
    bank_entry = {**entry, "reference_slide_id": "bank-specific"}
    spec = {"LFB-HE": entry, "LFB-HE/PART": bank_entry}
    selected = M.load_reference(" lfb/h&e ", bank="part", spec=spec)
    check("case-insensitive stain aliases select the bank-specific reference first",
          selected["error"] is None and selected["result"]["reference_slide_id"] == "bank-specific"
          and selected["params"]["reference_key"] == "LFB-HE/PART")
    fallback = M.load_reference("LFB/H&E", bank="OTHER", spec=spec)
    check("an absent bank-specific target falls back to its stain reference",
          fallback["error"] is None and fallback["result"]["reference_slide_id"] == "generic")
    direct = M.load_reference("LFB-HE", spec=entry)
    check("a directly supplied Macenko entry preserves reference approval and scale",
          direct["error"] is None and direct["result"]["is_placeholder"] is False
          and direct["result"]["fitted_at_mpp"] == 8.0)
    reinhard = M.load_reference("AT8", method=" REINHARD ", spec={"AT8": {"reinhard": targets["reinhard"]}})
    check("Reinhard-only references resolve through a stain-keyed mapping",
          reinhard["error"] is None and reinhard["result"]["method"] == "reinhard")
    default = M.load_reference("Beta-Amy")
    check("configured Beta-Amy alias resolves to an explicitly labelled placeholder",
          default["error"] is None and default["params"]["reference_key"] == "AMYB"
          and default["result"]["is_placeholder"] is True)

    print("\n[3] Numerical boundary conditions")
    intensities = np.array([[[0, 1, 255], [128, 64, 32]]], dtype=np.uint8)
    before = intensities.copy()
    expected_od = np.maximum(-np.log(np.maximum(intensities.astype(float), 1) / 255), M.OD_EPS)
    check("optical density uses a finite natural-log floor without mutating RGB",
          np.allclose(M.rgb2od(intensities), expected_od) and np.array_equal(intensities, before))
    expected_rgb = (255 * np.exp(-np.maximum(expected_od, M.OD_EPS))).astype(np.uint8)
    check("optical density reconstruction follows Beer-Lambert absorption",
          np.array_equal(M.od2rgb(expected_od), expected_rgb))
    flat = np.full((8, 8, 3), [120, 80, 60], dtype=np.uint8)
    flat_source = M.freeze_slide(flat, method="reinhard")["result"]
    flat_norm = M.normalize(flat, targets["reinhard"], flat_source, method="reinhard")
    check("zero-variance Reinhard channels remain finite and spatially constant",
          flat_norm["error"] is None and np.all(np.isfinite(flat_norm["result"]))
          and np.all(flat_norm["result"] == flat_norm["result"][0, 0]))
    # With two constant fields, matching LAB means must map source color onto target color.
    # Allow three RGB units for OpenCV's documented uint8 LAB representation/round trip.
    reference_color = np.array([160, 130, 110], dtype=np.uint8)
    constant_reference = np.broadcast_to(reference_color, flat.shape).copy()
    constant_target = M.fit_reference(constant_reference, method="reinhard")["result"]
    shifted = M.normalize(flat, constant_target, flat_source, method="reinhard")
    color_error = np.abs(shifted["result"].astype(int) - reference_color.astype(int)).max()
    check("Reinhard mean matching maps a constant source to a different constant target color",
          shifted["error"] is None and color_error <= 3, f"maximum difference {color_error}/255")
    # Doubling concentrations squares transmittance: I_out = 255 * (I_in / 255)^2.
    doubled = M.normalize(np.array([[[64, 128, 255]]], dtype=np.uint8),
                           {"stain_matrix": [[1, 0, 0], [0, 1, 0]], "maxC": [2, 2]},
                           {"stain_matrix": [[1, 0, 0], [0, 1, 0]], "maxC": [1, 1]})
    expected = np.array([64 ** 2 / 255, 128 ** 2 / 255, 255]).astype(np.uint8)
    check("Macenko concentration scaling follows the Beer-Lambert transmittance law",
          doubled["error"] is None and np.array_equal(doubled["result"][0, 0], expected))
    # Least squares in this source plane gives a negative first concentration for blue OD.
    # The reconstruction must saturate bright red at 255 instead of wrapping on uint8 cast.
    clipped = M.normalize(np.array([[[255, 255, 2]]], dtype=np.uint8),
                          {"stain_matrix": [[1, 0, 0], [0, 1, 0]], "maxC": [1, 1]},
                          {"stain_matrix": [[1, 1, 0], [0, 1, 1]], "maxC": [1, 1]})
    check("negative least-squares concentrations clip bright reconstruction instead of wrapping",
          clipped["error"] is None and clipped["result"][0, 0, 0] == 255)

    print("\n[4] Rejected inputs stay explicit failures")
    cases = [
        ("unsupported Vahadane has no fallback", lambda: M.freeze_slide(image, method="vahadane"), "not implemented"),
        ("unknown normalization method is refused", lambda: M.normalize(image, {}, {}, method="bogus"), "unknown method"),
        ("unknown reference method is refused", lambda: M.load_reference("AT8", method="bogus"), "unknown method"),
        ("empty tissue cannot fit a source", lambda: M.freeze_slide(image, np.zeros_like(mask)), "empty tissue mask"),
        ("mismatched support cannot fit a source", lambda: M.freeze_slide(image, mask[:1]), "tissue_mask shape"),
        ("Macenko refuses fewer than three pixels", lambda: M.freeze_slide(rgb[:1, :2]), "too few tissue pixels"),
        ("empty reference fitting has no metadata-only success", lambda: M.fit_reference(image, np.zeros_like(mask)), "empty tissue mask"),
        ("float RGB requires an explicit intensity conversion", lambda: M.freeze_slide(rgb.astype(float)), "dtype uint8"),
        ("empty images are rejected", lambda: M.freeze_slide(rgb[:0]), "nonempty"),
        ("unknown stains do not choose a default reference", lambda: M.load_reference("unknown", bank="PART", spec=spec), "no reference"),
        ("a reference missing the requested method is refused", lambda: M.load_reference("AT8", method="reinhard", spec={"AT8": {"macenko": {}}}), "no 'reinhard'"),
        ("a malformed Macenko target is refused at lookup", lambda: M.load_reference("AT8", spec={"macenko": {}}), "stain_matrix"),
        ("non-dict targets are rejected", lambda: M.normalize(image, None, sources["macenko"]), "target params must be a dict"),
        ("non-dict sources are rejected", lambda: M.normalize(image, targets["macenko"], None), "source params must be a dict"),
        ("target method mismatch is rejected", lambda: M.normalize(image, targets["reinhard"], sources["macenko"]), "target params were fitted"),
        ("source method mismatch is rejected", lambda: M.normalize(image, targets["macenko"], sources["reinhard"]), "source params were fitted"),
        ("empty normalization support is not a successful zero image", lambda: M.normalize(image, targets["macenko"], sources["macenko"], tissue_mask=np.zeros_like(mask)), "empty tissue mask"),
        ("missing frozen coefficients are reported", lambda: M.normalize(image, {}, sources["macenko"]), "stain_matrix"),
        ("convenience normalization propagates fitting failures", lambda: M.normalize_slide(image, targets["macenko"], tissue_mask=np.zeros_like(mask)), "empty tissue mask"),
    ]
    for name, operation, message in cases:
        result = operation()
        check(name, failed(result, message), result["error"])
    report = M.to_report(M.normalize(image, None, sources["macenko"]))
    check("failed normalization reports normalized=false and retains the error type",
          report["normalized"] is False and report["error_type"] == "TypeError"
          and bool(report["error"]))

    print("\n[5] Standalone provenance")
    record = M.slide_record("synthetic", "AT8", targets["macenko"], sources["macenko"],
                            extra={"analysis_mpp": 8.0})
    reordered = dict(reversed(list(targets["macenko"].items())))
    second = M.slide_record("synthetic", "AT8", reordered, sources["macenko"])
    changed = M.slide_record("synthetic", "AT8", {**targets["macenko"], "reference_slide_id": "other"}, sources["macenko"])
    check("reference hashes ignore dictionary order and distinguish changed references",
          len(record["reference_hash"]) == 64 and record["reference_hash"] == second["reference_hash"]
          and record["reference_hash"] != changed["reference_hash"])
    tile = M.tile_record("tile-1", (0, 0, 32, 32), out / "tile.png", record["reference_hash"])
    check("tile provenance links to its slide reference and serializes geometry and path",
          tile["source_xywh"] == [0, 0, 32, 32] and tile["normalized_path"] == str(out / "tile.png")
          and tile["provenance_ref"] == record["reference_hash"] and "target_params" not in tile)
    path = M.write_provenance(str(out / "nested" / "reports"), record, [tile])
    payload = json.loads(path.read_text())
    check("provenance creates its output directory and round-trips source, target and tile records",
          path.name == "synthetic_stainnorm.json" and payload["tiles"] == [tile]
          and payload["target_params"] == targets["macenko"] and payload["source_params"] == sources["macenko"]
          and payload["analysis_mpp"] == 8.0)
    missing = M.slide_record("empty", None, None, None)
    missing_tile = M.tile_record("unwritten", None, None, "ref")
    check("absent provenance inputs remain null instead of fabricating references or tile paths",
          missing["reference_hash"] is None and missing["n_tissue_px"] is None
          and missing_tile["source_xywh"] is None and missing_tile["normalized_path"] is None)
    with patch.dict(os.environ, {"PATHND_REPORT_DIR": str(out / "env-reports")}):
        env_path = M.write_provenance(None, missing)
    check("standalone provenance honors PATHND_REPORT_DIR when no directory is supplied",
          env_path.parent == out / "env-reports" and json.loads(env_path.read_text())["tiles"] == [])
finally:
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{'=' * 70}\nNORMALIZATION SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  - {name}")
sys.exit(1 if FAIL else 0)
