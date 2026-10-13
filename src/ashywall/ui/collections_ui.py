"""Collection picker for detail pages and the name dialog for collections."""

from collections.abc import Callable

from gi.repository import Adw, Gtk

from ..i18n import _
from ..models.wallpaper import Collection
from ..services.library_repository import COLLECTION_NAME_LIMIT, CollectionError


def collection_error_text(name: str) -> str:
    if not name.strip():
        return _("Enter a name for the collection")
    if len(name.strip()) > COLLECTION_NAME_LIMIT:
        return _("The name is too long")
    return _("A collection named “{name}” already exists").format(
        name=" ".join(name.split())
    )


class CollectionPicker(Gtk.MenuButton):
    """Header button listing collections with a check for the current wallpaper."""

    __gtype_name__ = "AshyWallCollectionPicker"

    def __init__(
        self,
        list_collections: Callable[[], list[Collection]],
        member_of: Callable[[], set[int]],
        set_member: Callable[[int, bool], None],
        create: Callable[[str], Collection],
    ) -> None:
        super().__init__(
            icon_name="bookmark-new-symbolic", tooltip_text=_("Add to Collection")
        )
        self._list_collections = list_collections
        self._member_of = member_of
        self._set_member = set_member
        self._create = create

        self._rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._rows.add_css_class("boxed-list")
        self._empty = Gtk.Label(label=_("No collections yet"), visible=False)
        self._empty.add_css_class("dim-label")
        self._entry = Gtk.Entry(
            placeholder_text=_("New collection"),
            max_length=COLLECTION_NAME_LIMIT,
            hexpand=True,
        )
        self._entry.connect("activate", lambda _e: self._on_create())
        create_button = Gtk.Button(
            icon_name="list-add-symbolic", tooltip_text=_("Create and Add")
        )
        create_button.connect("clicked", lambda _b: self._on_create())
        self._error = Gtk.Label(xalign=0, wrap=True, visible=False, max_width_chars=30)
        self._error.add_css_class("error")
        self._error.add_css_class("caption")

        new_row = Gtk.Box(spacing=6)
        new_row.append(self._entry)
        new_row.append(create_button)
        heading = Gtk.Label(label=_("Collections"), xalign=0)
        heading.add_css_class("heading")
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
            width_request=260,
        )
        scrolled = Gtk.ScrolledWindow(
            child=self._rows,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
            max_content_height=320,
        )
        for widget in (heading, self._empty, scrolled, new_row, self._error):
            box.append(widget)
        popover = Gtk.Popover(child=box)
        popover.connect("show", lambda _p: self._rebuild())
        self.set_popover(popover)

    def _rebuild(self) -> None:
        self._rows.remove_all()
        self._error.set_visible(False)
        collections = self._list_collections()
        members = self._member_of()
        self._empty.set_visible(not collections)
        self._rows.set_visible(bool(collections))
        for collection in collections:
            check = Gtk.CheckButton(
                label=collection.name, active=collection.id in members
            )
            check.connect(
                "toggled",
                lambda button, cid=collection.id: self._set_member(
                    cid, button.get_active()
                ),
            )
            row = Gtk.ListBoxRow(child=check, activatable=False)
            self._rows.append(row)

    def _on_create(self) -> None:
        name = self._entry.get_text()
        try:
            collection = self._create(name)
        except CollectionError:
            self._error.set_label(collection_error_text(name))
            self._error.set_visible(True)
            return
        self._set_member(collection.id, True)
        self._entry.set_text("")
        self._rebuild()


def ask_collection_name(
    parent: Gtk.Widget,
    heading: str,
    confirm_label: str,
    initial: str,
    submit: Callable[[str], str | None],
    problem: str | None = None,
) -> None:
    """Ask for a name; ``submit`` returns an error text (the dialog reopens with it) or None."""
    entry = Gtk.Entry(
        text=initial, max_length=COLLECTION_NAME_LIMIT, activates_default=True
    )
    error = Gtk.Label(
        label=problem or "", xalign=0, wrap=True, visible=problem is not None
    )
    error.add_css_class("error")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.append(entry)
    box.append(error)
    dialog = Adw.AlertDialog(heading=heading, extra_child=box, close_response="cancel")
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("confirm", confirm_label)
    dialog.set_response_appearance("confirm", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("confirm")
    # Responses close the dialog by default; validate before letting it close.
    dialog.set_response_enabled("confirm", bool(initial.strip()))
    entry.connect(
        "changed",
        lambda e: dialog.set_response_enabled("confirm", bool(e.get_text().strip())),
    )

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        if response != "confirm":
            return
        text = entry.get_text()
        if message := submit(text):
            # Reopen with the message so the user can fix the name.
            ask_collection_name(parent, heading, confirm_label, text, submit, message)

    dialog.connect("response", on_response)
    dialog.present(parent)
    entry.grab_focus()
