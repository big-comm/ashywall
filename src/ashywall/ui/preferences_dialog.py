"""Preferences: personal folders and storage locations."""

from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from .. import paths
from ..i18n import _
from ..models.wallpaper import Source
from ..services.thumbnails import ThumbnailCache
from .background_tasks import BackgroundRunner


class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "AshyWallPreferencesDialog"

    def __init__(
        self,
        folders: list[Source],
        thumbnails: ThumbnailCache,
        runner: BackgroundRunner,
        on_add_folder: Callable[[], None],
        on_remove_folder: Callable[[Source], None],
    ) -> None:
        super().__init__(title=_("Preferences"))
        self._thumbnails = thumbnails
        self._runner = runner
        self._on_remove_folder = on_remove_folder

        page = Adw.PreferencesPage(
            title=_("Library"), icon_name="folder-pictures-symbolic"
        )
        self._folders_group = Adw.PreferencesGroup(
            title=_("My Folders"),
            description=_(
                "Images in these folders appear in the library. Removing a folder never deletes its files."
            ),
        )
        add_button = Gtk.Button(
            icon_name="list-add-symbolic",
            tooltip_text=_("Add Folder"),
            valign=Gtk.Align.CENTER,
        )
        add_button.add_css_class("flat")
        add_button.connect("clicked", lambda _b: on_add_folder())
        self._folders_group.set_header_suffix(add_button)
        self._folder_rows: list[Adw.ActionRow] = []
        self.set_folders(folders)
        page.add(self._folders_group)
        page.add(self._build_storage_group())
        self.add(page)

    def set_folders(self, folders: list[Source]) -> None:
        for row in self._folder_rows:
            self._folders_group.remove(row)
        self._folder_rows = []
        if not folders:
            empty = Adw.ActionRow(title=_("No folders added"))
            empty.add_css_class("dim-label")
            self._folder_rows.append(empty)
            self._folders_group.add(empty)
            return
        for folder in folders:
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(folder.label),
                subtitle=GLib.markup_escape_text(str(folder.path)),
                subtitle_selectable=True,
            )
            remove = Gtk.Button(
                icon_name="list-remove-symbolic",
                valign=Gtk.Align.CENTER,
                tooltip_text=_("Remove from Library"),
            )
            remove.add_css_class("flat")
            remove.connect(
                "clicked", lambda _b, source=folder: self._confirm_remove(source)
            )
            row.add_suffix(remove)
            self._folder_rows.append(row)
            self._folders_group.add(row)

    def _confirm_remove(self, folder: Source) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Remove Folder?"),
            body=_(
                "“{name}” will no longer appear in AshyWall. The folder and its files stay on disk."
            ).format(name=folder.label),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect(
            "response",
            lambda _d, response: (
                response == "remove" and self._on_remove_folder(folder)
            ),
        )
        dialog.present(self)

    def _build_storage_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Storage"))
        downloads = Adw.ActionRow(
            title=_("Downloaded Wallpapers"),
            subtitle=GLib.markup_escape_text(str(paths.downloads_dir())),
            subtitle_selectable=True,
        )
        downloads.add_css_class("property")
        group.add(downloads)
        self._cache_row = Adw.ActionRow(
            title=_("Thumbnail Cache"), subtitle=_("Calculating…")
        )
        self._cache_row.add_css_class("property")
        clear = Gtk.Button(
            label=_("Clear"),
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Thumbnails are recreated when needed"),
        )
        clear.connect("clicked", self._on_clear_cache)
        self._cache_row.add_suffix(clear)
        group.add(self._cache_row)
        self._refresh_cache_size()
        return group

    def _refresh_cache_size(self) -> None:
        def show(size: int) -> None:
            limit = GLib.format_size(self._thumbnails.max_bytes)
            self._cache_row.set_subtitle(
                _("{used} of {limit}").format(used=GLib.format_size(size), limit=limit)
            )

        self._runner.submit(
            self._thumbnails.total_size,
            show,
            lambda _e: self._cache_row.set_subtitle(_("Unknown")),
        )

    def _on_clear_cache(self, _button: Gtk.Button) -> None:
        self._runner.submit(
            self._thumbnails.clear, lambda _r: self._refresh_cache_size()
        )
