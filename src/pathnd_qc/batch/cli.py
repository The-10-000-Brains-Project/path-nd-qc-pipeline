"""Run the pipeline in a separate process for each slide, with bounded concurrency.

    pathnd-qc-batch run --slide_dir /data/slides --out out/ --workers 8
    pathnd-qc-batch run --slides manifest.csv --out out/ --run_tissue_segmentation
    pathnd-qc-batch list --slide_dir /data/slides --out out/ --run_tiles
    pathnd-qc-batch summarize --out out/

The run command forwards component flags and analysis options to each invocation.
Artifact options accept templates such as {slide_id}, {slide_dir}, {stem},
{latest_run}, and {latest_artifact}. A manifest can supply per-slide stain, bank,
and artifact paths. See batch/README.md for input, output, and resume contracts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import signal
import shlex
import threading
from contextlib import contextmanager
from pathnd_qc._logging import configure_logging

from pathnd_qc import pipeline_spec as spec                                   # noqa: E402
from pathnd_qc.batch import manifest, runner, runs, summary              # noqa: E402

FORWARDED_FLAGS = (("--stain", "NAME"), ("--metadata", "PATH"), ("--metadata_key", "COL"),
                   ("--bank", "NAME"), ("--norm_method", "M"), ("--pen_weights", "PATH"),
                   ("--grandqc_repo", "DIR"), ("--grandqc_python", "PATH"))
FORWARDED_SWITCHES = ("--no_metadata", "--no_save_artifacts", "--no_model_download", "--quiet")


def _add_run_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("forwarded to every pathnd-qc run (components; none given = all enabled in config)")
    for key, s in spec.COMPONENTS.items():
        g.add_argument(s["flag"], dest=f"run_{key}", action="store_true", help=f"{s['file']} [{s['stage']}]")
    for alias, members in spec.ALIASES.items():
        g.add_argument(alias, dest=alias.lstrip("-"), action="store_true", help=f"= {', '.join(members)}")
    f = p.add_argument_group("forwarded to every pathnd-qc run (options)")
    for flag, meta in FORWARDED_FLAGS:
        f.add_argument(flag, metavar=meta, action="append" if flag == "--metadata" else None,
                       help=("metadata CSV (local, GCS, S3 or Azure); repeat to check several supplied files "
                             "for every slide; omission skips lookup" if flag == "--metadata" else None))
    for flag in FORWARDED_SWITCHES:
        f.add_argument(flag, action="store_true")
    a = p.add_argument_group("per-slide artifacts (templates: {slide_id} {slide_dir} {stem} {latest_run} {latest_artifact})")
    for name, art in spec.ARTIFACTS.items():
        a.add_argument(art["flag"], dest=f"in_{name}", metavar="TEMPLATE", help=art["help"])


def _add_input_flags(p: argparse.ArgumentParser) -> None:
    i = p.add_argument_group("which slides")
    i.add_argument("--slides", metavar="FILE", help="a list (one path per line, # comments) or a "
                   ".csv/.tsv manifest with a `slide` column (+ optional stain, bank, artifact columns)")
    i.add_argument("--slide_dir", metavar="DIR", help="a local folder, walked recursively; subfolders are mirrored under --out")
    i.add_argument("--ext", default=",".join(manifest.SLIDE_EXTS),
                   help="slide extensions for --slide_dir (default: %(default)s)")
    i.add_argument("--out", required=True, help="parent of the per-run folders (as pathnd-qc --out)")
    i.add_argument("--batch_id", help="ID for <out>/batch_<id>/; reuse to resume this batch (default: YYYYMMDD_HHMMSS, UTC clock)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the batch", formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_input_flags(run)
    b = run.add_argument_group("the batch")
    b.add_argument("--workers", type=int, default=None,
                   help="concurrent pathnd-qc processes (default: cores - 1 = %d)" % max(1, runs.cpu_count() - 1))
    b.add_argument("--timeout_s", type=float, default=3600.0,
                   help="kill a slide's run after this many seconds (default %(default)s; 0 = none)")
    b.add_argument("--download_slots", type=int, default=4,
                   help="at most this many slides downloading at once across all workers (default %(default)s; 0 = no limit)")
    b.add_argument("--retry_failed", action="store_true", help="rerun slides whose last run failed")
    b.add_argument("--force", action="store_true", help="rerun every slide, done or not")
    b.add_argument("--python", help="interpreter for the per-slide pathnd-qc process (default: this one)")
    _add_run_flags(run)

    lst = sub.add_parser("list", help="dry run: show the jobs and the first command, run nothing")
    _add_input_flags(lst)
    _add_run_flags(lst)

    sm = sub.add_parser("summarize", help="one table over every run folder under --out")
    sm.add_argument("--out", required=True)
    sm.add_argument("--to", metavar="DIR", help="where to write summary.csv/json (default: <out>/summary_<stamp>/)")
    return ap


def _run_args(args) -> list[str]:
    """The pathnd-qc flags this batch forwards to every invocation."""
    out: list[str] = []
    for key, s in spec.COMPONENTS.items():
        if getattr(args, f"run_{key}", False):
            out.append(s["flag"])
    for alias in spec.ALIASES:
        if getattr(args, alias.lstrip("-"), False):
            out.append(alias)
    for flag, _ in FORWARDED_FLAGS:
        val = getattr(args, flag.lstrip("-"), None)
        if val:
            for v in (val if isinstance(val, list) else [val]):
                out += [flag, str(v)]
    for flag in FORWARDED_SWITCHES:
        if getattr(args, flag.lstrip("-"), False):
            out.append(flag)
    return out


def _jobs(args) -> list[manifest.Job]:
    rows = manifest.discover(args.slides, args.slide_dir,
                             exts=tuple(e if e.startswith(".") else "." + e for e in args.ext.split(",")),
                             exclude_dir=args.out)
    templates = {name: getattr(args, f"in_{name}", None) for name in spec.ARTIFACTS}
    return manifest.build_jobs(rows, templates, out_dir=args.out, stain=args.stain, bank=args.bank)


@contextmanager
def _termination_signals():
    """CLI owns process-global handlers; library calls leave their host application alone."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        # Repeated TERM must not interrupt the cleanup that the first signal initiated.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    configure_logging(getattr(args, "quiet", False))
    try:
        with _termination_signals():
            return _execute(args, argv)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, ValueError) else 1


def _execute(args, argv) -> int:
    if not str(args.out).strip():
        raise ValueError("--out: expected a nonempty directory path")
    if args.command == "summarize":
        s = summary.summarize(args.out)
        dest = args.to or str(runs.new_timestamped_dir(args.out, "summary_"))
        if os.path.exists(dest) and not os.path.isdir(dest):
            # --to must name a directory for summary output.
            raise ValueError(f"--to: {dest} exists and is not a directory; the summary is written "
                             f"as summary.csv / summary.json inside the directory you name")
        paths = summary.write_summary(s, dest)
        print(json.dumps(s["counts"], indent=2))
        print(f"summary -> {paths['csv']}")
        return 1 if s["counts"]["read_errors"] else 0
    try:
        jobs = _jobs(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    run_args = _run_args(args)
    runner.validate_run_args(run_args)
    if not jobs:
        raise ValueError("nothing to run: no slides matched the manifest or directory filter")
    if args.command == "list":
        preview_root = runs.batch_path(args.out, args.batch_id if args.batch_id is not None else "<batch_id>")
        print(f"{len(jobs)} job(s); pathnd-qc flags: {' '.join(run_args) or '(all enabled components)'}")
        for j in jobs:
            flag = "REFUSED " if j.problems else ""
            extras = " ".join(f"{spec.ARTIFACTS[n]['flag']}={p}" for n, p in j.supplied.items())
            print(f"  {flag}{j.key}: {j.slide}" + (f"  [{extras}]" if extras else "")
                  + (f"  !! {'; '.join(j.problems)}" if j.problems else ""))
            print(f"    output: {runner.output_parent(j, preview_root) / (j.slide_id + '_output')}")
        if jobs:
            print("first command:", shlex.join(runner.build_command(jobs[0], preview_root, run_args)))
        return 2 if any(j.problems for j in jobs) else 0
    record = runner.run_batch(jobs, args.out, run_args=run_args, workers=args.workers,
                              timeout_s=args.timeout_s or None, download_slots=args.download_slots,
                              retry_failed=args.retry_failed, force=args.force, batch_id=args.batch_id,
                              quiet=args.quiet, python=args.python,
                              argv=sys.argv if argv is None else argv)
    paths = record["summary_paths"]
    counts = record["counts"]
    print(f"batch {record['batch_id']}: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))
    print(f"log     -> {record['log']}")
    print(f"results -> {os.path.join(record['batch_dir'], 'results.csv')}")
    print(f"summary -> {paths['csv']}")
    print(f"browse  -> {os.path.join(record['batch_dir'], 'index.html')}")
    bad = counts["failed"] + counts["timeout"] + counts["error"] + counts["refused"] + counts["interrupted"]
    return 0 if bad == 0 and not record["summary_counts"]["read_errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
