"""User-owned storage and reproducibility information for model backends."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from pathnd_qc._fs import sha256_file


def data_dir() -> Path:
    override = os.environ.get("PATHND_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "pathnd-qc"


def settings_path() -> Path:
    return data_dir() / "settings.json"


sha256 = sha256_file            # kept under this name for the manager and setup scripts


def describe_pen(path) -> dict:
    """Return the pen weights path and digest, or an error naming a missing file or directory."""
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        raise IsADirectoryError(f"pen weights path is a directory, not a .pt file: {p}")
    if not p.is_file():
        raise FileNotFoundError(f"pen weights file not found: {p}")
    return {"path": str(p), "sha256": sha256(p)}


def describe_grandqc(path, python=None) -> dict:
    root = Path(path).expanduser().resolve()
    files = sorted(root.glob("*.py")) + sorted(root.glob("models/*/*.pth"))
    hashes = {str(p.relative_to(root)): sha256(p) for p in files if p.is_file()}
    commit, dirty = None, None
    try:
        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                  text=True, timeout=10, check=True).stdout.strip()

        repository = Path(git("rev-parse", "--show-toplevel")).resolve()
        entrypoint = root / "main.py"
        entrypoint.relative_to(repository)
        # An installed backend nested under an unrelated checkout must not inherit its commit.
        git("ls-files", "--error-unmatch", "--", str(entrypoint))
        commit = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--", str(root)))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # Non-Git installations remain identified by the exact file hashes below.
    return {"path": str(root), "python": str(python or sys.executable),
            "commit": commit, "dirty": dirty, "files_sha256": hashes}
