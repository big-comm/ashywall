"""Online provider, HTTP client and downloads, with a fake opener (no network)."""

import io
import json
import threading
import urllib.error
from pathlib import Path

import pytest
from conftest import make_image

from ashywall.models.resolution import UHD_4K, Orientation
from ashywall.providers.base import (
    ProviderError,
    RemoteQuery,
    RemoteSort,
    RemoteWallpaper,
    validated_url,
)
from ashywall.providers.wallhaven import (
    IMAGE_HOSTS,
    THUMBNAIL_HOSTS,
    WallhavenProvider,
    parse_search,
    parse_wallpaper,
    search_params,
)
from ashywall.services.http import (
    HttpClient,
    HttpError,
    NetworkUnavailable,
    RateLimited,
    ResponseTooLarge,
    SlidingWindowLimiter,
    TransferCancelled,
)
from ashywall.services.library_repository import LibraryRepository, Section
from ashywall.services.remote_images import (
    DownloadError,
    RemoteThumbnailCache,
    download_original,
)


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, length: bool = True) -> None:
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body))} if length else {}


class FakeOpener:
    """Maps URL prefixes to bodies (bytes) or exceptions; records requests."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requests: list[str] = []

    def __call__(self, request, timeout):
        url = request.full_url
        self.requests.append(url)
        for prefix, result in self.routes.items():
            if url.startswith(prefix):
                if isinstance(result, Exception):
                    raise result
                return FakeResponse(result)
        raise urllib.error.URLError("no route")


def listing(
    remote_id: str = "abc123", width: int = 3840, height: int = 2160, **overrides
) -> dict:
    item = {
        "id": remote_id,
        "url": f"https://wallhaven.cc/w/{remote_id}",
        "short_url": f"https://whvn.cc/{remote_id}",
        "source": "https://example.org/art",
        "purity": "sfw",
        "category": "general",
        "dimension_x": width,
        "dimension_y": height,
        "resolution": f"{width}x{height}",
        "ratio": "1.78",
        "file_size": 1000,
        "file_type": "image/png",
        "path": f"https://w.wallhaven.cc/full/ab/wallhaven-{remote_id}.png",
        "thumbs": {
            "large": f"https://th.wallhaven.cc/lg/ab/{remote_id}.jpg",
            "original": f"https://th.wallhaven.cc/orig/ab/{remote_id}.jpg",
            "small": f"https://th.wallhaven.cc/small/ab/{remote_id}.jpg",
        },
    }
    item.update(overrides)
    return item


def search_payload(
    *items: dict, last_page: int = 3, total: int = 70, seed=None
) -> dict:
    return {
        "data": list(items),
        "meta": {
            "current_page": 1,
            "last_page": last_page,
            "per_page": 24,
            "total": total,
            "query": None,
            "seed": seed,
        },
    }


# Provider parsing and parameters ---------------------------------------------


def test_parse_valid_listing() -> None:
    item = parse_wallpaper(listing())
    assert (item.origin, item.remote_id, item.width, item.height) == (
        "wallhaven",
        "abc123",
        3840,
        2160,
    )
    assert item.original_url.startswith("https://w.wallhaven.cc/")
    assert item.source_url == "https://example.org/art"
    assert item.license is None  # Wallhaven does not provide licenses.


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "../etc"},
        {"path": "http://w.wallhaven.cc/full/x.png"},  # Not https.
        {"path": "https://evil.example/full/x.png"},  # Foreign host.
        {"path": "https://user@w.wallhaven.cc/x.png"},
        {"thumbs": {"large": "https://th.wallhaven.cc.evil.example/x.jpg"}},
        {"dimension_x": 0},
        {"dimension_y": "2160"},
        {"file_type": "image/gif"},
        {"thumbs": None},
    ],
)
def test_malformed_or_unsafe_listings_are_rejected(overrides: dict) -> None:
    with pytest.raises(ProviderError):
        parse_wallpaper(listing(**overrides))


def test_source_link_is_display_only_and_sanitized() -> None:
    assert parse_wallpaper(listing(source="javascript:alert(1)")).source_url is None
    assert parse_wallpaper(listing(source="")).source_url is None


def test_search_skips_bad_items_and_applies_local_criteria() -> None:
    payload = search_payload(
        listing("good01"),
        listing("bad"),  # Id too short.
        listing("small1", width=1920, height=1080),
        listing("tall01", width=2160, height=3840),
    )
    page = parse_search(payload, RemoteQuery(min_tier=UHD_4K))
    assert [item.remote_id for item in page.items] == ["good01", "tall01"]
    assert page.skipped == 2
    assert page.total is None  # Local filtering makes the provider total meaningless.
    assert page.has_more

    portrait = parse_search(
        payload, RemoteQuery(orientation=Orientation.PORTRAIT, min_tier=UHD_4K)
    )
    assert [item.remote_id for item in portrait.items] == ["tall01"]
    assert portrait.total == 70


def test_search_params_are_safe_by_default() -> None:
    params = search_params(RemoteQuery())
    assert params["purity"] == "100"
    assert params["sorting"] == "toplist"
    assert "apikey" not in params


@pytest.mark.parametrize(
    ("orientation", "ratios", "atleast"),
    [
        (Orientation.LANDSCAPE, "landscape", "3840x2160"),
        (Orientation.PORTRAIT, "portrait", "2160x3840"),
        (None, None, "2160x2160"),
    ],
)
def test_resolution_mapping(orientation, ratios, atleast) -> None:
    params = search_params(RemoteQuery(orientation=orientation, min_tier=UHD_4K))
    assert params.get("ratios") == ratios
    assert params["atleast"] == atleast


def test_random_sorting_keeps_a_valid_seed_only() -> None:
    assert (
        search_params(RemoteQuery(sort=RemoteSort.RANDOM, seed="aB3dE9"))["seed"]
        == "aB3dE9"
    )
    assert "seed" not in search_params(
        RemoteQuery(sort=RemoteSort.RANDOM, seed="bad seed")
    )


def test_provider_search_and_api_errors() -> None:
    opener = FakeOpener(
        {
            "https://wallhaven.cc/api/v1/search": json.dumps(
                search_payload(listing())
            ).encode()
        }
    )
    provider = WallhavenProvider(HttpClient(opener=opener))
    page = provider.search(RemoteQuery(text="owl  "))
    assert len(page.items) == 1
    assert "q=owl" in opener.requests[0] and "purity=100" in opener.requests[0]

    offline = WallhavenProvider(HttpClient(opener=FakeOpener({})))
    with pytest.raises(NetworkUnavailable):
        offline.search(RemoteQuery())

    limited = urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "30"}, None)
    with pytest.raises(RateLimited) as caught:
        WallhavenProvider(HttpClient(opener=FakeOpener({"https://": limited}))).search(
            RemoteQuery()
        )
    assert caught.value.retry_after == 30

    garbage = WallhavenProvider(HttpClient(opener=FakeOpener({"https://": b"<html>"})))
    with pytest.raises(HttpError):
        garbage.search(RemoteQuery())


def test_validated_url() -> None:
    assert validated_url("https://w.wallhaven.cc/x.png", IMAGE_HOSTS)
    for bad in (
        "https://w.wallhaven.cc:8443/x",
        "ftp://w.wallhaven.cc/x",
        None,
        "https://th.wallhaven.cc/x",
    ):
        with pytest.raises(ProviderError):
            validated_url(bad, IMAGE_HOSTS)


def test_remote_wallpaper_json_round_trip() -> None:
    item = parse_wallpaper(listing()).with_state(
        is_favorite=True, local_path=Path("/x")
    )
    restored = RemoteWallpaper.from_json(item.to_json())
    assert restored == item
    assert restored.local_path is None
    with pytest.raises(ProviderError):
        RemoteWallpaper.from_json(json.dumps({"origin": "x", "unexpected": 1}))


# HTTP client -------------------------------------------------------------------


def test_limiter_spaces_out_calls() -> None:
    now = [0.0]
    limiter = SlidingWindowLimiter(2, window=60.0, clock=lambda: now[0])
    assert limiter.wait_time() == 0
    assert limiter.wait_time() == 0
    assert limiter.wait_time() == pytest.approx(60.0)
    now[0] = 61.0
    assert limiter.wait_time() == 0


def test_response_size_limit() -> None:
    client = HttpClient(opener=FakeOpener({"https://": b"x" * 100}))
    with pytest.raises(ResponseTooLarge):
        client.get_bytes("https://example.org/", max_bytes=50)


# Thumbnails and downloads -----------------------------------------------------------


def png_bytes(tmp_path: Path, size: tuple[int, int]) -> bytes:
    return make_image(tmp_path / f"src-{size[0]}x{size[1]}.png", size).read_bytes()


def test_remote_thumbnail_is_validated_and_reencoded(tmp_path: Path) -> None:
    url = "https://th.wallhaven.cc/lg/ab/abc123.jpg"
    opener = FakeOpener({url: png_bytes(tmp_path, (432, 243))})
    cache = RemoteThumbnailCache(
        tmp_path / "cache", HttpClient(opener=opener), THUMBNAIL_HOSTS
    )
    first = cache.ensure(url)
    assert first.read_bytes()[:2] == b"\xff\xd8"  # Re-encoded as JPEG.
    assert cache.ensure(url) == first
    assert len(opener.requests) == 1

    with pytest.raises(ProviderError):
        cache.ensure("https://evil.example/x.jpg")


def test_corrupted_remote_thumbnail_is_not_cached(tmp_path: Path) -> None:
    url = "https://th.wallhaven.cc/lg/ab/broken.jpg"
    cache = RemoteThumbnailCache(
        tmp_path / "cache",
        HttpClient(opener=FakeOpener({url: b"nope" * 50})),
        THUMBNAIL_HOSTS,
    )
    with pytest.raises(Exception):
        cache.ensure(url)
    assert not [p for p in (tmp_path / "cache").rglob("*") if p.is_file()]


def remote_item(
    width: int = 64, height: int = 36, file_size: int | None = None
) -> RemoteWallpaper:
    return parse_wallpaper(
        listing("abc123", width=width, height=height, file_size=file_size or 10_000)
    )


def test_download_is_verified_and_atomic(tmp_path: Path) -> None:
    body = png_bytes(tmp_path, (64, 36))
    item = remote_item(file_size=len(body))
    opener = FakeOpener({item.original_url: body})
    progress: list[tuple[int, int | None]] = []
    target = tmp_path / "downloads"

    path = download_original(
        item,
        HttpClient(opener=opener),
        target,
        IMAGE_HOSTS,
        on_progress=lambda r, t: progress.append((r, t)),
    )

    assert path == target / "wallhaven-abc123.png"
    assert path.read_bytes() == body
    assert progress[-1] == (len(body), len(body))
    assert [p.name for p in target.iterdir()] == [
        "wallhaven-abc123.png"
    ]  # No leftovers.
    # A second request reuses the file without downloading again.
    assert (
        download_original(item, HttpClient(opener=opener), target, IMAGE_HOSTS) == path
    )
    assert len(opener.requests) == 1


def test_download_with_wrong_dimensions_is_discarded(tmp_path: Path) -> None:
    body = png_bytes(tmp_path, (64, 36))
    item = remote_item(width=3840, height=2160, file_size=len(body))
    with pytest.raises(DownloadError, match="announced"):
        download_original(
            item,
            HttpClient(opener=FakeOpener({item.original_url: body})),
            tmp_path / "d",
            IMAGE_HOSTS,
        )
    assert list((tmp_path / "d").iterdir()) == []


def test_download_that_is_not_an_image_is_discarded(tmp_path: Path) -> None:
    item = remote_item(file_size=500)
    with pytest.raises(DownloadError):
        download_original(
            item,
            HttpClient(opener=FakeOpener({item.original_url: b"<html>" * 50})),
            tmp_path / "d",
            IMAGE_HOSTS,
        )
    assert list((tmp_path / "d").iterdir()) == []


def test_download_larger_than_announced_is_refused(tmp_path: Path) -> None:
    item = remote_item(file_size=10)
    huge = b"x" * (10 + 1024 * 1024 + 1)
    with pytest.raises(ResponseTooLarge):
        download_original(
            item,
            HttpClient(opener=FakeOpener({item.original_url: huge})),
            tmp_path / "d",
            IMAGE_HOSTS,
        )
    assert list((tmp_path / "d").iterdir()) == []


def test_download_can_be_cancelled(tmp_path: Path) -> None:
    body = png_bytes(tmp_path, (64, 36))
    item = remote_item(file_size=len(body))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(TransferCancelled):
        download_original(
            item,
            HttpClient(opener=FakeOpener({item.original_url: body})),
            tmp_path / "d",
            IMAGE_HOSTS,
            cancel=cancel,
        )
    assert list((tmp_path / "d").iterdir()) == []


def test_download_from_foreign_host_is_refused(tmp_path: Path) -> None:
    item = remote_item()
    with pytest.raises(DownloadError):
        download_original(
            item,
            HttpClient(opener=FakeOpener({})),
            tmp_path / "d",
            frozenset({"other.example"}),
        )


# Catalog -------------------------------------------------------------------------------


def test_remote_favorites_and_download_records(conn) -> None:
    repository = LibraryRepository(conn)
    item = parse_wallpaper(listing())
    repository.set_remote_favorite(item, True)
    assert repository.remote_favorite_ids("wallhaven") == {"abc123"}
    assert repository.remote_favorites() == [item]
    assert repository.count_by_section()[Section.FAVORITES] == 1
    repository.set_remote_favorite(item, False)
    assert repository.remote_favorites() == []


def test_download_records_only_report_existing_files(conn, tmp_path: Path) -> None:
    repository = LibraryRepository(conn)
    item = parse_wallpaper(listing())
    path = make_image(tmp_path / "wallhaven-abc123.png", (8, 8))
    repository.record_download(item, path)
    assert repository.downloaded_paths("wallhaven") == {"abc123": path}
    record = repository.download_record_for(path)
    assert record is not None and record.page_url == item.page_url
    path.unlink()
    assert repository.downloaded_paths("wallhaven") == {}
