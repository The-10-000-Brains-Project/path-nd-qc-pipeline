"""Store batch files and discover runs for resume.

Resume skips a completed run only when its slide path and invocation fingerprint
match. Changed inputs, options, configuration, or code schedule new work, as do
statuses without a fingerprint. Batch files and slide folders share
<out>/batch_<batch_id>/. Reusing an ID resumes that batch; a new ID starts independent
work. A lock protects the batch for the duration of an invocation.
"""
from __future__ import annotations

import json
import hashlib
import os
import sys
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

from filelock import FileLock, Timeout

from pathnd_qc import artifact_store as store
from pathnd_qc import _provenance, output_layout
from pathnd_qc._logging import get_logger

BATCH_LAYOUT_VERSION = 2
log = get_logger(__name__)


def new_batch_id() -> str:
    """Compact UTC date/time (YYYYMMDD_HHMMSS); allocation handles same-second collisions."""
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def new_timestamped_dir(out_dir, prefix: str) -> Path:
    """Atomically reserve a dated directory, adding _2, _3, ... when a name is occupied."""
    root = Path(out_dir).expanduser().resolve()
    stamp = new_batch_id()
    number = 1
    while True:
        name = prefix + stamp + (f"_{number}" if number > 1 else "")
        path = root / name
        try:
            path.mkdir(parents=True, exist_ok=False)
            return path
        except FileExistsError:
            number += 1


def batch_path(out_dir, batch_id: str) -> Path:
    """Resolve a batch's destination without creating it."""
    if (not isinstance(batch_id, str) or not batch_id.strip() or batch_id in {".", ".."}
            or "/" in batch_id or "\\" in batch_id or "\x00" in batch_id):
        raise ValueError("batch_id must be a single non-empty folder name")
    root = Path(out_dir).expanduser().resolve()
    p = root / f"batch_{batch_id}"
    if p.is_symlink() or not p.resolve().is_relative_to(root):
        raise ValueError(f"batch folder must not be a symlink: {p}")
    return p


def batch_dir(out_dir, batch_id: str) -> Path:
    """Create a fresh batch directory; use open_batch_dir for resumable execution."""
    p = batch_path(out_dir, batch_id)
    try:
        p.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError(f"batch folder already exists: {p}; choose a new --batch_id") from exc
    return p


@contextmanager
def open_batch_dir(out_dir, batch_id: str | None = None):
    """Create or resume an owned batch, refusing concurrent writers and unrelated directories."""
    if batch_id is None:
        p = new_timestamped_dir(out_dir, "batch_")
        batch_id = p.name.removeprefix("batch_")
        created = True
    else:
        p = batch_path(out_dir, batch_id)
        try:
            p.mkdir(parents=True, exist_ok=False)
            created = True
        except FileExistsError:
            if not p.is_dir():
                raise ValueError(f"batch output is not a directory: {p}") from None
            created = False
    lock_path = p / ".batch.lock"
    if lock_path.is_symlink():
        raise ValueError(f"batch lock must not be a symlink: {lock_path}")
    lock = FileLock(str(lock_path), timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise ValueError(f"batch is already running: {p}; wait for it to finish or use a new --batch_id") from exc
    try:
        if created:
            write_json(p / "batch.json", {"batch_id": batch_id, "layout_version": BATCH_LAYOUT_VERSION})
        else:
            try:
                record = json.loads((p / "batch.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"cannot resume {p}: no readable batch.json; choose a new --batch_id") from exc
            if (not isinstance(record, dict) or record.get("batch_id") != batch_id
                    or record.get("layout_version") != BATCH_LAYOUT_VERSION):
                raise ValueError(f"cannot resume {p}: not an owned batch folder; choose a new --batch_id")
        yield p
    finally:
        lock.release()


def _run_order(entry: dict) -> tuple:
    """Order by start time, using the folder collision suffix to break timestamp ties."""
    stamp = entry.get("started_at")
    started = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else datetime.min
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    name = Path(entry["run_dir"]).name
    stem, _, suffix = name.rpartition("_")
    collision = int(suffix) if suffix.isdigit() and stem.endswith(("Z", "UTC")) else 0
    return started, collision, name


def read_status(path) -> dict:
    """Read a status object and validate fields used for indexing and summary provenance."""
    with open(path, encoding="utf-8") as handle:
        st = json.load(handle)
    if not isinstance(st, dict):
        raise ValueError("status must be a JSON object")
    if not isinstance(st.get("slide_path"), str) or not st["slide_path"]:
        raise ValueError("status needs a non-empty slide_path")
    if st.get("state") not in {"running", "completed", "failed"}:
        raise ValueError(f"unknown run state: {st.get('state')!r}")
    for field in ("started_at", "finished_at"):
        value = st.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"{field} must be an ISO timestamp")
            datetime.fromisoformat(value.replace("Z", "+00:00"))
    report = st.get("report")
    if report is not None and (not isinstance(report, str) or not report
                               or Path(report).name != report or report in {".", ".."}):
        raise ValueError("status report must name a file inside the run folder")
    return st


def scan_runs(out_dir, *, only_state: str | None = None) -> dict:
    """slide_path -> the LATEST run (by started_at) of that slide: {state, run_dir, started_at, ...}."""
    latest: dict = {}
    root = Path(out_dir)
    if not root.is_dir():
        return latest
    for status_path in (p for folder in output_layout.iter_run_dirs(root) for p in folder.glob("*_status.json")):
        try:
            st = read_status(status_path)
        except (OSError, ValueError):
            log.warning("Ignoring unreadable or invalid run status: %s", status_path, exc_info=True)
            continue
        if only_state is not None and st["state"] != only_state:
            continue
        entry = {"state": st.get("state"), "run_dir": str(status_path.parent),
                 "slide_id": status_path.name[: -len("_status.json")],
                 "started_at": st.get("started_at"), "finished_at": st.get("finished_at"),
                 "report": st.get("report"), "stage": st.get("stage"), "message": st.get("message"),
                 "batch_fingerprint": st.get("batch_fingerprint")}
        prev = latest.get(st["slide_path"])
        if prev is None or _run_order(entry) > _run_order(prev):
            latest[st["slide_path"]] = entry
    return latest


def latest_run(out_dir, slide: str, state: str = "completed") -> Path | None:
    """The newest run folder of `slide` in the given state, or None."""
    entry = scan_runs(out_dir, only_state=state).get(slide)
    return Path(entry["run_dir"]) if entry else None


def write_json(path, obj) -> None:
    store.save_json(obj, path)


def git_provenance() -> dict:
    """Use the same checkout/build identity as individual slide reports."""
    module = Path(__file__).resolve()
    identity = _provenance.implementation_provenance(module.parents[1], anchor=module)
    return {"commit": identity["git_commit"], "dirty": identity["git_dirty"]}


def git_commit() -> str | None:
    return git_provenance()["commit"]


def source_sha() -> str:
    return _provenance.source_sha(Path(__file__).resolve().parents[1])


def _input_stamp(path):
    path = str(path)
    if "://" in path:
        return {"path": path}
    resolved = Path(path).expanduser().resolve()
    try:
        stat = resolved.stat()
        return {"path": str(resolved), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return {"path": str(resolved), "missing": True}


def shared_input_stamps(run_args: list[str]) -> dict:
    """Stamp configured resources as well as explicit options; never read remote content."""
    from pathnd_qc.config.config import cfg
    flags = {arg.split("=", 1)[0] for arg in run_args}
    inputs = {}
    if "--pen_weights" not in flags and cfg("m2.pen.weights_path"):
        inputs["configured_pen_weights"] = _input_stamp(cfg("m2.pen.weights_path"))
    if "--grandqc_python" not in flags and cfg("m3.artifacts.python"):
        executable = cfg("m3.artifacts.python")
        inputs["configured_grandqc_python"] = _input_stamp(shutil.which(executable) or executable)
    repo = cfg("m3.artifacts.repo_path")
    for index, arg in enumerate(run_args):
        flag, sep, value = arg.partition("=")
        if flag == "--grandqc_repo":
            repo = value if sep else run_args[index + 1]
    if repo:
        inputs["grandqc_repo"] = _input_stamp(repo)
        root = Path(repo).expanduser()
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".py", ".pth", ".pt", ".json", ".yaml", ".yml"}:
                    inputs[f"grandqc/{path.relative_to(root)}"] = _input_stamp(path)
    return inputs


def run_fingerprint(job, run_args: list[str], *, source_digest: str | None = None,
                    shared_inputs: dict | None = None, python: str | None = None) -> str:
    """Conservative resume identity; changed input, options, config or code means new work."""
    files = {"slide": _input_stamp(job.slide),
             **{name: _input_stamp(path) for name, path in sorted(job.supplied.items())}}
    path_flags = {"--metadata", "--pen_weights", "--grandqc_repo", "--grandqc_python",
                  "--tissue_mask", "--fold_mask", "--pen_mask", "--thumbnail", "--tile_list"}
    for index, arg in enumerate(run_args):
        flag, sep, value = arg.partition("=")
        if flag in path_flags:
            value = value if sep else run_args[index + 1]
            files[f"option_{index}"] = _input_stamp(value)
    payload = {"schema": 1, "inputs": files, "stain": job.stain, "bank": job.bank,
               "output_subdir": str(job.output_subdir),
               "args": run_args, "config": config_sha(),
               "source": source_digest if source_digest is not None else source_sha(),
               "shared_inputs": shared_input_stamps(run_args) if shared_inputs is None else shared_inputs,
               "python": _input_stamp(shutil.which(python or sys.executable) or python or sys.executable)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def config_sha() -> str | None:
    try:
        from pathnd_qc.config.config import resolved_sha256
        return resolved_sha256()
    except Exception as exc:  # noqa: BLE001 -- provenance must not abort bookkeeping
        log.warning("Cannot collect resolved configuration hash: %s", exc)
        return None


def cpu_count() -> int:
    """Available CPU budget, respecting process affinity and a cgroup v2 quota."""
    count = (getattr(os, "process_cpu_count", os.cpu_count)() or 1)
    if hasattr(os, "sched_getaffinity"):
        try:
            count = min(count, len(os.sched_getaffinity(0)))
        except OSError:
            pass
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            count = min(count, max(1, int(quota) // int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    return max(1, count)
