"""Adapter contracts plus a real synthetic TIFF run; no cloud or model downloads.

From the repository root: python src/deploy/verily/test_workflow.py
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1]
sys.path.insert(0, str(SRC))
spec = importlib.util.spec_from_file_location("workflow_adapter", HERE / "run_workflow.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

# USAGE opening and deployment README: all nine are enabled by default; every component can be
# excluded by config, including from explicit requests. Names do not come from the registry.
ALL_COMPONENTS = {
    "tissue_segmentation", "fold_detection", "pen_detection", "staining_quality", "focus",
    "tile_selection", "tile_metrics", "tile_artifacts", "stain_normalization",
}


@contextlib.contextmanager
def present_pen_dependencies():
    """Stub only the adapter's two model checks; preserve unrelated lazy import detection."""
    find_spec = adapter.importlib.util.find_spec
    with patch.object(adapter.importlib.util, "find_spec", side_effect=lambda name, *a, **kw:
                      object() if name in {"torch", "segmentation_models_pytorch"}
                      else find_spec(name, *a, **kw)):
        yield


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pathnd-verily-test-")
        self.previous = Path.cwd()
        self.previous_sys_path = sys.path[:]
        os.chdir(self.temp.name)
        self.environment = patch.dict(os.environ, {"PATHND_DATA_DIR": str(Path("model-data").resolve())})
        self.environment.start()
        os.environ.pop("PATHND_CONFIG", None)
        from pathnd_qc.config import config
        self.config = config
        self.config_cache = patch.object(config, "_STATE", {"resolved": None, "sources": [], "error": None})
        self.config_cache.start()
        config.load(force=True)
        Path("slide.tif").touch()
        self.params = {"slide": str(Path("slide.tif").resolve()), "stain": "Hirano",
                       "components": ["tissue_segmentation"]}

    def tearDown(self):
        self.config_cache.stop()
        self.environment.stop()
        sys.path[:] = self.previous_sys_path
        os.chdir(self.previous)
        self.temp.cleanup()

    def configure_exclusions(self, names):
        path = Path("selection.json").resolve()
        path.write_text(json.dumps({"components": {name: False for name in names}}))
        self.params["config_file"] = str(path)
        # build_command consumes resolved configuration; adapter.main performs this reload itself.
        os.environ["PATHND_CONFIG"] = str(path)
        self.config.load(force=True)
        self.assertIsNone(self.config.config_error())

    def test_stain_and_paths_are_arguments_not_shell(self):
        self.params["stain"] = "Hirano'; $(touch INJECTED); `id`"
        cmd = adapter.build_command(self.params)
        self.assertEqual(cmd[cmd.index("--stain") + 1], self.params["stain"])
        self.assertIn("--no_metadata", cmd)
        self.assertFalse(Path("INJECTED").exists())

    def test_empty_and_unknown_component_selections_refused(self):
        for value in ([], None, "tissue_segmentation", ["not_a_component"]):
            self.params["components"] = value
            with self.assertRaises(ValueError):
                adapter.build_command(self.params)

    def configure_model_inputs(self):
        """Structural preflight fixtures; these checkpoints are never loaded as real models."""
        Path("pen.pt").touch()
        Path("config.json").write_text("{}")
        root = Path("grandqc").resolve()
        for name in ("main.py", "wsi_tis_detect.py", "models/qc/GrandQC_MPP1.pth",
                     "models/td/Tissue_Detection_MPP10.pth"):
            dest = root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.touch()
        self.params.update(pen_weights=str(Path("pen.pt").resolve()), grandqc_repo=str(root),
                           config_file=str(Path("config.json").resolve()))

    def test_omitted_selection_defaults_to_all_nine_components(self):
        self.params.pop("components")
        self.configure_model_inputs()
        with present_pen_dependencies():
            command = adapter.build_command(self.params)
        self.assertEqual({flag.removeprefix("--run_") for flag in command
                          if flag.startswith("--run_")}, ALL_COMPONENTS)

    def test_explicit_subset_does_not_require_unselected_models(self):
        self.params.update(pen_weights="missing-pen.pt", grandqc_repo="missing-grandqc")
        command = adapter.build_command(self.params)
        self.assertEqual([flag for flag in command if flag.startswith("--run_")],
                         ["--run_tissue_segmentation"])

    def test_config_can_exclude_each_component_from_default_and_explicit_selections(self):
        self.configure_model_inputs()
        for name in sorted(ALL_COMPONENTS):
            self.configure_exclusions([name])
            for explicit in (False, True):
                with self.subTest(disabled=name, explicit=explicit):
                    if explicit:
                        self.params["components"] = sorted(ALL_COMPONENTS)
                    else:
                        self.params.pop("components", None)
                    with present_pen_dependencies():
                        command = adapter.build_command(self.params)
                    selected = {flag.removeprefix("--run_") for flag in command
                                if flag.startswith("--run_")}
                    self.assertEqual(selected, ALL_COMPONENTS - {name})

    def test_disabled_models_do_not_check_assets_or_dependencies(self):
        from pathnd_qc.qc_tile.artifacts import artifacts
        self.configure_exclusions({"pen_detection", "tile_artifacts", "stain_normalization"})
        self.params["components"] = ["tissue_segmentation", "pen_detection", "tile_artifacts",
                                     "stain_normalization"]
        with patch.object(artifacts, "check_grandqc_repo", side_effect=AssertionError("disabled backend checked")), \
                patch.object(adapter.importlib.util, "find_spec", side_effect=AssertionError("disabled model checked")):
            command = adapter.build_command(self.params)
        self.assertEqual([flag for flag in command if flag.startswith("--run_")],
                         ["--run_tissue_segmentation"])

    def test_config_cannot_turn_an_empty_effective_selection_into_success(self):
        self.configure_exclusions(ALL_COMPONENTS)
        for requested in (None, *({name} for name in sorted(ALL_COMPONENTS))):
            with self.subTest(requested=requested):
                if requested is None:
                    self.params.pop("components", None)
                else:
                    self.params["components"] = list(requested)
                Path("params.json").write_text(json.dumps(self.params))
                with patch.object(adapter.subprocess, "Popen") as start:
                    with self.assertRaisesRegex(ValueError, "nothing to run"):
                        adapter.main("params.json")
                    start.assert_not_called()
                    self.assertFalse(Path("results").exists())

    def test_config_exclusions_do_not_hide_an_enabled_model_failure(self):
        self.configure_exclusions({"tile_artifacts"})
        self.params.update(components=["pen_detection", "tile_artifacts"], pen_weights="missing-pen.pt")
        Path("params.json").write_text(json.dumps(self.params))
        with patch.object(adapter.subprocess, "Popen") as start:
            with self.assertRaisesRegex(ValueError, "pen_detection"):
                adapter.main("params.json")
            start.assert_not_called()

    def test_explicit_metadata_replaces_private_defaults(self):
        self.params.update(metadata="my metadata.csv", metadata_key="slide_paths")
        cmd = adapter.build_command(self.params)
        self.assertNotIn("--no_metadata", cmd)
        self.assertEqual(cmd[cmd.index("--metadata") + 1], "my metadata.csv")

    def test_metadata_key_without_metadata_refused(self):
        self.params["metadata_key"] = "slide_paths"
        with self.assertRaises(ValueError):
            adapter.build_command(self.params)

    def test_missing_default_models_are_deferred_to_automatic_pipeline_setup(self):
        self.params["components"] = ["tissue_segmentation", "pen_detection", "tile_selection", "tile_artifacts"]
        command = adapter.build_command(self.params)
        self.assertIn("--run_pen_detection", command)
        self.assertIn("--run_tile_artifacts", command)
        self.assertNotIn("--no_model_download", command)

    def test_download_opt_out_is_forwarded(self):
        self.params["no_model_download"] = True
        self.assertIn("--no_model_download", adapter.build_command(self.params))

    def test_pen_prerequisites_with_downloads_disabled(self):
        self.params.update(no_model_download=True, components=["pen_detection"])
        with self.assertRaises(ValueError):
            adapter.build_command(self.params)

    def test_default_full_pipeline_allows_placeholder_references_with_warning(self):
        self.params.pop("components")
        with present_pen_dependencies(), self.assertLogs(level="WARNING") as logs:
            command = adapter.build_command(self.params)
        self.assertEqual({flag.removeprefix("--run_") for flag in command
                          if flag.startswith("--run_")}, ALL_COMPONENTS)
        self.assertIn("shipped m4.reference defaults", "\n".join(logs.output))
        self.assertIn("placeholders", "\n".join(logs.output))

    def test_normalization_uses_shipped_references_without_config(self):
        self.params["components"] = ["tissue_segmentation", "stain_normalization"]
        result = self.run_synthetic_slide()
        normalized = json.loads(Path("report.json").read_text())["m4"]["stain_norm"]
        self.assertTrue(normalized["normalized"])
        self.assertTrue(normalized["is_placeholder"])
        self.assertEqual(normalized["reference_key"], "Hirano")
        self.assertIn("shipped m4.reference defaults", result.stderr)
        self.assertIn("placeholders", result.stderr)

    def test_normalization_config_overrides_shipped_references(self):
        target = dict(self.config.cfg("m4.reference.Hirano"))
        target.update(reference_slide_id="synthetic-custom-reference", is_placeholder=False)
        config = Path("references.json").resolve()
        config.write_text(json.dumps({"m4": {"reference": {"Hirano": target}}}))
        self.params.update(components=["tissue_segmentation", "stain_normalization"], config_file=str(config))
        result = self.run_synthetic_slide()
        normalized = json.loads(Path("report.json").read_text())["m4"]["stain_norm"]
        self.assertTrue(normalized["normalized"])
        self.assertFalse(normalized["is_placeholder"])
        self.assertEqual(normalized["reference_slide_id"], "synthetic-custom-reference")
        self.assertNotIn("without config_file", result.stderr)

    def test_unselected_normalization_does_not_warn_about_references(self):
        with self.assertNoLogs(level="WARNING"):
            adapter.build_command(self.params)

    def test_companion_file_slide_refused(self):
        Path("slide.mrxs").touch()
        self.params["slide"] = str(Path("slide.mrxs").resolve())
        with self.assertRaises(ValueError):
            adapter.build_command(self.params)

    def test_version_and_source_guards(self):
        for key, value in (("expected_pipeline_version", "0.3.0"),
                           ("expected_source_sha256", "0" * 64)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                adapter.build_command({**self.params, key: value})

    def test_release_archive_preserves_checkout_identity(self):
        from pathnd_qc._provenance import implementation_provenance
        release_spec = importlib.util.spec_from_file_location("release_builder", HERE / "prepare_release.py")
        builder = importlib.util.module_from_spec(release_spec)
        release_spec.loader.exec_module(builder)
        destination = Path("release")
        with contextlib.redirect_stdout(io.StringIO()):
            record = builder.prepare(destination)
        current = implementation_provenance()
        archive = destination / record["source_archive"]
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), record["archive_sha256"])
        wheel = destination / record["wheel"]
        self.assertEqual(hashlib.sha256(wheel.read_bytes()).hexdigest(), record["wheel_sha256"])
        with zipfile.ZipFile(wheel) as distribution:
            for name, checksum in record["package_files_sha256"].items():
                self.assertEqual(hashlib.sha256(distribution.read("pathnd_qc/" + name)).hexdigest(), checksum)
            self.assertIn("pathnd_qc/_build_provenance.json", distribution.namelist())
        source = destination / "source"
        self.assertTrue((source / "deploy/verily/run_workflow.py").is_file())
        self.assertFalse((source / "tests").exists())
        self.assertFalse((source / "external/weights").exists())
        for name in ("pathnd_qc.wdl", "pathnd_qc_bucket.wdl"):
            wdl = (source / "deploy/verily" / name).read_text()
            self.assertIn('String expected_source_sha256 = "' + current["source_sha256"] + '"', wdl)
        result = subprocess.run([sys.executable, "-c", "import json; from pathnd_qc._provenance "
                                 "import implementation_provenance; print(json.dumps(implementation_provenance()))"],
                                cwd=source, env=dict(os.environ, PYTHONPATH=str(source.resolve())),
                                capture_output=True, text=True, check=True)
        built = json.loads(result.stdout)
        for key in ("git_commit", "git_dirty", "source_sha256"):
            self.assertEqual(built[key], current[key])
        self.assertEqual(built["git_source"], "build" if current["git_commit"] else "unavailable")
        with self.assertRaisesRegex(ValueError, "already exists"):
            builder.prepare(destination)
        params = json.loads((destination / "inputs.bucket.example.json").read_text())
        self.assertEqual(params["PathNDQC.source_sha256"], record["archive_sha256"])
        self.assertEqual(params["PathNDQC.expected_source_sha256"], record["source_sha256"])
        # Freeze verification covers edits, additions and deletions in a source copy.
        package = source / "pathnd_qc"
        module = package / "__init__.py"
        original = module.read_bytes()
        module.write_bytes(original + b"\n# changed\n")
        with self.assertRaisesRegex(ValueError, "Frozen package differs"):
            builder.verify_frozen_package(source)
        module.write_bytes(original)
        addition = package / "extra.py"
        addition.write_text("VALUE = 1\n")
        with self.assertRaisesRegex(ValueError, "extra.py"):
            builder.verify_frozen_package(source)
        addition.unlink()
        module.unlink()
        with self.assertRaisesRegex(ValueError, "__init__.py"):
            builder.verify_frozen_package(source)
        module.write_bytes(original)
        builder.verify_frozen_package(source)

    def test_wdls_forward_every_reusable_artifact(self):
        from pathnd_qc.pipeline_spec import ARTIFACTS
        for file in ("pathnd_qc.wdl", "pathnd_qc_bucket.wdl"):
            wdl = (HERE / file).read_text()
            for name in ARTIFACTS:
                self.assertIn(f"File? {name}", wdl)
                self.assertIn(f"{name} = {name}", wdl)
                self.assertIn(f"{name}: {name}", wdl)

    def test_wdls_forward_model_inputs_and_select_both_model_components(self):
        for filename in ("pathnd_qc.wdl", "pathnd_qc_bucket.wdl"):
            wdl = (HERE / filename).read_text()
            for declaration in ("File? pen_weights", 'String grandqc_repo = ""',
                                'String grandqc_python = ""'):
                self.assertIn(declaration, wdl)
            for name in ("pen_weights", "grandqc_repo", "grandqc_python", "no_model_download"):
                self.assertIn(f"{name} = {name}", wdl)
                self.assertIn(f"{name}: {name}", wdl)
            defaults = re.search(r"Array\[String\] components = (\[.*?\])", wdl)
            self.assertTrue({"pen_detection", "tile_artifacts"}.issubset(json.loads(defaults[1])))

    def test_cloud_uri_is_not_mistaken_for_a_localized_file(self):
        for uri in ("gs://bucket/slide.svs", "s3://bucket/slide.svs",
                    "az://container/slide.svs", "abfss://container/slide.svs"):
            request = {**self.params, "slide": None, "slide_uri": uri}
            cmd = adapter.build_command(request)
            self.assertEqual(cmd[cmd.index("--slide") + 1], uri)
        for request in ({**self.params, "slide_uri": "s3://bucket/a.svs"},
                        {**self.params, "slide": None},
                        {**self.params, "slide": None, "slide_uri": "https://example.com/a.svs"}):
            with self.assertRaises(ValueError):
                adapter.build_command(request)

    def test_multiple_metadata_files_preserve_precedence(self):
        self.params.update(metadata="legacy.csv", metadata_files=["one.csv", "two files.csv"],
                           metadata_uris=["s3://bucket/three.csv"], metadata_key="path")
        cmd = adapter.build_command(self.params)
        actual = [cmd[i + 1] for i, flag in enumerate(cmd) if flag == "--metadata"]
        self.assertEqual(actual, ["legacy.csv", "one.csv", "two files.csv", "s3://bucket/three.csv"])
        self.assertNotIn("--no_metadata", cmd)
        with self.assertRaisesRegex(ValueError, "no_metadata"):
            adapter.build_command({**self.params, "no_metadata": True})
        with self.assertRaisesRegex(ValueError, "list"):
            adapter.build_command({**self.params, "metadata_files": "one.csv"})

    def test_artifacts_and_analysis_options_are_forwarded(self):
        from pathnd_qc.pipeline_spec import ARTIFACTS
        self.params.update({name: f"/inputs/{name} file" for name in ARTIFACTS})
        self.params.update(norm_method="reinhard", bank="my bank", grandqc_python="/backend/python",
                           quiet=True, no_save_artifacts=True)
        cmd = adapter.build_command(self.params)
        for name in (*ARTIFACTS, "norm_method", "bank", "grandqc_python"):
            self.assertEqual(cmd[cmd.index("--" + name) + 1], self.params[name])
        self.assertIn("--quiet", cmd)
        self.assertIn("--no_save_artifacts", cmd)

    def test_missing_grandqc_assets_fail_instead_of_silent_gating(self):
        self.params.update(components=["tile_artifacts"], grandqc_repo=str(Path("backend").resolve()))
        root = Path(self.params["grandqc_repo"])
        for name in ("main.py", "wsi_tis_detect.py", "models/qc/GrandQC_MPP1.pth"):
            dest = root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.touch()
        with self.assertRaisesRegex(ValueError, "Tissue_Detection_MPP10"):
            adapter.build_command(self.params)
        dest = root / "models/td/Tissue_Detection_MPP10.pth"
        dest.parent.mkdir(parents=True)
        dest.touch()
        cmd = adapter.build_command(self.params)
        self.assertEqual(cmd[cmd.index("--grandqc_repo") + 1], str(root))
        (root / "models/qc/GrandQC_MPP1.pth").unlink()
        with self.assertRaisesRegex(ValueError, "artifact checkpoint"):
            adapter.build_command(self.params)

    def test_pen_weights_and_dependencies_are_required(self):
        self.params.update(components=["pen_detection"], pen_weights="pen.pt")
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            adapter.build_command(self.params)
        Path("pen.pt").touch()
        with patch.object(adapter.importlib.util, "find_spec", return_value=None):
            with self.assertRaisesRegex(ValueError, "dependencies"):
                adapter.build_command(self.params)
        with present_pen_dependencies():
            self.assertIn("--run_pen_detection", adapter.build_command(self.params))

    def test_missing_selected_backend_stops_before_pipeline_launch(self):
        for component, inputs in (
            ("pen_detection", {"pen_weights": "missing-pen.pt"}),
            ("tile_artifacts", {"grandqc_repo": "missing-grandqc"}),
        ):
            params = {**self.params, "components": [component], **inputs}
            Path("params.json").write_text(json.dumps(params))
            with self.subTest(component=component), patch.object(adapter.subprocess, "Popen") as start:
                with self.assertRaises(ValueError):
                    adapter.main("params.json")
                start.assert_not_called()
                self.assertFalse(Path("results").exists())

    def test_synthetic_run_uses_second_explicit_metadata_file(self):
        Path("first.csv").write_text("slide_paths,stain_type\nelsewhere.tif,GFAP\n")
        Path("second.csv").write_text("slide_paths,stain_type\nslide.tif,Hirano\n")
        self.params.update(stain="", metadata_files=["first.csv", "second.csv"])
        self.run_synthetic_slide()
        report = json.loads(Path("report.json").read_text())
        self.assertEqual(report["provenance"]["stain_type"], "Hirano")
        self.assertIn("second.csv", json.dumps(report["m1"]["ingestion"]["metadata"]))

    def test_fline_example_configuration_reaches_detector(self):
        self.params.update(config_file=str(HERE / "config.folds.example.json"),
                           stain="LFB", components=["tissue_segmentation", "fold_detection"])
        self.run_synthetic_slide()
        folds = json.loads(Path("report.json").read_text())["m2"]["folds"]
        self.assertEqual(folds["method"], "d+fline")
        self.assertIsNone(folds["error"])
        self.assertEqual(folds["fline_params"]["hard_index"], 200)
        self.assertEqual(folds["fline_min_area_px"], round(44800 / folds["fline_params"]["mpp_used"] ** 2))

    def test_saved_identity_includes_backend_details(self):
        self.run_synthetic_slide()
        report = json.loads(Path("report.json").read_text())
        identity = json.loads(Path("provenance.json").read_text())
        for key in ("git_commit", "source_sha256", "external_backends"):
            self.assertEqual(identity[key], report["provenance"].get(key, {}))

    def fake_run(self, *, code=0, complete=True, report=True, require_complete=None):
        if require_complete is not None:
            self.params["require_complete"] = require_complete
        Path("params.json").write_text(json.dumps(self.params))

        def start(*args, **kwargs):
            if report:
                folder = Path("results/slide_run")
                folder.mkdir()
                (folder / "slide_report.json").write_text(json.dumps({
                    "provenance": {"execution": {"complete": complete,
                                             "reasons": ["test finding"]}},
                    "error": {"message": "failed"} if code else None}))
            class Process:
                stdout = io.StringIO("pipeline output\n")
                def wait(self):
                    return code
            return Process()

        with patch.object(adapter.subprocess, "Popen", start), contextlib.redirect_stdout(io.StringIO()):
            result = adapter.main("params.json")
        return result, json.loads(Path("summary.json").read_text())

    def test_incomplete_report_is_visible_without_discarding_outputs(self):
        code, summary = self.fake_run(complete=False)
        self.assertEqual(code, 1)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["pipeline_exit_code"], 0)
        self.assertEqual(summary["workflow_exit_code"], 1)
        self.assertNotIn("trustworthy", summary)
        self.assertEqual(Path("complete.txt").read_text().strip(), "false")
        self.assertFalse(Path("trustworthy.txt").exists())
        with tarfile.open("results.tar.gz") as archive:
            self.assertIn("results/slide_run/slide_report.json", archive.getnames())
            self.assertIn("pipeline.log", archive.getnames())
            self.assertIn("index.html", archive.getnames())

    def test_legacy_false_cannot_allow_incomplete_requested_work(self):
        code, summary = self.fake_run(complete=False, require_complete=False)
        self.assertEqual(code, 1)
        self.assertEqual(summary["pipeline_exit_code"], 0)
        self.assertEqual(summary["workflow_exit_code"], 1)
        self.assertTrue(Path("results.tar.gz").is_file())

    def test_pipeline_failure_is_not_masked(self):
        code, summary = self.fake_run(code=7, complete=False)
        self.assertEqual(code, 7)
        self.assertEqual(summary["error"]["message"], "failed")

    def test_missing_report_is_failure(self):
        code, summary = self.fake_run(report=False)
        self.assertEqual(code, 1)
        self.assertFalse(summary["report_found"])

    def test_real_synthetic_slide(self):
        self.run_synthetic_slide()

    def test_real_synthetic_slide_uses_config_exclusions_before_model_checks(self):
        self.configure_exclusions(ALL_COMPONENTS - {"tissue_segmentation"})
        self.params.pop("components")
        self.params.update(pen_weights="missing-pen.pt", grandqc_repo="missing-grandqc")
        self.run_synthetic_slide()
        report = json.loads(Path("report.json").read_text())
        self.assertEqual(set(report["provenance"]["components_run"]), {"tissue_segmentation"})
        self.assertEqual(set(report["provenance"]["components_disabled_config"]),
                         ALL_COMPONENTS - {"tissue_segmentation"})
        self.assertTrue(report["provenance"]["execution"]["complete"])

    def test_both_wdls_default_to_the_full_pipeline(self):
        self.configure_model_inputs()
        for filename in ("pathnd_qc.wdl", "pathnd_qc_bucket.wdl"):
            with self.subTest(workflow=filename):
                wdl = (HERE / filename).read_text()
                match = re.search(r'Array\[String\] components = (\[.*\])', wdl)
                self.assertIsNotNone(match)
                self.params["components"] = json.loads(match.group(1))
                self.assertEqual(set(self.params["components"]), ALL_COMPONENTS)
                self.assertNotIn("require_complete", wdl)
                with present_pen_dependencies():
                    command = adapter.build_command(self.params)
                self.assertEqual({flag.removeprefix("--run_") for flag in command
                                  if flag.startswith("--run_")}, ALL_COMPONENTS)

    def run_synthetic_slide(self):
        # The shared fixture has smooth glass and Beer-Lambert tissue, rather than random RGB.
        sys.path.insert(0, str(SRC / "tests"))
        from fake_slide import FakeSlide, write_tiff
        write_tiff(FakeSlide(base_wh=(2048, 1536), seed=42), "slide.tif")
        Path("params.json").write_text(json.dumps(self.params))
        result = subprocess.run([sys.executable, str(HERE / "run_workflow.py"), "--request", "params.json"],
                                env=dict(os.environ, PYTHONPATH=str(SRC),
                                         PYTHONDONTWRITEBYTECODE="1"),
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(Path("report.json").read_text())
        summary = json.loads(Path("summary.json").read_text())
        self.assertEqual(summary["pipeline_exit_code"], 0)
        self.assertEqual(summary["complete"], report["provenance"]["execution"]["complete"])
        self.assertIn("tissue_segmentation", report["provenance"]["components_run"])
        self.assertTrue(Path("results.tar.gz").is_file())
        with tarfile.open("results.tar.gz") as archive:
            names = archive.getnames()
            self.assertIn("index.html", names)
            nested_reports = [name for name in names if name.startswith("results/slide_output/")
                              and name.endswith("_report.json")]
            self.assertEqual(len(nested_reports), 1)
            run_dir = Path(nested_reports[0]).parent
            self.assertIn(str(run_dir / "index.html"), names)
            self.assertIn(str(run_dir / "images" / "slide_tissue_mask.png"), names)
            # Links in a downloaded archive remain valid without the original task directory.
            archive.extractall("downloaded", filter="data")
        from urllib.parse import unquote
        for page in Path("downloaded").rglob("index.html"):
            for href in re.findall(r'href="([^"]+)"', page.read_text()):
                self.assertFalse(href.startswith("/"), href)
                self.assertTrue((page.parent / unquote(href)).is_file(), (page, href))
        return result


if __name__ == "__main__":
    unittest.main(verbosity=2)
