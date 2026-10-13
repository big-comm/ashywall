import os
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest
from conftest import make_image

from ashywall.models.resolution import UHD_4K, UHD_8K, Orientation
from ashywall.models.wallpaper import SourceKind
from ashywall.services import database
from ashywall.services.database import LATEST_VERSION, connect, schema_version
from ashywall.services.image_probe import probe_image
from ashywall.services.library_index import IndexCancelled, LibraryIndexer
from ashywall.services.library_repository import (
    FolderError,
    LibraryQuery,
    LibraryRepository,
    Section,
    SortOrder,
)


def indexer_for(
    conn: sqlite3.Connection, layout: dict[str, Path], **kwargs
) -> LibraryIndexer:
    return LibraryIndexer(
        conn,
        data_dirs=[layout["data_dir"]],
        user_data_home=layout["user_data_home"],
        downloads_dir=layout["downloads"],
        **kwargs,
    )


def titles(repository: LibraryRepository, **query) -> list[str]:
    return [w.title for w in repository.list_wallpapers(LibraryQuery(**query))]


def test_schema_is_created_and_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    first = connect(path)
    assert schema_version(first) == LATEST_VERSION
    first.close()
    second = connect(path)
    assert schema_version(second) == LATEST_VERSION
    second.close()


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    conn = connect(path)
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    conn.close()
    with pytest.raises(database.DatabaseError):
        connect(path)


def test_index_system_folder_and_downloads(
    conn: sqlite3.Connection, layout: dict[str, Path]
) -> None:
    make_image(layout["data_dir"] / "backgrounds" / "sys.png", (64, 36))
    make_image(layout["downloads"] / "dl.jpg", (36, 64), format="JPEG")
    make_image(layout["pictures"] / "mine.webp", (50, 50))
    repository = LibraryRepository(conn)
    repository.ensure_builtin_sources(layout["downloads"])
    repository.add_folder(layout["pictures"])

    summary = indexer_for(conn, layout).run()

    assert summary.added == 3
    assert titles(repository, section=Section.SYSTEM) == ["Sys"]
    assert titles(repository, section=Section.DOWNLOADS) == ["Dl"]
    assert titles(repository, section=Section.FOLDERS) == ["Mine"]
    assert repository.count_by_section()[Section.ALL] == 3
    portrait = repository.list_wallpapers(LibraryQuery(section=Section.DOWNLOADS))[0]
    assert (portrait.width, portrait.height, portrait.orientation) == (
        36,
        64,
        Orientation.PORTRAIT,
    )


def test_unchanged_files_are_not_probed_again(
    conn: sqlite3.Connection, layout: dict[str, Path]
) -> None:
    image = make_image(layout["data_dir"] / "backgrounds" / "a.png", (64, 36))
    make_image(layout["data_dir"] / "backgrounds" / "b.png", (64, 36), color=(1, 2, 3))
    calls: list[Path] = []

    def counting_probe(path: Path):
        calls.append(path)
        return probe_image(path)

    indexer_for(conn, layout, probe=counting_probe).run()
    assert len(calls) == 2
    calls.clear()
    indexer_for(conn, layout, probe=counting_probe).run()
    assert calls == []

    make_image(image, (128, 72))
    os.utime(image, ns=(1, 1))  # Guarantee a different mtime.
    summary = indexer_for(conn, layout, probe=counting_probe).run()
    assert calls == [image]
    assert summary.updated == 1
    assert LibraryRepository(conn).list_wallpapers(LibraryQuery())[0].width == 128


def test_removed_files_leave_the_catalog_but_favorites_survive(
    conn: sqlite3.Connection, layout: dict[str, Path], tmp_path: Path
) -> None:
    image = make_image(layout["data_dir"] / "backgrounds" / "fav.png", (64, 36))
    repository = LibraryRepository(conn)
    indexer_for(conn, layout).run()
    repository.set_favorite(repository.list_wallpapers(LibraryQuery())[0], True)

    parked = tmp_path / "parked.png"
    shutil.move(image, parked)
    summary = indexer_for(conn, layout).run()
    assert summary.removed == 1
    assert repository.list_wallpapers(LibraryQuery(section=Section.FAVORITES)) == []

    shutil.move(parked, image)
    indexer_for(conn, layout).run()
    assert titles(repository, section=Section.FAVORITES) == ["Fav"]


def test_corrupted_images_are_marked_invalid_and_hidden(
    conn: sqlite3.Connection, layout: dict[str, Path]
) -> None:
    make_image(layout["data_dir"] / "backgrounds" / "good.png", (64, 36))
    (layout["data_dir"] / "backgrounds" / "bad.jpg").write_bytes(b"garbage" * 100)
    summary = indexer_for(conn, layout).run()
    repository = LibraryRepository(conn)
    assert summary.invalid == 1
    assert titles(repository) == ["Good"]
    assert repository.count_invalid() == 1


def test_exact_duplicates_are_detected_with_full_hash(
    conn: sqlite3.Connection, layout: dict[str, Path]
) -> None:
    original = make_image(layout["data_dir"] / "backgrounds" / "one.png", (300, 200))
    copy = layout["pictures"] / "copy-of-one.png"
    shutil.copyfile(original, copy)
    make_image(layout["pictures"] / "different.png", (300, 200), color=(200, 10, 10))
    repository = LibraryRepository(conn)
    repository.ensure_builtin_sources(layout["downloads"])
    repository.add_folder(layout["pictures"])
    indexer_for(conn, layout).run()

    by_title = {w.title: w for w in repository.list_wallpapers(LibraryQuery())}
    assert repository.duplicates_of(by_title["One"]) == [copy]
    assert repository.duplicates_of(by_title["Copy of one"]) == [original]
    assert repository.duplicates_of(by_title["Different"]) == []
    hashed = conn.execute(
        "SELECT COUNT(*) FROM wallpapers WHERE content_hash IS NOT NULL"
    ).fetchone()[0]
    assert hashed == 2  # The unique file was never read in full.


def test_resolution_orientation_and_text_filters(
    conn: sqlite3.Connection, layout: dict[str, Path]
) -> None:
    backgrounds = layout["data_dir"] / "backgrounds"
    sizes = {
        "uhd": (3840, 2160),
        "tall": (2160, 3840),
        "ultrawide": (5120, 1440),
        "eight": (7680, 4320),
        "square": (900, 900),
    }
    for name, size in sizes.items():
        make_image(backgrounds / f"{name}.jpg", size, format="JPEG", quality=10)
    indexer_for(conn, layout).run()
    repository = LibraryRepository(conn)

    assert titles(repository, min_tier=UHD_4K) == ["Eight", "Tall", "Uhd"]
    assert titles(repository, min_tier=UHD_8K) == ["Eight"]
    assert titles(repository, orientation=Orientation.PORTRAIT) == ["Tall"]
    assert titles(repository, orientation=Orientation.SQUARE) == ["Square"]
    assert titles(repository, text="wide") == ["Ultrawide"]
    assert titles(repository, text="%") == []  # LIKE wildcards are escaped.
    assert titles(repository, sort=SortOrder.LARGEST)[0] == "Eight"


def test_folders_cannot_overlap_and_removal_keeps_files(
    conn: sqlite3.Connection, layout: dict[str, Path]
) -> None:
    image = make_image(layout["pictures"] / "nested" / "keep.png", (64, 36))
    repository = LibraryRepository(conn)
    source = repository.add_folder(layout["pictures"])
    with pytest.raises(FolderError):
        repository.add_folder(layout["pictures"] / "nested")
    with pytest.raises(FolderError):
        repository.add_folder(layout["pictures"].parent)
    with pytest.raises(FolderError):
        repository.add_folder(layout["pictures"] / "does-not-exist")
    indexer_for(conn, layout).run()
    assert titles(repository, section=Section.FOLDERS) == ["Keep"]

    repository.remove_folder(source.id)
    assert repository.list_sources(SourceKind.FOLDER) == []
    assert titles(repository, section=Section.FOLDERS) == []
    assert image.is_file()


def test_favorites_persist_after_reopening(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    make_image(layout["data_dir"] / "backgrounds" / "keep.png", (64, 36))
    db_path = tmp_path / "persist.sqlite3"
    first = connect(db_path)
    indexer_for(first, layout).run()
    repository = LibraryRepository(first)
    repository.set_favorite(repository.list_wallpapers(LibraryQuery())[0], True)
    first.close()

    second = connect(db_path)
    assert titles(LibraryRepository(second), section=Section.FAVORITES) == ["Keep"]
    second.close()


def test_indexing_can_be_cancelled(
    conn: sqlite3.Connection, layout: dict[str, Path]
) -> None:
    for index in range(5):
        make_image(
            layout["data_dir"] / "backgrounds" / f"{index}.png",
            (8, 8),
            color=(index, 0, 0),
        )
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(IndexCancelled):
        indexer_for(conn, layout, cancel_event=cancel).run()


def test_unavailable_folder_is_reported(
    conn: sqlite3.Connection, layout: dict[str, Path]
) -> None:
    repository = LibraryRepository(conn)
    repository.add_folder(layout["pictures"])
    layout["pictures"].rmdir()
    summary = indexer_for(conn, layout).run()
    assert summary.unavailable_folders == (layout["pictures"],)


def test_downloads_show_the_title_from_their_source(
    conn: sqlite3.Connection, layout: dict[str, Path]
) -> None:
    from dataclasses import replace

    from ashywall.providers.wallhaven import parse_wallpaper

    path = make_image(
        layout["downloads"] / "wallhaven-abc123.jpg", (64, 36), format="JPEG"
    )
    repository = LibraryRepository(conn)
    repository.ensure_builtin_sources(layout["downloads"])
    indexer_for(conn, layout).run()
    assert titles(repository, section=Section.DOWNLOADS) == ["Wallhaven abc123"]
    item = parse_wallpaper(
        {
            "id": "abc123",
            "url": "https://wallhaven.cc/w/abc123",
            "path": "https://w.wallhaven.cc/full/ab/wallhaven-abc123.jpg",
            "thumbs": {"large": "https://th.wallhaven.cc/lg/ab/abc123.jpg"},
            "dimension_x": 64,
            "dimension_y": 36,
            "file_size": 1000,
            "file_type": "image/jpeg",
        }
    )
    repository.record_download(replace(item, title="Misty peak"), path)
    assert titles(repository, section=Section.DOWNLOADS) == ["Misty peak"]
    assert titles(repository, text="misty") == ["Misty peak"]
