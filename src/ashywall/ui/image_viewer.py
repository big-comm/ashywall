"""Full-screen viewer: the image as a wallpaper, whole, or at actual size.

The file is decoded at full resolution in a worker thread (reduced only past
``MAX_DISPLAY_PIXELS``) and shown in a window that covers the monitor, so
"as wallpaper" is what the desktop would show with the image zoomed to fill
the screen. "Actual size" maps one image pixel to one screen pixel, also on
scaled displays.
"""

from concurrent.futures import Future
from enum import StrEnum
from pathlib import Path

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from ..i18n import _
from ..services.thumbnails import decode_for_display
from .background_tasks import BackgroundRunner

CONTROLS_TIMEOUT_MS = 2500


class ViewMode(StrEnum):
    WALLPAPER = "wallpaper"
    WHOLE = "whole"
    ACTUAL = "actual"


def load_full_texture(path: Path) -> Gdk.Texture:
    image = decode_for_display(path)
    return Gdk.MemoryTexture.new(
        image.width,
        image.height,
        Gdk.MemoryFormat.R8G8B8,
        GLib.Bytes.new(image.tobytes()),
        image.width * 3,
    )


class ImageViewer(Adw.Window):
    __gtype_name__ = "AshyWallImageViewer"

    def __init__(
        self,
        parent: Gtk.Window | None,
        title: str,
        path: Path,
        width: int,
        height: int,
        crops: bool,
        runner: BackgroundRunner,
    ) -> None:
        super().__init__(
            title=title,
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.add_css_class("image-viewer")
        self._width = width
        self._height = height
        self._crops = crops
        self._texture: Gdk.Texture | None = None
        self._mode = ViewMode.WALLPAPER
        self._size = (0, 0)
        self._hide_source: int | None = None
        self._pending: Future[None] | None = None
        self._drag_origin = (0.0, 0.0)
        self._anchor: tuple[float, float, float, float] | None = None
        # Scroll position to set once the actual-size layout is allocated.
        self._position_pending = False

        self._picture = Gtk.Picture(can_shrink=True)
        self._picture.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Full-screen view of {title}").format(title=title)],
        )
        self._scrolled = Gtk.ScrolledWindow(child=self._picture)

        self._spinner = Adw.Spinner(
            width_request=48,
            height_request=48,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self._error = Gtk.Label(
            label=_("Could not open the image"),
            wrap=True,
            visible=False,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self._error.add_css_class("title-2")

        self._info = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        info_box = Gtk.Box(
            halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=18
        )
        info_box.add_css_class("viewer-info")
        info_box.add_css_class("osd")
        info_box.append(self._info)

        overlay = Gtk.Overlay(child=self._scrolled)
        overlay.add_overlay(self._spinner)
        overlay.add_overlay(self._error)
        self._controls = Gtk.Revealer(
            child=self._build_controls(title),
            transition_type=Gtk.RevealerTransitionType.CROSSFADE,
            valign=Gtk.Align.START,
            reveal_child=True,
        )
        self._info_revealer = Gtk.Revealer(
            child=info_box,
            transition_type=Gtk.RevealerTransitionType.CROSSFADE,
            valign=Gtk.Align.END,
            reveal_child=True,
        )
        overlay.add_overlay(self._controls)
        overlay.add_overlay(self._info_revealer)
        self.set_content(overlay)

        self._install_controllers()
        self.connect("close-request", self._on_close_request)
        self._apply_mode(ViewMode.WALLPAPER)
        self._update_info()
        self._pending = runner.submit(
            lambda: load_full_texture(path), self._on_loaded, self._on_failed
        )

    # Construction ----------------------------------------------------------

    def _build_controls(self, title: str) -> Gtk.Widget:
        self._mode_buttons: dict[ViewMode, Gtk.ToggleButton] = {}
        modes = Gtk.Box()
        modes.add_css_class("linked")
        group: Gtk.ToggleButton | None = None
        for mode, label, tooltip in (
            (
                ViewMode.WALLPAPER,
                _("As Wallpaper"),
                _("Fill the screen, as the desktop shows it (W)"),
            ),
            (
                ViewMode.WHOLE,
                _("Whole Image"),
                _("Fit the whole image on the screen (F)"),
            ),
            (
                ViewMode.ACTUAL,
                _("Actual Size"),
                _("One image pixel per screen pixel (1)"),
            ),
        ):
            button = Gtk.ToggleButton(label=label, tooltip_text=tooltip, group=group)
            button.connect(
                "toggled", lambda b, m=mode: b.get_active() and self._set_mode(m)
            )
            group = group or button
            modes.append(button)
            self._mode_buttons[mode] = button
        self._mode_buttons[ViewMode.WALLPAPER].set_active(True)

        heading = Gtk.Label(
            label=title,
            ellipsize=Pango.EllipsizeMode.END,
            hexpand=True,
            xalign=0,
            max_width_chars=40,
        )
        heading.add_css_class("heading")
        close = Gtk.Button(
            icon_name="window-close-symbolic", tooltip_text=_("Close (Esc)")
        )
        close.add_css_class("circular")
        close.connect("clicked", lambda _b: self.close())

        bar = Gtk.Box(
            spacing=12,
            margin_top=12,
            margin_start=12,
            margin_end=12,
        )
        bar.add_css_class("toolbar")
        bar.add_css_class("osd")
        bar.add_css_class("viewer-controls")
        bar.append(heading)
        bar.append(modes)
        bar.append(close)
        return bar

    def _install_controllers(self) -> None:
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda *_a: self._show_controls())
        self.add_controller(motion)

        clicks = Gtk.GestureClick()
        clicks.connect("pressed", self._on_pressed)
        self._scrolled.add_controller(clicks)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", lambda *_a: self._update_cursor())
        self._scrolled.add_controller(drag)

    # Loading ---------------------------------------------------------------

    def _on_loaded(self, texture: Gdk.Texture) -> None:
        self._pending = None
        self._spinner.set_visible(False)
        self._texture = texture
        self._picture.set_paintable(texture)
        self._position_pending = self._mode is ViewMode.ACTUAL
        self._apply_mode(self._mode)
        self._update_info()
        self._schedule_hide()

    def _on_failed(self, _error: Exception) -> None:
        self._pending = None
        self._spinner.set_visible(False)
        self._error.set_visible(True)

    def _on_close_request(self, _window: Adw.Window) -> bool:
        if self._pending is not None:
            self._pending.cancel()
        if self._hide_source is not None:
            GLib.source_remove(self._hide_source)
            self._hide_source = None
        # Drop the full-size texture right away instead of waiting for collection.
        self._picture.set_paintable(None)
        self._texture = None
        return False

    # Modes -----------------------------------------------------------------

    def _set_mode(
        self, mode: ViewMode, anchor: tuple[float, float] | None = None
    ) -> None:
        if mode is self._mode and anchor is None:
            return
        self._anchor = self._anchor_for(anchor) if mode is ViewMode.ACTUAL else None
        self._position_pending = mode is ViewMode.ACTUAL
        self._apply_mode(mode)
        if not self._mode_buttons[mode].get_active():
            self._mode_buttons[mode].set_active(True)
        self._update_info()
        self._update_cursor()

    def _apply_mode(self, mode: ViewMode) -> None:
        self._mode = mode
        if mode is ViewMode.ACTUAL:
            self._scrolled.set_policy(
                Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC
            )
            self._picture.set_content_fit(Gtk.ContentFit.FILL)
            self._picture.set_halign(Gtk.Align.CENTER)
            self._picture.set_valign(Gtk.Align.CENTER)
            width, height = self._actual_logical_size()
            self._picture.set_size_request(width, height)
            return
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        self._picture.set_size_request(-1, -1)
        self._picture.set_halign(Gtk.Align.FILL)
        self._picture.set_valign(Gtk.Align.FILL)
        fill = mode is ViewMode.WALLPAPER and self._crops
        self._picture.set_content_fit(
            Gtk.ContentFit.COVER if fill else Gtk.ContentFit.CONTAIN
        )

    def _scale(self) -> float:
        surface = self.get_surface()
        return surface.get_scale() if surface is not None else self.get_scale_factor()

    def _texture_size(self) -> tuple[int, int]:
        if self._texture is None:
            return self._width, self._height
        return self._texture.get_width(), self._texture.get_height()

    def _actual_logical_size(self) -> tuple[int, int]:
        width, height = self._texture_size()
        scale = self._scale() or 1.0
        return max(1, round(width / scale)), max(1, round(height / scale))

    def _anchor_for(
        self, point: tuple[float, float] | None
    ) -> tuple[float, float, float, float] | None:
        """Image fraction under ``point`` and the point itself, to keep it in place."""
        view_width, view_height = self._size
        if point is None or not view_width or not view_height:
            return None
        width, height = self._actual_logical_size()
        fit = max if self._mode is ViewMode.WALLPAPER and self._crops else min
        factor = fit(view_width / width, view_height / height)
        left = (view_width - width * factor) / 2
        top = (view_height - height * factor) / 2
        fraction_x = min(1.0, max(0.0, (point[0] - left) / (width * factor)))
        fraction_y = min(1.0, max(0.0, (point[1] - top) / (height * factor)))
        return fraction_x, fraction_y, point[0], point[1]

    def _position_actual(self) -> bool:
        """Center the image, or keep the double-clicked point under the pointer."""
        if self._mode is ViewMode.ACTUAL:
            for index, adjustment in enumerate(
                (self._scrolled.get_hadjustment(), self._scrolled.get_vadjustment())
            ):
                upper, page = adjustment.get_upper(), adjustment.get_page_size()
                if self._anchor is not None:
                    value = upper * self._anchor[index] - self._anchor[index + 2]
                else:
                    value = (upper - page) / 2
                adjustment.set_value(min(max(0.0, value), max(0.0, upper - page)))
        return GLib.SOURCE_REMOVE

    # Information -----------------------------------------------------------

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        Adw.Window.do_size_allocate(self, width, height, baseline)
        if self._position_pending:
            self._position_pending = False
            # Adjustments hold the new layout only after this allocation.
            GLib.idle_add(self._position_actual)
        if (width, height) != self._size:
            self._size = (width, height)
            # Labels cannot change while the window is being allocated.
            GLib.idle_add(self._update_info_once)

    def _update_info_once(self) -> bool:
        self._update_info()
        return GLib.SOURCE_REMOVE

    def _update_info(self) -> None:
        dimensions = _("{width} × {height} px").format(
            width=self._width, height=self._height
        )
        texture_width, texture_height = self._texture_size()
        parts = [dimensions]
        if self._texture is not None and texture_width < self._width:
            parts.append(
                _("shown reduced to {width} × {height} to save memory").format(
                    width=texture_width, height=texture_height
                )
            )
        if (zoom := self._zoom()) is not None:
            parts.append(_("{zoom}% of actual size").format(zoom=zoom))
        self._info.set_label(" · ".join(parts))

    def _zoom(self) -> int | None:
        """Screen pixels per image pixel of the original file, as a percentage."""
        view_width, view_height = self._size
        if not view_width or not view_height or not self._width:
            return None
        scale = self._scale() or 1.0
        if self._mode is ViewMode.ACTUAL:
            texture_width, _texture_height = self._texture_size()
            return round(100 * texture_width / self._width)
        fit = max if self._mode is ViewMode.WALLPAPER and self._crops else min
        factor = fit(
            view_width * scale / self._width, view_height * scale / self._height
        )
        return max(1, round(100 * factor))

    # Input -----------------------------------------------------------------

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        name = Gdk.keyval_name(keyval) or ""
        if name in ("Escape", "F11", "q"):
            self.close()
            return True
        mode = {"w": ViewMode.WALLPAPER, "f": ViewMode.WHOLE, "1": ViewMode.ACTUAL}.get(
            name.lower()
        )
        if mode is not None:
            self._set_mode(mode)
            self._show_controls()
            return True
        return False

    def _on_pressed(
        self, gesture: Gtk.GestureClick, presses: int, x: float, y: float
    ) -> None:
        if presses != 2:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        if self._mode is ViewMode.ACTUAL:
            self._set_mode(ViewMode.WALLPAPER)
        else:
            self._set_mode(ViewMode.ACTUAL, (x, y))

    def _on_drag_begin(self, _gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        self._drag_origin = (
            self._scrolled.get_hadjustment().get_value(),
            self._scrolled.get_vadjustment().get_value(),
        )
        if self._mode is ViewMode.ACTUAL:
            self.set_cursor_from_name("grabbing")

    def _on_drag_update(self, _gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._mode is not ViewMode.ACTUAL:
            return
        self._anchor = None
        self._scrolled.get_hadjustment().set_value(self._drag_origin[0] - dx)
        self._scrolled.get_vadjustment().set_value(self._drag_origin[1] - dy)

    def _update_cursor(self) -> None:
        if not self._controls.get_reveal_child():
            self.set_cursor_from_name("none")
        elif self._mode is ViewMode.ACTUAL:
            self.set_cursor_from_name("grab")
        else:
            self.set_cursor_from_name(None)

    # Controls visibility ---------------------------------------------------

    def _show_controls(self) -> None:
        if not self._controls.get_reveal_child():
            self._controls.set_reveal_child(True)
            self._info_revealer.set_reveal_child(True)
            self._update_cursor()
        self._schedule_hide()

    def _schedule_hide(self) -> None:
        if self._hide_source is not None:
            GLib.source_remove(self._hide_source)
        self._hide_source = GLib.timeout_add(CONTROLS_TIMEOUT_MS, self._hide_controls)

    def _hide_controls(self) -> bool:
        self._hide_source = None
        if self._texture is not None:
            self._controls.set_reveal_child(False)
            self._info_revealer.set_reveal_child(False)
            self._update_cursor()
        return GLib.SOURCE_REMOVE


def open_viewer(
    widget: Gtk.Widget,
    title: str,
    path: Path,
    width: int,
    height: int,
    crops: bool,
    runner: BackgroundRunner,
) -> ImageViewer:
    root = widget.get_root()
    viewer = ImageViewer(
        root if isinstance(root, Gtk.Window) else None,
        title,
        path,
        width,
        height,
        crops,
        runner,
    )
    viewer.fullscreen()
    viewer.present()
    return viewer
