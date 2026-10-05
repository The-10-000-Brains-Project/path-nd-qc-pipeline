"""Path-ND QC config.

Public component functions are imported on demand.
"""
from importlib import import_module

__all__ = ['cfg', 'load', 'resolved_sha256', 'resolve_path', 'resolve_out_dir']
_EXPORTS = {'cfg': 'config',
 'load': 'config',
 'resolved_sha256': 'config',
 'resolve_path': 'config',
 'resolve_out_dir': 'config'}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
