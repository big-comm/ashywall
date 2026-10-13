import shutil
import sqlite3
from pathlib import Path

import pytest
from conftest import make_image
from test_library import indexer_for, titles
from test_remote import listing

from ashywall.providers.wallhaven import parse_wallpaper
from ashywall.services.database import connect
from ashywall.services.library_repository import (
    CollectionError,
    LibraryQuery,
    LibraryRepository,
    Section,
)


def local_wallpapers(repository: LibraryRepository) -> dict:
    return {w.title: w for w in repository.list_wallpapers(LibraryQuery())}


def test_create_rename_delete_and_name_rules(conn: sqlite3.Connection) -> None:
    repository = LibraryRepository(conn)
    nature = repository.create_collection("  Nature   shots ")
    assert nature.name == "Nature shots"
    with pytest.raises(CollectionError):
        repository.create_collection("nature SHOTS")  # Case-insensitive clash.
    for bad in ("", "   ", "x" * 81):
        with pytest.raises(CollectionError):
            repository.create_collection(bad)
    other = repository.create_collection("Dark")
    with pytest.raises(CollectionError):
        repository.rename_collection(other.id, "Nature shots")
    repository.rename_collection(nature.id, "Nature")
    repository.rename_collection(nature.id, "nature")  # Same collection: allowed.
    assert [c.name for c in repository.list_collections()] == ["Dark", "nature"]
    repository.delete_collection(other.id)
    assert [c.name for c in repository.list_collections()] == ["nature"]


def test_local_and_remote_members(
    conn: sqlite3.Connection, layout: dict[str, Path]
) -> None:
    make_image(layout["data_dir"] / "backgrounds" / "lake.png", (64, 36))
    make_image(layout["data_dir"] / "backgrounds" / "tall.png", (36, 64))
    indexer_for(conn, layout).run()
    repository = LibraryRepository(conn)
    pick = repository.create_collection("Pick")
    walls = local_wallpapers(repository)
    remote = parse_wallpaper(listing())

    repository.set_in_collection(pick.id, walls["Lake"], True)
    repository.set_in_collection(pick.id, walls["Tall"], True)
    repository.set_in_collection(pick.id, remote, True)
    repository.set_in_collection(pick.id, remote, True)  # Idempotent.

    assert repository.list_collections()[0].count == 3
    assert repository.collections_containing(walls["Lake"]) == {pick.id}
    assert repository.collections_containing(remote) == {pick.id}
    query = {"section": Section.COLLECTION, "collection_id": pick.id}
    assert titles(repository, **query) == ["Lake", "Tall"]
    assert repository.remote_collection_items(pick.id) == [remote]

    repository.set_in_collection(pick.id, walls["Tall"], False)
    assert titles(repository, **query) == ["Lake"]
    assert repository.count_by_section()[Section.ALL] == 2  # Library untouched.


def test_deleting_collection_keeps_files_and_favorites(
    conn: sqlite3.Connection, layout: dict[str, Path]
) -> None:
    image = make_image(layout["data_dir"] / "backgrounds" / "keep.png", (64, 36))
    indexer_for(conn, layout).run()
    repository = LibraryRepository(conn)
    wallpaper = local_wallpapers(repository)["Keep"]
    repository.set_favorite(wallpaper, True)
    collection = repository.create_collection("Temp")
    repository.set_in_collection(collection.id, wallpaper, True)

    repository.delete_collection(collection.id)

    assert image.is_file()
    assert titles(repository, section=Section.FAVORITES) == ["Keep"]
    assert conn.execute("SELECT COUNT(*) FROM collection_items").fetchone()[0] == 0


def test_missing_files_are_not_counted_and_come_back(
    conn: sqlite3.Connection, layout: dict[str, Path], tmp_path: Path
) -> None:
    image = make_image(layout["data_dir"] / "backgrounds" / "away.png", (64, 36))
    indexer_for(conn, layout).run()
    repository = LibraryRepository(conn)
    collection = repository.create_collection("Mine")
    repository.set_in_collection(
        collection.id, local_wallpapers(repository)["Away"], True
    )

    parked = tmp_path / "parked.png"
    shutil.move(image, parked)
    indexer_for(conn, layout).run()
    assert repository.list_collections()[0].count == 0

    shutil.move(parked, image)
    indexer_for(conn, layout).run()
    assert repository.list_collections()[0].count == 1


def test_collections_persist_after_reopening(tmp_path: Path) -> None:
    path = tmp_path / "c.sqlite3"
    first = connect(path)
    LibraryRepository(first).create_collection("Persist")
    first.close()
    second = connect(path)
    assert [c.name for c in LibraryRepository(second).list_collections()] == ["Persist"]
    second.close()
