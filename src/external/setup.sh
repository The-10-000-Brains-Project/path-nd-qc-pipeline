#!/usr/bin/env bash
# Compatibility entry point. The installable Python package owns backend setup.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$HERE/..${PYTHONPATH:+:$PYTHONPATH}"
# Set PATHND_PYTHON when the package dependencies live in another environment.
exec "${PATHND_PYTHON:-python3}" -m pathnd_qc setup grandqc "$@"
