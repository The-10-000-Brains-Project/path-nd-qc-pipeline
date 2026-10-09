"""Test the actual bootstrap embedded in the bucket WDL, without cloud writes.

Set PATHND_TEST_SOURCE_ARCHIVE to the release tarball for the archived-pipeline test.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
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

    def localize_request(self, filename, params, localized, *, success=True):
        """Render the actual command prelude with Cromwell-style localized File values."""
        wdl = (HERE / filename).read_text()
        if "bucket" in filename:
            self.assertIn("python - localized-request.json <<'PATHND_BOOTSTRAP'", wdl)
        else:
            self.assertIn("python /opt/pathnd/run_workflow.py --request localized-request.json", wdl)
        prelude = "    cat > localized-files.txt" + wdl.split("    cat > localized-files.txt", 1)[1]
        prelude = textwrap.dedent(prelude.split("    PATHND_LOCALIZE\n", 1)[0] + "    PATHND_LOCALIZE\n")
        Path("parameters.json").write_text(json.dumps(params))
        values = {**localized, "parameters": str(Path("parameters.json").resolve())}

        def substitute(match):
            expression = match.group(1)
            key = expression.split()[-1]
            value = values.get(key)
            if expression.startswith("sep="):
                return "\n".join(value or [])
            return value or ""

        command = re.sub(r"~\{([^}]+)\}", substitute, prelude)
        # Run with this test environment's interpreter rather than whichever Python is on PATH.
        command = command.replace("python - <<", "exec " + shlex.quote(sys.executable) + " - <<")
        result = subprocess.run(["bash", "-eu", "-c", command], capture_output=True, text=True)
        if not success:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            return result
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(Path("parameters.json").read_text()), params)
        return json.loads(Path("localized-request.json").read_text())

    def test_cloud_json_files_are_replaced_by_command_localizations_in_both_wdls(self):
        for filename in ("pathnd_qc.wdl", "pathnd_qc_bucket.wdl"):
            with self.subTest(wdl=filename):
                wdl = (HERE / filename).read_text()
                task_inputs = wdl.split("task ", 1)[1].split("  # JSON", 1)[0]
                keys = re.findall(r"^    File\?? (\w+)$", task_inputs, re.MULTILINE)
                arrays = re.findall(r"^    Array\[File\] (\w+)$", task_inputs, re.MULTILINE)
                self.assertEqual(arrays, ["metadata_files"])
                localized, params = {}, {"stain": "AT8", "components": ["tissue_segmentation"],
                                         "slide_uri": "s3://remote/untouched.svs",
                                         "metadata_uris": ["gs://remote/untouched.csv"]}
                for key in keys:
                    path = Path(f"{key} ' $(touch INJECTED) `touch INJECTED` \".dat").resolve()
                    path.touch()
                    localized[key] = str(path)
                    params[key] = "gs://bucket/original/" + key
                localized["metadata_files"] = [localized["metadata"], str(Path("second metadata.csv").resolve())]
                Path(localized["metadata_files"][1]).touch()
                params["metadata_files"] = ["gs://bucket/first.csv", "gs://bucket/second.csv"]
                result = self.localize_request(filename, params, localized)
                for key, value in localized.items():
                    self.assertEqual(result[key], value)
                for key in ("stain", "components", "slide_uri", "metadata_uris"):
                    self.assertEqual(result[key], params[key])
                self.assertFalse(Path("INJECTED").exists())

    def test_absent_optional_files_and_remote_uri_mode_survive_localization(self):
        for filename in ("pathnd_qc.wdl", "pathnd_qc_bucket.wdl"):
            with self.subTest(wdl=filename):
                params = {"slide": None, "slide_uri": "gs://bucket/slide.svs", "metadata_files": [],
                          "metadata_uris": ["s3://bucket/metadata.csv"]}
                localized = {}
                if "bucket" in filename:
                    self.archive()
                    params["source_archive"] = "gs://bucket/source.tar.gz"
                    localized["source_archive"] = self.params["source_archive"]
                result = self.localize_request(filename, params, localized)
                self.assertIsNone(result["slide"])
                self.assertEqual(result["metadata_files"], [])
                self.assertEqual(result["slide_uri"], params["slide_uri"])
                self.assertEqual(result["metadata_uris"], params["metadata_uris"])

    def test_unlocalized_file_is_rejected_before_bootstrap(self):
        result = self.localize_request("pathnd_qc.wdl", {"slide": "gs://bucket/a.svs"},
                                       {"slide": "gs://bucket/a.svs"}, success=False)
        self.assertIn("not localized", result.stderr)

    def test_missing_localized_value_and_line_breaks_fail_loudly(self):
        result = self.localize_request("pathnd_qc.wdl", {"slide": "gs://bucket/a.svs"}, {}, success=False)
        self.assertIn("Missing localized workflow input: slide", result.stderr)
        path = Path("slide with\nline break.tif").resolve()
        path.touch()
        result = self.localize_request("pathnd_qc.wdl", {"slide": "gs://bucket/a.svs"},
                                       {"slide": str(path)}, success=False)
        self.assertIn("must not contain line breaks", result.stderr)

    def test_localized_archive_reaches_bootstrap_and_adapter_request(self):
        self.archive()
        archive = self.params["source_archive"]
        original = {**self.params, "source_archive": "gs://bucket/source.tar.gz", "docker_image": "fixture"}
        with self.assertRaises(FileNotFoundError):
            NAMESPACE["prepare_source"](original)
        params = self.localize_request("pathnd_qc_bucket.wdl", original, {"source_archive": archive})
        # main must consume the corrected request before opening the archive, then forward it.
        with patch.dict(NAMESPACE, {"install_source": lambda root, request: self.assertEqual(request, params)}), \
                patch("subprocess.run"), patch("os.execv") as execute, \
                patch.object(Path, "write_text", autospec=True) as write:
            NAMESPACE["main"]("localized-request.json")
        self.assertTrue(Path("source/pathnd-qc-verily/deploy/verily/run_workflow.py").is_file())
        self.assertEqual(execute.call_args.args[1][-2:], ["--request", str(Path("localized-request.json").resolve())])
        self.assertTrue(write.called)

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
                       {"components": ["pen_detection"], "no_model_download": True}):
            with self.subTest(params=params):
                with patch.object(subprocess, "run") as calls, self.assertRaises(ValueError):
                    NAMESPACE["install_source"](self.source, params)
                calls.assert_not_called()

    def test_default_and_selected_normalization_allow_placeholder_references(self):
        for params in ({}, {"components": ["stain_normalization"]}):
            with self.subTest(params=params), patch.object(subprocess, "run") as calls, \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                NAMESPACE["install_source"](self.source, params)
            self.assert_standard_install([call.args[0] for call in calls.call_args_list])
            self.assertIn("shipped m4.reference defaults", stderr.getvalue())
            self.assertIn("placeholders", stderr.getvalue())

    def test_supplied_config_and_unselected_normalization_do_not_warn(self):
        for params in ({"components": ["stain_normalization"], "config_file": "config.json"},
                       {"components": ["tissue_segmentation"]}):
            with self.subTest(params=params), patch.object(subprocess, "run"), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                NAMESPACE["install_source"](self.source, params)
            self.assertNotIn("without config_file", stderr.getvalue())

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
    def test_pinned_archive_runs_tissue_folds_and_default_normalization_on_real_tiff(self):
        sys.path.insert(0, str(SRC / "tests"))
        from fake_slide import FakeSlide, write_tiff
        self.params.update(
            source_archive=os.environ["PATHND_TEST_SOURCE_ARCHIVE"],
            source_sha256=hashlib.sha256(Path(os.environ["PATHND_TEST_SOURCE_ARCHIVE"]).read_bytes()).hexdigest(),
            components=["tissue_segmentation", "fold_detection", "stain_normalization"],
            stain="Hirano", slide=str(Path("slide.tif").resolve()))
        write_tiff(FakeSlide(base_wh=(2048, 1536), seed=43), "slide.tif")
        localized = {key: self.params[key] for key in ("slide", "source_archive")}
        self.params = self.localize_request("pathnd_qc_bucket.wdl",
            {**self.params, "slide": "gs://bucket/slide.tif",
             "source_archive": "gs://bucket/source.tar.gz"}, localized)
        root = self.prepare()
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
        self.assertTrue(report["m4"]["stain_norm"]["normalized"])
        self.assertTrue(report["m4"]["stain_norm"]["is_placeholder"])
        self.assertIn("shipped m4.reference defaults", result.stderr)
        with tarfile.open("results.tar.gz") as archive:
            self.assertIn("pipeline.log", archive.getnames())
            self.assertTrue(any(name.endswith("_report.json") for name in archive.getnames()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
