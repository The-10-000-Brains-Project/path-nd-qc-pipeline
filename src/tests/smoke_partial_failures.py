"""Smoke test — late corruption and partial-output publication failures.

Hermetic synthetic slide reads and temporary local outputs. Assertions follow the
reporting README, ingestion integrity contract and publication helper docstrings.
No network, trained models, production edits or recorded-output snapshots.
Run: .venv/bin/python src/tests/smoke_partial_failures.py
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from fake_slide import FakeReader, FakeSlide
from pathnd_qc import pipeline as M

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(
        f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + str(detail) if detail else ''}"
    )


def caught(call):
    try:
        return call(), None
    except (Exception, KeyboardInterrupt) as exc:
        return None, exc


def failure_report(exc):
    return exc.report if isinstance(exc, M.RunFailed) else {}


def status_for(report):
    path = Path(report["provenance"]["out_dir"]) / "slide_status.json"
    return json.loads(path.read_text())


out = tempfile.mkdtemp(prefix="pathnd-partial-failures-")
root = Path(out)
try:
    slide_path = root / "slide.svs"
    slide_path.touch()
    slide = FakeSlide(base_wh=(1024, 768), downsamples=(1, 4, 16), mpp=0.5016, seed=63)
    image = slide._levels[1]
    image.save(root / "thumbnail.png")
    shape = (image.height, image.width)
    full = np.ones(shape, bool)
    empty = np.zeros(shape, bool)
    for name, mask in (("tissue", full), ("empty", empty)):
        Image.fromarray(mask.astype(np.uint8) * 255).save(root / f"{name}.png")
    supplied = {
        "thumbnail": str(root / "thumbnail.png"),
        "tissue_mask": str(root / "tissue.png"),
        "fold_mask": str(root / "empty.png"),
        "pen_mask": str(root / "empty.png"),
    }
    tiles = [{"x": x, "y": 256, "w": 128, "h": 128} for x in (128, 384, 640)]
    tile_path = root / "tiles.json"
    tile_path.write_text(json.dumps(tiles))

    def run(name, *, source=slide, **kwargs):
        options = dict(
            out_dir=str(root / name),
            metadata_enabled=False,
            stain_type="AT8",
            download_models=False,
            supplied=supplied,
            components={"focus"},
        )
        options.update(kwargs)
        with patch.object(M, "GCSWSIReader", return_value=FakeReader(source)):
            return M.run(str(slide_path), **options)

    print("\n[1] Corruption beyond healthy integrity samples")
    # Integrity README: bounded probes do not prove every tile decodable. Tile metrics
    # must retain good measurements on both sides of a late corrupt block.
    corrupt = FakeSlide(base_wh=(1024, 768), mpp=0.5016, seed=64)
    original_integrity = M.check_integrity

    def corrupt_second_tile(source, info):
        result = original_integrity(source, info)
        source.fail_at = source.n_reads + 2
        return result

    with patch.object(M, "check_integrity", side_effect=corrupt_second_tile):
        _, exc = caught(
            lambda: run(
                "late_corruption",
                source=corrupt,
                components={"tile_metrics"},
                supplied=dict(supplied, tile_list=str(tile_path)),
            )
        )
    report = failure_report(exc)
    check(
        "late corruption fails requested work despite healthy sampled integrity",
        isinstance(exc, M.RunFailed)
        and report["m1"]["ingestion"]["integrity_passed"] is True
        and report["m3"]["tiles"]["n_read_failed"] == 1
        and report["provenance"]["execution"]["complete"] is False,
        exc,
    )
    records = json.loads(
        Path(report["provenance"]["outputs"]["tile_records"]["path"]).read_text()
    )
    check(
        "good tile measurements before and after corruption remain usable",
        len(records) == 3
        and all(
            records[i]["kept"]
            and records[i]["focus"] is not None
            and records[i]["read_error"] is None
            for i in (0, 2)
        ),
    )
    check(
        "corrupt tile retains its position and cause with null focus rather than zero",
        records[1]["x"] == tiles[1]["x"]
        and records[1]["read_error"]
        and records[1]["focus"] is None
        and not records[1]["kept"],
        records[1],
    )
    check(
        "late decode failure publishes failed status and its partial report",
        status_for(report)["state"] == "failed" and Path(exc.report_path).is_file(),
    )

    print("\n[2] Fine mask allocation fails while tile measurements survive")
    # reporting README: failure to construct the optional fine mask must not erase metrics.
    with patch.object(
        M.store, "TiledMaskWriter", side_effect=MemoryError("synthetic mask allocation")
    ):
        _, exc = caught(
            lambda: run(
                "mask_allocation",
                components={"tile_metrics"},
                supplied=dict(supplied, tile_list=str(tile_path)),
            )
        )
    report = failure_report(exc)
    check(
        "fine-mask allocation refusal makes execution incomplete with its actual cause",
        isinstance(exc, M.RunFailed)
        and report["m3"]["tiles"]["mask_complete"] is False
        and "MemoryError" in report["m3"]["tiles"]["mask_write_error"],
        exc,
    )
    outputs = report["provenance"]["outputs"]
    check(
        "allocation failure still publishes measured records and overlay without a fine mask",
        {"tile_records", "blur_overlay"} <= set(outputs)
        and "tissue_mask_20x" not in outputs
        and all(Path(item["path"]).is_file() for item in outputs.values()),
    )

    print("\n[3] Independent artifact publication after multiple write failures")
    # _save_artifacts: every independent output is attempted, failed files cannot gain
    # successful provenance, and supplied inputs are not copied into generated outputs.
    savedir = root / "multiple_outputs"
    savedir.mkdir()
    generated = {
        "tissue": {"tissue_mask": full},
        "folds": {"fold_mask": empty},
        "pen": {"pen_mask": empty},
        "tile_list": tiles,
        "plane_dims": slide.dimensions,
        "tiles": {"tiles": [{"x": 1, "focus": 2.0}]},
        "stain_norm": {"result": np.asarray(image)},
        "overlay": image,
        "artifact_overlay": image,
    }
    original_json, original_image = M.store.save_json, M.store.save_image_png

    def fail_records(value, path):
        if "tile_records" in Path(path).name:
            raise OSError("synthetic records disk failure")
        return original_json(value, path)

    def fail_normalized(value, path):
        if "normalized" in Path(path).name:
            raise PermissionError("synthetic normalized image permission")
        return original_image(value, path)

    errors = []
    with patch.object(M.store, "save_json", side_effect=fail_records), patch.object(
        M.store, "save_image_png", side_effect=fail_normalized
    ):
        written = M._save_artifacts(
            generated, image, savedir, "slide", {"tile_selection"}, errors=errors
        )
    check(
        "simultaneous image and record failures retain both causes by artifact name",
        {e["artifact"] for e in errors} == {"tile_records", "normalized_image"}
        and {e["type"] for e in errors} == {"OSError", "PermissionError"},
        errors,
    )
    check(
        "independent masks geometry and both later overlays survive multiple save failures",
        set(written)
        == {
            "tissue_mask",
            "fold_mask",
            "pen_mask",
            "tile_list",
            "blur_overlay",
            "artifact_overlay",
        }
        and all(Path(item["path"]).is_file() for item in written.values()),
        list(written),
    )
    check(
        "successfully saved tile geometry preserves the declared plane and coordinates",
        json.loads(Path(written["tile_list"]["path"]).read_text())
        == {
            "plane_mpp": M.m3_tiles.TILE_TARGET_MPP,
            "plane_dims": list(slide.dimensions),
            "tiles": tiles,
        },
    )

    with patch.object(M.store, "save_image_png", side_effect=fail_normalized):
        _, exc = caught(
            lambda: run(
                "normalization_save",
                components={"focus", "stain_normalization"},
                norm_method="reinhard",
            )
        )
    report = failure_report(exc)
    check(
        "normalized-image publication failure preserves successful normalization and focus results",
        isinstance(exc, M.RunFailed)
        and report["m4"]["stain_norm"]["normalized"] is True
        and report["m2"]["focus"]["error"] is None
        and report["error"]["artifact"] == "normalized_image",
        exc,
    )
    check(
        "failed normalization publication is not advertised as a saved output or complete run",
        "normalized_image" not in report["provenance"]["outputs"]
        and report["provenance"]["execution"]["complete"] is False
        and status_for(report)["state"] == "failed",
    )

    print("\n[4] Interrupted publication and failures while handling another failure")
    original_report = M.write_report
    attempts = []

    def interrupted_once(report, directory):
        attempts.append(directory)
        if len(attempts) == 1:
            raise KeyboardInterrupt("synthetic interruption during report publication")
        return original_report(report, directory)

    # _publish docstring and retry comment: one interruption gets a failure-report retry.
    with patch.object(M, "write_report", side_effect=interrupted_once):
        _, exc = caught(lambda: run("report_interrupt"))
    report = failure_report(exc)
    check(
        "interrupted report publication retries once and retains the interrupted outcome",
        isinstance(exc, M.RunFailed)
        and len(attempts) == 2
        and report["error"]["type"] == "KeyboardInterrupt"
        and Path(exc.report_path).is_file(),
        exc,
    )
    check(
        "interrupted publication keeps measured focus and publishes failed status",
        report["m2"]["focus"]["focus_score"] is not None
        and status_for(report)["state"] == "failed",
    )

    with patch.object(
        M.store, "save_image_png", side_effect=fail_normalized
    ), patch.object(
        M, "write_report", side_effect=OSError("synthetic report disk failure")
    ):
        _, exc = caught(
            lambda: run(
                "save_then_report_failure",
                components={"stain_normalization"},
                norm_method="reinhard",
            )
        )
    report = failure_report(exc)
    check(
        "report-write failure retains original artifact failure plus the publication cause",
        isinstance(exc, M.RunFailed)
        and exc.report_path is None
        and report["error"]["artifact"] == "normalized_image"
        and report["error"]["write_error"]["stage"] == "write_report",
        exc,
    )
    check(
        "failed disk report still returns measured normalization in memory and failed status",
        report["m4"]["stain_norm"]["normalized"] is True
        and status_for(report)["state"] == "failed",
    )

    with patch.object(
        M, "_assemble", side_effect=RuntimeError("synthetic report assembly failure")
    ):
        _, exc = caught(lambda: run("assembly_failure"))
    report = failure_report(exc)
    check(
        "assembly failure returns a minimal identifiable failure instead of a raw exception",
        isinstance(exc, M.RunFailed)
        and report["slide_path"] == str(slide_path)
        and report["slide_id"] == "slide"
        and report["error"]["stage"] == "assemble"
        and report["error"]["type"] == "RuntimeError",
        exc,
    )

    print("\n[5] Status damage is diagnosed without masking report outcomes")

    # _write_status docstring: advisory markers never mask the analysis outcome.
    def fail_status(value, path):
        if str(path).endswith("_status.json"):
            raise PermissionError("synthetic status file permission")
        return original_json(value, path)

    with patch.object(M.store, "save_json", side_effect=fail_status):
        result = run("status_denied")
    check(
        "unwritable status markers leave successful analysis and its report usable",
        result["report"]["error"] is None and Path(result["report_path"]).is_file(),
    )

    for label, contents in (
        ("invalid_json", "{broken"),
        ("non_object", "[]"),
        ("missing_identity", "{}"),
        ("blank_identity", '{"slide_path":"  "}'),
    ):
        directory = root / label
        directory.mkdir()
        status_path = directory / "slide_status.json"
        status_path.write_text(contents)
        report_path = directory / "slide_report.json"
        report_path.write_text(json.dumps({"slide_path": str(slide_path)}))
        M._finish_status(directory, "slide", None, report_path)
        state = json.loads(status_path.read_text())
        check(
            f"{label} status is rebuilt with report identity and a visible diagnostic",
            state["slide_path"] == str(slide_path)
            and state["state"] == "completed"
            and state["prior_status_error"]
            and state["report"] == report_path.name,
            state,
        )

    directory = root / "status_keep_fields"
    directory.mkdir()
    status_path = directory / "slide_status.json"
    status_path.write_text(
        json.dumps(
            {
                "slide_path": str(slide_path),
                "batch_fingerprint": "request-identity",
                "started_at": "recorded-start",
                "state": "running",
            }
        )
    )
    M._finish_status(
        directory,
        "slide",
        {"stage": "m3", "type": "OSError", "message": "decode failed"},
        None,
    )
    state = json.loads(status_path.read_text())
    check(
        "final failed status preserves launch identity and fingerprint for batch resume",
        state["started_at"] == "recorded-start"
        and state["batch_fingerprint"] == "request-identity"
        and state["state"] == "failed"
        and state["type"] == "OSError"
        and state["report"] is None,
    )
finally:
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{'=' * 70}\nPARTIAL FAILURES SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  - {name}")
sys.exit(1 if FAIL else 0)
