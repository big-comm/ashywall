"""Bing image of the day (the archive behind bing.com's home page).

Bing lists the last couple of weeks, with a title and a caption that credits
the photographer and agency; it publishes no license, and the images remain
the property of their owners. The archive does not state the size of the
UHD original, so each listing reads the first bytes of the file to learn its
real dimensions and size before showing it.
"""

import io
import locale
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PIL import Image, UnidentifiedImageError

from ..services.http import HttpClient, HttpError, SlidingWindowLimiter
from ..services.image_probe import ALLOWED_FORMATS, MAX_PIXELS
from .base import (
    ProviderError,
    RemotePage,
    RemoteQuery,
    RemoteWallpaper,
    optional_web_url,
    query_accepts,
    validated_url,
)

ARCHIVE_URL = "https://www.bing.com/HPImageArchive.aspx"
HOST = "www.bing.com"
HOSTS = frozenset({HOST})
# The archive serves at most 8 entries per request and about 16 days in all.
PAGE_SIZE = 8
LAST_PAGE = 2
PROBE_BYTES = 64 * 1024
HASH_PATTERN = re.compile(r"^[0-9a-f]{32}$")
URLBASE_PATTERN = re.compile(r"^/th\?id=OHR\.[A-Za-z0-9_-]{1,200}$")
DATE_PATTERN = re.compile(r"^\d{8}$")
CREDIT_PATTERN = re.compile(r"\(©\s*([^()]{1,200})\)\s*$")
MARKETS = frozenset(
    {
        "de-DE", "en-AU", "en-CA", "en-GB", "en-IN", "en-US", "es-ES", "fr-CA",
        "fr-FR", "it-IT", "ja-JP", "pt-BR", "zh-CN",
    }
)  # fmt: skip


def market() -> str:
    """Bing market for the user's language, so titles and captions match it."""
    language = locale.getlocale(locale.LC_MESSAGES)[0] or "en_US"
    candidate = language.replace("_", "-")
    if candidate in MARKETS:
        return candidate
    prefix = candidate.split("-")[0]
    return next((m for m in sorted(MARKETS) if m.startswith(prefix + "-")), "en-US")


def _text(value: Any, limit: int) -> str | None:
    if isinstance(value, str) and 0 < len(value.strip()) <= limit:
        return value.strip()
    return None


def credit_of(caption: str | None) -> str | None:
    """ "Lake (© Name/Agency)" -> "Name/Agency"."""
    if caption is None:
        return None
    match = CREDIT_PATTERN.search(caption)
    return match.group(1).strip() if match else None


def parse_entry(data: Any) -> tuple[str, dict[str, Any]]:
    """Validate one archive entry; returns its UHD URL and the fields known so far."""
    if not isinstance(data, dict):
        raise ProviderError("Entry is not an object")
    remote_id = data.get("hsh")
    urlbase = data.get("urlbase")
    if not isinstance(remote_id, str) or not HASH_PATTERN.match(remote_id):
        raise ProviderError(f"Invalid id {remote_id!r}")
    if not isinstance(urlbase, str) or not URLBASE_PATTERN.match(urlbase):
        raise ProviderError(f"Invalid image path {urlbase!r}")
    base = f"https://{HOST}{urlbase}"
    caption = _text(data.get("copyright"), 400)
    date = data.get("startdate")
    fields = {
        "remote_id": remote_id,
        "title": _text(data.get("title"), 200) or caption or f"Bing {remote_id[:8]}",
        "description": caption,
        "author": credit_of(caption),
        "thumbnail_url": validated_url(f"{base}_800x480.jpg", HOSTS),
        "preview_url": validated_url(f"{base}_1920x1080.jpg", HOSTS),
        "page_url": optional_web_url(
            f"https://{HOST}{data['copyrightlink']}"
            if isinstance(data.get("copyrightlink"), str)
            and data["copyrightlink"].startswith("/")
            else data.get("copyrightlink")
        ),
        "date": f"{date[:4]}-{date[4:6]}-{date[6:]}"
        if isinstance(date, str) and DATE_PATTERN.match(date)
        else None,
    }
    return validated_url(f"{base}_UHD.jpg", HOSTS), fields


def probe_prefix(prefix: bytes) -> tuple[int, int, str]:
    """Dimensions and format from the start of an image file."""
    try:
        with Image.open(io.BytesIO(prefix)) as image:
            width, height = image.size
            image_format = image.format or ""
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise ProviderError(f"Unreadable image header: {error}") from error
    if image_format not in ALLOWED_FORMATS or not 0 < width * height <= MAX_PIXELS:
        raise ProviderError(f"Unsupported image {image_format} {width}x{height}")
    return width, height, image_format


class BingProvider:
    id = "bing"
    display_name = "Bing"
    icon_name = "weather-clear-symbolic"
    download_hosts = HOSTS
    thumbnail_hosts = HOSTS
    searchable = False
    sorts = ()
    has_details = False
    auto_original = True

    def __init__(self, http: HttpClient | None = None) -> None:
        self._http = http or HttpClient(limiter=SlidingWindowLimiter(60))
        # Probing reads 64 KiB per image; remember the answers for this session.
        self._probed: dict[str, tuple[int, int, str, int | None]] = {}
        self._lock = threading.Lock()

    def search(
        self, query: RemoteQuery, cancel: threading.Event | None = None
    ) -> RemotePage:
        page = min(max(1, query.page), LAST_PAGE)
        payload = self._http.get_json(
            ARCHIVE_URL,
            params={
                "format": "js",
                "idx": str((page - 1) * PAGE_SIZE),
                "n": str(PAGE_SIZE),
                "mkt": market(),
            },
            cancel=cancel,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("images"), list):
            raise ProviderError("Unexpected archive response")
        entries = []
        skipped = 0
        for data in payload["images"]:
            try:
                entries.append(parse_entry(data))
            except ProviderError:
                skipped += 1
        with ThreadPoolExecutor(max_workers=4) as pool:
            probes = list(
                pool.map(lambda entry: self._probe(entry[0], entry[1], cancel), entries)
            )
        items = []
        for (url, fields), probe in zip(entries, probes, strict=True):
            if probe is None:
                skipped += 1
                continue
            width, height, image_format, size = probe
            item = RemoteWallpaper(
                origin=self.id,
                remote_id=fields["remote_id"],
                title=fields["title"],
                width=width,
                height=height,
                file_size=size,
                media_type="image/jpeg" if image_format == "JPEG" else None,
                thumbnail_url=fields["thumbnail_url"],
                preview_url=fields["preview_url"],
                original_url=url,
                page_url=fields["page_url"],
                author=fields["author"],
                description=fields["description"],
                date=fields["date"],
            )
            if query_accepts(query, item):
                items.append(item)
            else:
                skipped += 1
        return RemotePage(
            items=items,
            page=page,
            last_page=LAST_PAGE,
            total=None,
            skipped=skipped,
        )

    def _probe(
        self, url: str, fields: dict[str, Any], cancel: threading.Event | None
    ) -> tuple[int, int, str, int | None] | None:
        key = fields["remote_id"]
        with self._lock:
            if key in self._probed:
                return self._probed[key]
        try:
            prefix, size = self._http.get_prefix(url, PROBE_BYTES, cancel=cancel)
            width, height, image_format = probe_prefix(prefix)
        except (HttpError, ProviderError):
            return None
        result = (width, height, image_format, size)
        with self._lock:
            self._probed[key] = result
        return result

    def details(
        self, remote_id: str, cancel: threading.Event | None = None
    ) -> RemoteWallpaper:
        raise ProviderError("Bing listings are already complete")
