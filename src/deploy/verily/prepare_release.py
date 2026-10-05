"""Build a provenance-stamped source archive and Docker context for the current checkout.

Run from any directory: python /path/to/src/deploy/verily/prepare_release.py --out /new/release
No cloud writes, model downloads, image build or package-index publication occur.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

PROJECT = Path(__file__).resolve().parents[2]


def verify_frozen_package(project: Path = PROJECT) -> dict:
    """Reject added, missing or edited package files before preparing a release."""
    record = json.loads((project / "package-freeze.json").read_text())
    package = project / "pathnd_qc"
    files = {}
    for path in sorted(package.rglob("*")):
        relative = path.relative_to(package)
        if not path.is_file() or {"__pycache__", "tests"}.intersection(relative.parts):
            continue
        if (path.suffix in {".py", ".patch", ".png"}
                or path.name in {"README.md", "MIGRATION.md", "defaults.json", "catalog.json"}):
            files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = record["files_sha256"]
    changed = sorted(name for name in files.keys() | expected.keys() if files.get(name) != expected.get(name))
    if changed:
        raise ValueError("Frozen package differs: " + ", ".join(changed))
    version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
    if version != record["pipeline_version"]:
        raise ValueError("Package version differs from package-freeze.json")
    return record


def prepare(out_dir: Path, project: Path = PROJECT) -> dict:
    project = project.resolve()
    freeze = verify_frozen_package(project)
    version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
    out_dir = out_dir.resolve()
    if out_dir.exists():
        raise ValueError(f"Release directory already exists: {out_dir}; choose a fresh --out")
    spec = importlib.util.spec_from_file_location("pathnd_release_identity", project / "pathnd_qc/_provenance.py")
    identity_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(identity_module)
    identity = identity_module.implementation_provenance(project / "pathnd_qc")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    # Stage beside the destination so successful publication is one rename.
    with tempfile.TemporaryDirectory(prefix=".pathnd-release-", dir=out_dir.parent) as temporary:
        work = Path(temporary)
        staging = work / "project"
        staging.mkdir()
        for name in ("pyproject.toml", "MANIFEST.in", "build_hooks.py", "README.md", "compose.yaml",
                     "requirements.txt", "package-freeze.json", "USAGE_CLI.md", "USAGE_LIBRARY.md",
                     "USAGE_VERILY.md", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
            shutil.copy2(project / name, staging / name)
        for name in ("pathnd_qc", "deploy"):
            shutil.copytree(project / name, staging / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "dist", "build",
                                                          "deploy.env"))
        if identity["git_commit"] is not None:
            (staging / "pathnd_qc" / identity_module.BUILD_RECORD).write_text(
                json.dumps({"format": 1, **identity}, indent=2) + "\n")
        # Require the image and archive to match the prepared source identity, not just its version label.
        for name in ("pathnd_qc.wdl", "pathnd_qc_bucket.wdl"):
            wdl = staging / "deploy/verily" / name
            content = wdl.read_text()
            content, count = re.subn(r'(?m)^(    String expected_source_sha256 = )"[^"]*"$',
                                     lambda match: match[1] + json.dumps(identity["source_sha256"]),
                                     content, count=1)
            if count != 1 or not identity["source_sha256"]:
                raise ValueError(f"Cannot pin pipeline source identity in {name}")
            wdl.write_text(content)
        release = work / "release"
        release.mkdir()
        env = dict(os.environ, PYTHONPATH=str(staging), PYTHONDONTWRITEBYTECODE="1")
        # Source build only: setuptools must already be installed; no network/build isolation.
        subprocess.run([sys.executable, "-c", "from setuptools.build_meta import build_sdist; "
                        "import sys; build_sdist(sys.argv[1])", str(release)],
                       cwd=staging, env=env, check=True)
        archives = list(release.glob("*.tar.gz"))
        if len(archives) != 1:
            raise ValueError("Expected one source archive")
        archive_path = archives[0]
        with archive_path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        unpack = work / "unpack"
        unpack.mkdir()
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(unpack, filter="data")
        roots = list(unpack.iterdir())
        if len(roots) != 1 or not (roots[0] / "pyproject.toml").is_file():
            raise ValueError("Source archive has no unique project root")
        shutil.move(str(roots[0]), release / "source")
        source = release / "source"
        verify_frozen_package(source)
        # Build the installable wheel from the same archive used by the bucket workflow.
        subprocess.run([sys.executable, "-c", "from setuptools.build_meta import build_wheel; "
                        "import sys; build_wheel(sys.argv[1])", str(release)],
                       cwd=source, env=dict(env, PYTHONPATH=str(source)), check=True)
        wheels = list(release.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("Expected one wheel")
        wheel = wheels[0]
        with zipfile.ZipFile(wheel) as archive:
            for name in ("USAGE_CLI.md", "USAGE_LIBRARY.md"):
                if archive.read(name) != (source / name).read_bytes():
                    raise ValueError(f"Wheel guide differs from source: {name}")
            packaged = {name.removeprefix("pathnd_qc/"): hashlib.sha256(archive.read(name)).hexdigest()
                        for name in archive.namelist()
                        if name.startswith("pathnd_qc/") and not name.endswith("/" + identity_module.BUILD_RECORD)}
        if packaged != freeze["files_sha256"]:
            raise ValueError("Wheel contents differ from the frozen package")
        wheel_checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
        shutil.rmtree(source / "build", ignore_errors=True)
        for egg_info in source.glob("*.egg-info"):
            shutil.rmtree(egg_info)
        record = {"pipeline_version": version, **identity, "source_archive": archive_path.name,
                  "archive_sha256": checksum,
                  "wheel": wheel.name, "wheel_sha256": wheel_checksum,
                  "package_files_sha256": freeze["files_sha256"],
                  "workflow_files_sha256": {
                      name: hashlib.sha256((release / "source/deploy/verily" / name).read_bytes()).hexdigest()
                      for name in ("pathnd_qc.wdl", "pathnd_qc_bucket.wdl", "run_workflow.py", "Dockerfile")}}
        (release / "release.json").write_text(json.dumps(record, indent=2) + "\n")
        (release / (archive_path.name + ".sha256")).write_text(checksum + "  " + archive_path.name + "\n")
        (release / (wheel.name + ".sha256")).write_text(wheel_checksum + "  " + wheel.name + "\n")
        for name in ("inputs.example.json", "inputs.models.example.json", "inputs.models-check.example.json", "inputs.bucket.example.json",
                     "request.compose.example.json"):
            params = json.loads((source / "deploy/verily" / name).read_text())
            prefix = "" if name.startswith("request.") else "PathNDQC."
            params[prefix + "expected_source_sha256"] = identity["source_sha256"]
            if name == "inputs.bucket.example.json":
                params[prefix + "source_sha256"] = checksum
            (release / name).write_text(json.dumps(params, indent=2) + "\n")
        release.rename(out_dir)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--out", type=Path, help="new destination directory")
    action.add_argument("--check", action="store_true", help="verify the frozen package without building")
    args = parser.parse_args()
    try:
        if args.check:
            freeze = verify_frozen_package()
            print(f"Frozen package verified: {len(freeze['files_sha256'])} files, version {freeze['pipeline_version']}")
        else:
            print(json.dumps(prepare(args.out), indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Release preparation failed: {exc}\n")
