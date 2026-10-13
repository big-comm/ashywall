"""Gallery: a virtualized grid with search, compact filters and real states.

The same page shows the local library and online results; the window tells
it which sort options apply and feeds it entries.
"""

from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import Adw, Gio, GObject, Gtk

from ..i18n import _
from ..models.resolution import TIERS, Orientation, ResolutionTier
from ..providers.base import RemoteSort
from ..services.library_repository import LibraryQuery, Section, SortOrder
from .thumbnail_loader import GalleryEntry, ThumbnailLoader
from .wallpaper_card import WallpaperCard, WallpaperItem, entry_identity

ORIENTATION_CHOICES: tuple[Orientation | None, ...] = (
    None,
    Orientation.LANDSCAPE,
    Orientation.PORTRAIT,
    Orientation.SQUARE,
)
LOCAL_SORTS: tuple[SortOrder, ...] = (
    SortOrder.TITLE,
    SortOrder.NEWEST,
    SortOrder.LARGEST,
)
LOCAL_SEARCH_DELAY_MS = 150
REMOTE_SEARCH_DELAY_MS = 700


@dataclass(frozen=True, slots=True)
class EmptyState:
    icon: str
    title: str
    description: str
    action_label: str | None = None
    action_name: str | None = None


@dataclass(frozen=True, slots=True)
class GalleryFilters:
    text: str
    orientation: Orientation | None
    min_tier: ResolutionTier | None

    @property
    def active(self) -> bool:
        return (
            bool(self.text.strip())
            or self.orientation is not None
            or self.min_tier is not None
        )


def remote_sort_label(sort: RemoteSort) -> str:
    return {
        RemoteSort.TOP: _("Top this month"),
        RemoteSort.LATEST: _("Latest"),
        RemoteSort.VIEWS: _("Most viewed"),
        RemoteSort.FAVORITES: _("Most favorited"),
        RemoteSort.RANDOM: _("Random"),
        RemoteSort.RELEVANCE: _("Relevance"),
    }[sort]


def _local_sort_labels() -> list[str]:
    return [_("Name"), _("Recently modified"), _("Largest first")]


class GalleryPage(Adw.NavigationPage):
    __gtype_name__ = "AshyWallGalleryPage"
    __gsignals__ = {
        "query-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "entry-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "load-more": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        loader: ThumbnailLoader,
        on_unreadable: Callable[[GalleryEntry, Exception], None],
    ) -> None:
        super().__init__(title=_("Wallpapers"), tag="gallery")
        self._section = Section.ALL
        self._collection_id: int | None = None
        self._remote = False
        self._quiet = False
        self._searched_text = ""
        # Online sorting follows the search text (relevance while searching)
        # until the user picks a sort order.
        self._sort_chosen = False
        self._remote_sorts: tuple[RemoteSort, ...] = ()
        self._searchable = True
        self._store = Gio.ListStore(item_type=WallpaperItem)
        self._window_title = Adw.WindowTitle(title=_("Wallpapers"))

        header = Adw.HeaderBar(title_widget=self._window_title)
        self._search_button = Gtk.ToggleButton(
            icon_name="system-search-symbolic", tooltip_text=_("Search")
        )
        self._filter_button = Gtk.MenuButton(
            icon_name="view-sort-ascending-symbolic",
            tooltip_text=_("Filters and sorting"),
            popover=self._build_filters_popover(),
        )
        self._add_folder_button = Gtk.Button(
            icon_name="folder-new-symbolic",
            tooltip_text=_("Add Folder"),
            action_name="win.add-folder",
            visible=False,
        )
        collection_menu = Gio.Menu()
        collection_menu.append(_("Rename Collection…"), "win.rename-collection")
        collection_menu.append(_("Delete Collection…"), "win.delete-collection")
        self._collection_button = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            tooltip_text=_("Collection Options"),
            menu_model=collection_menu,
            visible=False,
        )
        header.pack_start(self._add_folder_button)
        header.pack_start(self._collection_button)
        header.pack_end(self._filter_button)
        header.pack_end(self._search_button)

        self._search_entry = Gtk.SearchEntry(
            placeholder_text=_("Search by name or path"),
            hexpand=True,
            search_delay=LOCAL_SEARCH_DELAY_MS,
        )
        self._search_entry.connect(
            "search-changed", lambda _e: self._on_search_changed()
        )
        self._search_bar = Gtk.SearchBar(
            child=Adw.Clamp(child=self._search_entry, maximum_size=480),
            show_close_button=False,
        )
        self._search_bar.connect_entry(self._search_entry)
        self._search_button.bind_property(
            "active",
            self._search_bar,
            "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL,
        )

        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            vexpand=True,
            hexpand=True,
        )
        self._stack.add_named(self._build_grid(loader, on_unreadable), "grid")
        self._stack.add_named(self._build_loading(), "loading")
        self._stack.add_named(self._build_status(), "status")

        toolbar = Adw.ToolbarView(content=self._stack)
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self._search_bar)
        self.set_child(toolbar)

    # Construction ----------------------------------------------------------

    def _build_grid(
        self,
        loader: ThumbnailLoader,
        on_unreadable: Callable[[GalleryEntry, Exception], None],
    ) -> Gtk.Widget:
        factory = Gtk.SignalListItemFactory()
        factory.connect(
            "setup",
            lambda _f, item: item.set_child(WallpaperCard(loader, on_unreadable)),
        )
        factory.connect(
            "bind", lambda _f, item: item.get_child().bind(item.get_item().entry)
        )
        factory.connect("unbind", lambda _f, item: item.get_child().unbind())
        self._grid = Gtk.GridView(
            model=Gtk.NoSelection(model=self._store),
            factory=factory,
            min_columns=1,
            # GridView keeps widgets for about max_columns x 30 rows around the
            # visible area, so this also bounds how many thumbnails stay loaded.
            max_columns=6,
            single_click_activate=True,
        )
        self._grid.add_css_class("wallpaper-grid")
        self._grid.connect("activate", self._on_activate)
        self._grid.update_property([Gtk.AccessibleProperty.LABEL], [_("Wallpapers")])
        self._scrolled = Gtk.ScrolledWindow(
            child=self._grid, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True
        )
        self._scrolled.connect(
            "edge-reached",
            lambda _s, position: (
                position == Gtk.PositionType.BOTTOM and self.emit("load-more")
            ),
        )
        self._more_spinner = Adw.Spinner(
            width_request=28,
            height_request=28,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.END,
            margin_bottom=16,
            visible=False,
        )
        overlay = Gtk.Overlay(child=self._scrolled)
        overlay.add_overlay(self._more_spinner)
        return overlay

    def _build_loading(self) -> Gtk.Widget:
        self._loading_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._loading_label.add_css_class("dim-label")
        loading = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.CENTER
        )
        loading.append(Adw.Spinner(width_request=48, height_request=48))
        loading.append(self._loading_label)
        return loading

    def _build_status(self) -> Gtk.Widget:
        self._status_page = Adw.StatusPage(vexpand=True)
        self._status_button = Gtk.Button(halign=Gtk.Align.CENTER, visible=False)
        self._status_button.add_css_class("pill")
        self._status_button.add_css_class("suggested-action")
        self._status_page.set_child(self._status_button)
        return self._status_page

    def _build_filters_popover(self) -> Gtk.Popover:
        self._orientation_drop = Gtk.DropDown.new_from_strings(
            [_("Any orientation"), _("Landscape"), _("Portrait"), _("Square")]
        )
        self._resolution_drop = Gtk.DropDown.new_from_strings(
            [
                _("Any resolution"),
                _("Full HD or larger"),
                _("QHD or larger"),
                _("4K or larger"),
                _("8K or larger"),
            ]
        )
        self._sort_drop = Gtk.DropDown.new_from_strings(_local_sort_labels())
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        for title, drop in (
            (_("Orientation"), self._orientation_drop),
            (_("Resolution"), self._resolution_drop),
            (_("Sort by"), self._sort_drop),
        ):
            label = Gtk.Label(label=title, xalign=0)
            if drop is self._sort_drop:
                self._sort_label = label
            label.add_css_class("heading")
            drop.connect("notify::selected", self._on_filter_changed)
            if drop is self._sort_drop:
                drop.connect("notify::selected", self._on_sort_chosen)
            drop.update_property([Gtk.AccessibleProperty.LABEL], [title])
            box.append(label)
            box.append(drop)
        criteria = Gtk.Label(
            label=_(
                "Resolution compares the long side and the short side in either orientation; "
                "4K means at least 3840 × 2160 px. Pixel size does not show whether an image was upscaled."
            ),
            wrap=True,
            max_width_chars=34,
            xalign=0,
        )
        criteria.add_css_class("caption")
        criteria.add_css_class("dim-label")
        box.append(criteria)
        box.append(
            Gtk.Button(label=_("Clear Filters"), action_name="win.clear-filters")
        )
        return Gtk.Popover(child=box)

    # Query -----------------------------------------------------------------

    def _on_search_changed(self) -> None:
        # GtkSearchEntry also emits search-changed without any edit (for example
        # after its delay changes); re-querying then would discard fresh results
        # and, for online sources, spend a request.
        text = self._search_entry.get_text()
        if text != self._searched_text:
            searching = bool(text.strip())
            if searching != bool(self._searched_text.strip()):
                self._follow_search_sort(searching)
            self._searched_text = text
            self._emit_query_changed()

    def _follow_search_sort(self, searching: bool) -> None:
        """Wallhaven's "top this month" list barely matches any search terms."""
        sorts = self._remote_sorts
        if not self._remote or self._sort_chosen or RemoteSort.RELEVANCE not in sorts:
            return
        sort = RemoteSort.RELEVANCE if searching else sorts[0]
        quiet, self._quiet = self._quiet, True
        self._sort_drop.set_selected(sorts.index(sort))
        self._quiet = quiet

    def _emit_query_changed(self) -> None:
        if not self._quiet:
            self.emit("query-changed")

    def _on_filter_changed(self, *_args: object) -> None:
        filters = self.filters()
        if filters.orientation is not None or filters.min_tier is not None:
            self._filter_button.add_css_class("accent")
        else:
            self._filter_button.remove_css_class("accent")
        self._emit_query_changed()

    def _on_sort_chosen(self, *_args: object) -> None:
        if not self._quiet:
            self._sort_chosen = True

    def filters(self) -> GalleryFilters:
        resolution = self._resolution_drop.get_selected()
        return GalleryFilters(
            text=self._search_entry.get_text(),
            orientation=ORIENTATION_CHOICES[self._orientation_drop.get_selected()],
            min_tier=TIERS[resolution - 1] if resolution > 0 else None,
        )

    def current_query(self) -> LibraryQuery:
        filters = self.filters()
        sort_index = min(self._sort_drop.get_selected(), len(LOCAL_SORTS) - 1)
        return LibraryQuery(
            section=self._section,
            text=filters.text,
            orientation=filters.orientation,
            min_tier=filters.min_tier,
            sort=LOCAL_SORTS[sort_index],
            collection_id=self._collection_id,
        )

    def remote_sort(self) -> RemoteSort:
        """The chosen order; providers without choices get their fixed one."""
        if not self._remote_sorts:
            return RemoteSort.LATEST
        index = min(self._sort_drop.get_selected(), len(self._remote_sorts) - 1)
        return self._remote_sorts[index]

    def clear_filters(self) -> None:
        self._quiet = True
        self._search_entry.set_text("")
        self._searched_text = ""
        self._follow_search_sort(False)
        self._orientation_drop.set_selected(0)
        self._resolution_drop.set_selected(0)
        self._quiet = False
        self._on_filter_changed()

    def focus_search(self) -> None:
        self._search_button.set_active(True)
        self._search_entry.grab_focus()

    # Content ---------------------------------------------------------------

    def set_section(
        self,
        section: Section | None,
        title: str,
        remote: bool = False,
        collection_id: int | None = None,
        *,
        provider_name: str | None = None,
        sorts: tuple[RemoteSort, ...] = (),
        searchable: bool = True,
    ) -> None:
        self._section = section or Section.ALL
        self._collection_id = collection_id
        self.set_title(title)
        self._window_title.set_title(title)
        self._add_folder_button.set_visible(section is Section.FOLDERS)
        self._collection_button.set_visible(section is Section.COLLECTION)
        self._filter_button.set_sensitive(True)
        sorts = sorts if remote else ()
        if remote != self._remote or sorts != self._remote_sorts:
            self._remote = remote
            self._remote_sorts = sorts
            self._quiet = True
            self._sort_chosen = False
            labels = (
                [remote_sort_label(sort) for sort in sorts]
                if remote
                else _local_sort_labels()
            )
            self._sort_drop.set_model(Gtk.StringList.new(labels))
            self._sort_drop.set_selected(0)
            self._sort_drop.set_visible(bool(labels))
            self._sort_label.set_visible(bool(labels))
            self._search_entry.set_search_delay(
                REMOTE_SEARCH_DELAY_MS if remote else LOCAL_SEARCH_DELAY_MS
            )
            self._quiet = False
        searchable = searchable or not remote
        if searchable != self._searchable:
            self._searchable = searchable
            if not searchable:
                # Feeds without search ignore text; drop any left from another section.
                self._quiet = True
                self._search_button.set_active(False)
                self._search_entry.set_text("")
                self._searched_text = ""
                self._quiet = False
        self._search_button.set_visible(searchable)
        self._search_button.set_sensitive(True)
        self._search_entry.set_placeholder_text(
            _("Search {provider}").format(provider=provider_name)
            if remote and provider_name
            else _("Search by name or path")
        )
        quiet, self._quiet = self._quiet, True
        self._follow_search_sort(bool(self._searched_text.strip()))
        self._quiet = quiet

    def show_entries(
        self, entries: list[GalleryEntry], empty_state: EmptyState, subtitle: str
    ) -> None:
        if entries:
            self._stack.set_visible_child_name("grid")
        self._store.splice(
            0, self._store.get_n_items(), [WallpaperItem(e) for e in entries]
        )
        self._window_title.set_subtitle(subtitle)
        if not entries:
            self.show_status(empty_state)

    def append_entries(self, entries: list[GalleryEntry]) -> None:
        known = {
            entry_identity(self._store.get_item(i).entry)
            for i in range(self._store.get_n_items())
        }
        fresh = [WallpaperItem(e) for e in entries if entry_identity(e) not in known]
        if fresh:
            self._stack.set_visible_child_name("grid")
        self._store.splice(self._store.get_n_items(), 0, fresh)

    def show_subtitle(self, subtitle: str) -> None:
        self._window_title.set_subtitle(subtitle)

    def entry_count(self) -> int:
        return self._store.get_n_items()

    def set_loading_more(self, loading: bool) -> None:
        self._more_spinner.set_visible(loading)

    def fills_view(self) -> bool:
        """False when the grid is shorter than the window, so scrolling cannot load more."""
        adjustment = self._scrolled.get_vadjustment()
        return adjustment.get_upper() > adjustment.get_page_size() + 1

    def show_loading(self, text: str) -> None:
        self._loading_label.set_label(text)
        self._window_title.set_subtitle("")
        self._stack.set_visible_child_name("loading")

    def show_status(self, state: EmptyState) -> None:
        self._status_page.set_icon_name(state.icon)
        self._status_page.set_title(state.title)
        self._status_page.set_description(state.description)
        self._status_button.set_visible(state.action_name is not None)
        if state.action_name is not None:
            self._status_button.set_label(state.action_label or "")
            self._status_button.set_action_name(state.action_name)
        self._stack.set_visible_child_name("status")

    def show_unavailable(self, title: str, state: EmptyState) -> None:
        """For sections that are planned but not implemented yet."""
        self.set_section(None, title)
        self._window_title.set_subtitle("")
        self._search_button.set_active(False)
        self._search_button.set_sensitive(False)
        self._filter_button.set_sensitive(False)
        self._store.remove_all()
        self.show_status(state)

    def replace_entry(self, entry: GalleryEntry) -> None:
        identity = entry_identity(entry)
        for position in range(self._store.get_n_items()):
            if entry_identity(self._store.get_item(position).entry) == identity:
                self._store.splice(position, 1, [WallpaperItem(entry)])
                return

    def _on_activate(self, _grid: Gtk.GridView, position: int) -> None:
        item = self._store.get_item(position)
        if item is not None:
            self.emit("entry-activated", item.entry)
