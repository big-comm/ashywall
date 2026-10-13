"""Safe image opening: allowed formats, size limits and header-only probing."""

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

# Decoders AshyWall accepts. Anything else is rejected before decoding.
ALLOWED_FORMATS: tuple[str, ...] = ("JPEG", "PNG", "WEBP", "AVIF", "TIFF", "BMP")
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".avif", ".tif", ".tiff", ".bmp"}
)

MAX_FILE_SIZE = 512 * 1024 * 1024
# 16384 x 16384. Checked against the header before any pixel is decoded.
MAX_PIXELS = 16384 * 16384

# Pillow's own check only warns below 2x its limit; ours (above) is strict and
# runs on the header, so Pillow's global check is redundant.
Image.MAX_IMAGE_PIXELS = None

EXIF_ORIENTATION_TAG = 0x0112
# EXIF orientations 5-8 rotate the image by 90 degrees, swapping its sides.
_SWAPPING_ORIENTATIONS = frozenset({5, 6, 7, 8})
_TRANSPOSE_BY_ORIENTATION = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


class ImageProbeError(Exception):
    """The file is not an image AshyWall can read safely."""


@dataclass(frozen=True, slots=True)
class ImageInfo:
    width: int
    height: int
    image_format: str


def has_image_extension(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def open_image(path: Path) -> Image.Image:
    """Open lazily (header only) after validating size, format and pixel count."""
    try:
        file_size = path.stat().st_size
    except OSError as error:
        raise ImageProbeError(str(error)) from error
    if file_size > MAX_FILE_SIZE:
        raise ImageProbeError(f"File too large ({file_size} bytes)")
    try:
        image = Image.open(path, formats=ALLOWED_FORMATS)
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        raise ImageProbeError(f"Unreadable image: {error}") from error
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        image.close()
        raise ImageProbeError(f"Unsupported image size {width}x{height}")
    return image


def exif_orientation(image: Image.Image) -> int:
    try:
        return int(image.getexif().get(EXIF_ORIENTATION_TAG, 1))
    except Exception:  # Broken EXIF must not make a valid image unreadable.
        return 1


def apply_orientation(image: Image.Image, orientation: int) -> Image.Image:
    operation = _TRANSPOSE_BY_ORIENTATION.get(orientation)
    return image.transpose(operation) if operation is not None else image


def probe_image(path: Path) -> ImageInfo:
    """Read dimensions as displayed (EXIF rotation applied) without decoding pixels."""
    with open_image(path) as image:
        width, height = image.size
        if exif_orientation(image) in _SWAPPING_ORIENTATIONS:
            width, height = height, width
        return ImageInfo(width=width, height=height, image_format=image.format or "")
