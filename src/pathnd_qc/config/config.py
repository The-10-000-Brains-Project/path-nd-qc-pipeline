"""Load and expose configuration from defaults.json and override layers.

Modules use cfg() at import time to bind algorithm constants and keyword defaults.
Explicit function arguments override those defaults. Invalid override layers are
rejected as a whole, preserving earlier valid layers and recording config_error().

Precedence is defaults, managed backend settings, PATHND_CONFIG, then load(path=...).
Configuration is cached; load(force=True) or load(path=...) reloads it without
rebinding defaults in modules that have already been imported.
resolved_sha256() hashes the merged mapping for report provenance.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pathnd_qc import REPORT_VERSION, __version__
from pathnd_qc._provenance import implementation_provenance
from pathnd_qc._logging import get_logger
from .validation import validate
logger = get_logger(__name__)

DEFAULTS_PATH = Path(__file__).resolve().parent / "defaults.json"
ENV_VAR = "PATHND_CONFIG"

_STATE: dict = {"resolved": None, "sources": [], "error": None}


def error_kind(error, explicit=None):
    """Return a component error kind from its explicit type or ClassName: prefix.

    Skipped reasons return "skipped", other failures return "failed", and no error returns None.
    """
    if error in (None, ""):
        return None
    if explicit:
        return str(explicit)
    text = str(error)
    if text.startswith("skipped"):
        return "skipped"
    m = re.match(r"^([A-Z][A-Za-z0-9_]*(?:Error|Exception|Interrupt|Exit|Warning)):", text)
    return m.group(1) if m else "failed"


def now_stamps() -> tuple[str, str]:
    """Return UTC and local ISO-8601 timestamps for the same instant, both with explicit offsets."""
    utc = datetime.now(timezone.utc)
    return (utc.isoformat(timespec="microseconds"), utc.astimezone().isoformat(timespec="microseconds"))


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge `over` into a copy of `base` (dicts merge, scalars/lists replace)."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_json(path) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"config root must be an object, got {type(data).__name__}")
        return data
    except Exception as exc:  # noqa: BLE001 - a broken config must degrade, never raise
        logger.warning("Could not read config %s: %s", path, exc)
        return None


def load(path: Optional[str] = None, force: bool = False) -> dict:
    """Resolve defaults -> managed backend settings -> $PATHND_CONFIG -> `path`, then cache."""
    if _STATE["resolved"] is not None and not force and path is None:
        return _STATE["resolved"]

    sources: list[str] = []
    resolved: dict = {}
    errors = []

    base = _read_json(DEFAULTS_PATH)
    if base is None:
        # Every caller passes a hardcoded fallback, so an unusable defaults file is survivable.
        errors.append(f"defaults.json unreadable at {DEFAULTS_PATH}")
    else:
        resolved = base
        sources.append(str(DEFAULTS_PATH))

    def merge_layer(over, source):
        nonlocal resolved
        try:
            if base is None:
                raise ValueError("cannot validate overrides without readable defaults.json")
            candidate = _deep_merge(resolved, over)
            validate(over, base)
            validate(candidate, base)
        except (TypeError, ValueError) as exc:
            message = f"config override rejected ({source}): {exc}; previous settings retained"
            errors.append(message)
            logger.warning(message)
        else:
            resolved = candidate
            sources.append(str(source))

    from pathnd_qc.external.paths import settings_path
    managed = settings_path()
    if managed.is_file():
        registered = _read_json(managed)
        if registered is not None and isinstance(registered.get("config"), dict):
            merge_layer(registered["config"], managed)
        else:
            errors.append(f"managed settings unreadable or invalid: {managed}")

    for candidate in (os.environ.get(ENV_VAR), path):
        if not candidate:
            continue
        over = _read_json(candidate)
        if over is None:
            errors.append(f"override unreadable: {candidate}")
            continue
        merge_layer(over, candidate)

    _STATE.update(resolved=resolved, sources=sources, error="; ".join(errors) or None)
    return resolved


def resolve_out_dir(out_dir=None) -> Path:
    """Explicit path wins; relative output paths resolve against the caller's working directory."""
    return Path(out_dir or cfg("report.out_dir", "reports")).expanduser().resolve()


def resolve_path(value: Optional[str]) -> Optional[str]:
    """Resolve configured filesystem paths against the caller's cwd, never an installed package."""
    if not value:
        return None
    path = Path(value).expanduser()
    return str(path.resolve())


def cfg(dotted_key: str, default: Any = None) -> Any:
    """Look up `"m2.folds.opening_radius"`; return `default` if absent (or the config is broken)."""
    node: Any = load()
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return copy.deepcopy(node) if isinstance(node, (dict, list)) else node


def thresholds() -> dict:
    """Return the configured metric bounds; shipped lower and upper bounds are null."""
    return cfg("thresholds", {}) or {}


def sources() -> list[str]:
    """Files that contributed to the resolved config, in application order."""
    load()
    return list(_STATE["sources"])


def config_error() -> Optional[str]:
    """Why the config is degraded, or None. Callers still work — they use their own fallbacks."""
    load()
    return _STATE["error"]


def resolved_sha256() -> str:
    """SHA-256 of the RESOLVED config — canonical JSON, so key order cannot change the digest."""
    blob = json.dumps(load(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def provenance() -> dict:
    """Build report provenance from software identity and the resolved configuration."""
    return {
        **implementation_provenance(),
        "pipeline_version": __version__,
        "report_version": REPORT_VERSION,
        "config_sources": sources(),
        "config_sha256": resolved_sha256(),
        "config_error": config_error(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("sources:", sources())
    print("pipeline_version:", __version__)
    print("m2 target_mpp:", cfg("m2.read.target_mpp"), "| m3 tile:", cfg("m3.read.tile_target_mpp"))
    print("sha256:", resolved_sha256()[:16], "| error:", config_error())
    print("thresholds:", len(thresholds()), "entries, all null:",
          all(v.get("min") is None and v.get("max") is None for v in thresholds().values()))
