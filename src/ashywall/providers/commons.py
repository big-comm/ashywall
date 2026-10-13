"""Wikimedia Commons picture of the day.

Every day the Commons community features one picture, chosen among its
featured pictures. Each comes with its author and license (Creative Commons
or public domain), which AshyWall shows and records with downloads. Listing
a page of 30 days takes two API requests: one maps the days to their files,
the other returns sizes, links and licensing for all of them.

Only the curated daily selection is browsed: Commons has no safe-search
filter, so free-text search over all of it is not offered.
"""

import datetime as dt
import html.parser
import threading
from collections.abc import Callable
from typing import Any

from ..services.http import HttpClient, SlidingWindowLimiter
from ..services.image_probe import MAX_PIXELS
from .base import (
    ProviderError,
    RemotePage,
    RemoteQuery,
    RemoteWallpaper,
    optional_web_url,
    query_accepts,
    validated_url,
)

API_URL = "https://commons.wikimedia.org/w/api.php"
IMAGE_HOSTS = frozenset({"upload.wikimedia.org"})
THUMBNAIL_HOSTS = frozenset({"upload.wikimedia.org", "thumb.wikimedia.org"})
PAGE_HOSTS = frozenset({"commons.wikimedia.org"})
MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
DAYS_PER_PAGE = 30
# The picture of the day started on Commons in November 2004.
FIRST_DAY = dt.date(2004, 11, 1)
THUMBNAIL_WIDTH = 500
# Wikimedia only serves thumbnails in standard widths; this one suits the preview.
PREVIEW_WIDTH = 1920
MAX_FILE_BYTES = 256 * 1024 * 1024
EXTMETADATA = "ObjectName|Artist|LicenseShortName|LicenseUrl|Credit"


class _TextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(value: Any, limit: int = 300) -> str | None:
    """Text of a Commons metadata value, which may contain HTML links."""
    if not isinstance(value, str):
        return None
    extractor = _TextExtractor()
    extractor.feed(value)
    text = " ".join(" ".join(extractor.parts).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def days_for_page(page: int, today: dt.date) -> list[dt.date]:
    newest = today - dt.timedelta(days=(page - 1) * DAYS_PER_PAGE)
    days = [newest - dt.timedelta(days=offset) for offset in range(DAYS_PER_PAGE)]
    return [day for day in days if day >= FIRST_DAY]


def last_page(today: dt.date) -> int:
    return (today - FIRST_DAY).days // DAYS_PER_PAGE + 1


def template_title(day: dt.date) -> str:
    return f"Template:Potd/{day.isoformat()}"


def parse_day_files(payload: Any) -> dict[str, str]:
    """Template title -> file title, from a prop=images response."""
    pages = _pages(payload)
    files = {}
    for page in pages:
        images = page.get("images") if isinstance(page, dict) else None
        title = page.get("title") if isinstance(page, dict) else None
        if isinstance(title, str) and isinstance(images, list) and len(images) >= 1:
            first = images[0].get("title") if isinstance(images[0], dict) else None
            if isinstance(first, str) and first.startswith("File:"):
                files[title] = first
    return files


def _pages(payload: Any) -> list[Any]:
    query = payload.get("query") if isinstance(payload, dict) else None
    pages = query.get("pages") if isinstance(query, dict) else None
    if not isinstance(pages, list):
        raise ProviderError("Unexpected Commons response")
    return pages


def _meta(extmetadata: dict[str, Any], key: str) -> Any:
    entry = extmetadata.get(key)
    return entry.get("value") if isinstance(entry, dict) else None


def preview_url(thumbnail: Any, width: int) -> str:
    """The standard 1920 px rendition, from the 500 px one the API returned."""
    url = validated_url(thumbnail, THUMBNAIL_HOSTS)
    marker = f"/{THUMBNAIL_WIDTH}px-"
    if width > PREVIEW_WIDTH and url.count(marker) == 1:
        return validated_url(
            url.replace(marker, f"/{PREVIEW_WIDTH}px-"), THUMBNAIL_HOSTS
        )
    return url


def parse_file(page: Any, day: dt.date | None) -> RemoteWallpaper:
    if not isinstance(page, dict):
        raise ProviderError("File is not an object")
    page_id = page.get("pageid")
    infos = page.get("imageinfo")
    if not isinstance(page_id, int) or page_id <= 0 or not isinstance(infos, list):
        raise ProviderError("Missing file information")
    info = infos[0] if infos and isinstance(infos[0], dict) else {}
    width, height, size = info.get("width"), info.get("height"), info.get("size")
    if not all(
        isinstance(v, int) and not isinstance(v, bool) and v > 0
        for v in (width, height)
    ):
        raise ProviderError("Invalid dimensions")
    if width * height > MAX_PIXELS:
        raise ProviderError("Image too large")
    if not isinstance(size, int) or not 0 < size <= MAX_FILE_BYTES:
        raise ProviderError("Invalid file size")
    media_type = info.get("mime")
    if media_type not in MEDIA_TYPES:
        raise ProviderError(f"Unsupported media type {media_type!r}")
    extmetadata = (
        info.get("extmetadata") if isinstance(info.get("extmetadata"), dict) else {}
    )
    title = page.get("title") if isinstance(page.get("title"), str) else ""
    fallback = title.removeprefix("File:").rsplit(".", 1)[0] or f"Commons {page_id}"
    license_name = plain_text(_meta(extmetadata, "LicenseShortName"), 80)
    license_url = optional_web_url(_meta(extmetadata, "LicenseUrl"))
    return RemoteWallpaper(
        origin="commons",
        remote_id=str(page_id),
        title=plain_text(_meta(extmetadata, "ObjectName"), 200) or fallback,
        width=width,
        height=height,
        file_size=size,
        media_type=media_type,
        thumbnail_url=validated_url(info.get("thumburl"), THUMBNAIL_HOSTS),
        preview_url=preview_url(info.get("thumburl"), width),
        original_url=validated_url(info.get("url"), IMAGE_HOSTS),
        page_url=validated_url(info.get("descriptionurl"), PAGE_HOSTS)
        if info.get("descriptionurl")
        else None,
        author=plain_text(_meta(extmetadata, "Artist"), 200),
        license=license_name,
        license_url=license_url if license_name else None,
        date=day.isoformat() if day else None,
    )


class CommonsProvider:
    id = "commons"
    display_name = "Wikimedia Commons"
    icon_name = "emblem-photos-symbolic"
    download_hosts = IMAGE_HOSTS
    thumbnail_hosts = THUMBNAIL_HOSTS
    searchable = False
    sorts = ()
    has_details = False
    auto_original = False

    def __init__(
        self,
        http: HttpClient | None = None,
        today: Callable[[], dt.date] | None = None,
    ) -> None:
        self._http = http or HttpClient(limiter=SlidingWindowLimiter(60))
        self._today = today or (lambda: dt.datetime.now(dt.UTC).date())

    def search(
        self, query: RemoteQuery, cancel: threading.Event | None = None
    ) -> RemotePage:
        today = self._today()
        page = max(1, query.page)
        days = days_for_page(page, today)
        if not days:
            return RemotePage(
                items=[], page=page, last_page=last_page(today), total=None
            )
        templates = {template_title(day): day for day in days}
        mapping = parse_day_files(
            self._http.get_json(
                API_URL,
                params={
                    "action": "query",
                    "format": "json",
                    "formatversion": "2",
                    "prop": "images",
                    "imlimit": "max",
                    "titles": "|".join(templates),
                },
                cancel=cancel,
            )
        )
        day_of_file = {
            file: templates[t] for t, file in mapping.items() if t in templates
        }
        items: list[RemoteWallpaper] = []
        skipped = len(days) - len(day_of_file)
        if day_of_file:
            payload = self._http.get_json(
                API_URL,
                params={
                    "action": "query",
                    "format": "json",
                    "formatversion": "2",
                    "prop": "imageinfo",
                    "iiprop": "url|size|mime|extmetadata",
                    "iiurlwidth": str(THUMBNAIL_WIDTH),
                    "iiextmetadatafilter": EXTMETADATA,
                    "titles": "|".join(day_of_file),
                },
                cancel=cancel,
            )
            for file_page in _pages(payload):
                title = file_page.get("title") if isinstance(file_page, dict) else None
                try:
                    item = parse_file(file_page, day_of_file.get(title))
                except ProviderError:
                    skipped += 1
                    continue
                if query_accepts(query, item):
                    items.append(item)
                else:
                    skipped += 1
        # Newest first, as the days were featured.
        items.sort(key=lambda item: item.date or "", reverse=True)
        return RemotePage(
            items=items,
            page=page,
            last_page=last_page(today),
            total=None,
            skipped=skipped,
        )

    def details(
        self, remote_id: str, cancel: threading.Event | None = None
    ) -> RemoteWallpaper:
        raise ProviderError("Commons listings are already complete")
