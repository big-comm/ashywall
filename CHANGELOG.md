# Changelog

## Unreleased

- Optional Reading section for RSS 2 and Atom, enabled in Preferences.
  Native GNOME navigation, topic filters, search, read/unread state and
  bounded offline caching. Only explicit actions contact sources; article
  pages, scripts, images and tracking resources are never fetched by the
  reader. Source updates run independently of cached searches.
- Feed XML rejects DTDs and entities. Feed connections require public
  HTTPS destinations and keep the same-origin redirect policy.
- Reading follows the system theme and locale, with Brazilian Portuguese
  translations; wallpaper controls and the GNOME desktop backend are retained.
- Feed dates outside the local calendar's range show the unknown-date label
  instead of interrupting article rows.

- HTTPS redirects keep the original host and port on direct and proxy
  connections; unsafe or malformed URLs are rejected before being fetched.
- Invalid links in provider responses are handled as provider errors or
  omitted from display, rather than leaking URL parser exceptions.
- Concurrent API requests reserve separate future rate-limit windows;
  already cancelled requests never open a connection.

Speed that does not depend on the size of the library.

- The gallery reads only the rows on screen; searches and section changes
  run off the main loop and drop the queries they outdate.
- Sidebar counts are recomputed only when the library changes, in a few
  passes over a new index.
- Large batches of new images are probed in worker processes; folder scans
  read directory entries instead of checking each file.
- Online sources reuse their HTTPS connections and cache name lookups,
  asking for IPv4 and IPv6 addresses separately (some resolvers stall
  for seconds on simultaneous lookups). Online thumbnails load in their
  own, wider pool. The first thumbnails of a source appear in about 1 to
  1.5 s instead of 6 to 7 s on such a network.
- System wallpapers installed by a package show the license that package
  declares, with a link to the license text it installed.
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
