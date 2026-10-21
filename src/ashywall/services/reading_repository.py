"""Independent, bounded article cache in the application's SQLite catalog."""

import sqlite3
import time
from dataclasses import dataclass

from .database import transaction
from .feeds import FeedDocument

MAX_FEEDS = 50
ARTICLES_PER_FEED = 500
PAGE_SIZE = 60


class ReadingError(Exception):
    """A user-correctable source configuration error, identified by its code."""


@dataclass(frozen=True, slots=True)
class ReadingFeed:
    id: int
    url: str
    title: str
    category: str
    checked_at: int | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class Article:
    id: int
    feed_id: int
    feed_title: str
    category: str
    title: str
    url: str
    summary: str
    published_at: int | None
    is_read: bool


@dataclass(frozen=True, slots=True)
class ReadingQuery:
    category: str = ""
    text: str = ""
    unread_only: bool = False


class ReadingRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def feeds(self) -> list[ReadingFeed]:
        return [
            ReadingFeed(**dict(row))
            for row in self.conn.execute(
                "SELECT id,url,title,category,checked_at,last_error FROM reading_feeds ORDER BY title COLLATE NOCASE,id"
            )
        ]

    def add(self, url: str, category: str, document: FeedDocument) -> int:
        category = self._category(category)
        with transaction(self.conn):
            if self.conn.execute(
                "SELECT 1 FROM reading_feeds WHERE url=?", (url,)
            ).fetchone():
                raise ReadingError("duplicate")
            if (
                self.conn.execute("SELECT COUNT(*) FROM reading_feeds").fetchone()[0]
                >= MAX_FEEDS
            ):
                raise ReadingError("too-many-feeds")
            cursor = self.conn.execute(
                "INSERT INTO reading_feeds(url,title,category,added_at) VALUES (?,?,?,?)",
                (url, document.title, category, int(time.time())),
            )
            feed_id = cursor.lastrowid
            self._store(feed_id, document)
        return feed_id

    def store(self, feed_id: int, document: FeedDocument) -> None:
        with transaction(self.conn):
            if self.conn.execute(
                "SELECT 1 FROM reading_feeds WHERE id=?", (feed_id,)
            ).fetchone():
                self._store(feed_id, document)

    def _store(self, feed_id: int, document: FeedDocument) -> None:
        now = int(time.time())
        self.conn.execute(
            "UPDATE reading_feeds SET title=?,checked_at=?,last_error=NULL WHERE id=?",
            (document.title, now, feed_id),
        )
        self.conn.executemany(
            """INSERT INTO reading_articles(feed_id,entry_key,title,url,summary,published_at,seen_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(feed_id,entry_key) DO UPDATE SET
            title=excluded.title,url=excluded.url,summary=excluded.summary,published_at=excluded.published_at""",
            (
                (feed_id, e.key, e.title, e.url, e.summary, e.published_at, now)
                for e in document.entries
            ),
        )
        self.conn.execute(
            """DELETE FROM reading_articles WHERE feed_id=? AND id NOT IN
            (SELECT id FROM reading_articles WHERE feed_id=?
            ORDER BY COALESCE(published_at,seen_at) DESC,id DESC LIMIT ?)""",
            (feed_id, feed_id, ARTICLES_PER_FEED),
        )

    def record_error(self, feed_id: int, reason: str) -> None:
        self.conn.execute(
            "UPDATE reading_feeds SET last_error=? WHERE id=?", (reason, feed_id)
        )

    def edit(self, feed_id: int, category: str) -> None:
        category = self._category(category)
        self.conn.execute(
            "UPDATE reading_feeds SET category=? WHERE id=?", (category, feed_id)
        )

    def _category(self, category: str) -> str:
        category = " ".join(category.split())
        if len(category) > 60:
            raise ReadingError("category-too-long")
        # At most 50 sources: Unicode casefold keeps manually entered variants
        # such as "Tecnologia" and "tecnologia" in the same visible category.
        for row in self.conn.execute("SELECT category FROM reading_feeds ORDER BY id"):
            if row[0].casefold() == category.casefold():
                return row[0]
        return category

    def remove(self, feed_id: int) -> None:
        self.conn.execute("DELETE FROM reading_feeds WHERE id=?", (feed_id,))

    def mark_read(self, article_id: int, read: bool) -> None:
        self.conn.execute(
            "UPDATE reading_articles SET is_read=? WHERE id=?", (int(read), article_id)
        )

    def articles(
        self, query: ReadingQuery, offset: int = 0
    ) -> tuple[list[Article], bool]:
        clauses, args = [], []
        if query.category:
            clauses.append("f.category=?")
            args.append(query.category)
        if query.unread_only:
            clauses.append("a.is_read=0")
        if query.text.strip():
            text = (
                query.text.strip()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            clauses.append("(a.title LIKE ? ESCAPE '\\' OR f.title LIKE ? ESCAPE '\\')")
            args.extend([f"%{text}%"] * 2)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            """SELECT a.id,a.feed_id,f.title AS feed_title,f.category,a.title,a.url,
            a.summary,a.published_at,a.is_read FROM reading_articles a
            JOIN reading_feeds f ON f.id=a.feed_id"""
            + where
            + " ORDER BY COALESCE(a.published_at,a.seen_at) DESC,a.id DESC LIMIT ? OFFSET ?",
            (*args, PAGE_SIZE + 1, max(0, offset)),
        ).fetchall()
        return [Article(**dict(row)) for row in rows[:PAGE_SIZE]], len(rows) > PAGE_SIZE
