"""XDG locations used by AshyWall.

Environment variables are read on every call (instead of through GLib, which
caches them) so tests and isolated sessions can redirect every location.
"""

import os
from pathlib import Path

APP_ID = "org.communitybig.ashywall"
APP_DIR_NAME = "ashywall"


def _xdg_home(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable, "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def user_data_home() -> Path:
    return _xdg_home("XDG_DATA_HOME", ".local/share")


def system_data_dirs() -> list[Path]:
    value = os.environ.get("XDG_DATA_DIRS", "") or "/usr/local/share:/usr/share"
    return [Path(entry) for entry in value.split(":") if os.path.isabs(entry)]


def data_dir() -> Path:
    return user_data_home() / APP_DIR_NAME


def cache_dir() -> Path:
    return _xdg_home("XDG_CACHE_HOME", ".cache") / APP_DIR_NAME


def database_path() -> Path:
    return data_dir() / "library.sqlite3"


def thumbnails_dir() -> Path:
    """Disposable thumbnails; safe to delete at any time."""
    return cache_dir() / "thumbnails"


def slideshow_dir() -> Path:
    """Slideshow files GNOME reads for the automatic change; must stay in place."""
    return data_dir() / "slideshow"


def originals_dir() -> Path:
    """Full-resolution previews of online wallpapers; safe to delete at any time."""
    return cache_dir() / "originals"


def downloads_dir() -> Path:
    """Originals downloaded from online sources (kept until the user removes them)."""
    return data_dir() / "downloads"


def display_path(path: Path) -> str:
    """A path as shown to the user, with the home folder abbreviated to ~."""
    try:
        relative = Path(path).relative_to(Path.home())
    except ValueError:
        return str(path)
    return "~" if str(relative) == "." else f"~/{relative}"
