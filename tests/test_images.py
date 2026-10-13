import io
import os
from pathlib import Path

import pytest
from conftest import make_image
from PIL import Image

from ashywall.services import image_probe
from ashywall.services.image_probe import ImageProbeError, probe_image
from ashywall.services.thumbnails import THUMBNAIL_SIZE, ThumbnailCache, render_preview


@pytest.mark.parametrize("extension", ["png", "jpg", "webp", "avif"])
def test_probe_reads_real_dimensions(tmp_path: Path, extension: str) -> None:
    path = make_image(tmp_path / f"wide.{extension}", (320, 180))
    info = probe_image(path)
    assert (info.width, info.height) == (320, 180)


def test_probe_applies_exif_rotation(tmp_path: Path) -> None:
    exif = Image.Exif()
    exif[image_probe.EXIF_ORIENTATION_TAG] = 6  # Rotated 90 degrees.
    path = make_image(tmp_path / "rotated.jpg", (300, 100), exif=exif)
    info = probe_image(path)
    assert (info.width, info.height) == (100, 300)


def test_file_name_does_not_decide_format(tmp_path: Path) -> None:
    path = make_image(tmp_path / "actually-png.jpg", (16, 9), format="PNG")
    assert probe_image(path).image_format == "PNG"


def test_corrupted_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0 definitely not a jpeg" * 10)
    with pytest.raises(ImageProbeError):
        probe_image(path)


def test_disallowed_format_is_rejected(tmp_path: Path) -> None:
    path = make_image(tmp_path / "anim.gif", (16, 16), format="GIF")
    with pytest.raises(ImageProbeError):
        probe_image(path)


def test_pixel_limit_is_checked_before_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_image(tmp_path / "big.png", (200, 200))
    monkeypatch.setattr(image_probe, "MAX_PIXELS", 199 * 199)
    with pytest.raises(ImageProbeError, match="Unsupported image size"):
        probe_image(path)


def test_truncated_image_fails_at_decode(tmp_path: Path) -> None:
    source = make_image(tmp_path / "full.png", (400, 300))
    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(source.read_bytes()[:200])
    cache = ThumbnailCache(tmp_path / "thumbs")
    with pytest.raises(ImageProbeError):
        cache.ensure(truncated, truncated.stat().st_size, truncated.stat().st_mtime_ns)
    assert not any((tmp_path / "thumbs").rglob("*"))  # No partial files left behind.


def test_thumbnail_is_cached_and_bounded(tmp_path: Path) -> None:
    source = make_image(tmp_path / "huge.png", (2400, 1200))
    cache = ThumbnailCache(tmp_path / "thumbs")
    stat = source.stat()
    first = cache.ensure(source, stat.st_size, stat.st_mtime_ns)
    with Image.open(first) as thumb:
        assert max(thumb.size) == THUMBNAIL_SIZE
        assert thumb.size == (THUMBNAIL_SIZE, THUMBNAIL_SIZE // 2)
    first_mtime = first.stat().st_mtime_ns
    assert cache.ensure(source, stat.st_size, stat.st_mtime_ns) == first
    assert first.stat().st_mtime_ns >= first_mtime
    # A modified source gets a different cache entry.
    assert cache.path_for(source, stat.st_size, stat.st_mtime_ns + 1) != first


def test_transparent_images_are_flattened(tmp_path: Path) -> None:
    path = tmp_path / "alpha.png"
    Image.new("RGBA", (40, 20), (255, 0, 0, 0)).save(path)
    with Image.open(io.BytesIO(render_preview(path))) as preview:
        assert preview.mode == "RGB"


def test_prune_removes_least_recently_used(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path / "thumbs", max_bytes=10_000)
    paths = []
    for index in range(5):
        path = cache.root / "aa" / f"{index}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 4000)
        os.utime(path, ns=(index * 10**9, index * 10**9))
        paths.append(path)
    removed = cache.prune()
    assert removed == 3
    assert [path.exists() for path in paths] == [False, False, False, True, True]
