"""Settings for the automatic wallpaper change."""

from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from ..i18n import _, ngettext
from ..models.resolution import TIERS
from ..models.wallpaper import Collection
from ..services.library_repository import Section
from ..services.rotation import INTERVALS, RotationConfig, RotationState


def interval_label(seconds: int) -> str:
    if seconds < 3600:
        minutes = seconds // 60
        return ngettext("Every minute", "Every {count} minutes", minutes).format(
            count=minutes
        )
    if seconds < 86400:
        hours = seconds // 3600
        return ngettext("Every hour", "Every {count} hours", hours).format(count=hours)
    return _("Every day")


def source_choices(collections: list[Collection]) -> list[tuple[str, str]]:
    return [
        (Section.ALL.value, _("All Wallpapers")),
        (Section.SYSTEM.value, _("System")),
        (Section.FOLDERS.value, _("My Folders")),
        (Section.DOWNLOADS.value, _("Downloads")),
        (Section.FAVORITES.value, _("Favorites")),
        *((f"collection:{c.id}", c.name) for c in collections),
    ]


def tier_labels() -> list[str]:
    return [
        _("Any resolution"),
        _("Full HD or larger"),
        _("QHD or larger"),
        _("4K or larger"),
        _("8K or larger"),
    ]


# Callbacks return the new state, or a message explaining why nothing changed.
Outcome = RotationState | str


class RotationDialog(Adw.Dialog):
    """Edits a RotationConfig; the window does the work through the callbacks."""

    __gtype_name__ = "AshyWallRotationDialog"

    def __init__(
        self,
        config: RotationConfig,
        active: RotationState | None,
        collections: list[Collection],
        count_candidates: Callable[[RotationConfig], int],
        current_title: Callable[[], str | None],
        on_start: Callable[[RotationConfig], Outcome],
        on_stop: Callable[[], str],
        on_next: Callable[[], Outcome],
        unavailable_reason: str | None,
    ) -> None:
        super().__init__(title=_("Automatic Change"), content_width=520)
        self._count_candidates = count_candidates
        self._current_title = current_title
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_next = on_next
        self._active = active is not None
        self._unavailable = unavailable_reason is not None
        self._loading = True
        self._sources = source_choices(collections)
        if config.source not in {key for key, _label in self._sources}:
            config = RotationConfig(
                interval=config.interval,
                shuffle=config.shuffle,
                min_tier=config.min_tier,
                skip_portrait=config.skip_portrait,
            )

        page = Adw.PreferencesPage()
        status = Adw.PreferencesGroup(
            description=_(
                "GNOME changes the wallpaper on its own, so AshyWall does not need "
                "to stay open. New files join the list the next time AshyWall opens."
            )
        )
        self._switch = Adw.SwitchRow(
            title=_("Change Wallpaper Automatically"), active=self._active
        )
        self._switch.connect("notify::active", self._on_switch)
        status.add(self._switch)
        self._now_row = Adw.ActionRow(title=_("Now Showing"))
        self._now_row.add_css_class("property")
        self._next_button = Gtk.Button(
            label=_("Next Wallpaper"), valign=Gtk.Align.CENTER
        )
        self._next_button.connect("clicked", lambda _b: self._next())
        self._now_row.add_suffix(self._next_button)
        status.add(self._now_row)
        page.add(status)

        pictures = Adw.PreferencesGroup(title=_("Wallpapers"))
        self._source_row = Adw.ComboRow(
            title=_("Use Wallpapers From"),
            model=Gtk.StringList.new([label for _key, label in self._sources]),
            selected=[key for key, _label in self._sources].index(config.source),
        )
        tier_keys = [None, *(tier.key for tier in TIERS)]
        self._tier_keys = tier_keys
        self._tier_row = Adw.ComboRow(
            title=_("Minimum Resolution"),
            model=Gtk.StringList.new(tier_labels()),
            selected=tier_keys.index(config.min_tier),
        )
        self._portrait_row = Adw.SwitchRow(
            title=_("Skip Portrait Images"), active=config.skip_portrait
        )
        self._count_row = Adw.ActionRow(title=_("Matching Wallpapers"))
        self._count_row.add_css_class("property")
        for row in (self._source_row, self._tier_row, self._portrait_row):
            pictures.add(row)
        pictures.add(self._count_row)
        page.add(pictures)

        timing = Adw.PreferencesGroup(title=_("Timing"))
        self._interval_row = Adw.ComboRow(
            title=_("Change"),
            model=Gtk.StringList.new([interval_label(s) for s in INTERVALS]),
            selected=INTERVALS.index(config.interval),
        )
        self._shuffle_row = Adw.SwitchRow(
            title=_("Shuffle"),
            subtitle=_("Otherwise wallpapers follow the library order"),
            active=config.shuffle,
        )
        timing.add(self._interval_row)
        timing.add(self._shuffle_row)
        page.add(timing)

        for row in (self._source_row, self._tier_row, self._interval_row):
            row.connect("notify::selected", self._on_changed)
        for row in (self._portrait_row, self._shuffle_row):
            row.connect("notify::active", self._on_changed)

        if unavailable_reason is not None:
            self._switch.set_subtitle(GLib.markup_escape_text(unavailable_reason))

        toolbar = Adw.ToolbarView(content=page)
        toolbar.add_top_bar(Adw.HeaderBar())
        self._toasts = Adw.ToastOverlay(child=toolbar)
        self.set_child(self._toasts)
        self._loading = False
        self._update()

    def config(self) -> RotationConfig:
        return RotationConfig(
            source=self._sources[self._source_row.get_selected()][0],
            interval=INTERVALS[self._interval_row.get_selected()],
            shuffle=self._shuffle_row.get_active(),
            min_tier=self._tier_keys[self._tier_row.get_selected()],
            skip_portrait=self._portrait_row.get_active(),
        )

    def _update(self) -> None:
        count = self._count_candidates(self.config())
        self._count_row.set_subtitle(
            ngettext("{count} wallpaper", "{count} wallpapers", count).format(
                count=count
            )
            if count
            else _("No wallpapers match: choose another source or resolution")
        )
        self._switch.set_sensitive(
            not self._unavailable and (self._active or count > 0)
        )
        self._now_row.set_visible(self._active)
        title = self._current_title() if self._active else None
        self._now_row.set_subtitle(GLib.markup_escape_text(title or _("Unknown")))

    def _set_switch(self, active: bool) -> None:
        self._loading = True
        self._switch.set_active(active)
        self._loading = False

    def _toast(self, text: str) -> None:
        self._toasts.add_toast(
            Adw.Toast(title=GLib.markup_escape_text(text), timeout=5)
        )

    def _start(self) -> None:
        outcome = self._on_start(self.config())
        if isinstance(outcome, str):
            self._toast(outcome)
            self._active = False
        else:
            self._active = True
        self._set_switch(self._active)

    def _on_switch(self, row: Adw.SwitchRow, _pspec: object) -> None:
        if self._loading:
            return
        if row.get_active():
            self._start()
        else:
            self._toast(self._on_stop())
            self._active = False
        self._update()

    def _on_changed(self, *_args: object) -> None:
        if self._loading:
            return
        if self._active:
            self._start()  # Apply the new settings right away.
        self._update()

    def _next(self) -> None:
        outcome = self._on_next()
        if isinstance(outcome, str):
            self._toast(outcome)
        self._update()
