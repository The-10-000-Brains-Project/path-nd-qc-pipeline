"""Smoke test — package-ready imports, relocation, CLI and configuration contracts.

No install, network, model weights or real slides. All generated files stay beneath src/tests.
Run: PYTHONDONTWRITEBYTECODE=1 python src/tests/smoke_api.py
"""
from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
sys.path.insert(0, str(SRC))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + str(detail) if detail else ''}")


out = Path(tempfile.mkdtemp(prefix="api_", dir=HERE))
try:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(out),
               MPLCONFIGDIR=str(out / "mpl"), PYTHONPATH=str(SRC))
    os.environ["MPLCONFIGDIR"] = env["MPLCONFIGDIR"]
    root = logging.getLogger()
    original_logging = (root.level, list(root.handlers), logging.getLogRecordFactory())
    import pathnd_qc
    check("import pathnd_qc does not eagerly load torch", "torch" not in sys.modules)
    for name in ("pathnd_qc", "pathnd_qc.ingestion", "pathnd_qc.qc_slide", "pathnd_qc.qc_tile",
                 "pathnd_qc.normalization", "pathnd_qc.reporting", "pathnd_qc.batch", "pathnd_qc.config"):
        module = importlib.import_module(name)
        check(f"all public exports resolve: {name}", all(callable(getattr(module, key)) for key in module.__all__))
    check("public run exposes named arguments", {"components", "supplied", "out_dir", "warn"}
          <= set(inspect.signature(pathnd_qc.run).parameters))
    check("library imports preserve application logging",
          original_logging == (root.level, list(root.handlers), logging.getLogRecordFactory()))

    # Import every implementation module through its namespace, never sys.path hacks within it.
    imported = []
    for file in (SRC / "pathnd_qc").rglob("*.py"):
        if file.name in {"__init__.py", "__main__.py"}:
            continue
        name = ".".join(file.relative_to(SRC).with_suffix("").parts)
        importlib.import_module(name)
        imported.append(name)
    check("all implementation modules import", bool(imported), len(imported))

    from pathnd_qc.config import resolve_out_dir, resolve_path
    previous = Path.cwd()
    os.chdir(out)
    try:
        check("default output belongs to caller cwd", resolve_out_dir() == out / "reports")
        check("configured relative model path belongs to caller cwd", resolve_path("weights/pen.pt")
              == str(out / "weights" / "pen.pt"))
    finally:
        os.chdir(previous)

    for command in ([sys.executable, str(SRC / "run.py"), "--help"],
                    [sys.executable, str(SRC / "batch_run.py"), "--help"],
                    [sys.executable, "-m", "pathnd_qc", "--help"],
                    [sys.executable, "-m", "pathnd_qc.batch", "--help"]):
        result = subprocess.run(command, cwd=out, env=env, capture_output=True, text=True, timeout=60)
        check(f"CLI help works away from source: {' '.join(command[1:])}",
              result.returncode == 0 and "usage:" in result.stdout, result.stderr[-300:])

    # Copy Python modules and their JSON resources: proves no wrapper, repository parent or image asset
    # is required to execute from a relocated package. This is not a wheel/install check.
    portable = out / "portable"
    for file in (SRC / "pathnd_qc").rglob("*"):
        if file.is_file() and (file.suffix == ".py" or file.suffix == ".json"):
            target = portable / file.relative_to(SRC)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
    env["PYTHONPATH"] = str(portable)
    script = '''import json
from pathnd_qc import run_batch, Job
from pathnd_qc.config import cfg
r = run_batch([Job("missing.svs", "missing", "missing")], "results", run_args=["--no_metadata", "--no_model_download"], workers=1)
print(json.dumps({"counts": r["counts"], "mpp": cfg("m2.read.target_mpp")}))
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=out, env=env,
                            capture_output=True, text=True, timeout=60)
    value = json.loads(result.stdout.splitlines()[-1]) if result.returncode == 0 else {}
    check("relocated package launches child without src/run.py", result.returncode == 0
          and value.get("counts", {}).get("refused") == 1 and value.get("mpp") == 8.0,
          result.stderr[-500:])
finally:
    shutil.rmtree(out, ignore_errors=True)

print(f"\n{'=' * 70}\nAPI SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  - {name}")
sys.exit(1 if FAIL else 0)
