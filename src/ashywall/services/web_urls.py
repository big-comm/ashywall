"""Strict URL parsing shared by providers and both HTTPS transports."""

import urllib.parse


def web_url_parts(value: object, *, max_length: int = 2048) -> urllib.parse.SplitResult:
    """Parse a credential-free HTTP(S) URL, raising ValueError on invalid input."""
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError("URL missing or too long")
    # urlsplit silently strips some controls; reject them before parsing.
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("URL contains whitespace or control characters")
    parts = urllib.parse.urlsplit(value)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("URL must use HTTP(S), have a host and contain no credentials")
    # Accessing .port validates its syntax and range, unlike urlsplit alone.
    if parts.port is not None and parts.port <= 0:
        raise ValueError("Invalid port")
    return parts


def https_origin(url: str) -> tuple[str, int]:
    # Batched Commons API queries are longer than provider-supplied image URLs.
    parts = web_url_parts(url, max_length=16 * 1024)
    if parts.scheme != "https":
        raise ValueError("Only HTTPS is allowed")
    return parts.hostname, parts.port or 443


def same_origin_redirect(url: str, location: str) -> str:
    """Resolve a Location without allowing HTTPS, host or port changes."""
    if any(ord(char) <= 32 or ord(char) == 127 for char in location):
        raise ValueError("Redirect contains whitespace or control characters")
    target = urllib.parse.urljoin(url, location)
    if https_origin(target) != https_origin(url):
        raise ValueError("Redirect changes host or port")
    return target
