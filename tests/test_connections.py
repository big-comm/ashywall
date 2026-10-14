"""Connection reuse, redirects and the name cache, against a local server (no network)."""

import http.client
import http.server
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from ashywall.services.connections import ConnectionPool, NameCache


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # Keep-alive.

    def do_GET(self) -> None:  # noqa: N802
        self.server.peers.add(self.client_address)  # type: ignore[attr-defined]
        routes = {
            "/small": (200, b"hello", {}),
            "/big": (200, b"x" * 300_000, {}),
            "/missing": (404, b"not here", {}),
            "/to-small": (302, b"", {"Location": "/small"}),
            "/to-http": (302, b"", {"Location": "http://example.com/"}),
        }
        status, body, headers = routes[self.path]
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[http.server.ThreadingHTTPServer]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.peers = set()  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def pool() -> Iterator[ConnectionPool]:
    pool = local_pool()
    yield pool
    pool.close()


def local_pool() -> ConnectionPool:
    def lookup(host, port, family, kind):
        if family != socket.AF_INET:
            raise socket.gaierror("no IPv6 here")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    # Plain HTTP stands in for TLS; the pool still only accepts https URLs.
    return ConnectionPool(
        NameCache(lookup), connection_class=http.client.HTTPConnection
    )


def get(pool: ConnectionPool, url: str, read: bool = True) -> bytes:
    with pool.open(urllib.request.Request(url), 5.0) as response:
        return response.read() if read else response.read(10)


def test_connections_are_reused_and_dropped_when_needed(
    server, pool: ConnectionPool
) -> None:
    base = f"https://localhost:{server.server_port}"
    assert get(pool, f"{base}/small") == b"hello"
    assert get(pool, f"{base}/small") == b"hello"
    assert len(server.peers) == 1  # One connection for both requests.

    get(pool, f"{base}/big", read=False)  # Body left unread: connection dropped.
    assert get(pool, f"{base}/small") == b"hello"
    assert len(server.peers) == 2

    with pytest.raises(urllib.error.HTTPError) as missing:
        get(pool, f"{base}/missing")
    assert missing.value.code == 404
    missing.value.close()  # HttpClient closes error responses the same way.
    assert get(pool, f"{base}/small") == b"hello"
    assert len(server.peers) == 2  # Small error bodies keep the connection.


def test_redirects_stay_on_https(server, pool: ConnectionPool) -> None:
    base = f"https://localhost:{server.server_port}"
    assert get(pool, f"{base}/to-small") == b"hello"
    with pytest.raises(urllib.error.HTTPError) as refused:
        get(pool, f"{base}/to-http")
    refused.value.close()
    with pytest.raises(urllib.error.URLError):
        get(pool, f"http://localhost:{server.server_port}/small")


def test_a_connection_closed_while_idle_is_replaced(
    server, pool: ConnectionPool
) -> None:
    base = f"https://localhost:{server.server_port}"
    get(pool, f"{base}/small")
    for connection in pool._connections().values():
        connection.sock.shutdown(socket.SHUT_RDWR)  # As if the server had closed it.
    assert get(pool, f"{base}/small") == b"hello"


def test_names_are_looked_up_one_family_at_a_time_and_cached() -> None:
    calls = []
    now = [0.0]

    def lookup(host, port, family, kind):
        calls.append(family)
        if family == socket.AF_INET6:
            raise socket.gaierror("no AAAA record")
        return [(family, kind, 6, "", ("192.0.2.1", port))]

    names = NameCache(lookup, clock=lambda: now[0])
    assert names.resolve("example.com", 443)[0][4] == ("192.0.2.1", 443)
    names.resolve("example.com", 443)
    assert calls == [socket.AF_INET, socket.AF_INET6]  # Never AF_UNSPEC; cached after.
    now[0] = 1000.0
    names.resolve("example.com", 443)
    assert len(calls) == 4
