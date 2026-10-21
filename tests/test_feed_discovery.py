"""Website input, bounded feed discovery and the Phoronix recording regression."""

import io
import threading
import urllib.error

import pytest

from ashywall.services.feed_discovery import (
    MAX_DISCOVERED_FEEDS,
    FeedDiscoveryError,
    discover_feeds,
    known_feed,
    source_url,
)
from ashywall.services.feeds import MAX_FEED_BYTES, FeedAddressError
from ashywall.services.http import (
    HttpClient,
    ResponseTooLarge,
    TransferCancelled,
)
from ashywall.services.reading import ReadingService
from ashywall.services.reading_repository import ReadingError, ReadingQuery

ROOT = "https://news.example.org/"
FEED = ROOT + "rss.xml"
RSS = b"""<rss version="2.0"><channel><title>News</title><item>
<title>Story</title><link>/story</link></item></channel></rss>"""
PAGE = b"""<!DOCTYPE html><html><head>
<link rel="alternate" type="application/rss+xml" href="rss.xml">
<script src="https://tracker.example.org/script.js"></script>
</head><body><img src="/tracking.png"></body></html>"""


class Response(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body)
        self.status = 200
        self.headers = {"Content-Length": str(len(body))}
        self.url = url

    def geturl(self):
        return self.url


def service_with_pages(tmp_path, pages, requests):
    def opener(request, timeout):
        url = request.full_url
        requests.append(url)
        result = pages[url]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, tuple):
            body, effective_url = result
        else:
            body, effective_url = result, url
        return Response(body, effective_url)

    return ReadingService(tmp_path / "reading.sqlite3", HttpClient(opener=opener))


@pytest.mark.parametrize(
    "value", ["phoronix.com", "https://phoronix.com", "www.phoronix.com/"]
)
def test_phoronix_uses_official_feed_without_requesting_blocked_homepage(
    tmp_path, value
):
    feed = "https://www.phoronix.com/rss.php"
    requests = []
    service = service_with_pages(tmp_path, {feed: RSS}, requests)
    service.add(value, "Technology", threading.Event())
    feeds, articles, _ = service.snapshot(ReadingQuery())
    assert requests == [feed]
    assert feeds[0].url == feed and len(articles) == 1
    with pytest.raises(ReadingError, match="duplicate"):
        service.add("https://www.phoronix.com/rss.php", "", threading.Event())
    assert requests == [feed]


@pytest.mark.parametrize(
    "value",
    [
        "localhost",
        "127.0.0.1",
        "192.168.1.2/feed",
        "http://news.example.org",
        "https://user:password@news.example.org",
        "https://news.example.org:444/feed",
        "javascript:alert(1)",
        "//news.example.org",
        "news.example.org\n/secret",
    ],
)
def test_bare_hostname_support_keeps_address_security(value):
    with pytest.raises(FeedAddressError):
        source_url(value)


def test_phoronix_special_case_is_limited_to_exact_host_and_root():
    for url in (
        "https://www.phoronix.com/news/story",
        "https://phoronix.com/?custom=1",
        "https://phoronix.com.evil.example/",
    ):
        assert known_feed(url) == url


def test_site_discovery_saves_feed_url_and_refresh_avoids_the_homepage(tmp_path):
    requests = []
    service = service_with_pages(tmp_path, {ROOT: PAGE, FEED: RSS}, requests)
    service.add("news.example.org", "Science", threading.Event())
    feeds, articles, _ = service.snapshot(ReadingQuery())
    assert requests == [ROOT, FEED]
    assert feeds[0].url == FEED and articles[0].category == "Science"
    assert service.refresh(threading.Event()) == 0
    assert requests == [ROOT, FEED, FEED]
    with pytest.raises(ReadingError, match="duplicate"):
        service.add(ROOT, "", threading.Event())
    assert len(service.snapshot(ReadingQuery())[0]) == 1


def test_relative_links_use_final_url_after_same_origin_redirect(tmp_path):
    requests = []
    final = ROOT + "news/"
    feed = final + "rss.xml"
    service = service_with_pages(
        tmp_path, {ROOT: (PAGE, final), feed: (RSS, final + "latest.xml")}, requests
    )
    service.add(ROOT, "", threading.Event())
    assert requests == [ROOT, feed]
    assert service.snapshot(ReadingQuery())[0][0].url == final + "latest.xml"


@pytest.mark.parametrize(
    "href",
    [
        "http://news.example.org/rss",
        "https://127.0.0.1/rss",
        "https://evil.example.org/rss",
        "https://news.example.org.evil.example/rss",
        "https://user:secret@news.example.org/rss",
        "https://news.example.org:444/rss",
        "file:///etc/passwd",
        "https://[broken/rss",
    ],
)
def test_discovery_does_not_fetch_unsafe_or_cross_origin_links(tmp_path, href):
    page = (
        '<html><head><link rel="alternate" type="application/rss+xml" '
        f'href="{href}"></head></html>'
    ).encode()
    requests = []
    service = service_with_pages(tmp_path, {ROOT: page}, requests)
    with pytest.raises(FeedDiscoveryError):
        service.add(ROOT, "", threading.Event())
    assert requests == [ROOT]
    assert service.snapshot(ReadingQuery()) == ([], [], False)


def test_discovery_limits_deduplicates_and_ignores_embedded_resources():
    page = b'<html><head><base href="/news/">'
    page += b'<link rel="alternate" type="text/html" href="/not-a-feed">'
    page += b'<template><link rel="alternate" type="application/rss+xml" href="/hidden"></template>'
    page += (
        b'<link rel="ALTERNATE" type="Application/Atom+XML; charset=utf-8" href="atom.xml">'
        * 2
    )
    page += b"".join(
        f'<link rel="alternate" type="application/rss+xml" href="rss{i}.xml">'.encode()
        for i in range(10)
    )
    page += b"</head></html>"
    links = discover_feeds(page, ROOT)
    assert len(links) == MAX_DISCOVERED_FEEDS
    assert links[0] == ROOT + "news/atom.xml"
    assert links[1] == ROOT + "news/rss0.xml"


def test_cross_origin_base_does_not_move_relative_feed_links():
    page = PAGE.replace(b"<head>", b'<head><base href="https://evil.example/">')
    assert discover_feeds(page, ROOT) == (FEED,)


def test_unsafe_xml_is_not_reinterpreted_as_a_discovery_page():
    body = b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss>'
    body += PAGE + b"</rss>"
    assert discover_feeds(body, ROOT) == ()


def test_failed_candidate_does_not_hide_another_advertised_feed(tmp_path):
    page = PAGE.replace(
        b"</head>",
        b'<link rel="alternate" type="application/atom+xml" href="atom.xml"></head>',
    )
    requests = []
    service = service_with_pages(
        tmp_path,
        {
            ROOT: page,
            FEED: urllib.error.HTTPError(FEED, 403, "Forbidden", {}, None),
            ROOT + "atom.xml": RSS,
        },
        requests,
    )
    service.add(ROOT, "", threading.Event())
    assert requests == [ROOT, FEED, ROOT + "atom.xml"]


def test_discovery_keeps_size_limits_and_does_not_save_failed_sources(tmp_path):
    requests = []
    service = service_with_pages(
        tmp_path, {ROOT: b"x" * (MAX_FEED_BYTES + 1)}, requests
    )
    with pytest.raises(ResponseTooLarge):
        service.add(ROOT, "", threading.Event())
    assert service.snapshot(ReadingQuery()) == ([], [], False)


def test_cancelled_source_does_not_open_a_connection(tmp_path):
    requests = []
    service = service_with_pages(tmp_path, {ROOT: PAGE, FEED: RSS}, requests)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(TransferCancelled):
        service.add(ROOT, "", cancel)
    assert requests == []
