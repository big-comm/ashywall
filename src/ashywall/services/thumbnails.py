"""Thumbnail cache and preview rendering.

Thumbnails are disposable JPEGs in the cache directory, keyed by path, size
and mtime, written atomically and pruned by least-recent use. Previews for
the detail page are rendered in memory and never stored.
"""

import hashlib
import io
import os
import tempfile
from pathlib import Path

from PIL import Image

from .image_probe import (
    ImageProbeError,
    apply_orientation,
    exif_orientation,
    open_image,
)

THUMBNAIL_SIZE = 480
PREVIEW_SIZE = 2048
DEFAULT_CACHE_LIMIT = 250 * 1000 * 1000
# Transparent areas are drawn over a neutral grey, close to what GNOME shows.
BACKDROP_COLOR = (128, 128, 128)


def render_scaled_jpeg(path: Path, max_side: int, quality: int) -> bytes:
    """Decode ``path`` at reduced size and return it as JPEG bytes."""
    try:
        with open_image(path) as image:
            orientation = exif_orientation(image)
            image.draft("RGB", (max_side, max_side))  # Fast DCT scaling for JPEG.
            image.thumbnail(
                (max_side, max_side), Image.Resampling.LANCZOS, reducing_gap=2.0
            )
            image = apply_orientation(image, orientation)
            rgb = _flatten_to_rgb(image)
    except ImageProbeError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
        raise ImageProbeError(f"Could not decode image: {error}") from error
    output = io.BytesIO()
    rgb.save(output, "JPEG", quality=quality, optimize=True)
    return output.getvalue()


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        backdrop = Image.new("RGB", rgba.size, BACKDROP_COLOR)
        backdrop.paste(rgba, mask=rgba.getchannel("A"))
        return backdrop
    return image.convert("RGB")


def render_preview(path: Path, max_side: int = PREVIEW_SIZE) -> bytes:
    return render_scaled_jpeg(path, max_side, quality=90)


class ThumbnailCache:
    def __init__(self, root: Path, max_bytes: int = DEFAULT_CACHE_LIMIT) -> None:
        self.root = root
        self.max_bytes = max_bytes

    def path_for(self, image_path: Path, file_size: int, mtime_ns: int) -> Path:
        key = hashlib.sha256(
            f"{image_path}\0{file_size}\0{mtime_ns}".encode()
        ).hexdigest()
        return self.root / key[:2] / f"{key}.jpg"

    def ensure(self, image_path: Path, file_size: int, mtime_ns: int) -> Path:
        """Return the thumbnail path, generating it if missing."""
        target = self.path_for(image_path, file_size, mtime_ns)
        if target.is_file():
            try:
                os.utime(target)  # Marks it as recently used for pruning.
            except OSError:
                pass
            return target
        data = render_scaled_jpeg(image_path, THUMBNAIL_SIZE, quality=85)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, suffix=".part")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return target

    def _entries(self) -> list[tuple[float, int, Path]]:
        entries = []
        if not self.root.is_dir():
            return entries
        for directory, _subdirs, files in os.walk(self.root):
            for name in files:
                path = Path(directory, name)
                try:
                    info = path.stat()
                except OSError:
                    continue
                entries.append((info.st_mtime, info.st_size, path))
        return entries

    def total_size(self) -> int:
        return sum(size for _mtime, size, _path in self._entries())

    def prune(self) -> int:
        """Delete least recently used thumbnails until the cache is under 80% of its limit."""
        entries = sorted(self._entries())
        total = sum(size for _mtime, size, _path in entries)
        removed = 0
        target = int(self.max_bytes * 0.8)
        if total <= self.max_bytes:
            return 0
        for _mtime, size, path in entries:
            if total <= target:
                break
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed

    def clear(self) -> None:
        for _mtime, _size, path in self._entries():
            path.unlink(missing_ok=True)
