import io
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}

HEIF_MIME_TYPES = {
    "image/heic",
    "image/heif",
    "image/heic-sequence",
    "image/heif-sequence",
    "image/x-heic",
    "image/x-heif",
}

HEIF_EXTENSIONS = {"heic", "heif", "heics", "heifs", "hif", "hifs"}

HEIF_BRANDS = {
    b"heic",
    b"heix",
    b"hevc",
    b"hevx",
    b"heim",
    b"heis",
    b"hevm",
    b"hevs",
    b"mif1",
    b"msf1",
}

_HEIF_OPENER_REGISTERED = False


def normalize_image_mime_type(mime_type: Optional[str]) -> Optional[str]:
    if not mime_type:
        return None

    normalized = mime_type.strip().lower().split(";", 1)[0].strip()
    if normalized == "image/jpg":
        return "image/jpeg"
    if normalized == "image/pjpeg":
        return "image/jpeg"
    if normalized == "image/x-png":
        return "image/png"
    if normalized in ("image/x-heic", "image/heic-sequence"):
        return "image/heic"
    if normalized in ("image/x-heif", "image/heif-sequence"):
        return "image/heif"
    return normalized


def get_file_extension(filename: Optional[str]) -> str:
    if not filename:
        return ""
    return os.path.splitext(filename)[1].lower().lstrip(".")


def is_heif_extension(filename_or_extension: Optional[str]) -> bool:
    if not filename_or_extension:
        return False
    value = filename_or_extension.lower().lstrip(".")
    if os.path.sep in value or "." in value:
        value = get_file_extension(value)
    return value in HEIF_EXTENSIONS


def is_heif_mime_type(mime_type: Optional[str]) -> bool:
    return normalize_image_mime_type(mime_type) in {"image/heic", "image/heif"}


def sniff_image_mime_type(image_data: bytes) -> Optional[str]:
    if not image_data:
        return None

    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    if image_data.startswith(b"GIF87a") or image_data.startswith(b"GIF89a"):
        return "image/gif"

    if (
        len(image_data) >= 12
        and image_data[:4] == b"RIFF"
        and image_data[8:12] == b"WEBP"
    ):
        return "image/webp"

    if image_data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"

    if len(image_data) >= 12 and image_data[4:8] == b"ftyp":
        brands = {image_data[8:12]}
        brands.update(
            image_data[i : i + 4]
            for i in range(16, len(image_data) - 3, 4)
            if len(image_data[i : i + 4]) == 4
        )
        if brands & HEIF_BRANDS:
            return "image/heic"

    return None


def infer_image_mime_type_from_filename(filename: Optional[str]) -> Optional[str]:
    ext = get_file_extension(filename)
    if ext == "png":
        return "image/png"
    if ext in ("jpg", "jpeg"):
        return "image/jpeg"
    if ext == "gif":
        return "image/gif"
    if ext == "webp":
        return "image/webp"
    if ext in ("heic", "heics"):
        return "image/heic"
    if ext in ("heif", "heifs", "hif", "hifs"):
        return "image/heif"
    return None


def is_heif_image(
    mime_type: Optional[str] = None,
    filename: Optional[str] = None,
    image_data: Optional[bytes] = None,
) -> bool:
    if is_heif_mime_type(mime_type):
        return True
    if is_heif_extension(filename):
        return True
    if image_data and sniff_image_mime_type(image_data) in ("image/heic", "image/heif"):
        return True
    return False


def _register_heif_opener() -> None:
    global _HEIF_OPENER_REGISTERED
    if _HEIF_OPENER_REGISTERED:
        return
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
        _HEIF_OPENER_REGISTERED = True
    except Exception as e:
        log.debug(f"pillow-heif is unavailable; HEIC/HEIF decode may fail: {e}")


def transcode_image_to_jpeg(image_data: bytes) -> Optional[bytes]:
    """Best-effort JPEG transcode for provider-safe image payloads.

    HEIC/HEIF requires pillow-heif/libheif support. Other formats depend on the
    Pillow build. Returns None when the runtime cannot decode the input.
    """
    try:
        from PIL import Image, ImageOps

        _register_heif_opener()

        with Image.open(io.BytesIO(image_data)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info
            ):
                background = Image.new("RGB", img.size, (255, 255, 255))
                rgba = img.convert("RGBA")
                background.paste(rgba, mask=rgba.getchannel("A"))
                img = background
            else:
                img = img.convert("RGB")

            out = io.BytesIO()
            img.save(out, format="JPEG", quality=92, optimize=True)
            return out.getvalue()
    except Exception as e:
        log.debug(f"Could not transcode image to JPEG: {e}")
        return None


def convert_heif_to_jpeg(image_data: bytes) -> bytes:
    converted = transcode_image_to_jpeg(image_data)
    if not converted:
        raise ValueError(
            "Could not convert HEIC/HEIF image to JPEG. Ensure pillow-heif/libheif "
            "is installed and the uploaded file is a valid Apple Photos image."
        )
    return converted


def resolve_image_mime_type(
    mime_type: Optional[str],
    filename: Optional[str],
    image_data: bytes,
) -> str:
    # Trust the bytes before browser-provided MIME/extension. Safari/iOS can
    # report surprising MIME/extension pairs for Photos/Live Photos.
    sniffed = sniff_image_mime_type(image_data)
    if sniffed:
        return sniffed

    normalized = normalize_image_mime_type(mime_type)
    if normalized in SUPPORTED_IMAGE_MIME_TYPES:
        return normalized

    inferred = infer_image_mime_type_from_filename(filename)
    if inferred:
        return inferred

    return normalized or "application/octet-stream"


def prepare_image_data_for_provider(
    image_data: bytes,
    mime_type: Optional[str],
    filename: Optional[str],
) -> tuple[bytes, str]:
    resolved = resolve_image_mime_type(mime_type, filename, image_data)
    if resolved in SUPPORTED_IMAGE_MIME_TYPES:
        return image_data, resolved

    converted = transcode_image_to_jpeg(image_data)
    if converted:
        return converted, "image/jpeg"

    raise ValueError(
        f"Unsupported image format {resolved or mime_type or filename or 'unknown'}"
    )
