"""Server-side image thumbnail helpers (routers/files.py).

Covers the `w` width whitelist, raster resize + on-disk sidecar caching with the
correct media type, and the pass-through rules (SVG, animated GIF, non-raster,
oversized decompression bombs) that must serve the ORIGINAL untouched.

Pure-logic tests: the thumbnail helpers take a lightweight file stub + a real
on-disk path and never touch the DB.
"""

import io
import os

import pytest

from open_webui.routers.files import (
    THUMBNAIL_WIDTHS,
    _generate_thumbnail,
    _resized_image_for,
    _thumbnail_sidecar_path,
)

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


class _FileStub:
    """Minimal stand-in for FileModel — only `.meta` is read by the helpers."""

    def __init__(self, content_type):
        self.meta = {"content_type": content_type}


def _write_png(path, size=(4000, 3000)):
    img = Image.new("RGB", size, (123, 200, 50))
    img.save(path, format="PNG")


def test_width_whitelist_matches_client_contract():
    assert THUMBNAIL_WIDTHS == {256, 768, 1024, 2048}


def test_non_whitelisted_width_serves_original(tmp_path):
    src = tmp_path / "photo.png"
    _write_png(src)
    file = _FileStub("image/png")
    # None, 0, and any off-list value must fall through to the original.
    for w in (None, 0, 500, 4096, -256):
        assert _resized_image_for(file, src, w) is None


def test_whitelisted_width_generates_downscaled_sidecar(tmp_path):
    src = tmp_path / "photo.png"
    _write_png(src, size=(4000, 3000))
    file = _FileStub("image/png")

    result = _resized_image_for(file, src, 768)
    assert result is not None
    out_path, media_type = result
    assert os.path.isfile(out_path)
    assert media_type in ("image/webp", "image/jpeg")

    # Longest edge fits within the requested width.
    with Image.open(out_path) as im:
        assert max(im.size) <= 768

    # Sidecar is cached next to the original and reused (no regeneration).
    with Image.open(out_path) as im:
        pass
    result2 = _resized_image_for(file, src, 768)
    assert result2 is not None
    assert str(result2[0]) == str(out_path)


def test_sidecar_path_is_stable_and_keyed_by_width():
    from pathlib import Path

    base = Path("/data/uploads/abc123_photo.png")
    p256 = _thumbnail_sidecar_path(base, 256, "webp")
    p768 = _thumbnail_sidecar_path(base, 768, "webp")
    assert p256 != p768
    assert p256.name.endswith(".w256.webp")
    assert "abc123_photo.png" in p256.name


def test_svg_passes_through_untouched(tmp_path):
    src = tmp_path / "vector.svg"
    src.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    file = _FileStub("image/svg+xml")
    assert _resized_image_for(file, src, 768) is None


def test_non_raster_content_type_passes_through(tmp_path):
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4\n")
    file = _FileStub("application/pdf")
    assert _resized_image_for(file, src, 768) is None


def test_animated_gif_passes_through(tmp_path):
    from PIL import ImageDraw

    src = tmp_path / "anim.gif"
    frames = []
    for i in range(3):
        im = Image.new("RGB", (64, 64), (255, 255, 255))
        ImageDraw.Draw(im).rectangle(
            [i * 10, i * 10, i * 10 + 20, i * 10 + 20], fill=(i * 80, 10, 10)
        )
        frames.append(im)
    frames[0].save(
        src,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    # _generate_thumbnail must refuse to rasterize an animated image.
    assert _generate_thumbnail(src, 256) is None


def test_static_gif_can_thumbnail(tmp_path):
    src = tmp_path / "static.gif"
    Image.new("P", (2000, 1000), 5).save(src, format="GIF")
    result = _generate_thumbnail(src, 256)
    # Single-frame GIF is safe to resize.
    assert result is not None
    out_path, media_type = result
    with Image.open(out_path) as im:
        assert max(im.size) <= 256


def test_decode_error_passes_through(tmp_path):
    src = tmp_path / "broken.png"
    src.write_bytes(b"not a real image")
    file = _FileStub("image/png")
    # Corrupt bytes must not raise — serve the original instead.
    assert _resized_image_for(file, src, 768) is None


def test_decompression_bomb_passes_through(tmp_path, monkeypatch):
    # Force Pillow to treat a modest image as a bomb, exercising the
    # DecompressionBombError → pass-through path deterministically.
    src = tmp_path / "big.png"
    _write_png(src, size=(2000, 2000))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10, raising=False)
    assert _generate_thumbnail(src, 768) is None
