"""Applying wallpapers on GNOME through the org.gnome.desktop.background schema.

GNOME keeps one picture for the light style and one for the dark style and
does not support a different picture per monitor ("spanned" stretches one
image across all of them). Before every change the previous values are
captured so they can be restored exactly.

Inside a Flatpak sandbox these settings are not writable; the wallpaper
portal would be needed there and is not implemented yet.
"""

import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from gi.repository import Gio, GLib

BACKGROUND_SCHEMA = "org.gnome.desktop.background"
STATE_KEYS: tuple[str, ...] = (
    "picture-uri",
    "picture-uri-dark",
    "picture-options",
    "primary-color",
    "secondary-color",
    "color-shading-type",
)
DEFAULT_PICTURE_OPTIONS = "zoom"


class ApplyMode(StrEnum):
    BOTH = "both"
    LIGHT = "light"
    DARK = "dark"


class DesktopError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BackgroundState:
    """Snapshot of the background keys, stored as JSON for restoring."""

    values: dict[str, str]

    def to_json(self) -> str:
        return json.dumps(self.values, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "BackgroundState":
        data = json.loads(text)
        if not isinstance(data, dict) or not all(
            isinstance(v, str) for v in data.values()
        ):
            raise DesktopError("Invalid saved background state")
        return cls({key: value for key, value in data.items() if key in STATE_KEYS})


def is_gnome_session() -> bool:
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "")
    return any(name.strip().lower() == "gnome" for name in desktops.split(":"))


def is_sandboxed() -> bool:
    return os.path.exists("/.flatpak-info")


def background_schema_installed() -> bool:
    source = Gio.SettingsSchemaSource.get_default()
    return source is not None and source.lookup(BACKGROUND_SCHEMA, True) is not None


class GnomeBackground:
    def __init__(self, settings: Gio.Settings | None = None) -> None:
        if settings is None:
            if not background_schema_installed():
                raise DesktopError(
                    "The GNOME background settings schema is not installed"
                )
            settings = Gio.Settings.new(BACKGROUND_SCHEMA)
        self._settings = settings

    def read_state(self) -> BackgroundState:
        return BackgroundState({key: self._read(key) for key in STATE_KEYS})

    def _read(self, key: str) -> str:
        # Every key used here is a string or an enum, and enums are stored as their nick.
        return self._settings.get_value(key).get_string()

    def _write(self, key: str, value: str) -> None:
        variant = GLib.Variant("s", value)
        schema_key = self._settings.props.settings_schema.get_key(key)
        if not schema_key.range_check(variant) or not self._settings.set_value(
            key, variant
        ):
            raise DesktopError(f"GNOME does not accept {value!r} for {key}")

    def apply(
        self,
        path: Path,
        *,
        dark_path: Path | None = None,
        mode: ApplyMode = ApplyMode.BOTH,
        picture_options: str | None = None,
    ) -> BackgroundState:
        """Set the wallpaper and return the state it replaced."""
        light_file = _validated_file(path)
        dark_file = _validated_file(dark_path) if dark_path is not None else light_file
        changes: dict[str, str] = {
            "picture-options": picture_options or DEFAULT_PICTURE_OPTIONS
        }
        if mode in (ApplyMode.BOTH, ApplyMode.LIGHT):
            changes["picture-uri"] = light_file.as_uri()
        if mode in (ApplyMode.BOTH, ApplyMode.DARK):
            changes["picture-uri-dark"] = dark_file.as_uri()
        previous = self.read_state()
        self._write_all(changes)
        return previous

    def restore(self, state: BackgroundState) -> None:
        self._write_all(state.values)

    def _write_all(self, changes: dict[str, str]) -> None:
        self._settings.delay()
        try:
            for key, value in changes.items():
                self._write(key, value)
        except Exception:
            self._settings.revert()
            raise
        self._settings.apply()
        Gio.Settings.sync()
        mismatched = [key for key, value in changes.items() if self._read(key) != value]
        if mismatched:
            raise DesktopError(f"Settings were not saved: {', '.join(mismatched)}")


def _validated_file(path: Path) -> Path:
    path = path.absolute()
    if not path.is_file():
        raise DesktopError(f"File not found: {path}")
    return path
