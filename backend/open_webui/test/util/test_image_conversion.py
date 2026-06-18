from open_webui.utils.image_conversion import (
    is_heif_image,
    normalize_image_mime_type,
    prepare_image_data_for_provider,
    sniff_image_mime_type,
    _downscale_image_bytes,
    HEIF_MIME_TYPES,
)


def test_normalizes_browser_image_mime_variants():
    assert normalize_image_mime_type("image/jpg") == "image/jpeg"
    assert normalize_image_mime_type("image/pjpeg") == "image/jpeg"
    assert normalize_image_mime_type("image/x-png") == "image/png"
    assert normalize_image_mime_type("image/heic-sequence") == "image/heic"
    assert normalize_image_mime_type("image/heif-sequence") == "image/heif"


def test_detects_apple_heif_by_extension_mime_and_ftyp_signature():
    heic_header = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heic"

    assert is_heif_image(mime_type="image/heic")
    assert is_heif_image(filename="IMG_0001.HEICS")
    assert is_heif_image(image_data=heic_header)
    assert sniff_image_mime_type(heic_header) == "image/heic"


def test_provider_image_preparation_trusts_bytes_before_mime():
    # Browser-provided MIME can be wrong for Apple Photos uploads. A PNG header
    # must stay image/png even if the multipart content-type says octet-stream.
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    prepared, mime = prepare_image_data_for_provider(
        png_bytes,
        "application/octet-stream",
        "upload.bin",
    )

    assert prepared == png_bytes
    assert mime == "image/png"


def _apply_upload_sniff_override(content_type, header):
    """Mirror of the upload-time content-type correction in files.py so the
    decision logic is unit-tested without standing up the full upload handler."""
    sniffed = sniff_image_mime_type(header)
    if (
        sniffed
        and sniffed not in HEIF_MIME_TYPES
        and sniffed != content_type
    ):
        return sniffed
    return content_type


def test_upload_sniff_corrects_wrong_image_content_type():
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (1, 2, 3)).save(buf, format="PNG")
    header = buf.getvalue()[:512]

    # Browser mislabeled a real PNG; the stored content_type drives serving's
    # inline-vs-attachment choice, so it must be corrected to image/png.
    assert _apply_upload_sniff_override("application/octet-stream", header) == "image/png"
    assert _apply_upload_sniff_override("text/plain", header) == "image/png"
    # Already correct → unchanged (no needless override).
    assert _apply_upload_sniff_override("image/png", header) == "image/png"


def test_upload_sniff_leaves_non_images_alone():
    # A document (no image magic) must not be relabeled as an image.
    assert _apply_upload_sniff_override("application/pdf", b"%PDF-1.7\n...") == "application/pdf"
    assert _apply_upload_sniff_override("text/csv", b"a,b,c\n1,2,3\n") == "text/csv"


def test_upload_sniff_defers_heif_to_conversion_branch():
    # HEIF is handled by the dedicated convert-to-JPEG branch; the sniff override
    # must NOT pre-empt it (it would mislabel the to-be-converted file).
    heic_header = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heic"
    assert _apply_upload_sniff_override("application/octet-stream", heic_header) == (
        "application/octet-stream"
    )


def test_downscale_never_upscales_small_image():
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (100, 80), (5, 5, 5)).save(buf, format="PNG")
    small = buf.getvalue()
    # Within the cap → no work, returns None (caller keeps original bytes).
    assert _downscale_image_bytes(small, 2048) is None


def test_downscale_disabled_returns_none():
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (5000, 5000)).save(buf, format="PNG")
    assert _downscale_image_bytes(buf.getvalue(), 0) is None
    assert _downscale_image_bytes(buf.getvalue(), -1) is None


def test_downscale_undecodable_returns_none_no_raise():
    assert _downscale_image_bytes(b"\xff\xd8\xff not a jpeg", 2048) is None
