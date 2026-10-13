"""Apply/restore against an in-memory GSettings backend: the real desktop is never touched."""

from pathlib import Path

import pytest
from conftest import make_image
from gi.repository import Gio

from ashywall.desktop.gnome import (
    BACKGROUND_SCHEMA,
    ApplyMode,
    BackgroundState,
    DesktopError,
    GnomeBackground,
    background_schema_installed,
)

pytestmark = pytest.mark.skipif(
    not background_schema_installed(), reason="GNOME schema not installed"
)


@pytest.fixture
def background() -> GnomeBackground:
    settings = Gio.Settings.new_with_backend(
        BACKGROUND_SCHEMA, Gio.memory_settings_backend_new()
    )
    settings.set_string("picture-uri", "file:///original/light.png")
    settings.set_string("picture-uri-dark", "file:///original/dark.png")
    settings.set_string("picture-options", "centered")
    return GnomeBackground(settings)


def test_apply_both_styles_and_restore(
    background: GnomeBackground, tmp_path: Path
) -> None:
    image = make_image(tmp_path / "new wallpaper.png", (16, 9))
    before = background.read_state()

    previous = background.apply(image)
    state = background.read_state().values
    assert state["picture-uri"] == state["picture-uri-dark"] == image.as_uri()
    assert state["picture-uri"].endswith("new%20wallpaper.png")
    assert state["picture-options"] == "zoom"
    assert previous == before

    background.restore(previous)
    assert background.read_state() == before


def test_dark_variant_and_single_style(
    background: GnomeBackground, tmp_path: Path
) -> None:
    light = make_image(tmp_path / "l.png", (16, 9))
    dark = make_image(tmp_path / "d.png", (16, 9))

    background.apply(light, dark_path=dark, picture_options="scaled")
    state = background.read_state().values
    assert (state["picture-uri"], state["picture-uri-dark"]) == (
        light.as_uri(),
        dark.as_uri(),
    )
    assert state["picture-options"] == "scaled"

    other = make_image(tmp_path / "o.png", (16, 9))
    background.apply(other, mode=ApplyMode.DARK)
    state = background.read_state().values
    assert (state["picture-uri"], state["picture-uri-dark"]) == (
        light.as_uri(),
        other.as_uri(),
    )


def test_missing_file_changes_nothing(
    background: GnomeBackground, tmp_path: Path
) -> None:
    before = background.read_state()
    with pytest.raises(DesktopError):
        background.apply(tmp_path / "missing.png")
    assert background.read_state() == before


def test_invalid_option_is_rolled_back(
    background: GnomeBackground, tmp_path: Path
) -> None:
    image = make_image(tmp_path / "x.png", (16, 9))
    before = background.read_state()
    with pytest.raises(DesktopError):
        background.apply(image, picture_options="explode")
    assert background.read_state() == before


def test_state_round_trips_through_json(background: GnomeBackground) -> None:
    state = background.read_state()
    assert BackgroundState.from_json(state.to_json()) == state
    with pytest.raises(DesktopError):
        BackgroundState.from_json('["not", "a", "dict"]')
