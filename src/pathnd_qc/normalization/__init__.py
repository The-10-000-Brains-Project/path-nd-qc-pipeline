"""Module 4 — Stain Normalization.

Public component functions are imported on demand.
"""
from importlib import import_module

__all__ = ['fit_reference', 'freeze_slide', 'normalize', 'normalize_slide', 'load_reference']
_EXPORTS = {'fit_reference': 'stain_norm.stain_norm',
 'freeze_slide': 'stain_norm.stain_norm',
 'normalize': 'stain_norm.stain_norm',
 'normalize_slide': 'stain_norm.stain_norm',
 'load_reference': 'stain_norm.stain_norm'}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
