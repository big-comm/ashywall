"""Browsing an online provider: one search at a time, page by page.

Each new search gets a generation number; results that arrive for an older
generation are dropped, so typing quickly never shows stale pages.
"""

import threading
from collections.abc import Callable

from ..providers.base import (
    RemotePage,
    RemoteProvider,
    RemoteQuery,
    RemoteSort,
    RemoteWallpaper,
)
from ..services.library_repository import LibraryRepository
from .background_tasks import BackgroundRunner
from .gallery_page import GalleryFilters


class ExploreController:
    def __init__(
        self,
        provider: RemoteProvider,
        repository: LibraryRepository,
        on_page: Callable[[RemotePage, list[RemoteWallpaper], bool], None],
        on_error: Callable[[Exception, bool], None],
    ) -> None:
        self.provider = provider
        self._repository = repository
        self._on_page = on_page
        self._on_error = on_error
        self._runner = BackgroundRunner(1, "ashywall-explore")
        self._generation = 0
        self._cancel = threading.Event()
        self._query: RemoteQuery | None = None
        self._last: RemotePage | None = None
        self.loading = False

    @property
    def has_more(self) -> bool:
        return self._last is not None and self._last.has_more

    @property
    def last_page(self) -> RemotePage | None:
        return self._last

    def set_provider(self, provider: RemoteProvider) -> None:
        if provider is not self.provider:
            self.stop()
            self.provider = provider
            self._query = None
            self._last = None

    def search(self, filters: GalleryFilters, sort: RemoteSort) -> None:
        self._cancel.set()
        self._cancel = threading.Event()
        self._generation += 1
        self._last = None
        self._query = RemoteQuery(
            text=filters.text,
            sort=sort,
            orientation=filters.orientation,
            min_tier=filters.min_tier,
        )
        self._fetch(self._query, append=False)

    def load_more(self) -> bool:
        if (
            self.loading
            or self._query is None
            or self._last is None
            or not self._last.has_more
        ):
            return False
        query = RemoteQuery(
            text=self._query.text,
            sort=self._query.sort,
            orientation=self._query.orientation,
            min_tier=self._query.min_tier,
            page=self._last.page + 1,
            seed=self._last.seed,
        )
        self._fetch(query, append=True)
        return True

    def retry(self) -> None:
        if self._query is not None:
            self._generation += 1
            self._last = None
            self._fetch(self._query, append=False)

    def stop(self) -> None:
        self._cancel.set()
        self._generation += 1
        self.loading = False

    def shutdown(self) -> None:
        self.stop()
        self._runner.shutdown()

    def enrich(self, items: list[RemoteWallpaper]) -> list[RemoteWallpaper]:
        """Mark favorites and downloads known to the local catalog."""
        return self._repository.with_local_state(items)

    def _fetch(self, query: RemoteQuery, append: bool) -> None:
        generation = self._generation
        cancel = self._cancel
        self.loading = True

        def done(page: RemotePage) -> None:
            if generation != self._generation:
                return
            self.loading = False
            self._last = page
            self._on_page(page, self.enrich(page.items), append)

        def failed(error: Exception) -> None:
            if generation != self._generation:
                return
            self.loading = False
            self._on_error(error, append)

        provider = self.provider
        self._runner.submit(lambda: provider.search(query, cancel), done, failed)
