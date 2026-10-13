from pathlib import Path

import pytest
from PIL import Image

from ashywall.services.database import connect


def make_image(
    path: Path,
    size: tuple[int, int],
    color: tuple[int, int, int] = (40, 90, 160),
    **save_args,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, **save_args)
    return path


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "state" / "library.sqlite3")
    yield connection
    connection.close()


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    """An isolated XDG layout with empty system, user and download folders."""
    paths = {
        "data_dir": tmp_path / "usr-share",
        "user_data_home": tmp_path / "home" / ".local" / "share",
        "downloads": tmp_path / "home" / ".local" / "share" / "ashywall" / "downloads",
        "pictures": tmp_path / "home" / "Pictures",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths
