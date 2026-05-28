from open_webui.utils.image_conversion import (
    is_heif_image,
    normalize_image_mime_type,
    prepare_image_data_for_provider,
    sniff_image_mime_type,
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
