"""Automatic wallpaper change through GNOME's own slideshow format.

GNOME Shell accepts an XML "slideshow" as the wallpaper: a start time plus a
list of pictures, each shown for a duration and followed by a cross-fade. The
shell itself switches the picture, so nothing has to keep running after
AshyWall writes the file. Because the schedule is deterministic, AshyWall can
also tell which picture is on screen at any moment.

The file is rewritten under a new name for every change (GNOME reloads when
the picture URI changes) and only picture files that exist are listed.
"""

import json
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from ..desktop.gnome import ApplyMode, DesktopError, GnomeBackground
from ..models.resolution import Orientation, orientation_of, tier_by_key
from ..models.wallpaper import Wallpaper
from .library_repository import LibraryQuery, LibraryRepository, Section

SETTING_KEY = "rotation"
INTERVALS: tuple[int, ...] = (
    300,
    900,
    1800,
    3600,
    3 * 3600,
    6 * 3600,
    12 * 3600,
    86400,
)
DEFAULT_INTERVAL = 3600
TRANSITION_SECONDS = 2.0
MAX_SLIDES = 1000
FILE_PREFIX = "rotation-"


class RotationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RotationConfig:
    # "all", "system", "folders", "downloads", "favorites" or "collection:<id>".
    source: str = Section.ALL.value
    interval: int = DEFAULT_INTERVAL
    shuffle: bool = True
    min_tier: str | None = None
    skip_portrait: bool = True

    def __post_init__(self) -> None:
        if self.interval not in INTERVALS:
            raise RotationError(f"Unsupported interval {self.interval}")
        if self.min_tier is not None and tier_by_key(self.min_tier) is None:
            raise RotationError(f"Unknown resolution {self.min_tier!r}")
        parse_source(self.source)


@dataclass(frozen=True, slots=True)
class RotationState:
    """What was written: kept to know the picture on screen and to update the list."""

    config: RotationConfig
    paths: tuple[str, ...]
    start: float
    file: str

    def to_json(self) -> str:
        data = asdict(self)
        data["paths"] = list(self.paths)
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "RotationState":
        try:
            data = json.loads(text)
            config = RotationConfig(**data["config"])
            paths = tuple(str(path) for path in data["paths"])
            return cls(config, paths, float(data["start"]), str(data["file"]))
        except (ValueError, TypeError, KeyError) as error:
            raise RotationError(f"Invalid saved rotation: {error}") from error


def parse_source(source: str) -> tuple[Section, int | None]:
    if source.startswith("collection:"):
        identifier = source.removeprefix("collection:")
        if not identifier.isdigit():
            raise RotationError(f"Invalid collection {source!r}")
        return Section.COLLECTION, int(identifier)
    try:
        section = Section(source)
    except ValueError as error:
        raise RotationError(f"Unknown source {source!r}") from error
    if section is Section.COLLECTION:
        raise RotationError("A collection source needs an id")
    return section, None


def eligible(width: int, height: int, config: RotationConfig) -> bool:
    if config.skip_portrait and orientation_of(width, height) is Orientation.PORTRAIT:
        return False
    tier = tier_by_key(config.min_tier) if config.min_tier else None
    return tier is None or tier.accepts(width, height)


def select_paths(repository: LibraryRepository, config: RotationConfig) -> list[Path]:
    """Existing local files for ``config``, deduplicated, in library order."""
    section, collection_id = parse_source(config.source)
    wallpapers: Iterable[Wallpaper] = repository.list_wallpapers(
        LibraryQuery(section=section, collection_id=collection_id)
    )
    candidates = [(w.path, w.width, w.height) for w in wallpapers]
    # Online favorites and collection items count once they are downloaded.
    if section is Section.FAVORITES:
        remote = repository.remote_favorites()
    elif section is Section.COLLECTION and collection_id is not None:
        remote = repository.remote_collection_items(collection_id)
    else:
        remote = []
    downloads: dict[str, dict[str, Path]] = {}
    for item in remote:
        if item.origin not in downloads:
            downloads[item.origin] = repository.downloaded_paths(item.origin)
        path = downloads[item.origin].get(item.remote_id)
        if path is not None:
            candidates.append((path, item.width, item.height))
    seen: set[Path] = set()
    selected = []
    for path, width, height in candidates:
        if path in seen or not eligible(width, height, config) or not path.is_file():
            continue
        seen.add(path)
        selected.append(path)
    return selected


def order_paths(
    paths: list[Path], shuffle: bool, rng: random.Random | None = None
) -> list[Path]:
    ordered = list(paths)
    if shuffle:
        (rng or random.Random()).shuffle(ordered)
    return ordered[:MAX_SLIDES]


def slideshow_xml(
    paths: list[Path],
    interval: int,
    start: float,
    transition: float = TRANSITION_SECONDS,
) -> str:
    """GNOME slideshow: each picture stays ``interval`` seconds including its fade."""
    if not paths:
        raise RotationError("No pictures to show")
    moment = datetime.fromtimestamp(start)  # GNOME reads the start as local time.
    lines = [
        "<background>",
        "  <starttime>",
        f"    <year>{moment.year}</year>",
        f"    <month>{moment.month}</month>",
        f"    <day>{moment.day}</day>",
        f"    <hour>{moment.hour}</hour>",
        f"    <minute>{moment.minute}</minute>",
        f"    <second>{moment.second}</second>",
        "  </starttime>",
    ]
    names = [escape(str(path)) for path in paths]
    if len(names) == 1:
        lines += [
            "  <static>",
            f"    <duration>{float(interval)}</duration>",
            f"    <file>{names[0]}</file>",
            "  </static>",
        ]
    else:
        for index, name in enumerate(names):
            following = names[(index + 1) % len(names)]
            lines += [
                "  <static>",
                f"    <duration>{float(interval) - transition}</duration>",
                f"    <file>{name}</file>",
                "  </static>",
                '  <transition type="overlay">',
                f"    <duration>{transition}</duration>",
                f"    <from>{name}</from>",
                f"    <to>{following}</to>",
                "  </transition>",
            ]
    lines.append("</background>")
    return "\n".join(lines) + "\n"


def slide_index(count: int, interval: int, start: float, now: float) -> int:
    """Index of the picture on screen (the outgoing one during a fade)."""
    if count <= 0:
        raise RotationError("No pictures to show")
    whole_start = int(start)  # The XML keeps whole seconds.
    elapsed = max(0.0, now - whole_start)
    return int(elapsed // interval) % count


class RotationManager:
    def __init__(
        self,
        repository: LibraryRepository,
        desktop: GnomeBackground | None,
        directory: Callable[[], Path],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._desktop = desktop
        self._directory = directory
        self._clock = clock

    # State -----------------------------------------------------------------

    def saved_config(self) -> RotationConfig:
        state = self._saved_state()
        if state is not None:
            return state.config
        text = self._repository.get_setting(f"{SETTING_KEY}.config")
        if text:
            try:
                return RotationConfig(**json.loads(text))
            except (ValueError, TypeError, RotationError):
                pass
        return RotationConfig()

    def _saved_state(self) -> RotationState | None:
        text = self._repository.get_setting(SETTING_KEY)
        if not text:
            return None
        try:
            return RotationState.from_json(text)
        except RotationError:
            return None

    def active_state(self) -> RotationState | None:
        """The saved rotation, if GNOME is still showing it."""
        state = self._saved_state()
        if state is None or self._desktop is None:
            return None
        current = self._desktop.read_state().values
        uri = Path(state.file).as_uri()
        if uri not in (current.get("picture-uri"), current.get("picture-uri-dark")):
            return None
        return state

    def current_path(self, state: RotationState) -> Path:
        index = slide_index(
            len(state.paths), state.config.interval, state.start, self._clock()
        )
        return Path(state.paths[index])

    def candidates(self, config: RotationConfig) -> list[Path]:
        return select_paths(self._repository, config)

    # Changes ---------------------------------------------------------------

    def start(self, config: RotationConfig) -> tuple[RotationState, str | None]:
        """Write and apply a slideshow; returns the state and the replaced desktop state."""
        paths = order_paths(self.candidates(config), config.shuffle)
        if not paths:
            raise RotationError("No wallpapers match these settings")
        previous_active = self.active_state()
        state, replaced = self._write_and_apply(config, paths)
        # Only the first start remembers what was on screen before the rotation.
        return state, None if previous_active is not None else replaced

    def next(self) -> RotationState:
        state = self.active_state()
        if state is None:
            raise RotationError("Automatic change is not active")
        paths = [Path(path) for path in state.paths]
        index = slide_index(
            len(paths), state.config.interval, state.start, self._clock()
        )
        following = (index + 1) % len(paths)
        state, _replaced = self._write_and_apply(
            state.config, paths[following:] + paths[:following]
        )
        return state

    def refresh(self) -> RotationState | None:
        """Update the list after library changes without changing the picture on screen.

        Returns the new state, or None when the rotation stopped because nothing
        matches any more or GNOME shows something else.
        """
        state = self.active_state()
        if state is None:
            self._forget()
            return None
        available = self.candidates(state.config)
        if not available:
            self.stop()
            return None
        if {str(path) for path in available} == set(state.paths) and all(
            Path(path).is_file() for path in state.paths
        ):
            return state
        current = self.current_path(state)
        paths = order_paths(available, state.config.shuffle)
        if current in paths:
            paths.remove(current)
            paths.insert(0, current)
        new_state, _replaced = self._write_and_apply(state.config, paths[:MAX_SLIDES])
        return new_state

    def stop(self) -> Path | None:
        """Keep the picture on screen as a fixed wallpaper and end the rotation."""
        state = self.active_state()
        kept = None
        if state is not None and self._desktop is not None:
            current = self.current_path(state)
            if current.is_file():
                self._desktop.apply(current, mode=ApplyMode.BOTH)
                kept = current
        self._forget()
        return kept

    def save_config(self, config: RotationConfig) -> None:
        self._repository.set_setting(
            f"{SETTING_KEY}.config", json.dumps(asdict(config))
        )

    def _forget(self) -> None:
        self._repository.set_setting(SETTING_KEY, None)
        self._remove_files(keep=None)

    def _write_and_apply(
        self, config: RotationConfig, paths: list[Path]
    ) -> tuple[RotationState, str]:
        if self._desktop is None:
            raise RotationError("Changing the wallpaper is not available")
        start = float(int(self._clock()))
        directory = self._directory()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{FILE_PREFIX}{int(start)}.xml"
        if target.exists():
            target = directory / f"{FILE_PREFIX}{int(start)}-{len(paths)}.xml"
        temporary = target.with_name(f".{target.name}.part")
        temporary.write_text(
            slideshow_xml(paths, config.interval, start), encoding="utf-8"
        )
        temporary.replace(target)
        try:
            previous = self._desktop.apply(target, mode=ApplyMode.BOTH)
        except DesktopError:
            target.unlink(missing_ok=True)
            raise
        state = RotationState(
            config, tuple(str(path) for path in paths), start, str(target)
        )
        self._repository.set_setting(SETTING_KEY, state.to_json())
        self.save_config(config)
        self._remove_files(keep=target)
        return state, previous.to_json()

    def _remove_files(self, keep: Path | None) -> None:
        directory = self._directory()
        if not directory.is_dir():
            return
        for path in directory.glob(f"{FILE_PREFIX}*.xml"):
            if path != keep:
                path.unlink(missing_ok=True)
