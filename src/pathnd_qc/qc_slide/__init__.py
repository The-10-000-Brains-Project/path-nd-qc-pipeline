"""Path-ND QC qc slide.

Public component functions are imported on demand.
"""
from importlib import import_module

__all__ = ['compute_tissue_mask',
 'detect_folds',
 'detect_pen',
 'compute_focus_score',
 'compute_staining_metrics']
_EXPORTS = {'compute_tissue_mask': 'tissue.tissue',
 'detect_folds': 'folds.folds',
 'detect_pen': 'pen.pen',
 'compute_focus_score': 'focus.focus',
 'compute_staining_metrics': 'staining.staining'}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
