"""Implementation identity shared by reports, batch bookkeeping and package builds.

This module uses only the standard library so the build backend can load it without installing
scientific dependencies. Identity follows the implementation location, never the caller's cwd.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess

log = logging.getLogger(__name__)
BUILD_RECORD = "_build_provenance.json"


def git_provenance(package_dir=None, *, anchor=None) -> dict:
    """Return the owned checkout's HEAD and package/build-file dirty state, or unknown values."""
    package = Path(package_dir or Path(__file__).resolve().parent).resolve()
    anchor = Path(anchor or package / "__init__.py").resolve()
    empty = {"commit": None, "dirty": None}
    # Explicit Git environment overrides must not redirect discovery into the caller's repo.
    env = {k: v for k, v in os.environ.items() if k not in {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }}
    workdir = package

    def git(*args):
        return subprocess.run(["git", "--no-optional-locks", *args], cwd=workdir, env=env,
                              capture_output=True, text=True, timeout=5)

    try:
        found = git("rev-parse", "--show-toplevel")
        if found.returncode:
            return empty
        root = Path(found.stdout.strip()).resolve()
        workdir = root
        relative = anchor.relative_to(root)
        if git("ls-files", "--error-unmatch", "--", relative.as_posix()).returncode:
            return empty
        head = git("rev-parse", "HEAD")
        commit = head.stdout.strip()
        if head.returncode or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            return empty
        paths = [package.relative_to(root).as_posix()]
        for name in ("pyproject.toml", "MANIFEST.in", "build_hooks.py"):
            candidate = package.parent / name
            if candidate.is_file():
                paths.append(candidate.relative_to(root).as_posix())
        status = git("status", "--porcelain", "--untracked-files=normal", "--", *paths)
        return {"commit": commit, "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning("Cannot collect pipeline Git provenance: %s", exc)
        return empty


def source_sha(package_dir=None) -> str:
    """Fingerprint installed Python and declared configuration, excluding generated outputs."""
    root = Path(package_dir or Path(__file__).resolve().parent).resolve()
    digest = hashlib.sha256()
    paths = list(root.rglob("*.py")) + [root / "config/defaults.json", root / "external/catalog.json"]
    for path in sorted(paths):
        relative = path.relative_to(root)
        if path.is_file() and not {"__pycache__", "tests"}.intersection(relative.parts):
            digest.update(relative.as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def implementation_provenance(package_dir=None, *, anchor=None) -> dict:
    """Snapshot code identity; prefer an owned checkout, then validated build metadata.

    A build record preserves identity through a wheel or source archive. Its source digest also
    detects later edits to an installed package. Missing Git/metadata remains explicitly unknown.
    """
    package = Path(package_dir or Path(__file__).resolve().parent).resolve()
    git = git_provenance(package, anchor=anchor)
    origin = "checkout" if git["commit"] else "unavailable"
    try:
        digest = source_sha(package)
    except OSError as exc:
        log.warning("Cannot fingerprint pipeline source: %s", exc)
        digest = None
    record = package / BUILD_RECORD
    if git["commit"] is None and record.is_file():
        try:
            saved = json.loads(record.read_text(encoding="utf-8"))
            if (not isinstance(saved, dict) or saved.get("format") != 1
                    or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(saved.get("git_commit")))
                    or saved.get("git_dirty") is not None and type(saved["git_dirty"]) is not bool
                    or not re.fullmatch(r"[0-9a-f]{64}", str(saved.get("source_sha256")))):
                raise ValueError("invalid pipeline build provenance")
            git = {"commit": saved["git_commit"], "dirty": saved.get("git_dirty")}
            if digest is None:
                git["dirty"] = None
            elif digest != saved["source_sha256"]:
                git["dirty"] = True
            origin = "build"
        except (OSError, ValueError) as exc:
            log.warning("Cannot read pipeline build provenance: %s", exc)
    return {"git_commit": git["commit"], "git_dirty": git["dirty"],
            "source_sha256": digest, "git_source": origin}
