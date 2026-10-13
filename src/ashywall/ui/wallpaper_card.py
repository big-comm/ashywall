"""Grid cell: thumbnail, title, pixel size and a few state badges."""

from collections.abc import Callable
from concurrent.futures import Future

from gi.repository import GObject, Gtk, Pango

from ..i18n import _
from ..providers.base import RemoteWallpaper
from .thumbnail_loader import GalleryEntry, ThumbnailLoader

THUMBNAIL_WIDTH = 232
THUMBNAIL_HEIGHT = 145


class WallpaperItem(GObject.Object):
    __gtype_name__ = "AshyWallWallpaperItem"

    def __init__(self, entry: GalleryEntry) -> None:
        super().__init__()
        self.entry = entry


def dimensions_text(entry: GalleryEntry) -> str:
    return f"{entry.width} × {entry.height}"


def entry_identity(entry: GalleryEntry) -> tuple[str, str]:
    if isinstance(entry, RemoteWallpaper):
        return entry.favorite_key
    return "local", str(entry.id)


def availability_text(entry: GalleryEntry) -> str | None:
    """How an online wallpaper relates to this computer; None for local files."""
    if not isinstance(entry, RemoteWallpaper):
        return None
    if entry.local_path is not None:
        return _("Downloaded")
    return _("Online, not downloaded")


class WallpaperCard(Gtk.Box):
    def __init__(
        self,
        loader: ThumbnailLoader,
        on_unreadable: Callable[[GalleryEntry, Exception], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("wallpaper-card")
        self._loader = loader
        self._on_unreadable = on_unreadable
        self._entry: GalleryEntry | None = None
        self._pending: Future[None] | None = None

        self._picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self._picture.set_size_request(THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
        self._broken = Gtk.Image(
            icon_name="image-missing-symbolic", pixel_size=32, visible=False
        )
        self._broken.add_css_class("dim-label")
        self._star = Gtk.Image(
            icon_name="starred-symbolic",
            visible=False,
            halign=Gtk.Align.END,
            valign=Gtk.Align.START,
        )
        self._star.add_css_class("card-badge")
        self._availability = Gtk.Image(
            visible=False, halign=Gtk.Align.START, valign=Gtk.Align.START
        )
        self._availability.add_css_class("card-badge")

        frame = Gtk.Overlay(child=self._picture, overflow=Gtk.Overflow.HIDDEN)
        frame.add_css_class("thumbnail-frame")
        for overlay in (self._broken, self._star, self._availability):
            frame.add_overlay(overlay)

        self._title = Gtk.Label(
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
            width_chars=1,
            max_width_chars=1,
            hexpand=True,
        )
        self._meta = Gtk.Label(
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
            width_chars=1,
            max_width_chars=1,
            hexpand=True,
        )
        self._meta.add_css_class("caption")
        self._meta.add_css_class("dim-label")
        self._meta.add_css_class("numeric")

        self.append(frame)
        self.append(self._title)
        self.append(self._meta)

    def bind(self, entry: GalleryEntry) -> None:
        self._entry = entry
        availability = availability_text(entry)
        self._title.set_label(entry.title)
        self._meta.set_label(dimensions_text(entry))
        self._star.set_visible(entry.is_favorite)
        self._broken.set_visible(False)
        self._availability.set_visible(availability is not None)
        if isinstance(entry, RemoteWallpaper):
            downloaded = entry.local_path is not None
            self._availability.set_from_icon_name(
                "object-select-symbolic" if downloaded else "weather-overcast-symbolic"
            )
            self._availability.set_tooltip_text(availability)
        label_parts = [entry.title, dimensions_text(entry)]
        if availability:
            label_parts.append(availability)
        if entry.is_favorite:
            label_parts.append(_("Favorite"))
        self.update_property([Gtk.AccessibleProperty.LABEL], [", ".join(label_parts)])

        texture = self._loader.cached(entry)
        self._picture.set_paintable(texture)
        if texture is None:
            self._pending = self._loader.request(
                entry,
                lambda loaded, expected=entry: self._show_texture(expected, loaded),
                lambda error, expected=entry: self._show_broken(expected, error),
            )

    def unbind(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        self._entry = None
        self._picture.set_paintable(None)

    def _show_texture(self, expected: GalleryEntry, texture) -> None:
        if self._entry is expected:
            self._pending = None
            self._picture.set_paintable(texture)

    def _show_broken(self, expected: GalleryEntry, error: Exception) -> None:
        self._on_unreadable(expected, error)
        if self._entry is expected:
            self._pending = None
            self._broken.set_visible(True)
            self._meta.set_label(_("Could not load image"))
