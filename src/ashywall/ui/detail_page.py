"""Detail page for a wallpaper on this computer."""

from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from ..i18n import _, ngettext
from ..models.wallpaper import SourceKind, Wallpaper
from ..providers.base import RemoteWallpaper
from ..providers.registry import provider_name
from ..services.library_repository import DownloadRecord
from ..services.package_owner import owning_package
from ..services.thumbnails import render_preview
from .background_tasks import BackgroundRunner
from .detail_widgets import (
    FavoriteButton,
    PreviewPanel,
    apply_button,
    detail_layout,
    link_row,
    property_row,
)
from .image_viewer import open_viewer
from .wallpaper_card import dimensions_text


def source_description(wallpaper: Wallpaper, provenance: DownloadRecord | None) -> str:
    if wallpaper.source_kind is SourceKind.SYSTEM:
        return _("System")
    if wallpaper.source_kind is SourceKind.DOWNLOADS:
        if provenance is not None:
            return _("Downloaded from {provider}").format(
                provider=provider_name(provenance.origin)
            )
        return _("Downloads")
    return _("My folder “{name}”").format(name=wallpaper.source_label)


def render_texture(path: Path) -> Gdk.Texture:
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(render_preview(path)))


class DetailPage(Adw.NavigationPage):
    __gtype_name__ = "AshyWallDetailPage"
    __gsignals__ = {
        "apply-requested": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        "favorite-toggled": (GObject.SignalFlags.RUN_FIRST, None, (object, bool)),
    }

    def __init__(
        self,
        wallpaper: Wallpaper,
        duplicates: list[Path],
        provenance: DownloadRecord | None,
        runner: BackgroundRunner,
        apply_unavailable_reason: str | None,
        collection_picker: Gtk.Widget,
    ) -> None:
        super().__init__(title=wallpaper.title, tag="detail")
        self.wallpaper = wallpaper
        self._runner = runner
        self._pending: list[Future[None]] = []
        self._install_actions(apply_unavailable_reason is None)

        header = Adw.HeaderBar()
        favorite = FavoriteButton(
            wallpaper.is_favorite,
            lambda active: (
                active != self.wallpaper.is_favorite
                and self.emit("favorite-toggled", self.wallpaper, active)
            ),
        )
        header.pack_end(apply_button(_("Apply"), apply_unavailable_reason))
        header.pack_end(favorite)
        header.pack_end(collection_picker)

        self._preview = PreviewPanel(
            wallpaper.title,
            wallpaper.width,
            wallpaper.height,
            wallpaper.picture_options,
            runner,
        )
        toolbar = Adw.ToolbarView(
            content=detail_layout(
                self._preview, self._build_details_group(duplicates, provenance)
            )
        )
        toolbar.add_top_bar(header)
        self.set_child(toolbar)
        self.connect("hidden", lambda _page: self._cancel_pending())
        path = wallpaper.path
        self._preview.load(lambda: render_texture(path))
        self._preview.set_fullscreen_handler(
            lambda: open_viewer(
                self,
                self.wallpaper.title,
                self.wallpaper.path,
                self.wallpaper.width,
                self.wallpaper.height,
                self._preview.crops,
                self._runner,
            )
        )

    def _install_actions(self, apply_enabled: bool) -> None:
        group = Gio.SimpleActionGroup()
        action = Gio.SimpleAction(
            name="apply", parameter_type=GLib.VariantType("s"), enabled=apply_enabled
        )
        action.connect(
            "activate",
            lambda _a, mode: self.emit(
                "apply-requested", self.wallpaper, mode.get_string()
            ),
        )
        group.add_action(action)
        self.insert_action_group("detail", group)

    def set_wallpaper(self, wallpaper: Wallpaper) -> None:
        self.wallpaper = wallpaper

    def _cancel_pending(self) -> None:
        self._preview.cancel()
        for future in self._pending:
            future.cancel()
        self._pending.clear()

    # Details -----------------------------------------------------------------

    def _build_details_group(
        self, duplicates: list[Path], provenance: DownloadRecord | None
    ) -> Gtk.Widget:
        wallpaper = self.wallpaper
        group = Adw.PreferencesGroup(title=_("Details"))
        group.add(
            property_row(
                _("Dimensions"), _("{size} px").format(size=dimensions_text(wallpaper))
            )
        )
        group.add(property_row(_("Aspect Ratio"), wallpaper.aspect_ratio))
        group.add(property_row(_("File Size"), GLib.format_size(wallpaper.file_size)))
        group.add(property_row(_("Format"), wallpaper.image_format or _("Unknown")))

        self._source_row = property_row(
            _("Source"), source_description(wallpaper, provenance)
        )
        group.add(self._source_row)
        if wallpaper.source_kind is SourceKind.SYSTEM:
            self._lookup_package()

        location = property_row(_("Location"), str(wallpaper.path))
        open_button = Gtk.Button(
            icon_name="folder-open-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Show in Folder"),
        )
        open_button.add_css_class("flat")
        open_button.connect("clicked", self._on_show_in_folder)
        location.add_suffix(open_button)
        group.add(location)

        if wallpaper.dark_path is not None:
            group.add(property_row(_("Dark Style Variant"), str(wallpaper.dark_path)))

        if provenance is not None:
            for row in attribution_rows(Attribution.of(provenance)):
                group.add(row)
        else:
            group.add(property_row(_("Author"), _("Not provided by this source")))
            group.add(property_row(_("License"), _("Not provided by this source")))

        group.add(self._duplicates_row(duplicates))
        return group

    def _duplicates_row(self, duplicates: list[Path]) -> Gtk.Widget:
        if not duplicates:
            return property_row(_("Identical Copies"), _("None in the library"))
        expander = Adw.ExpanderRow(
            title=_("Identical Copies"),
            subtitle=ngettext(
                "{count} other file with the same content",
                "{count} other files with the same content",
                len(duplicates),
            ).format(count=len(duplicates)),
        )
        for path in duplicates:
            expander.add_row(
                Adw.ActionRow(
                    title=GLib.markup_escape_text(path.name),
                    subtitle=GLib.markup_escape_text(str(path.parent)),
                )
            )
        return expander

    def _lookup_package(self) -> None:
        path = self.wallpaper.path

        def show(package: str | None) -> None:
            if package:
                self._source_row.set_subtitle(
                    GLib.markup_escape_text(
                        _("System — package {name}").format(name=package)
                    )
                )

        self._pending.append(
            self._runner.submit(lambda: owning_package(path), show, lambda _e: None)
        )

    def _on_show_in_folder(self, _button: Gtk.Button) -> None:
        launcher = Gtk.FileLauncher(
            file=Gio.File.new_for_path(str(self.wallpaper.path))
        )
        launcher.open_containing_folder(self.get_root(), None, _on_launcher_done)


def _on_launcher_done(launcher: Gtk.FileLauncher, result: Gio.AsyncResult) -> None:
    try:
        launcher.open_containing_folder_finish(result)
    except GLib.Error:
        pass  # The user dismissed the chooser or no file manager is available.


@dataclass(frozen=True, slots=True)
class Attribution:
    origin: str
    author: str | None = None
    uploader: str | None = None
    source_url: str | None = None
    page_url: str | None = None
    license: str | None = None
    license_url: str | None = None

    @classmethod
    def of(cls, item: RemoteWallpaper | DownloadRecord) -> "Attribution":
        return cls(
            origin=item.origin,
            author=item.author,
            uploader=item.uploader,
            source_url=item.source_url,
            page_url=item.page_url,
            license=item.license,
            license_url=item.license_url,
        )


def attribution_rows(attribution: Attribution) -> list[Gtk.Widget]:
    """Author or uploader, original source, provider page and license.

    Only what the source states is shown. Wallhaven names who uploaded an
    image, not necessarily who made it, and publishes no license; Bing credits
    the photographer without a license; Wikimedia Commons gives both.
    """
    provider = provider_name(attribution.origin)
    rows: list[Gtk.Widget] = []
    if attribution.author:
        rows.append(property_row(_("Author"), attribution.author))
    if attribution.uploader or attribution.origin == "wallhaven":
        rows.append(
            property_row(
                _("Uploaded By"),
                attribution.uploader or _("Not provided by this source"),
            )
        )
    if not rows:
        rows.append(property_row(_("Author"), _("Not provided by this source")))
    if attribution.source_url:
        rows.append(link_row(_("Original Source"), attribution.source_url))
    elif attribution.origin == "wallhaven":
        rows.append(
            property_row(_("Original Source"), _("Not provided by this source"))
        )
    if attribution.page_url:
        rows.append(
            link_row(
                _("Page on {provider}").format(provider=provider), attribution.page_url
            )
        )
    if attribution.license and attribution.license_url:
        rows.append(
            link_row(_("License"), attribution.license_url, attribution.license)
        )
    elif attribution.license:
        rows.append(property_row(_("License"), attribution.license))
    else:
        rows.append(
            property_row(
                _("License"),
                _(
                    "Not provided by {provider}. Images remain the property of their owners."
                ).format(provider=provider),
            )
        )
    return rows
