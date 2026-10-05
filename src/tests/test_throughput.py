"""Throughput contracts: full-image pen, bounded CPU threads and manifest identity.

Synthetic inputs only; no model downloads or external slide reads.
Run: python src/tests/test_throughput.py
"""

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))
from fake_slide import FakeSlide, write_tiff
from pathnd_qc.batch import manifest, runner, runs
from pathnd_qc.config import cfg
from pathnd_qc.config.validation import validate
from pathnd_qc.qc_slide.pen import pen


class ThroughputTests(unittest.TestCase):
    def test_default_pen_is_one_whole_image_forward(self):
        self.assertIsNone(cfg("m2.pen.tile_px"))
        image = Image.new("RGB", (1057, 781))
        mask = np.zeros((781, 1057), np.uint8)
        mask[:, 500:] = 1
        with patch.object(pen, "_get_model", return_value=object()), patch.object(
            pen, "_pred", return_value=mask
        ) as predict, patch.object(
            pen, "_pred_tiled", side_effect=AssertionError("unexpected tiling")
        ):
            result = pen.detect_pen(image, weights_path="fixture")
        self.assertIsNone(result["pen_error"])
        self.assertIsNone(result["tile_px"])
        self.assertEqual(predict.call_count, 1)
        self.assertEqual(predict.call_args.args[0].size, image.size)
        np.testing.assert_array_equal(result["pen_mask"], mask.astype(bool))

    def test_explicit_large_tiles_retain_all_pixels(self):
        pixels = np.zeros((1101, 2051, 3), np.uint8)
        pixels[:, ::3, 0] = 255
        sizes = []

        def predict(image, *_):
            sizes.append(image.size)
            return (np.asarray(image)[:, :, 0] > 127).astype(np.uint8)

        with patch.object(pen, "_get_model", return_value=object()), patch.object(
            pen, "_pred", side_effect=predict
        ):
            result = pen.detect_pen(
                Image.fromarray(pixels), weights_path="fixture", tile_px=1024
            )
        self.assertIsNone(result["pen_error"])
        self.assertEqual(len(sizes), 6)
        self.assertLessEqual(max(max(s) for s in sizes), 1152)
        np.testing.assert_array_equal(result["pen_mask"], pixels[:, :, 0] > 127)

    def test_pen_configuration_accepts_whole_image_and_large_tiles_only(self):
        schema = json.loads((SRC / "pathnd_qc/config/defaults.json").read_text())
        for value in (None, 1024, 2048):
            validate({"m2": {"pen": {"tile_px": value}}}, schema)
        for value in (True, 0, -32, 1025, "2048", 2048.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate({"m2": {"pen": {"tile_px": value}}}, schema)

    def test_thread_budget_caps_every_child_library_and_preserves_lower_limits(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            runs, "cpu_count", return_value=32
        ):
            env = runner.child_environment(8)
            for key in (
                "PATHND_CPU_THREADS",
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            ):
                self.assertEqual(env[key], "4")
            self.assertNotIn("PATHND_CPU_THREADS", os.environ)
            with patch.dict(
                os.environ, {"OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "16"}
            ):
                self.assertEqual(runner.child_environment(8)["PATHND_CPU_THREADS"], "2")
            self.assertEqual(runner.child_environment(64)["PATHND_CPU_THREADS"], "1")
            with patch.dict(os.environ, {"PATHND_CPU_THREADS": "0"}), self.assertRaises(
                ValueError
            ):
                runner.child_environment(8)

    def test_real_child_sets_torch_budget_before_pipeline_import(self):
        code = """
import sys, types, json
from pathnd_qc import cli
assert 'torch' not in sys.modules
pipeline = types.ModuleType('pathnd_qc.pipeline')
def run(args):
    import torch
    print(json.dumps([torch.get_num_threads(), torch.get_num_interop_threads()]))
    return 0
pipeline.main = run
sys.modules['pathnd_qc.pipeline'] = pipeline
raise SystemExit(cli.main(['run']))
"""
        env = dict(
            os.environ,
            PYTHONPATH=str(SRC),
            PATHND_CPU_THREADS="2",
            OMP_NUM_THREADS="2",
            MKL_NUM_THREADS="2",
        )
        result = subprocess.run(
            [sys.executable, "-P", "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [2, 1])

    def test_available_cpu_budget_honors_affinity_and_quota(self):
        with patch.object(
            runs.os, "process_cpu_count", return_value=32, create=True
        ), patch.object(
            runs.os, "sched_getaffinity", return_value=set(range(16)), create=True
        ), patch.object(
            Path, "read_text", return_value="600000 100000"
        ):
            self.assertEqual(runs.cpu_count(), 6)
        with patch.object(
            runs.os, "process_cpu_count", return_value=32, create=True
        ), patch.object(
            runs.os, "sched_getaffinity", side_effect=OSError(), create=True
        ), patch.object(
            Path, "read_text", side_effect=FileNotFoundError()
        ):
            self.assertEqual(runs.cpu_count(), 32)

    def test_manifest_same_stem_locations_stay_stable_for_subset_and_order(self):
        rows = [
            {"slide": "gs://example/PART/AB/46884.svs"},
            {"slide": "gs://example/PART/HE/46884.svs"},
        ]
        jobs = manifest.build_jobs(rows)
        self.assertTrue(all(not j.problems for j in jobs))
        self.assertNotEqual(jobs[0].output_subdir, jobs[1].output_subdir)
        self.assertEqual(
            jobs[0].output_subdir, manifest.build_jobs(rows[:1])[0].output_subdir
        )
        self.assertEqual(
            jobs[0].output_subdir,
            manifest.build_jobs(list(reversed(rows)))[1].output_subdir,
        )
        self.assertEqual([j.slide_id for j in jobs], ["46884", "46884"])

    def test_actual_same_name_manifest_batch_completes_and_subset_resumes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"PATHND_CPU_THREADS": "1"}
        ):
            root = Path(tmp).resolve()
            rows = []
            for index, folder in enumerate(("AB", "HE")):
                source = root / folder / "46884.tif"
                source.parent.mkdir()
                write_tiff(FakeSlide(base_wh=(512, 384), seed=50 + index), str(source))
                rows.append({"slide": str(source), "stain": "AT8", "bank": "PART"})
            jobs = manifest.build_jobs(rows)
            kwargs = dict(
                run_args=[
                    "--run_tissue_segmentation",
                    "--no_metadata",
                    "--no_model_download",
                ],
                workers=2,
                batch_id="manifest",
                quiet=True,
                timeout_s=90,
            )
            result = runner.run_batch(jobs, root / "out", **kwargs)
            self.assertEqual(result["counts"]["completed"], 2, result["counts"])
            self.assertEqual(result["threads_per_worker"], 1)
            saved = list((root / "out").rglob("*_report.json"))
            self.assertEqual(len(saved), 2)
            for path in saved:
                report = json.loads(path.read_text())
                self.assertEqual(report["slide_id"], "46884")
                self.assertEqual(report["m1"]["ingestion"]["source"]["dataset"], "PART")
                self.assertEqual(report["provenance"]["execution"]["reasons"], [])
            repeat = runner.run_batch(
                manifest.build_jobs(rows[:1]), root / "out", **kwargs
            )
            self.assertEqual(repeat["counts"]["skipped"], 1)
            self.assertEqual(list((root / "out").rglob("*_report.json")), saved)


if __name__ == "__main__":
    unittest.main()
