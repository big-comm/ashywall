"""Thumbnails of online wallpapers and downloads of their originals.

Remote thumbnails are fetched with a size limit, decoded by Pillow through
the same allow-list as local files and re-encoded before the interface ever
loads them. Originals are downloaded only when the user asks, written to a
hidden temporary file, checked (format, pixel limits, dimensions announced by
the provider) and then linked into place without overwriting anything.
"""

import hashlib
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from ..providers.base import (
    REMOTE_ID_PATTERN,
    ProviderError,
    RemoteWallpaper,
    validated_url,
)
from .http import HttpClient
from .image_probe import ImageProbeError, probe_image
from .thumbnails import THUMBNAIL_SIZE, render_scaled_jpeg

MAX_THUMBNAIL_BYTES = 4 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
# Slack over the size announced by the provider before a transfer is refused.
DOWNLOAD_SIZE_SLACK = 1024 * 1024

EXTENSIONS: dict[str, str] = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "AVIF": ".avif",
    "TIFF": ".tif",
    "BMP": ".bmp",
}


class DownloadError(Exception):
    pass


def _write_atomically(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=".", suffix=".part"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class RemoteThumbnailCache:
    """Keeps re-encoded remote thumbnails inside the (pruned) thumbnail cache."""

    def __init__(
        self, root: Path, http: HttpClient, allowed_hosts: frozenset[str]
    ) -> None:
        self.root = root
        self._http = http
        self._allowed_hosts = allowed_hosts

    def path_for(self, url: str, max_side: int) -> Path:
        key = hashlib.sha256(f"{url}\0{max_side}".encode()).hexdigest()
        return self.root / key[:2] / f"{key}.jpg"

    def ensure(self, url: str, max_side: int = THUMBNAIL_SIZE) -> Path:
        validated_url(url, self._allowed_hosts)
        target = self.path_for(url, max_side)
        if target.is_file():
            try:
                os.utime(target)
            except OSError:
                pass
            return target
        data = self._http.get_bytes(url, max_bytes=MAX_THUMBNAIL_BYTES)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=target.parent, prefix=".", suffix=".download"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            encoded = render_scaled_jpeg(Path(temporary), max_side, quality=85)
        finally:
            Path(temporary).unlink(missing_ok=True)
        _write_atomically(target, encoded)
        return target


def download_stem(item: RemoteWallpaper) -> str:
    for part in (item.origin, item.remote_id):
        if not REMOTE_ID_PATTERN.match(part):
            raise DownloadError(f"Unsafe identifier {part!r}")
    return f"{item.origin}-{item.remote_id}"


def existing_download(target_dir: Path, item: RemoteWallpaper) -> Path | None:
    stem = download_stem(item)
    for extension in EXTENSIONS.values():
        candidate = target_dir / f"{stem}{extension}"
        if candidate.is_file():
            return candidate
    return None


def download_original(
    item: RemoteWallpaper,
    http: HttpClient,
    target_dir: Path,
    allowed_hosts: frozenset[str],
    *,
    on_progress: Callable[[int, int | None], None] | None = None,
    cancel: threading.Event | None = None,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> Path:
    """Download ``item`` into ``target_dir`` and return the final path.

    An existing download of the same item is returned as is.
    """
    try:
        url = validated_url(item.original_url, allowed_hosts)
    except ProviderError as error:
        raise DownloadError(str(error)) from error
    if (existing := existing_download(target_dir, item)) is not None:
        return existing
    limit = max_bytes
    if item.file_size:
        limit = min(max_bytes, item.file_size + DOWNLOAD_SIZE_SLACK)

    target_dir.mkdir(parents=True, exist_ok=True)
    # Hidden name: the library scanner skips dot files, so partial downloads never show up.
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target_dir, prefix=".", suffix=".part"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            http.stream(
                url,
                handle.write,
                max_bytes=limit,
                on_progress=on_progress,
                cancel=cancel,
            )
            handle.flush()
            os.fsync(handle.fileno())
        try:
            info = probe_image(temporary)
        except ImageProbeError as error:
            raise DownloadError(
                f"The downloaded file is not a valid image: {error}"
            ) from error
        if (info.width, info.height) != (item.width, item.height):
            raise DownloadError(
                f"The downloaded image is {info.width}x{info.height}, "
                f"but {item.width}x{item.height} was announced"
            )
        final = target_dir / f"{download_stem(item)}{EXTENSIONS[info.image_format]}"
        try:
            os.link(temporary, final)  # Fails instead of overwriting.
        except FileExistsError:
            pass
        except OSError:
            # Filesystems without hard links: replace only if nothing is there.
            if not final.exists():
                os.replace(temporary, final)
        return final
    finally:
        temporary.unlink(missing_ok=True)
