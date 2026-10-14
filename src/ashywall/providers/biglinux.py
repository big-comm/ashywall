"""BigLinux wallpapers, read from the BigLinux packages on GitHub.

The remade BigLinux wallpapers live in two package repositories,
biglinux-wallpapers-base and extra-biglinux-wallpapers. They are AVIF files
(named .heic) with no manifest or thumbnails, so AshyWall lists each
repository's file tree through the GitHub API, reads the first bytes of each
file to learn its real dimensions, and keeps both in a small cache: the list
for a day, the dimensions for as long as the file does not change.

Both packages declare the GPL license in their PKGBUILD; AshyWall shows that
and links to where it is declared.
"""

import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..services.http import HttpClient, HttpError, SlidingWindowLimiter
from ..services.image_probe import MAX_PIXELS
from .base import (
    ProviderError,
    RemotePage,
    RemoteQuery,
    RemoteWallpaper,
    query_accepts,
    validated_url,
)

OWNER = "biglinux"
REPOSITORIES: tuple[str, ...] = (
    "biglinux-wallpapers-base",
    "extra-biglinux-wallpapers",
)
BRANCH = "main"
API_URL = "https://api.github.com"
API_HOSTS = frozenset({"api.github.com"})
FILE_HOSTS = frozenset({"raw.githubusercontent.com"})
WALLPAPER_DIR = "usr/share/wallpapers/"
EXTENSIONS = frozenset({".heic", ".heif", ".avif", ".jpg", ".jpeg", ".png", ".webp"})
PAGE_SIZE = 24
PROBE_BYTES = 4096
INDEX_MAX_AGE = 24 * 3600
MAX_FILE_BYTES = 64 * 1024 * 1024
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
LICENSE = "GPL"


@dataclass(frozen=True, slots=True)
class WallpaperFile:
    repository: str
    commit: str
    path: str
    size: int
    sha: str

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


def title_of(name: str) -> str:
    stem = name.rsplit(".", 1)[0].strip()
    return stem[:1].upper() + stem[1:] if stem else name


def slug_of(name: str) -> str:
    stem = name.rsplit(".", 1)[0].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    return slug[:60] or "wallpaper"


def isobmff_dimensions(data: bytes) -> tuple[int, int]:
    """Width and height from the 'ispe' box of an AVIF/HEIF header."""
    index = data.find(b"ispe")
    if index < 4 or len(data) < index + 16:
        raise ProviderError("No image size in the file header")
    width = int.from_bytes(data[index + 8 : index + 12], "big")
    height = int.from_bytes(data[index + 12 : index + 16], "big")
    if not 0 < width * height <= MAX_PIXELS:
        raise ProviderError(f"Unexpected size {width}x{height}")
    # 'irot' with an odd quarter turn swaps the displayed sides.
    rotation = data.find(b"irot")
    if (
        rotation >= 4
        and len(data) > rotation + 4
        and data[rotation + 4] & 0b11 in (1, 3)
    ):
        width, height = height, width
    return width, height


def parse_tree(payload: Any, repository: str, commit: str) -> list[WallpaperFile]:
    if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
        raise ProviderError("Unexpected repository listing")
    files = []
    for entry in payload["tree"]:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path, size, sha = entry.get("path"), entry.get("size"), entry.get("sha")
        if (
            not isinstance(path, str)
            or not path.startswith(WALLPAPER_DIR)
            or "/" in path[len(WALLPAPER_DIR) :]
            or os.path.splitext(path)[1].lower() not in EXTENSIONS
            or not isinstance(size, int)
            or not 0 < size <= MAX_FILE_BYTES
            or not isinstance(sha, str)
            or not SHA_PATTERN.match(sha)
        ):
            continue
        files.append(WallpaperFile(repository, commit, path, size, sha))
    return files


def raw_url(file: WallpaperFile) -> str:
    quoted = urllib.parse.quote(file.path)
    return validated_url(
        f"https://raw.githubusercontent.com/{OWNER}/{file.repository}/{file.commit}/{quoted}",
        FILE_HOSTS,
    )


class BigLinuxProvider:
    id = "biglinux"
    display_name = "BigLinux"
    icon_name = "image-x-generic-symbolic"
    download_hosts = FILE_HOSTS
    thumbnail_hosts = FILE_HOSTS
    # Names are searched locally; the collection is small.
    searchable = True
    sorts = ()
    has_details = False
    auto_original = True

    def __init__(
        self,
        cache_file: Path | None = None,
        http: HttpClient | None = None,
        clock=time.time,
    ) -> None:
        self._cache_file = cache_file
        # GitHub allows 60 unauthenticated API requests per hour.
        self._api = http or HttpClient(limiter=SlidingWindowLimiter(30))
        self._files_http = http or HttpClient()
        self._clock = clock
        self._lock = threading.Lock()
        self._files: list[WallpaperFile] | None = None
        self._fetched_at = 0.0
        self._sizes: dict[str, tuple[int, int]] = {}
        self._load_cache()

    # Cache -----------------------------------------------------------------

    def _load_cache(self) -> None:
        if self._cache_file is None or not self._cache_file.is_file():
            return
        try:
            data = json.loads(self._cache_file.read_text())
            self._fetched_at = float(data["fetched_at"])
            self._files = [WallpaperFile(**entry) for entry in data["files"]]
            self._sizes = {
                sha: (int(size[0]), int(size[1]))
                for sha, size in data["sizes"].items()
                if SHA_PATTERN.match(sha)
            }
        except (OSError, ValueError, KeyError, TypeError):
            self._files, self._fetched_at, self._sizes = None, 0.0, {}

    def _save_cache(self) -> None:
        if self._cache_file is None or self._files is None:
            return
        data = {
            "fetched_at": self._fetched_at,
            "files": [_as_dict(file) for file in self._files],
            "sizes": {sha: list(size) for sha, size in self._sizes.items()},
        }
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self._cache_file.parent, prefix=".", suffix=".part"
        )
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(data, handle)
            os.replace(temporary, self._cache_file)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    # Listing ---------------------------------------------------------------

    def _list_files(self, cancel: threading.Event | None) -> list[WallpaperFile]:
        if self._files is not None and self._clock() - self._fetched_at < INDEX_MAX_AGE:
            return self._files
        try:
            files: list[WallpaperFile] = []
            for repository in REPOSITORIES:
                branch = self._api.get_json(
                    validated_url(
                        f"{API_URL}/repos/{OWNER}/{repository}/branches/{BRANCH}",
                        API_HOSTS,
                    ),
                    cancel=cancel,
                )
                commit = (
                    branch.get("commit", {}).get("sha")
                    if isinstance(branch, dict)
                    else None
                )
                if not isinstance(commit, str) or not SHA_PATTERN.match(commit):
                    raise ProviderError("Unexpected branch information")
                tree = self._api.get_json(
                    validated_url(
                        f"{API_URL}/repos/{OWNER}/{repository}/git/trees/{commit}",
                        API_HOSTS,
                    ),
                    params={"recursive": "1"},
                    max_bytes=8 * 1024 * 1024,
                    cancel=cancel,
                )
                files.extend(parse_tree(tree, repository, commit))
        except (HttpError, ProviderError):
            if self._files is not None:
                return self._files  # A stale list is better than none.
            raise
        files.sort(key=lambda file: (file.name.lower(), file.repository))
        with self._lock:
            self._files = files
            self._fetched_at = self._clock()
            known = {file.sha for file in files}
            self._sizes = {
                sha: size for sha, size in self._sizes.items() if sha in known
            }
        self._save_cache()
        return files

    def _size_of(
        self, file: WallpaperFile, cancel: threading.Event | None
    ) -> tuple[int, int] | None:
        with self._lock:
            if file.sha in self._sizes:
                return self._sizes[file.sha]
        try:
            prefix, _total = self._files_http.get_prefix(
                raw_url(file), PROBE_BYTES, cancel=cancel
            )
            size = isobmff_dimensions(prefix)
        except (HttpError, ProviderError):
            return None
        with self._lock:
            self._sizes[file.sha] = size
        return size

    def _item(
        self, file: WallpaperFile, size: tuple[int, int], remote_id: str
    ) -> RemoteWallpaper:
        url = raw_url(file)
        return RemoteWallpaper(
            origin=self.id,
            remote_id=remote_id,
            title=title_of(file.name),
            width=size[0],
            height=size[1],
            file_size=file.size,
            media_type="image/avif",
            thumbnail_url=url,
            preview_url=url,
            original_url=url,
            page_url=f"https://github.com/{OWNER}/{file.repository}/blob/{BRANCH}/"
            + urllib.parse.quote(file.path),
            license=LICENSE,
            license_url=f"https://github.com/{OWNER}/{file.repository}/blob/{BRANCH}/pkgbuild/PKGBUILD",
            description=file.repository,
        )

    def search(
        self, query: RemoteQuery, cancel: threading.Event | None = None
    ) -> RemotePage:
        files = self._list_files(cancel)
        identifiers = _identifiers(files)
        text = query.text.strip().lower()
        if text:
            files = [file for file in files if text in file.name.lower()]
        last_page = max(1, -(-len(files) // PAGE_SIZE))
        page = max(1, query.page)
        chunk = files[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        with ThreadPoolExecutor(max_workers=6) as pool:
            sizes = list(pool.map(lambda file: self._size_of(file, cancel), chunk))
        self._save_cache()
        items, skipped = [], 0
        for file, size in zip(chunk, sizes, strict=True):
            if size is None:
                skipped += 1
                continue
            item = self._item(file, size, identifiers[file])
            if query_accepts(query, item):
                items.append(item)
            else:
                skipped += 1
        filtered = query.orientation is not None or query.min_tier is not None
        return RemotePage(
            items=items,
            page=page,
            last_page=last_page,
            total=None if filtered else len(files),
            skipped=skipped,
        )

    def details(
        self, remote_id: str, cancel: threading.Event | None = None
    ) -> RemoteWallpaper:
        raise ProviderError("BigLinux listings are already complete")


def _identifiers(files: list[WallpaperFile]) -> dict[WallpaperFile, str]:
    """Stable ids from file names, unique across both packages."""
    seen: dict[str, int] = {}
    identifiers = {}
    for file in files:
        slug = slug_of(file.name)
        count = seen.get(slug, 0) + 1
        seen[slug] = count
        identifiers[file] = slug if count == 1 else f"{slug}-{count}"
    return identifiers


def _as_dict(file: WallpaperFile) -> dict[str, Any]:
    return {
        "repository": file.repository,
        "commit": file.commit,
        "path": file.path,
        "size": file.size,
        "sha": file.sha,
    }
