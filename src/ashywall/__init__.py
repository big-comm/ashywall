"""AshyWall: wallpaper library for GNOME."""

__version__ = "1.0.0"


def main() -> int:
    from .app import run

    return run()
