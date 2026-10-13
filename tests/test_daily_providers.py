"""Bing and Wikimedia Commons daily feeds, parsed from recorded-shape responses (no network)."""

import datetime as dt
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from conftest import make_image

from ashywall.models.resolution import UHD_4K, Orientation
from ashywall.providers.base import ProviderError, RemoteQuery
from ashywall.providers.bing import BingProvider, credit_of, parse_entry, probe_prefix
from ashywall.providers.commons import (
    CommonsProvider,
    days_for_page,
    last_page,
    parse_file,
    plain_text,
)
from ashywall.services.http import HttpClient


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None):
        super().__init__(body)
        self.status = status
        self.headers = (
            headers if headers is not None else {"Content-Length": str(len(body))}
        )


class Router:
    """Answers by URL prefix; callables receive the request (to inspect headers)."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        for prefix, result in self.routes.items():
            if request.full_url.startswith(prefix):
                return result(request) if callable(result) else FakeResponse(result)
        raise urllib.error.URLError("no route")


def bing_entry(hsh: str = "e2b35d96ba2728fa19ff20230314b928", **overrides) -> dict:
    entry = {
        "startdate": "20261013",
        "urlbase": "/th?id=OHR.PlitviceAutumn_PT-BR4568960471",
        "copyright": "Parque Nacional dos Lagos de Plitvice, Croácia (© Jan Wlodarczyk/Alamy)",
        "copyrightlink": "https://www.bing.com/search?q=Plitvice",
        "title": "A sinfonia das cachoeiras",
        "hsh": hsh,
    }
    entry.update(overrides)
    return entry


def test_bing_entry_is_validated() -> None:
    url, fields = parse_entry(bing_entry())
    assert (
        url == "https://www.bing.com/th?id=OHR.PlitviceAutumn_PT-BR4568960471_UHD.jpg"
    )
    assert fields["title"] == "A sinfonia das cachoeiras"
    assert fields["author"] == "Jan Wlodarczyk/Alamy"
    assert fields["date"] == "2026-10-13"
    assert fields["thumbnail_url"].endswith("_800x480.jpg")
    for bad in (
        {"hsh": "nothex"},
        {"urlbase": "https://evil.example/x"},
        {"urlbase": "/th?id=OHR.x&redirect=evil"},
    ):
        with pytest.raises(ProviderError):
            parse_entry(bing_entry(**bad))
    assert credit_of("No credit here") is None


def test_probe_reads_dimensions_from_the_first_bytes(tmp_path: Path) -> None:
    data = make_image(tmp_path / "uhd.jpg", (384, 216), format="JPEG").read_bytes()
    assert probe_prefix(data[:4096]) == (384, 216, "JPEG")
    with pytest.raises(ProviderError):
        probe_prefix(b"<html>not an image</html>")


def test_bing_search_probes_each_original_once(tmp_path: Path) -> None:
    image = make_image(tmp_path / "uhd.jpg", (3840, 2160), format="JPEG").read_bytes()
    entries = [
        bing_entry(),
        bing_entry(hsh="a" * 32, urlbase="/th?id=OHR.Other_PT-BR1"),
    ]

    def ranged(request):
        assert request.get_header("Range") == "bytes=0-65535"
        return FakeResponse(
            image[:65536],
            status=206,
            headers={"Content-Range": f"bytes 0-65535/{len(image)}"},
        )

    router = Router(
        {
            "https://www.bing.com/HPImageArchive.aspx": json.dumps(
                {"images": [*entries, {"hsh": "broken"}]}
            ).encode(),
            "https://www.bing.com/th?id=OHR.": ranged,
        }
    )
    provider = BingProvider(HttpClient(opener=router))
    page = provider.search(RemoteQuery())
    assert [item.title for item in page.items] == ["A sinfonia das cachoeiras"] * 2
    first = page.items[0]
    assert (first.width, first.height, first.file_size) == (3840, 2160, len(image))
    assert first.license is None and first.author == "Jan Wlodarczyk/Alamy"
    assert page.skipped == 1 and page.has_more
    probes = sum("_UHD.jpg" in r.full_url for r in router.requests)
    provider.search(RemoteQuery(orientation=Orientation.PORTRAIT))
    assert sum("_UHD.jpg" in r.full_url for r in router.requests) == probes  # Cached.
    archive = next(r for r in router.requests if "HPImageArchive" in r.full_url)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(archive.full_url).query)
    assert query["idx"] == ["0"] and query["n"] == ["8"]


def commons_file(page_id: int = 139933531, title: str = "File:Elephant.jpg", **info):
    data = {
        "url": "https://upload.wikimedia.org/wikipedia/commons/d/d1/Elephant.jpg",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Elephant.jpg",
        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d1/Elephant.jpg/500px-Elephant.jpg",
        "width": 5139,
        "height": 3426,
        "size": 22323125,
        "mime": "image/jpeg",
        "extmetadata": {
            "ObjectName": {"value": "Desert elephant spraying sand"},
            "Artist": {
                "value": '<a href="//commons.wikimedia.org/wiki/User:X">Charles J. Sharp</a>'
            },
            "LicenseShortName": {"value": "CC BY-SA 4.0"},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
        },
    }
    data.update(info)
    return {"pageid": page_id, "title": title, "imageinfo": [data]}


def test_commons_file_keeps_author_and_license() -> None:
    item = parse_file(commons_file(), dt.date(2026, 10, 13))
    assert item.title == "Desert elephant spraying sand"
    assert item.author == "Charles J. Sharp"
    assert item.license == "CC BY-SA 4.0"
    assert item.license_url == "https://creativecommons.org/licenses/by-sa/4.0"
    assert item.date == "2026-10-13" and item.remote_id == "139933531"
    # Previews use Wikimedia's standard 1920 px width; small files keep the thumbnail.
    assert "/1920px-Elephant.jpg" in item.preview_url
    small = parse_file(commons_file(width=1600, height=900), None)
    assert small.preview_url == small.thumbnail_url
    for bad in (
        {"mime": "image/svg+xml"},
        {"url": "https://evil.example/x.jpg"},
        {"width": 0},
        {"width": 40000, "height": 40000},
        {"size": None},
    ):
        with pytest.raises(ProviderError):
            parse_file(commons_file(**bad), None)
    assert plain_text("<b>A</b>  &amp; <i>B</i>") == "A & B"


def test_commons_pages_cover_thirty_days_back_to_the_start() -> None:
    today = dt.date(2026, 10, 13)
    days = days_for_page(1, today)
    assert days[0] == today and len(days) == 30
    assert days_for_page(2, today)[0] == today - dt.timedelta(days=30)
    assert days_for_page(last_page(today) + 1, today) == []


def test_commons_search_maps_days_to_files() -> None:
    images = {
        "query": {
            "pages": [
                {
                    "title": "Template:Potd/2026-10-13",
                    "images": [{"title": "File:Elephant.jpg"}],
                },
                {
                    "title": "Template:Potd/2026-10-12",
                    "images": [{"title": "File:Tall.jpg"}],
                },
            ]
        }
    }
    info = {
        "query": {
            "pages": [
                commons_file(),
                commons_file(2, "File:Tall.jpg", width=3000, height=4500),
            ]
        }
    }

    def api(request):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        body = images if query["prop"] == ["images"] else info
        return FakeResponse(json.dumps(body).encode())

    router = Router({"https://commons.wikimedia.org/w/api.php": api})
    provider = CommonsProvider(
        HttpClient(opener=router), today=lambda: dt.date(2026, 10, 13)
    )
    page = provider.search(RemoteQuery())
    assert [(i.date, i.remote_id) for i in page.items] == [
        ("2026-10-13", "139933531"),
        ("2026-10-12", "2"),
    ]
    assert len(router.requests) == 2
    assert router.requests[0].get_header("User-agent").startswith("AshyWall/")
    landscape_4k = provider.search(
        RemoteQuery(orientation=Orientation.LANDSCAPE, min_tier=UHD_4K)
    )
    assert [i.remote_id for i in landscape_4k.items] == ["139933531"]
