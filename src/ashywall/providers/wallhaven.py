"""Wallhaven provider (API v1, https://wallhaven.cc/help/api).

Only safe-for-work content is requested (purity=100) and no API key is used,
so browsing never requires an account. Responses are validated item by item:
URLs must be https on Wallhaven's own hosts, dimensions must be sane, and
anything malformed is skipped instead of shown.

Wallhaven's ``atleast`` filter compares width and height literally, so for
"any orientation" AshyWall asks for both sides to be at least the tier's
short side and applies the long-side rule locally.
"""

import re
import threading
from typing import Any

from ..models.resolution import Orientation, ResolutionTier
from ..services.http import HttpClient, SlidingWindowLimiter
from .base import (
    ProviderError,
    RemotePage,
    RemoteQuery,
    RemoteSort,
    RemoteWallpaper,
    optional_web_url,
    query_accepts,
    validated_url,
)

API_URL = "https://wallhaven.cc/api/v1"
# Published quota is 45 requests per minute; stay slightly below it.
REQUESTS_PER_MINUTE = 40
IMAGE_HOSTS = frozenset({"w.wallhaven.cc"})
THUMBNAIL_HOSTS = frozenset({"th.wallhaven.cc"})
PAGE_HOSTS = frozenset({"wallhaven.cc", "whvn.cc"})
MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
ID_PATTERN = re.compile(r"^[a-z0-9]{4,16}$")
SEED_PATTERN = re.compile(r"^[a-zA-Z0-9]{6}$")
MAX_DIMENSION = 100_000

_SORTING = {
    RemoteSort.TOP: "toplist",
    RemoteSort.LATEST: "date_added",
    RemoteSort.VIEWS: "views",
    RemoteSort.FAVORITES: "favorites",
    RemoteSort.RANDOM: "random",
    RemoteSort.RELEVANCE: "relevance",
}


def new_api_client() -> HttpClient:
    return HttpClient(limiter=SlidingWindowLimiter(REQUESTS_PER_MINUTE))


def _atleast(orientation: Orientation | None, tier: ResolutionTier) -> str:
    if orientation is Orientation.PORTRAIT:
        return f"{tier.short_side}x{tier.long_side}"
    if orientation in (Orientation.LANDSCAPE, Orientation.SQUARE):
        return f"{tier.long_side}x{tier.short_side}"
    return f"{tier.short_side}x{tier.short_side}"


def search_params(query: RemoteQuery) -> dict[str, str]:
    params = {
        "purity": "100",
        "categories": "111",
        "sorting": _SORTING[query.sort],
        "order": "desc",
        "page": str(max(1, query.page)),
    }
    if text := query.text.strip():
        params["q"] = text[:200]
    if query.sort is RemoteSort.TOP:
        params["topRange"] = "1M"
    if (
        query.sort is RemoteSort.RANDOM
        and query.seed
        and SEED_PATTERN.match(query.seed)
    ):
        params["seed"] = query.seed
    if query.orientation is Orientation.LANDSCAPE:
        params["ratios"] = "landscape"
    elif query.orientation is Orientation.PORTRAIT:
        params["ratios"] = "portrait"
    elif query.orientation is Orientation.SQUARE:
        params["ratios"] = "1x1"
    if query.min_tier is not None:
        params["atleast"] = _atleast(query.orientation, query.min_tier)
    return params


def _positive_int(value: Any, limit: int = MAX_DIMENSION) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= limit:
        raise ProviderError(f"Invalid number {value!r}")
    return value


def parse_wallpaper(data: Any) -> RemoteWallpaper:
    if not isinstance(data, dict):
        raise ProviderError("Item is not an object")
    remote_id = data.get("id")
    if not isinstance(remote_id, str) or not ID_PATTERN.match(remote_id):
        raise ProviderError(f"Invalid id {remote_id!r}")
    thumbs = data.get("thumbs")
    if not isinstance(thumbs, dict):
        raise ProviderError("Missing thumbnails")
    media_type = data.get("file_type")
    if media_type not in MEDIA_TYPES:
        raise ProviderError(f"Unsupported media type {media_type!r}")
    file_size = data.get("file_size")
    uploader = data.get("uploader")
    page_url = data.get("url")
    return RemoteWallpaper(
        origin="wallhaven",
        remote_id=remote_id,
        title=f"Wallhaven {remote_id}",
        width=_positive_int(data.get("dimension_x")),
        height=_positive_int(data.get("dimension_y")),
        file_size=_positive_int(file_size, 2**40) if file_size is not None else None,
        media_type=media_type,
        thumbnail_url=validated_url(thumbs.get("large"), THUMBNAIL_HOSTS),
        preview_url=validated_url(
            thumbs.get("original") or thumbs.get("large"), THUMBNAIL_HOSTS
        ),
        original_url=validated_url(data.get("path"), IMAGE_HOSTS),
        page_url=validated_url(page_url, PAGE_HOSTS) if page_url else None,
        source_url=optional_web_url(data.get("source")),
        uploader=_uploader_name(uploader),
    )


def _uploader_name(uploader: Any) -> str | None:
    if isinstance(uploader, dict):
        name = uploader.get("username")
        if isinstance(name, str) and 0 < len(name) <= 64:
            return name
    return None


def parse_search(payload: Any, query: RemoteQuery) -> RemotePage:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProviderError("Unexpected search response")
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    items: list[RemoteWallpaper] = []
    skipped = 0
    for entry in payload["data"]:
        try:
            item = parse_wallpaper(entry)
        except ProviderError:
            skipped += 1
            continue
        if query_accepts(query, item):
            items.append(item)
        else:
            skipped += 1
    last_page = meta.get("last_page")
    total = meta.get("total")
    seed = meta.get("seed")
    locally_filtered = query.min_tier is not None and query.orientation is None
    return RemotePage(
        items=items,
        page=query.page,
        last_page=last_page
        if isinstance(last_page, int) and last_page >= 0
        else query.page,
        total=None if locally_filtered or not isinstance(total, int) else total,
        seed=seed if isinstance(seed, str) and SEED_PATTERN.match(seed) else query.seed,
        skipped=skipped,
    )


class WallhavenProvider:
    id = "wallhaven"
    display_name = "Wallhaven"
    icon_name = "web-browser-symbolic"
    download_hosts = IMAGE_HOSTS
    thumbnail_hosts = THUMBNAIL_HOSTS
    searchable = True
    sorts = (
        RemoteSort.TOP,
        RemoteSort.LATEST,
        RemoteSort.VIEWS,
        RemoteSort.FAVORITES,
        RemoteSort.RANDOM,
        RemoteSort.RELEVANCE,
    )
    has_details = True
    auto_original = True

    def __init__(self, api: HttpClient | None = None) -> None:
        self._api = api or new_api_client()

    def search(
        self, query: RemoteQuery, cancel: threading.Event | None = None
    ) -> RemotePage:
        payload = self._api.get_json(
            f"{API_URL}/search", params=search_params(query), cancel=cancel
        )
        return parse_search(payload, query)

    def details(
        self, remote_id: str, cancel: threading.Event | None = None
    ) -> RemoteWallpaper:
        if not ID_PATTERN.match(remote_id):
            raise ProviderError(f"Invalid id {remote_id!r}")
        payload = self._api.get_json(f"{API_URL}/w/{remote_id}", cancel=cancel)
        if not isinstance(payload, dict):
            raise ProviderError("Unexpected details response")
        return parse_wallpaper(payload.get("data"))
