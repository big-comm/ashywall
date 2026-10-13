"""Minimal blocking HTTP client for worker threads.

Every read is size-limited, every request has a timeout, and an optional
sliding-window limiter keeps API calls under a provider's published quota.
The opener is injectable so tests never touch the network.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

from .. import __version__

USER_AGENT = f"AshyWall/{__version__}"
CHUNK_SIZE = 64 * 1024
DEFAULT_TIMEOUT = 20.0


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NetworkUnavailable(HttpError):
    """The server could not be reached (offline, DNS, timeout, TLS)."""


class RateLimited(HttpError):
    def __init__(self, retry_after: float | None) -> None:
        super().__init__("Too many requests", 429)
        self.retry_after = retry_after


class ResponseTooLarge(HttpError):
    pass


class TransferCancelled(Exception):
    pass


class Response(Protocol):
    status: int
    headers: Any

    def read(self, size: int = -1) -> bytes: ...
    def __enter__(self) -> "Response": ...
    def __exit__(self, *args: object) -> object: ...


Opener = Callable[[urllib.request.Request, float], Response]


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https URLs, so a redirect cannot downgrade a transfer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(
                newurl, code, "Refusing non-https redirect", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_DEFAULT_OPENER = urllib.request.build_opener(_HttpsOnlyRedirects)


def _default_opener(request: urllib.request.Request, timeout: float) -> Response:
    return _DEFAULT_OPENER.open(request, timeout=timeout)


class SlidingWindowLimiter:
    """Allow at most ``limit`` calls in any ``window`` seconds, waiting if needed."""

    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait_time(self) -> float:
        """Reserve a slot and return how long the caller must wait before using it."""
        with self._lock:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self._window:
                self._calls.popleft()
            delay = 0.0
            if len(self._calls) >= self._limit:
                delay = self._window - (now - self._calls[0])
            self._calls.append(now + delay)
            return max(0.0, delay)


class HttpClient:
    def __init__(
        self,
        opener: Opener | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        limiter: SlidingWindowLimiter | None = None,
    ) -> None:
        self._opener = opener or _default_opener
        self._timeout = timeout
        self._limiter = limiter

    def _open(
        self, url: str, params: dict[str, str] | None, cancel: threading.Event | None
    ) -> Response:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        if self._limiter is not None:
            delay = self._limiter.wait_time()
            if delay and cancel is not None:
                if cancel.wait(delay):
                    raise TransferCancelled()
            elif delay:
                time.sleep(delay)
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
        )
        try:
            return self._opener(request, self._timeout)
        except urllib.error.HTTPError as error:
            error.close()  # Releases the connection held by the error response.
            if error.code == 429:
                retry = error.headers.get("Retry-After") if error.headers else None
                raise RateLimited(
                    float(retry) if retry and retry.isdigit() else None
                ) from error
            raise HttpError(f"HTTP {error.code}", error.code) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise NetworkUnavailable(str(getattr(error, "reason", error))) from error

    def stream(
        self,
        url: str,
        sink: Callable[[bytes], object],
        *,
        max_bytes: int,
        params: dict[str, str] | None = None,
        on_progress: Callable[[int, int | None], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """Copy the body into ``sink``; returns the byte count."""
        with self._open(url, params, cancel) as response:
            length_header = (
                response.headers.get("Content-Length") if response.headers else None
            )
            expected = (
                int(length_header)
                if length_header and length_header.isdigit()
                else None
            )
            if expected is not None and expected > max_bytes:
                raise ResponseTooLarge(
                    f"Response of {expected} bytes exceeds {max_bytes}"
                )
            received = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise TransferCancelled()
                try:
                    chunk = response.read(CHUNK_SIZE)
                except (TimeoutError, OSError) as error:
                    raise NetworkUnavailable(str(error)) from error
                if not chunk:
                    break
                received += len(chunk)
                if received > max_bytes:
                    raise ResponseTooLarge(f"Response exceeds {max_bytes} bytes")
                sink(chunk)
                if on_progress is not None:
                    on_progress(received, expected)
            if expected is not None and received != expected:
                raise NetworkUnavailable(
                    f"Transfer interrupted ({received} of {expected} bytes)"
                )
            return received

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        params: dict[str, str] | None = None,
        cancel: threading.Event | None = None,
    ) -> bytes:
        parts: list[bytes] = []
        self.stream(
            url, parts.append, max_bytes=max_bytes, params=params, cancel=cancel
        )
        return b"".join(parts)

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        max_bytes: int = 4 * 1024 * 1024,
        cancel: threading.Event | None = None,
    ) -> Any:
        body = self.get_bytes(url, max_bytes=max_bytes, params=params, cancel=cancel)
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HttpError(f"Invalid JSON: {error}") from error
