"""Native category entry with reuse of existing reading categories."""

from gi.repository import Adw, Gtk

from ..i18n import _


class CategoryGroup(Adw.PreferencesGroup):
    __gtype_name__ = "AshyWallReadingCategoryGroup"

    def __init__(self, categories: list[str], current: str = "") -> None:
        super().__init__(
            description=_(
                "Choose an existing category or enter a new one. Leave it empty for Uncategorized."
            )
        )
        self.entry = Adw.EntryRow(title=_("Category (optional)"), text=current)
        self.add(self.entry)
        if categories:
            selecting = False
            choices = [_("Choose a Category"), *categories]
            existing = Adw.ComboRow(
                title=_("Existing Categories"), model=Gtk.StringList.new(choices)
            )
            existing.set_selected(
                categories.index(current) + 1 if current in categories else 0
            )

            def selected(_row, _property):
                nonlocal selecting
                index = existing.get_selected()
                if 0 < index < len(choices):
                    selecting = True
                    try:
                        self.entry.set_text(choices[index])
                    finally:
                        selecting = False

            def changed(_entry):
                if selecting:
                    return
                index = existing.get_selected()
                if 0 < index < len(choices) and self.entry.get_text() != choices[index]:
                    existing.set_selected(0)

            existing.connect("notify::selected", selected)
            self.entry.connect("changed", changed)
            self.add(existing)
