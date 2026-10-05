"""Batch processing with one isolated pathnd-qc process per slide.

A worker pool launches up to N slides at once, combines their logs, records
individual failures, and resumes matching completed runs from saved status.
Each child owns its memory and follows the pipeline’s exit-code and output contracts.

manifest.py discovers slides and per-slide inputs.
runs.py manages saved status, fingerprints, and batch files.
runner.py launches children, streams logs, enforces timeouts, and collects outcomes.
summary.py aggregates status, timing, and headline metrics from saved reports.

Public component functions are imported on demand.
"""
from importlib import import_module

__all__ = ['Job', 'discover', 'build_jobs', 'Outcome', 'run_batch', 'summarize', 'write_summary']
_EXPORTS = {'Job': 'manifest',
 'discover': 'manifest',
 'build_jobs': 'manifest',
 'Outcome': 'runner',
 'run_batch': 'runner',
 'summarize': 'summary',
 'write_summary': 'summary'}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
