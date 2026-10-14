"""Which distribution package installed a system file, and the license it declares (pacman only)."""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

LICENSES_DIR = Path("/usr/share/licenses")
# Arch package names; anything else is never used to build a path.
PACKAGE_NAME = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]*$")


@dataclass(frozen=True, slots=True)
class PackageInfo:
    name: str
    # As declared in the package metadata (pacman's "Licenses" field).
    licenses: tuple[str, ...]
    # License texts the package installed under /usr/share/licenses/<name>.
    license_files: tuple[Path, ...]


def _pacman(*args: str) -> str | None:
    pacman = shutil.which("pacman")
    if pacman is None:
        return None
    try:
        result = subprocess.run(
            [pacman, *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            # Field names are translated otherwise.
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def owning_package(path: Path) -> str | None:
    """Return the package name, or None when unknown. Blocking: call off the main thread."""
    if not str(path).startswith("/usr/"):
        return None
    output = _pacman("-Qqo", "--", str(path))
    names = output.strip().splitlines() if output else []
    return names[0] if names and PACKAGE_NAME.match(names[0]) else None


def parse_licenses(info: str) -> tuple[str, ...]:
    """The "Licenses" field of ``pacman -Qi`` output; pacman separates values by two spaces."""
    for line in info.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "Licenses":
            values = tuple(v.strip() for v in value.split("  ") if v.strip())
            return () if values == ("None",) else values
    return ()


def package_info(path: Path) -> PackageInfo | None:
    """Package, declared licenses and installed license texts for ``path``. Blocking."""
    name = owning_package(path)
    if name is None:
        return None
    info = _pacman("-Qi", "--", name)
    folder = LICENSES_DIR / name
    files = (
        tuple(sorted(p for p in folder.iterdir() if p.is_file()))
        if folder.is_dir()
        else ()
    )
    return PackageInfo(name, parse_licenses(info) if info else (), files)
