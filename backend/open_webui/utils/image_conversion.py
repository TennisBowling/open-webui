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


def transcode_image_to_jpeg(image_data: bytes, quality: int = 92) -> Optional[bytes]:
    """Best-effort JPEG transcode for provider-safe image payloads.

    Decodes any Pillow-readable image and re-encodes it as JPEG at the given
    quality. Used both to convert unsupported formats (HEIC/HEIF, which need
    pillow-heif/libheif) and to shrink oversized JPEGs. Never raises: returns
    None when the runtime cannot decode the input, so callers can fall back to
    the original bytes.
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
            img.save(out, format="JPEG", quality=quality, optimize=True)
            return out.getvalue()
    except Exception as e:
        log.debug(f"Could not transcode image to JPEG: {e}")
        return None


def _downscale_image_bytes(
    image_data: bytes,
    max_dimension: int,
    *,
    quality: int = 92,
    prefer_jpeg: bool = False,
) -> Optional[tuple[bytes, str]]:
    """Cap an image's longest edge at ``max_dimension``, format-preserving.

    Returns ``(bytes, mime)`` when a re-encode was produced, or ``None`` when no
    change is needed/possible — in which case the caller keeps the original
    bytes. ``None`` is returned for:
    - ``max_dimension <= 0`` (capping disabled — the documented escape hatch).
    - An image already within the cap that is NOT an animated frame set (so
      small images stay byte-identical to what the user uploaded).
    - Any decode/encode failure (never raises; we never drop an image).

    Output format mirrors the input (PNG->PNG, WEBP->WEBP, GIF->static GIF,
    JPEG/unknown->JPEG) so screenshots/transparency survive, unless
    ``prefer_jpeg`` forces a JPEG (used for the oversized-payload fallback and
    for already-transcoded HEIC). Animated GIF/WEBP are always flattened to
    their first frame when this runs, because multi-frame payloads are a common
    provider-rejection cause.
    """
    if max_dimension <= 0:
        return None
    try:
        from PIL import Image, ImageOps

        _register_heif_opener()

        resample = getattr(Image, "Resampling", Image).LANCZOS

        with Image.open(io.BytesIO(image_data)) as img:
            fmt = (img.format or "").upper()
            is_animated = getattr(img, "is_animated", False) and (
                getattr(img, "n_frames", 1) > 1
            )

            img = ImageOps.exif_transpose(img)

            # Palette/animated frames resample poorly in "P" mode — promote so
            # LANCZOS has real channels to work with.
            if img.mode in ("P", "PA"):
                img = img.convert("RGBA")

            width, height = img.size
            long_edge = max(width, height)
            needs_resize = long_edge > max_dimension

            if not needs_resize and not is_animated:
                return None

            if needs_resize:
                scale = max_dimension / float(long_edge)
                new_size = (
                    max(1, round(width * scale)),
                    max(1, round(height * scale)),
                )
                img = img.resize(new_size, resample)

            if prefer_jpeg or fmt not in ("PNG", "WEBP", "GIF", "JPEG"):
                out_fmt = "JPEG"
            else:
                out_fmt = fmt

            out = io.BytesIO()
            if out_fmt == "JPEG":
                if img.mode in ("RGBA", "LA") or (
                    img.mode == "P" and "transparency" in img.info
                ):
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    rgba = img.convert("RGBA")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    img = background
                else:
                    img = img.convert("RGB")
                img.save(out, format="JPEG", quality=quality, optimize=True)
                return out.getvalue(), "image/jpeg"
            if out_fmt == "PNG":
                img.save(out, format="PNG", optimize=True)
                return out.getvalue(), "image/png"
            if out_fmt == "WEBP":
                img.save(out, format="WEBP", quality=quality)
                return out.getvalue(), "image/webp"
            if out_fmt == "GIF":
                # Flatten any animation to a single static frame.
                img.convert("RGBA").convert(
                    "P", palette=Image.ADAPTIVE
                ).save(out, format="GIF")
                return out.getvalue(), "image/gif"
    except Exception as e:
        log.debug(f"Could not downscale image: {e}")
        return None
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
    *,
    optimize: bool = False,
    quality: int = 85,
    min_bytes: int = 1024 * 1024,
    max_dimension: int = 0,
) -> tuple[bytes, str]:
    """Resolve an image to a provider-safe (bytes, mime) pair.

    Unsupported formats (notably HEIC/HEIF) are always transcoded to JPEG so
    upstream providers don't fail request parsing.

    When ``optimize`` is True, the payload is additionally made delivery-safe.
    This is lossy/best-effort, NEVER raises, and NEVER drops an image (any
    failure falls back to the prior bytes):
    - ``max_dimension`` (when > 0) caps the longest edge for EVERY format,
      preserving aspect ratio and never upscaling. A pixel-capped image is
      always adopted even if its byte size didn't shrink, because providers
      reject/penalize on dimensions, not just bytes. Animated GIF/WEBP are
      flattened to their first frame.
    - Oversized JPEGs (> ``min_bytes``) are re-encoded at ``quality``, kept only
      if strictly smaller.
    - PNG/WEBP still over ``min_bytes`` after capping fall back to a JPEG
      re-encode, kept only if strictly smaller (screenshots commonly stay large
      as PNG; JPEG keeps them under provider limits).

    With ``optimize`` False the behavior is identical to a plain format
    normalization, so callers that only need provider-safety (e.g. full-quality
    pinned images) are unaffected and their bytes stay identical.
    """
    resolved = resolve_image_mime_type(mime_type, filename, image_data)

    if resolved in SUPPORTED_IMAGE_MIME_TYPES:
        data, mime = image_data, resolved

        if optimize and max_dimension > 0:
            capped = _downscale_image_bytes(data, max_dimension, quality=quality)
            if capped:
                data, mime = capped

        if optimize and mime == "image/jpeg" and len(data) > min_bytes:
            reencoded = transcode_image_to_jpeg(data, quality=quality)
            if reencoded and len(reencoded) < len(data):
                data, mime = reencoded, "image/jpeg"
        elif (
            optimize
            and max_dimension > 0
            and mime in ("image/png", "image/webp")
            and len(data) > min_bytes
        ):
            # Only when dimension-capping is actively enabled: a PNG/WEBP that is
            # still over the byte ceiling AFTER being capped (detailed
            # screenshots) can blow a provider's per-image limit. JPEG-flatten as
            # a last resort. When capping is OFF we never touch non-JPEG formats
            # (preserve transparency/animation — the documented passthrough).
            reencoded = transcode_image_to_jpeg(data, quality=quality)
            if reencoded and len(reencoded) < len(data):
                data, mime = reencoded, "image/jpeg"

        return data, mime

    converted = transcode_image_to_jpeg(
        image_data, quality=quality if optimize else 92
    )
    if converted:
        if optimize and max_dimension > 0:
            capped = _downscale_image_bytes(
                converted, max_dimension, quality=quality, prefer_jpeg=True
            )
            if capped:
                converted = capped[0]
        return converted, "image/jpeg"

    raise ValueError(
        f"Unsupported image format {resolved or mime_type or filename or 'unknown'}"
    )
