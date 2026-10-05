"""Private JSON-lines download worker. Owns all remote and destination handles."""
from __future__ import annotations

import json
import sys

from ._download import open_binary, verify_local_copy


def main() -> int:
    def emit(event):
        print(json.dumps(event), flush=True)

    try:
        request = json.loads(sys.stdin.readline())
        # Import only the storage backend here, never the pipeline or model dependencies.
        path, local = request["remote_path"], request["local_path"]
        count = 0

        def advance(size, phase="download"):
            nonlocal count
            count += size
            emit({"event": "progress", "bytes": count, "phase": phase})

        with open_binary(path, request["stall_timeout"]) as src:
            with open(local, "wb") as dst:
                for block in iter(lambda: src.read(8 * 1024 * 1024), b""):
                    dst.write(block)
                    advance(len(block))
        if request["verify"]:
            emit({"event": "phase", "phase": "verification"})
        info = (verify_local_copy(path, local,
                                 progress=lambda size: advance(size, "verification"),
                                 request_timeout=request["stall_timeout"])
                if request["verify"] else
                {"localized": True, "verified": None, "check": "skipped", "error": None})
        emit({"event": "result", "info": info})
        return 0
    except Exception as exc:  # noqa: BLE001 -- transport the original failure to the caller
        emit({"event": "error", "error_type": type(exc).__name__, "error": str(exc),
              "status_code": getattr(exc, "status_code", None)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
