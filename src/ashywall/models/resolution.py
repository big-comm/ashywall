"""Orientation, aspect ratio and resolution tiers computed from pixel sizes.

Criteria are explicit and orientation-aware: a tier compares the long side
with the long side and the short side with the short side, so a 2160x3840
portrait image is 4K just like its 3840x2160 landscape counterpart. Pixel
counts say nothing about whether an image was captured or upscaled.
"""

from dataclasses import dataclass
from enum import StrEnum
from math import gcd

# Images whose sides differ by at most 5% are treated as square.
SQUARE_TOLERANCE = 1.05

# Relative distance under which a ratio is labelled as "≈ a:b".
APPROXIMATE_RATIO_TOLERANCE = 0.03

COMMON_RATIOS: tuple[tuple[int, int], ...] = (
    (16, 9),
    (16, 10),
    (21, 9),
    (32, 9),
    (4, 3),
    (5, 4),
    (3, 2),
    (1, 1),
)


class Orientation(StrEnum):
    LANDSCAPE = "landscape"
    PORTRAIT = "portrait"
    SQUARE = "square"


def orientation_of(width: int, height: int) -> Orientation:
    if width > height * SQUARE_TOLERANCE:
        return Orientation.LANDSCAPE
    if height > width * SQUARE_TOLERANCE:
        return Orientation.PORTRAIT
    return Orientation.SQUARE


@dataclass(frozen=True, slots=True)
class ResolutionTier:
    key: str
    long_side: int
    short_side: int

    def accepts(self, width: int, height: int) -> bool:
        return (
            max(width, height) >= self.long_side
            and min(width, height) >= self.short_side
        )


FULL_HD = ResolutionTier("fhd", 1920, 1080)
QHD = ResolutionTier("qhd", 2560, 1440)
UHD_4K = ResolutionTier("4k", 3840, 2160)
UHD_8K = ResolutionTier("8k", 7680, 4320)
TIERS: tuple[ResolutionTier, ...] = (FULL_HD, QHD, UHD_4K, UHD_8K)


def tier_by_key(key: str) -> ResolutionTier | None:
    return next((tier for tier in TIERS if tier.key == key), None)


def _oriented_common_ratios() -> list[tuple[int, int]]:
    flipped = [(b, a) for a, b in COMMON_RATIOS if a != b]
    return list(COMMON_RATIOS) + flipped


def aspect_ratio_label(width: int, height: int) -> str:
    """Return "16:9" for exact matches, "≈ 16:9" for near ones, else "2.39:1"."""
    if width <= 0 or height <= 0:
        return "?"
    for a, b in _oriented_common_ratios():
        if width * b == height * a:
            return f"{a}:{b}"
    divisor = gcd(width, height)
    reduced_w, reduced_h = width // divisor, height // divisor
    if reduced_w <= 50 and reduced_h <= 50:
        return f"{reduced_w}:{reduced_h}"
    ratio = width / height
    for a, b in _oriented_common_ratios():
        if abs(ratio - a / b) / (a / b) <= APPROXIMATE_RATIO_TOLERANCE:
            return f"≈ {a}:{b}"
    if ratio >= 1:
        return f"{ratio:.2f}:1"
    return f"1:{1 / ratio:.2f}"
