"""Queries and small writes on the catalog used by the interface."""

import os
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..models.resolution import SQUARE_TOLERANCE, Orientation, ResolutionTier
from ..models.wallpaper import (
    LOCAL_ORIGIN,
    Collection,
    Source,
    SourceKind,
    Wallpaper,
)
from ..providers.base import ProviderError, RemoteWallpaper
from .database import transaction

SYSTEM_SOURCE_PATH = ""


class Section(StrEnum):
    ALL = "all"
    SYSTEM = "system"
    FOLDERS = "folders"
    DOWNLOADS = "downloads"
    FAVORITES = "favorites"
    COLLECTION = "collection"


class SortOrder(StrEnum):
    TITLE = "title"
    NEWEST = "newest"
    LARGEST = "largest"


class CollectionError(Exception):
    """A collection name is empty, too long or already used."""


class FolderProblem(StrEnum):
    NOT_A_FOLDER = "not-a-folder"
    ALREADY_COVERED = "already-covered"
    CONTAINS_EXISTING = "contains-existing"


class FolderError(Exception):
    """A folder cannot be added to the library."""

    def __init__(
        self, problem: FolderProblem, folder: Path, other: Path | None = None
    ) -> None:
        super().__init__(
            f"{problem.value}: {folder}" + (f" ({other})" if other else "")
        )
        self.problem = problem
        self.folder = folder
        self.other = other


@dataclass(frozen=True, slots=True)
class DownloadRecord:
    """Where a downloaded file came from, kept for attribution."""

    origin: str
    remote_id: str
    path: Path
    page_url: str | None
    source_url: str | None
    uploader: str | None
    license: str | None
    author: str | None = None
    license_url: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class LibraryQuery:
    section: Section = Section.ALL
    text: str = ""
    orientation: Orientation | None = None
    min_tier: ResolutionTier | None = None
    sort: SortOrder = SortOrder.TITLE
    collection_id: int | None = None

    @property
    def has_filters(self) -> bool:
        return (
            bool(self.text.strip())
            or self.orientation is not None
            or self.min_tier is not None
        )


_SECTION_KINDS: dict[Section, tuple[str, ...]] = {
    Section.ALL: ("system", "folder", "downloads"),
    Section.SYSTEM: ("system",),
    Section.FOLDERS: ("folder",),
    Section.DOWNLOADS: ("downloads",),
    Section.FAVORITES: ("system", "folder", "downloads"),
    Section.COLLECTION: ("system", "folder", "downloads"),
}

COLLECTION_NAME_LIMIT = 80

_ORIENTATION_SQL: dict[Orientation, str] = {
    Orientation.LANDSCAPE: f"w.width > w.height * {SQUARE_TOLERANCE}",
    Orientation.PORTRAIT: f"w.height > w.width * {SQUARE_TOLERANCE}",
    Orientation.SQUARE: (
        f"w.width <= w.height * {SQUARE_TOLERANCE} AND w.height <= w.width * {SQUARE_TOLERANCE}"
    ),
}

# Downloads show the title their source gave them rather than the file name.
_TITLE_SQL = (
    "COALESCE((SELECT d.title FROM downloads d WHERE d.path = w.path "
    "AND d.title IS NOT NULL ORDER BY d.downloaded_at DESC LIMIT 1), w.title)"
)

_ORDER_SQL: dict[SortOrder, str] = {
    SortOrder.TITLE: f"{_TITLE_SQL} COLLATE NOCASE, w.path",
    SortOrder.NEWEST: "w.mtime_ns DESC, w.path",
    SortOrder.LARGEST: "w.width * w.height DESC, w.path",
}

_WALLPAPER_COLUMNS = f"""
    w.id, w.source_id, s.kind AS source_kind, s.label AS source_label, w.path,
    {_TITLE_SQL} AS title,
    w.width, w.height, w.file_size, w.mtime_ns, w.image_format, w.dark_path,
    w.picture_options, (f.item_key IS NOT NULL) AS is_favorite
"""

_WALLPAPER_JOINS = f"""
    FROM wallpapers w
    JOIN sources s ON s.id = w.source_id
    LEFT JOIN favorites f ON f.origin = '{LOCAL_ORIGIN}' AND f.item_key = w.path
"""


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _where_clause(query: LibraryQuery) -> tuple[str, list[object]]:
    kinds = _SECTION_KINDS[query.section]
    placeholders = ", ".join("?" for _ in kinds)
    # The same file can belong to several sources (e.g. a personal folder
    # that overlaps a system folder); show it once.
    clauses = [
        "w.status = 'ok'",
        f"""w.id IN (
            SELECT MIN(w2.id) FROM wallpapers w2 JOIN sources s2 ON s2.id = w2.source_id
            WHERE w2.status = 'ok' AND s2.kind IN ({placeholders}) GROUP BY w2.path)""",
    ]
    params: list[object] = list(kinds)
    if query.section is Section.FAVORITES:
        clauses.append("f.item_key IS NOT NULL")
    if query.section is Section.COLLECTION:
        clauses.append(
            "w.path IN (SELECT item_key FROM collection_items "
            f"WHERE collection_id = ? AND origin = '{LOCAL_ORIGIN}')"
        )
        params.append(query.collection_id if query.collection_id is not None else -1)
    if text := query.text.strip():
        clauses.append(
            f"({_TITLE_SQL} LIKE ? ESCAPE '\\' OR w.path LIKE ? ESCAPE '\\')"
        )
        pattern = f"%{_escape_like(text)}%"
        params += [pattern, pattern]
    if query.orientation is not None:
        clauses.append(_ORIENTATION_SQL[query.orientation])
    if query.min_tier is not None:
        clauses.append("MAX(w.width, w.height) >= ? AND MIN(w.width, w.height) >= ?")
        params += [query.min_tier.long_side, query.min_tier.short_side]
    return " AND ".join(clauses), params


def _wallpaper_from_row(row: sqlite3.Row) -> Wallpaper:
    return Wallpaper(
        id=row["id"],
        source_id=row["source_id"],
        source_kind=SourceKind(row["source_kind"]),
        source_label=row["source_label"],
        path=Path(row["path"]),
        title=row["title"],
        width=row["width"],
        height=row["height"],
        file_size=row["file_size"],
        mtime_ns=row["mtime_ns"],
        image_format=row["image_format"],
        dark_path=Path(row["dark_path"]) if row["dark_path"] else None,
        picture_options=row["picture_options"],
        is_favorite=bool(row["is_favorite"]),
    )


def _source_from_row(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"],
        kind=SourceKind(row["kind"]),
        path=Path(row["path"]) if row["path"] else None,
        label=row["label"],
    )


class LibraryRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # Sources -------------------------------------------------------------

    def ensure_builtin_sources(self, downloads_dir: Path) -> None:
        now = int(time.time())
        with transaction(self._conn):
            self._conn.execute(
                "INSERT OR IGNORE INTO sources (kind, path, label, added_at) VALUES ('system', ?, 'System', ?)",
                (SYSTEM_SOURCE_PATH, now),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO sources (kind, path, label, added_at) VALUES ('downloads', ?, 'Downloads', ?)",
                (str(downloads_dir), now),
            )

    def list_sources(self, kind: SourceKind | None = None) -> list[Source]:
        sql = "SELECT id, kind, path, label FROM sources"
        params: tuple[str, ...] = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            params = (kind.value,)
        rows = self._conn.execute(sql + " ORDER BY label COLLATE NOCASE, path", params)
        return [_source_from_row(row) for row in rows]

    def add_folder(self, folder: Path) -> Source:
        folder = Path(os.path.normpath(os.path.abspath(folder)))
        if not folder.is_absolute() or not folder.is_dir():
            raise FolderError(FolderProblem.NOT_A_FOLDER, folder)
        for existing in self.list_sources(SourceKind.FOLDER):
            assert existing.path is not None
            if folder == existing.path or folder.is_relative_to(existing.path):
                raise FolderError(FolderProblem.ALREADY_COVERED, folder, existing.path)
            if existing.path.is_relative_to(folder):
                raise FolderError(
                    FolderProblem.CONTAINS_EXISTING, folder, existing.path
                )
        with transaction(self._conn):
            cursor = self._conn.execute(
                "INSERT INTO sources (kind, path, label, added_at) VALUES ('folder', ?, ?, ?)",
                (str(folder), folder.name or str(folder), int(time.time())),
            )
        return Source(
            id=cursor.lastrowid or 0,
            kind=SourceKind.FOLDER,
            path=folder,
            label=folder.name,
        )

    def remove_folder(self, source_id: int) -> None:
        """Forget a personal folder. Files on disk are never touched."""
        with transaction(self._conn):
            self._conn.execute(
                "DELETE FROM sources WHERE id = ? AND kind = 'folder'", (source_id,)
            )

    # Wallpapers ----------------------------------------------------------

    def list_wallpapers(self, query: LibraryQuery) -> list[Wallpaper]:
        where, params = _where_clause(query)
        sql = f"SELECT {_WALLPAPER_COLUMNS} {_WALLPAPER_JOINS} WHERE {where} ORDER BY {_ORDER_SQL[query.sort]}"
        return [_wallpaper_from_row(row) for row in self._conn.execute(sql, params)]

    def count_wallpapers(self, query: LibraryQuery) -> int:
        where, params = _where_clause(query)
        return self._conn.execute(
            f"SELECT COUNT(*) {_WALLPAPER_JOINS} WHERE {where}", params
        ).fetchone()[0]

    def count_by_section(self) -> dict[Section, int]:
        counts = {
            section: self.count_wallpapers(LibraryQuery(section=section))
            for section in Section
            if section is not Section.COLLECTION
        }
        counts[Section.FAVORITES] += self._conn.execute(
            "SELECT COUNT(*) FROM favorites WHERE origin != ? AND metadata IS NOT NULL",
            (LOCAL_ORIGIN,),
        ).fetchone()[0]
        return counts

    def count_invalid(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(DISTINCT path) FROM wallpapers WHERE status = 'invalid'"
        ).fetchone()[0]

    def get_wallpaper(self, wallpaper_id: int) -> Wallpaper | None:
        row = self._conn.execute(
            f"SELECT {_WALLPAPER_COLUMNS} {_WALLPAPER_JOINS} WHERE w.id = ?",
            (wallpaper_id,),
        ).fetchone()
        return _wallpaper_from_row(row) if row else None

    def duplicates_of(self, wallpaper: Wallpaper) -> list[Path]:
        """Other paths with byte-identical content (SHA-256 confirmed)."""
        rows = self._conn.execute(
            """
            SELECT DISTINCT other.path FROM wallpapers me
            JOIN wallpapers other ON other.content_hash = me.content_hash
            WHERE me.id = ? AND me.content_hash IS NOT NULL
              AND other.path != me.path AND other.status = 'ok'
            ORDER BY other.path
            """,
            (wallpaper.id,),
        )
        return [Path(row["path"]) for row in rows]

    def mark_invalid(self, wallpaper_id: int, error: str) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE wallpapers SET status = 'invalid', error = ? WHERE id = ?",
                (error[:500], wallpaper_id),
            )

    # Favorites -----------------------------------------------------------

    def set_favorite(self, wallpaper: Wallpaper, favorite: bool) -> None:
        origin, key = wallpaper.favorite_key
        with transaction(self._conn):
            if favorite:
                self._conn.execute(
                    "INSERT OR IGNORE INTO favorites (origin, item_key, title, added_at) VALUES (?, ?, ?, ?)",
                    (origin, key, wallpaper.title, int(time.time())),
                )
            else:
                self._conn.execute(
                    "DELETE FROM favorites WHERE origin = ? AND item_key = ?",
                    (origin, key),
                )

    def set_remote_favorite(self, item: RemoteWallpaper, favorite: bool) -> None:
        origin, key = item.favorite_key
        with transaction(self._conn):
            if favorite:
                self._conn.execute(
                    "INSERT OR REPLACE INTO favorites (origin, item_key, title, added_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (origin, key, item.title, int(time.time()), item.to_json()),
                )
            else:
                self._conn.execute(
                    "DELETE FROM favorites WHERE origin = ? AND item_key = ?",
                    (origin, key),
                )

    def remote_favorite_ids(self, origin: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT item_key FROM favorites WHERE origin = ?", (origin,)
        )
        return {row["item_key"] for row in rows}

    def with_local_state(self, items: list[RemoteWallpaper]) -> list[RemoteWallpaper]:
        """Mark favorites and existing downloads, whatever provider listed the items."""
        favorites: dict[str, set[str]] = {}
        downloads: dict[str, dict[str, Path]] = {}
        marked = []
        for item in items:
            if item.origin not in favorites:
                favorites[item.origin] = self.remote_favorite_ids(item.origin)
                downloads[item.origin] = self.downloaded_paths(item.origin)
            marked.append(
                item.with_state(
                    is_favorite=item.remote_id in favorites[item.origin],
                    local_path=downloads[item.origin].get(item.remote_id),
                )
            )
        return marked

    def remote_favorites(self) -> list[RemoteWallpaper]:
        """Remote favorites, most recent first, from their stored listings."""
        return self._remote_items(
            "SELECT metadata FROM favorites WHERE origin != ? AND metadata IS NOT NULL "
            "ORDER BY added_at DESC",
            (LOCAL_ORIGIN,),
        )

    def _remote_items(
        self, sql: str, params: tuple[object, ...]
    ) -> list[RemoteWallpaper]:
        items = []
        for row in self._conn.execute(sql, params):
            try:
                items.append(RemoteWallpaper.from_json(row["metadata"]))
            except (ProviderError, ValueError, TypeError):
                continue
        return items

    # Collections ---------------------------------------------------------

    def _validated_name(self, name: str, exclude_id: int | None = None) -> str:
        name = " ".join(name.split())
        if not name or len(name) > COLLECTION_NAME_LIMIT:
            raise CollectionError(name)
        clash = self._conn.execute(
            "SELECT id FROM collections WHERE name = ? COLLATE NOCASE AND id != ?",
            (name, exclude_id if exclude_id is not None else -1),
        ).fetchone()
        if clash is not None:
            raise CollectionError(name)
        return name

    def create_collection(self, name: str) -> Collection:
        name = self._validated_name(name)
        with transaction(self._conn):
            cursor = self._conn.execute(
                "INSERT INTO collections (name, created_at) VALUES (?, ?)",
                (name, int(time.time())),
            )
        return Collection(id=cursor.lastrowid or 0, name=name, count=0)

    def rename_collection(self, collection_id: int, name: str) -> None:
        name = self._validated_name(name, exclude_id=collection_id)
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE collections SET name = ? WHERE id = ?", (name, collection_id)
            )

    def delete_collection(self, collection_id: int) -> None:
        """Remove the collection only; its wallpapers stay where they are."""
        with transaction(self._conn):
            self._conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))

    def list_collections(self) -> list[Collection]:
        rows = self._conn.execute(
            f"""
            SELECT c.id, c.name,
                (SELECT COUNT(DISTINCT w.path) FROM collection_items ci
                 JOIN wallpapers w ON w.path = ci.item_key AND w.status = 'ok'
                 WHERE ci.collection_id = c.id AND ci.origin = '{LOCAL_ORIGIN}')
              + (SELECT COUNT(*) FROM collection_items ci
                 WHERE ci.collection_id = c.id AND ci.origin != '{LOCAL_ORIGIN}'
                   AND ci.metadata IS NOT NULL) AS count
            FROM collections c ORDER BY c.name COLLATE NOCASE
            """
        )
        return [Collection(row["id"], row["name"], row["count"]) for row in rows]

    def set_in_collection(
        self,
        collection_id: int,
        entry: Wallpaper | RemoteWallpaper,
        member: bool,
    ) -> None:
        origin, key = entry.favorite_key
        metadata = entry.to_json() if isinstance(entry, RemoteWallpaper) else None
        with transaction(self._conn):
            if member:
                self._conn.execute(
                    "INSERT OR REPLACE INTO collection_items "
                    "(collection_id, origin, item_key, title, metadata, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        collection_id,
                        origin,
                        key,
                        entry.title,
                        metadata,
                        int(time.time()),
                    ),
                )
            else:
                self._conn.execute(
                    "DELETE FROM collection_items "
                    "WHERE collection_id = ? AND origin = ? AND item_key = ?",
                    (collection_id, origin, key),
                )

    def collections_containing(self, entry: Wallpaper | RemoteWallpaper) -> set[int]:
        origin, key = entry.favorite_key
        rows = self._conn.execute(
            "SELECT collection_id FROM collection_items WHERE origin = ? AND item_key = ?",
            (origin, key),
        )
        return {row["collection_id"] for row in rows}

    def remote_collection_items(self, collection_id: int) -> list[RemoteWallpaper]:
        return self._remote_items(
            "SELECT metadata FROM collection_items WHERE collection_id = ? "
            "AND origin != ? AND metadata IS NOT NULL ORDER BY added_at DESC",
            (collection_id, LOCAL_ORIGIN),
        )

    # Downloads -----------------------------------------------------------

    def record_download(self, item: RemoteWallpaper, path: Path) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "INSERT OR REPLACE INTO downloads (origin, remote_id, path, page_url, "
                "source_url, uploader, license, author, license_url, title, downloaded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.origin,
                    item.remote_id,
                    str(path),
                    item.page_url,
                    item.source_url,
                    item.uploader,
                    item.license,
                    item.author,
                    item.license_url,
                    item.title,
                    int(time.time()),
                ),
            )

    def downloaded_paths(self, origin: str) -> dict[str, Path]:
        """Remote id -> local file, for downloads whose file still exists."""
        rows = self._conn.execute(
            "SELECT remote_id, path FROM downloads WHERE origin = ?", (origin,)
        )
        paths = {row["remote_id"]: Path(row["path"]) for row in rows}
        return {key: path for key, path in paths.items() if path.is_file()}

    def download_record_for(self, path: Path) -> DownloadRecord | None:
        row = self._conn.execute(
            "SELECT * FROM downloads WHERE path = ? ORDER BY downloaded_at DESC LIMIT 1",
            (str(path),),
        ).fetchone()
        if row is None:
            return None
        return DownloadRecord(
            origin=row["origin"],
            remote_id=row["remote_id"],
            path=Path(row["path"]),
            page_url=row["page_url"],
            source_url=row["source_url"],
            uploader=row["uploader"],
            license=row["license"],
            author=row["author"],
            license_url=row["license_url"],
            title=row["title"],
        )

    def title_for_path(self, path: Path) -> str | None:
        """The title to show for a file: its online listing if downloaded, else the library's."""
        record = self.download_record_for(path)
        if record is not None and record.title:
            return record.title
        if record is not None:
            # Downloads recorded before titles were kept: use a stored listing.
            for table in ("favorites", "collection_items"):
                row = self._conn.execute(
                    f"SELECT metadata FROM {table} WHERE origin = ? AND item_key = ? "
                    "AND metadata IS NOT NULL LIMIT 1",
                    (record.origin, record.remote_id),
                ).fetchone()
                if row is not None:
                    try:
                        return RemoteWallpaper.from_json(row["metadata"]).title
                    except (ProviderError, ValueError, TypeError):
                        pass
        row = self._conn.execute(
            "SELECT title FROM wallpapers WHERE path = ? LIMIT 1", (str(path),)
        ).fetchone()
        return row["title"] if row else None

    # Settings ------------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        with transaction(self._conn):
            if value is None:
                self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    # Desktop history -----------------------------------------------------

    def push_desktop_history(self, applied_uri: str, previous_state: str) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO desktop_history (applied_at, applied_uri, previous_state) VALUES (?, ?, ?)",
                (int(time.time()), applied_uri, previous_state),
            )

    def peek_desktop_history(self) -> tuple[int, str] | None:
        row = self._conn.execute(
            "SELECT id, previous_state FROM desktop_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return (row["id"], row["previous_state"]) if row else None

    def drop_desktop_history(self, entry_id: int) -> None:
        with transaction(self._conn):
            self._conn.execute("DELETE FROM desktop_history WHERE id = ?", (entry_id,))
