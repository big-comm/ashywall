"""Detail page for a wallpaper listed by an online provider."""

from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from ..i18n import _
from ..providers.base import RemoteWallpaper
from ..services.remote_images import RemoteThumbnailCache
from .background_tasks import BackgroundRunner
from .detail_page import attribution_rows, provider_name, render_texture
from .detail_widgets import (
    FavoriteButton,
    PreviewPanel,
    apply_button,
    detail_layout,
    property_row,
)
from .download_manager import DownloadManager
from .wallpaper_card import dimensions_text

# Wallhaven's largest thumbnail is a few hundred pixels wide; keep it as is.
REMOTE_PREVIEW_SIZE = 1024


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
        downloads: DownloadManager,
        details_loader,
        apply_unavailable_reason: str | None,
        collection_picker: Gtk.Widget,
    ) -> None:
        super().__init__(title=item.title, tag="remote-detail")
        self.item = item
        self._runner = runner
        self._remote_thumbnails = remote_thumbnails
        self._downloads = downloads
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
        local = self.item.local_path
        if local is not None:
            self._preview.set_caption(None)
            self._preview.load(lambda: render_texture(local))
            return
        self._preview.set_caption(
            _(
                "Low-resolution preview from {provider}. Download to see the full image."
            ).format(provider=provider_name(self.item.origin))
        )
        url = self.item.preview_url
        cache = self._remote_thumbnails

        def work() -> Gdk.Texture:
            return Gdk.Texture.new_from_filename(
                str(cache.ensure(url, REMOTE_PREVIEW_SIZE))
            )

        self._preview.load(work)

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
                GLib.markup_escape_text(str(self.item.local_path))
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
        self._load_preview()

    def on_download_failed(self, error: Exception) -> None:
        self._show_state(describe_error(error))

    def on_download_cancelled(self) -> None:
        self._show_state()
        self._status_row.set_subtitle(_("Download cancelled"))

    def _on_showing(self, _page: Adw.NavigationPage) -> None:
        self._downloads.add_listener(self.item.favorite_key, self)
        self._show_state()

    def _on_hidden(self, _page: Adw.NavigationPage) -> None:
        self._downloads.remove_listener(self.item.favorite_key, self)
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
        self._attribution = attribution_rows(
            item.uploader, item.source_url, item.page_url, item.origin
        )
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


def _format_label(media_type: str | None) -> str:
    return {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WebP"}.get(
        media_type or "", _("Unknown")
    )


def _ignore_launch(launcher: Gtk.FileLauncher, result: Gio.AsyncResult) -> None:
    try:
        launcher.open_containing_folder_finish(result)
    except GLib.Error:
        pass
