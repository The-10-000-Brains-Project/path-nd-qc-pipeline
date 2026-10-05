"""Compatibility entry point: ``python src/batch_run.py`` or ``python -m pathnd_qc.batch``."""
from pathnd_qc.batch.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
