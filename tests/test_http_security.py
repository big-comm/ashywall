"""Transport security on direct and proxy paths, without opening sockets."""

import io
import urllib.error
import urllib.request
from email.message import Message

import pytest

from ashywall.services import http
from ashywall.services.connections import ConnectionPool


class Response(io.BytesIO):
    def __init__(self, status: int, location: str | None = None) -> None:
        super().__init__(b"ok" if status == 200 else b"")
        self.status = self.code = status
        self.reason = self.msg = "Test response"
        self.headers = Message()
        self.headers["Content-Length"] = "2" if status == 200 else "0"
        if location is not None:
            self.headers["Location"] = location

    def info(self) -> Message:
        return self.headers

    def isclosed(self) -> bool:
        return self.closed or self.tell() == int(self.headers["Content-Length"])


@pytest.fixture(params=["direct", "proxy"])
def transport(request, monkeypatch):
    destinations = []
    replies = []

    def response(url):
        destinations.append(url)
        return replies.pop(0)

    if request.param == "direct":
        pool = ConnectionPool()

        def send(host, port, target, headers, timeout):
            return response(f"https://{host}:{port}{target}")

        monkeypatch.setattr(pool, "_send", send)
        monkeypatch.setattr(http, "_POOL", pool)
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    else:

        class HTTPS(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return response(req.full_url)

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), http._HttpsOnlyRedirects(), HTTPS()
        )
        monkeypatch.setattr(http, "_URLLIB_OPENER", opener)
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "proxy"})
    return http.HttpClient(), destinations, replies


@pytest.mark.parametrize("location", ["/next", "https://www.bing.com:443/next"])
def test_same_origin_redirect_succeeds(transport, location):
    client, destinations, replies = transport
    replies.extend([Response(302, location), Response(200)])
    assert client.get_bytes("https://www.bing.com/start", max_bytes=10) == b"ok"
    assert len(destinations) == 2


def test_batched_api_query_can_exceed_image_url_limit(transport):
    client, destinations, replies = transport
    replies.append(Response(200))
    params = {
        "titles": "|".join(
            f"File:Landscape with a descriptive name {i}.jpg" for i in range(60)
        )
    }
    assert (
        client.get_bytes(
            "https://commons.wikimedia.org/w/api.php", params=params, max_bytes=10
        )
        == b"ok"
    )
    assert len(destinations[0]) > 2048


def test_oversized_api_query_is_refused_without_network(transport):
    client, destinations, replies = transport
    replies.append(Response(200))
    with pytest.raises(http.HttpError):
        client.get_bytes(
            "https://commons.wikimedia.org/w/api.php",
            params={"titles": "x" * (16 * 1024)},
            max_bytes=10,
        )
    assert destinations == []


@pytest.mark.parametrize("status", [301, 303, 307, 308])
def test_all_redirect_statuses_enforce_the_same_policy(transport, status):
    client, destinations, replies = transport
    replies.extend([Response(status, "https://evil.example/private"), Response(200)])
    with pytest.raises(http.HttpError):
        client.get_bytes("https://www.bing.com/start", max_bytes=10)
    assert len(destinations) == 1


@pytest.mark.parametrize(
    "location",
    [
        "http://www.bing.com/next",
        "https://127.0.0.1/private",
        "https://evil.example/next",
        "https://www.bing.com.evil.example/next",
        "https://www.bing.com:8443/next",
        "https://user:secret@www.bing.com/next",
        "https://www.bing.com:bad/next",
        "https://[broken/next",
    ],
)
def test_unsafe_redirect_is_refused_before_contacting_target(transport, location):
    client, destinations, replies = transport
    redirect = Response(302, location)
    replies.extend([redirect, Response(200)])
    with pytest.raises(http.HttpError):
        client.get_bytes("https://www.bing.com/start", max_bytes=10)
    assert len(destinations) == 1
    assert redirect.closed


@pytest.mark.parametrize(
    "url",
    [
        "http://www.bing.com/image",
        "https://user:secret@www.bing.com/image",
        "https://www.bing.com:bad/image",
        "https://[broken/image",
        "https://www.bi\tng.com/image",
    ],
)
def test_bad_initial_url_is_refused_without_network(transport, url):
    client, destinations, replies = transport
    replies.append(Response(200))
    with pytest.raises(http.HttpError):
        client.get_bytes(url, max_bytes=10)
    assert destinations == []
