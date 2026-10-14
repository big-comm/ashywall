"""The grid model reads rows only for the pages the grid asks for."""

from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from ashywall.models.wallpaper import SourceKind, Wallpaper  # noqa: E402
from ashywall.providers.base import RemoteWallpaper  # noqa: E402
from ashywall.ui.gallery_model import MAX_PAGES, PAGE_SIZE, GalleryModel  # noqa: E402


def wallpaper(wallpaper_id: int, favorite: bool = False) -> Wallpaper:
    return Wallpaper(
        id=wallpaper_id,
        source_id=1,
        source_kind=SourceKind.FOLDER,
        source_label="Pictures",
        path=Path(f"/pictures/{wallpaper_id}.jpg"),
        title=f"Picture {wallpaper_id}",
        width=1920,
        height=1080,
        file_size=1000,
        mtime_ns=0,
        image_format="JPEG",
        dark_path=None,
        picture_options=None,
        is_favorite=favorite,
    )


def remote(remote_id: str) -> RemoteWallpaper:
    return RemoteWallpaper(
        origin="wallhaven",
        remote_id=remote_id,
        title=remote_id,
        width=1920,
        height=1080,
        file_size=None,
        media_type=None,
        thumbnail_url="https://th.wallhaven.cc/lg/ab/x.jpg",
        preview_url="https://th.wallhaven.cc/lg/ab/x.jpg",
        original_url="https://w.wallhaven.cc/full/ab/x.jpg",
        page_url=None,
    )


class Rows:
    """Stands in for the repository and records each read."""

    def __init__(self, missing: set[int] = frozenset()) -> None:
        self.reads: list[list[int]] = []
        self.missing = missing

    def __call__(self, ids: list[int]) -> dict[int, Wallpaper]:
        self.reads.append(ids)
        return {i: wallpaper(i) for i in ids if i not in self.missing}


def test_rows_are_read_by_page_and_only_when_asked() -> None:
    rows = Rows(missing={7})
    model = GalleryModel()
    changes = []
    model.connect("items-changed", lambda _m, *args: changes.append(args))
    ids = list(range(100_000))
    model.reset(ids, rows, [remote("a"), remote("b")])

    assert model.get_n_items() == 100_002 and rows.reads == []
    assert changes == [(0, 0, 100_002)]  # Nothing to remove: one signal.
    assert model.get_item(5).entry.title == "Picture 5"
    assert model.get_item(7).entry is None  # Removed after the query.
    assert len(rows.reads) == 1 and len(rows.reads[0]) == PAGE_SIZE
    model.get_item(PAGE_SIZE - 1)
    assert len(rows.reads) == 1  # Same page, no new read.
    assert model.get_item(100_001).entry.remote_id == "b"

    for page in range(MAX_PAGES + 5):  # Scrolling far keeps memory bounded.
        model.get_item(page * PAGE_SIZE)
    assert len(model._pages) == MAX_PAGES


def test_replace_and_append_update_single_positions() -> None:
    model = GalleryModel()
    model.reset([10, 11, 12], Rows(), [remote("a")])
    changes = []
    model.connect("items-changed", lambda _m, *args: changes.append(args))

    model.replace(wallpaper(11, favorite=True))
    assert changes == [(1, 1, 1)] and model.get_item(1).entry.is_favorite
    model.replace(wallpaper(99))  # Not listed: nothing happens.
    assert model.append([remote("a"), remote("c")]) == 1
    assert changes[-1] == (4, 0, 1) and model.get_n_items() == 5
    model.replace(remote("a"))
    assert changes[-1] == (3, 1, 1)
