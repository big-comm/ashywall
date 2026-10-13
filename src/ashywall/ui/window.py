"""Main window: sidebar navigation, gallery/detail stack, indexing status and actions."""

from dataclasses import dataclass
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from .. import paths
from ..desktop.gnome import ApplyMode, BackgroundState, DesktopError, GnomeBackground
from ..i18n import _, ngettext
from ..models.wallpaper import Source, SourceKind, Wallpaper
from ..providers.base import RemotePage, RemoteProvider, RemoteSort, RemoteWallpaper
from ..services.http import HttpClient
from ..services.library_index import IndexProgress, IndexSummary
from ..services.library_repository import (
    CollectionError,
    FolderError,
    LibraryRepository,
    Section,
)
from ..services.remote_images import OriginalCache, RemoteThumbnailCache
from ..services.rotation import (
    RotationConfig,
    RotationError,
    RotationManager,
    RotationState,
)
from ..services.thumbnails import ThumbnailCache
from .background_tasks import BackgroundRunner
from .collections_ui import CollectionPicker, ask_collection_name, collection_error_text
from .detail_page import DetailPage
from .download_manager import DownloadManager
from .errors import error_text
from .explore_controller import ExploreController
from .gallery_page import GalleryFilters, GalleryPage
from .index_controller import IndexController
from .preferences_dialog import PreferencesDialog
from .remote_detail_page import RemoteDetailPage
from .rotation_dialog import RotationDialog
from .thumbnail_loader import GalleryEntry, ThumbnailLoader
from .window_states import (
    NEW_COLLECTION_KEY,
    SidebarEntry,
    big_unavailable_state,
    filtered_empty_state,
    folder_error_text,
    library_empty_state,
    library_subtitle,
    remote_empty_state,
    remote_error_state,
    remote_subtitle,
    sidebar_entries,
)


@dataclass
class WindowServices:
    database_path: Path
    repository: LibraryRepository
    thumbnails: ThumbnailCache
    remote_thumbnails: RemoteThumbnailCache
    originals: OriginalCache
    originals_runner: BackgroundRunner
    loader: ThumbnailLoader
    runner: BackgroundRunner
    cdn: HttpClient
    providers: dict[str, RemoteProvider]
    desktop: GnomeBackground | None
    desktop_unavailable_reason: str | None


# Pages fetched automatically, without scrolling, when filters leave pages short.
MAX_AUTO_LOADS = 4


def remote_matches(item: RemoteWallpaper, filters: GalleryFilters) -> bool:
    text = filters.text.strip().lower()
    if text and text not in item.title.lower() and text not in item.remote_id.lower():
        return False
    if filters.orientation is not None and item.orientation is not filters.orientation:
        return False
    return filters.min_tier is None or filters.min_tier.accepts(item.width, item.height)


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "AshyWallMainWindow"

    def __init__(self, application: Adw.Application, services: WindowServices) -> None:
        super().__init__(
            application=application,
            default_width=1180,
            default_height=760,
            title="AshyWall",
        )
        self._services = services
        self._repository = services.repository
        self._providers = services.providers
        self._entries = sidebar_entries(
            self._repository.list_collections(), list(self._providers.values())
        )
        self._current = self._entries[0]
        self._count_labels: dict[str, Gtk.Label] = {}
        self._preferences: PreferencesDialog | None = None
        self._progress: IndexProgress | None = None
        self._unavailable_folders: tuple[Path, ...] = ()
        self._apply_after_download: dict[tuple[str, str], str] = {}
        self._auto_loads = 0
        self._rotation = RotationManager(
            self._repository, services.desktop, paths.slideshow_dir
        )
        self._indexer = IndexController(
            services.database_path,
            self._on_index_progress,
            self._on_index_finished,
            self._on_index_failed,
        )
        self._explore = ExploreController(
            next(iter(self._providers.values())),
            self._repository,
            self._on_remote_page,
            self._on_remote_error,
        )
        self._downloads = DownloadManager(
            services.cdn,
            paths.downloads_dir,
            lambda origin: self._providers[origin].download_hosts,
            self._on_download_finished,
            self._on_download_failed,
            services.originals.cached,
        )

        self._gallery = GalleryPage(services.loader, self._on_unreadable)
        self._gallery.connect("query-changed", lambda _page: self.refresh_gallery())
        self._gallery.connect("entry-activated", self._on_entry_activated)
        self._gallery.connect("load-more", lambda _page: self._load_more())
        self._navigation = Adw.NavigationView()
        self._navigation.add(self._gallery)
        self._navigation.connect("popped", lambda *_args: self._update_counts())

        self._split = Adw.NavigationSplitView(
            sidebar=self._build_sidebar(),
            content=Adw.NavigationPage(child=self._navigation, title=_("Wallpapers")),
            min_sidebar_width=220,
            max_sidebar_width=280,
        )
        breakpoint = Adw.Breakpoint(
            condition=Adw.BreakpointCondition.parse("max-width: 640sp")
        )
        breakpoint.add_setter(self._split, "collapsed", True)
        self.add_breakpoint(breakpoint)
        self._toasts = Adw.ToastOverlay(child=self._split)
        self.set_content(self._toasts)

        self._install_actions()
        self._sync_rotation()
        self.connect("close-request", self._on_close_request)
        self._select_entry(self._entries[0])
        self._indexer.start()

    # Sidebar -----------------------------------------------------------------

    def _build_sidebar(self) -> Adw.NavigationPage:
        self._sidebar_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._sidebar_list.add_css_class("navigation-sidebar")
        self._populate_sidebar()
        self._sidebar_list.set_header_func(self._sidebar_header)
        self._sidebar_list.connect("row-activated", self._on_sidebar_activated)

        menu = Gio.Menu()
        library_section = Gio.Menu()
        library_section.append(_("Refresh Library"), "win.refresh")
        library_section.append(_("Restore Previous Wallpaper"), "win.restore-wallpaper")
        menu.append_section(None, library_section)
        rotation_section = Gio.Menu()
        rotation_section.append(_("Automatic Change…"), "win.rotation")
        rotation_section.append(_("Next Wallpaper"), "win.next-wallpaper")
        menu.append_section(None, rotation_section)
        app_section = Gio.Menu()
        app_section.append(_("Preferences"), "app.preferences")
        app_section.append(_("About AshyWall"), "app.about")
        menu.append_section(None, app_section)
        header = Adw.HeaderBar()
        header.pack_end(
            Gtk.MenuButton(
                icon_name="open-menu-symbolic",
                menu_model=menu,
                primary=True,
                tooltip_text=_("Main Menu"),
            )
        )

        self._status_spinner = Adw.Spinner(visible=False)
        self._status_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self._status_label.add_css_class("caption")
        self._status_label.add_css_class("dim-label")
        self._status_box = Gtk.Box(
            spacing=8,
            margin_start=12,
            margin_end=12,
            margin_top=8,
            margin_bottom=10,
            visible=False,
        )
        self._status_box.append(self._status_spinner)
        self._status_box.append(self._status_label)

        toolbar = Adw.ToolbarView(
            content=Gtk.ScrolledWindow(
                child=self._sidebar_list, hscrollbar_policy=Gtk.PolicyType.NEVER
            )
        )
        toolbar.add_top_bar(header)
        toolbar.add_bottom_bar(self._status_box)
        return Adw.NavigationPage(child=toolbar, title="AshyWall")

    def _populate_sidebar(self) -> None:
        self._sidebar_list.remove_all()
        self._count_labels.clear()
        for entry in self._entries:
            self._sidebar_list.append(self._sidebar_row(entry))

    def _rebuild_sidebar(self, select_key: str | None = None) -> None:
        """Recreate rows after collections change, keeping or moving the selection."""
        self._entries = sidebar_entries(
            self._repository.list_collections(), list(self._providers.values())
        )
        self._populate_sidebar()
        key = select_key or self._current.key
        target = next((e for e in self._entries if e.key == key), self._entries[0])
        if select_key is not None or target.key != self._current.key:
            self._select_entry(target)
        else:
            self._current = target
            self._highlight(target)
            if target.collection_id is not None:  # A rename changes the page title.
                self._gallery.set_section(
                    target.section, target.title, collection_id=target.collection_id
                )
            self._update_counts()

    def _highlight(self, entry: SidebarEntry) -> None:
        self._sidebar_list.select_row(
            self._sidebar_list.get_row_at_index(self._entries.index(entry))
        )

    def _sidebar_row(self, entry: SidebarEntry) -> Gtk.ListBoxRow:
        box = Gtk.Box(spacing=12)
        box.append(Gtk.Image(icon_name=entry.icon))
        box.append(Gtk.Label(label=entry.title, xalign=0, hexpand=True))
        if entry.section is not None:
            count = Gtk.Label()
            count.add_css_class("dim-label")
            count.add_css_class("numeric")
            self._count_labels[entry.key] = count
            box.append(count)
        row = Gtk.ListBoxRow(child=box)
        row.entry = entry
        return row

    def _sidebar_header(
        self, row: Gtk.ListBoxRow, before: Gtk.ListBoxRow | None
    ) -> None:
        if before is not None and before.entry.group == row.entry.group:
            row.set_header(None)
            return
        label = Gtk.Label(
            label=row.entry.group,
            xalign=0,
            margin_start=12,
            margin_top=12 if before else 6,
            margin_bottom=6,
        )
        label.add_css_class("heading")
        label.add_css_class("dim-label")
        row.set_header(label)

    def _on_sidebar_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if row.entry.key == NEW_COLLECTION_KEY:
            self._highlight(self._current)
            self._new_collection()
            return
        self._select_entry(row.entry)
        self._split.set_show_content(True)

    def _select_entry(self, entry: SidebarEntry) -> None:
        self._current = entry
        self._highlight(entry)
        self._navigation.pop_to_tag("gallery")
        self._explore.stop()
        self._gallery.set_loading_more(False)
        provider = self._providers.get(entry.key)
        if provider is not None:
            self._explore.set_provider(provider)
            self._gallery.set_section(
                None,
                entry.title,
                remote=True,
                provider_name=provider.display_name,
                sorts=provider.sorts,
                searchable=provider.searchable,
            )
        elif entry.section is None:
            self._gallery.show_unavailable(entry.title, big_unavailable_state())
            self._update_counts()
            return
        else:
            self._gallery.set_section(
                entry.section, entry.title, collection_id=entry.collection_id
            )
        self.refresh_gallery()

    # Content ---------------------------------------------------------------

    def refresh_gallery(self) -> None:
        self._update_counts()
        if (provider := self._current_provider()) is not None:
            self._gallery.show_loading(
                _("Loading {provider}…").format(provider=provider.display_name)
            )
            self._explore.search(self._gallery.filters(), self._gallery.remote_sort())
            return
        if self._current.section is None:
            return
        query = self._gallery.current_query()
        entries: list[GalleryEntry] = list(self._repository.list_wallpapers(query))
        remote: list[RemoteWallpaper] = []
        if query.section is Section.FAVORITES:
            remote = self._repository.remote_favorites()
        elif query.section is Section.COLLECTION and query.collection_id is not None:
            remote = self._repository.remote_collection_items(query.collection_id)
        filters = self._gallery.filters()
        entries += [
            item
            for item in self._explore.enrich(remote)
            if remote_matches(item, filters)
        ]
        if not entries and self._indexer.running and not query.has_filters:
            self._gallery.show_loading(self._progress_text())
            return
        has_folders = bool(self._repository.list_sources(SourceKind.FOLDER))
        empty = (
            filtered_empty_state()
            if query.has_filters
            else library_empty_state(query.section, has_folders)
        )
        self._gallery.show_entries(entries, empty, library_subtitle(len(entries)))

    def _update_counts(self) -> None:
        counts = self._repository.count_by_section()
        collection_counts = {c.id: c.count for c in self._repository.list_collections()}
        for entry in self._entries:
            label = self._count_labels.get(entry.key)
            if label is None:
                continue
            if entry.collection_id is not None:
                label.set_label(str(collection_counts.get(entry.collection_id, 0)))
            else:
                label.set_label(str(counts[entry.section]))
        self._update_status()
        self.lookup_action("restore-wallpaper").set_enabled(
            self._services.desktop is not None
            and self._repository.peek_desktop_history() is not None
        )

    def _on_entry_activated(self, _gallery: GalleryPage, entry: GalleryEntry) -> None:
        if isinstance(entry, RemoteWallpaper):
            page = RemoteDetailPage(
                entry,
                self._services.runner,
                self._services.remote_thumbnails,
                self._services.originals,
                self._services.originals_runner,
                self._downloads,
                self._details_loader(entry.origin),
                self._providers[entry.origin].auto_original
                if entry.origin in self._providers
                else False,
                self._services.desktop_unavailable_reason,
                self._collection_picker(lambda: page.item),
            )
            page.connect("favorite-toggled", self._on_remote_favorite_toggled)
            page.connect("apply-requested", self._on_remote_apply_requested)
        else:
            page = DetailPage(
                entry,
                self._repository.duplicates_of(entry),
                self._repository.download_record_for(entry.path),
                self._services.runner,
                self._services.desktop_unavailable_reason,
                self._collection_picker(lambda: page.wallpaper),
            )
            page.connect("favorite-toggled", self._on_favorite_toggled)
            page.connect("apply-requested", self._on_apply_requested)
        self._navigation.push(page)

    def _on_favorite_toggled(
        self, page: DetailPage, wallpaper: Wallpaper, favorite: bool
    ) -> None:
        self._repository.set_favorite(wallpaper, favorite)
        updated = self._repository.get_wallpaper(wallpaper.id)
        if updated is None:
            return
        page.set_wallpaper(updated)
        self._after_favorite_change(updated)

    def _on_remote_favorite_toggled(
        self, page: RemoteDetailPage, item: RemoteWallpaper, favorite: bool
    ) -> None:
        self._repository.set_remote_favorite(item, favorite)
        updated = item.with_state(is_favorite=favorite, local_path=item.local_path)
        page.set_item(updated)
        self._after_favorite_change(updated)

    def _after_favorite_change(self, entry: GalleryEntry) -> None:
        if self._current.section is Section.FAVORITES:
            self.refresh_gallery()
        else:
            self._gallery.replace_entry(entry)
            self._update_counts()

    # Collections ---------------------------------------------------------------

    def _collection_picker(self, current_entry) -> CollectionPicker:
        """``current_entry`` is called lazily: the page is created after the picker."""

        def set_member(collection_id: int, member: bool) -> None:
            self._repository.set_in_collection(collection_id, current_entry(), member)
            self._collections_changed(collection_id)

        def create(name: str):
            collection = self._repository.create_collection(name)
            self._rebuild_sidebar()
            return collection

        return CollectionPicker(
            self._repository.list_collections,
            lambda: self._repository.collections_containing(current_entry()),
            set_member,
            create,
        )

    def _collections_changed(self, collection_id: int) -> None:
        if self._current.collection_id == collection_id:
            self.refresh_gallery()
        else:
            self._update_counts()

    def _new_collection(self, *_args: object) -> None:
        def submit(name: str) -> str | None:
            try:
                collection = self._repository.create_collection(name)
            except CollectionError:
                return collection_error_text(name)
            self._rebuild_sidebar(select_key=f"collection:{collection.id}")
            self._split.set_show_content(True)
            return None

        ask_collection_name(self, _("New Collection"), _("Create"), "", submit)

    def _rename_collection(self, *_args: object) -> None:
        collection_id = self._current.collection_id
        if collection_id is None:
            return

        def submit(name: str) -> str | None:
            try:
                self._repository.rename_collection(collection_id, name)
            except CollectionError:
                return collection_error_text(name)
            self._rebuild_sidebar()
            return None

        ask_collection_name(
            self, _("Rename Collection"), _("Rename"), self._current.title, submit
        )

    def _delete_collection(self, *_args: object) -> None:
        collection_id, name = self._current.collection_id, self._current.title
        if collection_id is None:
            return
        dialog = Adw.AlertDialog(
            heading=_("Delete Collection?"),
            body=_(
                "“{name}” will be deleted. Its wallpapers stay in your library and on disk."
            ).format(name=name),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "delete":
                self._repository.delete_collection(collection_id)
                self._rebuild_sidebar(select_key="all")
                self._toast(_("Collection “{name}” deleted").format(name=name))

        dialog.connect("response", on_response)
        dialog.present(self)

    def _on_unreadable(self, entry: GalleryEntry, error: Exception) -> None:
        if isinstance(entry, Wallpaper):
            self._repository.mark_invalid(entry.id, str(error))
            self._update_status()

    # Online browsing -----------------------------------------------------------

    def _load_more(self) -> None:
        if self._current_provider() is not None and self._explore.load_more():
            self._gallery.set_loading_more(True)

    def _on_remote_page(
        self, page: RemotePage, items: list[RemoteWallpaper], append: bool
    ) -> None:
        provider = self._current_provider()
        if provider is None or provider is not self._explore.provider:
            return
        self._gallery.set_loading_more(False)
        if append:
            self._gallery.append_entries(items)
        else:
            self._auto_loads = 0
            self._gallery.show_entries(
                items,
                remote_empty_state(
                    provider.display_name,
                    self._gallery.filters().active,
                    RemoteSort.TOP in provider.sorts
                    and self._gallery.remote_sort() is RemoteSort.TOP,
                ),
                "",
            )
        self._gallery.show_subtitle(
            remote_subtitle(provider.display_name, page, self._gallery.entry_count())
        )
        # Locally filtered pages can be short; load a few more until the view can scroll.
        if page.has_more and self._auto_loads < MAX_AUTO_LOADS:
            GLib.timeout_add(300, self._fill_view)

    def _fill_view(self) -> bool:
        if self._current_provider() is not None and not self._gallery.fills_view():
            self._auto_loads += 1
            self._load_more()
        return GLib.SOURCE_REMOVE

    def _on_remote_error(self, error: Exception, append: bool) -> None:
        provider = self._current_provider()
        if provider is None:
            return
        self._gallery.set_loading_more(False)
        if append:
            self._toast(error_text(error))
        else:
            self._gallery.show_status(remote_error_state(provider.display_name, error))

    # Downloads ---------------------------------------------------------------

    def _on_download_finished(self, item: RemoteWallpaper, path: Path) -> None:
        self._repository.record_download(item, path)
        details = self._details_loader(item.origin)
        if item.uploader is None and details is not None:
            # Listings omit the uploader; complete the attribution record in the background.
            self._services.runner.submit(
                lambda: details(item.remote_id),
                lambda details: self._repository.record_download(details, path),
                lambda _error: None,
            )
        updated = item.with_state(is_favorite=item.is_favorite, local_path=path)
        self._gallery.replace_entry(updated)
        self._indexer.start()
        mode = self._apply_after_download.pop(item.favorite_key, None)
        if mode is not None:
            self._apply_file(path, None, mode, None)
        else:
            self._toast(_("Download complete: {title}").format(title=item.title))

    def _on_download_failed(self, item: RemoteWallpaper, error: Exception) -> None:
        self._apply_after_download.pop(item.favorite_key, None)
        self._toast(_("Download failed: {reason}").format(reason=error_text(error)))

    # Desktop -----------------------------------------------------------------

    def _on_apply_requested(
        self, _page: DetailPage, wallpaper: Wallpaper, mode: str
    ) -> None:
        self._apply_file(
            wallpaper.path, wallpaper.dark_path, mode, wallpaper.picture_options
        )

    def _on_remote_apply_requested(
        self, _page: RemoteDetailPage, item: RemoteWallpaper, mode: str
    ) -> None:
        if item.local_path is not None and item.local_path.is_file():
            self._apply_file(item.local_path, None, mode, None)
            return
        self._apply_after_download[item.favorite_key] = mode
        self._downloads.start(item)
        self._toast(_("Downloading the original before applying…"))

    def _apply_file(
        self, path: Path, dark_path: Path | None, mode: str, options: str | None
    ) -> None:
        desktop = self._services.desktop
        if desktop is None:
            self._toast(
                self._services.desktop_unavailable_reason
                or _("Applying wallpapers is not available")
            )
            return
        try:
            previous = desktop.apply(
                path, dark_path=dark_path, mode=ApplyMode(mode), picture_options=options
            )
        except DesktopError as error:
            self._toast(_("Could not apply the wallpaper: {error}").format(error=error))
            return
        self._repository.push_desktop_history(path.as_uri(), previous.to_json())
        self._sync_rotation()
        self._update_counts()
        titles = {
            ApplyMode.BOTH: _("Wallpaper applied"),
            ApplyMode.LIGHT: _("Wallpaper applied to the light style"),
            ApplyMode.DARK: _("Wallpaper applied to the dark style"),
        }
        self._toasts.add_toast(
            Adw.Toast(
                title=titles[ApplyMode(mode)],
                button_label=_("Undo"),
                action_name="win.restore-wallpaper",
                timeout=8,
            )
        )

    def _restore_previous(self, *_args: object) -> None:
        entry = self._repository.peek_desktop_history()
        desktop = self._services.desktop
        if entry is None or desktop is None:
            return
        entry_id, state_json = entry
        try:
            desktop.restore(BackgroundState.from_json(state_json))
        except DesktopError as error:
            self._toast(
                _("Could not restore the previous wallpaper: {error}").format(
                    error=error
                )
            )
            return
        self._repository.drop_desktop_history(entry_id)
        self._sync_rotation()
        self._update_counts()
        self._toast(_("Previous wallpaper restored"))

    # Folders -----------------------------------------------------------------

    def add_folder(self, *_args: object) -> None:
        dialog = Gtk.FileDialog(title=_("Add Folder to Library"), modal=True)
        dialog.select_folder(self, None, self._on_folder_chosen)

    def _on_folder_chosen(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult
    ) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return  # Cancelled.
        if folder is None or folder.get_path() is None:
            self._toast(_("Only local folders can be added"))
            return
        try:
            source = self._repository.add_folder(Path(folder.get_path()))
        except FolderError as error:
            self._toast(folder_error_text(error))
            return
        self._toast(_("Adding “{name}”…").format(name=source.label))
        self._folders_changed()

    def _remove_folder(self, source: Source) -> None:
        self._repository.remove_folder(source.id)
        self._toast(_("“{name}” removed from the library").format(name=source.label))
        self._folders_changed()

    def _folders_changed(self) -> None:
        if self._preferences is not None:
            self._preferences.set_folders(
                self._repository.list_sources(SourceKind.FOLDER)
            )
        self.refresh_gallery()
        self._indexer.start()

    def show_preferences(self) -> None:
        self._preferences = PreferencesDialog(
            self._repository.list_sources(SourceKind.FOLDER),
            self._services.thumbnails,
            self._services.originals,
            self._services.runner,
            self.add_folder,
            self._remove_folder,
        )
        self._preferences.connect(
            "closed", lambda _d: setattr(self, "_preferences", None)
        )
        self._preferences.present(self)

    # Indexing ----------------------------------------------------------------

    def _progress_text(self) -> str:
        if self._progress is None or self._progress.total == 0:
            return _("Looking for wallpapers…")
        return _("Checking {processed} of {total} images…").format(
            processed=self._progress.processed, total=self._progress.total
        )

    def _update_status(self) -> None:
        if self._indexer.running:
            self._status_spinner.set_visible(True)
            self._status_label.set_label(self._progress_text())
            self._status_box.set_visible(True)
            return
        self._status_spinner.set_visible(False)
        messages = []
        invalid = self._repository.count_invalid()
        if invalid:
            messages.append(
                ngettext(
                    "{count} file could not be read",
                    "{count} files could not be read",
                    invalid,
                ).format(count=invalid)
            )
        for folder in self._unavailable_folders:
            messages.append(
                _("Folder not found: {path}").format(path=paths.display_path(folder))
            )
        self._status_label.set_label("\n".join(messages))
        self._status_box.set_visible(bool(messages))

    def _on_index_progress(self, progress: IndexProgress) -> None:
        self._progress = progress
        self._update_status()
        section = self._current.section
        if (
            section is not None
            and section is not Section.FAVORITES
            and self._current_provider() is None
        ):
            if self._repository.count_wallpapers(self._gallery.current_query()) == 0:
                self._gallery.show_loading(self._progress_text())

    def _on_index_finished(self, summary: IndexSummary) -> None:
        self._progress = None
        self._sync_rotation(update_list=True)
        self._unavailable_folders = summary.unavailable_folders
        if self._current_provider() is not None:
            self._update_counts()
        else:
            self.refresh_gallery()

    def _on_index_failed(self, error: Exception) -> None:
        self._progress = None
        self._update_counts()
        self._toast(_("The library could not be updated: {error}").format(error=error))

    # Actions -----------------------------------------------------------------

    def _install_actions(self) -> None:
        actions = {
            "add-folder": self.add_folder,
            "refresh": lambda *_a: self._indexer.start(),
            "restore-wallpaper": self._restore_previous,
            "clear-filters": lambda *_a: self._gallery.clear_filters(),
            "retry-explore": lambda *_a: self.refresh_gallery(),
            "search": lambda *_a: self._focus_search(),
            "new-collection": self._new_collection,
            "rename-collection": self._rename_collection,
            "delete-collection": self._delete_collection,
            "rotation": lambda *_a: self._show_rotation(),
            "next-wallpaper": lambda *_a: self._next_wallpaper(),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction(name=name)
            action.connect("activate", callback)
            self.add_action(action)

    # Automatic change ----------------------------------------------------------

    def _sync_rotation(self, update_list: bool = False) -> None:
        """Forget a slideshow GNOME no longer shows; after indexing, update its list."""
        try:
            if update_list:
                active = self._rotation.active_state() is not None
                if self._rotation.refresh() is None and active:
                    self._toast(
                        _("Automatic change stopped: no wallpapers match its settings")
                    )
            elif self._rotation.active_state() is None:
                self._rotation.refresh()
        except (RotationError, DesktopError) as error:
            self._toast(
                _("Automatic change could not be updated: {error}").format(error=error)
            )
        self.lookup_action("next-wallpaper").set_enabled(
            self._rotation.active_state() is not None
        )

    def _show_rotation(self) -> None:
        dialog = RotationDialog(
            self._rotation.saved_config(),
            self._rotation.active_state(),
            self._repository.list_collections(),
            lambda config: len(self._rotation.candidates(config)),
            self._rotation_title,
            self._start_rotation,
            self._stop_rotation,
            self._next_wallpaper,
            self._services.desktop_unavailable_reason,
        )
        dialog.connect("closed", lambda _d: self._sync_rotation())
        dialog.present(self)

    def _rotation_title(self) -> str | None:
        state = self._rotation.active_state()
        if state is None:
            return None
        path = self._rotation.current_path(state)
        return self._repository.title_for_path(path) or path.name

    def _start_rotation(self, config: RotationConfig) -> RotationState | str:
        try:
            state, replaced = self._rotation.start(config)
        except (RotationError, DesktopError) as error:
            self._rotation.save_config(config)
            return _("Automatic change could not start: {error}").format(error=error)
        if replaced is not None:
            # "Restore Previous Wallpaper" brings back what was on screen before.
            self._repository.push_desktop_history(Path(state.file).as_uri(), replaced)
        self._sync_rotation()
        self._update_counts()
        return state

    def _stop_rotation(self) -> str:
        try:
            kept = self._rotation.stop()
        except DesktopError as error:
            return _("Could not keep the current wallpaper: {error}").format(
                error=error
            )
        self._sync_rotation()
        if kept is None:
            return _("Automatic change turned off")
        return _("Automatic change turned off; “{name}” stays as the wallpaper").format(
            name=kept.name
        )

    def _next_wallpaper(self) -> RotationState | str:
        try:
            state = self._rotation.next()
        except (RotationError, DesktopError) as error:
            message = _("Could not change the wallpaper: {error}").format(error=error)
            self._toast(message)
            return message
        self._toast(_("Now showing {name}").format(name=self._rotation_title() or ""))
        return state

    def _current_provider(self) -> RemoteProvider | None:
        return self._providers.get(self._current.key)

    def _details_loader(self, origin: str):
        provider = self._providers.get(origin)
        if provider is None or not provider.has_details:
            return None
        return provider.details

    def _focus_search(self) -> None:
        provider = self._current_provider()
        if self._current.section is not None or (
            provider is not None and provider.searchable
        ):
            self._gallery.focus_search()

    def _toast(self, text: str) -> None:
        self._toasts.add_toast(
            Adw.Toast(title=GLib.markup_escape_text(text), timeout=5)
        )

    def _on_close_request(self, _window: Adw.ApplicationWindow) -> bool:
        self._indexer.cancel()
        self._explore.shutdown()
        self._downloads.shutdown()
        return False
