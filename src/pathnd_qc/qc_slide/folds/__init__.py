"""Fold detection and line-feature helpers, imported on demand."""
from importlib import import_module

__all__ = ["detect_folds", "fline_feature", "fline_threshold", "generate_fold_overlay",
           "subtract_pen", "to_report"]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.folds"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
