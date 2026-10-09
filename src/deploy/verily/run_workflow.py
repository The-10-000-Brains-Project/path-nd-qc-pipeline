"""Workflow adapter for one localized or cloud slide using the installed pipeline."""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from urllib.parse import quote, urlsplit


CLOUD_SCHEMES = {"gs", "s3", "az", "abfs", "abfss"}


def slide_input(params: dict) -> str:
    """Exactly one engine-localized File or a pipeline-managed cloud object URI."""
    local, uri = params.get("slide"), params.get("slide_uri")
    if bool(local) == bool(uri):
        raise ValueError("Supply exactly one of slide (localized File) or slide_uri")
    if uri:
        parsed = urlsplit(uri)
        if parsed.scheme not in CLOUD_SCHEMES or not parsed.netloc or not parsed.path.strip("/"):
            raise ValueError("slide_uri must name a gs://, s3://, az:// or abfs[s]:// slide object")
        suffix = Path(parsed.path).suffix.lower()
        value = uri
    else:
        path = Path(local)
        if not path.is_file():
            raise ValueError("slide must be a localized file")
        suffix, value = path.suffix.lower(), str(path.resolve())
    if suffix in {".mrxs", ".vms", ".vmu"}:
        raise ValueError("This workflow supports self-contained slide files only; "
                         "multi-file slide formats need companion-file staging")
    return value


def metadata_inputs(params: dict) -> list[str]:
    """Return the single metadata file, localized files, then explicit cloud URIs, in order."""
    paths = [params["metadata"]] if params.get("metadata") else []
    for name in ("metadata_files", "metadata_uris"):
        value = params.get(name, [])
        if not isinstance(value, list) or any(not isinstance(p, str) or not p for p in value):
            raise ValueError(f"{name} must be a list of nonempty paths")
        paths.extend(value)
    if params.get("no_metadata") and (paths or params.get("metadata_key")):
        raise ValueError("no_metadata cannot be combined with metadata paths or metadata_key")
    if params.get("metadata_key") and not paths:
        raise ValueError("metadata_key requires metadata")
    return paths


def build_command(params: dict) -> list[str]:
    # main() sets PATHND_CONFIG before importing the registry or pipeline components.
    from pathnd_qc import __version__
    from pathnd_qc.config import cfg
    from pathnd_qc.pipeline_spec import ARTIFACTS, COMPONENTS, select_run_set
    from pathnd_qc._provenance import implementation_provenance

    expected = params.get("expected_pipeline_version", "0.5.0")
    if expected != __version__:
        raise ValueError(f"Workflow requires pipeline {expected}, installed pipeline is {__version__}")
    expected_source = params.get("expected_source_sha256")
    if expected_source and expected_source != implementation_provenance()["source_sha256"]:
        raise ValueError("Installed pipeline source fingerprint differs from expected_source_sha256")
    selected = params.get("components", list(COMPONENTS))
    if not isinstance(selected, list) or not selected:
        raise ValueError("Select at least one component; an empty selection is not a full run")
    if any(not isinstance(name, str) or name not in COMPONENTS for name in selected):
        raise ValueError("Unknown component in selection")
    enabled = select_run_set(set(selected))
    if not enabled:
        raise ValueError("nothing to run: enable at least one requested component in configuration")
    selected = [name for name in dict.fromkeys(selected) if name in enabled]
    slide = slide_input(params)
    metadata = metadata_inputs(params)
    pen_weights = params.get("pen_weights") or cfg("m2.pen.weights_path", None)
    if "pen_detection" in selected:
        if (params.get("no_model_download") or params.get("pen_weights")) and (not pen_weights or not Path(pen_weights).is_file()):
            raise ValueError("pen_detection requires a pen_weights file or configured checkpoint")
        if any(importlib.util.find_spec(name) is None for name in ("torch", "segmentation_models_pytorch")):
            raise ValueError("pen_detection dependencies are missing; install the complete pathnd-qc package")
    grandqc_repo = params.get("grandqc_repo") or cfg("m3.artifacts.repo_path", None)
    if "tile_artifacts" in selected and (params.get("no_model_download") or params.get("grandqc_repo")):
        from pathnd_qc.qc_tile.artifacts.artifacts import check_grandqc_repo
        problem = check_grandqc_repo(grandqc_repo)
        if problem:
            raise ValueError(f"tile_artifacts requires a configured GrandQC checkout with checkpoints: {problem}")
    if "stain_normalization" in selected and not params.get("config_file"):
        logging.warning('stain_normalization selected without config_file; using shipped m4.reference defaults. These references are placeholders, not calibrated normalization targets.')
    command = [sys.executable, "-P", "-m", "pathnd_qc", "--slide", slide,
               "--out", str(Path("results").resolve())]
    for name in dict.fromkeys(selected):
        command.append(COMPONENTS[name]["flag"])
    for path in metadata:
        command.extend(["--metadata", path])
    if not metadata:
        command.append("--no_metadata")
    for name in ("stain", "metadata_key", "pen_weights", "bank", "norm_method",
                 "grandqc_repo", "grandqc_python"):
        if params.get(name):
            command.extend(["--" + name, params[name]])
    for name, artifact in ARTIFACTS.items():
        if params.get(name):
            command.extend([artifact["flag"], params[name]])
    for name in ("no_save_artifacts", "no_model_download", "quiet"):
        if params.get(name):
            command.append("--" + name)
    return command


def main(params_path: str) -> int:
    params = json.loads(Path(params_path).read_text())
    if not isinstance(params, dict):
        raise ValueError("request must be a JSON object")
    if params.get("s3_anonymous"):
        options = json.loads(os.environ.get("FSSPEC_S3", "{}"))
        options["anon"] = True
        os.environ["FSSPEC_S3"] = json.dumps(options)
    if params.get("azure_anonymous"):
        os.environ["AZURE_STORAGE_ANON"] = "true"
    if params.get("config_file"):
        config_path = Path(params["config_file"]).resolve()
        if not isinstance(json.loads(config_path.read_text()), dict):
            raise ValueError("config_file must contain a JSON object")
        os.environ["PATHND_CONFIG"] = str(config_path)
    else:
        os.environ.pop("PATHND_CONFIG", None)
    from pathnd_qc.config import load
    load(force=True)
    command = build_command(params)
    # Use a fresh output directory for each workflow execution.
    if Path("results").exists():
        raise ValueError("results already exists; run in a fresh directory")
    Path("results").mkdir()
    Path("workflow_inputs.json").write_text(json.dumps(params, indent=2) + "\n")
    with open("pipeline.log", "w") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True)
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            log.flush()
            print(line, end="", flush=True)
        code = process.wait()
    from pathnd_qc.output_layout import iter_report_paths
    from pathnd_qc.result_browser import write_page

    reports = list(iter_report_paths("results"))
    summary = {"pipeline_exit_code": code, "report_found": len(reports) == 1,
               "complete": False, "reasons": []}
    identity = dict.fromkeys(("pipeline_version", "report_version", "git_commit", "git_dirty",
                              "git_source", "source_sha256", "config_sha256"))
    if len(reports) == 1:
        shutil.copyfile(reports[0], "report.json")
        report = json.loads(reports[0].read_text())
        provenance = report.get("provenance", {})
        identity.update({key: provenance.get(key) for key in identity})
        identity["external_backends"] = provenance.get("external_backends", {})
        execution = provenance.get("execution", {})
        summary.update({key: execution[key] for key in ("complete", "reasons")
                        if key in execution})
        summary["error"] = report.get("error")
    else:
        summary["reasons"].append("Expected exactly one pipeline report")
        code = code or 1
    if summary["complete"] is not True:
        code = code or 1
    summary["workflow_exit_code"] = code
    summary["provenance"] = identity
    Path("provenance.json").write_text(json.dumps(identity, indent=2) + "\n")
    Path("summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for name in ("complete",):
        Path(name + ".txt").write_text("true\n" if summary[name] is True else "false\n")
    installed = Path("/opt/pathnd-requirements.txt")
    if installed.is_file():
        shutil.copyfile(installed, "results/environment.txt")
    body = '<p>Open the slide results below. Processing completion is separate from slide quality.</p><ul>'
    if len(reports) == 1:
        entry = reports[0].parent / "index.html"
        target = entry if entry.is_file() else reports[0]
        body += f'<li><a href="{quote(target.as_posix(), safe="/")}">Slide results</a></li>'
    body += '<li><a href="summary.json">Workflow summary (JSON)</a></li>'
    body += '<li><a href="provenance.json">Pipeline version and Git commit (JSON)</a></li>'
    body += '<li><a href="pipeline.log">Pipeline log</a></li></ul>'
    write_page("index.html", "Path-ND workflow results", body)
    with tarfile.open("results.tar.gz", "w:gz") as archive:
        for name in ("index.html", "results", "pipeline.log", "summary.json", "provenance.json", "workflow_inputs.json"):
            archive.add(name, arcname=name)
    print(json.dumps(summary, indent=2), flush=True)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    request = parser.add_mutually_exclusive_group(required=True)
    request.add_argument("params", nargs="?", help="request JSON (legacy positional form)")
    request.add_argument("--request", help="request JSON")
    args = parser.parse_args()
    try:
        raise SystemExit(main(args.request or args.params))
    except (ValueError, KeyError, OSError) as exc:
        print(f"Workflow setup failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
