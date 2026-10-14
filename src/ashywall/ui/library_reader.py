"""Library queries in a worker thread, so the window never waits on SQLite.

The worker keeps its own connection (WAL lets it read while the main thread
and the indexer write). Only the most recent request delivers its result:
typing a search quickly skips the queries it has already outdated.
"""

import threading
from collections.abc import Callable
from pathlib import Path

from ..services.database import connect
from ..services.library_repository import LibraryQuery, LibraryRepository
from .background_tasks import BackgroundRunner


class _Superseded(Exception):
    pass


class LibraryReader:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._runner = BackgroundRunner(1, "ashywall-library")
        self._local = threading.local()
        # Changed on the main thread only; the worker just compares it.
        self._generation = 0

    def _repository(self) -> LibraryRepository:
        repository = getattr(self._local, "repository", None)
        if repository is None:
            repository = LibraryRepository(connect(self._database_path))
            self._local.repository = repository
        return repository

    def wallpaper_ids(
        self,
        query: LibraryQuery,
        on_done: Callable[[list[int]], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        self._generation += 1
        generation = self._generation

        def work() -> list[int]:
            if generation != self._generation:
                raise _Superseded()
            return self._repository().wallpaper_ids(query)

        def done(ids: list[int]) -> None:
            if generation == self._generation:
                on_done(ids)

        def failed(error: Exception) -> None:
            if generation == self._generation and not isinstance(error, _Superseded):
                on_error(error)

        self._runner.submit(work, done, failed)

    def cancel(self) -> None:
        """Drop the pending result (the window switched to an online source)."""
        self._generation += 1

    def shutdown(self) -> None:
        self.cancel()
        self._runner.shutdown()
