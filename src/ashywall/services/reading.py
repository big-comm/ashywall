"""Worker-side reading operations. Only explicit add/refresh actions use the network."""

import threading
from contextlib import closing
from pathlib import Path

from .database import connect
from .feed_discovery import FeedDiscoveryError, discover_feeds, known_feed, source_url
from .feeds import MAX_FEED_BYTES, FeedError, feed_url, parse_feed
from .http import HttpClient, HttpError, TransferCancelled
from .reading_repository import MAX_FEEDS, ReadingError, ReadingQuery, ReadingRepository


class ReadingService:
    def __init__(self, database_path: Path, http: HttpClient | None = None) -> None:
        self.database_path = database_path
        self.http = http or HttpClient(timeout=10, public_only=True)

    def snapshot(self, query: ReadingQuery, offset: int = 0):
        with closing(connect(self.database_path)) as conn:
            repo = ReadingRepository(conn)
            articles, more = repo.articles(query, offset)
            return repo.feeds(), articles, more

    def add(self, url: str, category: str, cancel: threading.Event):
        url = known_feed(source_url(url))
        if len(" ".join(category.split())) > 60:
            raise ReadingError("category-too-long")
        with closing(connect(self.database_path)) as conn:
            feeds = ReadingRepository(conn).feeds()
            if any(feed.url == url for feed in feeds):
                raise ReadingError("duplicate")
            if len(feeds) >= MAX_FEEDS:
                raise ReadingError("too-many-feeds")
        url, document = self._load_source(url, cancel)
        if cancel.is_set():
            raise TransferCancelled()
        with closing(connect(self.database_path)) as conn:
            return ReadingRepository(conn).add(url, category, document)

    def _load_source(self, url: str, cancel: threading.Event):
        body, url = self.http.get_document(url, max_bytes=MAX_FEED_BYTES, cancel=cancel)
        try:
            return url, parse_feed(body, url)
        except FeedError as error:
            candidates = discover_feeds(body, url)
            if not candidates:
                raise FeedDiscoveryError("No supported feed advertised") from error
        for candidate in candidates:
            if cancel.is_set():
                raise TransferCancelled()
            try:
                body, effective_url = self.http.get_document(
                    candidate, max_bytes=MAX_FEED_BYTES, cancel=cancel
                )
                document = parse_feed(body, effective_url)
            except (HttpError, FeedError):
                continue
            return effective_url, document
        raise FeedDiscoveryError("Advertised feeds could not be read")

    def refresh(self, cancel: threading.Event) -> int:
        failures = 0
        with closing(connect(self.database_path)) as conn:
            repo = ReadingRepository(conn)
            for feed in repo.feeds():
                if cancel.is_set():
                    raise TransferCancelled()
                try:
                    url = feed_url(feed.url)
                    body, effective_url = self.http.get_document(
                        url, max_bytes=MAX_FEED_BYTES, cancel=cancel
                    )
                    document = parse_feed(body, effective_url)
                    if cancel.is_set():
                        raise TransferCancelled()
                    repo.store(feed.id, document)
                except TransferCancelled:
                    raise
                except (OSError, ValueError, FeedError, HttpError) as error:
                    # One failing source must not prevent others from updating.
                    # Store only a class name, not server text or URL query data.
                    repo.record_error(feed.id, type(error).__name__)
                    failures += 1
        return failures

    def mutate(self, method: str, *args) -> None:
        with closing(connect(self.database_path)) as conn:
            repo = ReadingRepository(conn)
            {"remove": repo.remove, "edit": repo.edit, "mark_read": repo.mark_read}[
                method
            ](*args)
