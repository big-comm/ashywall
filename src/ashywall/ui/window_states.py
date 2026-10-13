"""Sidebar entries and the texts of empty, error and unavailable states."""

from dataclasses import dataclass

from ..i18n import _, ngettext
from ..models.wallpaper import Collection
from ..providers.base import RemotePage, RemoteProvider
from ..services.http import NetworkUnavailable, RateLimited
from ..services.library_repository import FolderError, FolderProblem, Section
from .errors import error_text
from .gallery_page import EmptyState


@dataclass(frozen=True, slots=True)
class SidebarEntry:
    key: str
    title: str
    icon: str
    group: str
    section: Section | None
    collection_id: int | None = None


BIG_KEY = "big"
NEW_COLLECTION_KEY = "new-collection"


def sidebar_entries(
    collections: list[Collection], providers: list[RemoteProvider]
) -> list[SidebarEntry]:
    library, explore, group = _("Library"), _("Explore"), _("Collections")
    return [
        SidebarEntry(
            "all", _("All Wallpapers"), "view-grid-symbolic", library, Section.ALL
        ),
        SidebarEntry(
            "system", _("System"), "computer-symbolic", library, Section.SYSTEM
        ),
        SidebarEntry(
            "folders",
            _("My Folders"),
            "folder-pictures-symbolic",
            library,
            Section.FOLDERS,
        ),
        SidebarEntry(
            "downloads",
            _("Downloads"),
            "folder-download-symbolic",
            library,
            Section.DOWNLOADS,
        ),
        SidebarEntry(
            BIG_KEY, _("Big Collections"), "image-x-generic-symbolic", explore, None
        ),
        *(
            SidebarEntry(p.id, p.display_name, p.icon_name, explore, None)
            for p in providers
        ),
        SidebarEntry(
            "favorites", _("Favorites"), "starred-symbolic", group, Section.FAVORITES
        ),
        *(
            SidebarEntry(
                f"collection:{collection.id}",
                collection.name,
                "view-list-bullet-symbolic",
                group,
                Section.COLLECTION,
                collection.id,
            )
            for collection in collections
        ),
        SidebarEntry(
            NEW_COLLECTION_KEY, _("New Collection…"), "list-add-symbolic", group, None
        ),
    ]


def folder_error_text(error: FolderError) -> str:
    if error.problem is FolderProblem.ALREADY_COVERED:
        return _("This folder is already in the library through “{path}”").format(
            path=error.other
        )
    if error.problem is FolderProblem.CONTAINS_EXISTING:
        return _(
            "This folder contains “{path}”, which is already in the library. Remove it first."
        ).format(path=error.other)
    return _("“{path}” is not a folder").format(path=error.folder)


def filtered_empty_state() -> EmptyState:
    return EmptyState(
        "system-search-symbolic",
        _("No Matching Wallpapers"),
        _("Try other search terms or filters."),
        _("Clear Filters"),
        "win.clear-filters",
    )


def library_empty_state(section: Section, has_folders: bool) -> EmptyState:
    add = (_("Add Folder"), "win.add-folder")
    if section is Section.SYSTEM:
        return EmptyState(
            "computer-symbolic",
            _("No System Wallpapers Found"),
            _(
                "AshyWall looked in the GNOME background catalogs and in the shared backgrounds and wallpapers folders."
            ),
        )
    if section is Section.FOLDERS:
        return EmptyState(
            "folder-pictures-symbolic",
            _("No Images in Your Folders") if has_folders else _("No Folders Added"),
            _(
                "Add folders with images. AshyWall only reads them; nothing is moved or deleted."
            ),
            *add,
        )
    if section is Section.DOWNLOADS:
        return EmptyState(
            "folder-download-symbolic",
            _("No Downloads Yet"),
            _("Wallpapers you download from online sources appear here."),
        )
    if section is Section.COLLECTION:
        return EmptyState(
            "view-list-bullet-symbolic",
            _("This Collection Is Empty"),
            _("Use the bookmark button on a wallpaper's page to add it here."),
        )
    if section is Section.FAVORITES:
        return EmptyState(
            "starred-symbolic",
            _("No Favorites Yet"),
            _("Use the star on a wallpaper's page to keep it here."),
        )
    return EmptyState(
        "image-x-generic-symbolic",
        _("No Wallpapers Yet"),
        _("Add a folder with images to start your library."),
        *add,
    )


def big_unavailable_state() -> EmptyState:
    return EmptyState(
        "web-browser-symbolic",
        _("Not Available Yet"),
        _("Browsing the Big community collections is planned for a future version."),
    )


def remote_empty_state(
    provider: str, filtered: bool, monthly_top: bool = False
) -> EmptyState:
    if filtered:
        return EmptyState(
            "system-search-symbolic",
            _("No Results on {provider}").format(provider=provider),
            _(
                "“Top this month” only includes wallpapers ranked in the last month. "
                "Sort by relevance or most favorited to search all of Wallhaven."
            )
            if monthly_top
            else _("Try other search terms or filters."),
            _("Clear Filters"),
            "win.clear-filters",
        )
    return EmptyState(
        "system-search-symbolic",
        _("No Results on {provider}").format(provider=provider),
        _("Try again later."),
    )


def remote_error_state(provider: str, error: Exception) -> EmptyState:
    if isinstance(error, NetworkUnavailable):
        title = _("{provider} Is Unreachable").format(provider=provider)
    elif isinstance(error, RateLimited):
        title = _("Too Many Requests")
    else:
        title = _("{provider} Could Not Be Loaded").format(provider=provider)
    return EmptyState(
        "network-error-symbolic",
        title,
        error_text(error),
        _("Try Again"),
        "win.retry-explore",
    )


def remote_subtitle(provider: str, page: RemotePage | None, shown: int) -> str:
    if page is not None and page.total is not None:
        return ngettext(
            "{count} result on {provider}", "{count} results on {provider}", page.total
        ).format(count=page.total, provider=provider)
    if page is not None and page.last_page:
        return _("{count} shown · page {page} of {last}").format(
            count=shown, page=page.page, last=page.last_page
        )
    return ngettext("{count} wallpaper", "{count} wallpapers", shown).format(
        count=shown
    )


def library_subtitle(count: int) -> str:
    return ngettext("{count} wallpaper", "{count} wallpapers", count).format(
        count=count
    )
