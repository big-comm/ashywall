"""Adw.Application entry point."""

import logging
import os
import sys
from importlib import resources

# isort: off
from . import ui  # noqa: F401  (pins GTK/libadwaita versions before gi.repository imports)
from gi.repository import Adw, Gdk, Gio, GLib, Gtk
# isort: on

from . import __version__, paths
from .desktop.gnome import (
    DesktopError,
    GnomeBackground,
    background_schema_installed,
    is_gnome_session,
    is_sandboxed,
)
from .i18n import _
from .providers.wallhaven import WallhavenProvider
from .services.database import DatabaseError, connect
from .services.http import HttpClient
from .services.library_repository import LibraryRepository
from .services.remote_images import RemoteThumbnailCache
from .services.thumbnails import ThumbnailCache
from .ui.background_tasks import BackgroundRunner
from .ui.thumbnail_loader import ThumbnailLoader
from .ui.window import MainWindow, WindowServices

log = logging.getLogger(__name__)


def desktop_backend() -> tuple[GnomeBackground | None, str | None]:
    """Return the backend, or None with the reason applying is unavailable."""
    if is_sandboxed():
        return None, _("Applying wallpapers from a sandbox is not supported yet")
    if not is_gnome_session():
        return None, _("Applying wallpapers is supported only on GNOME in this version")
    if not background_schema_installed():
        return None, _("The GNOME background settings are not installed")
    try:
        return GnomeBackground(), None
    except DesktopError as error:
        return None, str(error)


class AshyWallApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=paths.APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS
        )
        self._services: WindowServices | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name("AshyWall")
        Gtk.Window.set_default_icon_name(paths.APP_ID)
        display = Gdk.Display.get_default()
        if display is not None:
            icon_dir = os.environ.get("ASHYWALL_ICON_DIR")
            if icon_dir:
                Gtk.IconTheme.get_for_display(display).add_search_path(icon_dir)
            provider = Gtk.CssProvider()
            provider.load_from_string(
                resources.files("ashywall.ui").joinpath("style.css").read_text()
            )
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        for name, callback, accels in (
            ("preferences", self._on_preferences, ["<primary>comma"]),
            ("about", self._on_about, []),
            ("quit", lambda *_a: self.quit(), ["<primary>q"]),
        ):
            action = Gio.SimpleAction(name=name)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("win.search", ["<primary>f"])
        self.set_accels_for_action("win.refresh", ["F5", "<primary>r"])
        self.set_accels_for_action("window.close", ["<primary>w"])

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            try:
                window = MainWindow(self, self._create_services())
            except DatabaseError as error:
                window = self._error_window(str(error))
        window.present()

    def do_shutdown(self) -> None:
        if self._services is not None:
            self._services.loader.shutdown()
            self._services.runner.shutdown()
        Adw.Application.do_shutdown(self)

    def _create_services(self) -> WindowServices:
        database_path = paths.database_path()
        thumbnails = ThumbnailCache(paths.thumbnails_dir())
        cdn = HttpClient()
        remote_thumbnails = RemoteThumbnailCache(
            paths.thumbnails_dir() / "remote", cdn, WallhavenProvider.thumbnail_hosts
        )
        runner = BackgroundRunner(2, "ashywall-tasks")
        runner.submit(thumbnails.prune, lambda _removed: None)
        desktop, reason = desktop_backend()
        self._services = WindowServices(
            database_path=database_path,
            repository=LibraryRepository(connect(database_path)),
            thumbnails=thumbnails,
            remote_thumbnails=remote_thumbnails,
            loader=ThumbnailLoader(thumbnails, remote_thumbnails),
            runner=runner,
            cdn=cdn,
            wallhaven=WallhavenProvider(),
            desktop=desktop,
            desktop_unavailable_reason=reason,
        )
        return self._services

    def _error_window(self, message: str) -> Adw.ApplicationWindow:
        page = Adw.StatusPage(
            icon_name="dialog-warning-symbolic",
            title=_("The Library Could Not Be Opened"),
            description=GLib.markup_escape_text(message),
        )
        toolbar = Adw.ToolbarView(content=page)
        toolbar.add_top_bar(Adw.HeaderBar())
        return Adw.ApplicationWindow(
            application=self, content=toolbar, default_width=600, default_height=420
        )

    def _on_preferences(self, *_args: object) -> None:
        window = self.props.active_window
        if isinstance(window, MainWindow):
            window.show_preferences()

    def _on_about(self, *_args: object) -> None:
        about = Adw.AboutDialog(
            application_name="AshyWall",
            application_icon=paths.APP_ID,
            version=__version__,
            comments=_("Browse, organize, preview and apply wallpapers"),
            license_type=Gtk.License.MIT_X11,
            developer_name=_("AshyWall contributors"),
            website="https://communitybig.org/",
        )
        about.add_legal_section(
            _("Owl icon"),
            "Copyright © 2024 Community Big",
            Gtk.License.MIT_X11,
            None,
        )
        about.present(self.props.active_window)


def run() -> int:
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    return AshyWallApplication().run(sys.argv)
