<p align="center">
  <img src="usr/share/icons/hicolor/scalable/apps/org.communitybig.ashywall.svg" width="128" height="128" alt="AshyWall icon">
</p>

# AshyWall

A native GNOME wallpaper library for BigCommunity, built with GTK 4 and
libadwaita. Browse system and online wallpapers, organize collections,
preview images and apply them to your desktop. An optional RSS/Atom reader
keeps your news sources in the same application.

**1.1.0 — development preview.** Available in English and Brazilian
Portuguese; the other 27 [BigCommunity language targets](locale/README.md)
are pending. See the [changelog](CHANGELOG.md).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/biglinux-wallpaper-dark.png">
  <img src="docs/screenshots/biglinux-wallpaper-light.png" alt="AshyWall previewing a BigLinux wallpaper with a pink sky reflected in a mountain lake" width="1280">
</picture>

*BigLinux wallpaper preview, in the light or dark system theme.*

| System wallpaper | Collections |
| --- | --- |
| ![BigCommunity owl wallpaper preview](docs/screenshots/system-wallpaper-dark.png) | ![A collection of colorful system wallpapers](docs/screenshots/collections-dark.png) |

[More screenshots and wallpaper credits](docs/screenshots/README.md),
including the BigLinux gallery and optional Reading section.

## Features

- **Local library:** system wallpapers and folders you choose, with search,
  orientation and resolution filters. Removing a folder never deletes its files.
- **Online wallpapers:** BigLinux, Wallhaven, Bing and Wikimedia Commons,
  with source information and available credits and licenses.
- **Preview and apply:** full-resolution and full-screen views; apply to
  GNOME's light style, dark style or both, with undo.
- **Organize and rotate:** favorites, collections and automatic wallpaper
  changes through GNOME, which continue after AshyWall closes.
- **Optional Reading:** enable it in Preferences → Reading. Add websites
  or RSS/Atom feeds, organize sources by category and read cached summaries
  offline. The **Sources** button lets you change categories or remove a
  source with confirmation. Full articles open in your browser; sources
  update only when added or refreshed.

See the [feature guide](docs/FEATURES.md) for source behavior, resolution
criteria, cache limits, security measures and known limitations. Applying
wallpapers currently targets GNOME; per-monitor wallpapers and applying
through the Flatpak wallpaper portal are not supported.

## Run from source

Requires Python ≥ 3.12, PyGObject, GTK ≥ 4.16, libadwaita ≥ 1.6,
Pillow ≥ 11.3, defusedxml ≥ 0.7.1 and `gsettings-desktop-schemas`.

```sh
./scripts/run-dev.sh
```

## Build and install on Arch / BigCommunity

With the dependencies installed and source changes committed, close
AshyWall and run:

```sh
./scripts/build-local-package.sh -si
```

The helper builds the current Git snapshot, runs the tests and installs
through pacman. Omit `-si` to build only; packages stay in `dist/packages/`.
Publishing source and promoting packages to testing or stable are separate
steps. See [development, validation and GitRepo packaging](CONTRIBUTING.md).

## Contribute

- [Development and testing](CONTRIBUTING.md)
- [Translations: 29-language release target](locale/README.md)
- [Feature guide and limitations](docs/FEATURES.md)
- [Changelog](CHANGELOG.md)

## License

[MIT](LICENSE). Wallpaper rights remain with their respective sources;
AshyWall displays the credits and licenses those sources provide.

The provisional owl icon is adapted from
[AshyTerm](https://github.com/big-comm/ashyterm) (MIT, © 2024 Community Big).
Its license is installed as
`/usr/share/licenses/ashywall/LICENSE.ashyterm-icon`.
