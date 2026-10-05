"""Smoke test — batch paths, resume, summaries and subprocess lifecycle.

No network, models or real slides. Tiny Python children exercise real process boundaries;
manifest and report fixtures exercise the documented one-outcome-per-job contract.
Every temporary file lives inside src/tests and is removed on completion.
Run: PYTHONDONTWRITEBYTECODE=1 python src/tests/smoke_batch_contract.py
"""
from __future__ import annotations

import csv
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from pathnd_qc.batch import manifest, runner, runs, summary                    # noqa: E402

PASS, FAIL = [], []
ROOT_LOGGER_STATE = (logging.getLogger().level, tuple(logging.getLogger().handlers),
                     tuple(logging.getLogger().filters))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + str(detail) if detail else ''}")


def raises(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 -- observe the promised public refusal
        return exc
    return None


def dump(path, obj):
    Path(path).write_text(json.dumps(obj), encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def child_command(script):
    """Keep the real resolved command arguments, replacing only the package entry point."""
    original = runner.build_command

    def build(*args, **kwargs):
        cmd = original(*args, **kwargs)
        cmd[2:4] = [str(script)]
        return cmd

    return build


def job(key="A"):
    return manifest.Job(str(out / f"{key}.svs"), key, key)


def status(root, folder, slide, report=None):
    dest = root / folder
    dest.mkdir(parents=True)
    dump(dest / "A_status.json", {
        "slide_path": slide, "state": "completed", "started_at": "2026-09-10T12:00:00Z",
        "finished_at": "2026-09-10T12:00:01Z", "report": "A_report.json",
    })
    if report is not None:
        dump(dest / "A_report.json", report)
    return dest


def section(name, fn):
    print(f"\n[{name}]")
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 -- retain findings in independent sections
        traceback.print_exc()
        check(f"{name} completes without an unexpected exception", False,
              f"{type(exc).__name__}: {exc}")


def paths_and_defaults():
    # A batch child resolves input paths relative to the caller.
    caller = out / "caller"
    caller.mkdir()
    (caller / "mask.png").write_bytes(b"fixture")
    (caller / "config.json").write_text("{}", encoding="utf-8")
    probe = caller / "probe.py"
    probe.write_text(
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "def arg(flag): return args[args.index(flag) + 1]\n"
        "dest = pathlib.Path(arg('--out')); dest.mkdir(parents=True, exist_ok=True)\n"
        "(dest / 'observed.json').write_text(json.dumps({\n"
        " 'cwd': os.getcwd(), 'mask': pathlib.Path(arg('--tissue_mask')).read_text(),\n"
        " 'config': pathlib.Path(os.environ['PATHND_CONFIG']).read_text()}))\n",
        encoding="utf-8")
    original = Path.cwd()
    try:
        os.chdir(caller)
        j = manifest.build_jobs([{"slide": str(out / "A.svs")}],
                                templates={"tissue_mask": "mask.png"})[0]
        check("relative artifact templates resolve to the caller's file",
              j.supplied.get("tissue_mask") == str(caller / "mask.png"), j.supplied)
        with patch.object(runner, "build_command", child_command(probe)):
            cmd = runner.build_command(j, "results", [])
        result = runner.run_one(j, cmd, 5, dict(os.environ, PATHND_CONFIG="config.json",
                                                PYTHONDONTWRITEBYTECODE="1"))
        observed = read(caller / "results" / j.output_subdir / "observed.json") if result.outcome == "completed" else {}
        check("relative output, artifact and config paths retain caller semantics across the child",
              result.outcome == "completed" and observed == {
                  "cwd": str(caller), "mask": "fixture", "config": "{}"},
              (result, observed))
    finally:
        os.chdir(original)
    j = job()
    j.stain, j.bank = "Hirano", "PART"
    for args in (["--stain", "AT8", "--bank", "SEA-AD"],
                 ["--stain=AT8", "--bank=SEA-AD"]):
        cmd = runner.build_command(j, out, args)
        check(f"manifest stain/bank override global defaults ({args[0]})",
              cmd.count("--stain") == 1 and cmd[cmd.index("--stain") + 1] == "Hirano"
              and cmd.count("--bank") == 1 and cmd[cmd.index("--bank") + 1] == "PART"
              and not any("AT8" in arg or "SEA-AD" in arg for arg in cmd), cmd)


def merging():
    # Compatible duplicate rows merge; conflicts refuse only that slide.
    slides = out / "slides"
    slides.mkdir()
    slide = slides / "A.svs"
    slide.touch()
    mask = out / "mask.png"
    mask.touch()
    csv_path = out / "manifest.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["slide", "stain", "bank", "tissue_mask"])
        writer.writerow([slide, "Hirano", "", mask])
        writer.writerow([slide, "Hirano", "PART", ""])
    rows = manifest.discover(slides_file=str(csv_path), slide_dir=str(slides))
    jobs = manifest.build_jobs(rows)
    check("directory and compatible manifest duplicates produce one fully populated job",
          len(jobs) == 1 and jobs[0].stain == "Hirano" and jobs[0].bank == "PART"
          and jobs[0].supplied.get("tissue_mask") == str(mask) and not jobs[0].problems, jobs)
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow([slide, "AT8", "PART", ""])
        csv.writer(handle).writerow([str(out / "B.svs"), "AT8", "", ""])
    jobs = manifest.build_jobs(manifest.discover(slides_file=str(csv_path), slide_dir=str(slides)))
    check("conflicting duplicate metadata refuses only the conflicting slide",
          len(jobs) == 2 and bool(jobs[0].problems) and not jobs[1].problems, jobs)


def identities_and_history():
    # Reusing an owned batch ID resumes its folder and retains existing slide outputs.
    jobs = [job("identity")]
    jobs[0].problems.append("synthetic refusal; no child process needed")
    root = out / "identities"
    first = runner.run_batch(jobs, root, run_args=[], batch_id="explicit", quiet=True)
    marker = Path(first["batch_dir"]) / "retained-output.txt"
    marker.write_text("existing slide output")
    resumed = runner.run_batch(jobs, root, run_args=[], batch_id="explicit", quiet=True)
    check("reusing an owned batch ID resumes without removing existing outputs",
          resumed["batch_dir"] == first["batch_dir"]
          and marker.read_text() == "existing slide output")
    unrelated = root / "batch_unrelated"
    unrelated.mkdir()
    marker = unrelated / "keep.txt"
    marker.write_text("unrelated data")
    exc = raises(lambda: runner.run_batch(jobs, root, run_args=[], batch_id="unrelated", quiet=True))
    check("unowned batch folders are refused without changing their contents",
          isinstance(exc, ValueError) and marker.read_text() == "unrelated data", exc)
    with patch.object(runs.time, "strftime", return_value="20260910T120000Z"):
        a = runner.run_batch(jobs, root, run_args=[], quiet=True)
        b = runner.run_batch(jobs, root, run_args=[], quiet=True)
    check("automatic batch ID collisions produce different intact batch folders",
          a["batch_dir"] != b["batch_dir"] and Path(a["batch_dir"]).is_dir()
          and Path(b["batch_dir"]).is_dir(), (a["batch_dir"], b["batch_dir"]))
    history = out / "history"
    slide = str(out / "A.svs")
    status(history, "A_20260910T120000Z_2", slide)
    newest = status(history, "A_20260910T120000Z_10", slide)
    broken = history / "broken"
    broken.mkdir()
    dump(broken / "A_status.json", [])
    check("latest lookup ignores non-object statuses and compares numeric collision suffixes",
          runs.latest_run(history, slide) == newest, runs.latest_run(history, slide))
    for name in ("tissue_mask", "fold_mask"):
        (newest / f"A_{name}.png").touch()
    with patch.object(runs, "scan_runs", wraps=runs.scan_runs) as scanned:
        jobs = manifest.build_jobs([{"slide": slide}],
                                   {name: f"{{latest_run}}/{{slide_id}}_{name}.png"
                                    for name in ("tissue_mask", "fold_mask")}, str(history))
    check("multiple latest-run templates share one historical scan",
          scanned.call_count == 1 and not jobs[0].problems, (scanned.call_count, jobs))


def malformed_summary():
    # Batch README: every run is traceable; one bad historical file must not discard other rows.
    root = out / "summary"
    status(root, "good", str(out / "good.svs"), {"provenance": {"execution": {
        "complete": True}}, "timing": {"total_s": 1.0}})
    bad = status(root, "bad", str(out / "bad.svs"))
    (bad / "A_report.json").write_text('{"provenance":', encoding="utf-8")
    status(root, "non_object", str(out / "non_object.svs"), [])
    status(root, "bad_timing", str(out / "bad_timing.svs"), {"timing": {"total_s": "broken"}})
    malformed = root / "malformed"
    malformed.mkdir()
    dump(malformed / "A_status.json", [])
    result = summary.summarize(root)
    rows = {Path(row["run_dir"]).name: row for row in result["rows"]}
    check("malformed reports retain good rows and surface a per-row read_error",
          set(rows) == {"good", "bad", "malformed", "non_object", "bad_timing"}
          and rows["good"]["complete"] is True
          and all(rows[name].get("read_error")
                  for name in ("bad", "malformed", "non_object", "bad_timing")), rows)
    check("summary omits trust verdicts and counts",
          "trustworthy" not in result["counts"] and "untrustworthy" not in result["counts"]
          and all("trustworthy" not in row and "trust_reasons" not in row for row in rows.values()))
    saved = summary.write_summary(result, out / "summary_export")
    with Path(saved["csv"]).open(newline="", encoding="utf-8") as handle:
        csv_rows = list(csv.DictReader(handle))
    check("JSON and CSV summary exports preserve healthy and damaged run rows",
          len(csv_rows) == len(result["rows"])
          and read(saved["json"])["rows"] == result["rows"], saved)


def worker_contract():
    # Batch README: any refusal/crash is an outcome row; all jobs finish bookkeeping.
    refuse = out / "refuse.py"
    refuse.write_text("import sys\nprint('error: refused fixture')\nsys.exit(2)\n", encoding="utf-8")
    root_log = logging.getLogger()
    with patch.object(runner, "build_command", child_command(refuse)):
        record = runner.run_batch([job()], out / "refused", run_args=[], workers=1, quiet=True)
    progress = read(Path(record["batch_dir"]) / "progress.json")
    check("a launched child exiting 2 finishes with refused=1 and pending=0",
          progress["final"] and progress["refused"] == 1 and progress["pending"] == 0
          and not progress["in_flight"], progress)
    check("library batch calls preserve root logger level, handlers and filters",
          ROOT_LOGGER_STATE == (root_log.level, tuple(root_log.handlers), tuple(root_log.filters)))
    original = child_command(refuse)

    def command(j, *args, **kwargs):
        if j.key == "broken":
            raise OSError("injected command-construction failure")
        return original(j, *args, **kwargs)

    with patch.object(runner, "build_command", command):
        record = runner.run_batch([job("broken"), job("healthy")], out / "workers", run_args=[],
                                  workers=2, quiet=True)
    results = read(Path(record["batch_dir"]) / "results.json")
    check("a worker exception retains one outcome for every job and allows its peer to finish",
          {r["key"]: r["outcome"] for r in results} == {"broken": "error", "healthy": "refused"}, results)
    progress = read(Path(record["batch_dir"]) / "progress.json")
    check("worker exceptions leave no jobs pending or in flight at finalization",
          progress["final"] and progress["pending"] == 0 and not progress["in_flight"], progress)


def lifecycle():
    # Library resource ownership: an unwritable progress file must not leak a handler or level.
    logger = logging.getLogger(f"{runner.__name__}.log_failure")
    original_level = logger.level
    logger.setLevel(logging.ERROR)
    before = (logger.level, tuple(logger.handlers))
    original_write = runs.write_json

    def failing_write(path, obj):
        if Path(path).name == "progress.json":
            raise OSError("injected progress write failure")
        return original_write(path, obj)

    try:
        with patch.object(runs, "write_json", failing_write):
            exc = raises(lambda: runner.run_batch([job("log_failure")], out / "log_failure", run_args=[],
                                                  batch_id="log_failure", quiet=True))
        check("a progress write failure restores the batch logger handlers and level",
              isinstance(exc, OSError) and before == (logger.level, tuple(logger.handlers)), exc)
    finally:
        logger.setLevel(original_level)

    # Batch README: progress distinguishes ongoing work before a slide has completed.
    slow = out / "slow.py"
    slow.write_text("import time\ntime.sleep(2.3)\n", encoding="utf-8")
    snapshots = []

    def record_write(path, obj):
        result = original_write(path, obj)
        if Path(path).name == "progress.json":
            snapshots.append(dict(obj))
        return result

    with patch.object(runner, "build_command", child_command(slow)), \
            patch.object(runs, "write_json", record_write):
        runner.run_batch([job("slow")], out / "heartbeat", run_args=[], workers=1,
                         timeout_s=5, quiet=True)
    ongoing = [s for s in snapshots if not s["final"] and s["in_flight"] == ["slow"]
               and s["completed"] == 0 and s["elapsed_s"] >= 1.0]
    check("a slow child produces progress heartbeats before completion", bool(ongoing), snapshots)

    # Cancellation is monotonic: a process created after the stop snapshot cannot be accepted.
    live = runner._Live()
    live.terminate_all()
    check("a process arriving after cancellation is rejected by the live registry",
          live.add("late", object()) is False)


def pump_start_failure():
    # A worker setup failure must not leave an already-launched slide running.
    processes = []
    original = runner.subprocess.Popen

    def capture(*args, **kwargs):
        proc = original(*args, **kwargs)
        processes.append(proc)
        return proc

    live = runner._Live()
    try:
        with patch.object(runner.subprocess, "Popen", capture), \
                patch.object(runner.threading.Thread, "start", side_effect=RuntimeError("no thread")):
            exc = raises(lambda: runner.run_one(job("pump"),
                         [sys.executable, "-c", "import time; time.sleep(60)"],
                         5, dict(os.environ), live))
        check("output thread startup failure reaps the slide and closes its pipe",
              isinstance(exc, RuntimeError) and str(exc) == "no thread"
              and len(processes) == 1 and processes[0].poll() is not None
              and processes[0].stdout.closed)
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()


def descendants():
    # A timed-out slide must not leave descendant processes running.
    if os.name != "posix":
        print("  NOT TESTABLE  POSIX process-group cleanup on this platform")
        return
    pid_path = out / "grandchild.pid"
    heartbeat = out / "descendant_heartbeat.txt"
    child = out / "descendant.py"
    child.write_text(
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True:\n"
        " pathlib.Path(sys.argv[2]).write_text(str(time.monotonic_ns()))\n"
        " time.sleep(0.02)\n", encoding="utf-8")
    parent = out / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]])\n"
        "time.sleep(60)\n", encoding="utf-8")
    try:
        with patch.object(runner, "KILL_GRACE_S", 0.2):
            result = runner.run_one(job("timeout"), [sys.executable, str(parent), str(child),
                                                      str(pid_path), str(heartbeat)],
                                    1.0, dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        time.sleep(0.15)
        initial = heartbeat.read_text() if heartbeat.exists() else None
        time.sleep(0.15)
        stopped = initial is not None and heartbeat.read_text() == initial
        check("timeout stops even a descendant that ignores SIGTERM",
              result.outcome == "timeout" and pid_path.exists() and stopped, result)
    finally:
        if pid_path.exists():
            pid = int(pid_path.read_text())
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


out = Path(tempfile.mkdtemp(prefix="batch_contract_", dir=HERE))
try:
    section("1 caller paths and per-slide overrides", paths_and_defaults)
    section("2 manifest duplicate merge and refusal", merging)
    section("3 batch identity and historical ordering", identities_and_history)
    section("4 malformed historical summary", malformed_summary)
    section("5 worker outcomes and library logging", worker_contract)
    section("6 lifecycle, heartbeat and cancellation", lifecycle)
    section("7 descendant timeout", descendants)
    section("8 output thread startup failure", pump_start_failure)
finally:
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{'=' * 70}\nBATCH CONTRACT SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for n in FAIL:
    print(f"  - {n}")
sys.exit(1 if FAIL else 0)
