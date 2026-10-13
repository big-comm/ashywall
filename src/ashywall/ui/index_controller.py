"""Runs the library indexer in a worker thread with its own connection."""

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .. import paths
from ..services.database import connect
from ..services.library_index import (
    IndexCancelled,
    IndexProgress,
    IndexSummary,
    LibraryIndexer,
)
from .background_tasks import run_on_main

PROGRESS_INTERVAL = 0.25
log = logging.getLogger(__name__)


class IndexController:
    def __init__(
        self,
        database_path: Path,
        on_progress: Callable[[IndexProgress], None],
        on_finished: Callable[[IndexSummary], None],
        on_failed: Callable[[Exception], None],
    ) -> None:
        self._database_path = database_path
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._on_failed = on_failed
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._rerun_requested = False
        self._last_report = 0.0

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        """Index now, or once more after the current run if one is active."""
        if self._thread is not None:
            self._rerun_requested = True
            return
        self._cancel = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="ashywall-index", daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        self._rerun_requested = False
        self._cancel.set()

    def _report(self, progress: IndexProgress) -> None:
        now = time.monotonic()
        if (
            now - self._last_report >= PROGRESS_INTERVAL
            or progress.processed >= progress.total
        ):
            self._last_report = now
            run_on_main(self._on_progress, progress)

    def _run(self) -> None:
        conn = None
        try:
            conn = connect(self._database_path)
            summary = LibraryIndexer(
                conn,
                data_dirs=paths.system_data_dirs(),
                user_data_home=paths.user_data_home(),
                downloads_dir=paths.downloads_dir(),
                on_progress=self._report,
                cancel_event=self._cancel,
            ).run()
        except IndexCancelled:
            run_on_main(self._done, None)
        except Exception as error:
            log.exception("Indexing failed")
            run_on_main(self._failed, error)
        else:
            run_on_main(self._done, summary)
        finally:
            if conn is not None:
                conn.close()

    def _done(self, summary: IndexSummary | None) -> None:
        self._thread = None
        if summary is not None:
            self._on_finished(summary)
        self._maybe_rerun()

    def _failed(self, error: Exception) -> None:
        self._thread = None
        self._on_failed(error)
        self._maybe_rerun()

    def _maybe_rerun(self) -> None:
        if self._rerun_requested:
            self._rerun_requested = False
            self.start()
