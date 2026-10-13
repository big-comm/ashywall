"""Building blocks shared by the local and online detail pages."""

from collections.abc import Callable
from concurrent.futures import Future

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ..i18n import _
from .background_tasks import BackgroundRunner

PREVIEW_HEIGHT = 420
# picture-options values whose result is "fill the screen and crop".
CROPPING_OPTIONS = frozenset({None, "zoom", "spanned"})


def property_row(title: str, value: str | None = None) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, subtitle_selectable=True)
    row.add_css_class("property")
    if value is not None:
        row.set_subtitle(GLib.markup_escape_text(value))
    return row


def link_row(title: str, url: str) -> Adw.ActionRow:
    """A property row whose value is a web address, with a button to open it."""
    row = property_row(title, url)
    button = Gtk.Button(
        icon_name="adw-external-link-symbolic",
        valign=Gtk.Align.CENTER,
        tooltip_text=_("Open in Browser"),
    )
    button.add_css_class("flat")
    button.connect(
        "clicked",
        lambda b: Gtk.UriLauncher(uri=url).launch(b.get_root(), None, _ignore_launch),
    )
    row.add_suffix(button)
    return row


def _ignore_launch(launcher: Gtk.UriLauncher, result: Gio.AsyncResult) -> None:
    try:
        launcher.launch_finish(result)
    except GLib.Error:
        pass  # No browser, or the user cancelled.


def apply_menu() -> Gio.Menu:
    menu = Gio.Menu()
    menu.append(_("Light and Dark Styles"), "detail.apply::both")
    menu.append(_("Light Style Only"), "detail.apply::light")
    menu.append(_("Dark Style Only"), "detail.apply::dark")
    return menu


def apply_button(label: str, unavailable_reason: str | None) -> Adw.SplitButton:
    button = Adw.SplitButton(
        label=label,
        action_name="detail.apply",
        action_target=GLib.Variant("s", "both"),
        menu_model=apply_menu(),
        dropdown_tooltip=_("Choose light or dark style"),
        tooltip_text=unavailable_reason or _("Use for the light and dark styles"),
    )
    button.add_css_class("suggested-action")
    return button


class FavoriteButton(Gtk.ToggleButton):
    __gtype_name__ = "AshyWallFavoriteButton"

    def __init__(self, active: bool, on_changed: Callable[[bool], None]) -> None:
        super().__init__(active=active)
        self._sync()
        self.connect(
            "toggled", lambda _b: (self._sync(), on_changed(self.get_active()))
        )

    def _sync(self) -> None:
        active = self.get_active()
        self.set_icon_name("starred-symbolic" if active else "non-starred-symbolic")
        self.set_tooltip_text(
            _("Remove from Favorites") if active else _("Add to Favorites")
        )


def monitor_pixels(widget: Gtk.Widget) -> tuple[int, int] | None:
    native = widget.get_native()
    display = Gdk.Display.get_default()
    if native is None or display is None or native.get_surface() is None:
        return None
    monitor = display.get_monitor_at_surface(native.get_surface())
    if monitor is None:
        return None
    geometry = monitor.get_geometry()
    scale = monitor.get_scale()
    return round(geometry.width * scale), round(geometry.height * scale)


class PreviewPanel(Gtk.Box):
    """Preview area plus the optional screen-crop simulation switch."""

    __gtype_name__ = "AshyWallPreviewPanel"

    def __init__(
        self,
        title: str,
        width: int,
        height: int,
        picture_options: str | None,
        runner: BackgroundRunner,
        caption: str | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._ratio = width / height if height else 16 / 9
        self._crops = picture_options in CROPPING_OPTIONS
        self._runner = runner
        self._pending: Future[None] | None = None

        self._picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self._picture.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Preview of {title}").format(title=title)],
        )
        self._spinner = Adw.Spinner(
            width_request=32,
            height_request=32,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self._error = Gtk.Label(
            label=_("Could not load the preview"), visible=False, wrap=True
        )
        self._error.add_css_class("dim-label")
        overlay = Gtk.Overlay(child=self._picture, overflow=Gtk.Overflow.HIDDEN)
        overlay.add_css_class("preview-frame")
        overlay.add_overlay(self._spinner)
        overlay.add_overlay(self._error)
        self._aspect = Gtk.AspectFrame(
            child=overlay,
            obey_child=False,
            ratio=self._ratio,
            height_request=PREVIEW_HEIGHT,
        )
        self.append(self._aspect)

        self._caption = Gtk.Label(wrap=True, xalign=0.5, visible=caption is not None)
        self._caption.add_css_class("caption")
        self._caption.add_css_class("dim-label")
        if caption:
            self._caption.set_label(caption)
        self.append(self._caption)

        group = Adw.PreferencesGroup(margin_top=12)
        self._simulate_row = Adw.SwitchRow(
            title=_("Simulate Screen Crop"), subtitle=self._idle_subtitle()
        )
        self._simulate_row.connect("notify::active", self._on_simulate_toggled)
        group.add(self._simulate_row)
        self.append(group)

    @staticmethod
    def _idle_subtitle() -> str:
        return _("Approximation only: panels, docks and extensions are not shown")

    def load(self, work: Callable[[], Gdk.Texture]) -> None:
        """Render a texture in the background and show it."""
        self.cancel()
        self._spinner.set_visible(True)
        self._error.set_visible(False)
        self._pending = self._runner.submit(work, self._on_ready, self._on_failed)

    def set_caption(self, text: str | None) -> None:
        self._caption.set_visible(bool(text))
        self._caption.set_label(text or "")

    def cancel(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    def _on_ready(self, texture: Gdk.Texture) -> None:
        self._pending = None
        self._spinner.set_visible(False)
        self._picture.set_paintable(texture)

    def _on_failed(self, _error: Exception) -> None:
        self._pending = None
        self._spinner.set_visible(False)
        self._error.set_visible(True)

    def _on_simulate_toggled(self, row: Adw.SwitchRow, _pspec: object) -> None:
        monitor = monitor_pixels(self) if row.get_active() else None
        if monitor is None:
            self._aspect.set_ratio(self._ratio)
            self._picture.set_content_fit(Gtk.ContentFit.COVER)
            row.set_subtitle(self._idle_subtitle())
            return
        width, height = monitor
        self._aspect.set_ratio(width / height)
        self._picture.set_content_fit(
            Gtk.ContentFit.COVER if self._crops else Gtk.ContentFit.CONTAIN
        )
        row.set_subtitle(
            _(
                "Simulated for a {width} × {height} display. Panels, docks and extensions are not shown."
            ).format(width=width, height=height)
        )


def detail_layout(*children: Gtk.Widget) -> Gtk.Widget:
    content = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=24,
        margin_top=18,
        margin_bottom=24,
        margin_start=12,
        margin_end=12,
    )
    for child in children:
        content.append(child)
    return Gtk.ScrolledWindow(
        child=Adw.Clamp(child=content, maximum_size=1000),
        hscrollbar_policy=Gtk.PolicyType.NEVER,
    )
