"""Discovery of wallpapers on this computer.

System wallpapers come from two places, both looked up in every XDG data
directory instead of a fixed path:

* GNOME background catalogs (``gnome-background-properties/*.xml``), which
  provide titles, dark variants and picture options;
* the shared ``backgrounds`` and ``wallpapers`` folders, plus the user's
  ``~/.local/share/backgrounds`` where GNOME stores pictures set by the user.

Personal folders are scanned only when the user adds them; the home
directory is never scanned on its own.
"""

import os
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator
from dataclasses import dataclass, replace
from operator import attrgetter
from pathlib import Path

from ..services.image_probe import has_image_extension

# Values accepted by org.gnome.desktop.background picture-options.
PICTURE_OPTIONS: frozenset[str] = frozenset(
    {"none", "wallpaper", "centered", "scaled", "stretched", "zoom", "spanned"}
)

SYSTEM_FOLDER_NAMES: tuple[str, ...] = ("backgrounds", "wallpapers")


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    path: Path
    title: str | None = None
    dark_path: Path | None = None
    picture_options: str | None = None


def title_from_path(path: Path) -> str:
    """'abstract-colors_10' -> 'Abstract colors 10'."""
    name = os.path.basename(path)
    words = os.path.splitext(name)[0].replace("_", " ").replace("-", " ").split()
    text = " ".join(words) or name
    return text[:1].upper() + text[1:]


def _absolute_image_path(text: str | None) -> Path | None:
    if not text:
        return None
    path = Path(text.strip())
    if not path.is_absolute() or not has_image_extension(path):
        return None
    return path


def parse_background_catalog(catalog: Path) -> list[DiscoveredFile]:
    """Parse one GNOME background XML. Unreadable catalogs yield nothing."""
    try:
        root = ElementTree.parse(catalog).getroot()
    except (ElementTree.ParseError, OSError):
        return []
    entries: list[DiscoveredFile] = []
    for element in root.iter("wallpaper"):
        if element.get("deleted", "false").lower() == "true":
            continue
        # Slideshow (.xml) and colour-only entries have no image path to index.
        path = _absolute_image_path(element.findtext("filename"))
        if path is None:
            continue
        options = (element.findtext("options") or "").strip()
        entries.append(
            DiscoveredFile(
                path=path,
                title=(element.findtext("name") or "").strip() or None,
                dark_path=_absolute_image_path(element.findtext("filename-dark")),
                picture_options=options if options in PICTURE_OPTIONS else None,
            )
        )
    return entries


def scan_folder(root: Path) -> Iterator[Path]:
    """Yield image files below ``root``, skipping hidden entries.

    Files come before subfolders, both by name. Directory symlinks are not
    followed, which rules out loops. Directory entries already tell what is a
    file, so this costs one read per folder rather than a call per file.
    """
    pending = [os.fspath(root)]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=attrgetter("name"))
        except OSError:
            continue
        subdirs = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.path)
                elif has_image_extension(entry.name) and entry.is_file():
                    yield Path(entry.path)
            except OSError:
                continue
        pending.extend(reversed(subdirs))


def _catalog_files(data_dirs: list[Path]) -> Iterator[Path]:
    for data_dir in data_dirs:
        folder = data_dir / "gnome-background-properties"
        if folder.is_dir():
            yield from sorted(folder.glob("*.xml"))


def discover_system_wallpapers(
    data_dirs: list[Path], user_data_home: Path
) -> list[DiscoveredFile]:
    """Merge catalog entries with images found in the shared folders.

    Catalog metadata wins over plain files, and images that a catalog lists
    as dark variants are not shown as separate wallpapers.
    """
    search_dirs = [user_data_home, *data_dirs]
    by_path: dict[Path, DiscoveredFile] = {}
    dark_variants: set[Path] = set()

    for catalog in _catalog_files(search_dirs):
        for entry in parse_background_catalog(catalog):
            if entry.path.is_file() and entry.path not in by_path:
                if entry.dark_path is not None and not entry.dark_path.is_file():
                    entry = replace(entry, dark_path=None)
                by_path[entry.path] = entry
                if entry.dark_path is not None:
                    dark_variants.add(entry.dark_path)

    for data_dir in search_dirs:
        for name in SYSTEM_FOLDER_NAMES:
            folder = data_dir / name
            if folder.is_dir():
                for path in scan_folder(folder):
                    by_path.setdefault(path, DiscoveredFile(path=path))

    return [entry for path, entry in by_path.items() if path not in dark_variants]
