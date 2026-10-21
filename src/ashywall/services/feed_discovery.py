"""Find feeds advertised by a website without loading its embedded resources."""

import html.parser
import urllib.parse

from .feeds import FeedAddressError, FeedError, feed_url
from .web_urls import https_origin

MAX_DISCOVERED_FEEDS = 4
FEED_TYPES = frozenset({"application/rss+xml", "application/atom+xml"})


class FeedDiscoveryError(FeedError):
    pass


def source_url(value: str) -> str:
    """Accept a bare public hostname, retaining strict validation of feed URLs."""
    value = value.strip()
    if "://" not in value and not value.startswith("//"):
        value = "https://" + value
    return feed_url(value)


def known_feed(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    # Phoronix's homepage can refuse automated requests while its official
    # feed remains available. Do not fetch the blocked page to discover it.
    if (
        parts.hostname in {"phoronix.com", "www.phoronix.com"}
        and parts.path == "/"
        and not parts.query
    ):
        return "https://www.phoronix.com/rss.php"
    return url


class _FeedLinks(html.parser.HTMLParser):
    def __init__(self, url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.origin = https_origin(url)
        self.base = url
        self.base_seen = False
        self.urls: list[str] = []
        self.html = False
        self.inert = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.inert += 1
        if self.inert:
            return
        if tag in {"html", "head"}:
            self.html = True
        if tag not in {"link", "base"}:
            return
        attrs = dict(attrs)
        href = attrs.get("href")
        if not href:
            return
        if tag == "base":
            if self.base_seen:
                return
            self.base_seen = True
        try:
            target = feed_url(urllib.parse.urljoin(self.base, href))
            if https_origin(target) != self.origin:
                return
        except (FeedAddressError, ValueError):
            return
        if tag == "base":
            self.base = target
            return
        rel = (attrs.get("rel") or "").lower().split()
        kind = (attrs.get("type") or "").partition(";")[0].strip().lower()
        if (
            "alternate" in rel
            and kind in FEED_TYPES
            and target not in self.urls
            and len(self.urls) < MAX_DISCOVERED_FEEDS
        ):
            self.urls.append(target)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template"}:
            self.inert = max(0, self.inert - 1)


def discover_feeds(body: bytes, url: str) -> tuple[str, ...]:
    # HTML attributes are normally ASCII; UTF-8 replacement still preserves
    # those links when a page uses another character encoding. Never run JS.
    text = body.decode("utf-8-sig", errors="replace")
    if not text.lstrip().lower().startswith(("<!doctype html", "<html", "<head")):
        return ()
    parser = _FeedLinks(url)
    parser.feed(text)
    return tuple(parser.urls) if parser.html else ()
