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
from open_webui.models.chats import Chats
from open_webui.config import ENABLE_ADMIN_CHAT_ACCESS
from open_webui.storage.provider import Storage
from open_webui.utils.access_control import has_access
from open_webui.utils.auth import get_admin_user, get_optional_user, get_verified_user
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

# These endpoints authorize access using the current session, while browser
# cache keys do not vary by that session. Do not retain private file bytes
# across logout or an account switch on a shared browser.
PRIVATE_FILE_CACHE_CONTROL = "private, no-store"


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


def _cleanup_losing_upload(
    file_item: FileModel, uploaded_id: str, uploaded_path: str
) -> None:
    """Delete storage written by an upload that lost the idempotency race."""
    if file_item.id == uploaded_id:
        return

    try:
        Storage.delete_file(uploaded_path)
    except Exception:
        # The winning row is valid and must still be returned. Keep a loud
        # record of cleanup failures so an object-store outage is actionable.
        log.exception(
            "Failed to delete storage for losing upload %s at %s",
            uploaded_id,
            uploaded_path,
        )


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

    # Idempotency: a client that tags a (retried) upload attempt with a stable
    # `upload_id` (see src/lib/apis/files/index.ts) gets the SAME file record
    # back on every retry instead of a fresh duplicate row. This is what makes
    # blind client-side retries on flaky connections safe — without it, a
    # retry after a request that actually reached the server (but whose
    # response was lost) would silently create a duplicate file. Callers that
    # don't send `upload_id` (older clients, non-retrying callers) are
    # unaffected.
    upload_id = (
        file_metadata.get("upload_id") if isinstance(file_metadata, dict) else None
    )
    if upload_id:
        existing_file = await Files.get_file_by_user_id_and_upload_id(
            user.id, upload_id
        )
        if existing_file:
            return {"status": True, **existing_file.model_dump()}

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
            # A concurrent request with the same upload_id may have committed
            # first. insert_new_file then returns that winning row; remove only
            # this request's distinct UUID-backed object before returning it.
            _cleanup_losing_upload(file_item, id, file_path)

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
# Share-scoped File Access (anonymous-allowed, membership-authorized)
############################


def _pending_forbidden(user):
    """Raise 401 for pending users (anonymous/verified/admin all allowed)."""
    if user and user.role == "pending":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


async def _resolve_shared_chat_for_file(share_id: str, user):
    """Resolve the shared chat a viewer may read, or raise 404.

    Mirrors the share-tool-results route: anonymous viewers are allowed,
    pending users are blocked, and admins keep raw-chat-id access when
    ``ENABLE_ADMIN_CHAT_ACCESS`` is on. Authorization for individual files is
    handled by the caller via ``Chats.get_referenced_file_ids``.
    """
    _pending_forbidden(user)

    chat = await Chats.resolve_shared_chat(
        share_id,
        admin_access=(
            user is not None
            and user.role == "admin"
            and ENABLE_ADMIN_CHAT_ACCESS
        ),
    )
    if not chat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    return chat


async def _authorize_shared_file(share_id: str, file_id: str, user):
    """Resolve the shared chat and authorize a single file for a shared viewer.

    A file is served through a share ONLY when it is BOTH referenced by the
    shared chat AND owned by the share's owner. The ownership check is essential:
    the chat JSON (and therefore ``get_referenced_file_ids``) is fully
    owner-controlled — ``update_chat_by_id`` merges the posted chat verbatim — so
    a user could otherwise inject an arbitrary victim-owned ``file_id`` into
    their own shared chat and read it. Returns the File, or raises 404.
    """
    chat = await _resolve_shared_chat_for_file(share_id, user)

    if file_id not in await Chats.get_referenced_file_ids(chat):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    file = await Files.get_file_by_id(file_id)
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    # The file must belong to the share's owner. `chat.user_id` is the fallback
    # for the admin raw-chat-id path (where share_id is not a real share_id).
    owner_id = (await Chats.get_share_owner_id(share_id)) or chat.user_id
    if file.user_id != owner_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    return file


@router.get("/share/{share_id}/{file_id}", response_model=Optional[FileModel])
async def get_shared_file_by_id(
    share_id: str, file_id: str, user=Depends(get_optional_user)
):
    file = await _authorize_shared_file(share_id, file_id, user)

    # Public share link — strip owner-private fields the shared preview never
    # reads (the owner's real user UUID, the internal storage path, and the
    # access-control policy), mirroring sanitize_shared_chat_model. ``data`` and
    # ``meta`` are kept: FilePreview consumes file.data (content /
    # container_workspace / preview ids) and meta (name/size/content_type). But
    # data.container_workspace embeds the ORIGINAL chat_id / message_id / sha256,
    # which the chat share response deliberately hides — drop those too so a
    # generated-file share can't leak the internal chat/message ids.
    projected = file.model_dump()
    projected["user_id"] = f"shared-{file.id}"
    projected["path"] = None
    projected["access_control"] = None

    # container_workspace metadata is embedded in TWO places on a File row —
    # data.container_workspace AND meta.data.container_workspace — strip the
    # internal ids from both.
    def _strip_cw(container):
        if isinstance(container, dict) and isinstance(
            container.get("container_workspace"), dict
        ):
            cw = container["container_workspace"]
            return {
                **container,
                "container_workspace": {
                    k: v
                    for k, v in cw.items()
                    if k not in ("chat_id", "message_id", "sha256")
                },
            }
        return container

    projected["data"] = _strip_cw(projected.get("data"))
    meta = projected.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("data"), dict):
        projected["meta"] = {**meta, "data": _strip_cw(meta.get("data"))}

    return FileModel(**projected)


@router.get("/share/{share_id}/{file_id}/content")
async def get_shared_file_content_by_id(
    share_id: str,
    file_id: str,
    user=Depends(get_optional_user),
    attachment: bool = Query(False),
    w: Optional[int] = Query(None),
):
    file = await _authorize_shared_file(share_id, file_id, user)

    # Membership + owner check already authorized this read — serve the bytes
    # exactly like get_file_content_by_id but WITHOUT re-running the owner/access
    # gate (the viewer is not the owner).
    try:
        file_path = Storage.get_file(file.path)
        file_path = Path(file_path)

        # Check if the file already exists in the cache
        if file_path.is_file():
            content_type = file.meta.get("content_type")
            filename = file.meta.get("name", file.filename)
            encoded_filename = quote(filename)  # RFC5987 encoding
            headers = {"Cache-Control": PRIVATE_FILE_CACHE_CONTROL}

            if attachment:
                headers["Content-Disposition"] = (
                    f"attachment; filename*=UTF-8''{encoded_filename}"
                )
            else:
                # Serve a resized thumbnail for a whitelisted `w` (see the
                # owner-authorized endpoint for rationale). Full-res omits `w`.
                resized = _resized_image_for(file, file_path, w)
                if resized:
                    resized_path, resized_media_type = resized
                    headers["Content-Disposition"] = (
                        f"inline; filename*=UTF-8''{encoded_filename}"
                    )
                    return FileResponse(
                        resized_path,
                        headers=headers,
                        media_type=resized_media_type,
                    )

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
    except HTTPException:
        raise
    except Exception as e:
        log.exception(e)
        log.error("Error getting shared file content")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error getting file content"),
        )


############################
# Server-side image thumbnails
############################

# Whitelisted thumbnail widths. Bounding the set keeps the on-disk resize cache
# small (re-upload mints a new id, so each id has at most len(THUMBNAIL_WIDTHS)
# sidecars). Any other `w` value falls through to the untouched original.
THUMBNAIL_WIDTHS = {256, 768, 1024, 2048}


def _thumbnail_sidecar_path(file_path: Path, w: int, ext: str) -> Path:
    # Cache next to the original. `id` is immutable, so this never goes stale.
    return file_path.with_name(f"{file_path.name}.w{w}.{ext}")


def _generate_thumbnail(src: Path, w: int) -> Optional[tuple[Path, str]]:
    """Resize a raster image to fit within `w` px and cache it on disk.

    Returns (path, media_type) for the resized bytes, or None to signal the
    caller should serve the ORIGINAL untouched. None is returned for anything
    that must pass through: animated images, non-raster/unsupported formats,
    decompression bombs, or any decode/encode error.
    """
    try:
        from PIL import Image, ImageOps

        resample = getattr(Image, "Resampling", Image).LANCZOS

        with Image.open(src) as im:
            # Animated GIF/WebP: never rasterize to a single frame — pass through.
            if getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1:
                return None

            im = ImageOps.exif_transpose(im)
            # Normalize exotic modes so the encoders below accept them.
            if im.mode == "P":
                im = im.convert("RGBA" if "transparency" in im.info else "RGB")
            elif im.mode not in ("RGB", "RGBA", "L", "LA"):
                im = im.convert("RGB")

            im.thumbnail((w, w), resample)

            # Prefer WebP; fall back to JPEG where the Pillow build lacks WebP.
            try:
                out = _thumbnail_sidecar_path(src, w, "webp")
                tmp = out.with_name(out.name + ".tmp")
                im.save(tmp, format="WEBP", quality=80, method=4)
                os.replace(tmp, out)
                return out, "image/webp"
            except Exception:
                out = _thumbnail_sidecar_path(src, w, "jpg")
                tmp = out.with_name(out.name + ".tmp")
                im.convert("RGB").save(tmp, format="JPEG", quality=82, optimize=True)
                os.replace(tmp, out)
                return out, "image/jpeg"
    except Exception as e:
        # Decompression bombs (PIL.Image.DecompressionBombError) and any decode
        # error land here → serve the original bytes untouched.
        log.debug("thumbnail generation skipped for %s (w=%s): %s", src, w, e)
        return None


def _resized_image_for(
    file: FileModel, file_path: Path, w: Optional[int]
) -> Optional[tuple[Path, str]]:
    """Return (path, media_type) for a cached/generated thumbnail, or None.

    None means: serve the original (invalid width, non-raster, SVG, or the
    format/content could not be safely resized).
    """
    if not w or w not in THUMBNAIL_WIDTHS:
        return None

    content_type = ((file.meta or {}).get("content_type") or "").lower()
    if not content_type.startswith("image/") or content_type == "image/svg+xml":
        return None

    # Serve a previously generated sidecar if present (immutable id ⇒ never stale).
    for ext, media_type in (("webp", "image/webp"), ("jpg", "image/jpeg")):
        cached = _thumbnail_sidecar_path(file_path, w, ext)
        if cached.is_file():
            return cached, media_type

    return _generate_thumbnail(file_path, w)


############################
# Image dimensions (layout-shift prevention)
############################
#
# The chat UI reserves the exact box an inline image will occupy BEFORE its
# bytes arrive (see src/lib/utils/imageDimensions.ts + common/Image.svelte).
# Without a reserved box every image pops in from zero height and shoves the
# whole conversation, which is precisely what makes reading an old chat feel
# like the page is teleporting. The client asks for the intrinsic size of the
# images it is about to render in ONE batched call; the answer is memoized in
# `file.meta.image` on first computation, so this is a PIL header read exactly
# once per file, ever.

# EXIF orientations 5-8 mean the stored pixels are rotated 90°: browsers honor
# the tag, so the LAID-OUT size has width/height swapped relative to the raw
# raster (and `ImageOps.exif_transpose` in the thumbnail path does the same).
_EXIF_ORIENTATION_TAG = 0x0112
_EXIF_SWAPPED_ORIENTATIONS = {5, 6, 7, 8}


async def _image_dimensions_for(
    file: FileModel, file_path: Path
) -> Optional[tuple[int, int]]:
    """Intrinsic (width, height) of a raster image, as the browser will lay it out.

    Cached in `file.meta.image`; returns None for anything PIL can't measure
    (SVG, non-images, unreadable bytes) so the caller can simply omit the entry.
    """
    meta = file.meta or {}
    cached = meta.get("image")
    if isinstance(cached, dict):
        width, height = cached.get("width"), cached.get("height")
        if isinstance(width, int) and isinstance(height, int):
            if width > 0 and height > 0:
                return width, height

    content_type = (meta.get("content_type") or "").lower()
    if not content_type.startswith("image/") or content_type == "image/svg+xml":
        return None

    try:
        from PIL import Image

        # Image.open is lazy — it parses the header only, so this stays cheap
        # even for large originals (no full decode).
        with Image.open(file_path) as im:
            width, height = im.size
            try:
                orientation = im.getexif().get(_EXIF_ORIENTATION_TAG)
            except Exception:
                orientation = None
        if orientation in _EXIF_SWAPPED_ORIENTATIONS:
            width, height = height, width
    except Exception as e:
        log.debug("image dimensions unavailable for %s: %s", file.id, e)
        return None

    if not width or not height:
        return None

    try:
        await Files.update_file_metadata_by_id(
            file.id, {"image": {"width": width, "height": height}}
        )
    except Exception:
        # A failed memoization only costs the next request another header read.
        log.debug("could not persist image dimensions for %s", file.id, exc_info=True)

    return width, height


class FileDimensionsForm(BaseModel):
    ids: list[str]
    # Set on the public share page, where the viewer is not the owner (and may
    # be anonymous) — authorization then runs through the share membership.
    share_id: Optional[str] = None


# Bounds the work one request can ask for; the client batches per rendered
# page of messages, which is far below this.
MAX_DIMENSION_IDS = 250


@router.post("/dimensions")
async def get_image_dimensions_by_ids(
    form_data: FileDimensionsForm, user=Depends(get_optional_user)
):
    """Batch intrinsic image sizes: {file_id: {"width": w, "height": h}}.

    Ids that are missing, unauthorized, or not measurable raster images are
    silently omitted — the client falls back to measuring on load.
    """
    share_id = form_data.share_id
    if not share_id and not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    dimensions: dict[str, dict[str, int]] = {}
    seen: set[str] = set()

    for file_id in form_data.ids[:MAX_DIMENSION_IDS]:
        if not file_id or file_id in seen:
            continue
        seen.add(file_id)

        try:
            if share_id:
                file = await _authorize_shared_file(share_id, file_id, user)
            else:
                file = await Files.get_file_by_id(file_id)
                if not file:
                    continue
                if not (
                    file.user_id == user.id
                    or user.role == "admin"
                    or await has_access_to_file(file_id, "read", user)
                ):
                    continue

            size = await _image_dimensions_for(file, Path(Storage.get_file(file.path)))
            if size:
                dimensions[file_id] = {"width": size[0], "height": size[1]}
        except Exception:
            # One bad id must never fail the batch for the others.
            continue

    return {"dimensions": dimensions}


############################
# Get File Content By Id
############################


@router.get("/{id}/content")
async def get_file_content_by_id(
    id: str,
    user=Depends(get_verified_user),
    attachment: bool = Query(False),
    w: Optional[int] = Query(None),
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
                headers = {"Cache-Control": PRIVATE_FILE_CACHE_CONTROL}

                if attachment:
                    headers["Content-Disposition"] = (
                        f"attachment; filename*=UTF-8''{encoded_filename}"
                    )
                else:
                    # Inline raster image + a whitelisted `w` ⇒ serve a resized
                    # thumbnail (bandwidth win on metered links). Full-res is
                    # requested without `w`. Non-raster/decode failures fall
                    # through to the original bytes below.
                    resized = _resized_image_for(file, file_path, w)
                    if resized:
                        resized_path, resized_media_type = resized
                        headers["Content-Disposition"] = (
                            f"inline; filename*=UTF-8''{encoded_filename}"
                        )
                        return FileResponse(
                            resized_path,
                            headers=headers,
                            media_type=resized_media_type,
                        )

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
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
            "Cache-Control": PRIVATE_FILE_CACHE_CONTROL,
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
