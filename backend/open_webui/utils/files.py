from open_webui.routers.images import (
    load_b64_image_data,
    upload_image,
)

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    UploadFile,
)

from open_webui.routers.files import upload_file_handler, uploaded_file_id

import mimetypes
import base64
import io


async def get_image_url_from_base64(request, base64_image_string, metadata, user):
    if base64_image_string.startswith("data:image/"):
        image_url = ""
        # Extract base64 image data from the line. load_b64_image_data parses the
        # real mime from the data-URL header, so any image/* (png, jpeg, webp, …)
        # is handled — not just png. upload_image derives the extension from the
        # content_type it returns.
        image_data, content_type = load_b64_image_data(base64_image_string)
        if image_data is not None:
            image_url = await upload_image(
                request,
                image_data,
                content_type,
                metadata,
                user,
            )
        return image_url
    return None


def load_b64_audio_data(b64_str):
    try:
        if "," in b64_str:
            header, b64_data = b64_str.split(",", 1)
        else:
            b64_data = b64_str
            header = "data:audio/wav;base64"
        audio_data = base64.b64decode(b64_data)
        content_type = (
            header.split(";")[0].split(":")[1] if ";" in header else "audio/wav"
        )
        return audio_data, content_type
    except Exception as e:
        print(f"Error decoding base64 audio data: {e}")
        return None, None


async def upload_audio(request, audio_data, content_type, metadata, user):
    audio_format = mimetypes.guess_extension(content_type)
    file = UploadFile(
        file=io.BytesIO(audio_data),
        filename=f"generated-{audio_format}",  # will be converted to a unique ID on upload_file
        headers={
            "content-type": content_type,
        },
    )
    file_item = await upload_file_handler(
        request,
        file=file,
        metadata=metadata,
        process=False,
        user=user,
    )
    url = request.app.url_path_for(
        "get_file_content_by_id", id=uploaded_file_id(file_item)
    )
    return url


async def get_audio_url_from_base64(request, base64_audio_string, metadata, user):
    if base64_audio_string.startswith("data:audio/"):
        audio_url = ""
        # Extract base64 audio data from the line. load_b64_audio_data parses the
        # mime from the header, so any audio/* is handled — not just wav.
        audio_data, content_type = load_b64_audio_data(base64_audio_string)
        if audio_data is not None:
            audio_url = await upload_audio(
                request,
                audio_data,
                content_type,
                metadata,
                user,
            )
        return audio_url
    return None


async def get_file_url_from_base64(request, base64_file_string, metadata, user):
    if base64_file_string.startswith("data:image/"):
        return await get_image_url_from_base64(request, base64_file_string, metadata, user)
    elif base64_file_string.startswith("data:audio/"):
        return await get_audio_url_from_base64(request, base64_file_string, metadata, user)
    return None
