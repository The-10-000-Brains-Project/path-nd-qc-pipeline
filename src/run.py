"""Compatibility entry point: ``python src/run.py`` or ``python -m pathnd_qc``."""
import sys
from pathnd_qc import pipeline

if __name__ == "__main__":
    raise SystemExit(pipeline.main())
else:
    # Keep existing ``import run`` callers on the same module, including dependency injection.
    sys.modules[__name__] = pipeline
