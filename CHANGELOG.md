# Changelog

## Unreleased

Speed that does not depend on the size of the library.

- The gallery reads only the rows on screen; searches and section changes
  run off the main loop and drop the queries they outdate.
- Sidebar counts are recomputed only when the library changes, in a few
  passes over a new index.
- Large batches of new images are probed in worker processes; folder scans
  read directory entries instead of checking each file.
- Automatic change filters in SQL, skips sorting when it shuffles, checks
  only the files that go into the slideshow and no longer rewrites a full
  slideshow after every indexing.

## 1.0.0 (2026-10-14)

First release.

- Wallpapers from the BigLinux packages (`biglinux-wallpapers-base` and
  `extra-biglinux-wallpapers`), with their declared license.
- Downloads keep the title their source gave them; paths under the home
  folder are shown as `~/…`.
- AppStream metainfo for software centers.

## 0.5.0

- Automatic change through GNOME's slideshow XML.
- Bing image of the day and Wikimedia Commons picture of the day, with
  author and license where the source gives them.
- Providers are generic in the interface (sort orders, search,
  attribution).

## 0.4.0

- Full-resolution previews of online wallpapers, kept in a pruned cache.
- Full-screen viewer: as wallpaper, whole image, actual size.
- Wallhaven searches sorted by relevance.

## 0.3.0

- User collections mixing local and online wallpapers.

## 0.2.0

- Wallhaven browsing, verified downloads, online favorites.

## 0.1.0

- Local library: system catalogs, personal folders, filters, duplicates,
  favorites, apply and restore on GNOME.
