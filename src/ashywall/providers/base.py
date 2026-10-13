"""Common types for online wallpaper providers."""

import json
import re
import threading
import urllib.parse
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from ..models.resolution import (
    Orientation,
    ResolutionTier,
    aspect_ratio_label,
    orientation_of,
)

REMOTE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ProviderError(Exception):
    """The provider answered with something AshyWall cannot use."""


class RemoteSort(StrEnum):
    TOP = "top"
    LATEST = "latest"
    VIEWS = "views"
    FAVORITES = "favorites"
    RANDOM = "random"
    RELEVANCE = "relevance"


@dataclass(frozen=True, slots=True)
class RemoteWallpaper:
    """A wallpaper listed by an online provider; nothing is downloaded yet."""

    origin: str
    remote_id: str
    title: str
    width: int
    height: int
    file_size: int | None
    media_type: str | None
    thumbnail_url: str
    preview_url: str
    original_url: str
    page_url: str | None = None
    source_url: str | None = None
    uploader: str | None = None
    license: str | None = None
    # Who made the image, when the provider says so (Wallhaven only names the uploader).
    author: str | None = None
    license_url: str | None = None
    # Bing's caption, which also credits the photographer.
    description: str | None = None
    # ISO date a daily feed featured the image.
    date: str | None = None
    # Interface state, filled from the catalog; not part of the provider data.
    is_favorite: bool = field(default=False, compare=False)
    local_path: Path | None = field(default=None, compare=False)

    @property
    def orientation(self) -> Orientation:
        return orientation_of(self.width, self.height)

    @property
    def aspect_ratio(self) -> str:
        return aspect_ratio_label(self.width, self.height)

    @property
    def favorite_key(self) -> tuple[str, str]:
        return self.origin, self.remote_id

    def with_state(
        self, *, is_favorite: bool, local_path: Path | None
    ) -> "RemoteWallpaper":
        return replace(self, is_favorite=is_favorite, local_path=local_path)

    def to_json(self) -> str:
        data = asdict(self)
        data.pop("is_favorite")
        data.pop("local_path")
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "RemoteWallpaper":
        data = json.loads(text)
        allowed = {
            name
            for name in cls.__dataclass_fields__
            if name not in ("is_favorite", "local_path")
        }
        if not isinstance(data, dict) or not set(data) <= allowed:
            raise ProviderError("Invalid stored remote wallpaper")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class RemoteQuery:
    text: str = ""
    sort: RemoteSort = RemoteSort.TOP
    orientation: Orientation | None = None
    min_tier: ResolutionTier | None = None
    page: int = 1
    seed: str | None = None


@dataclass(frozen=True, slots=True)
class RemotePage:
    items: list[RemoteWallpaper]
    page: int
    last_page: int
    # None when AshyWall filtered results locally and the provider's total no longer applies.
    total: int | None
    seed: str | None = None
    skipped: int = 0

    @property
    def has_more(self) -> bool:
        return self.page < self.last_page


class RemoteProvider(Protocol):
    id: str
    display_name: str
    icon_name: str
    download_hosts: frozenset[str]
    thumbnail_hosts: frozenset[str]
    # Free-text search; daily feeds only list.
    searchable: bool
    # Sort orders offered, the first being the default; empty when fixed.
    sorts: tuple[RemoteSort, ...]
    # Whether details() adds anything the listing lacks (Wallhaven's uploader).
    has_details: bool
    # Whether opening a wallpaper may fetch its original for the preview; Wikimedia
    # asks apps to fetch originals only on request and to use standard thumbnails.
    auto_original: bool

    def search(
        self, query: RemoteQuery, cancel: threading.Event | None = None
    ) -> RemotePage: ...
    def details(
        self, remote_id: str, cancel: threading.Event | None = None
    ) -> RemoteWallpaper: ...


def query_accepts(query: RemoteQuery, item: RemoteWallpaper) -> bool:
    """AshyWall's own orientation and resolution criteria, whatever the provider returned."""
    if query.orientation is not None and item.orientation is not query.orientation:
        return False
    return query.min_tier is None or query.min_tier.accepts(item.width, item.height)


def validated_url(value: object, allowed_hosts: frozenset[str] | None) -> str:
    """Return ``value`` if it is a plain https URL on an allowed host."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ProviderError("URL missing or too long")
    parts = urllib.parse.urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ProviderError(f"Unsafe URL: {value!r}")
    if parts.port not in (None, 443):
        raise ProviderError(f"Unexpected port in {value!r}")
    if allowed_hosts is not None and parts.hostname not in allowed_hosts:
        raise ProviderError(f"Unexpected host in {value!r}")
    return value


def optional_web_url(value: object) -> str | None:
    """A link shown to the user (never fetched by AshyWall): http(s) only."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return value
