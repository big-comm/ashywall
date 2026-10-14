"""The grid's list model, filled lazily so its cost follows the screen, not the library.

A library query yields only ordered ids; rows are read in small pages when
the grid first asks for one of their positions, and a bounded number of
pages stays in memory. Online entries (already complete objects) follow the
local ones.
"""

from collections import OrderedDict
from collections.abc import Callable, Iterable

from gi.repository import Gio, GObject

from ..models.wallpaper import Wallpaper
from .thumbnail_loader import GalleryEntry
from .wallpaper_card import WallpaperItem, entry_identity

PAGE_SIZE = 120
# GridView keeps about six columns x 30 rows alive around the view; this
# leaves room for scrolling back without reading the same rows again.
MAX_PAGES = 24

LoadRows = Callable[[list[int]], dict[int, Wallpaper]]


class GalleryModel(GObject.Object, Gio.ListModel):
    __gtype_name__ = "AshyWallGalleryModel"

    def __init__(self) -> None:
        super().__init__()
        self._ids: list[int] = []
        self._load: LoadRows | None = None
        self._pages: OrderedDict[int, list[WallpaperItem]] = OrderedDict()
        self._overrides: dict[int, Wallpaper] = {}
        self._positions: dict[int, int] | None = None
        self._online: list[WallpaperItem] = []
        self._online_keys: set[tuple[str, str]] = set()

    # Gio.ListModel ---------------------------------------------------------

    def do_get_item_type(self) -> GObject.GType:
        return WallpaperItem.__gtype__

    def do_get_n_items(self) -> int:
        return len(self._ids) + len(self._online)

    def do_get_item(self, position: int) -> WallpaperItem | None:
        if position < len(self._ids):
            page = self._page(position // PAGE_SIZE)
            return page[position % PAGE_SIZE]
        position -= len(self._ids)
        return self._online[position] if position < len(self._online) else None

    # Content ---------------------------------------------------------------

    def reset(
        self,
        ids: list[int],
        load: LoadRows | None,
        online: Iterable[GalleryEntry] = (),
    ) -> None:
        # Two signals, each matching the model's state when it is emitted:
        # given one that removes and adds, GridView looks for items to keep
        # across the whole range, reading every new row.
        removed = self.do_get_n_items()
        self._ids = []
        self._online = []
        self._online_keys = set()
        self._pages.clear()
        self._overrides.clear()
        self._positions = None
        if removed:
            self.items_changed(0, removed, 0)
        self._ids = list(ids)
        self._load = load
        self._add_online(online)
        if added := self.do_get_n_items():
            self.items_changed(0, 0, added)

    def append(self, entries: Iterable[GalleryEntry]) -> int:
        """Add online entries not shown yet; returns how many were new."""
        before = self.do_get_n_items()
        added = self._add_online(entries)
        if added:
            self.items_changed(before, 0, added)
        return added

    def replace(self, entry: GalleryEntry) -> None:
        """Show the new state (favorite, downloaded) of an entry already listed."""
        if isinstance(entry, Wallpaper):
            position = self._local_position(entry.id)
            if position is None:
                return
            self._overrides[entry.id] = entry
            page = self._pages.get(position // PAGE_SIZE)
            if page is not None:
                page[position % PAGE_SIZE] = WallpaperItem(entry)
            self.items_changed(position, 1, 1)
            return
        identity = entry_identity(entry)
        for index, item in enumerate(self._online):
            if item.entry is not None and entry_identity(item.entry) == identity:
                self._online[index] = WallpaperItem(entry)
                self.items_changed(len(self._ids) + index, 1, 1)
                return

    def entry_at(self, position: int) -> GalleryEntry | None:
        item = self.do_get_item(position)
        return item.entry if item is not None else None

    # Internals -------------------------------------------------------------

    def _add_online(self, entries: Iterable[GalleryEntry]) -> int:
        added = 0
        for entry in entries:
            key = entry_identity(entry)
            if key not in self._online_keys:
                self._online_keys.add(key)
                self._online.append(WallpaperItem(entry))
                added += 1
        return added

    def _page(self, number: int) -> list[WallpaperItem]:
        page = self._pages.get(number)
        if page is not None:
            self._pages.move_to_end(number)
            return page
        ids = self._ids[number * PAGE_SIZE : (number + 1) * PAGE_SIZE]
        rows = self._load(ids) if self._load is not None else {}
        rows.update((i, w) for i, w in self._overrides.items() if i in rows)
        # A row deleted since the query (the indexer runs meanwhile) shows as an
        # empty cell until the gallery refreshes after indexing.
        page = [WallpaperItem(rows.get(i)) for i in ids]
        self._pages[number] = page
        while len(self._pages) > MAX_PAGES:
            self._pages.popitem(last=False)
        return page

    def _local_position(self, wallpaper_id: int) -> int | None:
        if self._positions is None:
            self._positions = {i: p for p, i in enumerate(self._ids)}
        return self._positions.get(wallpaper_id)
