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
                      remote_images (remote thumbnails, full-resolution
                      preview cache, verified downloads).
  providers/          base (RemoteWallpaper, RemoteQuery, URL validation),
                      local (system catalogs/folders), wallhaven (API v1).
  desktop/gnome.py    Apply/restore through org.gnome.desktop.background.
  ui/                 Window, gallery, local and remote detail pages, shared
                      detail widgets, full-screen image viewer, preferences,
                      thumbnail loader, index, explore and download
                      controllers, style.css.
tests/                pytest suite (services, discovery, GNOME settings).
usr/                  Files installed to /usr (desktop entry, icon, .mo).
locale/               POT template and .po catalogs.
pkgbuild/             Arch/BigCommunity PKGBUILD.
```

## Rules

1. Never block the main loop: disk scans, decoding, hashing and subprocesses
   run in worker threads (`BackgroundRunner`, `IndexController`). Results
   return through `GLib.idle_add` (`run_on_main`).
2. One SQLite connection per thread. Schema changes go in a new entry of
   `MIGRATIONS` in `services/database.py`; never edit a released one.
3. Open images only through `image_probe.open_image` (format allow-list,
   size and pixel limits checked before decoding).
4. Never show invented data: counts come from the catalog, unknown fields say
   so, unimplemented features say "not available yet".
5. Tests must not touch the user's desktop: use
   `Gio.memory_settings_backend_new()` for GSettings.
6. Language: UI strings in English wrapped in `_()`, translated in
   `locale/pt_BR.po`; code, comments and commits in English.
7. Online data is untrusted: parse with strict type checks, accept only
   https URLs on the provider's own hosts, never fetch display-only links
   (sources, pages), and send safe-for-work queries only.
8. Run before finishing: `python -m pytest` and `ruff check src tests`.
