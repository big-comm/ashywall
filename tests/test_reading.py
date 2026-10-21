"""RSS/Atom, local article state, migration, offline behavior and public DNS."""

import datetime as dt
import io
import socket
import sqlite3
import threading
import urllib.error
from dataclasses import replace

import pytest

from ashywall.services import http
from ashywall.services.connections import ConnectionPool, NameCache
from ashywall.services.database import MIGRATIONS, connect
from ashywall.services.feeds import (
    MAX_FEED_BYTES,
    MAX_FEED_ENTRIES,
    FeedAddressError,
    FeedDocument,
    FeedEntry,
    FeedError,
    feed_url,
    parse_feed,
)
from ashywall.services.http import HttpClient, NetworkUnavailable, TransferCancelled
from ashywall.services.reading import ReadingService
from ashywall.services.reading_repository import (
    ARTICLES_PER_FEED,
    PAGE_SIZE,
    ReadingError,
    ReadingQuery,
    ReadingRepository,
)

URL = "https://news.example.org/feed.xml"
RSS = b"""<rss version="2.0"><channel><title>Example News</title>
<item><guid>story-1</guid><title>One &amp; Two</title><link>/news/one</link>
<description><![CDATA[<p>A <b>short</b> summary.</p><script>hidden()</script><img src="https://tracker.example/pixel">]]></description>
<pubDate>Wed, 21 Oct 2026 12:00:00 GMT</pubDate></item>
<item><guid>story-2</guid><title>Second story</title><link>https://other.example.org/story</link></item>
</channel></rss>"""


def document(count=1, start=0):
    return FeedDocument(
        "Example",
        tuple(
            FeedEntry(str(i), f"Story {i}", f"https://example.org/{i}", "Summary", i)
            for i in range(start, start + count)
        ),
    )


def test_rss_metadata_is_plain_text_and_links_are_display_only():
    feed = parse_feed(RSS, URL)
    assert feed.title == "Example News"
    assert len(feed.entries) == 2
    entry = feed.entries[0]
    assert entry.title == "One & Two"
    assert entry.summary == "A short summary."
    assert entry.url == "https://news.example.org/news/one"
    assert entry.published_at == int(
        dt.datetime(2026, 10, 21, 12, tzinfo=dt.timezone.utc).timestamp()
    )
    assert feed.entries[1].published_at is None


def test_atom_supports_xhtml_summaries_and_updated_dates():
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>
    <entry><id>urn:example:one</id><title type="html">A &amp; B</title>
    <link rel="self" href="https://example.org/entry.xml" type="application/atom+xml"/>
    <link href="https://example.org/one"/>
    <summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Hello <b>reader</b>.</p><script>hidden()</script></div></summary>
    <updated>2026-10-21T12:00:00Z</updated></entry></feed>"""
    entry = parse_feed(body, URL).entries[0]
    assert entry.url == "https://example.org/one"
    assert entry.summary == "Hello reader."
    assert entry.published_at is not None


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>This is a website</body></html>",
        b"not XML",
        b'<!DOCTYPE rss [<!ENTITY x "bad">]><rss><channel><title>&x;</title></channel></rss>',
        b'<!DOCTYPE rss SYSTEM "file:///etc/passwd"><rss><channel/></rss>',
        b"x" * (MAX_FEED_BYTES + 1),
        b"<rss><channel>" + b"<a>" * 70 + b"x" + b"</a>" * 70 + b"</channel></rss>",
    ],
)
def test_invalid_or_unsafe_xml_is_refused(body):
    with pytest.raises(FeedError):
        parse_feed(body, URL)


def test_invalid_links_are_skipped_and_duplicates_are_not_added_twice():
    items = "".join(
        f"<item><guid>{i}</guid><title>Bad</title><link>{url}</link></item>"
        for i, url in enumerate(
            ("javascript:alert(1)", "file:///etc/passwd", "https://[bad/x")
        )
    )
    items += (
        "<item><guid>same</guid><title>A</title><link>https://example.org/1</link></item>"
        * 2
    )
    body = f"<rss><channel><title>News</title>{items}</channel></rss>".encode()
    assert len(parse_feed(body, URL).entries) == 1


def test_entry_count_and_text_lengths_are_bounded():
    item = (
        "<item><title>"
        + "x" * 500
        + "</title><link>https://example.org/{0}</link><description>"
        + "y" * 2000
        + "</description></item>"
    )
    body = (
        "<rss><channel>"
        + "".join(item.format(i) for i in range(MAX_FEED_ENTRIES + 10))
        + "</channel></rss>"
    ).encode()
    feed = parse_feed(body, URL)
    assert len(feed.entries) == MAX_FEED_ENTRIES
    assert len(feed.entries[0].title) == 240
    assert len(feed.entries[0].summary) == 1000


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/rss",
        "https://localhost/rss",
        "https://127.0.0.1/rss",
        "https://192.168.0.1/rss",
        "https://[::1]/rss",
        "https://printer.local/rss",
        "https://u:p@example.org/rss",
        "https://example.org:444/rss",
        "https://[broken/rss",
    ],
)
def test_sources_must_be_public_https(url):
    with pytest.raises(FeedAddressError):
        feed_url(url)


def test_feed_address_is_normalized_without_changing_query_case():
    assert (
        feed_url(" HTTPS://News.Example.org/rss?Token=AbC#section ")
        == "https://news.example.org/rss?Token=AbC"
    )


def test_feed_address_encodes_accents_and_removes_the_default_port():
    assert (
        feed_url("https://example.org:443/notícias.xml?tema=saúde")
        == "https://example.org/not%C3%ADcias.xml?tema=sa%C3%BAde"
    )


def test_article_date_outside_the_local_calendar_is_displayed_as_unknown(monkeypatch):
    from types import SimpleNamespace

    pytest.importorskip("gi")
    from ashywall.services.reading_repository import Article
    from ashywall.ui import reading_page

    monkeypatch.setattr(
        reading_page,
        "GLib",
        SimpleNamespace(
            DateTime=SimpleNamespace(
                new_from_unix_local=lambda _timestamp: None,
            )
        ),
    )
    article = Article(
        1, 1, "Example", "Technology", "Story", URL, "Summary", -62135596800, False
    )
    assert reading_page.article_meta(
        article
    ) == "Example · Technology · " + reading_page._("Date not provided")


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1"],
)
def test_feed_dns_cannot_resolve_to_private_or_loopback_addresses(ip):
    def lookup(host, port, family, kind):
        return [(family, kind, 6, "", (ip, port))]

    with pytest.raises(OSError, match="non-public"):
        NameCache(lookup=lookup, public_only=True).resolve("news.example.org", 443)


def test_public_dns_addresses_are_cached_and_reused():
    calls = []

    def lookup(host, port, family, kind):
        calls.append(family)
        return (
            [(family, kind, 6, "", ("1.1.1.1", port))]
            if family == socket.AF_INET
            else []
        )

    cache = NameCache(lookup=lookup, public_only=True)
    assert cache.resolve("news.example.org", 443) == cache.resolve(
        "news.example.org", 443
    )
    assert len(calls) == 2


@pytest.mark.parametrize("proxy", [False, True])
def test_private_dns_is_blocked_before_direct_or_proxy_network_access(
    monkeypatch, proxy
):
    def lookup(host, port, family, kind):
        return [(family, kind, 6, "", ("127.0.0.1", port))]

    names = NameCache(lookup=lookup, public_only=True)
    pool = ConnectionPool(names)
    monkeypatch.setattr(http, "_public_transport", lambda: (names, pool))
    monkeypatch.setattr(
        http.urllib.request,
        "getproxies",
        lambda: {"https": "configured-proxy"} if proxy else {},
    )

    def unexpected_socket(*args, **kwargs):
        raise AssertionError("No socket should be opened for private feed addresses")

    monkeypatch.setattr(socket, "socket", unexpected_socket)
    with pytest.raises(NetworkUnavailable, match="non-public"):
        HttpClient(public_only=True).get_bytes(URL, max_bytes=MAX_FEED_BYTES)


def test_mixed_public_and_private_dns_answers_are_rejected():
    def lookup(host, port, family, kind):
        ip = "1.1.1.1" if family == socket.AF_INET else "::1"
        return [(family, kind, 6, "", (ip, port))]

    with pytest.raises(OSError, match="non-public"):
        NameCache(lookup=lookup, public_only=True).resolve("news.example.org", 443)


def test_feed_updates_preserve_read_state_and_cached_entries(conn):
    repo = ReadingRepository(conn)
    feed_id = repo.add(URL, "Technology", document(2))
    articles, more = repo.articles(ReadingQuery())
    assert len(articles) == 2 and not more
    repo.mark_read(articles[0].id, True)
    updated = replace(
        document(2), entries=(replace(document(2).entries[1], title="Updated"),)
    )
    repo.store(feed_id, updated)
    items, _ = repo.articles(ReadingQuery())
    assert len(items) == 2 and items[0].is_read
    assert items[0].title == "Updated"
    repo.record_error(feed_id, "NetworkUnavailable")
    assert repo.feeds()[0].last_error
    assert len(repo.articles(ReadingQuery())[0]) == 2
    repo.store(feed_id, document(2))
    assert repo.feeds()[0].last_error is None


def test_categories_search_paging_removal_and_storage_limits(conn):
    repo = ReadingRepository(conn)
    feed_id = repo.add(URL, "Technology", document(ARTICLES_PER_FEED + 20))
    first, more = repo.articles(ReadingQuery(category="Technology"))
    second, _ = repo.articles(ReadingQuery(), PAGE_SIZE)
    assert len(first) == len(second) == PAGE_SIZE and more
    assert not ({a.id for a in first} & {a.id for a in second})
    assert (
        conn.execute("SELECT COUNT(*) FROM reading_articles").fetchone()[0]
        == ARTICLES_PER_FEED
    )
    assert not repo.articles(ReadingQuery(text="%"))[0]
    repo.edit(feed_id, "Health")
    assert not repo.articles(ReadingQuery(category="Technology"))[0]
    assert repo.articles(ReadingQuery(category="Health"))[0]
    repo.remove(feed_id)
    assert conn.execute("SELECT COUNT(*) FROM reading_articles").fetchone()[0] == 0


def test_duplicate_sources_and_long_topics_are_rejected(conn):
    repo = ReadingRepository(conn)
    repo.add(URL, "", document())
    with pytest.raises(ReadingError, match="duplicate"):
        repo.add(URL, "", document())
    with pytest.raises(ReadingError, match="category-too-long"):
        repo.add(URL + "2", "x" * 61, document())


def test_categories_reuse_unicode_names_and_moves_preserve_article_state(conn):
    repo = ReadingRepository(conn)
    first = repo.add(URL, "Saúde", document(2))
    second = repo.add(URL + "2", "  SAÚDE  ", document(1))
    third = repo.add(URL + "3", "Ciência de dados", document(1))
    assert {feed.category for feed in repo.feeds()} == {"Saúde", "Ciência de dados"}
    articles, _ = repo.articles(ReadingQuery(category="Saúde"))
    selected = next(article for article in articles if article.feed_id == second)
    repo.mark_read(selected.id, True)
    repo.edit(second, "  ciência   DE dados  ")
    moved, _ = repo.articles(ReadingQuery(category="Ciência de dados"))
    assert {article.feed_id for article in moved} == {second, third}
    assert next(article for article in moved if article.feed_id == second).is_read
    assert len(repo.articles(ReadingQuery(category="Saúde"))[0]) == 2
    repo.edit(first, "  ")
    assert next(feed for feed in repo.feeds() if feed.id == first).category == ""
    assert len(repo.articles(ReadingQuery())[0]) == 4


def test_removing_one_source_preserves_other_sources_and_wallpaper_settings(conn):
    repo = ReadingRepository(conn)
    removed = repo.add(URL, "Technology", document(2))
    kept = repo.add(URL + "2", "Health", document(1))
    article = repo.articles(ReadingQuery(category="Health"))[0][0]
    repo.mark_read(article.id, True)
    conn.execute("INSERT INTO settings VALUES ('reading-removal-check','keep')")
    repo.remove(removed)
    assert [feed.id for feed in repo.feeds()] == [kept]
    remaining = repo.articles(ReadingQuery())[0]
    assert len(remaining) == 1 and remaining[0].is_read
    assert not conn.execute(
        "SELECT 1 FROM reading_articles WHERE feed_id=?", (removed,)
    ).fetchone()
    assert (
        conn.execute(
            "SELECT value FROM settings WHERE key='reading-removal-check'"
        ).fetchone()[0]
        == "keep"
    )


def test_migration_preserves_existing_wallpaper_state(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    for migration in MIGRATIONS[:7]:
        for sql in migration:
            old.execute(sql)
    old.execute("PRAGMA user_version=7")
    old.execute("INSERT INTO settings VALUES ('example','keep')")
    old.commit()
    old.close()
    conn = connect(path)
    assert (
        conn.execute("SELECT value FROM settings WHERE key='example'").fetchone()[0]
        == "keep"
    )
    assert ReadingRepository(conn).feeds() == []
    conn.close()


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.status, self.headers = 200, {"Content-Length": str(len(body))}


def test_only_explicit_source_actions_fetch_and_offline_cache_survives(tmp_path):
    requests = []
    offline = [False]

    def opener(request, timeout):
        requests.append(request.full_url)
        if offline[0]:
            raise urllib.error.URLError("offline")
        return Response(RSS)

    service = ReadingService(tmp_path / "library.sqlite3", HttpClient(opener=opener))
    assert service.snapshot(ReadingQuery()) == ([], [], False)
    assert requests == []
    cancel = threading.Event()
    service.add(URL, "Technology", cancel)
    feeds, articles, more = service.snapshot(ReadingQuery())
    assert len(feeds) == 1 and len(articles) == 2 and requests == [URL]
    offline[0] = True
    assert service.refresh(cancel) == 1
    assert len(service.snapshot(ReadingQuery())[1]) == 2
    assert requests == [
        URL,
        URL,
    ]  # No articles, tracking pixels or external links fetched.
    cancel.set()
    with pytest.raises(TransferCancelled):
        service.refresh(cancel)
