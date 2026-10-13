"""Downloads of originals in worker threads, with progress and cancellation.

Downloads keep running when the user leaves the detail page; pages attach a
listener while visible and detach when hidden.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..providers.base import RemoteWallpaper
from ..services.http import HttpClient, TransferCancelled
from ..services.remote_images import download_original
from .background_tasks import BackgroundRunner, run_on_main

PROGRESS_INTERVAL = 0.1
log = logging.getLogger(__name__)

DownloadKey = tuple[str, str]


class DownloadListener(Protocol):
    def on_download_progress(self, received: int, total: int | None) -> None: ...
    def on_download_finished(self, path: Path) -> None: ...
    def on_download_failed(self, error: Exception) -> None: ...
    def on_download_cancelled(self) -> None: ...


@dataclass
class _ActiveDownload:
    item: RemoteWallpaper
    cancel: threading.Event = field(default_factory=threading.Event)
    received: int = 0
    total: int | None = None
    last_report: float = 0.0


class DownloadManager:
    def __init__(
        self,
        http: HttpClient,
        target_dir: Callable[[], Path],
        allowed_hosts: Callable[[str], frozenset[str]],
        on_finished: Callable[[RemoteWallpaper, Path], None],
        on_failed: Callable[[RemoteWallpaper, Exception], None],
        cached_original: Callable[[RemoteWallpaper], Path | None] = lambda _item: None,
    ) -> None:
        self._http = http
        self._target_dir = target_dir
        self._allowed_hosts = allowed_hosts
        self._on_finished = on_finished
        self._on_failed = on_failed
        self._cached_original = cached_original
        self._runner = BackgroundRunner(2, "ashywall-downloads")
        self._active: dict[DownloadKey, _ActiveDownload] = {}
        self._listeners: dict[DownloadKey, list[DownloadListener]] = {}

    def is_active(self, key: DownloadKey) -> bool:
        return key in self._active

    def progress(self, key: DownloadKey) -> tuple[int, int | None] | None:
        active = self._active.get(key)
        return (active.received, active.total) if active else None

    def add_listener(self, key: DownloadKey, listener: DownloadListener) -> None:
        self._listeners.setdefault(key, []).append(listener)

    def remove_listener(self, key: DownloadKey, listener: DownloadListener) -> None:
        listeners = self._listeners.get(key, [])
        if listener in listeners:
            listeners.remove(listener)

    def start(self, item: RemoteWallpaper) -> None:
        key = item.favorite_key
        if key in self._active:
            return
        active = _ActiveDownload(item)
        self._active[key] = active
        target_dir = self._target_dir()
        hosts = self._allowed_hosts(item.origin)

        def report(received: int, total: int | None) -> None:
            active.received, active.total = received, total
            now = time.monotonic()
            if now - active.last_report >= PROGRESS_INTERVAL:
                active.last_report = now
                run_on_main(self._notify_progress, key, received, total)

        def work() -> Path:
            return download_original(
                item,
                self._http,
                target_dir,
                hosts,
                on_progress=report,
                cancel=active.cancel,
                # A full-resolution preview already fetched is reused.
                source=self._cached_original(item),
            )

        self._runner.submit(
            work,
            lambda path: self._finished(key, path),
            lambda error: self._failed(key, error),
        )

    def cancel(self, key: DownloadKey) -> None:
        active = self._active.get(key)
        if active is not None:
            active.cancel.set()

    def shutdown(self) -> None:
        for active in self._active.values():
            active.cancel.set()
        self._runner.shutdown()

    def _listeners_for(self, key: DownloadKey) -> list[DownloadListener]:
        return list(self._listeners.get(key, []))

    def _notify_progress(
        self, key: DownloadKey, received: int, total: int | None
    ) -> None:
        for listener in self._listeners_for(key):
            listener.on_download_progress(received, total)

    def _finished(self, key: DownloadKey, path: Path) -> None:
        active = self._active.pop(key)
        self._on_finished(active.item, path)
        for listener in self._listeners_for(key):
            listener.on_download_finished(path)

    def _failed(self, key: DownloadKey, error: Exception) -> None:
        active = self._active.pop(key)
        if isinstance(error, TransferCancelled):
            for listener in self._listeners_for(key):
                listener.on_download_cancelled()
            return
        log.warning("Download of %s failed: %s", key, error)
        self._on_failed(active.item, error)
        for listener in self._listeners_for(key):
            listener.on_download_failed(error)
