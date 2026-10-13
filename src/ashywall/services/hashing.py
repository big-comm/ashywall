"""Two-step duplicate detection.

Every indexed file gets a cheap signature (size plus the first and last
64 KiB). Only files whose signatures collide are read in full for a SHA-256,
and results are stored, so unchanged files are never read twice.
"""

import hashlib
import threading
from pathlib import Path

SAMPLE_SIZE = 64 * 1024
CHUNK_SIZE = 1024 * 1024


class HashCancelled(Exception):
    pass


def quick_signature(path: Path, file_size: int) -> str:
    digest = hashlib.sha256(file_size.to_bytes(8, "little"))
    with path.open("rb") as handle:
        digest.update(handle.read(SAMPLE_SIZE))
        if file_size > SAMPLE_SIZE:
            handle.seek(max(SAMPLE_SIZE, file_size - SAMPLE_SIZE))
            digest.update(handle.read(SAMPLE_SIZE))
    return digest.hexdigest()


def content_sha256(path: Path, cancel_event: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            if cancel_event is not None and cancel_event.is_set():
                raise HashCancelled()
            digest.update(chunk)
    return digest.hexdigest()
