"""Persistent HTTPS connections with a small name cache.

Opening a connection per request costs a name lookup, a TCP handshake and a
TLS handshake; for a page of thumbnails from one host that is most of the
time spent. Each worker thread keeps one connection per host and reuses it.

Lookups are cached for a few minutes and ask for IPv4 and IPv6 addresses one
after the other: some resolvers drop one of two simultaneous answers, which
stalls the lookup for the resolver's timeout (often 5 s) every time.
"""

import http.client
import io
import ipaddress
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from .web_urls import https_origin, same_origin_redirect

MAX_REDIRECTS = 5
NAME_CACHE_SECONDS = 300.0
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
# Error and redirect bodies up to this size are read so the connection stays usable.
DRAIN_LIMIT = 64 * 1024

AddressInfo = tuple[Any, ...]


class NameCache:
    def __init__(
        self,
        lookup: Callable[..., list[AddressInfo]] = socket.getaddrinfo,
        clock: Callable[[], float] = time.monotonic,
        public_only: bool = False,
    ) -> None:
        self._lookup = lookup
        self._clock = clock
        self._public_only = public_only
        self._entries: dict[tuple[str, int], tuple[float, list[AddressInfo]]] = {}
        self._lock = threading.Lock()

    def resolve(self, host: str, port: int) -> list[AddressInfo]:
        key = (host, port)
        with self._lock:
            entry = self._entries.get(key)
        if entry is not None and self._clock() - entry[0] < NAME_CACHE_SECONDS:
            return entry[1]
        addresses: list[AddressInfo] = []
        failure: OSError | None = None
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                addresses += self._lookup(host, port, family, socket.SOCK_STREAM)
            except OSError as error:
                failure = error
        if not addresses:
            raise failure or socket.gaierror(f"No address for {host}")
        if self._public_only and any(
            not ipaddress.ip_address(address[4][0]).is_global for address in addresses
        ):
            raise OSError("Refusing a non-public feed address")
        with self._lock:
            self._entries[key] = (self._clock(), addresses)
        return addresses

    def forget(self, host: str, port: int) -> None:
        with self._lock:
            self._entries.pop((host, port), None)


class _Response:
    """The response for HttpClient; leaves the connection reusable only when fully read."""

    def __init__(
        self,
        response: http.client.HTTPResponse,
        discard: Callable[[], None],
        url: str,
    ) -> None:
        self._response = response
        self._discard = discard
        self.status = response.status
        self.headers = response.headers
        self._url = url

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        try:
            return self._response.read(size) if size >= 0 else self._response.read()
        except http.client.HTTPException as error:
            raise ConnectionError(str(error)) from error

    def close(self) -> None:
        if not self._response.isclosed():
            # Unread body (size limit, prefix read, cancellation): drop the connection.
            self._discard()
        self._response.close()

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class ConnectionPool:
    """One connection per host and thread; HTTPS with same-origin redirects."""

    def __init__(
        self,
        names: NameCache | None = None,
        connection_class: type[
            http.client.HTTPConnection
        ] = http.client.HTTPSConnection,
    ) -> None:
        self._names = names or NameCache()
        self._connection_class = connection_class
        self._context = ssl.create_default_context()
        self._local = threading.local()

    def _connections(self) -> dict[tuple[str, int], http.client.HTTPConnection]:
        connections = getattr(self._local, "connections", None)
        if connections is None:
            connections = self._local.connections = {}
        return connections

    def _create_socket(
        self, address: tuple[str, int], timeout: object = None, *_args: object
    ) -> socket.socket:
        host, port = address
        failure: OSError | None = None
        for family, kind, protocol, _name, sockaddr in self._names.resolve(host, port):
            sock = socket.socket(family, kind, protocol)
            try:
                if isinstance(timeout, (int, float)):
                    sock.settimeout(timeout)
                sock.connect(sockaddr)
                return sock
            except OSError as error:
                failure = error
                sock.close()
        self._names.forget(host, port)  # The addresses may have changed.
        raise failure or OSError(f"Could not connect to {host}")

    def _connection(
        self, host: str, port: int, timeout: float
    ) -> tuple[http.client.HTTPConnection, bool]:
        connections = self._connections()
        connection = connections.get((host, port))
        if connection is None:
            kwargs: dict[str, Any] = {"timeout": timeout}
            if issubclass(self._connection_class, http.client.HTTPSConnection):
                kwargs["context"] = self._context
            connection = self._connection_class(host, port, **kwargs)
            connection._create_connection = self._create_socket  # type: ignore[attr-defined]
            connections[(host, port)] = connection
        connection.timeout = timeout
        reused = connection.sock is not None
        if reused:
            connection.sock.settimeout(timeout)
        return connection, reused

    def close(self) -> None:
        """Close the calling thread's connections."""
        connections = self._connections()
        for connection in connections.values():
            connection.close()
        connections.clear()

    def _discard(self, host: str, port: int) -> None:
        connection = self._connections().pop((host, port), None)
        if connection is not None:
            connection.close()

    def _send(
        self, host: str, port: int, target: str, headers: dict[str, str], timeout: float
    ) -> http.client.HTTPResponse:
        for attempt in range(2):
            connection, reused = self._connection(host, port, timeout)
            try:
                connection.request("GET", target, headers=headers)
                return connection.getresponse()
            except (ConnectionError, http.client.HTTPException) as error:
                self._discard(host, port)
                # A reused connection may have been closed by the server while idle.
                if not reused or attempt:
                    raise urllib.error.URLError(error) from error
            except BaseException:
                self._discard(host, port)
                raise
        raise AssertionError("unreachable")

    def _finish(self, response: http.client.HTTPResponse, host: str, port: int) -> None:
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) <= DRAIN_LIMIT:
            try:
                response.read()
            except (OSError, http.client.HTTPException):
                pass
        if not response.isclosed():
            self._discard(host, port)
        response.close()

    def open(self, request: urllib.request.Request, timeout: float) -> _Response:
        url = request.full_url
        headers = dict(request.header_items())
        for _redirect in range(MAX_REDIRECTS + 1):
            try:
                host, port = https_origin(url)
            except ValueError as error:
                raise urllib.error.URLError(str(error)) from error
            parts = urllib.parse.urlsplit(url)
            target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            response = self._send(host, port, target, headers, timeout)
            status = response.status
            if status in REDIRECT_CODES:
                location = response.headers.get("Location")
                self._finish(response, host, port)
                if not location:
                    raise urllib.error.HTTPError(
                        url,
                        status,
                        "Redirect without location",
                        response.headers,
                        io.BytesIO(),
                    )
                try:
                    url = same_origin_redirect(url, location)
                except ValueError as error:
                    raise urllib.error.HTTPError(
                        url,
                        status,
                        f"Refusing unsafe redirect: {error}",
                        response.headers,
                        io.BytesIO(),
                    ) from error
                continue
            if status >= 400:
                self._finish(response, host, port)
                raise urllib.error.HTTPError(
                    url, status, response.reason, response.headers, io.BytesIO()
                )
            return _Response(response, lambda: self._discard(host, port), url)
        raise urllib.error.HTTPError(url, 310, "Too many redirects", None, io.BytesIO())
