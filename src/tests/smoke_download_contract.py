"""Smoke test — native request deadlines versus an isolated download deadline.

Uses localhost HTTP only and controlled subprocess fixtures, never cloud credentials or slides.
The pre-request delay is an explicit auth/open simulation, not a cloud performance benchmark.
Run: PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/src/tests" python src/tests/smoke_download_contract.py
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch

import aiohttp
import fsspec

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from pathnd_qc.ingestion import _download as download                          # noqa: E402
from pathnd_qc.ingestion.wsi_reader import wsi_reader as reader_module           # noqa: E402

PASS, FAIL = [], []
TIMINGS = {}
PAYLOAD = bytes(range(256)) * 256


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + str(detail) if detail else ''}")


def section(name, fn):
    print(f"\n[{name}]")
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 -- preserve independent results
        traceback.print_exc()
        check(f"{name} completes without an unexpected exception", False,
              f"{type(exc).__name__}: {exc}")


def measure(fn):
    start = time.monotonic()
    try:
        return fn(), None, time.monotonic() - start
    except Exception as exc:  # noqa: BLE001 -- capture expected failure and actual latency
        return None, exc, time.monotonic() - start


class Handler(BaseHTTPRequestHandler):
    """A known byte source; named endpoints deliberately violate one transfer property."""

    protocol_version = "HTTP/1.1"
    corrupt_downloaded = False
    unavailable_downloaded = False

    def log_message(self, *_args):
        pass

    def _respond(self, body):
        try:
            if self.path == "/slow":
                time.sleep(0.8)
            if self.path == "/missing":
                self.send_error(404, "fixture missing")
                return
            if self.path == "/unverifiable" and Handler.unavailable_downloaded:
                self.send_error(503, "fixture verification metadata unavailable")
                return
            self.send_response(200)
            # Initially the source has N bytes. After GET it advertises N+1: verification
            # must reject this changed source rather than bless the cached download size.
            size = len(PAYLOAD) + int(self.path == "/changed" and Handler.corrupt_downloaded
                                      and not body)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "none")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if body:
                self.wfile.write(PAYLOAD)
                self.wfile.flush()
                if self.path == "/changed":
                    Handler.corrupt_downloaded = True
                if self.path == "/unverifiable":
                    Handler.unavailable_downloaded = True
        except (BrokenPipeError, ConnectionResetError):
            pass  # client timeout intentionally disconnects before the delayed response

    def do_HEAD(self):
        self._respond(False)

    def do_GET(self):
        self._respond(True)


def native_read(path):
    with fsspec.open(url + path, "rb", block_size=0,
                     client_kwargs={"timeout": aiohttp.ClientTimeout(total=0.15)}) as source:
        return source.read()


def native_comparison():
    # aiohttp's ClientTimeout bounds its HTTP request, not surrounding Python auth/open work.
    payload, exc, elapsed = measure(lambda: native_read("/ok"))
    TIMINGS["native_success_s"] = round(elapsed, 3)
    check("native HTTP timeout settings preserve a successful full copy", payload == PAYLOAD, exc)
    _, exc, elapsed = measure(lambda: native_read("/slow"))
    TIMINGS["native_slow_request_s"] = round(elapsed, 3)
    check("native HTTP deadline interrupts a response delayed beyond the request timeout",
          exc is not None and elapsed < 0.8, (type(exc).__name__, str(exc), round(elapsed, 3)))

    def pre_request():
        time.sleep(0.45)  # controlled stand-in for auth/client opening before the HTTP request
        return native_read("/ok")

    payload, exc, elapsed = measure(pre_request)
    TIMINGS["native_with_pre_request_delay_s"] = round(elapsed, 3)
    check("a native request timeout does not bound controlled pre-request work",
          payload == PAYLOAD and exc is None and elapsed >= 0.45, round(elapsed, 3))


def request(path, timeout=0.25):
    return {"remote_path": url + "/ok", "local_path": str(path), "verify": True,
            "stall_timeout": timeout, "progress_every": 0.05}


def monitor(code, req, *args):
    return download._monitor_download([sys.executable, "-c", code, *args], req)


def process_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def blocked_phases():
    # The process contract covers arbitrary blocked open/read/info calls, including code that
    # cannot accept aiohttp settings. Each fixture identifies its phase then makes no progress.
    code = (
        "import json, os, pathlib, sys, time\n"
        "r=json.loads(sys.stdin.readline())\n"
        "pathlib.Path(r['local_path']+'.pid').write_text(str(os.getpid()))\n"
        "print(json.dumps({'event':'progress','bytes':0,'phase':sys.argv[1]}),flush=True)\n"
        "time.sleep(60)\n"
    )
    for phase in ("open", "read", "info"):
        dest = out / phase
        _, exc, elapsed = measure(lambda: monitor(code, request(dest), phase))
        TIMINGS[f"isolated_blocked_{phase}_s"] = round(elapsed, 3)
        pid_path = Path(str(dest) + ".pid")
        check(f"isolated monitor bounds blocked {phase} and reaps its worker before returning",
              isinstance(exc, TimeoutError) and elapsed < 2.0 and pid_path.exists()
              and process_gone(int(pid_path.read_text()))
              and not any(t.name == "pathnd_qc-download-output" for t in threading.enumerate()),
              (type(exc).__name__, str(exc), round(elapsed, 3)))


def process_protocol():
    # Only increasing copied/hashed bytes may reset the stall timer; messages alone cannot.
    code = (
        "import json, pathlib, sys, time\n"
        "r=json.loads(sys.stdin.readline()); p=pathlib.Path(r['local_path'])\n"
        "with p.open('wb') as f:\n"
        " for i in range(6):\n"
        "  f.write(b'chunk'); f.flush()\n"
        "  print(json.dumps({'event':'progress','bytes':f.tell(),'phase':'download'}),flush=True)\n"
        "  time.sleep(0.08)\n"
        "print(json.dumps({'event':'result','info':{'localized':True,'verified':True,'check':'size'}}),flush=True)\n"
    )
    dest = out / "progress.bin"
    info, exc, elapsed = measure(lambda: monitor(code, request(dest, 0.25)))
    TIMINGS["isolated_progressing_copy_s"] = round(elapsed, 3)
    check("increasing actual copied bytes keep a transfer alive beyond one stall interval",
          exc is None and info.get("verified") is True and elapsed > 0.25
          and dest.read_bytes() == b"chunk" * 6, (info, exc, round(elapsed, 3)))
    hashing = (
        "import hashlib, json, pathlib, sys, time\n"
        "r=json.loads(sys.stdin.readline()); p=pathlib.Path(r['local_path']); p.write_bytes(b'chunk'*6)\n"
        "count=p.stat().st_size\n"
        "print(json.dumps({'event':'progress','bytes':count,'phase':'download'}),flush=True)\n"
        "digest=hashlib.md5()\n"
        "with p.open('rb') as f:\n"
        " for block in iter(lambda:f.read(5),b''):\n"
        "  digest.update(block); count+=len(block)\n"
        "  print(json.dumps({'event':'progress','bytes':count,'phase':'verification'}),flush=True)\n"
        "  time.sleep(0.08)\n"
        "print(json.dumps({'event':'result','info':{'localized':True,'verified':True}}),flush=True)\n"
    )
    info, exc, elapsed = measure(lambda: monitor(hashing, request(out / "hashing.bin")))
    check("increasing actual hashed bytes reset the verification stall deadline",
          exc is None and info.get("verified") is True and elapsed > 0.25,
          (info, exc, round(elapsed, 3)))
    idle = (
        "import json, sys, time\n"
        "r=json.loads(sys.stdin.readline())\n"
        "for i in range(100):\n"
        " print(json.dumps({'event':'progress','bytes':0,'phase':'read'}),flush=True)\n"
        " time.sleep(0.04)\n"
    )
    _, exc, elapsed = measure(lambda: monitor(idle, request(out / "idle")))
    check("repeated messages without byte progress do not extend the stall deadline",
          isinstance(exc, TimeoutError) and elapsed < 2.0, (exc, round(elapsed, 3)))
    error = (
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'event':'error','error_type':'PermissionError','error':'fixture access denied'}),flush=True)\n"
        "sys.exit(1)\n"
    )
    _, exc, _ = measure(lambda: monitor(error, request(out / "denied")))
    check("subprocess errors preserve their original exception type and cause",
          getattr(exc, "error_type", None) == "PermissionError" and "fixture access denied" in str(exc), exc)
    premature = (
        "import json, sys, time\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'event':'result','info':{'localized':True}}),flush=True)\n"
        "time.sleep(60)\n"
    )
    _, exc, _ = measure(lambda: monitor(premature, request(out / "premature")))
    check("a result message cannot release an attempt while its worker remains alive",
          isinstance(exc, TimeoutError), exc)
    _, exc, _ = measure(lambda: monitor("import sys; sys.stdin.readline()", request(out / "empty")))
    check("exit zero without a result is a worker failure",
          getattr(exc, "error_type", None) == "WorkerError", exc)


def real_worker():
    dest = out / "actual_http.bin"
    info, exc, elapsed = measure(lambda: download.download_attempt(
        url + "/ok", str(dest), verify=True, stall_timeout=3.0, progress_every=0.05))
    TIMINGS["isolated_http_success_s"] = round(elapsed, 3)
    check("real isolated HTTP worker copies every byte and verifies source size",
          exc is None and dest.read_bytes() == PAYLOAD and info.get("verified") is True
          and info.get("size_remote") == len(PAYLOAD), (info, exc, round(elapsed, 3)))
    _, exc, elapsed = measure(lambda: download.download_attempt(
        url + "/slow", str(out / "slow_http.bin"), verify=True,
        stall_timeout=0.25, progress_every=0.05))
    TIMINGS["isolated_slow_request_s"] = round(elapsed, 3)
    check("the real isolated worker also bounds a slow HTTP source",
          isinstance(exc, TimeoutError) and elapsed < 2.0,
          (type(exc).__name__, str(exc), round(elapsed, 3)))
    Handler.corrupt_downloaded = False
    info, exc, _ = measure(lambda: download.download_attempt(
        url + "/changed", str(out / "changed.bin"), verify=True,
        stall_timeout=3.0, progress_every=0.05))
    check("real verification detects source-size changes after download",
          exc is None and info.get("verified") is False and "mismatch" in str(info.get("error")), (info, exc))
    _, exc, _ = measure(lambda: download.download_attempt(
        url + "/missing", str(out / "missing.bin"), verify=True,
        stall_timeout=3.0, progress_every=0.05))
    check("real subprocess download failure retains the source error cause",
          exc is not None and getattr(exc, "error_type", None) in {"FileNotFoundError", "ClientResponseError"}, exc)
    Handler.unavailable_downloaded = False
    dest = out / "unverifiable.bin"
    info, exc, _ = measure(lambda: download.download_attempt(
        url + "/unverifiable", str(dest), verify=True,
        stall_timeout=3.0, progress_every=0.05))
    check("unavailable verification metadata preserves the explicit unverified-copy policy",
          exc is None and dest.read_bytes() == PAYLOAD and info.get("localized") is True
          and info.get("verified") is None and info.get("check") == "unavailable", (info, exc))


def retry_integration():
    # Reader contract H1/H2: failed verification is retried, never yielded as a usable copy.
    cache = out / "cache"
    cache.mkdir()
    seen = []

    def attempt(remote, local, **_kwargs):
        seen.append(local)
        Path(local).write_bytes(PAYLOAD)
        verified = len(seen) > 1
        return {"localized": True, "verified": verified, "check": "size",
                "error": None if verified else "size mismatch"}

    with patch.object(reader_module, "download_attempt", attempt), \
            patch.object(reader_module, "LOCALIZE_RETRY_BACKOFF_S", 0), \
            patch.object(reader_module, "LOCALIZE_RETRIES", 2), \
            patch.dict(os.environ, {"PATHND_LOCALIZE_SLOTS": ""}):
        reader = reader_module.GCSWSIReader()
        with reader.localize("http://synthetic/slide.svs", cache_dir=str(cache), verify=True) as local:
            check("a failed transfer verification retries and yields only the successful copy",
                  local is not None and Path(local).read_bytes() == PAYLOAD
                  and reader.last_localize.get("attempts") == 2
                  and reader.last_localize.get("verified") is True, reader.last_localize)
        check("a successful localized copy is removed when its context ends", not list(cache.iterdir()))

    def corrupt(remote, local, **_kwargs):
        Path(local).write_bytes(b"corrupt")
        return {"localized": True, "verified": False, "check": "size", "error": "size mismatch"}

    with patch.object(reader_module, "download_attempt", corrupt), \
            patch.object(reader_module, "LOCALIZE_RETRY_BACKOFF_S", 0), \
            patch.object(reader_module, "LOCALIZE_RETRIES", 2), \
            patch.dict(os.environ, {"PATHND_LOCALIZE_SLOTS": ""}):
        reader = reader_module.GCSWSIReader()
        with reader.localize("http://synthetic/slide.svs", cache_dir=str(cache), verify=True) as local:
            check("all verification attempts failing refuses the copy and removes its temp file",
                  local is None and not list(cache.iterdir())
                  and reader.last_localize.get("attempts") == 2
                  and reader.last_localize.get("localized") is False, reader.last_localize)


out = Path(tempfile.mkdtemp(prefix="download_contract_", dir=HERE))
server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = True
url = f"http://127.0.0.1:{server.server_port}"
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    section("1 native request timeout comparison", native_comparison)
    section("2 isolated blocked phases and cleanup", blocked_phases)
    section("3 progress and subprocess error protocol", process_protocol)
    section("4 real isolated HTTP worker", real_worker)
    section("5 reader verification retry integration", retry_integration)
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    shutil.rmtree(out, ignore_errors=True)

print("\nControlled timing comparison:", json.dumps(TIMINGS, sort_keys=True))
print(f"\n{'=' * 70}\nDOWNLOAD CONTRACT SMOKE: {len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  - {name}")
sys.exit(1 if FAIL else 0)
