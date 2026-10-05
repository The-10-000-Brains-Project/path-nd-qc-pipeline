"""Run each slide in a child Python process, using a thread pool to supervise the children.

Workers stream logs, enforce slide deadlines and record per-job outcomes. Ordinary slide failures
are isolated; failures in the parent, filesystem or host can still interrupt the batch. Child exit
codes are 0 for a completed invocation, 1 for failure and 2 for preflight refusal. Reports are best
effort. Negative child codes can indicate signals; timeout is tracked as a separate outcome.

Progress is refreshed while jobs run and on completion. results.csv/results.json contain one row
per job; Ctrl-C attempts to stop live process groups and retain interruption outcomes.
"""
from __future__ import annotations

import csv
import logging
import json
import hashlib
import math
import platform
import os
import re
import signal
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from pathnd_qc import pipeline_spec as spec
from pathnd_qc._logging import get_logger
from pathnd_qc.batch import manifest, runs, summary
from pathnd_qc import output_layout, result_browser
from pathnd_qc.config.config import config_error, now_stamps
from pathnd_qc import artifact_store as store

log = get_logger(__name__)
REPORT_LINE = re.compile(r"report(?: \(FAILED run; see its `error` block\))? -> (.+)$")
KILL_GRACE_S = 10.0
OUTCOMES = ("completed", "failed", "refused", "timeout", "error", "skipped", "interrupted")


@dataclass
class Outcome:
    key: str
    slide: str
    outcome: str                    # one of OUTCOMES -- the batch's verdict on this job, not the run's lifecycle
    exit_code: int | None = None
    run_dir: str | None = None
    duration_s: float | None = None
    message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


def output_parent(job: manifest.Job, out_dir) -> Path:
    return output_layout.job_output_parent(out_dir, job.output_subdir)


def build_command(job: manifest.Job, out_dir, run_args: list[str],
                  python: str | None = None) -> list[str]:
    """The `pathnd-qc` command line for one job: the batch's flags plus this job's own inputs."""
    cmd = [python or sys.executable, "-P", "-m", "pathnd_qc", "--slide", job.slide,
           "--out", str(output_parent(job, out_dir))]
    # Jobs already resolve manifest values over batch defaults. Remove overridden defaults,
    # including argparse's --flag=value spelling, before appending the resolved values once.
    overrides = {flag for flag, value in (("--stain", job.stain), ("--bank", job.bank)) if value}
    args = iter(run_args)
    for arg in args:
        if arg in overrides:
            next(args, None)
        elif arg.split("=", 1)[0] not in overrides:
            cmd.append(arg)
    if job.stain:
        cmd += ["--stain", job.stain]
    if job.bank:
        cmd += ["--bank", job.bank]
    for name, path in job.supplied.items():
        cmd += [spec.ARTIFACTS[name]["flag"], path]
    return cmd


def validate_run_args(run_args: list[str]) -> None:
    """Refuse invalid shared options once, before creating a batch or any slide process."""
    from pathnd_qc.pipeline import build_parser, m3_artifacts
    parser = build_parser()

    def refuse(message):
        raise ValueError(f"batch run options: {message}")

    parser.error = refuse
    parser.exit = lambda status=0, message=None: refuse(message or "informational flags are not run options")
    args = parser.parse_args(run_args)
    if args.slide or args.out or args.list_components:
        refuse("--slide, --out and --list_components are not forwarded run options")
    if args.no_metadata and (args.metadata or args.metadata_key):
        refuse("--no_metadata cannot be combined with --metadata/--metadata_key")
    if args.metadata_key and not args.metadata:
        refuse("--metadata_key requires --metadata PATH")
    if args.grandqc_python and (
        not Path(args.grandqc_python).is_absolute()
        or not Path(args.grandqc_python).is_file()
        or not os.access(args.grandqc_python, os.X_OK)
    ):
        refuse("--grandqc_python requires an absolute path to an existing executable; "
               "omit it to use the configured/default interpreter")
    selected = {name for name in spec.COMPONENTS if getattr(args, f"run_{name}")}
    aliases = {flag for flag in spec.ALIASES if getattr(args, flag.lstrip("-"))}
    run_set = spec.resolve_run_set(selected, aliases)
    if not run_set:
        refuse("nothing to run: enable at least one requested component in configuration")
    paths = [("--metadata", p) for p in (args.metadata or [])]
    if "pen_detection" in run_set:
        paths.append(("--pen_weights", args.pen_weights))
    for flag, path in paths:
        if path and "://" not in path and not Path(path).is_file():
            refuse(f"{flag}: expected a readable file: {path}")
    if "tile_artifacts" in run_set and args.grandqc_repo and not Path(args.grandqc_repo).is_dir():
        refuse(f"--grandqc_repo: expected a directory: {args.grandqc_repo}")
    if args.grandqc_repo and "tile_artifacts" in run_set:
        problem = m3_artifacts.check_grandqc_repo(args.grandqc_repo)
        if problem:
            refuse(f"--grandqc_repo: {problem}")


def prepare_models(run_args: list[str]) -> list[str]:
    """Resolve shared model assets once before starting any timed slide child."""
    from pathnd_qc.pipeline import build_parser
    from pathnd_qc.external.manager import ensure_models
    args = build_parser().parse_args(run_args)
    selected = {name for name in spec.COMPONENTS if getattr(args, f"run_{name}")}
    aliases = {flag for flag in spec.ALIASES if getattr(args, flag.lstrip("-"))}
    components = spec.resolve_run_set(selected, aliases)
    pen, repo, python = ensure_models(
        components, pen_weights=args.pen_weights, grandqc_repo=args.grandqc_repo,
        grandqc_python=args.grandqc_python, download_models=not args.no_model_download)
    prepared = list(run_args)
    for component, flag, path in (("pen_detection", "--pen_weights", pen),
                                  ("tile_artifacts", "--grandqc_repo", repo),
                                  ("tile_artifacts", "--grandqc_python", python)):
        if component in components and path:
            prepared += [flag, str(path)]
    return prepared


def child_environment(workers: int) -> dict:
    """Cap numerical-library threads before child imports; preserve stricter caller limits."""
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    limit = max(1, runs.cpu_count() // workers)
    keys = ("PATHND_CPU_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")
    for key in keys:
        if key in env:
            try:
                requested = int(env[key])
            except ValueError:
                raise ValueError(f"{key} must be a positive integer") from None
            if requested < 1:
                raise ValueError(f"{key} must be a positive integer")
            limit = min(limit, requested)
    env.update({key: str(limit) for key in keys})
    env["NUMEXPR_NUM_THREADS"] = str(limit)
    return env


class _Live:
    """Track running child processes so interruption can terminate them."""

    def __init__(self):
        self._procs: dict = {}
        self._lock = threading.Lock()
        self._stopping = False

    def add(self, key, proc):
        with self._lock:
            if self._stopping:
                return False
            self._procs[key] = proc
            return True

    def remove(self, key):
        with self._lock:
            self._procs.pop(key, None)

    def terminate_all(self):
        with self._lock:
            self._stopping = True
            procs = list(self._procs.values())
        _terminate_many(procs)

    @property
    def stopping(self):
        with self._lock:
            return self._stopping


def _kill(proc: subprocess.Popen) -> None:
    """Terminate a slide tree once, including when timeout and batch cancellation race."""
    lock = getattr(proc, "_pathnd_cleanup_lock", None)
    if lock is None:
        lock = proc._pathnd_cleanup_lock = threading.Lock()
    with lock:
        if getattr(proc, "_pathnd_cleaned", False):
            return
        _terminate_tree(proc)
        proc._pathnd_cleaned = True


def _terminate_many(procs) -> None:
    """Signal every child first and share deadlines; cleanup failure never skips a peer."""
    if not procs:
        return
    for proc in procs:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            elif os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=KILL_GRACE_S, check=False)
            elif proc.poll() is None:
                proc.terminate()
        except (OSError, subprocess.TimeoutExpired) as exc:
            if not isinstance(exc, ProcessLookupError):
                log.warning("Could not terminate child %s: %s", proc.pid, exc)
    deadline = time.monotonic() + KILL_GRACE_S
    for proc in procs:
        try:
            proc.wait(timeout=max(0, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            pass
    for proc in procs:
        try:
            if os.name == "posix":
                # Descendants can survive an exited leader and still hold the output pipe.
                os.killpg(proc.pid, signal.SIGKILL)
            elif proc.poll() is None:
                proc.kill()
        except OSError as exc:
            if not isinstance(exc, ProcessLookupError):
                log.warning("Could not kill child %s: %s", proc.pid, exc)
    deadline = time.monotonic() + KILL_GRACE_S
    for proc in procs:
        try:
            proc.wait(timeout=max(0, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("Could not reap child %s after termination: %s", proc.pid, exc)


def _terminate_tree(proc: subprocess.Popen) -> None:
    _terminate_many([proc])


def _known_run_dir(env: dict, job: manifest.Job) -> str | None:
    """Read the child's early, atomic run-folder handshake; never trust another job's status."""
    marker = env.get("PATHND_BATCH_RUN_STARTED_FILE")
    if not marker:
        return None
    try:
        value = json.loads(Path(marker).read_text(encoding="utf-8"))
        folder = Path(value["run_dir"]).resolve()
        expected_root = Path(env["PATHND_BATCH_OUT_DIR"]).resolve()
        if not output_layout.is_run_dir(expected_root, folder):
            raise ValueError("run folder is outside the batch output directory")
        statuses = list(folder.glob("*_status.json"))
        if len(statuses) != 1:
            raise ValueError("run folder must contain exactly one status")
        status = runs.read_status(statuses[0])
        if status["slide_path"] != job.slide:
            raise ValueError("run folder belongs to another slide")
        fingerprint = env.get("PATHND_BATCH_FINGERPRINT")
        if fingerprint and status.get("batch_fingerprint") != fingerprint:
            raise ValueError("run folder belongs to another invocation")
        return str(folder)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        log.warning("Cannot read run identity for %s: %s", job.key, exc)
        return None


def _reconcile_run(run_dir, job, outcome_state, message, logger):
    """A stopped work unit cannot leave its last durable state as running."""
    if run_dir is None or outcome_state not in {"timeout", "interrupted", "error", "failed"}:
        return
    for path in Path(run_dir).glob("*_status.json"):
        try:
            status = runs.read_status(path)
            if status["slide_path"] != job.slide or status["state"] != "running":
                continue
            status.update(state="failed", finished_at=now_stamps()[0], stage="batch",
                          message=message or outcome_state, batch_outcome=outcome_state)
            runs.write_json(path, status)
        except (OSError, ValueError) as exc:
            logger.warning("[%s] cannot finalize stopped run status: %s", job.key, exc)


def run_one(job: manifest.Job, cmd: list[str], timeout_s: float | None, env: dict,
            live: _Live | None = None, *, logger: logging.Logger | None = None) -> Outcome:
    """Launch, stream, wait, classify. Never raises for anything the child does."""
    started = now_stamps()[0]
    t0 = time.monotonic()
    run_dir: str | None = None
    last_lines: list[str] = []
    logger = logger or log
    try:
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1,
                                start_new_session=os.name == "posix")
    except OSError as exc:
        return Outcome(job.key, job.slide, "error", None, None, 0.0, f"could not start: {exc}",
                       started, now_stamps()[0])
    proc._pathnd_cleanup_lock = threading.Lock()
    if live is not None and not live.add(job.key, proc):
        try:
            _kill(proc)
        finally:
            proc.stdout.close()
        run_dir = _known_run_dir(env, job)
        message = "batch interrupted during launch"
        _reconcile_run(run_dir, job, "interrupted", message, logger)
        return Outcome(job.key, job.slide, "interrupted", proc.returncode, run_dir,
                       round(time.monotonic() - t0, 2), message, started, now_stamps()[0])

    def pump():
        nonlocal run_dir
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if not line:
                continue
            m = REPORT_LINE.search(line)
            if m:
                run_dir = os.path.dirname(m.group(1).strip())
            last_lines.append(line)
            del last_lines[:-5]
            logger.info("[%s] %s", job.key, line)

    pumper = threading.Thread(target=pump, name=f"pump-{job.key}", daemon=True)
    timed_out = False
    try:
        pumper.start()
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        logger.warning("[%s] no exit after %g s: terminating", job.key, timeout_s or 0)
        _kill(proc)
    finally:
        try:
            if os.name == "posix" or proc.poll() is None:
                _kill(proc)                 # reap descendants even if the slide exited first
        finally:
            try:
                if pumper.ident is not None:
                    pumper.join(timeout=KILL_GRACE_S)
                if not pumper.is_alive():
                    proc.stdout.close()
            finally:
                if live is not None:
                    live.remove(job.key)
    code = proc.returncode
    duration = round(time.monotonic() - t0, 2)
    # The cause, for the results row: the child's own `error: ...` line (never its `report ... ->`
    # line, which mentions the `error` block), else the last line it printed.
    error_lines = [l for l in last_lines if not REPORT_LINE.search(l)
                   and (l.lower().startswith("error:") or " error " in l.lower() or "error:" in l.lower())]
    tail = error_lines[-1] if error_lines else (last_lines[-1] if last_lines else None)
    run_dir = _known_run_dir(env, job) or run_dir
    if live is not None and live.stopping:
        st, msg = "interrupted", "batch interrupted while this slide was running"
    elif timed_out:
        st, msg = "timeout", f"killed after {timeout_s:g} s ({tail})" if tail else f"killed after {timeout_s:g} s"
    elif code == 0:
        st, msg = "completed", None
    elif code == 1:
        st, msg = "failed", tail
    elif code == 2:
        st, msg = "refused", tail
    elif code is not None and code < 0:
        st, msg = "error", f"killed by signal {-code} ({signal.Signals(-code).name if -code in signal.Signals._value2member_map_ else '?'})"
    else:
        st, msg = "error", f"exit {code}: {tail}"
    _reconcile_run(run_dir, job, st, msg, logger)
    return Outcome(job.key, job.slide, st, code, run_dir, duration, msg, started, now_stamps()[0])


def _setup_log(log_path: Path, logger: logging.Logger) -> logging.Handler:
    """Attach only this batch's file handler; application console handlers belong to its caller."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return fh


@contextmanager
def _batch_logging(log_path, batch_id):
    logger = get_logger(f"{__name__}.{batch_id}")
    previous_level = logger.level
    handler = _setup_log(log_path, logger)
    logger.setLevel(logging.INFO)
    try:
        yield logger
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)


def run_batch(jobs: list[manifest.Job], out_dir, *, run_args: list[str], workers: int | None = None,
              timeout_s: float | None = 3600.0, download_slots: int = 4, retry_failed: bool = False,
              force: bool = False, batch_id: str | None = None, quiet: bool = False,
              python: str | None = None, argv: list[str] | None = None) -> dict:
    """Run/resume one self-contained batch; return its record, summary counts and file paths."""
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int) or workers < 1):
        raise ValueError("workers must be a positive integer")
    if isinstance(download_slots, bool) or not isinstance(download_slots, int) or download_slots < 0:
        raise ValueError("download_slots must be a non-negative integer")
    if timeout_s is not None and (not math.isfinite(timeout_s) or timeout_s < 0):
        raise ValueError("timeout_s must be finite and non-negative (0 or None means no timeout)")
    timeout_s = timeout_s or None
    if not jobs:
        raise ValueError("nothing to run: no slides matched the manifest or directory filter")
    validate_run_args(run_args)
    if not str(out_dir).strip():
        raise ValueError("--out: expected a nonempty directory path")
    if len({job.key for job in jobs}) != len(jobs):
        raise ValueError("job keys must be unique within the batch")
    out_dir = Path(out_dir).expanduser().resolve()
    if out_dir.exists() and not out_dir.is_dir():
        raise ValueError(f"--out: {out_dir} exists and is not a directory")
    with runs.open_batch_dir(out_dir, batch_id) as bdir:
        batch_id = bdir.name.removeprefix("batch_")
        destinations = [output_parent(job, bdir) / f"{job.slide_id}_output" for job in jobs if not job.problems]
        if len(set(destinations)) != len(destinations):
            raise ValueError("output folder collision: different jobs target the same <slidename>_output folder")
        record = _run_batch(jobs, out_dir, bdir, run_args=run_args, workers=workers,
                            timeout_s=timeout_s, download_slots=download_slots,
                            retry_failed=retry_failed, force=force, batch_id=batch_id,
                            quiet=quiet, python=python, argv=argv)
        table = summary.summarize(bdir)
        record["summary_paths"] = summary.write_summary(table, bdir)
        record["summary_counts"] = table["counts"]
        runs.write_json(bdir / "batch.json", record)
        result_browser.write_batch_index(record)
        return record


def _run_batch(jobs, out_dir, bdir, *, run_args, workers, timeout_s, download_slots,
               retry_failed, force, batch_id, quiet, python, argv):
    """Execute inside an exclusively owned batch directory."""
    source_digest = runs.source_sha()
    git = runs.git_provenance()
    log_path = bdir / "batch.log"
    workers = min(len(jobs), max(1, workers or max(1, runs.cpu_count() - 1)))
    env = child_environment(workers)
    t_start = time.monotonic()
    record = {"batch_id": batch_id, "layout_version": runs.BATCH_LAYOUT_VERSION,
              "started_at": now_stamps()[0], "finished_at": None,
              "out_dir": str(out_dir), "batch_dir": str(bdir), "log": str(log_path),
              "n_jobs": len(jobs), "manifest_sha256": manifest.manifest_hash(jobs),
              "run_args": list(run_args), "workers": workers, "threads_per_worker": int(env["PATHND_CPU_THREADS"]),
              "timeout_s": timeout_s,
              "download_slots": download_slots, "retry_failed": retry_failed, "force": force,
              "argv": list(sys.argv if argv is None else argv),
              "python": sys.version.split()[0] if python is None else None,
              "python_executable": python or sys.executable,
              "git_commit": git["commit"], "git_dirty": git["dirty"], "source_sha256": source_digest,
              # Record configuration errors alongside the hash of the resolved fallback configuration.
              "config_sha256": runs.config_sha(), "config_error": config_error(), "host": platform.node()}
    runs.write_json(bdir / "batch.json", record)
    with _batch_logging(log_path, batch_id) as batch_log:
        batch_log.info("batch %s: %d job(s), %d worker(s), timeout %s s, %d download slot(s) -> %s",
                 batch_id, len(jobs), workers, timeout_s, download_slots, out_dir)

        batch_log.info("CPU threads per worker: %s", env["PATHND_CPU_THREADS"])

        # ---- decide: skip what is done (resume), refuse what cannot launch, launch the rest
        on_disk = {} if force else runs.scan_runs(bdir)
        shared_inputs = runs.shared_input_stamps(run_args)
        fingerprints = {job.key: runs.run_fingerprint(job, run_args, source_digest=source_digest,
                                                       shared_inputs=shared_inputs, python=python)
                        for job in jobs}
        outcomes: list[Outcome] = []
        to_run: list[manifest.Job] = []
        for job in jobs:
            prev = on_disk.get(job.slide)
            if prev and prev.get("batch_fingerprint") != fingerprints[job.key]:
                prev = None
            if job.problems:
                outcomes.append(Outcome(job.key, job.slide, "refused", 2, None, 0.0, "; ".join(job.problems)))
                batch_log.warning("[%s] refused before launch: %s", job.key, "; ".join(job.problems))
            elif prev and prev["state"] == "completed":
                outcomes.append(Outcome(job.key, job.slide, "skipped", None, prev["run_dir"], 0.0,
                                        f"completed at {prev['finished_at']} (pass --force to rerun)"))
            elif prev and prev["state"] == "failed" and not retry_failed:
                outcomes.append(Outcome(job.key, job.slide, "skipped", None, prev["run_dir"], 0.0,
                                        f"failed at {prev['finished_at']} during {prev.get('stage')} "
                                        f"(pass --retry_failed or --force)"))
            else:
                to_run.append(job)
        batch_log.info("%d to run, %d skipped (already done), %d refused before launch",
                 len(to_run), sum(o.outcome == "skipped" for o in outcomes),
                 sum(o.outcome == "refused" for o in outcomes))

        child_args = prepare_models(run_args) if to_run else run_args
        # Supports both a source checkout and the future installed package without changing cwd.
        package_parent = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (package_parent, env.get("PYTHONPATH"))))
        if download_slots and download_slots > 0:
            env["PATHND_LOCALIZE_SLOTS"] = f"{bdir / 'slots'}:{int(download_slots)}"
        else:
            env.pop("PATHND_LOCALIZE_SLOTS", None)
        live = _Live()
        counts = {s: 0 for s in OUTCOMES}
        for o in outcomes:
            counts[o.outcome] += 1
        in_flight: set = set()
        lock = threading.Lock()

        def progress(final: bool = False):
            with lock:
                active = sorted(in_flight)
                current_counts = dict(counts)
            finished = [o for o in outcomes if o.key in launched_keys]
            done = len(outcomes)
            launched_done = sum(o.duration_s is not None and o.outcome != "interrupted" for o in finished)
            elapsed = time.monotonic() - t_start
            rate = launched_done / elapsed * 60 if elapsed > 0 and launched_done else 0.0
            pending = max(0, len(to_run) - len(finished) - len(active))
            eta = (pending + len(active)) / rate * 60 if rate else None
            runs.write_json(bdir / "progress.json", {
                "batch_id": batch_id, "final": final, "total": len(jobs), "done": done,
                **current_counts, "in_flight": active, "pending": pending,
                "elapsed_s": round(elapsed, 1), "rate_per_min": round(rate, 3),
                "eta_s": round(eta, 1) if eta is not None else None, "updated_at": now_stamps()[0]})

        def work(job: manifest.Job) -> Outcome:
            with lock:
                in_flight.add(job.key)
            started = now_stamps()[0]
            t0 = time.monotonic()
            try:
                cmd = build_command(job, bdir, child_args, python=python)
                batch_log.info("[%s] start: %s", job.key, shlex.join(cmd[4:]))
                job_token = hashlib.sha256(job.key.encode()).hexdigest()
                marker = bdir / f"run-{job_token}.json"
                marker.unlink(missing_ok=True)  # Clear the marker so each launch reports only its own run folder.
                job_env = dict(env, PATHND_BATCH_RUN_STARTED_FILE=str(marker),
                               PATHND_BATCH_OUT_DIR=str(bdir),
                               PATHND_BATCH_FINGERPRINT=fingerprints[job.key])
                return run_one(job, cmd, timeout_s, job_env, live, logger=batch_log)
            except Exception as exc:  # noqa: BLE001 - an infrastructure failure belongs to this job
                batch_log.exception("[%s] worker failed", job.key)
                return Outcome(job.key, job.slide, "error", None, None,
                               round(time.monotonic() - t0, 2), f"{type(exc).__name__}: {exc}",
                               started, now_stamps()[0])
            finally:
                with lock:
                    in_flight.discard(job.key)

        launched_keys = {job.key for job in to_run}
        progress()
        interrupted = False
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch") as pool:
                futures = {}
                try:
                    for job in to_run:
                        futures[pool.submit(work, job)] = job
                    pending_futures = set(futures)
                    while pending_futures:
                        ready, pending_futures = wait(pending_futures, timeout=1.0,
                                                       return_when=FIRST_COMPLETED)
                        for fut in ready:
                            o = fut.result()
                            with lock:
                                outcomes.append(o)
                                counts[o.outcome] += 1
                            (batch_log.info if o.outcome == "completed" else batch_log.warning)(
                                "[%s] %s in %.1f s%s", o.key, o.outcome.upper(), o.duration_s or 0.0,
                                f": {o.message}" if o.message else "")
                        progress()
                except KeyboardInterrupt:
                    interrupted = True
                    batch_log.warning("interrupted: terminating %d live run(s) and cancelling the rest", len(in_flight))
                    for fut in futures:
                        fut.cancel()
                    live.terminate_all()
                    raise
                except BaseException:
                    for fut in futures:
                        fut.cancel()
                    live.terminate_all()
                    raise
        except KeyboardInterrupt:
            pass
        finally:
            if interrupted:
                done_keys = {o.key for o in outcomes}
                for job in to_run:
                    if job.key not in done_keys:
                        future = next((f for f, j in futures.items() if j.key == job.key), None)
                        if future is not None and future.done() and not future.cancelled():
                            outcome = future.result()
                        else:
                            outcome = Outcome(job.key, job.slide, "interrupted", None, None, None,
                                              "batch interrupted before this slide started")
                        outcomes.append(outcome)
                        counts[outcome.outcome] += 1
                        batch_log.warning("[%s] %s%s", outcome.key, outcome.outcome.upper(),
                                          f": {outcome.message}" if outcome.message else "")
            record["finished_at"] = now_stamps()[0]
            record["elapsed_s"] = round(time.monotonic() - t_start, 1)
            record["counts"] = dict(counts)
            record["interrupted"] = interrupted
            rows = [asdict(o) for o in sorted(outcomes, key=lambda o: o.key)]
            runs.write_json(bdir / "results.json", rows)
            with store.atomic_write(bdir / "results.csv") as tmp:
                with open(tmp, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=list(Outcome.__dataclass_fields__))
                    w.writeheader()
                    w.writerows(rows)
            runs.write_json(bdir / "batch.json", record)
            progress(final=True)
            result_browser.write_batch_index(record, rows)
            batch_log.info("batch %s finished in %.1f s: %s", batch_id, record["elapsed_s"],
                     ", ".join(f"{k} {v}" for k, v in counts.items() if v))
        return record
