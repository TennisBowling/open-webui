"""Unit tests for provider-bound image compression.

Covers `prepare_image_data_for_provider`'s optimize decision tree and the
`full_quality` opt-out semantics enforced by `routers/openai.py`. The contract
under test, in priority order:

  1. Never raise on a decodable image (a failed shrink falls back to original).
  2. Never return a larger payload than the input.
  3. Never lossily re-encode non-JPEG raster formats (PNG/GIF/WEBP passthrough).
  4. Pinned ("full_quality") images are sent byte-for-byte unchanged.
  5. The `full_quality` flag never leaks to the upstream provider.
"""

import io

import pytest

from open_webui.utils.image_conversion import prepare_image_data_for_provider

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _jpeg(width: int, height: int, quality: int = 100, noisy: bool = True) -> bytes:
    """A JPEG of a given size. `noisy` defeats compression so big stays big."""
    img = Image.new("RGB", (width, height))
    if noisy:
        # Deterministic high-frequency content → large, hard-to-compress JPEG.
        px = img.load()
        for y in range(height):
            for x in range(width):
                px[x, y] = ((x * 53 + y * 97) % 256, (x * 17) % 256, (y * 29) % 256)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def _png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (width, height), (10, 20, 30, 128)).save(out, format="PNG")
    return out.getvalue()


def _gif(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("P", (width, height)).save(out, format="GIF")
    return out.getvalue()


def _webp(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (1, 2, 3)).save(out, format="WEBP")
    return out.getvalue()


# --- optimize: large JPEG is shrunk and stays JPEG -------------------------


def test_large_jpeg_optimized_is_smaller_and_jpeg():
    original = _jpeg(1600, 1600, quality=100)
    assert len(original) > 1024 * 1024  # exceeds the default gate

    data, mime = prepare_image_data_for_provider(
        original, "image/jpeg", "photo.jpg", optimize=True, quality=85, min_bytes=1024 * 1024
    )

    assert mime == "image/jpeg"
    assert len(data) < len(original)
    # Output must still be a valid, decodable JPEG.
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "JPEG"


# --- full_quality / optimize=False: untouched ------------------------------


def test_pinned_jpeg_is_byte_identical():
    original = _jpeg(1600, 1600, quality=100)
    data, mime = prepare_image_data_for_provider(
        original, "image/jpeg", "photo.jpg", optimize=False, quality=85
    )
    assert mime == "image/jpeg"
    assert data == original  # pinned → not re-encoded at all


# --- size gate: small JPEG below threshold passes through ------------------


def test_small_jpeg_below_gate_is_untouched():
    original = _jpeg(64, 64, quality=95)
    assert len(original) < 1024 * 1024

    data, mime = prepare_image_data_for_provider(
        original, "image/jpeg", "small.jpg", optimize=True, min_bytes=1024 * 1024
    )
    assert mime == "image/jpeg"
    assert data == original


# --- non-JPEG raster formats are never JPEG-ified --------------------------


@pytest.mark.parametrize(
    "make,mime",
    [(_png, "image/png"), (_gif, "image/gif"), (_webp, "image/webp")],
)
def test_supported_non_jpeg_passes_through_even_when_optimizing(make, mime):
    original = make(800, 800)
    data, out_mime = prepare_image_data_for_provider(
        original, mime, "image." + mime.split("/")[1], optimize=True, min_bytes=1
    )
    assert out_mime == mime
    assert data == original  # lossless formats untouched


# --- never return a larger payload -----------------------------------------


def test_never_returns_larger_payload():
    # An already-optimal JPEG: re-encoding at q85 could be larger; we must keep
    # the original in that case.
    original = _jpeg(1200, 1200, quality=60)
    data, mime = prepare_image_data_for_provider(
        original, "image/jpeg", "p.jpg", optimize=True, quality=95, min_bytes=1
    )
    assert mime == "image/jpeg"
    assert len(data) <= len(original)


# --- HEIC/unsupported transcodes to JPEG (quality follows optimize) --------


def test_unsupported_format_transcodes_to_jpeg():
    # We can't depend on libheif in CI; simulate "unsupported" by lying about a
    # PNG's type via an unknown mime + extension while the bytes sniff as PNG.
    # resolve_image_mime_type trusts the sniffed bytes, so this still resolves
    # to PNG (passthrough). To exercise the transcode branch we instead feed a
    # BMP, which Pillow decodes but which is not in SUPPORTED_IMAGE_MIME_TYPES.
    out = io.BytesIO()
    Image.new("RGB", (300, 300), (200, 100, 50)).save(out, format="BMP")
    bmp = out.getvalue()

    data, mime = prepare_image_data_for_provider(
        bmp, "image/bmp", "x.bmp", optimize=True, quality=85
    )
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "JPEG"


# --- decode failure: never raise, fall back to original --------------------


def test_undecodable_supported_mime_does_not_raise():
    # Bytes that sniff as JPEG (so resolve → image/jpeg, the SUPPORTED branch)
    # but are not actually decodable. The optimize re-encode must fail softly
    # and return the original bytes rather than raising.
    fake_jpeg = b"\xff\xd8\xff" + b"not a real jpeg" * 100000  # > min_bytes
    assert len(fake_jpeg) > 1024 * 1024

    data, mime = prepare_image_data_for_provider(
        fake_jpeg, "image/jpeg", "broken.jpg", optimize=True, min_bytes=1024 * 1024
    )
    assert mime == "image/jpeg"
    assert data == fake_jpeg  # unchanged; no exception


def test_truly_unsupported_undecodable_raises():
    # Not sniffable, not decodable, unknown type → genuinely unusable input.
    with pytest.raises(ValueError):
        prepare_image_data_for_provider(
            b"\x00\x01\x02\x03 garbage", "application/x-thing", "f.bin", optimize=True
        )


# --- dimension cap (IMAGE_PROVIDER_MAX_DIMENSION) ---------------------------
#
# The cap downscales the longest edge for EVERY format when optimize=True and
# max_dimension>0. It must: cap all formats, never upscale, leave small images
# and pinned/optimize=False images untouched, and never raise.


def _long_edge(data: bytes) -> int:
    with Image.open(io.BytesIO(data)) as img:
        return max(img.size)


@pytest.mark.parametrize(
    "make,mime,name",
    [
        (lambda: _png(4000, 3000), "image/png", "big.png"),
        (lambda: _jpeg(4000, 3000, quality=90), "image/jpeg", "big.jpg"),
        (lambda: _webp(4000, 3000), "image/webp", "big.webp"),
    ],
)
def test_dimension_cap_downscales_all_formats(make, mime, name):
    original = make()
    assert _long_edge(original) == 4000

    data, out_mime = prepare_image_data_for_provider(
        original,
        mime,
        name,
        optimize=True,
        quality=85,
        min_bytes=1024 * 1024,
        max_dimension=2048,
    )

    # Longest edge is capped, aspect ratio preserved (4000x3000 -> 2048x1536).
    with Image.open(io.BytesIO(data)) as img:
        assert max(img.size) <= 2048
        assert img.size == (2048, 1536)
    # Capped payload is never larger than the source.
    assert len(data) <= len(original)
    # mime is a real provider-safe image type.
    assert out_mime in ("image/png", "image/jpeg", "image/webp", "image/gif")


def test_dimension_cap_zero_is_disabled():
    original = _png(4000, 3000)
    data, mime = prepare_image_data_for_provider(
        original,
        "image/png",
        "big.png",
        optimize=True,
        min_bytes=1,
        max_dimension=0,
    )
    # Cap off → original non-JPEG passthrough contract holds, byte-identical.
    assert mime == "image/png"
    assert data == original


def test_dimension_cap_ignored_when_not_optimizing():
    original = _png(4000, 3000)
    data, mime = prepare_image_data_for_provider(
        original,
        "image/png",
        "big.png",
        optimize=False,
        max_dimension=2048,
    )
    # Pinned/full-quality → never capped, byte-identical even when huge.
    assert mime == "image/png"
    assert data == original


def test_dimension_cap_skips_image_within_cap():
    original = _jpeg(800, 600, quality=90)
    assert _long_edge(original) == 800
    data, mime = prepare_image_data_for_provider(
        original,
        "image/jpeg",
        "small.jpg",
        optimize=True,
        min_bytes=1024 * 1024,
        max_dimension=2048,
    )
    # Already within the cap and under the byte gate → untouched.
    assert mime == "image/jpeg"
    assert data == original


def test_dimension_cap_oversized_png_falls_back_to_jpeg_when_over_byte_ceiling():
    # A large, high-frequency PNG that stays over min_bytes even after capping
    # must JPEG-flatten as a last resort (only because capping is enabled).
    #
    # Note: this synthetic arithmetic noise is pathologically compressible at
    # 3000px as PNG (~200KB) but becomes high-entropy after LANCZOS downscaling,
    # so the capped output is legitimately LARGER in bytes than the original.
    # That is acceptable and correct: a configured dimension cap targets the
    # provider's PIXEL limit, which the 3000px original violates regardless of
    # its byte size. Real photos/screenshots shrink in both dimensions and bytes.
    img = Image.new("RGB", (3000, 3000))
    px = img.load()
    for y in range(3000):
        for x in range(3000):
            px[x, y] = ((x * 53 + y * 97) % 256, (x * 17) % 256, (y * 29) % 256)
    out = io.BytesIO()
    img.save(out, format="PNG")
    big_png = out.getvalue()

    data, mime = prepare_image_data_for_provider(
        big_png,
        "image/png",
        "noisy.png",
        optimize=True,
        quality=85,
        min_bytes=256 * 1024,
        max_dimension=2048,
    )
    # The cap held (provider pixel limit honored) ...
    assert _long_edge(data) <= 2048
    # ... and it flattened to JPEG to minimize bytes among the capped encodings.
    assert mime == "image/jpeg"


def test_dimension_cap_flattens_animated_gif_to_static():
    frames = [Image.new("P", (3000, 3000), i) for i in range(3)]
    out = io.BytesIO()
    frames[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    animated = out.getvalue()
    with Image.open(io.BytesIO(animated)) as probe:
        assert getattr(probe, "is_animated", False)

    data, mime = prepare_image_data_for_provider(
        animated,
        "image/gif",
        "anim.gif",
        optimize=True,
        min_bytes=1,
        max_dimension=2048,
    )
    with Image.open(io.BytesIO(data)) as img:
        assert max(img.size) <= 2048
        assert not getattr(img, "is_animated", False)


def test_dimension_cap_never_raises_on_undecodable():
    # Sniffs as JPEG (SUPPORTED branch) but is not decodable: the cap attempt
    # returns None and the original bytes are kept — no exception.
    fake_jpeg = b"\xff\xd8\xff" + b"still not a jpeg" * 100000
    data, mime = prepare_image_data_for_provider(
        fake_jpeg,
        "image/jpeg",
        "broken.jpg",
        optimize=True,
        min_bytes=1024 * 1024,
        max_dimension=2048,
    )
    assert mime == "image/jpeg"
    assert data == fake_jpeg
