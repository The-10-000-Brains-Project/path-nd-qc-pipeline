"""Isolated downloads: the worker owns files; this process owns its lifetime."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathnd_qc._logging import get_logger

logger = get_logger(__name__)
_CHUNK = 8 * 1024 * 1024


def open_binary(path: str, timeout: float):
    """Open a binary stream with request limits and synchronous GCS caching.

    File options belong to fs.open, not url_to_fs: filesystem constructor options
    do not configure GCSFile's prefetcher. The caller owns and closes the stream.
    """
    import fsspec
    fs, key = fsspec.core.url_to_fs(path, **storage_options(path, timeout))
    options = ({"cache_type": "readahead", "use_experimental_adaptive_prefetching": False}
               if str(path).split(":", 1)[0].lower() in {"gs", "gcs"} else {})
    return fs.open(key, "rb", **options)


def storage_options(path: str, timeout: float) -> dict:
    """Native request limits; authentication and complete operations may take longer."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("remote request timeout must be finite and positive")
    protocol = str(path).split(":", 1)[0].lower()
    if protocol in {"gs", "gcs"}:
        return {"requests_timeout": float(timeout)}
    if protocol in {"s3", "s3a"}:
        return {"config_kwargs": {"connect_timeout": float(timeout),
                                  "read_timeout": float(timeout)}}
    if protocol in {"az", "abfs", "abfss"}:
        # Older adlfs releases default to anonymous access. Use the Azure credential
        # chain unless the user explicitly requests public access, on every version.
        return {"anon": os.getenv("AZURE_STORAGE_ANON", "false").lower()
                        not in {"false", "0", "f", ""},
                "connection_timeout": float(timeout), "read_timeout": float(timeout)}
    if protocol in {"http", "https"}:
        from aiohttp import ClientTimeout
        return {"client_kwargs": {"timeout": ClientTimeout(total=float(timeout))}}
    return {}


def verify_local_copy(remote_path: str, local_path: str, progress=None,
                      request_timeout: float | None = None) -> dict:
    """Compare size and available MD5; unavailable remote metadata stays unverified.

    ``progress`` receives the number of newly hashed bytes after each completed block.
    Called inside the isolated worker in production downloads; no remote handles escape.
    """
    import fsspec
    out = {"localized": True, "verified": None, "check": None, "error": None,
           "size_remote": None, "size_local": os.path.getsize(local_path)}
    try:
        options = storage_options(remote_path, request_timeout) if request_timeout else {}
        fs, rpath = fsspec.core.url_to_fs(remote_path, **options)
        meta = fs.info(rpath)
    except Exception as exc:  # noqa: BLE001 -- preserve the unavailable-check contract
        out.update(verified=None, check="unavailable", error=f"{type(exc).__name__}: {exc}")
        return out
    size = meta.get("size") if isinstance(meta, dict) else None
    out["size_remote"] = size
    if size is not None and int(size) != out["size_local"]:
        out.update(verified=False, check="size",
                   error=f"size mismatch: remote {size} != local {out['size_local']}")
        return out
    checks = ["size"] if size is not None else []
    md5_b64 = meta.get("md5Hash") if isinstance(meta, dict) else None
    if not md5_b64 and isinstance(meta, dict):
        # adlfs forwards Azure's ContentSettings with the whole-blob MD5 as bytes.
        settings = meta.get("content_settings") or {}
        azure_md5 = settings.get("content_md5")
        if isinstance(azure_md5, (bytes, bytearray)) and azure_md5:
            md5_b64 = base64.b64encode(azure_md5).decode()
    # S3 ETags may represent multipart uploads or encrypted objects, not content MD5.
    # With no supported checksum, report the size check alone.
    if md5_b64:
        digest = hashlib.md5()
        with open(local_path, "rb") as handle:
            for block in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(block)
                if progress is not None:
                    progress(len(block))
        if base64.b64encode(digest.digest()).decode() != md5_b64:
            out.update(verified=False, check="+".join(checks + ["md5"]), error="md5 mismatch")
            return out
        checks.append("md5")
    out.update(verified=bool(checks), check="+".join(checks) if checks else "none")
    return out


class DownloadError(RuntimeError):
    """Worker failure with its original exception class preserved for diagnostics."""

    def __init__(self, error_type: str, message: str, status_code: int | None = None):
        self.error_type = error_type
        self.status_code = status_code
        super().__init__(f"{error_type}: {message}")


def download_attempt(remote_path: str, local_path: str, verify: bool,
                     stall_timeout: float, progress_every: float) -> dict:
    """Download and verify once, stopping the worker when byte progress stalls.

    Opening, copying and verification share the inactivity deadline. Copying and hashing
    reset it only when bytes advance. The caller owns retry policy and destination cleanup.
    """
    request = dict(remote_path=str(remote_path), local_path=str(local_path), verify=verify,
                   stall_timeout=stall_timeout, progress_every=progress_every)
    return _monitor_download([sys.executable, "-P", "-m", "pathnd_qc.ingestion._download_worker"], request)


def _monitor_download(command, request: dict) -> dict:
    """Monitor the JSON-lines worker protocol; injectable command supports hermetic checks."""
    timeout = float(request["stall_timeout"])
    every = float(request["progress_every"])
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("download stall timeout must be finite and positive")
    if not math.isfinite(every) or every < 0:
        raise ValueError("download progress interval must be finite and non-negative")
    env = os.environ.copy()
    # Also works from a source checkout imported by a caller's sys.path, with any cwd.
    source_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [source_root, env.get("PYTHONPATH")]))
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1, env=env)
    events = queue.Queue(maxsize=128)
    stop = threading.Event()
    tail = deque(maxlen=5)

    def pump():
        try:
            for line in iter(lambda: proc.stdout.readline(65536), ""):
                while not stop.is_set():
                    try:
                        events.put(line, timeout=0.1)
                        break
                    except queue.Full:
                        pass
                if stop.is_set():
                    break
        finally:
            stop.set()

    thread = threading.Thread(target=pump, name="pathnd_qc-download-output", daemon=True)
    started = last_progress = last_log = time.monotonic()
    seen, result, failure, phase = 0, None, None, "opening"
    try:
        thread.start()
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.close()
        while True:
            try:
                line = events.get(timeout=min(0.1, timeout))
            except queue.Empty:
                line = None
            if line is not None:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    tail.append(line.strip()[-1000:])
                    event = {}
                if not isinstance(event, dict):
                    event = {}
                if event.get("event") == "progress":
                    count = event.get("bytes")
                    if isinstance(count, int) and not isinstance(count, bool) and count > seen:
                        seen, last_progress = count, time.monotonic()
                        phase = event.get("phase", "download")
                elif event.get("event") == "result":
                    result = event.get("info")
                elif event.get("event") == "error":
                    failure = event
                elif event.get("event") == "phase":
                    phase = event.get("phase", phase)  # context only, never resets the deadline
            now = time.monotonic()
            if stop.is_set() and events.empty() and proc.poll() is not None:
                if failure:
                    raise DownloadError(failure.get("error_type", "RuntimeError"),
                                        failure.get("error", "download worker failed"),
                                        failure.get("status_code"))
                if proc.returncode == 0 and isinstance(result, dict):
                    return result
                detail = " | ".join(tail)
                raise DownloadError("WorkerError", f"exit {proc.returncode}; no successful result"
                                    + (f": {detail}" if detail else ""))
            if now - last_progress >= timeout:
                raise TimeoutError(f"download stalled: no bytes for {timeout:g} s after "
                                   f"{seen:,} bytes of {request['remote_path']} ({phase})")
            if every and now - last_log >= every:
                logger.info("Localizing %s: %.2f GB processed (%.1f MB/s; %s)",
                            request["remote_path"], seen / 1e9,
                            seen / max(now - started, 1e-6) / 1e6, phase)
                last_log = now
    finally:
        # No retry or file cleanup can race a worker still using the destination.
        if proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:  # exited between poll and signal
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait()
        stop.set()
        if thread.ident is not None:
            thread.join()
        proc.stdout.close()
        proc.stdin.close()
