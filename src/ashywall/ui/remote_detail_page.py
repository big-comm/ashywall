"""Detail page for a wallpaper listed by an online provider."""

import threading
import time
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from ..i18n import _
from ..paths import display_path
from ..providers.base import RemoteWallpaper
from ..providers.registry import provider_name
from ..services.http import TransferCancelled
from ..services.remote_images import OriginalCache, RemoteThumbnailCache
from .background_tasks import BackgroundRunner, run_on_main
from .detail_page import Attribution, attribution_rows, render_texture
from .detail_widgets import (
    FavoriteButton,
    PreviewPanel,
    apply_button,
    detail_layout,
    property_row,
)
from .download_manager import PROGRESS_INTERVAL, DownloadManager
from .errors import error_text
from .image_viewer import open_viewer
from .wallpaper_card import dimensions_text

# Shown until the original arrives (or instead of it): sharp enough for the panel.
REMOTE_PREVIEW_SIZE = 2048


# Larger originals (Commons has 20-35 MB photos) load only on request.
AUTO_ORIGINAL_MAX_BYTES = 15 * 1000 * 1000


def network_is_metered() -> bool:
    return Gio.NetworkMonitor.get_default().get_network_metered()


def loads_original_automatically(item: RemoteWallpaper, allowed: bool) -> bool:
    if not allowed or network_is_metered():
        return False
    return item.file_size is None or item.file_size <= AUTO_ORIGINAL_MAX_BYTES


def describe_error(error: Exception) -> str:
    text = str(error).strip()
    return text or error.__class__.__name__


class RemoteDetailPage(Adw.NavigationPage):
    __gtype_name__ = "AshyWallRemoteDetailPage"
    __gsignals__ = {
        "apply-requested": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        "favorite-toggled": (GObject.SignalFlags.RUN_FIRST, None, (object, bool)),
    }

    def __init__(
        self,
        item: RemoteWallpaper,
        runner: BackgroundRunner,
        remote_thumbnails: RemoteThumbnailCache,
        originals: OriginalCache,
        originals_runner: BackgroundRunner,
        downloads: DownloadManager,
        details_loader,
        auto_original: bool,
        apply_unavailable_reason: str | None,
        collection_picker: Gtk.Widget,
    ) -> None:
        super().__init__(title=item.title, tag="remote-detail")
        self.item = item
        self._runner = runner
        self._remote_thumbnails = remote_thumbnails
        self._downloads = downloads
        self._originals = originals
        self._originals_runner = originals_runner
        self._auto_original = auto_original
        # Full-resolution file shown in the preview and the full-screen view.
        self._full_path: Path | None = None
        self._original_cancel: threading.Event | None = None
        self._pending: list[Future[None]] = []
        self._install_actions(apply_unavailable_reason is None)

        header = Adw.HeaderBar()
        self._apply_button = apply_button(_("Apply"), apply_unavailable_reason)
        header.pack_end(self._apply_button)
        header.pack_end(
            FavoriteButton(
                item.is_favorite,
                lambda active: (
                    active != self.item.is_favorite
                    and self.emit("favorite-toggled", self.item, active)
                ),
            )
        )
        header.pack_end(collection_picker)

        self._preview = PreviewPanel(item.title, item.width, item.height, None, runner)
        toolbar = Adw.ToolbarView(
            content=detail_layout(
                self._preview, self._build_download_group(), self._build_details_group()
            )
        )
        toolbar.add_top_bar(header)
        self.set_child(toolbar)

        self.connect("hidden", self._on_hidden)
        self.connect("showing", self._on_showing)
        self._show_state()
        self._load_preview()
        self._load_details(details_loader)

    # Actions ---------------------------------------------------------------

    def _install_actions(self, apply_enabled: bool) -> None:
        group = Gio.SimpleActionGroup()
        apply_action = Gio.SimpleAction(
            name="apply", parameter_type=GLib.VariantType("s"), enabled=apply_enabled
        )
        apply_action.connect(
            "activate",
            lambda _a, mode: self.emit("apply-requested", self.item, mode.get_string()),
        )
        group.add_action(apply_action)
        self.insert_action_group("detail", group)

    def set_item(self, item: RemoteWallpaper) -> None:
        self.item = item
        self._show_state()

    # Preview ---------------------------------------------------------------

    def _load_preview(self) -> None:
        path = self.item.local_path or self._originals.cached(self.item)
        if path is not None:
            self._show_full_resolution(path)
            return
        url = self.item.preview_url
        cache = self._remote_thumbnails

        def work() -> Gdk.Texture:
            return Gdk.Texture.new_from_filename(
                str(cache.ensure(url, REMOTE_PREVIEW_SIZE))
            )

        self._preview.load(work)
        if loads_original_automatically(self.item, self._auto_original):
            self._fetch_original()
        else:
            self._preview.set_caption(
                _(
                    "Reduced preview: {provider} asks apps to load originals "
                    "({size}) only when you want them."
                ).format(
                    provider=provider_name(self.item.origin), size=self._size_text()
                )
                if not self._auto_original
                else _(
                    "Reduced preview: your connection is metered, so the original "
                    "({size}) is not loaded automatically."
                ).format(size=self._size_text())
                if network_is_metered()
                else _(
                    "Reduced preview: the original is large ({size}), so it is "
                    "loaded only when you ask."
                ).format(size=self._size_text()),
                _("Load Full Resolution"),
                self._fetch_original,
            )

    def _size_text(self) -> str:
        return (
            GLib.format_size(self.item.file_size)
            if self.item.file_size
            else _("unknown size")
        )

    def _show_full_resolution(self, path: Path) -> None:
        self._full_path = path
        self._preview.set_caption(None)
        self._preview.set_progress(None)
        self._preview.load(lambda: render_texture(path))
        self._preview.set_fullscreen_handler(self._open_fullscreen)

    def _open_fullscreen(self) -> None:
        if self._full_path is None:
            return
        open_viewer(
            self,
            self.item.title,
            self._full_path,
            self.item.width,
            self.item.height,
            self._preview.crops,
            self._runner,
        )

    def _fetch_original(self) -> None:
        """Fetch the original into the preview cache, never into the library."""
        if self._original_cancel is not None or self._full_path is not None:
            return
        cancel = threading.Event()
        self._original_cancel = cancel
        item = self.item
        originals = self._originals
        last_report = 0.0

        def report(received: int, total: int | None) -> None:
            nonlocal last_report
            now = time.monotonic()
            if now - last_report >= PROGRESS_INTERVAL:
                last_report = now
                run_on_main(self._on_original_progress, cancel, received, total)

        self._preview.set_caption(
            _("Loading the full-resolution image ({size})…").format(
                size=self._size_text()
            )
        )
        self._preview.set_progress(0.0)
        self._originals_runner.submit(
            lambda: originals.ensure(item, on_progress=report, cancel=cancel),
            lambda path: self._on_original_ready(cancel, path),
            lambda error: self._on_original_failed(cancel, error),
        )

    def _on_original_progress(
        self, cancel: threading.Event, received: int, total: int | None
    ) -> None:
        if cancel is not self._original_cancel:
            return
        total = total or self.item.file_size
        self._preview.set_progress(min(1.0, received / total) if total else None)
        if total:
            self._preview.set_caption(
                _("Loading the full-resolution image: {received} of {total}…").format(
                    received=GLib.format_size(received), total=GLib.format_size(total)
                )
            )

    def _on_original_ready(self, cancel: threading.Event, path: Path) -> None:
        if cancel is not self._original_cancel:
            return
        self._original_cancel = None
        if self._full_path is None:
            self._show_full_resolution(path)

    def _on_original_failed(self, cancel: threading.Event, error: Exception) -> None:
        if cancel is not self._original_cancel:
            return
        self._original_cancel = None
        self._preview.set_progress(None)
        if isinstance(error, TransferCancelled):
            return
        self._preview.set_caption(
            _(
                "Showing a reduced preview: the full-resolution image could not be "
                "loaded. {reason}"
            ).format(reason=error_text(error)),
            _("Try Again"),
            self._fetch_original,
        )

    def _stop_original(self) -> None:
        if self._original_cancel is not None:
            self._original_cancel.set()
            self._original_cancel = None
            self._preview.set_progress(None)

    # Download ----------------------------------------------------------------

    def _build_download_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup()
        self._status_row = Adw.ActionRow(title=_("Not Downloaded"))
        self._download_button = Gtk.Button(label=_("Download"), valign=Gtk.Align.CENTER)
        self._download_button.connect(
            "clicked", lambda _b: self._downloads.start(self.item)
        )
        self._cancel_button = Gtk.Button(
            icon_name="process-stop-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Cancel Download"),
        )
        self._cancel_button.add_css_class("flat")
        self._cancel_button.connect(
            "clicked", lambda _b: self._downloads.cancel(self.item.favorite_key)
        )
        self._open_button = Gtk.Button(
            icon_name="folder-open-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Show in Folder"),
        )
        self._open_button.add_css_class("flat")
        self._open_button.connect("clicked", self._on_show_in_folder)
        self._progress = Gtk.ProgressBar(
            valign=Gtk.Align.CENTER, hexpand=True, width_request=120
        )
        for widget in (
            self._progress,
            self._cancel_button,
            self._open_button,
            self._download_button,
        ):
            self._status_row.add_suffix(widget)
        group.add(self._status_row)
        return group

    def _show_state(self, error: str | None = None) -> None:
        key = self.item.favorite_key
        active = self._downloads.is_active(key)
        downloaded = self.item.local_path is not None
        self._download_button.set_visible(not active and not downloaded)
        self._cancel_button.set_visible(active)
        self._progress.set_visible(active)
        self._open_button.set_visible(downloaded)
        if downloaded:
            self._status_row.set_title(_("Downloaded"))
            self._status_row.set_subtitle(
                GLib.markup_escape_text(display_path(self.item.local_path))
            )
        elif active:
            self._status_row.set_title(_("Downloading…"))
            progress = self._downloads.progress(key)
            self.on_download_progress(*(progress or (0, None)))
        else:
            self._status_row.set_title(
                _("Download Failed") if error else _("Not Downloaded")
            )
            size = (
                GLib.format_size(self.item.file_size)
                if self.item.file_size
                else _("unknown size")
            )
            self._status_row.set_subtitle(
                GLib.markup_escape_text(error)
                if error
                else _(
                    "Original file: {size}. It is saved to your Downloads section."
                ).format(size=size)
            )

    def on_download_progress(self, received: int, total: int | None) -> None:
        if total:
            self._progress.set_fraction(min(1.0, received / total))
            self._status_row.set_subtitle(
                _("{received} of {total}").format(
                    received=GLib.format_size(received), total=GLib.format_size(total)
                )
            )
        else:
            self._progress.pulse()
            self._status_row.set_subtitle(GLib.format_size(received))

    def on_download_finished(self, path: Path) -> None:
        self.item = self.item.with_state(
            is_favorite=self.item.is_favorite, local_path=path
        )
        self._show_state()
        if self._full_path is None:
            self._stop_original()
            self._show_full_resolution(path)

    def on_download_failed(self, error: Exception) -> None:
        self._show_state(describe_error(error))

    def on_download_cancelled(self) -> None:
        self._show_state()
        self._status_row.set_subtitle(_("Download cancelled"))

    def _on_showing(self, _page: Adw.NavigationPage) -> None:
        self._downloads.add_listener(self.item.favorite_key, self)
        self._show_state()
        if self._full_path is None and loads_original_automatically(
            self.item, self._auto_original
        ):
            self._fetch_original()

    def _on_hidden(self, _page: Adw.NavigationPage) -> None:
        self._downloads.remove_listener(self.item.favorite_key, self)
        self._stop_original()
        self._preview.cancel()
        for future in self._pending:
            future.cancel()
        self._pending.clear()

    def _on_show_in_folder(self, _button: Gtk.Button) -> None:
        if self.item.local_path is None:
            return
        launcher = Gtk.FileLauncher(
            file=Gio.File.new_for_path(str(self.item.local_path))
        )
        launcher.open_containing_folder(self.get_root(), None, _ignore_launch)

    # Details -------------------------------------------------------------------

    def _build_details_group(self) -> Gtk.Widget:
        item = self.item
        self._details_group = Adw.PreferencesGroup(title=_("Details"))
        group = self._details_group
        if item.description:
            group.add(property_row(_("Description"), item.description))
        if (featured := featured_date(item.date)) is not None:
            group.add(property_row(_("Featured On"), featured))
        group.add(
            property_row(
                _("Dimensions"), _("{size} px").format(size=dimensions_text(item))
            )
        )
        group.add(property_row(_("Aspect Ratio"), item.aspect_ratio))
        group.add(
            property_row(
                _("File Size"),
                GLib.format_size(item.file_size) if item.file_size else _("Unknown"),
            )
        )
        group.add(property_row(_("Format"), _format_label(item.media_type)))
        group.add(property_row(_("Source"), provider_name(item.origin)))
        self._attribution: list[Gtk.Widget] = []
        self._show_attribution(item)
        return group

    def _show_attribution(self, item: RemoteWallpaper) -> None:
        for row in self._attribution:
            self._details_group.remove(row)
        self._attribution = attribution_rows(Attribution.of(item))
        for row in self._attribution:
            self._details_group.add(row)

    def _load_details(self, details_loader) -> None:
        """The listing has no uploader; fetch it once the page is open."""
        if self.item.uploader is not None or details_loader is None:
            return
        remote_id = self.item.remote_id

        def show(details: RemoteWallpaper) -> None:
            # Keep the completed attribution so a later download records it too.
            self.item = replace(
                self.item, uploader=details.uploader, source_url=details.source_url
            )
            self._show_attribution(self.item)

        self._pending.append(
            self._runner.submit(
                lambda: details_loader(remote_id), show, lambda _e: None
            )
        )


def featured_date(value: str | None) -> str | None:
    """An ISO date in the user's format."""
    try:
        year, month, day = (int(part) for part in (value or "").split("-"))
        moment = GLib.DateTime.new_local(year, month, day, 12, 0, 0)
    except (ValueError, TypeError):
        return None
    return moment.format("%x") if moment is not None else None


def _format_label(media_type: str | None) -> str:
    return {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WebP"}.get(
        media_type or "", _("Unknown")
    )


def _ignore_launch(launcher: Gtk.FileLauncher, result: Gio.AsyncResult) -> None:
    try:
        launcher.open_containing_folder_finish(result)
    except GLib.Error:
        pass
