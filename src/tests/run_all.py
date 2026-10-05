"""Run checkout smoke/unit suites and both workflow suites with the same interpreter.

Set PATHND_TEST_SOURCE_ARCHIVE to include the bucket workflow's release-archive case.
Run from any directory: python /path/to/repo/src/tests/run_all.py
"""

from pathlib import Path
import os
import subprocess
import sys


def main():
    source = Path(__file__).resolve().parents[1]
    test_dir = source / "tests"
    suites = sorted(test_dir.glob("smoke_*.py")) + sorted(test_dir.glob("test_*.py"))
    suites += [
        source / "deploy/verily" / name
        for name in ("test_workflow.py", "test_bucket_workflow.py")
    ]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(source), env.get("PYTHONPATH")))
    )
    failed = []
    for suite in suites:
        print(f"\nRunning {suite.relative_to(source)}", flush=True)
        result = subprocess.run(
            [sys.executable, str(suite)], cwd=source.parent, env=env
        )
        if result.returncode:
            failed.append(str(suite.relative_to(source)))
    print(f"\n{len(suites) - len(failed)}/{len(suites)} suites passed", flush=True)
    for name in failed:
        print(f"FAILED: {name}")
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
