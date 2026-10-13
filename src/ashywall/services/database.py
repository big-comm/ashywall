"""SQLite catalog: connection setup and explicit, versioned migrations.

Each thread opens its own connection (the indexer runs in a worker thread,
the interface reads on the main thread); WAL mode lets them coexist.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Migration N upgrades the schema from version N-1 to N. Never edit a
# released migration: append a new one instead.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('system', 'folder', 'downloads')),
            path TEXT NOT NULL,
            label TEXT NOT NULL,
            added_at INTEGER NOT NULL,
            UNIQUE (kind, path)
        )
        """,
        """
        CREATE TABLE wallpapers (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            title TEXT NOT NULL,
            dark_path TEXT,
            picture_options TEXT,
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            image_format TEXT,
            file_size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            quick_hash TEXT,
            content_hash TEXT,
            status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'invalid')),
            error TEXT,
            indexed_at INTEGER NOT NULL,
            UNIQUE (source_id, path)
        )
        """,
        "CREATE INDEX wallpapers_by_path ON wallpapers(path)",
        "CREATE INDEX wallpapers_by_quick_hash ON wallpapers(file_size, quick_hash)",
        "CREATE INDEX wallpapers_by_content_hash ON wallpapers(content_hash)",
        """
        CREATE TABLE favorites (
            origin TEXT NOT NULL,
            item_key TEXT NOT NULL,
            title TEXT,
            added_at INTEGER NOT NULL,
            PRIMARY KEY (origin, item_key)
        )
        """,
        """
        CREATE TABLE desktop_history (
            id INTEGER PRIMARY KEY,
            applied_at INTEGER NOT NULL,
            applied_uri TEXT NOT NULL,
            previous_state TEXT NOT NULL
        )
        """,
    ),
    (
        # Remote favorites keep the provider's listing so they display offline.
        "ALTER TABLE favorites ADD COLUMN metadata TEXT",
        """
        CREATE TABLE downloads (
            origin TEXT NOT NULL,
            remote_id TEXT NOT NULL,
            path TEXT NOT NULL,
            page_url TEXT,
            source_url TEXT,
            uploader TEXT,
            license TEXT,
            downloaded_at INTEGER NOT NULL,
            PRIMARY KEY (origin, remote_id)
        )
        """,
        "CREATE INDEX downloads_by_path ON downloads(path)",
    ),
    (
        """
        CREATE TABLE collections (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX collections_by_name ON collections(name COLLATE NOCASE)",
        # Items are keyed like favorites (origin + path or remote id) so they
        # survive re-indexing; remote items keep their listing in metadata.
        """
        CREATE TABLE collection_items (
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            origin TEXT NOT NULL,
            item_key TEXT NOT NULL,
            title TEXT,
            metadata TEXT,
            added_at INTEGER NOT NULL,
            PRIMARY KEY (collection_id, origin, item_key)
        )
        """,
    ),
    (
        # Small application state (automatic change settings), stored as JSON text.
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    ),
    (
        # Sources that name the author and link the license (Wikimedia Commons).
        "ALTER TABLE downloads ADD COLUMN author TEXT",
        "ALTER TABLE downloads ADD COLUMN license_url TEXT",
    ),
)

LATEST_VERSION = len(MIGRATIONS)


class DatabaseError(Exception):
    pass


def connect(path: Path) -> sqlite3.Connection:
    """Open the catalog, creating or migrating it as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, autocommit=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    if schema_version(conn) > LATEST_VERSION:
        raise DatabaseError(
            f"Catalog schema {schema_version(conn)} is newer than this version supports ({LATEST_VERSION})"
        )
    for version in range(1, LATEST_VERSION + 1):
        with transaction(conn):
            # Re-check inside the lock: another connection may have migrated.
            if schema_version(conn) >= version:
                continue
            for statement in MIGRATIONS[version - 1]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
