import os
from pathlib import Path

from conftest import make_image

from ashywall.providers.local import (
    discover_system_wallpapers,
    parse_background_catalog,
    scan_folder,
    title_from_path,
)

CATALOG = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE wallpapers SYSTEM "gnome-wp-list.dtd">
<wallpapers>
  <wallpaper deleted="false">
    <name>Blobs</name>
    <filename>{light}</filename>
    <filename-dark>{dark}</filename-dark>
    <options>zoom</options>
  </wallpaper>
  <wallpaper deleted="true">
    <name>Removed</name>
    <filename>{removed}</filename>
  </wallpaper>
  <wallpaper>
    <name>Slideshow</name>
    <filename>/usr/share/backgrounds/timed.xml</filename>
  </wallpaper>
  <wallpaper>
    <name>Relative</name>
    <filename>relative.png</filename>
  </wallpaper>
  <wallpaper>
    <name>Bad options</name>
    <filename>{other}</filename>
    <options>explode</options>
  </wallpaper>
</wallpapers>
"""


def write_catalog(data_dir: Path, **paths: Path) -> Path:
    catalog = data_dir / "gnome-background-properties" / "test.xml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        CATALOG.format(**{key: str(value) for key, value in paths.items()})
    )
    return catalog


def test_catalog_parsing_skips_deleted_slideshows_and_relative_paths(
    tmp_path: Path,
) -> None:
    catalog = write_catalog(
        tmp_path,
        light=tmp_path / "l.png",
        dark=tmp_path / "d.png",
        removed=tmp_path / "x.png",
        other=tmp_path / "o.jpg",
    )
    entries = parse_background_catalog(catalog)
    assert [entry.title for entry in entries] == ["Blobs", "Bad options"]
    assert entries[0].dark_path == tmp_path / "d.png"
    assert entries[0].picture_options == "zoom"
    assert entries[1].picture_options is None


def test_broken_catalog_yields_nothing(tmp_path: Path) -> None:
    catalog = tmp_path / "broken.xml"
    catalog.write_text("<wallpapers><wallpaper>")
    assert parse_background_catalog(catalog) == []


def test_system_discovery_merges_catalog_and_folders(layout: dict[str, Path]) -> None:
    data_dir = layout["data_dir"]
    light = make_image(data_dir / "backgrounds" / "gnome" / "blobs-l.png", (64, 36))
    dark = make_image(data_dir / "backgrounds" / "gnome" / "blobs-d.png", (64, 36))
    other = make_image(data_dir / "backgrounds" / "gnome" / "other.jpg", (64, 36))
    plain = make_image(data_dir / "wallpapers" / "plain.png", (64, 36))
    user_set = make_image(
        layout["user_data_home"] / "backgrounds" / "mine.png", (64, 36)
    )
    write_catalog(
        data_dir, light=light, dark=dark, removed=data_dir / "nope.png", other=other
    )

    found = {
        entry.path: entry
        for entry in discover_system_wallpapers([data_dir], layout["user_data_home"])
    }

    assert set(found) == {
        light,
        other,
        plain,
        user_set,
    }  # Dark variant is not listed alone.
    assert found[light].title == "Blobs"
    assert found[light].dark_path == dark
    assert found[plain].title is None


def test_missing_dark_variant_is_dropped(layout: dict[str, Path]) -> None:
    data_dir = layout["data_dir"]
    light = make_image(data_dir / "backgrounds" / "l.png", (8, 8))
    other = make_image(data_dir / "backgrounds" / "o.png", (8, 8))
    write_catalog(
        data_dir, light=light, dark=data_dir / "missing.png", removed=light, other=other
    )
    found = {
        e.path: e
        for e in discover_system_wallpapers([data_dir], layout["user_data_home"])
    }
    assert found[light].dark_path is None


def test_scan_folder_skips_hidden_non_images_and_directory_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pics"
    keep = make_image(root / "a" / "keep.JPG", (8, 8), format="JPEG")
    make_image(root / ".hidden" / "secret.png", (8, 8))
    make_image(root / ".dot.png", (8, 8))
    (root / "notes.txt").write_text("not an image")
    os.symlink(root, root / "a" / "loop")

    assert list(scan_folder(root)) == [keep]


def test_title_from_path() -> None:
    assert title_from_path(Path("/x/abstract-colors_10.avif")) == "Abstract colors 10"
