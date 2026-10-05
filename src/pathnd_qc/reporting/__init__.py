"""Path-ND QC reporting.

Public component functions are imported on demand.
"""
from importlib import import_module

__all__ = ['build_report', 'write_report']
_EXPORTS = {'build_report': 'reporting', 'write_report': 'reporting'}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
