import io
import logging
import os
import uuid
import json
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import (
    BackgroundTasks,
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
    Query,
)

from fastapi.responses import FileResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from open_webui.constants import ERROR_MESSAGES
from open_webui.env import SRC_LOG_LEVELS

from open_webui.models.users import Users
from open_webui.models.files import (
    FileForm,
    FileModel,
    FileModelResponse,
    Files,
)
from open_webui.storage.provider import Storage
from open_webui.utils.access_control import has_access
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.file_extraction import (
    extract_and_cache_file_content,
    file_needs_extraction,
)
from open_webui.utils.file_conversion import convert_and_cache_file_to_pdf, get_or_convert_to_pdf
from open_webui.utils.image_conversion import (
    convert_heif_to_jpeg,
    is_heif_image,
    sniff_image_mime_type,
    HEIF_MIME_TYPES,
)
from pydantic import BaseModel

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

router = APIRouter()


############################
# Check if the current user has access to a file (owner/admin/access_control).
############################


async def has_access_to_file(
    file_id: Optional[str], access_type: str, user=Depends(get_verified_user)
) -> bool:
    file = await Files.get_file_by_id(file_id)
    log.debug(f"Checking if user has {access_type} access to file")

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if file.user_id == user.id or user.role == "admin":
        return True

    access_control = getattr(file, "access_control", None)
    if access_control:
        return has_access(user.id, type=access_type, access_control=access_control)

    return False


############################
# Upload File
############################

@router.post("/", response_model=FileModelResponse)
async def upload_file(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    metadata: Optional[dict | str] = Form(None),
    process: bool = Query(True),
    process_in_background: bool = Query(True),
    user=Depends(get_verified_user),
):
    return await upload_file_handler(
        request,
        file=file,
        metadata=metadata,
        process=process,
        process_in_background=process_in_background,
        user=user,
        background_tasks=background_tasks,
    )


def uploaded_file_id(file_item) -> str:
    """Pull the file id from an upload_file_handler result.

    upload_file_handler returns a dict ({"status": True, **file_item.model_dump()})
    rather than a FileModel. Callers that pre-dated that change (upload_image,
    upload_audio) did `file_item.id` and crashed with
    'dict' object has no attribute 'id'. Accept either shape.
    """
    if file_item is None:
        raise ValueError("file upload returned no result")
    fid = (
        file_item.get("id")
        if isinstance(file_item, dict)
        else getattr(file_item, "id", None)
    )
    if not fid:
        raise ValueError("uploaded file has no id")
    return fid


async def upload_file_handler(
    request: Request,
    file: UploadFile = File(...),
    metadata: Optional[dict | str] = Form(None),
    process: bool = Query(True),
    process_in_background: bool = Query(True),
    user=Depends(get_verified_user),
    background_tasks: Optional[BackgroundTasks] = None,
):
    log.info(f"file.content_type: {file.content_type}")

    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Invalid metadata format"),
            )
    file_metadata = metadata if metadata else {}

    try:
        unsanitized_filename = file.filename
        filename = os.path.basename(unsanitized_filename)

        file_extension = os.path.splitext(filename)[1]
        # Remove the leading dot from the file extension
        file_extension = file_extension[1:].lower() if file_extension else ""
        original_file_extension = file_extension

        content_type = file.content_type
        if content_type:
            content_type = content_type.split(";", 1)[0].strip().lower()
            if content_type == "image/jpg":
                content_type = "image/jpeg"
            elif content_type == "image/pjpeg":
                content_type = "image/jpeg"
            elif content_type == "image/x-png":
                content_type = "image/png"
            elif content_type in ("image/x-heic", "image/heic-sequence"):
                content_type = "image/heic"
            elif content_type in ("image/x-heif", "image/heif-sequence"):
                content_type = "image/heif"

        inferred_content_type = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
            "heic": "image/heic",
            "heics": "image/heic",
            "heif": "image/heif",
            "heifs": "image/heif",
            "hif": "image/heif",
            "hifs": "image/heif",
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "doc": "application/msword",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xls": "application/vnd.ms-excel",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "ppt": "application/vnd.ms-powerpoint",
            "odt": "application/vnd.oasis.opendocument.text",
            "odp": "application/vnd.oasis.opendocument.presentation",
            "ods": "application/vnd.oasis.opendocument.spreadsheet",
            "epub": "application/epub+zip",
            "csv": "text/csv",
            "html": "text/html",
            "htm": "text/html",
            "md": "text/markdown",
            "rtf": "application/rtf",
            "txt": "text/plain",
            "json": "application/json",
            "xml": "application/xml",
        }.get(file_extension.lower())

        if inferred_content_type:
            if not content_type or content_type == "application/octet-stream":
                content_type = inferred_content_type
            elif content_type in (
                "application/zip",
                "application/x-zip-compressed",
                "multipart/x-zip",
            ) and file_extension.lower() in (
                "docx",
                "xlsx",
                "pptx",
                "odt",
                "odp",
                "ods",
                "epub",
            ):
                # macOS Safari (and some other browsers) report zip-based office
                # formats with a generic zip MIME. Prefer the extension-based MIME
                # so downstream loader/extraction code routes the file correctly.
                content_type = inferred_content_type

        upload_stream = file.file
        conversion_metadata = None

        try:
            file.file.seek(0)
        except Exception:
            pass

        try:
            header = file.file.read(512)
        except Exception:
            header = b""
        finally:
            try:
                file.file.seek(0)
            except Exception:
                pass

        # Trust the actual image magic bytes over the browser-reported MIME and
        # the extension. Safari/iOS Photos and some share sheets attach images
        # with a wrong/empty content_type; the stored value drives both the
        # served Content-Type and the inline-vs-attachment disposition below, so
        # a wrong value renders images as broken/download-only. sniff only ever
        # returns a real image type (or None for non-images), so this never
        # mislabels documents. HEIF is left to the dedicated branch that follows
        # (it converts to JPEG and sets content_type itself).
        sniffed_content_type = sniff_image_mime_type(header)
        if (
            sniffed_content_type
            and sniffed_content_type not in HEIF_MIME_TYPES
            and sniffed_content_type != content_type
        ):
            content_type = sniffed_content_type

        if is_heif_image(content_type, filename, header):
            original_name = filename
            original_content_type = content_type
            try:
                raw = file.file.read()
                if not raw:
                    raise ValueError(ERROR_MESSAGES.EMPTY_CONTENT)
                converted = convert_heif_to_jpeg(raw)
            except Exception as e:
                log.exception("HEIC/HEIF conversion failed for %s: %s", filename, e)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT(
                        f"Could not convert HEIC/HEIF image to JPEG: {e}"
                    ),
                )

            base, _ = os.path.splitext(filename)
            filename = f"{base or 'image'}.jpg"
            file_extension = "jpg"
            content_type = "image/jpeg"
            upload_stream = io.BytesIO(converted)
            conversion_metadata = {
                "type": "heic_to_jpeg",
                "original_name": original_name,
                "original_content_type": original_content_type,
                "original_extension": original_file_extension,
                "original_size": len(raw),
                "converted_content_type": content_type,
                "converted_size": len(converted),
            }
        else:
            try:
                file.file.seek(0)
            except Exception:
                pass

        if request.app.state.config.ALLOWED_FILE_EXTENSIONS:
            allowed_extensions = [
                ext.lower().lstrip(".")
                for ext in request.app.state.config.ALLOWED_FILE_EXTENSIONS
                if ext
            ]

            if "jpeg" in allowed_extensions and "jpg" not in allowed_extensions:
                allowed_extensions.append("jpg")
            if "jpg" in allowed_extensions and "jpeg" not in allowed_extensions:
                allowed_extensions.append("jpeg")

            original_extension_allowed = bool(
                conversion_metadata
                and original_file_extension
                and original_file_extension in allowed_extensions
            )
            if file_extension not in allowed_extensions and not original_extension_allowed:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT(
                        f"File type {file_extension} is not allowed"
                    ),
                )

        # replace filename with uuid
        id = str(uuid.uuid4())
        name = filename
        filename = f"{id}_{filename}"
        size, file_path = Storage.upload_file(
            upload_stream,
            filename,
            {
                "OpenWebUI-User-Email": user.email,
                "OpenWebUI-User-Id": user.id,
                "OpenWebUI-User-Name": user.name,
                "OpenWebUI-File-Id": id,
            },
        )

        needs_extraction = file_needs_extraction(content_type, file_extension)
        initial_status = "pending" if needs_extraction and process else "completed"

        file_item = await Files.insert_new_file(
            user.id,
            FileForm(
                **{
                    "id": id,
                    "filename": name,
                    "path": file_path,
                    "data": {
                        "status": initial_status,
                    },
                    "meta": {
                        "name": name,
                        "content_type": content_type,
                        "size": size,
                        "data": file_metadata,
                        **(
                            {"converted_from": conversion_metadata}
                            if conversion_metadata
                            else {}
                        ),
                    },
                }
            ),
        )

        if file_item:
            # Kick off text extraction in the background. The Loader runs against
            # the on-disk file, populates `file.data.content`, and walks status
            # pending → processing → completed | failed. The lazy fallback in
            # openai.py's file-part loop covers the race window where the user
            # submits before the background task finishes, plus old files that
            # were uploaded before background extraction was wired up.
            if needs_extraction and process:
                if process_in_background and background_tasks is not None:
                    background_tasks.add_task(
                        extract_and_cache_file_content, request, file_item.id
                    )
                else:
                    await extract_and_cache_file_content(request, file_item.id)

            return {"status": True, **file_item.model_dump()}

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error uploading file"),
        )

    except (HTTPException, StarletteHTTPException):
        raise
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error uploading file"),
        )


############################
# List Files
############################


@router.get("/", response_model=list[FileModelResponse])
async def list_files(user=Depends(get_verified_user), content: bool = Query(True)):
    if user.role == "admin":
        files = await Files.get_files()
    else:
        files = await Files.get_files_by_user_id(user.id)

    if not content:
        for file in files:
            if "content" in file.data:
                del file.data["content"]

    return files


############################
# Search Files
############################


@router.get("/search", response_model=list[FileModelResponse])
async def search_files(
    filename: str = Query(
        ...,
        description="Filename pattern to search for. Supports wildcards such as '*.txt'",
    ),
    content: bool = Query(True),
    user=Depends(get_verified_user),
):
    """
    Search for files by filename with support for wildcard patterns.
    """
    # Get files according to user role
    if user.role == "admin":
        files = await Files.get_files()
    else:
        files = await Files.get_files_by_user_id(user.id)

    # Get matching files
    matching_files = [
        file for file in files if fnmatch(file.filename.lower(), filename.lower())
    ]

    if not matching_files:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No files found matching the pattern.",
        )

    if not content:
        for file in matching_files:
            if "content" in file.data:
                del file.data["content"]

    return matching_files


############################
# Delete All Files
############################


@router.delete("/all")
async def delete_all_files(user=Depends(get_admin_user)):
    result = await Files.delete_all_files()
    if result:
        try:
            Storage.delete_all_files()
        except Exception as e:
            log.exception(e)
            log.error("Error deleting files")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error deleting files"),
            )
        return {"message": "All files deleted successfully"}
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error deleting files"),
        )


############################
# Get File By Id
############################


@router.get("/{id}", response_model=Optional[FileModel])
async def get_file_by_id(id: str, user=Depends(get_verified_user)):
    file = await Files.get_file_by_id(id)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or await has_access_to_file(id, "read", user)
    ):
        return file
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# Set Processing Mode
############################


class ProcessModeForm(BaseModel):
    mode: str  # "text" | "pdf"


def _file_is_pdf(file: FileModel) -> bool:
    content_type = ((file.meta or {}).get("content_type") or "").lower()
    return content_type == "application/pdf" or file.filename.lower().endswith(".pdf")


def _file_can_preview_as_pdf(file: FileModel) -> bool:
    if _file_is_pdf(file):
        return True
    name = ((file.meta or {}).get("name") or file.filename or "").lower()
    ext = Path(name).suffix.lower().lstrip(".")
    content_type = ((file.meta or {}).get("content_type") or "").lower()
    return ext in {
        "doc", "docx", "odt", "rtf", "ppt", "pptx", "odp", "xls", "xlsx", "ods"
    } or content_type in {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.presentation",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/rtf",
    }


@router.post("/{id}/preview")
async def ensure_file_preview_by_id(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
):
    """Return or create a PDF preview for document-like files.

    Used for model-produced artifacts. The original file stays the download;
    the preview file is a cached PDF sidecar.
    """
    file = await Files.get_file_by_id(id)
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    if not (
        file.user_id == user.id
        or user.role == "admin"
        or await has_access_to_file(id, "read", user)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if _file_is_pdf(file):
        return {"status": True, "preview_file": file.model_dump()}

    if not _file_can_preview_as_pdf(file):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("No PDF preview is available for this file type"),
        )

    preview_file = await get_or_convert_to_pdf(request, id, user)
    if not preview_file:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Failed to create PDF preview"),
        )
    return {"status": True, "preview_file": preview_file.model_dump()}


@router.post("/{id}/process-mode")
async def set_file_processing_mode(
    request: Request,
    id: str,
    form_data: ProcessModeForm,
    background_tasks: BackgroundTasks,
    user=Depends(get_verified_user),
):
    """Set how a file should be processed when attached to a chat.

    For ``mode == "pdf"``, kicks off a background LibreOffice conversion if the
    file isn't already a PDF and hasn't been converted yet. The chat-completion
    file-part loop reads the converted PDF when this file flows into a message
    with ``processing_mode: "pdf"``.

    For ``mode == "text"``, this is a no-op: text extraction always runs at
    upload time (or lazily at chat-completion time for old files).
    """
    file = await Files.get_file_by_id(id)
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if not (
        file.user_id == user.id
        or user.role == "admin"
        or await has_access_to_file(id, "read", user)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    mode = (form_data.mode or "").lower()
    if mode not in ("text", "pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(f"Invalid processing mode: {form_data.mode}"),
        )

    if mode == "pdf":
        if not getattr(request.app.state, "PDF_CONVERSION_AVAILABLE", False):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=ERROR_MESSAGES.DEFAULT(
                    "PDF conversion is unavailable. Install LibreOffice on the server to enable."
                ),
            )

        data = file.data or {}
        already_converted = bool(data.get("pdf_file_id"))
        in_progress = data.get("pdf_status") == "processing"

        # Skip the source file itself if it's a PDF — nothing to convert.
        content_type = (file.meta or {}).get("content_type") or ""
        if content_type == "application/pdf" or file.filename.lower().endswith(".pdf"):
            already_converted = True

        if not already_converted and not in_progress:
            await Files.update_file_data_by_id(id, {"pdf_status": "pending"})
            background_tasks.add_task(
                convert_and_cache_file_to_pdf, request, id
            )

    file = await Files.get_file_by_id(id)
    return {
        "status": True,
        "file_id": id,
        "mode": mode,
        "pdf_status": (file.data or {}).get("pdf_status") if file else None,
        "pdf_file_id": (file.data or {}).get("pdf_file_id") if file else None,
    }


############################
# Get File Content By Id
############################


@router.get("/{id}/content")
async def get_file_content_by_id(
    id: str, user=Depends(get_verified_user), attachment: bool = Query(False)
):
    file = await Files.get_file_by_id(id)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or await has_access_to_file(id, "read", user)
    ):
        try:
            file_path = Storage.get_file(file.path)
            file_path = Path(file_path)

            # Check if the file already exists in the cache
            if file_path.is_file():
                # Handle Unicode filenames
                filename = file.meta.get("name", file.filename)
                encoded_filename = quote(filename)  # RFC5987 encoding

                content_type = file.meta.get("content_type")
                filename = file.meta.get("name", file.filename)
                encoded_filename = quote(filename)
                headers = {}

                if attachment:
                    headers["Content-Disposition"] = (
                        f"attachment; filename*=UTF-8''{encoded_filename}"
                    )
                else:
                    if content_type == "application/pdf" or filename.lower().endswith(
                        ".pdf"
                    ):
                        headers["Content-Disposition"] = (
                            f"inline; filename*=UTF-8''{encoded_filename}"
                        )
                        content_type = "application/pdf"
                    elif (
                        content_type
                        and content_type.startswith("image/")
                        and content_type != "image/svg+xml"
                    ):
                        headers["Content-Disposition"] = (
                            f"inline; filename*=UTF-8''{encoded_filename}"
                        )
                    elif content_type != "text/plain":
                        headers["Content-Disposition"] = (
                            f"attachment; filename*=UTF-8''{encoded_filename}"
                        )

                return FileResponse(file_path, headers=headers, media_type=content_type)

            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=ERROR_MESSAGES.NOT_FOUND,
                )
        except Exception as e:
            log.exception(e)
            log.error("Error getting file content")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error getting file content"),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


@router.get("/{id}/content/html")
async def get_html_file_content_by_id(id: str, user=Depends(get_verified_user)):
    file = await Files.get_file_by_id(id)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    file_user = await Users.get_user_by_id(file.user_id)
    if not file_user.role == "admin":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or await has_access_to_file(id, "read", user)
    ):
        try:
            file_path = Storage.get_file(file.path)
            file_path = Path(file_path)

            # Check if the file already exists in the cache
            if file_path.is_file():
                log.info(f"file_path: {file_path}")
                return FileResponse(file_path)
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=ERROR_MESSAGES.NOT_FOUND,
                )
        except Exception as e:
            log.exception(e)
            log.error("Error getting file content")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error getting file content"),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


@router.get("/{id}/content/{file_name}")
async def get_file_content_by_id(id: str, user=Depends(get_verified_user)):
    file = await Files.get_file_by_id(id)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or await has_access_to_file(id, "read", user)
    ):
        file_path = file.path

        # Handle Unicode filenames
        filename = file.meta.get("name", file.filename)
        encoded_filename = quote(filename)  # RFC5987 encoding
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        }

        if file_path:
            file_path = Storage.get_file(file_path)
            file_path = Path(file_path)

            # Check if the file already exists in the cache
            if file_path.is_file():
                return FileResponse(file_path, headers=headers)
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=ERROR_MESSAGES.NOT_FOUND,
                )
        else:
            # File path doesn’t exist, return the content as .txt if possible
            file_content = file.content.get("content", "")
            file_name = file.filename

            # Create a generator that encodes the file content
            def generator():
                yield file_content.encode("utf-8")

            return StreamingResponse(
                generator(),
                media_type="text/plain",
                headers=headers,
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# Delete File By Id
############################


@router.delete("/{id}")
async def delete_file_by_id(id: str, user=Depends(get_verified_user)):
    file = await Files.get_file_by_id(id)

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        file.user_id == user.id
        or user.role == "admin"
        or await has_access_to_file(id, "write", user)
    ):

        result = await Files.delete_file_by_id(id)
        if result:
            try:
                Storage.delete_file(file.path)
            except Exception as e:
                log.exception(e)
                log.error("Error deleting files")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT("Error deleting files"),
                )
            return {"message": "File deleted successfully"}
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Error deleting file"),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
