"""Library entities shared by services and the interface."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .resolution import Orientation, aspect_ratio_label, orientation_of

LOCAL_ORIGIN = "local"


class SourceKind(StrEnum):
    SYSTEM = "system"
    FOLDER = "folder"
    DOWNLOADS = "downloads"


@dataclass(frozen=True, slots=True)
class Source:
    id: int
    kind: SourceKind
    path: Path | None
    label: str


@dataclass(frozen=True, slots=True)
class Collection:
    id: int
    name: str
    # Items currently available: local files that still exist plus online items.
    count: int


@dataclass(frozen=True, slots=True)
class Wallpaper:
    id: int
    source_id: int
    source_kind: SourceKind
    source_label: str
    path: Path
    title: str
    width: int
    height: int
    file_size: int
    mtime_ns: int
    image_format: str | None
    dark_path: Path | None
    picture_options: str | None
    is_favorite: bool

    @property
    def orientation(self) -> Orientation:
        return orientation_of(self.width, self.height)

    @property
    def aspect_ratio(self) -> str:
        return aspect_ratio_label(self.width, self.height)

    @property
    def favorite_key(self) -> tuple[str, str]:
        """Favorites store origin + identifier so they survive re-indexing."""
        return LOCAL_ORIGIN, str(self.path)
