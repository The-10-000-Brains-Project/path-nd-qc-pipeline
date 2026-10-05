"""Test the actual bootstrap embedded in the bucket WDL, without cloud writes.

Set PATHND_TEST_SOURCE_ARCHIVE to the release tarball for the archived-pipeline test.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1]
WDL = (HERE / "pathnd_qc_bucket.wdl").read_text()
BOOTSTRAP = textwrap.dedent(WDL.split("<<'PATHND_BOOTSTRAP'\n", 1)[1]
                           .split("    PATHND_BOOTSTRAP", 1)[0])
NAMESPACE = {"__name__": "bucket_bootstrap_test"}
exec(compile(BOOTSTRAP, "pathnd_qc_bucket.wdl:bootstrap", "exec"), NAMESPACE)

# config/README.md: the same boolean switch applies to each named component.
ALL_COMPONENTS = {
    "tissue_segmentation", "fold_detection", "pen_detection", "staining_quality", "focus",
    "tile_selection", "tile_metrics", "tile_artifacts", "stain_normalization",
}


class BucketWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pathnd-bucket-test-")
        self.previous = Path.cwd()
        self.previous_sys_path = sys.path[:]
        os.chdir(self.temp.name)
        self.source = SRC
        sys.path.insert(0, str(self.source))
        self.environment = patch.dict(os.environ, {"PATHND_DATA_DIR": str(Path("model-data").resolve())})
        self.environment.start()
        os.environ.pop("PATHND_CONFIG", None)
        from pathnd_qc.config import config
        self.config = config
        self.config_cache = patch.object(config, "_STATE", {"resolved": None, "sources": [], "error": None})
        self.config_cache.start()
        config.load(force=True)
        Path("config.json").write_text("{}")
        Path("pen.pt").touch()
        self.params = {"components": ["tissue_segmentation"]}

    def tearDown(self):
        self.config_cache.stop()
        self.environment.stop()
        sys.path[:] = self.previous_sys_path
        os.chdir(self.previous)
        self.temp.cleanup()

    def configure_exclusions(self, names):
        path = Path("config.json").resolve()
        path.write_text(json.dumps({"components": {name: False for name in names}}))
        return str(path)

    def archive(self, members=None):
        if members is None:
            members = {"pathnd-qc-verily/pyproject.toml": b'[project]\nversion = "0.5.0"\n',
                       "pathnd-qc-verily/build_hooks.py": b"# fixture\n",
                       "pathnd-qc-verily/pathnd_qc/__init__.py": b"# fixture\n",
                       "pathnd-qc-verily/deploy/verily/run_workflow.py": b"# fixture\n"}
        with tarfile.open("source.tar.gz", "w:gz") as archive:
            for name, value in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
        self.params.update(source_archive=str(Path("source.tar.gz").resolve()),
                           source_sha256=hashlib.sha256(Path("source.tar.gz").read_bytes()).hexdigest())

    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return NAMESPACE["prepare_source"](self.params)

    def test_valid_archive_extracts_expected_layout(self):
        self.archive()
        root = self.prepare()
        self.assertTrue((root / "deploy/verily/run_workflow.py").is_file())

    def test_source_guard_fails_before_dependency_installation(self):
        with patch("subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "fingerprint differs"):
                NAMESPACE["install_source"](self.source, {**self.params, "expected_source_sha256": "0" * 64})
            run.assert_not_called()

    def test_checksum_failure_happens_before_extraction(self):
        self.archive()
        self.params["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.prepare()
        self.assertFalse(Path("source").exists())

    def test_malformed_checksum_is_rejected(self):
        self.archive()
        self.params["source_sha256"] = "$(touch INJECTED)"
        with self.assertRaisesRegex(ValueError, "64-character"):
            self.prepare()
        self.assertFalse(Path("INJECTED").exists())

    def test_missing_adapter_is_rejected(self):
        self.archive({"pathnd-qc-verily/pyproject.toml": b'[project]\nversion = "0.5.0"\n',
                      "pathnd-qc-verily/build_hooks.py": b"# fixture\n"})
        with self.assertRaisesRegex(ValueError, "missing deploy/verily/run_workflow.py"):
            self.prepare()

    def test_archive_cannot_escape_extraction_directory(self):
        self.archive({"../ESCAPED": b"unsafe"})
        with self.assertRaises(tarfile.FilterError):
            self.prepare()
        self.assertFalse(Path("ESCAPED").exists())

    def test_unknown_components_and_missing_model_inputs_fail_before_installation(self):
        for params in ({"components": []}, {"components": ["unknown"]},
                       {"components": ["pen_detection"], "no_model_download": True},
                       {"components": ["stain_normalization"]}, {},
                       {"pen_weights": "pen.pt"}):
            with self.subTest(params=params):
                with patch.object(subprocess, "run") as calls, self.assertRaises(ValueError):
                    NAMESPACE["install_source"](self.source, params)
                calls.assert_not_called()

    def test_missing_default_pen_is_allowed_for_automatic_setup(self):
        with patch.object(subprocess, "run") as calls:
            NAMESPACE["install_source"](self.source, {"components": ["pen_detection"]})
        self.assert_standard_install([call.args[0] for call in calls.call_args_list])

    def test_model_components_install_package_and_defer_assets_to_pipeline(self):
        params = {"components": ["pen_detection", "tile_artifacts", "stain_normalization"],
                  "pen_weights": "pen.pt", "config_file": "config.json"}
        with patch.object(subprocess, "run") as calls:
            NAMESPACE["install_source"](self.source, params)
        commands = [call.args[0] for call in calls.call_args_list]
        self.assert_standard_install(commands)
        self.assertFalse(any("setup" in command for command in commands))
        self.assertTrue(any("git" in c for c in commands))

    def test_explicit_subset_installs_same_package_without_unused_model_downloads(self):
        with patch.object(subprocess, "run") as calls:
            NAMESPACE["install_source"](self.source, self.params)
        commands = [call.args[0] for call in calls.call_args_list]
        self.assert_standard_install(commands)
        self.assertFalse(any("setup" in c for c in commands))

    def test_omitted_components_install_full_pipeline(self):
        with patch.object(subprocess, "run") as calls:
            NAMESPACE["install_source"](self.source,
                                        {"pen_weights": "pen.pt", "config_file": "config.json"})
        commands = [call.args[0] for call in calls.call_args_list]
        self.assert_standard_install(commands)
        self.assertFalse(any("setup" in command for command in commands))

    def assert_standard_install(self, commands):
        """Deployment contract: CPU torch first, then the same complete package for every run."""
        install = [sys.executable, "-m", "pip", "install", str(self.source)]
        self.assertIn(install, commands)
        torch_commands = [i for i, command in enumerate(commands)
                          if "https://download.pytorch.org/whl/cpu" in command]
        self.assertEqual(len(torch_commands), 1)
        self.assertLess(torch_commands[0], commands.index(install))
        self.assertIn([sys.executable, "-m", "pip", "check"], commands)

    def test_custom_grandqc_checkout_does_not_download_a_replacement(self):
        with patch.object(subprocess, "run") as calls:
            NAMESPACE["install_source"](self.source, {"components": ["tile_artifacts"],
                                                      "grandqc_repo": "/opt/custom"})
        self.assertFalse(any("setup" in call.args[0] for call in calls.call_args_list))

    def test_configured_grandqc_checkout_does_not_download_a_replacement(self):
        config = Path("config.json").resolve()
        repo = Path("custom-grandqc").resolve()
        repo.mkdir()
        config.write_text(json.dumps({"m3": {"artifacts": {"repo_path": str(repo)}}}))
        with patch.object(subprocess, "run") as calls:
            NAMESPACE["install_source"](self.source, {"components": ["tile_artifacts"],
                                                      "config_file": str(config)})
        self.assertFalse(any("setup" in call.args[0] for call in calls.call_args_list))

    def test_missing_pen_file_fails_before_dependency_installation(self):
        with patch.object(subprocess, "run") as calls:
            with self.assertRaisesRegex(ValueError, "pen_detection"):
                NAMESPACE["install_source"](self.source, {"components": ["pen_detection"],
                                                          "pen_weights": "missing.pt"})
            calls.assert_not_called()

    def test_config_excludes_each_component_before_default_and_explicit_asset_setup(self):
        for name in sorted(ALL_COMPONENTS):
            config_file = self.configure_exclusions([name])
            for explicit in (False, True):
                with self.subTest(disabled=name, explicit=explicit):
                    params = {"config_file": config_file}
                    if name != "pen_detection":
                        params["pen_weights"] = "pen.pt"
                    if explicit:
                        params["components"] = sorted(ALL_COMPONENTS)
                    with patch.object(subprocess, "run") as calls:
                        NAMESPACE["install_source"](self.source, params)
                    self.assertIsNone(self.config.config_error())
                    commands = [call.args[0] for call in calls.call_args_list]
                    self.assert_standard_install(commands)
                    self.assertFalse(any("setup" in command for command in commands))

    def test_disabled_requested_component_is_empty_before_installation(self):
        for name in sorted(ALL_COMPONENTS):
            with self.subTest(disabled=name):
                params = {"components": [name], "config_file": self.configure_exclusions([name])}
                with patch.object(subprocess, "run") as calls:
                    with self.assertRaisesRegex(ValueError, "nothing to run"):
                        NAMESPACE["install_source"](self.source, params)
                    calls.assert_not_called()
        params = {"config_file": self.configure_exclusions(ALL_COMPONENTS)}
        with patch.object(subprocess, "run") as calls:
            with self.assertRaisesRegex(ValueError, "nothing to run"):
                NAMESPACE["install_source"](self.source, params)
            calls.assert_not_called()

    def test_config_disables_model_assets_and_setup_for_a_tissue_run(self):
        params = {"config_file": self.configure_exclusions(ALL_COMPONENTS - {"tissue_segmentation"})}
        with patch.object(subprocess, "run") as calls:
            NAMESPACE["install_source"](self.source, params)
        commands = [call.args[0] for call in calls.call_args_list]
        self.assert_standard_install(commands)
        self.assertFalse(any("setup" in command for command in commands))

    def test_excluding_another_component_keeps_selected_pen_prerequisites(self):
        params = {"components": ["pen_detection", "tile_artifacts"], "no_model_download": True,
                  "config_file": self.configure_exclusions(["tile_artifacts"])}
        with patch.object(subprocess, "run") as calls:
            with self.assertRaisesRegex(ValueError, "pen_detection"):
                NAMESPACE["install_source"](self.source, params)
            calls.assert_not_called()

    def test_configured_pen_checkpoint_satisfies_selected_pen(self):
        weights = Path("registered-pen.pt").resolve()
        weights.touch()
        config = Path("config.json").resolve()
        config.write_text(json.dumps({"m2": {"pen": {"weights_path": str(weights)}}}))
        with patch.object(subprocess, "run") as calls:
            NAMESPACE["install_source"](self.source, {"components": ["pen_detection"],
                                                      "config_file": str(config)})
        self.assert_standard_install([call.args[0] for call in calls.call_args_list])

    def test_older_pipeline_archive_is_refused(self):
        self.archive()
        self.params["expected_pipeline_version"] = "0.3.0"
        with self.assertRaisesRegex(ValueError, "version"):
            self.prepare()

    @unittest.skipUnless(os.environ.get("PATHND_TEST_SOURCE_ARCHIVE"), "release archive not supplied")
    def test_pinned_archive_runs_explicit_tissue_and_folds_on_real_tiff(self):
        sys.path.insert(0, str(SRC / "tests"))
        from fake_slide import FakeSlide, write_tiff
        self.params.update(
            source_archive=os.environ["PATHND_TEST_SOURCE_ARCHIVE"],
            source_sha256=hashlib.sha256(Path(os.environ["PATHND_TEST_SOURCE_ARCHIVE"]).read_bytes()).hexdigest(),
            components=["tissue_segmentation", "fold_detection"],
            stain="Hirano", slide=str(Path("slide.tif").resolve()))
        root = self.prepare()
        write_tiff(FakeSlide(base_wh=(2048, 1536), seed=43), "slide.tif")
        Path("params.json").write_text(json.dumps(self.params))
        result = subprocess.run(
            [sys.executable, str(root / "deploy/verily/run_workflow.py"), "params.json"],
            env=dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(Path("report.json").read_text())
        summary = json.loads(Path("summary.json").read_text())
        self.assertNotIn("trust", report["provenance"])
        self.assertNotIn("trustworthy", summary)
        self.assertFalse(Path("trustworthy.txt").exists())
        self.assertEqual(summary["complete"], report["provenance"]["execution"]["complete"])
        self.assertIn("tissue_segmentation", report["provenance"]["components_run"])
        self.assertEqual(report["m2"]["folds"]["method"], "d+fline")
        self.assertIsNone(report["m2"]["folds"]["fline_error"])
        self.assertTrue(report["m2"]["folds"]["fline_enabled"])
        with tarfile.open("results.tar.gz") as archive:
            self.assertIn("pipeline.log", archive.getnames())
            self.assertTrue(any(name.endswith("_report.json") for name in archive.getnames()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
