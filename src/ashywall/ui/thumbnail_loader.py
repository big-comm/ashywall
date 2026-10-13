"""Asynchronous thumbnail textures with a small in-memory LRU.

Only the cells the grid keeps alive request textures; requests for cells
recycled before their turn are cancelled before they start.
"""

from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path

from gi.repository import Gdk

from ..models.wallpaper import Wallpaper
from ..providers.base import RemoteWallpaper
from ..services.remote_images import RemoteThumbnailCache
from ..services.thumbnails import ThumbnailCache
from .background_tasks import BackgroundRunner

GalleryEntry = Wallpaper | RemoteWallpaper
TextureKey = tuple[str, ...]


def texture_key(entry: GalleryEntry) -> TextureKey:
    if isinstance(entry, RemoteWallpaper):
        if entry.local_path is not None:
            return ("file", str(entry.local_path))
        return ("remote", entry.thumbnail_url)
    return ("local", str(entry.path), str(entry.file_size), str(entry.mtime_ns))


class ThumbnailLoader:
    def __init__(
        self,
        cache: ThumbnailCache,
        remote_cache: RemoteThumbnailCache,
        max_textures: int = 150,
        workers: int = 3,
    ) -> None:
        self._cache = cache
        self._remote_cache = remote_cache
        self._max_textures = max_textures
        self._textures: OrderedDict[TextureKey, Gdk.Texture] = OrderedDict()
        self._runner = BackgroundRunner(workers, "ashywall-thumbs")

    def cached(self, entry: GalleryEntry) -> Gdk.Texture | None:
        key = texture_key(entry)
        texture = self._textures.get(key)
        if texture is not None:
            self._textures.move_to_end(key)
        return texture

    def _thumbnail_path(self, entry: GalleryEntry) -> Path:
        if isinstance(entry, RemoteWallpaper):
            if entry.local_path is not None:
                stat = entry.local_path.stat()
                return self._cache.ensure(
                    entry.local_path, stat.st_size, stat.st_mtime_ns
                )
            return self._remote_cache.ensure(entry.thumbnail_url)
        return self._cache.ensure(entry.path, entry.file_size, entry.mtime_ns)

    def request(
        self,
        entry: GalleryEntry,
        on_ready: Callable[[Gdk.Texture], None],
        on_failed: Callable[[Exception], None],
    ) -> Future[None]:
        key = texture_key(entry)

        def load() -> Gdk.Texture:
            return Gdk.Texture.new_from_filename(str(self._thumbnail_path(entry)))

        def ready(texture: Gdk.Texture) -> None:
            self._remember(key, texture)
            on_ready(texture)

        return self._runner.submit(load, ready, on_failed)

    def _remember(self, key: TextureKey, texture: Gdk.Texture) -> None:
        self._textures[key] = texture
        self._textures.move_to_end(key)
        while len(self._textures) > self._max_textures:
            self._textures.popitem(last=False)

    def forget_all(self) -> None:
        self._textures.clear()

    def shutdown(self) -> None:
        self._runner.shutdown()
