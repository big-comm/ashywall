<p align="center">
  <img src="usr/share/icons/hicolor/scalable/apps/org.communitybig.ashywall.svg" width="128" height="128" alt="AshyWall icon">
</p>

# AshyWall

A wallpaper library for **BigCommunity GNOME**, built with Python, GTK4
and libadwaita. Browse system wallpapers, personal folders and online
sources, organize your images and apply them to your desktop. An optional
RSS/Atom reading section lets you follow websites by category.

The interface follows GNOME's light and dark themes and adapts to narrow
windows. GNOME is the current desktop target.

## Interface

A BigLinux wallpaper preview with a pink sky reflected in a mountain lake.
The screenshot below follows the theme used to view this page.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/biglinux-wallpaper-dark.png">
  <img src="docs/screenshots/biglinux-wallpaper-light.png" alt="AshyWall's native GNOME interface previewing mountains, a lake and a pink sky" width="1280">
</picture>

| System wallpaper | Collections |
| --- | --- |
| ![BigCommunity owl wallpaper preview](docs/screenshots/system-wallpaper-dark.png) | ![A collection of colorful system wallpapers](docs/screenshots/collections-dark.png) |

These are real application captures in Brazilian Portuguese, taken in an
isolated demonstration session. See [more screenshots and wallpaper credits](docs/screenshots/README.md),
including the BigLinux gallery and optional Reading section.

## Highlights

- **Local library:** system wallpapers, downloads and folders you choose,
  with search and filters for orientation and resolution.
- **Online sources:** BigLinux, Wallhaven, Bing and Wikimedia Commons, with
  the source information, author credits and licenses each service provides.
- **Preview and full-screen view:** real image dimensions, monitor crop
  simulation, fit-to-window viewing and actual pixel size.
- **Apply on GNOME:** choose the light style, dark style or both, with undo
  and restoration of the previous wallpaper.
- **Favorites and collections:** organize local and online images without
  duplicating files. Removing folders or collections never deletes your images.
- **Automatic change:** use local images from a section or collection,
  choose an interval and shuffle. GNOME keeps changing wallpapers after
  AshyWall closes.
- **Optional Reading:** RSS/Atom sources grouped by category, search,
  read/unread state and cached summaries for offline reading.

## Installation

### BigCommunity

With the BigCommunity repository providing `ashywall` enabled:

```bash
sudo pacman -Syu ashywall
```

The installed version follows the package channel configured on your system.
Launch **AshyWall** from the application menu or run `ashywall`.

### Build and install locally

On systems using pacman, install the build tools and get the source:

```bash
sudo pacman -S --needed base-devel git
git clone --branch dev-xathay https://github.com/big-comm/ashywall.git
cd ashywall
./scripts/build-local-package.sh -si
```

The script installs dependencies declared in the package recipe, builds the
current Git snapshot, runs the tests and installs through pacman. Close the
application before upgrading. Commit any checkout changes before building.
Omit `-si` to build only; packages are saved in `dist/packages/`.

### Runtime requirements

- Python ≥ 3.12 and PyGObject.
- GTK ≥ 4.16 and libadwaita ≥ 1.6.
- Pillow ≥ 11.3 and defusedxml ≥ 0.7.1.
- GNOME and `gsettings-desktop-schemas` to apply wallpapers.

## Usage

1. Open **System** to browse installed wallpapers, or add your folders in
   **Preferences → Library → My Folders**.
2. Select an image to open its preview, inspect its details and apply it to
   the light style, dark style or both.
3. Organize your library with favorites and collections. Online sources
   appear under **Explore** in the sidebar.
4. Open **Automatic Change** from the main menu to choose a section or
   collection and configure the interval. Download online images before
   including them in automatic changes.

### Reading by category

Enable **Preferences → Reading → Enable Reading**. In **Sources → Add Source**,
enter a website such as `phoronix.com`, or an HTTPS RSS/Atom feed address.
Choose an existing category or enter a new one.

The folder button changes a source's category; the trash button removes
that source and its cached articles after confirmation. Leave the category
empty to list the source under **Uncategorized**. Use the reading filter
to select a category.

Summaries remain available offline; full articles open in your browser.
Sources are fetched when explicitly added or refreshed, without notifications
or loading article images and scripts.

### Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `Ctrl+F` | Search the current section |
| `F5` or `Ctrl+R` | Refresh the library or Reading sources |
| `Ctrl+,` | Open preferences |
| `Ctrl+Q` | Quit |

## Data and current limitations

The library and downloads are stored in `~/.local/share/ashywall/`;
thumbnail and preview caches are stored in `~/.cache/ashywall/`. These paths
respect XDG environment variables when configured. Your home directory is
not scanned automatically.

GNOME uses one wallpaper per style across all monitors. Per-monitor
wallpapers and applying through the Flatpak wallpaper portal are not yet
supported. Author credits and licenses are shown when supplied by the source;
AshyWall does not grant rights to the images.

See the [feature guide and limitations](docs/FEATURES.md) for resolution
criteria, source behavior, cache limits and security measures.

## Development

From the checkout, with the runtime dependencies installed:

```bash
./scripts/run-dev.sh
```

For full validation, also install the tools listed in
[CONTRIBUTING.md](CONTRIBUTING.md), then run:

```bash
./scripts/validate.sh
```

Tests use temporary data and isolated GNOME settings.
[CONTRIBUTING.md](CONTRIBUTING.md) documents development on `dev-xathay`,
direct publishing and testing-package builds through GitRepo.
[Translations](locale/README.md) and the [changelog](CHANGELOG.md)
have their own documentation.

## License and authorship

[MIT](LICENSE). Developed by BigCommunity contributors.
Wallpaper credits and licenses belong to their respective sources.

The provisional icon adapts the owl from
[AshyTerm](https://github.com/big-comm/ashyterm) (MIT, © 2024 Community Big).
Its license is installed at
`/usr/share/licenses/ashywall/LICENSE.ashyterm-icon`.
