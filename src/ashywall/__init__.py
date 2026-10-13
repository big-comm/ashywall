"""AshyWall: wallpaper library for GNOME."""

__version__ = "0.5.0"


def main() -> int:
    from .app import run

    return run()
