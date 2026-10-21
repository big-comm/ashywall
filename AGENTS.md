# AGENTS.md

Guide for AI agents editing this repository.

## What this is

GTK 4 / libadwaita wallpaper library for GNOME (BigCommunity first). Pure
Python via PyGObject; Pillow for image work; SQLite catalog.

## Layout

```
src/ashywall/
  app.py              Adw.Application, service wiring, app actions.
  paths.py            XDG locations (read from env on every call).
  i18n.py             gettext setup, exports _ and ngettext.
  models/             Wallpaper, Source, resolution/orientation rules.
  services/           database (migrations), library_index (incremental
                      indexer), library_repository (queries), image_probe
                      (safe opening), thumbnails, hashing, package_owner,
                      http (size limits, rate limiter, https-only redirects),
                      connections (persistent HTTPS connections, name cache),
                      remote_images (remote thumbnails, full-resolution
                      preview cache, verified downloads), rotation (GNOME
                      slideshow XML for the automatic change).
  providers/          base (RemoteWallpaper, RemoteQuery, URL validation),
                      local (system catalogs/folders), biglinux (BigLinux
                      wallpaper packages on GitHub), wallhaven (API v1),
                      bing (image of the day), commons (Wikimedia picture
                      of the day), registry (the providers, in sidebar order).
  desktop/gnome.py    Apply/restore through org.gnome.desktop.background.
  ui/                 Window, gallery (lazy list model, library reader),
                      local and remote detail pages, shared detail widgets,
                      full-screen image viewer, automatic change dialog,
                      preferences, thumbnail loader, index, explore and
                      download controllers, style.css.
tests/                pytest suite (services, discovery, GNOME settings).
usr/                  Files installed to /usr (desktop entry, icon, .mo).
locale/               POT template and .po catalogs.
pkgbuild/             Arch/BigCommunity PKGBUILD.
```

## Rules

1. Never block the main loop: disk scans, decoding, hashing and subprocesses
   run in worker threads (`BackgroundRunner`, `IndexController`). Results
   return through `GLib.idle_add` (`run_on_main`).
2. Cost must follow the screen, not the library: never load every row into
   Python objects on the main thread. The gallery takes ordered ids
   (`wallpaper_ids`) and reads rows by page (`GalleryModel`); library
   queries run in `LibraryReader`. Check plans with `EXPLAIN QUERY PLAN` on
   a large catalog when changing SQL.
3. One SQLite connection per thread. Schema changes go in a new entry of
   `MIGRATIONS` in `services/database.py`; never edit a released one.
4. Open images only through `image_probe.open_image` (format allow-list,
   size and pixel limits checked before decoding).
5. Never show invented data: counts come from the catalog, unknown fields say
   so, unimplemented features say "not available yet".
6. Tests must not touch the user's desktop: use
   `Gio.memory_settings_backend_new()` for GSettings, and run interface
   sessions with `GSETTINGS_BACKEND=keyfile` and their own `XDG_*`
   directories (never start a `dconf-service` there), preferably inside
   `dbus-run-session` so the application name stays off the real bus.
7. Language: UI strings in English wrapped in `_()`, translated in
   `locale/pt_BR.po`; code, comments and commits in English.
8. Online data is untrusted: parse with strict type checks, accept only
   https URLs on the provider's own hosts, never fetch display-only links
   (sources, pages), and send safe-for-work queries only.
9. Run before finishing: `python -m pytest` and `ruff check src tests`.
10. While AshyWall is in BigCommunity testing, keep all development, commits
    and pushes on `dev-xathay`. The package recipe must fetch that branch.
    Do not merge into, push to or rewrite `main` without a new, explicit
    maintainer instruction to promote the work.
