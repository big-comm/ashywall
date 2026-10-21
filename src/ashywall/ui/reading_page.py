"""Quiet native reading view: cached summaries, explicit refresh and browser links."""

import threading
from dataclasses import replace

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..i18n import _, ngettext
from ..providers.base import optional_web_url
from ..services.feed_discovery import FeedDiscoveryError
from ..services.feeds import FeedAddressError, FeedError
from ..services.http import HttpError, TransferCancelled
from ..services.reading import ReadingService
from ..services.reading_repository import (
    Article,
    ReadingError,
    ReadingFeed,
    ReadingQuery,
)
from .background_tasks import BackgroundRunner
from .errors import error_text
from .reading_categories import CategoryGroup


def reading_error(error: Exception) -> str:
    if isinstance(error, FeedAddressError):
        return _(
            "Enter a public website or HTTPS feed address. Local network addresses are not supported."
        )
    if isinstance(error, FeedDiscoveryError):
        return _(
            "No readable feed was found. Paste the site's direct RSS or Atom address."
        )
    if isinstance(error, FeedError):
        return _("This address did not return a supported RSS or Atom feed.")
    if isinstance(error, HttpError) and error.status == 403:
        return _("This site refused access. Try its direct RSS or Atom address.")
    if isinstance(error, ReadingError):
        return {
            "duplicate": _("This source is already in your reading list."),
            "category-too-long": _("Use at most 60 characters for the category."),
            "too-many-feeds": _("You can add up to 50 sources."),
        }.get(str(error), _("The source could not be saved."))
    return error_text(error)


def article_meta(article: Article) -> str:
    local_date = (
        GLib.DateTime.new_from_unix_local(article.published_at)
        if article.published_at is not None
        else None
    )
    date = local_date.format("%x") if local_date is not None else None
    date = date or _("Date not provided")
    return " · ".join(
        part for part in (article.feed_title, article.category, date) if part
    )


class ArticleItem(GObject.Object):
    def __init__(self, article: Article) -> None:
        super().__init__()
        self.article = article


class ArticleRow(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_start=20,
            margin_end=20,
            margin_top=18,
            margin_bottom=18,
        )
        top = Gtk.Box(spacing=10)
        self._unread = Gtk.Image(
            icon_name="mail-unread-symbolic",
            pixel_size=16,
            valign=Gtk.Align.START,
            margin_top=3,
        )
        self._unread.add_css_class("accent")
        self._title = Gtk.Label(
            xalign=0,
            wrap=True,
            lines=2,
            ellipsize=Pango.EllipsizeMode.END,
            hexpand=True,
        )
        self._title.add_css_class("heading")
        top.append(self._title)
        top.append(self._unread)
        self._meta = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self._meta.add_css_class("caption")
        self._meta.add_css_class("dim-label")
        self._summary = Gtk.Label(
            xalign=0, wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END
        )
        self._summary.add_css_class("dim-label")
        for widget in (top, self._meta, self._summary):
            self.append(widget)

    def bind(self, article: Article) -> None:
        self._title.set_label(article.title)
        self._meta.set_label(article_meta(article))
        self._summary.set_label(article.summary)
        self._summary.set_visible(bool(article.summary))
        self._unread.set_opacity(0 if article.is_read else 1)
        self._unread.set_tooltip_text(_("Unread"))
        self.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [article.title + ". " + (_("Read") if article.is_read else _("Unread"))],
        )


class ReadingPage(Adw.NavigationPage):
    __gtype_name__ = "AshyWallReadingPage"

    def __init__(
        self, service: ReadingService, navigation: Adw.NavigationView, toast
    ) -> None:
        super().__init__(title=_("Reading"), tag="reading")
        self._service, self._navigation, self._toast = service, navigation, toast
        self._runner = BackgroundRunner(1, "ashywall-reading")
        self._network = BackgroundRunner(1, "ashywall-feeds")
        self._query = ReadingQuery()
        self._generation = 0
        self._closed = False
        self._refreshing = False
        self._cancel = threading.Event()
        self._dialog_cancels: list[threading.Event] = []
        self._feeds: list[ReadingFeed] = []
        self._categories: list[str] = []
        self._quiet = False
        self._sources_dialog: Adw.Dialog | None = None
        self._store = Gio.ListStore(item_type=ArticleItem)
        self._title = Adw.WindowTitle(
            title=_("Reading"), subtitle=_("Articles from your sources")
        )
        header = Adw.HeaderBar(title_widget=self._title)
        self._sources_button = Gtk.Button(
            child=Adw.ButtonContent(
                icon_name="application-rss+xml-symbolic", label=_("Sources")
            ),
            tooltip_text=_("Manage Sources"),
        )
        self._sources_button.connect("clicked", lambda _b: self.show_sources())
        header.pack_start(self._sources_button)
        self._refresh_button = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text=_("Refresh Sources")
        )
        self._refresh_button.connect("clicked", lambda _b: self.refresh())
        header.pack_end(self._refresh_button)
        search_button = Gtk.ToggleButton(
            icon_name="system-search-symbolic", tooltip_text=_("Search Articles")
        )
        header.pack_end(search_button)
        self._search = Gtk.SearchEntry(
            placeholder_text=_("Search articles or sources"),
            search_delay=200,
            hexpand=True,
        )
        self._search.connect("search-changed", lambda _e: self._filters_changed())
        search_bar = Gtk.SearchBar(
            child=Adw.Clamp(child=self._search, maximum_size=480)
        )
        search_bar.connect_entry(self._search)
        search_button.bind_property(
            "active",
            search_bar,
            "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL,
        )
        self._search_button = search_button

        self._topics = Gtk.DropDown.new_from_strings([_("All Categories")])
        topic_factory = Gtk.SignalListItemFactory()
        topic_factory.connect(
            "setup",
            lambda _f, item: item.set_child(
                Gtk.Label(
                    xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=18
                )
            ),
        )
        topic_factory.connect(
            "bind",
            lambda _f, item: item.get_child().set_label(item.get_item().get_string()),
        )
        self._topics.set_factory(topic_factory)
        self._topics.set_tooltip_text(_("Filter by Category"))
        self._topics.connect("notify::selected", lambda *_a: self._filters_changed())
        self._unread = Gtk.ToggleButton(label=_("Unread Only"))
        self._unread.connect("toggled", lambda _b: self._filters_changed())
        filters = Gtk.Box(
            spacing=8, margin_start=12, margin_end=12, margin_top=8, margin_bottom=8
        )
        spacer = Gtk.Box(hexpand=True)
        filters.append(self._topics)
        filters.append(spacer)
        filters.append(self._unread)
        filter_bin = Adw.BreakpointBin(child=filters, width_request=1, height_request=1)
        narrow = Adw.Breakpoint(
            condition=Adw.BreakpointCondition.parse("max-width: 420sp")
        )
        narrow.add_setter(filters, "orientation", Gtk.Orientation.VERTICAL)
        narrow.add_setter(spacer, "visible", False)
        narrow.add_setter(self._unread, "halign", Gtk.Align.END)
        narrow.add_setter(self._title, "visible", False)
        filter_bin.add_breakpoint(narrow)

        self._banner = Adw.Banner(title="", button_label=_("Sources"), revealed=False)
        self._banner.connect("button-clicked", lambda _b: self.show_sources())
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, item: item.set_child(ArticleRow()))
        factory.connect(
            "bind", lambda _f, item: item.get_child().bind(item.get_item().article)
        )
        self._list = Gtk.ListView(
            model=Gtk.NoSelection(model=self._store),
            factory=factory,
            single_click_activate=True,
        )
        self._list.add_css_class("reading-list")
        self._list.connect("activate", self._on_article)
        self._list.update_property([Gtk.AccessibleProperty.LABEL], [_("Articles")])
        scrolled = Gtk.ScrolledWindow(
            child=Adw.Clamp(child=self._list, maximum_size=840),
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vexpand=True,
        )
        self._status = Adw.StatusPage(icon_name="application-rss+xml-symbolic")
        self._empty_button = Gtk.Button(label=_("Add Source"), halign=Gtk.Align.CENTER)
        self._empty_button.add_css_class("pill")
        self._empty_button.add_css_class("suggested-action")
        self._empty_button.connect(
            "clicked",
            lambda _b: self.add_source() if not self._feeds else self.refresh(),
        )
        self._status.set_child(self._empty_button)
        self._stack = Gtk.Stack(
            vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE
        )
        self._stack.add_named(scrolled, "articles")
        self._stack.add_named(self._status, "status")
        self._more = Gtk.Button(
            label=_("Load More"),
            halign=Gtk.Align.CENTER,
            margin_top=6,
            margin_bottom=12,
            visible=False,
        )
        self._more.add_css_class("pill")
        self._more.connect("clicked", lambda _b: self.reload(append=True))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for widget in (self._banner, filter_bin, self._stack, self._more):
            body.append(widget)
        toolbar = Adw.ToolbarView(content=body)
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(search_bar)
        self.set_child(toolbar)

    def focus_search(self) -> None:
        self._search_button.set_active(True)
        self._search.grab_focus()

    def _filters_changed(self) -> None:
        if self._quiet:
            return
        selected = self._topics.get_selected()
        category = (
            self._categories[selected - 1]
            if 0 < selected <= len(self._categories)
            else ""
        )
        self._query = ReadingQuery(
            category, self._search.get_text(), self._unread.get_active()
        )
        self.reload()

    def reload(self, append: bool = False) -> None:
        if self._closed:
            return
        self._generation += 1
        generation, query = self._generation, self._query
        offset = self._store.get_n_items() if append else 0
        self._more.set_sensitive(False)

        def work():
            if generation != self._generation or self._closed:
                return None
            return self._service.snapshot(query, offset)

        def done(result):
            if self._closed or generation != self._generation or result is None:
                return
            feeds, articles, more = result
            self._feeds = feeds
            categories = sorted(
                {feed.category for feed in feeds if feed.category}, key=str.casefold
            )
            if categories != self._categories:
                self._quiet = True
                self._categories = categories
                self._topics.set_model(
                    Gtk.StringList.new([_("All Categories"), *categories])
                )
                self._topics.set_selected(
                    categories.index(query.category) + 1
                    if query.category in categories
                    else 0
                )
                self._quiet = False
                if query.category and query.category not in categories:
                    self._query = replace(query, category="")
                    self.reload()
                    return
            if not append:
                self._store.remove_all()
            for article in articles:
                self._store.append(ArticleItem(article))
            self._more.set_visible(more)
            self._more.set_sensitive(True)
            self._show_state()
            self._update_sources()

        def failed(error):
            if not self._closed and generation == self._generation:
                self._toast(
                    _("Reading could not be loaded: {error}").format(
                        error=reading_error(error)
                    )
                )
                self._more.set_sensitive(True)

        self._runner.submit(work, done, failed)

    def _show_state(self) -> None:
        if self._store.get_n_items():
            self._stack.set_visible_child_name("articles")
        else:
            self._stack.set_visible_child_name("status")
            filtered = bool(
                self._query.text or self._query.category or self._query.unread_only
            )
            self._status.set_title(
                _("A Quiet Place to Read")
                if not self._feeds
                else (_("No Matching Articles") if filtered else _("No Articles Yet"))
            )
            self._status.set_description(
                _(
                    "Add RSS or Atom sources and organize them by category. Articles open in your browser."
                )
                if not self._feeds
                else (
                    _("Try another category or search, or turn off the unread filter.")
                    if filtered
                    else _("Update your sources or add another feed.")
                )
            )
            self._empty_button.set_label(
                _("Add Source") if not self._feeds else _("Refresh Sources")
            )
            self._empty_button.set_visible(not filtered or not self._feeds)
        if not self._refreshing:
            self._title.set_subtitle(_("Articles from your sources"))
        failures = sum(bool(feed.last_error) for feed in self._feeds)
        self._banner.set_title(
            ngettext(
                "One source could not be updated. Cached articles are still available.",
                "{count} sources could not be updated. Cached articles are still available.",
                failures,
            ).format(count=failures)
        )
        self._banner.set_revealed(bool(failures))

    def refresh(self) -> None:
        if self._refreshing:
            self._cancel.set()
            return
        if not self._feeds:
            self.add_source()
            return
        self._cancel = threading.Event()
        self._set_refreshing(True)

        def done(_failures):
            if not self._closed:
                self._set_refreshing(False)
                self.reload()

        def failed(error):
            if not self._closed:
                self._set_refreshing(False)
                self.reload()
                if not isinstance(error, TransferCancelled):
                    self._toast(reading_error(error))

        self._network.submit(lambda: self._service.refresh(self._cancel), done, failed)

    def _set_refreshing(self, busy: bool) -> None:
        self._refreshing = busy
        self._refresh_button.set_icon_name(
            "process-stop-symbolic" if busy else "view-refresh-symbolic"
        )
        self._refresh_button.set_tooltip_text(
            _("Stop Updating") if busy else _("Refresh Sources")
        )
        self._title.set_subtitle(
            _("Updating sources…") if busy else _("Articles from your sources")
        )

    def _mutate(self, method: str, *args) -> None:
        if self._closed:
            return
        self._runner.submit(
            lambda: self._service.mutate(method, *args),
            lambda _r: self.reload(),
            lambda error: (
                self._toast(reading_error(error)) if not self._closed else None
            ),
        )

    def add_source(self) -> None:
        cancel = threading.Event()
        self._dialog_cancels.append(cancel)
        closed = [False]
        dialog = Adw.Dialog(title=_("Add Source"), content_width=480)
        address = Adw.EntryRow(title=_("Website or Feed Address"))
        category_group = CategoryGroup(self._categories)
        category = category_group.entry
        group = Adw.PreferencesGroup(
            description=_(
                "Enter a website, such as phoronix.com, or an HTTPS RSS or Atom address. Organize sources under Technology, Health or Science."
            )
        )
        group.add(address)
        error_label = Gtk.Label(wrap=True, xalign=0, visible=False)
        error_label.add_css_class("error")
        submit = Gtk.Button(
            label=_("Add Source"), halign=Gtk.Align.END, sensitive=False
        )
        submit.add_css_class("suggested-action")
        submit.add_css_class("pill")
        address.connect(
            "changed", lambda _e: submit.set_sensitive(bool(address.get_text().strip()))
        )
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=18,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )
        for widget in (group, category_group, error_label, submit):
            content.append(widget)
        toolbar = Adw.ToolbarView(content=content)
        toolbar.add_top_bar(Adw.HeaderBar())
        dialog.set_child(toolbar)

        def finish_close(_d):
            closed[0] = True
            cancel.set()
            self._dialog_cancels.remove(cancel)

        dialog.connect("closed", finish_close)

        def failed(error):
            if closed[0] or self._closed:
                return
            submit.set_label(_("Add Source"))
            submit.set_sensitive(True)
            address.set_sensitive(True)
            category_group.set_sensitive(True)
            error_label.set_label(reading_error(error))
            error_label.set_visible(True)

        def done(_feed_id):
            if self._closed:
                return
            self.reload()
            if not closed[0]:
                dialog.close()

        def add(_button):
            url, topic = address.get_text(), category.get_text()
            submit.set_sensitive(False)
            submit.set_label(_("Checking Source…"))
            address.set_sensitive(False)
            category_group.set_sensitive(False)
            error_label.set_visible(False)
            self._network.submit(
                lambda: self._service.add(url, topic, cancel), done, failed
            )

        submit.connect("clicked", add)
        dialog.present(self)

    def show_sources(self) -> None:
        if self._sources_dialog is not None:
            self._sources_dialog.present(self)
            return
        self._sources_dialog = Adw.Dialog(
            title=_("Sources"), content_width=540, content_height=520
        )
        self._source_groups = []
        self._source_content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=24,
            margin_start=18,
            margin_end=18,
            margin_top=18,
            margin_bottom=24,
        )
        description = Gtk.Label(
            label=_(
                "Select a source to change its category. Use the trash button to remove it."
            ),
            wrap=True,
            xalign=0,
        )
        description.add_css_class("dim-label")
        self._source_content.append(description)
        header = Adw.HeaderBar()
        add = Gtk.Button(
            child=Adw.ButtonContent(
                icon_name="list-add-symbolic", label=_("Add Source")
            )
        )
        add.connect("clicked", lambda _b: self.add_source())
        header.pack_end(add)
        toolbar = Adw.ToolbarView(
            content=Gtk.ScrolledWindow(
                child=self._source_content, hscrollbar_policy=Gtk.PolicyType.NEVER
            )
        )
        toolbar.add_top_bar(header)
        self._sources_dialog.set_child(toolbar)
        self._sources_dialog.connect(
            "closed", lambda _d: setattr(self, "_sources_dialog", None)
        )
        self._update_sources()
        self._sources_dialog.present(self)

    def _update_sources(self) -> None:
        if self._sources_dialog is None:
            return
        for group in self._source_groups:
            self._source_content.remove(group)
        self._source_groups = []
        if not self._feeds:
            group = Adw.PreferencesGroup(title=_("Your Sources"))
            group.add(Adw.ActionRow(title=_("No sources added")))
            self._source_content.append(group)
            self._source_groups.append(group)
        categories = sorted(
            {feed.category for feed in self._feeds},
            key=lambda category: (not bool(category), category.casefold()),
        )
        for category in categories:
            feeds = [feed for feed in self._feeds if feed.category == category]
            group = Adw.PreferencesGroup(
                title=GLib.markup_escape_text(category)
                if category
                else _("Uncategorized"),
                description=ngettext(
                    "{count} source", "{count} sources", len(feeds)
                ).format(count=len(feeds)),
            )
            for feed in feeds:
                subtitle = feed.url
                if feed.last_error:
                    subtitle = _("Could not update this source") + "\n" + subtitle
                row = Adw.ActionRow(
                    title=GLib.markup_escape_text(feed.title),
                    subtitle=GLib.markup_escape_text(subtitle),
                    title_lines=1,
                    subtitle_lines=2,
                    activatable=True,
                    tooltip_text=_("Change Category"),
                )
                row.connect(
                    "activated", lambda _r, source=feed: self._edit_topic(source)
                )
                edit = Gtk.Button(
                    icon_name="folder-symbolic",
                    tooltip_text=_("Change Category"),
                    valign=Gtk.Align.CENTER,
                )
                edit.add_css_class("flat")
                edit.connect(
                    "clicked", lambda _b, source=feed: self._edit_topic(source)
                )
                remove = Gtk.Button(
                    icon_name="user-trash-symbolic",
                    tooltip_text=_("Remove Source"),
                    valign=Gtk.Align.CENTER,
                )
                remove.add_css_class("flat")
                remove.connect(
                    "clicked", lambda _b, source=feed: self._remove_source(source)
                )
                row.add_suffix(edit)
                row.add_suffix(remove)
                group.add(row)
            self._source_content.append(group)
            self._source_groups.append(group)

    def _edit_topic(self, feed: ReadingFeed) -> None:
        group = CategoryGroup(self._categories, feed.category)
        dialog = Adw.AlertDialog(
            heading=_("Change Category"),
            body=feed.title,
        )
        dialog.set_body_use_markup(False)
        error_label = Gtk.Label(wrap=True, xalign=0, visible=False)
        error_label.add_css_class("error")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.append(group)
        content.append(error_label)
        dialog.set_extra_child(content)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("save", _("Save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def changed(_entry):
            valid = len(" ".join(group.entry.get_text().split())) <= 60
            dialog.set_response_enabled("save", valid)
            error_label.set_label(reading_error(ReadingError("category-too-long")))
            error_label.set_visible(not valid)

        group.entry.connect("changed", changed)
        dialog.connect(
            "response",
            lambda _d, response: (
                self._mutate("edit", feed.id, group.entry.get_text())
                if response == "save"
                else None
            ),
        )
        dialog.present(self)

    def _remove_source(self, feed: ReadingFeed) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Remove Source?"),
            body=_(
                "“{name}” and its cached articles will be removed from Reading."
            ).format(name=feed.title),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect(
            "response",
            lambda _d, response: (
                self._mutate("remove", feed.id) if response == "remove" else None
            ),
        )
        dialog.present(self)

    def _on_article(self, _view, position: int) -> None:
        article = self._store.get_item(position).article
        page = Adw.NavigationPage(title=_("Article"))
        header = Adw.HeaderBar()
        title = Gtk.Label(label=article.title, xalign=0, wrap=True, selectable=True)
        title.add_css_class("title-1")
        meta = Gtk.Label(
            label=article_meta(article), xalign=0, wrap=True, selectable=True
        )
        meta.add_css_class("dim-label")
        summary = Gtk.Label(
            label=article.summary or _("This source did not provide a summary."),
            xalign=0,
            wrap=True,
            selectable=True,
        )
        note = Gtk.Label(
            label=_(
                "Summary provided by the source. Read the full article in your browser."
            ),
            xalign=0,
            wrap=True,
        )
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        read = Gtk.ToggleButton(active=bool(article.is_read), halign=Gtk.Align.START)

        def read_changed(_button):
            read.set_label(
                _("Mark as Unread") if read.get_active() else _("Mark as Read")
            )
            self._mutate("mark_read", article.id, read.get_active())

        read.set_label(_("Mark as Unread") if article.is_read else _("Mark as Read"))
        read.connect("toggled", read_changed)
        launch = Gtk.Button(label=_("Open in Browser"), halign=Gtk.Align.START)
        launch.add_css_class("pill")
        launch.add_css_class("suggested-action")

        def open_article(_button):
            url = optional_web_url(article.url)
            if url is None:
                self._toast(_("This article has an invalid address."))
                return

            def opened(launcher, result):
                if self._closed:
                    return
                try:
                    launcher.launch_finish(result)
                    read.set_active(True)
                except GLib.Error:
                    self._toast(_("The article could not be opened in the browser."))

            Gtk.UriLauncher.new(url).launch(self.get_root(), None, opened)

        launch.connect("clicked", open_article)
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        actions.append(launch)
        actions.append(read)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=24,
            margin_start=24,
            margin_end=24,
            margin_top=28,
            margin_bottom=36,
        )
        for widget in (meta, title, summary, note, actions):
            content.append(widget)
        toolbar = Adw.ToolbarView(
            content=Gtk.ScrolledWindow(
                child=Adw.Clamp(child=content, maximum_size=720),
                hscrollbar_policy=Gtk.PolicyType.NEVER,
            )
        )
        toolbar.add_top_bar(header)
        page.set_child(toolbar)
        self._navigation.push(page)

    def stop(self) -> None:
        self._cancel.set()
        for cancel in self._dialog_cancels:
            cancel.set()

    def shutdown(self) -> None:
        self._closed = True
        self._generation += 1
        self.stop()
        self._runner.shutdown()
        self._network.shutdown()
