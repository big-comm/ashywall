"""Incremental indexing of local sources into the catalog.

A file is probed (header only) and fingerprinted only when it is new or its
size/mtime changed. Rows for files that disappeared are deleted; favorites
are keyed by path, so they come back if the file does.
"""

import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..models.wallpaper import SourceKind
from ..providers.local import (
    DiscoveredFile,
    discover_system_wallpapers,
    scan_folder,
    title_from_path,
)
from .database import transaction
from .hashing import HashCancelled, content_sha256, quick_signature
from .image_probe import ImageInfo, ImageProbeError, probe_image
from .library_repository import LibraryRepository

WRITE_BATCH_SIZE = 50


class IndexCancelled(Exception):
    pass


@dataclass(frozen=True, slots=True)
class IndexProgress:
    processed: int
    total: int


@dataclass(slots=True)
class IndexSummary:
    added: int = 0
    updated: int = 0
    removed: int = 0
    invalid: int = 0
    unavailable_folders: tuple[Path, ...] = ()


_UPSERT_SQL = """
    INSERT INTO wallpapers (
        source_id, path, title, dark_path, picture_options, width, height, image_format,
        file_size, mtime_ns, quick_hash, content_hash, status, error, indexed_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
    ON CONFLICT (source_id, path) DO UPDATE SET
        title = excluded.title, dark_path = excluded.dark_path,
        picture_options = excluded.picture_options, width = excluded.width,
        height = excluded.height, image_format = excluded.image_format,
        file_size = excluded.file_size, mtime_ns = excluded.mtime_ns,
        quick_hash = excluded.quick_hash, content_hash = NULL,
        status = excluded.status, error = excluded.error, indexed_at = excluded.indexed_at
"""


class LibraryIndexer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        data_dirs: list[Path],
        user_data_home: Path,
        downloads_dir: Path,
        on_progress: Callable[[IndexProgress], None] | None = None,
        cancel_event: threading.Event | None = None,
        probe: Callable[[Path], ImageInfo] = probe_image,
    ) -> None:
        self._conn = conn
        self._data_dirs = data_dirs
        self._user_data_home = user_data_home
        self._downloads_dir = downloads_dir
        self._on_progress = on_progress
        self._cancel = cancel_event or threading.Event()
        self._probe = probe
        self._processed = 0
        self._total = 0

    def run(self) -> IndexSummary:
        repository = LibraryRepository(self._conn)
        repository.ensure_builtin_sources(self._downloads_dir)
        summary = IndexSummary()
        jobs: list[tuple[int, list[DiscoveredFile]]] = []
        unavailable: list[Path] = []

        for source in repository.list_sources():
            if source.kind is SourceKind.SYSTEM:
                files = discover_system_wallpapers(
                    self._data_dirs, self._user_data_home
                )
            elif source.path is not None and source.path.is_dir():
                files = [DiscoveredFile(path=path) for path in scan_folder(source.path)]
            else:
                files = []
                if source.kind is SourceKind.FOLDER and source.path is not None:
                    unavailable.append(source.path)
            jobs.append((source.id, files))
            self._check_cancelled()

        self._total = sum(len(files) for _, files in jobs)
        self._report()
        for source_id, files in jobs:
            self._sync_source(source_id, files, summary)
        self._resolve_content_hashes()
        summary.unavailable_folders = tuple(unavailable)
        return summary

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise IndexCancelled()

    def _report(self) -> None:
        if self._on_progress is not None:
            self._on_progress(IndexProgress(self._processed, self._total))

    def _sync_source(
        self, source_id: int, files: Iterable[DiscoveredFile], summary: IndexSummary
    ) -> None:
        existing = {
            row["path"]: row
            for row in self._conn.execute(
                "SELECT path, file_size, mtime_ns, title, dark_path, picture_options "
                "FROM wallpapers WHERE source_id = ?",
                (source_id,),
            )
        }
        seen: set[str] = set()
        pending: list[tuple[object, ...]] = []

        for entry in files:
            self._check_cancelled()
            self._processed += 1
            key = str(entry.path)
            try:
                file_stat = os.stat(entry.path)
            except OSError:
                continue
            if key in seen or not stat.S_ISREG(file_stat.st_mode):
                continue
            seen.add(key)
            row = existing.get(key)
            title = entry.title or title_from_path(entry.path)
            dark = str(entry.dark_path) if entry.dark_path else None
            if (
                row
                and row["file_size"] == file_stat.st_size
                and row["mtime_ns"] == file_stat.st_mtime_ns
            ):
                if (row["title"], row["dark_path"], row["picture_options"]) != (
                    title,
                    dark,
                    entry.picture_options,
                ):
                    self._update_metadata(
                        source_id, key, title, dark, entry.picture_options
                    )
            else:
                record = self._inspect(source_id, entry, title, dark, file_stat)
                pending.append(record)
                summary.updated += 1 if row else 0
                summary.added += 0 if row else 1
                summary.invalid += 1 if record[11] == "invalid" else 0
            if len(pending) >= WRITE_BATCH_SIZE:
                self._flush(pending)
            if self._processed % 25 == 0:
                self._report()
        self._flush(pending)
        self._report()

        gone = [path for path in existing if path not in seen]
        if gone:
            with transaction(self._conn):
                self._conn.executemany(
                    "DELETE FROM wallpapers WHERE source_id = ? AND path = ?",
                    [(source_id, path) for path in gone],
                )
            summary.removed += len(gone)

    def _inspect(
        self,
        source_id: int,
        entry: DiscoveredFile,
        title: str,
        dark: str | None,
        file_stat: os.stat_result,
    ) -> tuple[object, ...]:
        width = height = 0
        image_format: str | None = None
        quick_hash: str | None = None
        status, error = "ok", None
        try:
            info = self._probe(entry.path)
            width, height, image_format = info.width, info.height, info.image_format
            quick_hash = quick_signature(entry.path, file_stat.st_size)
        except (ImageProbeError, OSError) as failure:
            status, error = "invalid", str(failure)[:500]
        return (
            source_id, str(entry.path), title, dark, entry.picture_options, width, height,
            image_format, file_stat.st_size, file_stat.st_mtime_ns, quick_hash, status, error,
            int(time.time()),
        )  # fmt: skip

    def _update_metadata(
        self,
        source_id: int,
        path: str,
        title: str,
        dark: str | None,
        options: str | None,
    ) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE wallpapers SET title = ?, dark_path = ?, picture_options = ? "
                "WHERE source_id = ? AND path = ?",
                (title, dark, options, source_id, path),
            )

    def _flush(self, pending: list[tuple[object, ...]]) -> None:
        if pending:
            with transaction(self._conn):
                self._conn.executemany(_UPSERT_SQL, pending)
            pending.clear()

    def _resolve_content_hashes(self) -> None:
        """Hash in full only files whose quick signatures collide."""
        rows = self._conn.execute(
            """
            SELECT id, path FROM wallpapers
            WHERE content_hash IS NULL AND status = 'ok' AND (file_size, quick_hash) IN (
                SELECT file_size, quick_hash FROM wallpapers
                WHERE status = 'ok' AND quick_hash IS NOT NULL
                GROUP BY file_size, quick_hash HAVING COUNT(DISTINCT path) > 1)
            """
        ).fetchall()
        for row in rows:
            self._check_cancelled()
            try:
                digest = content_sha256(Path(row["path"]), self._cancel)
            except HashCancelled as cancelled:
                raise IndexCancelled() from cancelled
            except OSError:
                continue
            with transaction(self._conn):
                self._conn.execute(
                    "UPDATE wallpapers SET content_hash = ? WHERE id = ?",
                    (digest, row["id"]),
                )
