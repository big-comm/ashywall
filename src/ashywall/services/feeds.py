"""Bounded RSS 2 / Atom parsing. Feed text is never rendered as HTML."""

import datetime as dt
import hashlib
import html.parser
import ipaddress
import urllib.parse
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from ..providers.base import optional_web_url
from .web_urls import web_url_parts

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_FEED_ENTRIES = 200
ATOM = "{http://www.w3.org/2005/Atom}"


class FeedError(Exception):
    pass


class FeedAddressError(FeedError):
    pass


@dataclass(frozen=True, slots=True)
class FeedEntry:
    key: str
    title: str
    url: str
    summary: str
    published_at: int | None


@dataclass(frozen=True, slots=True)
class FeedDocument:
    title: str
    entries: tuple[FeedEntry, ...]


def feed_url(value: str) -> str:
    try:
        parts = web_url_parts(value.strip())
        if parts.scheme != "https" or parts.port not in (None, 443):
            raise ValueError("Feeds require HTTPS on the default port")
        host = parts.hostname.rstrip(".").lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or host.endswith(
                (".local", ".localhost", ".internal", ".lan")
            ):
                raise ValueError("Feeds require a public host") from None
        else:
            if not address.is_global:
                raise ValueError("Feeds require a public address")
        authority = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
        return urllib.parse.urlunsplit(
            (
                "https",
                authority,
                urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                urllib.parse.quote(parts.query, safe="%/?@:!$&'()*+,;=-._~"),
                "",
            )
        )
    except (ValueError, UnicodeError) as error:
        raise FeedAddressError(str(error)) from error


class _Text(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.rsplit(":", 1)[-1]
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "div", "br", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        tag = tag.rsplit(":", 1)[-1]
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("p", "div", "li"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str, limit: int) -> str:
    parser = _Text()
    parser.feed(value)
    text = " ".join("".join(parser.parts).split())
    # Drop controls and bidi overrides from titles supplied by remote sites.
    text = "".join(
        c
        for c in text
        if ord(c) >= 32
        and c not in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    )
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _content(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return (element.text or "") + "".join(
        ET.tostring(child, encoding="unicode") for child in element
    )


def _date(value: str | None) -> int | None:
    if not value:
        return None
    try:
        try:
            date = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            date = parsedate_to_datetime(value.strip())
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        return int(date.timestamp())
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def parse_feed(body: bytes, url: str) -> FeedDocument:
    if len(body) > MAX_FEED_BYTES:
        raise FeedError("Feed exceeds the size limit")
    try:
        root = SafeET.fromstring(
            body, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except (ET.ParseError, DefusedXmlException, ValueError) as error:
        raise FeedError("Invalid or unsafe XML") from error
    pending, count = [(root, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > 64 or count > 30_000:
            raise FeedError("Feed has too many elements or nesting levels")
        pending.extend((child, depth + 1) for child in node)
    atom = root.tag == ATOM + "feed"
    if atom:
        channel = root
        nodes = root.findall(ATOM + "entry")
        title = root.find(ATOM + "title")
    elif root.tag == "rss" and (channel := root.find("channel")) is not None:
        nodes = channel.findall("item")
        title = channel.find("title")
    else:
        raise FeedError("Only RSS 2 and Atom feeds are supported")
    entries: list[FeedEntry] = []
    seen = set()
    for node in nodes:
        prefix = ATOM if atom else ""
        name = plain_text(_content(node.find(prefix + "title")), 240)
        if atom:
            links = node.findall(ATOM + "link")
            href = next(
                (
                    link.get("href", "")
                    for link in links
                    if link.get("rel", "alternate") == "alternate"
                    and link.get("type", "text/html")
                    in ("text/html", "application/xhtml+xml")
                ),
                "",
            )
        else:
            href = node.findtext("link", "")
        try:
            link = (
                optional_web_url(urllib.parse.urljoin(url, href.strip()))
                if href.strip()
                else None
            )
        except ValueError:
            link = None
        if not name or link is None:
            continue
        identifier = node.findtext(prefix + ("id" if atom else "guid")) or link
        key = hashlib.sha256(identifier.encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        summary_node = node.find(prefix + ("summary" if atom else "description"))
        if summary_node is None and atom:
            summary_node = node.find(ATOM + "content")
        date = node.findtext(prefix + ("published" if atom else "pubDate"))
        if not date and atom:
            date = node.findtext(ATOM + "updated")
        entries.append(
            FeedEntry(
                key, name, link, plain_text(_content(summary_node), 1000), _date(date)
            )
        )
        if len(entries) >= MAX_FEED_ENTRIES:
            break
    return FeedDocument(
        plain_text(_content(title), 160) or urllib.parse.urlsplit(url).hostname,
        tuple(entries),
    )
