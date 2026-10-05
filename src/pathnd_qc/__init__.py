"""Path-ND QC: whole-slide ingestion, quality control, normalization and batch processing.

Public entry points are imported on demand; importing ``pathnd_qc`` does not load models or
configure application logging. Component functions live in the corresponding subpackages.
"""
__version__ = "0.5.0"
REPORT_VERSION = "2.0"

from importlib import import_module

__all__ = ["run", "RunFailed", "Job", "Outcome", "run_batch", "WSIReader", "GCSWSIReader"]

_EXPORTS = {
    "run": ("pathnd_qc.pipeline", "run"),
    "RunFailed": ("pathnd_qc.pipeline", "RunFailed"),
    "Job": ("pathnd_qc.batch.manifest", "Job"),
    "Outcome": ("pathnd_qc.batch.runner", "Outcome"),
    "run_batch": ("pathnd_qc.batch.runner", "run_batch"),
    "GCSWSIReader": ("pathnd_qc.ingestion.wsi_reader.wsi_reader", "GCSWSIReader"),
    "WSIReader": ("pathnd_qc.ingestion.wsi_reader.wsi_reader", "WSIReader"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attr = _EXPORTS[name]
    value = getattr(import_module(module), attr)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
