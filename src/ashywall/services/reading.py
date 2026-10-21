"""Worker-side reading operations. Only explicit add/refresh actions use the network."""

import threading
from contextlib import closing
from pathlib import Path

from .database import connect
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
        url = feed_url(url)
        if len(" ".join(category.split())) > 60:
            raise ReadingError("category-too-long")
        with closing(connect(self.database_path)) as conn:
            feeds = ReadingRepository(conn).feeds()
            if any(feed.url == url for feed in feeds):
                raise ReadingError("duplicate")
            if len(feeds) >= MAX_FEEDS:
                raise ReadingError("too-many-feeds")
        document = parse_feed(
            self.http.get_bytes(url, max_bytes=MAX_FEED_BYTES, cancel=cancel), url
        )
        if cancel.is_set():
            raise TransferCancelled()
        with closing(connect(self.database_path)) as conn:
            return ReadingRepository(conn).add(url, category, document)

    def refresh(self, cancel: threading.Event) -> int:
        failures = 0
        with closing(connect(self.database_path)) as conn:
            repo = ReadingRepository(conn)
            for feed in repo.feeds():
                if cancel.is_set():
                    raise TransferCancelled()
                try:
                    url = feed_url(feed.url)
                    document = parse_feed(
                        self.http.get_bytes(
                            url, max_bytes=MAX_FEED_BYTES, cancel=cancel
                        ),
                        url,
                    )
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
