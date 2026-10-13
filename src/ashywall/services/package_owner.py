"""Find which distribution package installed a system file (pacman only)."""

import shutil
import subprocess
from pathlib import Path


def owning_package(path: Path) -> str | None:
    """Return the package name, or None when unknown. Blocking: call off the main thread."""
    pacman = shutil.which("pacman")
    if pacman is None or not str(path).startswith("/usr/"):
        return None
    try:
        result = subprocess.run(
            [pacman, "-Qqo", "--", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = result.stdout.strip().splitlines()
    return name[0] if result.returncode == 0 and name else None
