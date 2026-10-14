"""BigLinux provider: GitHub listing, AVIF header sizes and the cache (no network)."""

import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ashywall.models.resolution import Orientation
from ashywall.providers.base import ProviderError, RemoteQuery
from ashywall.providers.biglinux import (
    BigLinuxProvider,
    isobmff_dimensions,
    parse_tree,
    slug_of,
    title_of,
)
from ashywall.services.http import HttpClient

COMMIT = "b1fe70c28d61b67134e390d1053b1d2e9088d628"


def avif_header(width: int, height: int, rotation: int | None = None) -> bytes:
    ispe = (20).to_bytes(4, "big") + b"ispe" + b"\0\0\0\0"
    ispe += width.to_bytes(4, "big") + height.to_bytes(4, "big")
    irot = (
        b""
        if rotation is None
        else (9).to_bytes(4, "big") + b"irot" + bytes([rotation])
    )
    return b"\0\0\0\x1cftypavif" + b"\0" * 16 + ispe + irot + b"\0" * 64


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body))}


class Router:
    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[str] = []

    def __call__(self, request: urllib.request.Request, timeout: float):
        self.requests.append(request.full_url)
        for prefix, body in self.routes.items():
            if request.full_url.startswith(prefix):
                return FakeResponse(body)
        raise urllib.error.URLError("no route")


def tree(*names: str) -> bytes:
    entries = [
        {
            "type": "blob",
            "path": f"usr/share/wallpapers/{name}",
            "size": 1000,
            "sha": f"{i:040x}",
        }
        for i, name in enumerate(names, start=1)
    ]
    entries += [
        {"type": "blob", "path": "pkgbuild/PKGBUILD", "size": 900, "sha": "f" * 40},
        {"type": "tree", "path": "usr/share/wallpapers", "sha": "e" * 40},
    ]
    return json.dumps({"tree": entries}).encode()


def router_for(
    base: tuple[str, ...], extra: tuple[str, ...], sizes: dict[str, bytes]
) -> Router:
    api = "https://api.github.com/repos/biglinux"
    routes: dict[str, bytes] = {}
    for repository, names in (
        ("biglinux-wallpapers-base", base),
        ("extra-biglinux-wallpapers", extra),
    ):
        routes[f"{api}/{repository}/branches/main"] = json.dumps(
            {"commit": {"sha": COMMIT}}
        ).encode()
        routes[f"{api}/{repository}/git/trees/{COMMIT}"] = tree(*names)
    for name, body in sizes.items():
        routes[
            f"https://raw.githubusercontent.com/biglinux/extra-biglinux-wallpapers/{COMMIT}/usr/share/wallpapers/{name}"
        ] = body
        routes[
            f"https://raw.githubusercontent.com/biglinux/biglinux-wallpapers-base/{COMMIT}/usr/share/wallpapers/{name}"
        ] = body
    return Router(routes)


def test_avif_header_gives_the_displayed_size() -> None:
    assert isobmff_dimensions(avif_header(3840, 2161)) == (3840, 2161)
    assert isobmff_dimensions(avif_header(3840, 2161, rotation=1)) == (2161, 3840)
    with pytest.raises(ProviderError):
        isobmff_dimensions(b"\x89PNG not an avif")
    with pytest.raises(ProviderError):
        isobmff_dimensions(avif_header(0, 10))


def test_tree_keeps_only_wallpapers() -> None:
    payload = json.loads(tree("animal owl2.heic", "notes.txt"))
    payload["tree"].append(
        {
            "type": "blob",
            "path": "usr/share/wallpapers/sub/x.heic",
            "size": 5,
            "sha": "a" * 40,
        }
    )
    files = parse_tree(payload, "extra-biglinux-wallpapers", COMMIT)
    assert [f.name for f in files] == ["animal owl2.heic"]
    assert title_of("animal owl2.heic") == "Animal owl2"
    assert slug_of("abstract colors11.heic") == "abstract-colors11"


def test_search_lists_both_packages_and_caches(tmp_path: Path) -> None:
    sizes = {
        "animal%20owl2.heic": avif_header(3840, 2161),
        "abstract%20art.heic": avif_header(3840, 2161),
        "tall%20tree.heic": avif_header(2000, 3000),
    }
    router = router_for(
        ("abstract art.heic",), ("animal owl2.heic", "tall tree.heic"), sizes
    )
    cache = tmp_path / "biglinux.json"
    provider = BigLinuxProvider(cache, HttpClient(opener=router))

    page = provider.search(RemoteQuery())
    assert [item.title for item in page.items] == [
        "Abstract art",
        "Animal owl2",
        "Tall tree",
    ]
    first = page.items[0]
    assert (first.width, first.height, first.license) == (3840, 2161, "GPL")
    assert first.description == "biglinux-wallpapers-base"
    assert first.license_url.endswith(
        "/biglinux-wallpapers-base/blob/main/pkgbuild/PKGBUILD"
    )
    assert COMMIT in first.original_url and "%20" in first.original_url
    assert page.total == 3

    assert [i.title for i in provider.search(RemoteQuery(text="OWL")).items] == [
        "Animal owl2"
    ]
    portrait = provider.search(RemoteQuery(orientation=Orientation.PORTRAIT))
    assert [i.title for i in portrait.items] == ["Tall tree"] and portrait.total is None

    # A new session reuses the cached list and sizes without touching the network.
    requests = len(router.requests)
    again = BigLinuxProvider(cache, HttpClient(opener=Router({})))
    assert len(again.search(RemoteQuery()).items) == 3
    assert len(router.requests) == requests


def test_stale_list_is_used_when_github_fails(tmp_path: Path) -> None:
    cache = tmp_path / "biglinux.json"
    router = router_for(
        (), ("animal owl2.heic",), {"animal%20owl2.heic": avif_header(3840, 2161)}
    )
    BigLinuxProvider(cache, HttpClient(opener=router)).search(RemoteQuery())
    later = BigLinuxProvider(cache, HttpClient(opener=Router({})), clock=lambda: 10**12)
    assert [i.title for i in later.search(RemoteQuery()).items] == ["Animal owl2"]
