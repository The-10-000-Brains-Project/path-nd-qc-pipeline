"""Shared filesystem helpers for slide identifiers and streamed file hashing."""
from __future__ import annotations

import hashlib


def slide_id(path) -> str:
    """Return the basename without its extension as the slide identifier.

    Run folders and batch display names use this rule. Extension-only names
    retain their basename; paths without a basename use "slide".
    """
    name = str(path).rstrip("/").split("/")[-1]
    return name.rsplit(".", 1)[0] or name or "slide"


def sha256_file(path, chunk_bytes: int = 1 << 20) -> str:
    """Hex SHA-256 of a file, streamed in `chunk_bytes` blocks so a 2 GB slide is never in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(chunk_bytes), b""):
            digest.update(block)
    return digest.hexdigest()
