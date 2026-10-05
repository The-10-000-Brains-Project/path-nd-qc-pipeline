"""Path-ND QC ingestion.

Public component functions are imported on demand.
"""
from importlib import import_module

__all__ = ['WSIReader', 'GCSWSIReader', 'check_mpp', 'check_integrity', 'build_ingestion_record']
_EXPORTS = {'GCSWSIReader': 'wsi_reader.wsi_reader',
 'WSIReader': 'wsi_reader.wsi_reader',
 'check_mpp': 'ingestion_checks.ingestion_checks',
 'check_integrity': 'ingestion_checks.ingestion_checks',
 'build_ingestion_record': 'ingestion_checks.ingestion_checks'}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
