import pytest

from ashywall.models.resolution import (
    FULL_HD,
    UHD_4K,
    UHD_8K,
    Orientation,
    aspect_ratio_label,
    orientation_of,
)


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (3840, 2160, True),
        (2160, 3840, True),  # Portrait 4K counts too.
        (4096, 2160, True),
        (5120, 1440, False),  # Super-ultrawide: long side is enough, short side is not.
        (3839, 2160, False),
        (3840, 2159, False),
    ],
)
def test_4k_requires_both_sides(width: int, height: int, expected: bool) -> None:
    assert UHD_4K.accepts(width, height) is expected


def test_8k_and_full_hd_thresholds() -> None:
    assert UHD_8K.accepts(7680, 4320)
    assert UHD_8K.accepts(4320, 7680)
    assert not UHD_8K.accepts(7680, 4319)
    assert FULL_HD.accepts(1080, 1920)
    assert not FULL_HD.accepts(1600, 1200)


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (1920, 1080, Orientation.LANDSCAPE),
        (1080, 1920, Orientation.PORTRAIT),
        (2000, 2000, Orientation.SQUARE),
        (2000, 1950, Orientation.SQUARE),
        (2200, 2000, Orientation.LANDSCAPE),
    ],
)
def test_orientation(width: int, height: int, expected: Orientation) -> None:
    assert orientation_of(width, height) is expected


@pytest.mark.parametrize(
    ("width", "height", "label"),
    [
        (3840, 2160, "16:9"),
        (1920, 1200, "16:10"),
        (1080, 1920, "9:16"),
        (1366, 768, "≈ 16:9"),
        (2560, 1080, "≈ 21:9"),
        (5120, 1440, "32:9"),
        (2000, 2000, "1:1"),
        (2400, 1000, "12:5"),
        (3000, 1001, "3.00:1"),
        (0, 100, "?"),
    ],
)
def test_aspect_ratio_label(width: int, height: int, label: str) -> None:
    assert aspect_ratio_label(width, height) == label
