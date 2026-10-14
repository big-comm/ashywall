"""Automatic change: slideshow files, schedule math and the manager (in-memory GSettings)."""

import random
import sqlite3
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import gi
import pytest
from conftest import make_image
from gi.repository import Gio

from ashywall.desktop.gnome import (
    BACKGROUND_SCHEMA,
    GnomeBackground,
    background_schema_installed,
)
from ashywall.services import rotation
from ashywall.services.library_index import LibraryIndexer
from ashywall.services.library_repository import LibraryRepository
from ashywall.services.rotation import (
    RotationConfig,
    RotationError,
    RotationManager,
    RotationState,
    order_paths,
    parse_source,
    slide_index,
    slideshow_xml,
)


def test_slideshow_xml_lists_pictures_and_fades() -> None:
    paths = [Path("/a/one.jpg"), Path("/a/two & three.png")]
    root = ElementTree.fromstring(slideshow_xml(paths, 600, 1_700_000_000))
    statics = root.findall("static")
    transitions = root.findall("transition")
    assert [s.findtext("file") for s in statics] == ["/a/one.jpg", "/a/two & three.png"]
    assert [float(s.findtext("duration")) for s in statics] == [598.0, 598.0]
    assert [(t.findtext("from"), t.findtext("to")) for t in transitions] == [
        ("/a/one.jpg", "/a/two & three.png"),
        ("/a/two & three.png", "/a/one.jpg"),
    ]
    single = ElementTree.fromstring(slideshow_xml(paths[:1], 600, 1_700_000_000))
    assert single.find("transition") is None
    with pytest.raises(RotationError):
        slideshow_xml([], 600, 0)


def test_slideshow_is_read_by_gnome(tmp_path: Path) -> None:
    try:
        gi.require_version("GnomeBG", "4.0")
        from gi.repository import GnomeBG
    except (ValueError, ImportError):
        pytest.skip("gnome-desktop is not installed")
    paths = [make_image(tmp_path / f"{n}.png", (16, 9)) for n in range(3)]
    start = 1_700_000_000.0
    xml = tmp_path / "show.xml"
    xml.write_text(slideshow_xml(paths, 900, start))
    show = GnomeBG.BGSlideShow.new(str(xml))
    assert show.load()
    assert show.get_num_slides() == 6  # Three pictures, three fades.
    assert show.get_total_duration() == pytest.approx(2700)
    assert show.get_start_time() == pytest.approx(start)


def test_slide_index_follows_the_schedule() -> None:
    assert slide_index(3, 600, 1000, 1000) == 0
    assert slide_index(3, 600, 1000, 1599) == 0
    assert slide_index(3, 600, 1000, 1600) == 1
    assert slide_index(3, 600, 1000, 1000 + 3 * 600 + 5) == 0
    assert slide_index(3, 600, 1000, 10) == 0  # Clock before the start.


def test_sources_and_config_are_validated() -> None:
    assert parse_source("favorites")[1] is None
    assert parse_source("collection:7")[1] == 7
    for bad in ("collection:x", "collection", "elsewhere"):
        with pytest.raises(RotationError):
            parse_source(bad)
    with pytest.raises(RotationError):
        RotationConfig(interval=7)
    with pytest.raises(RotationError):
        RotationConfig(min_tier="16k")
    state = RotationState(
        RotationConfig(source="collection:3"), ("/a.png",), 5.0, "/s.xml"
    )
    assert RotationState.from_json(state.to_json()) == state
    with pytest.raises(RotationError):
        RotationState.from_json('{"config": {"interval": 1}}')


def test_order_is_shuffled_only_when_asked() -> None:
    paths = [Path(f"/{n}") for n in range(20)]
    assert order_paths(paths, shuffle=False) == paths
    shuffled = order_paths(paths, shuffle=True, rng=random.Random(4))
    assert sorted(shuffled) == sorted(paths) and shuffled != paths


@pytest.fixture
def desktop() -> GnomeBackground:
    if not background_schema_installed():
        pytest.skip("GNOME schema not installed")
    settings = Gio.Settings.new_with_backend(
        BACKGROUND_SCHEMA, Gio.memory_settings_backend_new()
    )
    settings.set_string("picture-uri", "file:///original/light.png")
    return GnomeBackground(settings)


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def library(conn: sqlite3.Connection, layout: dict[str, Path]) -> LibraryRepository:
    make_image(layout["pictures"] / "wide.png", (64, 36))
    make_image(layout["pictures"] / "big.png", (4000, 2250))
    make_image(layout["pictures"] / "tall.png", (36, 64))
    repository = LibraryRepository(conn)
    repository.ensure_builtin_sources(layout["downloads"])
    repository.add_folder(layout["pictures"])
    LibraryIndexer(
        conn,
        data_dirs=[layout["data_dir"]],
        user_data_home=layout["user_data_home"],
        downloads_dir=layout["downloads"],
    ).run()
    return repository


def test_candidates_follow_the_filters(
    library: LibraryRepository, tmp_path: Path
) -> None:
    manager = RotationManager(library, None, lambda: tmp_path / "slides")
    names = lambda config: sorted(p.name for p in manager.candidates(config))  # noqa: E731
    assert names(RotationConfig(source="folders")) == ["big.png", "wide.png"]
    assert names(RotationConfig(source="folders", skip_portrait=False)) == [
        "big.png",
        "tall.png",
        "wide.png",
    ]
    assert names(RotationConfig(source="folders", min_tier="4k")) == ["big.png"]
    assert names(RotationConfig(source="favorites")) == []


def test_start_next_refresh_and_stop(
    library: LibraryRepository,
    desktop: GnomeBackground,
    layout: dict[str, Path],
    tmp_path: Path,
) -> None:
    clock = Clock(1_700_000_000.0)
    slides = tmp_path / "slides"
    manager = RotationManager(library, desktop, lambda: slides, clock)
    config = RotationConfig(source="folders", interval=900, shuffle=False)

    state, replaced = manager.start(config)
    uri = desktop.read_state().values["picture-uri"]
    assert (
        uri
        == Path(state.file).as_uri()
        == desktop.read_state().values["picture-uri-dark"]
    )
    assert "file:///original/light.png" in replaced
    assert [Path(p).name for p in state.paths] == ["big.png", "wide.png"]
    assert manager.active_state() == state
    assert manager.current_path(state).name == "big.png"

    clock.now += 60
    following = manager.next()
    assert manager.current_path(following).name == "wide.png"
    assert list(slides.glob("rotation-*.xml")) == [Path(following.file)]
    # A second start does not report a desktop state to restore (it would be a slideshow).
    assert manager.start(config)[1] is None

    # A new matching file joins the list; the picture on screen stays first.
    following = manager.active_state()
    on_screen = manager.current_path(following)
    make_image(layout["pictures"] / "new.png", (3000, 1700))
    LibraryIndexer(
        library._conn,
        data_dirs=[layout["data_dir"]],
        user_data_home=layout["user_data_home"],
        downloads_dir=layout["downloads"],
    ).run()
    clock.now += 5
    refreshed = manager.refresh()
    assert refreshed is not None and len(refreshed.paths) == 3
    assert manager.current_path(refreshed) == on_screen

    kept = manager.stop()
    assert kept == on_screen
    assert desktop.read_state().values["picture-uri"] == on_screen.as_uri()
    assert manager.active_state() is None
    assert not list(slides.glob("rotation-*.xml"))
    assert manager.saved_config() == config


def test_rotation_ends_when_the_wallpaper_changes_elsewhere(
    library: LibraryRepository, desktop: GnomeBackground, tmp_path: Path
) -> None:
    manager = RotationManager(library, desktop, lambda: tmp_path / "slides")
    manager.start(RotationConfig(source="folders"))
    other = make_image(tmp_path / "other.png", (16, 9))
    desktop.apply(other)
    assert manager.active_state() is None
    assert manager.refresh() is None
    assert manager.stop() is None
    assert desktop.read_state().values["picture-uri"] == other.as_uri()


def test_nothing_to_rotate_is_an_error(
    library: LibraryRepository, desktop: GnomeBackground, tmp_path: Path
) -> None:
    manager = RotationManager(library, desktop, lambda: tmp_path / "slides")
    with pytest.raises(RotationError):
        manager.start(RotationConfig(source="favorites"))
    with pytest.raises(RotationError):
        manager.next()


def test_a_full_slideshow_is_kept_after_indexing(
    library: LibraryRepository,
    desktop: GnomeBackground,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two pictures match but a slideshow holds one: the library is "larger".
    monkeypatch.setattr(rotation, "MAX_SLIDES", 1)
    manager = RotationManager(library, desktop, lambda: tmp_path / "slides")
    state, _replaced = manager.start(RotationConfig(source="folders", shuffle=True))
    assert len(state.paths) == 1
    assert manager.refresh() == state  # Not rewritten (and not reshuffled).

    Path(state.paths[0]).unlink()  # Its picture is gone: the list is rebuilt.
    refreshed = manager.refresh()
    assert refreshed is not None and refreshed.paths != state.paths


def test_slides_skip_missing_files_and_stop_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rotation, "MAX_SLIDES", 2)
    files = [make_image(tmp_path / f"{n}.png", (16, 9)) for n in range(3)]
    assert rotation.slides([tmp_path / "gone.png", *files]) == files[:2]
