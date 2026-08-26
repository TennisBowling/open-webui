import asyncio
import json
import logging
from typing import Optional
from urllib.parse import quote
from uuid import uuid4

import aiohttp

from open_webui.socket.main import (
    broadcast_sidebar_event,
    get_active_streams_for_chat,
    get_event_emitter,
    get_stream_state,
    get_tool_result_body,
)
from open_webui.models.chats import (
    ChatForm,
    ChatImportForm,
    ChatResponse,
    ChatBranchConflictError,
    ChatMessageParentMissingError,
    ChatSearchResponse,
    Chats,
    ChatTitleIdResponse,
    _project_message_slim,
    sanitize_shared_chat_model,
    strip_tool_result_bodies_from_chat_model,
    strip_tool_result_bodies_from_message,
)
from open_webui.models.tags import TagModel, Tags
from open_webui.models.folders import Folders
from open_webui.models.models import Models

from open_webui.config import ENABLE_ADMIN_CHAT_ACCESS, ENABLE_ADMIN_EXPORT
from open_webui.constants import ERROR_MESSAGES
from open_webui.env import SRC_LOG_LEVELS
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Any, List, Literal


from open_webui.utils.auth import get_admin_user, get_verified_user, get_optional_user
from open_webui.utils.access_control import has_permission
from open_webui.utils.lazy_blocks import (
    parse_reasoning_ref,
    reasoning_body_from_blocks,
    sanitize_content_blocks,
)
from open_webui.utils.cache import etag_response
from open_webui.utils.chat_embedder import aembed_query
from open_webui.utils import chat_embedder as ce
from open_webui.utils.container_workspace import _container_connection_url

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

router = APIRouter()


def _message_parent_conflict(error: ChatMessageParentMissingError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": str(error),
            "code": error.code,
            "message_id": error.message_id,
            "parent_id": error.parent_id,
        },
    )


def _chat_row_payload(chat) -> dict:
    """Minimal row data the sidebar needs to render a chat entry without a
    refetch. Returned for chat:created / chat:renamed / chat:pinned /
    chat:archived / chat:folder events."""
    return {
        "id": chat.id,
        "title": chat.title,
        "updated_at": chat.updated_at,
        "created_at": chat.created_at,
        "pinned": bool(getattr(chat, "pinned", False) or False),
        "archived": bool(getattr(chat, "archived", False) or False),
        "folder_id": getattr(chat, "folder_id", None),
    }


def _skip_sid(request: Request) -> Optional[str]:
    return request.headers.get("x-session-id") if request else None


############################
# GetChatList
############################


@router.get("/", response_model=None)
@router.get("/list", response_model=None)
async def get_session_user_chat_list(
    request: Request,
    user=Depends(get_verified_user),
    page: Optional[int] = None,
    include_pinned: Optional[bool] = False,
    include_folders: Optional[bool] = False,
):
    try:
        if page is not None:
            limit = 60
            skip = (page - 1) * limit
        else:
            # No page => historically unbounded. Cap it: every in-app caller
            # paginates, and an accidental no-page call on a large account
            # dumped a multi-MB title dump (measured ~2MB raw here).
            limit = 1000
            skip = 0

        chat_list = await Chats.get_chat_title_id_list_by_user_id(
            user.id,
            include_folders=include_folders,
            include_pinned=include_pinned,
            skip=skip,
            limit=limit,
        )
        # ETag/304: the sidebar refetches page 1 on reconnect/visibility-regain
        # and after archive/pin/delete — usually unchanged, so let it revalidate.
        content = [
            item.model_dump() if hasattr(item, "model_dump") else dict(item)
            for item in chat_list
        ]
        return etag_response(content, request)
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# GetChatCount
############################


@router.get("/count", response_model=int)
async def get_session_user_chat_count(user=Depends(get_verified_user)):
    return await Chats.count_chats_by_user_id(user.id)


############################
# DeleteAllChats
############################


@router.delete("/", response_model=bool)
async def delete_all_user_chats(request: Request, user=Depends(get_verified_user)):

    if user.role == "user" and not has_permission(
        user.id, "chat.delete", request.app.state.config.USER_PERMISSIONS
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    result = await Chats.delete_chats_by_user_id(user.id)
    if result:
        await broadcast_sidebar_event(
            user.id,
            {"type": "chats:bulk", "data": {"operation": "delete_all"}},
            skip_sid=_skip_sid(request),
        )
    return result


############################
# GetUserChatList
############################


@router.get("/list/user/{user_id}", response_model=list[ChatTitleIdResponse])
async def get_user_chat_list_by_user_id(
    user_id: str,
    page: Optional[int] = None,
    query: Optional[str] = None,
    order_by: Optional[str] = None,
    direction: Optional[str] = None,
    user=Depends(get_admin_user),
):
    if not ENABLE_ADMIN_CHAT_ACCESS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    if page is None:
        page = 1

    limit = 60
    skip = (page - 1) * limit

    filter = {}
    if query:
        filter["query"] = query
    if order_by:
        filter["order_by"] = order_by
    if direction:
        filter["direction"] = direction

    return await Chats.get_chat_list_by_user_id(
        user_id, include_archived=True, filter=filter, skip=skip, limit=limit
    )


############################
# CreateNewChat
############################


@router.post("/new", response_model=Optional[ChatResponse])
async def create_new_chat(
    request: Request, form_data: ChatForm, user=Depends(get_verified_user)
):
    try:
        chat = await Chats.insert_new_chat(user.id, form_data)
        await broadcast_sidebar_event(
            user.id,
            {"type": "chat:created", "data": _chat_row_payload(chat)},
            skip_sid=_skip_sid(request),
        )
        return ChatResponse(
            **strip_tool_result_bodies_from_chat_model(chat).model_dump()
        )
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# ImportChat
############################


@router.post("/import", response_model=Optional[ChatResponse])
async def import_chat(
    request: Request, form_data: ChatImportForm, user=Depends(get_verified_user)
):
    try:
        chat = await Chats.import_chat(user.id, form_data)
        if chat:
            tags = chat.meta.get("tags", [])
            for tag_id in tags:
                tag_id = tag_id.replace(" ", "_").lower()
                tag_name = " ".join([word.capitalize() for word in tag_id.split("_")])
                if (
                    tag_id != "none"
                    and await Tags.get_tag_by_name_and_user_id(tag_name, user.id)
                    is None
                ):
                    await Tags.insert_new_tag(tag_name, user.id)

            await broadcast_sidebar_event(
                user.id,
                {"type": "chat:created", "data": _chat_row_payload(chat)},
                skip_sid=_skip_sid(request),
            )

        return ChatResponse(
            **strip_tool_result_bodies_from_chat_model(chat).model_dump()
        )
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# GetChats
############################


# Per-user search single-flight state (one event loop per worker process).
_search_locks: dict[str, asyncio.Lock] = {}


def _semantic_search_text(text: str) -> str:
    """Return the part of a query worth embedding, or ``""`` for lexical-only.

    The ranker deliberately disables semantic fusion for one-token queries, so
    embedding those here only added latency and load. Keep this gate in front of
    the network call as well.
    """
    cleaned = " ".join(
        word
        for word in (text or "").split()
        if not word.lower().startswith(
            ("tag:", "folder:", "pinned:", "archived:", "shared:")
        )
    ).strip()
    semantic_tokens = cleaned.replace("-", " ").replace("/", " ").split()
    return cleaned if len(semantic_tokens) >= 2 else ""


@router.get("/search", response_model=ChatSearchResponse)
async def search_user_chats(
    text: str = "",
    page: Optional[int] = 1,
    folder_ids: list[str] = Query(default=[]),
    tag_ids: list[str] = Query(default=[]),
    pinned: Optional[bool] = None,
    archived: Optional[bool] = None,
    shared: Optional[bool] = None,
    updated_after: Optional[int] = None,
    updated_before: Optional[int] = None,
    sort: str = "relevance",
    limit: int = 30,
    user=Depends(get_verified_user),
):
    """GOATED chat search. Returns ranked hits with snippets + match counts +
    facet aggregates. `archived` defaults to None (include both archived and
    non-archived) so users can find old chats they thought were lost."""
    if page is None or page < 1:
        page = 1
    if limit < 1 or limit > 100:
        limit = 30
    skip = (page - 1) * limit

    # Embed the query (async, off the DB connection) so the lexical ranking can be
    # RRF-fused with a semantic ANN ranking. We strip the hidden prefix qualifiers
    # (tag:/folder:/...) first so the vector reflects the real intent, not the filter
    # tokens, and mirror the model's FTS routing gate (a single ASCII char never runs
    # FTS, so it gets no vector either; non-ASCII single chars do). Degrades to
    # lexical-only if the embedder is disabled/unreachable (aembed_query -> None).
    query_vector = None
    # Read the live module global (updated by the admin config bridge) so toggling
    # semantic search on/off in the admin UI takes effect without a restart.
    if ce.CHAT_SEMANTIC_ENABLED:
        cleaned = _semantic_search_text(text)
        if cleaned:
            query_vector = await aembed_query(cleaned)

    # Per-user search single-flight: each search holds a pooled DB connection for
    # its whole duration, so a fast typist firing overlapping requests (or a slow
    # query) must not be able to pin several at once. Serialize searches per user
    # so at most one runs at a time. We intentionally do NOT short-circuit
    # "superseded" requests with a blank response — a second browser tab of the
    # same user shares user.id, and returning an empty result to it would blank a
    # query that actually matches. The client already aborts its own stale
    # requests; the lock just bounds concurrency.
    lock = _search_locks.get(user.id)
    if lock is None:
        lock = _search_locks[user.id] = asyncio.Lock()

    async with lock:
        return await Chats.search_chats(
            user.id,
            text,
            folder_ids=folder_ids or None,
            tag_ids=tag_ids or None,
            pinned=pinned,
            archived=archived,
            shared=shared,
            updated_after=updated_after,
            updated_before=updated_before,
            sort=sort,
            skip=skip,
            limit=limit,
            query_vector=query_vector,
        )


############################
# GetChatsByFolderId
############################


@router.get("/folder/{folder_id}", response_model=list[ChatResponse])
async def get_chats_by_folder_id(folder_id: str, user=Depends(get_verified_user)):
    folder_ids = [folder_id]
    children_folders = await Folders.get_children_folders_by_id_and_user_id(
        folder_id, user.id
    )
    if children_folders:
        folder_ids.extend([folder.id for folder in children_folders])

    return [
        ChatResponse(**strip_tool_result_bodies_from_chat_model(chat).model_dump())
        for chat in await Chats.get_chats_by_folder_ids_and_user_id(folder_ids, user.id)
    ]


@router.get("/folder/{folder_id}/list")
async def get_chat_list_by_folder_id(
    folder_id: str, page: Optional[int] = 1, user=Depends(get_verified_user)
):
    try:
        limit = 60
        skip = (page - 1) * limit

        return [
            {"title": chat.title, "id": chat.id, "updated_at": chat.updated_at}
            for chat in await Chats.get_chats_by_folder_id_and_user_id(
                folder_id, user.id, skip=skip, limit=limit
            )
        ]

    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# GetPinnedChats
############################


@router.get("/pinned", response_model=list[ChatTitleIdResponse])
async def get_user_pinned_chats(request: Request, user=Depends(get_verified_user)):
    rows = await Chats.get_pinned_chats_by_user_id(user.id)
    content = [row.model_dump() for row in rows]
    return etag_response(content, request)


############################
# GetChats
############################


@router.get("/all", response_model=list[ChatResponse])
async def get_user_chats(user=Depends(get_verified_user)):
    # Export endpoint — needs full chat JSON.
    return [
        ChatResponse(**chat.model_dump())
        for chat in await Chats.get_chats_with_data_by_user_id(user.id)
    ]


############################
# GetArchivedChats
############################


@router.get("/all/archived", response_model=list[ChatResponse])
async def get_user_archived_chats(user=Depends(get_verified_user)):
    # Export endpoint — needs full chat JSON.
    return [
        ChatResponse(**chat.model_dump())
        for chat in await Chats.get_archived_chats_with_data_by_user_id(user.id)
    ]


############################
# GetAllTags
############################


@router.get("/all/tags")
async def get_all_user_tags(request: Request, user=Depends(get_verified_user)):
    try:
        tags = await Tags.get_tags_by_user_id(user.id)
        # Trim to the fields list consumers read (tag pickers/filters use id+name).
        # user_id is the caller's own id repeated per row and meta is ~always null;
        # on tag-heavy accounts they multiply the payload ~3x (measured 297KB->~100KB
        # for 2.4k tags) and this component dominates the boot bootstrap bundle.
        content = [{"id": tag.id, "name": tag.name} for tag in tags]
        return etag_response(content, request)
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# GetAllChatsInDB
############################


@router.get("/all/db", response_model=list[ChatResponse])
async def get_all_user_chats_in_db(user=Depends(get_admin_user)):
    if not ENABLE_ADMIN_EXPORT:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    # Admin export endpoint — needs full chat JSON.
    return [
        ChatResponse(**chat.model_dump()) for chat in await Chats.get_chats_with_data()
    ]


############################
# GetArchivedChats
############################


@router.get("/archived", response_model=list[ChatTitleIdResponse])
async def get_archived_session_user_chat_list(
    page: Optional[int] = None,
    query: Optional[str] = None,
    order_by: Optional[str] = None,
    direction: Optional[str] = None,
    user=Depends(get_verified_user),
):
    if page is None:
        page = 1

    limit = 60
    skip = (page - 1) * limit

    filter = {}
    if query:
        filter["query"] = query
    if order_by:
        filter["order_by"] = order_by
    if direction:
        filter["direction"] = direction

    chat_list = [
        ChatTitleIdResponse(**chat.model_dump())
        for chat in await Chats.get_archived_chat_list_by_user_id(
            user.id,
            filter=filter,
            skip=skip,
            limit=limit,
        )
    ]

    return chat_list


############################
# ArchiveAllChats
############################


@router.post("/archive/all", response_model=bool)
async def archive_all_chats(request: Request, user=Depends(get_verified_user)):
    result = await Chats.archive_all_chats_by_user_id(user.id)
    if result:
        await broadcast_sidebar_event(
            user.id,
            {"type": "chats:bulk", "data": {"operation": "archive_all"}},
            skip_sid=_skip_sid(request),
        )
    return result


############################
# UnarchiveAllChats
############################


@router.post("/unarchive/all", response_model=bool)
async def unarchive_all_chats(request: Request, user=Depends(get_verified_user)):
    result = await Chats.unarchive_all_chats_by_user_id(user.id)
    if result:
        await broadcast_sidebar_event(
            user.id,
            {"type": "chats:bulk", "data": {"operation": "unarchive_all"}},
            skip_sid=_skip_sid(request),
        )
    return result


############################
# GetSharedChatById
############################


@router.get("/share/{share_id}", response_model=Optional[ChatResponse])
async def get_shared_chat_by_id(share_id: str, user=Depends(get_optional_user)):
    if user and user.role == "pending":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    admin_access = bool(
        user is not None and user.role == "admin" and ENABLE_ADMIN_CHAT_ACCESS
    )
    chat = await Chats.resolve_shared_chat(share_id, admin_access=admin_access)

    if chat:
        return ChatResponse(
            **sanitize_shared_chat_model(chat, share_id=share_id).model_dump()
        )

    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.NOT_FOUND
        )


@router.get("/share/{share_id}/messages/{message_id}/tool-results/{tool_call_id}")
async def get_shared_chat_message_tool_result(
    share_id: str,
    message_id: str,
    tool_call_id: str,
    user=Depends(get_optional_user),
):
    # Mirror get_shared_chat_by_id access: anonymous users can read shared
    # chats; pending users cannot; admins with admin-chat access can use a raw
    # chat id as the share_id.
    if user and user.role == "pending":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    admin_access = bool(
        user is not None and user.role == "admin" and ENABLE_ADMIN_CHAT_ACCESS
    )
    chat = await Chats.resolve_shared_chat(share_id, admin_access=admin_access)

    if not chat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    message = await Chats.get_message_by_id_and_message_id(chat.id, message_id)
    if not message:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )

    bodies = message.get("tool_result_bodies") if isinstance(message, dict) else None
    body = bodies.get(tool_call_id) if isinstance(bodies, dict) else None
    if isinstance(body, dict):
        return body

    # Reasoning stubs share this endpoint (ref = "reasoning:{block_index}") —
    # shared views ship the same lazy blocks as the owner's chat open.
    if parse_reasoning_ref(tool_call_id) is not None:
        inline = reasoning_body_from_blocks(
            message.get("content_blocks") if isinstance(message, dict) else None,
            tool_call_id,
        )
        if inline is not None:
            return inline

    for block in message.get("content_blocks", []) if isinstance(message, dict) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_calls":
            continue
        for result in block.get("results") or []:
            if isinstance(result, dict) and result.get("tool_call_id") == tool_call_id:
                return result

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Tool result not found",
    )


@router.get("/share/{share_id}/models")
async def get_shared_chat_models(
    request: Request, share_id: str, user=Depends(get_optional_user)
):
    # Anonymous-allowed: resolve model display names for a shared chat so
    # viewers who never populated $models see friendly names instead of raw ids.
    if user and user.role == "pending":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    admin_access = bool(
        user is not None and user.role == "admin" and ENABLE_ADMIN_CHAT_ACCESS
    )
    chat = await Chats.resolve_shared_chat(share_id, admin_access=admin_access)
    if not chat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    chat_data = chat.chat if isinstance(chat.chat, dict) else {}

    ids: set = set()
    models = chat_data.get("models")
    if isinstance(models, list):
        for m in models:
            if isinstance(m, str) and m:
                ids.add(m)

    messages = []
    history = chat_data.get("history")
    if isinstance(history, dict) and isinstance(history.get("messages"), dict):
        messages.extend(history["messages"].values())
    if isinstance(chat_data.get("messages"), list):
        messages.extend(chat_data["messages"])

    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("model"), str) and m.get("model"):
            ids.add(m["model"])

    # Resolve display names the same way the normal UI does: prefer the app's
    # merged model cache (base + custom models, exactly what /api/models
    # surfaces), fall back to the persisted model row, then the raw id. The
    # cache may be empty on a freshly-booted server that has not served
    # /api/models yet — the DB/id fallbacks keep this correct if so. We also
    # carry the model's profile image (non-sensitive, shown app-wide) so the
    # shared view renders the real avatar next to the resolved name.
    app_models = getattr(request.app.state, "MODELS", None) or {}

    def _profile_image(source) -> Optional[str]:
        if not isinstance(source, dict):
            return None
        return ((source.get("info") or {}).get("meta") or {}).get(
            "profile_image_url"
        ) or (source.get("meta") or {}).get("profile_image_url")

    result = []
    for model_id in ids:
        name = None
        image = None
        cached = app_models.get(model_id)
        if isinstance(cached, dict):
            name = cached.get("name")
            image = _profile_image(cached)
        if not name or not image:
            model = await Models.get_model_by_id(model_id)
            if model:
                name = name or model.name
                # model.meta is a ModelMeta pydantic object on the model row
                # (dict only in legacy/raw paths) — support both.
                meta = model.meta
                if isinstance(meta, dict):
                    image = image or meta.get("profile_image_url")
                elif meta is not None:
                    image = image or getattr(meta, "profile_image_url", None)
        entry = {"id": model_id, "name": name or model_id}
        if image:
            entry["info"] = {"meta": {"profile_image_url": image}}
        result.append(entry)

    return result


############################
# GetChatsByTags
############################


class TagForm(BaseModel):
    name: str


class TagFilterForm(TagForm):
    skip: Optional[int] = 0
    limit: Optional[int] = 50


@router.post("/tags", response_model=list[ChatTitleIdResponse])
async def get_user_chat_list_by_tag_name(
    form_data: TagFilterForm, user=Depends(get_verified_user)
):
    chats = await Chats.get_chat_list_by_user_id_and_tag_name(
        user.id, form_data.name, form_data.skip, form_data.limit
    )
    if len(chats) == 0:
        await Tags.delete_tag_by_name_and_user_id(form_data.name, user.id)

    return chats


############################
# GetChatById
############################


async def _chat_has_active_work(request: Request, chat_id: str, draining: bool) -> bool:
    """True when the chat has live work whose writes may not be visible in the
    chat row's xmin yet: streaming checkpoints land between row writes, and the
    migrated-chat tool-result-body / subagent-run / status writers touch ONLY
    chat_message. A conditional open must never answer 304 in that window.

    Ordered cheapest-first; any hit (or any doubt) forces the full 200.

    NOTE: the stream index (and the redis=None task fallback) are per-worker
    in-process state — this predicate assumes the single-worker deployment.
    A multi-worker future needs Redis-backed tasks (already supported here)
    AND a cross-worker stream signal before conditional opens stay airtight.
    """
    if draining:
        return True
    try:
        if get_active_streams_for_chat(chat_id):
            return True
    except Exception:
        return True  # can't prove quiescence — serve the full body
    try:
        from open_webui.tasks import collect_chat_work_state

        redis = getattr(request.app.state, "redis", None)
        # `draining` was already proven falsy by the early return above — pass it
        # in so this conditional-open probe doesn't pay for a redundant row read.
        work_state = await collect_chat_work_state(redis, chat_id, draining=None)
        if work_state["generations"] or work_state["rerun_task_ids"]:
            return True
    except Exception:
        return True
    return False


class BrowserHandoffForm(BaseModel):
    # Mirrors CAM's HUMAN_ACTION_SPECS (src/cam/browser/router.py): the panel
    # sends raw gestures (click / drag / scroll / type / snapshot / dismiss);
    # required fields are enforced there AND here so a bad payload fails fast.
    session: str = Field(default="main", min_length=1, max_length=128)
    action: Literal["click", "drag", "scroll", "type", "snapshot", "dismiss"]
    x: Optional[float] = None
    y: Optional[float] = None
    x2: Optional[float] = None
    y2: Optional[float] = None
    delta_x: Optional[float] = None
    delta_y: Optional[float] = None
    text: Optional[str] = None

    @model_validator(mode="after")
    def validate_action_fields(self):
        required = {
            "click": ("x", "y"),
            "drag": ("x", "y", "x2", "y2"),
            "scroll": ("delta_y",),
            "type": ("text",),
        }.get(self.action, ())
        missing = [
            field for field in required if getattr(self, field, None) in (None, "")
        ]
        if missing:
            raise ValueError(
                f"{', '.join(missing)} required for action '{self.action}'"
            )
        return self


@router.post("/{id}/browser/handoff")
async def browser_handoff(
    request: Request,
    id: str,
    form_data: BrowserHandoffForm,
    user=Depends(get_verified_user),
):
    """Proxy a human-only browser-panel action to the configured CAM server."""
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if not chat:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    # Per-action required fields are enforced by BrowserHandoffForm's validator.

    server_id = str(request.app.state.config.CONTAINER_MCP_SERVER_ID or "").strip()
    base = await _container_connection_url(request, server_id)
    if not base:
        raise HTTPException(status_code=503, detail="Container browser is not configured")
    base = base.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    endpoint = f"{base}/browser/handoff/{quote(id, safe='')}"

    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                endpoint, json=form_data.model_dump(exclude_none=True)
            ) as response:
                try:
                    data = await response.json()
                except Exception:
                    data = {"detail": (await response.text())[:500]}
                if response.status >= 400:
                    raise HTTPException(
                        status_code=response.status,
                        detail=data.get("detail", "Container browser handoff failed"),
                    )
                return data
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("container browser handoff failed")
        raise HTTPException(
            status_code=502, detail=f"Container browser handoff failed: {exc}"
        ) from exc


@router.get("/{id}")
async def get_chat_by_id(
    request: Request,
    response: Response,
    id: str,
    meta_only: bool = False,
    include_tail: Optional[int] = None,
    tail_manifest: bool = False,
    user=Depends(get_verified_user),
):
    if meta_only:
        # Conditional open (tail contract only): a client holding a stored copy
        # sends If-None-Match with the opaque ETag it captured earlier; when the
        # row version still matches AND no live work is in flight, answer 304
        # and skip the expensive path entirely (full blob hydrate + sibling-stub
        # scan + branch walk + tags read). The validator is a pure optimization:
        # None falls through to the authoritative path below, which stays the
        # sole 401 authority — a deleted/revoked chat can never 304.
        etag = None
        if include_tail is not None:
            v = await Chats.get_chat_open_validator(id, user.id)
            if v is not None:
                etag = f'W/"v1-{v["xmin"]}-{v["updated_at"]}-{v["current_id"] or ""}"'
                inm = {
                    p.strip()
                    for p in (request.headers.get("if-none-match") or "").split(",")
                    if p.strip()
                }
                # Exact-match compare (no quote-stripping): weak ETags round-trip
                # verbatim; etag_response's strip('"') would mangle the W/ prefix.
                if etag in inm and not await _chat_has_active_work(
                    request, id, v["draining"]
                ):
                    return Response(
                        status_code=status.HTTP_304_NOT_MODIFIED,
                        headers={"ETag": etag, "Cache-Control": "private, no-store"},
                    )

        meta = await Chats.get_chat_meta_by_id_and_user_id(id, user.id)
        if meta is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )
        # Contract 2: consolidated chat open. Embed the branch page + tags that
        # the client would otherwise fetch in two follow-up requests, reusing the
        # exact code paths of the dedicated endpoints (no projection fork).
        #
        # Contract 3 (incremental open): a client holding a stored copy whose
        # messages carry `_rev` row versions asks for `tail_manifest` — the
        # branch window as lean [{id, parentId, role, rev}] rows instead of
        # bodies. It then diffs locally and batch-fetches only changed rows via
        # /messages/by-ids. Legacy blob chats have no rows to version, so the
        # manifest is None and we serve the full branch exactly as Contract 2.
        if include_tail is not None:
            tail = max(1, min(int(include_tail), 60))
            current_id = (meta.get("history") or {}).get("currentId")
            work_state = None
            try:
                from open_webui.tasks import collect_chat_work_state
                from open_webui.utils.subagent import (
                    reconcile_stranded_subagent_runs_by_chat_id,
                )

                redis = getattr(request.app.state, "redis", None)
                work_state = await collect_chat_work_state(redis, id)
                healed = await reconcile_stranded_subagent_runs_by_chat_id(
                    id,
                    parent_live=bool(work_state["generations"]),
                    live_rerun_entry_keys=work_state["subagent_rerun_entry_keys"],
                    user_id=user.id,
                )
                if healed:
                    # Reconciliation changed one or more message rows and
                    # rotated the chat validator. Refresh metadata before
                    # constructing the branch/manifest so this very response
                    # carries the repaired state and its new ETag.
                    refreshed_meta = await Chats.get_chat_meta_by_id_and_user_id(
                        id, user.id
                    )
                    if refreshed_meta is not None:
                        meta = refreshed_meta
                        current_id = (meta.get("history") or {}).get("currentId")
                    refreshed_validator = await Chats.get_chat_open_validator(
                        id, user.id
                    )
                    if refreshed_validator is not None:
                        etag = (
                            f'W/"v1-{refreshed_validator["xmin"]}-'
                            f'{refreshed_validator["updated_at"]}-'
                            f'{refreshed_validator["current_id"] or ""}"'
                        )
            except Exception:
                log.exception("chat-open stranded subagent reconcile failed for %s", id)

            manifest = None
            if tail_manifest and current_id:
                manifest = await Chats.get_chat_messages_branch_manifest(
                    id, current_id, limit=tail
                )
            if tail_manifest and (manifest is not None or not current_id):
                meta["branch_manifest"] = manifest or []
            else:
                meta["branch"] = (
                    await get_chat_messages_paginated(
                        id, leaf=current_id, limit=tail, user=user
                    )
                    if current_id
                    else []
                )
            meta["tags"] = await get_chat_tags_by_id(id, user=user)
            # Contract 2 continued: embed the live task/stream state the client
            # would otherwise fetch in TWO follow-up round-trips (task ids +
            # active streams) — on high-RTT links each serialized request adds
            # a full RTT to the working-state reconcile. Same sources as the
            # dedicated endpoints (no projection fork). On any failure the key
            # is simply omitted and the client falls back to those endpoints.
            # The 304 path needs no equivalent: _chat_has_active_work forces a
            # full 200 whenever live work exists, so a 304 IS the authoritative
            # "no active tasks, no active streams" answer.
            try:
                from open_webui.routers.streams import collect_active_streams
                from open_webui.tasks import collect_chat_work_state

                redis = getattr(request.app.state, "redis", None)
                if work_state is None:
                    work_state = await collect_chat_work_state(redis, id)
                meta["active"] = {
                    **work_state,
                    "streams": await collect_active_streams(id),
                }
            except Exception:
                log.debug(
                    "chat-open active-state embed failed for %s", id, exc_info=True
                )
        if etag is not None:
            # Hand the client the validator for its next open; no-store keeps
            # the browser HTTP cache out of the loop (the app owns caching).
            response.headers["ETag"] = etag
            response.headers["Cache-Control"] = "private, no-store"
        return meta

    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)

    if chat:
        return ChatResponse(
            **strip_tool_result_bodies_from_chat_model(chat).model_dump()
        )

    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.NOT_FOUND
        )


############################
# UpdateChatById
############################


@router.post("/{id}", response_model=Optional[ChatResponse])
async def update_chat_by_id(
    request: Request, id: str, form_data: ChatForm, user=Depends(get_verified_user)
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        prior_title = chat.title
        # The sidebar rename sends only {title}. Route that through the O(1)
        # targeted writer: the generic whole-chat update hydrates every message
        # and can later DELETE/INSERT that stale snapshot while a generation is
        # appending a sibling. A title change must never touch conversation rows.
        if set(form_data.chat.keys()) == {"title"} and isinstance(
            form_data.chat.get("title"), str
        ):
            chat = await Chats.update_chat_title_by_id(id, form_data.chat["title"])
        else:
            incoming_history = form_data.chat.get("history")
            if (
                chat.messages_migrated
                and isinstance(incoming_history, dict)
                and "messages" in incoming_history
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Migrated chat history is append-only. Use message PATCH "
                        "operations instead of replacing history.messages."
                    ),
                )

            updated_chat = {**chat.chat, **form_data.chat}
            if chat.messages_migrated:
                # A hydrated messages map is a read projection, never mutable
                # chat metadata. Remove it unconditionally at this boundary.
                stored_history = updated_chat.get("history")
                if isinstance(stored_history, dict) and "messages" in stored_history:
                    updated_chat = {
                        **updated_chat,
                        "history": {
                            key: value
                            for key, value in stored_history.items()
                            if key != "messages"
                        },
                    }
            chat = await Chats.update_chat_by_id(id, updated_chat)
        if chat is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The chat changed while it was being updated.",
            )
        if chat and chat.title != prior_title:
            await broadcast_sidebar_event(
                user.id,
                {
                    "type": "chat:renamed",
                    "data": _chat_row_payload(chat),
                },
                skip_sid=_skip_sid(request),
            )
        return ChatResponse(
            **strip_tool_result_bodies_from_chat_model(chat).model_dump()
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )


############################
# GetChatMessagesPaginated
############################


@router.get("/{id}/messages")
async def get_chat_messages_paginated(
    id: str,
    skip: int = 0,
    limit: Optional[int] = None,
    leaf: Optional[str] = None,
    before: Optional[str] = None,
    current_leaf: Optional[str] = None,
    slim: bool = True,
    user=Depends(get_verified_user),
):
    """Paginated message list for a single chat.

    Two modes:

    1. ``?leaf=<msg_id>`` (optionally with ``before=<msg_id>`` and ``limit``) —
       branch-aware ancestor pagination. Walks ``parent_id`` from leaf to root
       and returns the last ``limit`` ancestors (oldest-first), or the
       ``limit`` ancestors immediately older than ``before`` for upward scroll.
       Default ``limit`` is 7 when ``leaf`` is set.

    2. ``?skip=&limit=`` — legacy offset/limit pagination over the full
       chat_message table, used by existing callers (admin, export). Default
       ``limit`` is 100.

    Reads directly from the chat_message table for migrated chats so we
    don't have to ship the entire 100+ MB JSON blob over the wire just to
    render a window of messages. Falls back to JSON slicing for unmigrated
    chats so the response shape is identical regardless of storage path.

    ``slim=true`` (default) strips bandwidth-heavy per-message fields.
    Large web tool bodies are always fetched through the dedicated lazy
    endpoint; ``slim=false`` still omits ``tool_result_bodies``.
    """
    # Lightweight ownership check — avoids hydrating the whole message tree
    # (which would defeat the entire point of paginating).
    if not await Chats.user_owns_chat(id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if leaf is not None:
        branch_limit = limit if limit and limit > 0 else 7
        messages = await Chats.get_chat_messages_branch(
            id, leaf, before_message_id=before, limit=branch_limit
        )
    else:
        offset_limit = limit if limit and limit > 0 else 100
        messages = await Chats.get_chat_messages_paginated(
            id, skip=skip, limit=offset_limit
        )

    if not slim:
        return [strip_tool_result_bodies_from_message(m) for m in messages]

    leaf_for_projection = current_leaf or leaf
    return [
        _project_message_slim(
            m,
            is_current_leaf=(
                leaf_for_projection is not None and m.get("id") == leaf_for_projection
            ),
            is_current_branch=True,
        )
        for m in messages
        if isinstance(m, dict)
    ]


# Loaded only when the user opens Chat Overview. Unlike sibling_stubs (needed
# on every open), this returns short previews for every preserved branch node.
# Keep it before /messages/{message_id}/... so "overview" is never captured as
# a message id by a future dynamic route.
@router.get("/{id}/messages/overview")
async def get_chat_messages_overview(
    id: str,
    user=Depends(get_verified_user),
):
    if not await Chats.user_owns_chat(id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    return await Chats.get_chat_messages_overview(id)


# Declared BEFORE the /{id}/messages/{message_id}/... routes so "by-ids" can
# never be captured as a message_id path segment.
@router.get("/{id}/messages/by-ids")
async def get_chat_messages_by_ids(
    id: str,
    ids: str,
    leaf: Optional[str] = None,
    user=Depends(get_verified_user),
):
    """Batch message fetch — the incremental open's second round-trip.

    After diffing the ``tail_manifest`` against its stored copy's ``_rev``
    versions, the client downloads ONLY the changed/missing rows here (slim
    projection, same as the tail). ``leaf`` marks the current leaf so its
    reasoning_details replay context ships. Capped at the tail window size.
    """
    if not await Chats.user_owns_chat(id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    id_list = [s for s in (ids or "").split(",") if s][:60]
    messages = await Chats.get_messages_by_ids(id, id_list)
    return [
        _project_message_slim(
            m,
            is_current_leaf=(leaf is not None and m.get("id") == leaf),
            is_current_branch=True,
        )
        for m in messages
        if isinstance(m, dict)
    ]


############################
# GetChatMessageSiblings
############################


@router.get("/{id}/messages/{message_id}/tool-results/{tool_call_id}")
async def get_chat_message_tool_result(
    id: str,
    message_id: str,
    tool_call_id: str,
    user=Depends(get_verified_user),
):
    """Fetch a large lazy message body when the user expands a collapsed card.

    Serves BOTH body classes of the lazy contract (utils/lazy_blocks.py):
    - tool results (``tool_call_id`` is a real tool call id): stream-v2.1 keeps
      large bodies out of socket snapshots and message content_blocks;
    - reasoning text (``tool_call_id`` is a ``reasoning:{block_index}`` ref):
      closed reasoning blocks persist as "Thought for N seconds" stubs.

    Resolution order for either class: live in-memory stream state while the
    generation is active → the persisted message's ``tool_result_bodies`` map →
    inline block content (rows persisted before the canonical slim form).
    """
    if not await Chats.user_owns_chat(id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    message = await Chats.get_message_by_id_and_message_id(id, message_id)
    if not isinstance(message, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )

    is_reasoning_ref = parse_reasoning_ref(tool_call_id) is not None

    live_state = get_stream_state(message_id)
    if live_state:
        if live_state.get("chat_id") != id or live_state.get("user_id") != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tool result not found",
            )
        if is_reasoning_ref:
            # The live stream state keeps reasoning text in full — resolve the
            # ref against its blocks so an expand during generation never 404s.
            live_body = reasoning_body_from_blocks(
                live_state.get("content_blocks"), tool_call_id
            )
        else:
            live_body = get_tool_result_body(message_id, tool_call_id)
        if isinstance(live_body, dict):
            return live_body
    bodies = message.get("tool_result_bodies") if isinstance(message, dict) else None
    body = bodies.get(tool_call_id) if isinstance(bodies, dict) else None
    if isinstance(body, dict):
        return body

    # Backward compatibility: old rows may still have full bodies inline.
    if is_reasoning_ref:
        inline = reasoning_body_from_blocks(message.get("content_blocks"), tool_call_id)
        if inline is not None:
            return inline
    for block in message.get("content_blocks", []) if isinstance(message, dict) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_calls":
            continue
        for result in block.get("results") or []:
            if isinstance(result, dict) and result.get("tool_call_id") == tool_call_id:
                return result

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Tool result not found",
    )


class CompactForm(BaseModel):
    model: Optional[str] = None
    leaf_id: Optional[str] = None


@router.post("/{id}/compact")
async def compact_chat(
    id: str,
    request: Request,
    form_data: CompactForm,
    user=Depends(get_verified_user),
):
    """The `/compact` command, for a chat at rest.

    The in-flight cases do NOT come through here — a `/compact` typed while the
    model is working is a steer (consumed at the tool-round boundary) or a queued
    item (consumed by the drain), because both must land at a point where cutting
    is safe. This endpoint is only the idle path, where the branch is quiescent
    and the cut can be taken immediately.
    """
    from open_webui.utils.compaction import (
        CompactionError,
        NothingToCompactError,
        compact_chat_now,
    )

    if not await Chats.user_owns_chat(id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    model = None
    if form_data.model:
        models = request.app.state.MODELS or {}
        model = models.get(form_data.model)
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A model is required to summarize the conversation",
        )

    try:
        result = await compact_chat_now(
            request,
            user,
            chat_id=id,
            model=model,
            leaf_id=form_data.leaf_id,
        )
    except NothingToCompactError:
        # A no-op, not a failure: everything on this branch is already behind an
        # anchor. Distinguished from an error so the UI can say so plainly.
        return {"compacted": False, "reason": "nothing_to_compact"}
    except CompactionError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e

    return {"compacted": True, **result}


@router.get("/{id}/messages/{message_id}/compaction/{block_index}")
async def get_chat_message_compaction(
    id: str,
    message_id: str,
    block_index: int,
    user=Depends(get_verified_user),
):
    """Render the ``<compacted_context>`` a ``compaction`` block stands for.

    COMPACTION.md §8: the divider expands to show the full compacted context. We
    author that summary ourselves, so unlike OpenAI's opaque ``cmp_*`` items it is
    fully human-readable — "you cannot read the compressed output to verify what
    was preserved" is exactly what Factory criticised about theirs.

    Serves the bytes that were ACTUALLY sent: the outbound path records the
    assembled envelope back onto the anchor at the HTTP boundary
    (``capture_compaction_envelope``), so what comes back here is a copy of the
    wire payload, not a reconstruction of it.

    ``source: "rendered"`` is the fallback for an anchor written before that
    capture existed, or one whose turn never reached the wire. It re-renders from
    the same generators over the ancestor chain, which is close but not
    guaranteed identical — the send path folds attached-file text into user
    message content and can inherit a carried instruction list, neither of which
    a tree walk reproduces. The response says which one it is; the UI must too.
    """
    from open_webui.utils.chat import _walk_messages_from_leaf
    from open_webui.utils.compaction import (
        collect_tool_index,
        collect_user_instructions,
        is_compaction_block,
        render_compacted_context,
    )

    if not await Chats.user_owns_chat(id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    messages_map = await Chats.get_messages_map_by_chat_id(id) or {}
    chain = _walk_messages_from_leaf(messages_map, message_id)
    if not chain:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Message not found"
        )

    blocks = chain[-1].get("content_blocks") or []
    if block_index < 0 or block_index >= len(blocks):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Block not found"
        )
    block = blocks[block_index]
    if not is_compaction_block(block):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not a compaction block"
        )

    sent = block.get("envelope")
    if isinstance(sent, str) and sent:
        compacted_context, source = sent, "sent"
    else:
        # Everything before the anchor: the ancestor messages, plus this
        # message's own blocks up to the anchor.
        before = list(chain[:-1])
        if block_index:
            before.append({**chain[-1], "content_blocks": blocks[:block_index]})
        compacted_context = render_compacted_context(
            narrative=block.get("narrative") or "",
            compacted_at=block.get("compacted_at") or "",
            instructions=collect_user_instructions(before),
            tool_index=collect_tool_index(before),
        )
        source = "rendered"

    return {
        "compacted_at": block.get("compacted_at"),
        "covers": block.get("covers"),
        "tokens": block.get("tokens"),
        "context_length": block.get("context_length"),
        "narrative": block.get("narrative") or "",
        "compacted_context": compacted_context,
        "source": source,
    }


@router.get("/{id}/messages/{message_id}/siblings")
async def get_chat_message_siblings(
    id: str,
    message_id: str,
    user=Depends(get_verified_user),
):
    """Return the messages that share a parent with ``message_id`` (including
    ``message_id`` itself). Used by the frontend's branch-switch lazy-load path
    (prev/next arrows) and by the rewind/retry context hydrator.

    Served leaf-slim: content_blocks follow the lazy contract (tool/reasoning
    bodies fetch on expand — a branch switch must not re-download megabytes of
    collapsed content), while the ``reasoning_details`` replay-context fields
    are KEPT (``is_current_leaf=True``): any sibling is a potential rebase
    target for rewind/retry, and this endpoint is exactly where the client
    hydrates that context from on demand.
    """
    if not await Chats.user_owns_chat(id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    return [
        _project_message_slim(m, is_current_leaf=True, is_current_branch=True)
        for m in await Chats.get_message_siblings(id, message_id)
    ]


############################
# UpdateChatMessageById
############################
class MessageForm(BaseModel):
    content: str


@router.post("/{id}/messages/{message_id}", response_model=Optional[ChatResponse])
async def update_chat_message_by_id(
    id: str, message_id: str, form_data: MessageForm, user=Depends(get_verified_user)
):
    chat = await Chats.get_chat_by_id(id)

    if not chat:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    if chat.user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    # Historical API compatibility: this endpoint used to overwrite the source
    # row in place. Route it through the same append-only storage primitive as
    # the current editor so no caller can bypass version preservation.
    version_message_id = str(uuid4())
    try:
        await Chats.fork_message_version_atomic(
            id,
            message_id,
            version_message_id,
            content=form_data.content,
            user_id=user.id if chat.user_id == user.id else None,
        )
    except (ChatBranchConflictError, ChatMessageParentMissingError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(error),
                "code": getattr(error, "code", "message_version_conflict"),
            },
        ) from error

    chat = await Chats.get_chat_by_id(id)
    if chat is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The edited chat no longer exists.",
        )

    event_emitter = get_event_emitter(
        {
            "user_id": user.id,
            "chat_id": id,
            "message_id": version_message_id,
        },
        False,
    )

    if event_emitter:
        await event_emitter(
            {
                "type": "chat:message",
                "data": {
                    "chat_id": id,
                    "message_id": version_message_id,
                    "content": form_data.content,
                },
            }
        )

    return ChatResponse(**strip_tool_result_bodies_from_chat_model(chat).model_dump())


############################
# SendChatMessageEventById
############################
class EventForm(BaseModel):
    type: str
    data: dict


@router.post("/{id}/messages/{message_id}/event", response_model=Optional[bool])
async def send_chat_message_event_by_id(
    id: str, message_id: str, form_data: EventForm, user=Depends(get_verified_user)
):
    chat = await Chats.get_chat_by_id(id)

    if not chat:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    if chat.user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    event_emitter = get_event_emitter(
        {
            "user_id": user.id,
            "chat_id": id,
            "message_id": message_id,
        }
    )

    try:
        if event_emitter:
            await event_emitter(form_data.model_dump())
        else:
            return False
        return True
    except:
        return False


############################
# DeleteChatById
############################


async def _release_chat_mutation_block(
    request: Request, chat_id: str, block_token: str
) -> None:
    """Release one destructive-mutation admission barrier, even if its HTTP
    owner is being cancelled.

    Message-graph deletion and whole-chat deletion share the same writer
    quiescence protocol.  Keeping the release in one place prevents one path
    from leaking a chat-global work block while another path handles request
    cancellation correctly.
    """
    if not block_token:
        return
    try:
        from open_webui.tasks import release_chat_work_block

        await asyncio.shield(
            release_chat_work_block(
                getattr(request.app.state, "redis", None),
                chat_id,
                block_token,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("releasing mutation barrier for chat %s failed", chat_id)


async def _acquire_quiescent_chat_mutation_block(
    request: Request, chat_id: str
) -> str:
    """Close chat-work admission, cancel every existing writer, and wait for
    its terminal DB write before allowing a destructive graph mutation.

    The barrier is acquired *before* work discovery, closing both races:
    already-registered work is latched and stopped, while a completion or
    detached rerun still in pre-registration observes the barrier at its start
    gate.  The caller owns the returned token and must release it when the chat
    itself survives the mutation.
    """
    from open_webui.tasks import (
        acquire_chat_work_block,
        latch_generation_cancellation,
        list_generation_operations_by_item,
        list_item_task_ids_by_prefix,
        stop_tasks_and_wait,
    )

    redis = getattr(request.app.state, "redis", None)
    block_token = await acquire_chat_work_block(redis, chat_id)
    try:
        generation_operations = await list_generation_operations_by_item(
            redis, chat_id
        )
        generation_operations = await latch_generation_cancellation(
            redis,
            chat_id,
            generation_ids=(
                operation["generation_id"] for operation in generation_operations
            ),
            turn_ids=(operation["turn_id"] for operation in generation_operations),
        )
        task_ids = [
            *(
                operation["task_id"]
                for operation in generation_operations
                if operation.get("task_id")
            ),
            *(
                await list_item_task_ids_by_prefix(
                    redis, f"subagent-rerun:{chat_id}:"
                )
            ),
        ]
        remaining = await stop_tasks_and_wait(redis, task_ids, timeout=30.0)
        if remaining:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "The chat still has running work.",
                    "code": "chat_work_not_quiescent",
                    "task_ids": remaining,
                },
            )

        # A pre-bind completion has no task id yet.  Its cancellation latch and
        # the admission barrier make it unwind; wait for operation cleanup as
        # the acknowledgement before entering the graph transaction.
        deadline = asyncio.get_running_loop().time() + 2.0
        while True:
            remaining_operations = await list_generation_operations_by_item(
                redis, chat_id
            )
            if not remaining_operations:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "message": "The chat still has pending generation work.",
                        "code": "chat_work_not_quiescent",
                        "generation_ids": [
                            operation["generation_id"]
                            for operation in remaining_operations
                            if operation.get("generation_id")
                        ],
                    },
                )
            await asyncio.sleep(0.05)
    except BaseException:
        await _release_chat_mutation_block(request, chat_id, block_token)
        raise
    return block_token


@router.delete("/{id}", response_model=bool)
async def delete_chat_by_id(request: Request, id: str, user=Depends(get_verified_user)):
    async def _stop_then_delete(delete_call):
        block_token = await _acquire_quiescent_chat_mutation_block(request, id)
        try:
            result = await delete_call()
        except BaseException:
            await _release_chat_mutation_block(request, id, block_token)
            raise
        if not result:
            await _release_chat_mutation_block(request, id, block_token)
        return result

    if user.role == "admin":
        chat = await Chats.get_chat_by_id(id)
        payload = _chat_row_payload(chat) if chat else {"id": id}
        for tag in ((chat.meta if chat else {}) or {}).get("tags", []):
            if await Chats.count_chats_by_tag_name_and_user_id(tag, user.id) == 1:
                await Tags.delete_tag_by_name_and_user_id(tag, user.id)

        result = await _stop_then_delete(lambda: Chats.delete_chat_by_id(id))

        if result:
            await broadcast_sidebar_event(
                user.id,
                {"type": "chat:deleted", "data": payload},
                skip_sid=_skip_sid(request),
            )

        return result
    else:
        if not has_permission(
            user.id, "chat.delete", request.app.state.config.USER_PERMISSIONS
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )

        chat = await Chats.get_chat_by_id(id)
        payload = _chat_row_payload(chat) if chat else {"id": id}
        for tag in ((chat.meta if chat else {}) or {}).get("tags", []):
            if await Chats.count_chats_by_tag_name_and_user_id(tag, user.id) == 1:
                await Tags.delete_tag_by_name_and_user_id(tag, user.id)

        # Only tear down generations for a chat THIS user owns — `chat` is fetched
        # unscoped above, but the delete below is ownership-scoped, so gate the
        # task-stop the same way (a delete-permitted user must not be able to stop
        # another user's in-flight generation by passing its id).
        if chat is not None and getattr(chat, "user_id", None) == user.id:
            result = await _stop_then_delete(
                lambda: Chats.delete_chat_by_id_and_user_id(id, user.id)
            )
        else:
            result = await Chats.delete_chat_by_id_and_user_id(id, user.id)
        if result:
            await broadcast_sidebar_event(
                user.id,
                {"type": "chat:deleted", "data": payload},
                skip_sid=_skip_sid(request),
            )
        return result


############################
# GetPinnedStatusById
############################


@router.get("/{id}/pinned", response_model=Optional[bool])
async def get_pinned_status_by_id(id: str, user=Depends(get_verified_user)):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        return chat.pinned
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# PinChatById
############################


@router.post("/{id}/pin", response_model=Optional[ChatResponse])
async def pin_chat_by_id(request: Request, id: str, user=Depends(get_verified_user)):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        chat = await Chats.toggle_chat_pinned_by_id(id)
        if chat:
            await broadcast_sidebar_event(
                user.id,
                {
                    "type": "chat:pinned",
                    "data": _chat_row_payload(chat),
                },
                skip_sid=_skip_sid(request),
            )
        return chat
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# CloneChat
############################


class CloneForm(BaseModel):
    title: Optional[str] = None


@router.post("/{id}/clone", response_model=Optional[ChatResponse])
async def clone_chat_by_id(
    request: Request, form_data: CloneForm, id: str, user=Depends(get_verified_user)
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        updated_chat = {
            **chat.chat,
            "originalChatId": chat.id,
            "branchPointMessageId": chat.chat["history"]["currentId"],
            "title": form_data.title if form_data.title else f"Clone of {chat.title}",
        }

        chat = await Chats.import_chat(
            user.id,
            ChatImportForm(
                **{
                    "chat": updated_chat,
                    "meta": chat.meta,
                    "pinned": chat.pinned,
                    "folder_id": chat.folder_id,
                }
            ),
        )

        if chat:
            await broadcast_sidebar_event(
                user.id,
                {"type": "chat:created", "data": _chat_row_payload(chat)},
                skip_sid=_skip_sid(request),
            )

        return ChatResponse(
            **strip_tool_result_bodies_from_chat_model(chat).model_dump()
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# CloneSharedChatById
############################


@router.post("/{id}/clone/shared", response_model=Optional[ChatResponse])
async def clone_shared_chat_by_id(
    request: Request, id: str, user=Depends(get_verified_user)
):

    # Clone EXACTLY what the viewer sees: resolve the share the same way the
    # shared-view GET does (snapshot -> frozen clone, live -> live original),
    # keyed off the URL share_id. Using the resolved chat's own id here would
    # either 401 (live: real chat id is not a share_id) or leak the un-frozen
    # live original (snapshot). admin_access preserves raw-chat-id cloning.
    chat = await Chats.resolve_shared_chat(
        id,
        admin_access=(user.role == "admin" and ENABLE_ADMIN_CHAT_ACCESS),
    )

    if chat:
        chat_data = chat.chat if isinstance(chat.chat, dict) else {}
        history = chat_data.get("history") if isinstance(chat_data, dict) else None
        branch_point = history.get("currentId") if isinstance(history, dict) else None
        updated_chat = {
            **chat_data,
            "originalChatId": chat.id,
            "branchPointMessageId": branch_point,
            "title": f"Clone of {chat.title}",
        }

        # Do not carry the original owner's private organizational tags into the
        # cloning viewer's own chat.
        clone_meta = {k: v for k, v in (chat.meta or {}).items() if k != "tags"}

        chat = await Chats.import_chat(
            user.id,
            ChatImportForm(
                **{
                    "chat": updated_chat,
                    "meta": clone_meta,
                    "pinned": chat.pinned,
                    "folder_id": chat.folder_id,
                }
            ),
        )
        if chat:
            await broadcast_sidebar_event(
                user.id,
                {"type": "chat:created", "data": _chat_row_payload(chat)},
                skip_sid=_skip_sid(request),
            )
        return ChatResponse(
            **strip_tool_result_bodies_from_chat_model(chat).model_dump()
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# ArchiveChat
############################


@router.post("/{id}/archive", response_model=Optional[ChatResponse])
async def archive_chat_by_id(
    request: Request, id: str, user=Depends(get_verified_user)
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        chat = await Chats.toggle_chat_archive_by_id(id)

        # Delete tags if chat is archived
        if chat.archived:
            for tag_id in chat.meta.get("tags", []):
                if (
                    await Chats.count_chats_by_tag_name_and_user_id(tag_id, user.id)
                    == 0
                ):
                    log.debug(f"deleting tag: {tag_id}")
                    await Tags.delete_tag_by_name_and_user_id(tag_id, user.id)
        else:
            for tag_id in chat.meta.get("tags", []):
                tag = await Tags.get_tag_by_name_and_user_id(tag_id, user.id)
                if tag is None:
                    log.debug(f"inserting tag: {tag_id}")
                    tag = await Tags.insert_new_tag(tag_id, user.id)

        await broadcast_sidebar_event(
            user.id,
            {
                "type": "chat:archived",
                "data": _chat_row_payload(chat),
            },
            skip_sid=_skip_sid(request),
        )

        return ChatResponse(
            **strip_tool_result_bodies_from_chat_model(chat).model_dump()
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# ShareChatById
############################


class ShareChatForm(BaseModel):
    mode: Optional[str] = None


@router.post("/{id}/share", response_model=Optional[ChatResponse])
async def share_chat_by_id(
    request: Request,
    id: str,
    form_data: Optional[ShareChatForm] = None,
    user=Depends(get_verified_user),
):
    if (user.role != "admin") and (
        not has_permission(
            user.id, "chat.share", request.app.state.config.USER_PERMISSIONS
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)

    if not chat:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    # Determine the effective share mode. An explicit valid mode in the body
    # wins; otherwise keep the existing mode (default 'snapshot' when absent).
    existing_mode = ((chat.meta or {}).get("share") or {}).get("mode") or "snapshot"
    requested_mode = form_data.mode if form_data else None
    effective_mode = (
        requested_mode if requested_mode in ("snapshot", "live") else existing_mode
    )

    # Persist the mode on the ORIGINAL chat BEFORE (re)building the clone so
    # the refreshed clone's meta carries the new share.mode.
    await Chats.update_chat_share_mode(chat.id, effective_mode)

    if chat.share_id:
        shared_chat = await Chats.update_shared_chat_by_chat_id(chat.id)
    else:
        shared_chat = await Chats.insert_shared_chat_by_chat_id(chat.id)

    if not shared_chat:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ERROR_MESSAGES.DEFAULT(),
        )

    return ChatResponse(
        **strip_tool_result_bodies_from_chat_model(shared_chat).model_dump()
    )


############################
# DeletedSharedChatById
############################


@router.delete("/{id}/share", response_model=Optional[bool])
async def delete_shared_chat_by_id(id: str, user=Depends(get_verified_user)):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        if not chat.share_id:
            return False

        # Clear the original's share_id FIRST so that if the clone delete then
        # fails, the link is already dead (get_chat_by_share_id finds nothing)
        # rather than left pointing at a clone-less share that would resolve to
        # LIVE content. A leftover clone row is harmless; a live-leaking link is
        # not.
        update_result = await Chats.update_chat_share_id_by_id(id, None)
        result = await Chats.delete_shared_chat_by_chat_id(id)

        return result and update_result is not None
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )


############################
# UpdateChatFolderIdById
############################


class ChatFolderIdForm(BaseModel):
    folder_id: Optional[str] = None


@router.post("/{id}/folder", response_model=Optional[ChatResponse])
async def update_chat_folder_id_by_id(
    request: Request,
    id: str,
    form_data: ChatFolderIdForm,
    user=Depends(get_verified_user),
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        previous_folder_id = chat.folder_id
        chat = await Chats.update_chat_folder_id_by_id_and_user_id(
            id, user.id, form_data.folder_id
        )
        if chat:
            await broadcast_sidebar_event(
                user.id,
                {
                    "type": "chat:folder",
                    "data": {
                        **_chat_row_payload(chat),
                        "previous_folder_id": previous_folder_id,
                    },
                },
                skip_sid=_skip_sid(request),
            )
        return ChatResponse(
            **strip_tool_result_bodies_from_chat_model(chat).model_dump()
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# GetChatTagsById
############################


@router.get("/{id}/tags", response_model=list[TagModel])
async def get_chat_tags_by_id(id: str, user=Depends(get_verified_user)):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        tags = chat.meta.get("tags", [])
        return await Tags.get_tags_by_ids_and_user_id(tags, user.id)
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.NOT_FOUND
        )


############################
# AddChatTagById
############################


@router.post("/{id}/tags", response_model=list[TagModel])
async def add_tag_by_id_and_tag_name(
    request: Request, id: str, form_data: TagForm, user=Depends(get_verified_user)
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        tags = chat.meta.get("tags", [])
        tag_id = form_data.name.replace(" ", "_").lower()

        if tag_id == "none":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Tag name cannot be 'None'"),
            )

        if tag_id not in tags:
            await Chats.add_chat_tag_by_id_and_user_id_and_tag_name(
                id, user.id, form_data.name
            )

        chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
        tags = chat.meta.get("tags", [])
        await broadcast_sidebar_event(
            user.id,
            {"type": "chat:tags", "data": {"id": id}},
            skip_sid=_skip_sid(request),
        )
        return await Tags.get_tags_by_ids_and_user_id(tags, user.id)
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# DeleteChatTagById
############################


@router.delete("/{id}/tags", response_model=list[TagModel])
async def delete_tag_by_id_and_tag_name(
    request: Request, id: str, form_data: TagForm, user=Depends(get_verified_user)
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        await Chats.delete_tag_by_id_and_user_id_and_tag_name(
            id, user.id, form_data.name
        )

        if (
            await Chats.count_chats_by_tag_name_and_user_id(form_data.name, user.id)
            == 0
        ):
            await Tags.delete_tag_by_name_and_user_id(form_data.name, user.id)

        chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
        tags = chat.meta.get("tags", [])
        await broadcast_sidebar_event(
            user.id,
            {"type": "chat:tags", "data": {"id": id}},
            skip_sid=_skip_sid(request),
        )
        return await Tags.get_tags_by_ids_and_user_id(tags, user.id)
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.NOT_FOUND
        )


############################
# DeleteAllTagsById
############################


@router.delete("/{id}/tags/all", response_model=Optional[bool])
async def delete_all_tags_by_id(
    request: Request, id: str, user=Depends(get_verified_user)
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        await Chats.delete_all_tags_by_id_and_user_id(id, user.id)

        for tag in chat.meta.get("tags", []):
            if await Chats.count_chats_by_tag_name_and_user_id(tag, user.id) == 0:
                await Tags.delete_tag_by_name_and_user_id(tag, user.id)

        await broadcast_sidebar_event(
            user.id,
            {"type": "chat:tags", "data": {"id": id}},
            skip_sid=_skip_sid(request),
        )

        return True
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.NOT_FOUND
        )


############################
# PatchChatById — single op-vocabulary endpoint
############################


class PatchOp(BaseModel):
    # Frontend may send extra per-op fields (e.g. statusHistory, error, done,
    # annotation, model, modelName, modelIdx, files, timestamp, merged) that
    # the upsert partial should preserve. Allow extras through so the
    # update_message_content handler can spread them into the partial dict.
    model_config = ConfigDict(extra="allow")

    op: Literal[
        "set_param",
        "set_meta",
        "set_models",
        "set_files",
        "set_queue",
        "append_queue_item",
        "remove_queue_item",
        "update_queue_item",
        "set_question_state",
        "set_tags",
        "set_history_current_id",
        "fork_message_version",
        "append_message",
        "update_message_content",
        "set_message_annotation",
        "delete_message",
    ]
    key: Optional[str] = None
    value: Any = None
    message_id: Optional[str] = None
    source_message_id: Optional[str] = None
    parent_id: Optional[str] = None
    role: Optional[str] = None
    content: Any = None
    files: Any = None
    models: Optional[list] = None
    queue: Optional[list] = None
    item: Optional[dict] = None
    item_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    patch: Optional[dict] = None
    tags: Optional[list] = None
    current_id: Optional[str] = None
    model: Optional[str] = None
    annotation: Any = None
    extra: Optional[dict] = None


class PatchChatForm(BaseModel):
    ops: List[PatchOp]


def _message_from_append_patch(op: PatchOp) -> dict:
    if not op.message_id or not op.role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="append_message requires 'message_id' and 'role'",
        )

    message: dict = {
        "id": op.message_id,
        "parentId": op.parent_id,
        "childrenIds": [],
        "role": op.role,
    }
    if op.content is not None:
        message["content"] = op.content
    if op.files is not None:
        message["files"] = op.files
    if op.model is not None:
        message["model"] = op.model
    if op.models is not None:
        message["models"] = op.models

    reserved = {
        "op",
        "key",
        "value",
        "message_id",
        "source_message_id",
        "parent_id",
        "role",
        "content",
        "files",
        "models",
        "queue",
        "tags",
        "current_id",
        "model",
        "annotation",
        "extra",
        "copy_tool_result_bodies_from",
    }
    extra_fields = getattr(op, "model_extra", {}) or {}
    for key, value in extra_fields.items():
        if key not in reserved:
            message.setdefault(key, value)
    if isinstance(op.extra, dict):
        for key, value in op.extra.items():
            if key != "copy_tool_result_bodies_from":
                message.setdefault(key, value)

    copy_source = extra_fields.get("copy_tool_result_bodies_from")
    if copy_source is None and isinstance(op.extra, dict):
        copy_source = op.extra.get("copy_tool_result_bodies_from")
    if isinstance(copy_source, str) and copy_source:
        message["_copy_tool_result_bodies_from"] = copy_source

    if "content_blocks" in message:
        message["content_blocks"] = sanitize_content_blocks(message["content_blocks"])
    return message


@router.patch("/{id}")
async def patch_chat_by_id(
    request: Request,
    id: str,
    form_data: PatchChatForm,
    user=Depends(get_verified_user),
):
    version_ops = [op for op in form_data.ops if op.op == "fork_message_version"]
    if version_ops:
        if len(version_ops) != 1 or len(form_data.ops) != 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": "fork_message_version must be the only patch operation.",
                    "code": "message_version_must_be_isolated",
                },
            )
        if not await Chats.user_owns_chat(id, user.id):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )
        version_op = version_ops[0]
        if not version_op.message_id or not version_op.source_message_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="fork_message_version requires message_id and source_message_id",
            )
        try:
            version = await Chats.fork_message_version_atomic(
                id,
                version_op.source_message_id,
                version_op.message_id,
                content=version_op.content if version_op.content is not None else "",
                files=version_op.files,
                models=version_op.models,
                user_id=user.id,
            )
        except (ChatBranchConflictError, ChatMessageParentMissingError) as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": str(error),
                    "code": getattr(error, "code", "message_version_conflict"),
                },
            ) from error

        await broadcast_sidebar_event(
            user.id,
            {
                "type": "chat:updated",
                "data": {"id": id, "updated_at": version["updated_at"]},
            },
            skip_sid=_skip_sid(request),
        )
        return {
            "updated_at": version["updated_at"],
            "ops_applied": ["fork_message_version"],
            "message": strip_tool_result_bodies_from_message(version["message"]),
            "idempotent": version["idempotent"],
        }

    append_ops = [op for op in form_data.ops if op.op == "append_message"]
    if append_ops:
        allowed_ops = {"append_message", "set_history_current_id", "set_models"}
        unsupported_ops = [op.op for op in form_data.ops if op.op not in allowed_ops]
        if unsupported_ops:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": (
                        "Message appends may only be combined with branch selection "
                        "and model selection."
                    ),
                    "code": "chat_message_append_must_be_atomic",
                },
            )
        if not await Chats.user_owns_chat(id, user.id):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
            )

        pointer_ops = [
            op for op in form_data.ops if op.op == "set_history_current_id"
        ]
        final_pointer = pointer_ops[-1] if pointer_ops else None
        current_id = None
        if final_pointer is not None:
            current_id = (
                final_pointer.current_id
                if final_pointer.current_id is not None
                else final_pointer.value
                if final_pointer.value is not None
                else final_pointer.message_id
            )
        model_ops = [op for op in form_data.ops if op.op == "set_models"]
        final_models = model_ops[-1].models if model_ops else None

        append_messages = [_message_from_append_patch(op) for op in append_ops]
        try:
            append_result = await Chats.append_messages_atomic(
                id,
                append_messages,
                current_id=current_id,
                update_current_id=final_pointer is not None,
                models=final_models,
                update_models=bool(model_ops),
                user_id=user.id,
            )
        except (ChatBranchConflictError, ChatMessageParentMissingError) as error:
            if getattr(error, "code", "") == "chat_access_prohibited":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
                ) from error
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": str(error),
                    "code": getattr(error, "code", "chat_message_append_conflict"),
                },
            ) from error

        generation_placeholders = [
            (
                str(message.get("id") or ""),
                str(message.get("generation_id") or ""),
                str(message.get("turn_id") or ""),
            )
            for message in append_messages
            if message.get("role") == "assistant"
            and message.get("generation_id")
            and message.get("turn_id")
        ]
        if generation_placeholders:
            from open_webui.tasks import (
                is_generation_cancelled,
                is_generation_turn_cancelled,
            )

            redis = getattr(request.app.state, "redis", None)
            for message_id, generation_id, turn_id in dict.fromkeys(
                generation_placeholders
            ):
                try:
                    if await is_generation_cancelled(
                        redis, id, generation_id
                    ) or await is_generation_turn_cancelled(redis, id, turn_id):
                        await Chats.mark_generation_stopped_if_current(
                            id,
                            message_id,
                            generation_id,
                            turn_id,
                        )
                except Exception:
                    log.exception(
                        "reconciling cancellation after placeholder write failed for %s/%s",
                        id,
                        message_id,
                    )

        await broadcast_sidebar_event(
            user.id,
            {
                "type": "chat:updated",
                "data": {"id": id, "updated_at": append_result["updated_at"]},
            },
            skip_sid=_skip_sid(request),
        )
        return {
            "updated_at": append_result["updated_at"],
            "ops_applied": [op.op for op in form_data.ops],
            "messages": [
                strip_tool_result_bodies_from_message(message)
                for message in append_result["messages"]
            ],
            "idempotent": append_result["idempotent"],
        }

    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    delete_ops = [op for op in form_data.ops if op.op == "delete_message"]
    if delete_ops:
        # A graph deletion is a transaction boundary, not one operation that
        # can safely share a stale hydrated snapshot with unrelated mutations.
        # The frontend already sends it alone; rejecting mixed batches keeps the
        # server contract explicit for API clients and future callers.
        if len(delete_ops) != 1 or len(form_data.ops) != 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": "delete_message must be the only patch operation.",
                    "code": "chat_message_delete_must_be_isolated",
                },
            )
        delete_op = delete_ops[0]
        if not delete_op.message_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="delete_message requires 'message_id'",
            )

        # Root correctness boundary: no graph edge is removed until every
        # possible writer has acknowledged cancellation, and new work cannot be
        # admitted until the one locked DB transaction below has committed.
        block_token = await _acquire_quiescent_chat_mutation_block(request, id)
        try:
            try:
                deletion = await Chats.delete_message_with_relink_atomic(
                    id, delete_op.message_id
                )
            except ChatMessageParentMissingError as error:
                raise _message_parent_conflict(error) from error
        finally:
            await _release_chat_mutation_block(request, id, block_token)

        if deletion is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The chat no longer exists.",
            )

        updated_at = deletion["updated_at"]
        updated_chat_for_payload = await Chats.get_chat_by_id(id)
        await broadcast_sidebar_event(
            user.id,
            {
                "type": "chat:updated",
                "data": (
                    _chat_row_payload(updated_chat_for_payload)
                    if updated_chat_for_payload
                    else {"id": id, "updated_at": updated_at}
                ),
            },
            skip_sid=_skip_sid(request),
        )
        return {
            "updated_at": updated_at,
            "ops_applied": ["delete_message"],
        }

    async def upsert_message_checked(message_id: str, message: dict) -> None:
        try:
            await Chats.upsert_message_to_chat_by_id_and_message_id(
                id, message_id, message, return_model=False
            )
        except ChatMessageParentMissingError as error:
            raise _message_parent_conflict(error) from error

    # Branch navigation is an O(1), pointer-only operation. Do not turn it into
    # a stale whole-chat JSON replacement: that can roll back unrelated body
    # state another tab/generation committed after the read above. A mixed
    # append+pointer batch still follows the graph-aware path below because its
    # target row may be created by the same request.
    if form_data.ops and all(op.op == "set_history_current_id" for op in form_data.ops):
        final_op = form_data.ops[-1]
        target_id = (
            final_op.current_id
            if final_op.current_id is not None
            else final_op.value if final_op.value is not None else final_op.message_id
        )
        updated_at = await Chats.set_history_current_id_atomic(id, target_id)
        if updated_at is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The selected chat branch no longer exists.",
            )
        return {
            "updated_at": updated_at,
            "ops_applied": ["set_history_current_id"] * len(form_data.ops),
        }

    # Working copy of chat.chat that we mutate in-memory; flushed via a
    # single update_chat_by_id at the end when any body op ran.
    chat_body = json.loads(json.dumps(chat.chat)) if chat.chat else {}
    body_dirty = False
    # Multi-client sync (G3): a queue-item mutation (append/remove/update) doesn't
    # go through the body-flush path, so it never reordered the sidebar OR pushed a
    # chat:queue:updated — other tabs/devices only learned of a newly queued/steered
    # message when it drained or on a manual reload. Track it here and broadcast once
    # after the writes so every client's queue chip reflects it live.
    queue_changed = False

    # Title routes through the O(1) helper that skips the full message
    # table re-sync update_chat_by_id performs.
    title_change: Optional[str] = None
    tags_changed = False

    sidebar_events: list[dict] = []
    ops_applied: list[str] = []

    # Deferred content/annotation writes remain row-scoped and run after the
    # metadata flush so response streaming cannot be overwritten by chat-body
    # state. Structural appends never enter this generic path.
    deferred_row_writes: list[tuple[str, dict]] = []
    def _queue_sidebar(event: dict) -> None:
        if event not in sidebar_events:
            sidebar_events.append(event)

    for op in form_data.ops:
        if op.op == "set_param":
            if not op.key:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="set_param requires 'key'",
                )
            params = chat_body.get("params") or {}
            params[op.key] = op.value
            chat_body["params"] = params
            body_dirty = True

        elif op.op == "set_meta":
            if not op.key:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="set_meta requires 'key'",
                )
            if op.key == "title" and isinstance(op.value, str):
                title_change = op.value
            else:
                chat_body[op.key] = op.value
                body_dirty = True

        elif op.op == "set_models":
            models = op.models if isinstance(op.models, list) else op.value
            chat_body["models"] = models if isinstance(models, list) else []
            body_dirty = True

        elif op.op == "set_files":
            files = op.files if isinstance(op.files, list) else op.value
            chat_body["files"] = files if isinstance(files, list) else []
            body_dirty = True

        elif op.op == "set_queue":
            queue = op.queue if isinstance(op.queue, list) else op.value
            chat_body["queue"] = queue if isinstance(queue, list) else []
            body_dirty = True
            queue_changed = True

        elif op.op == "append_queue_item":
            # Atomic single-item append (avoids the whole-array clobber two tabs
            # would cause with set_queue). Writes directly via the model's
            # read-modify-write helper rather than the chat_body flush path.
            if not isinstance(op.item, dict):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="append_queue_item requires 'item'",
                )
            await Chats.append_queue_item_by_id(id, op.item)
            ops_applied.append(op.op)
            queue_changed = True
            continue

        elif op.op == "remove_queue_item":
            if not op.item_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="remove_queue_item requires 'item_id'",
                )
            await Chats.remove_queue_item_by_id(id, op.item_id)
            ops_applied.append(op.op)
            queue_changed = True
            continue

        elif op.op == "update_queue_item":
            # In-place edit of a queued item (position preserved). Replaces the
            # remove+append edit path, which moved the item to the tail and
            # reordered the drain / a steer's injection slot.
            if not isinstance(op.item, dict) or not op.item.get("id"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="update_queue_item requires 'item' with an 'id'",
                )
            await Chats.update_queue_item_by_id(id, op.item)
            ops_applied.append(op.op)
            queue_changed = True
            continue

        elif op.op == "set_question_state":
            # Durable delivery channel for the built-in ask_user tool: the
            # frontend autosaves draft selections and submits the final
            # answer/skip here. Atomic single-entry merge (like
            # append_queue_item) so two tabs can't clobber each other. After a
            # terminal write (answer/skip) we wake the blocked generation
            # immediately via the in-process registry; the generation's own
            # poll is the durable backstop if the signal is missed.
            if not op.tool_call_id or not isinstance(op.patch, dict):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="set_question_state requires 'tool_call_id' and 'patch'",
                )
            await Chats.set_question_state_by_id(id, op.tool_call_id, op.patch)
            if op.patch.get("answer") is not None or op.patch.get("skipped"):
                try:
                    from open_webui.utils import ask_user_registry

                    ask_user_registry.signal(id, op.tool_call_id)
                except Exception:
                    log.exception("ask_user signal failed for %s", id)
            ops_applied.append(op.op)
            continue

        elif op.op == "set_tags":
            tags = op.tags if isinstance(op.tags, list) else op.value
            chat_body["tags"] = tags if isinstance(tags, list) else []
            body_dirty = True
            tags_changed = True

        elif op.op == "set_history_current_id":
            history = chat_body.get("history") or {"messages": {}, "currentId": None}
            target_id = (
                op.current_id
                if op.current_id is not None
                else op.value if op.value is not None else op.message_id
            )
            history["currentId"] = target_id
            chat_body["history"] = history
            body_dirty = True

        elif op.op == "update_message_content":
            if not op.message_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="update_message_content requires 'message_id'",
                )
            # Build the partial from any non-canonical fields the frontend
            # sent (merged, statusHistory, error, done, annotation, model,
            # modelName, modelIdx, files, timestamp, ...) — ConfigDict
            # extra='allow' captures them on the BaseModel. Canonical
            # control fields are excluded so they don't leak into the
            # message row. ``op.extra`` (the explicit nested dict) wins
            # over the spread, and ``content``/``files`` win last.
            base = op.model_dump(
                exclude={
                    "op",
                    "message_id",
                    "key",
                    "value",
                    "parent_id",
                    "role",
                    "annotation",
                    "content",
                    "files",
                    "models",
                    "queue",
                    "tags",
                    "current_id",
                    "extra",
                },
                exclude_none=True,
            )
            partial: dict = dict(base)
            if isinstance(op.extra, dict):
                partial.update(op.extra)
            if op.content is not None:
                partial["content"] = op.content
            if op.files is not None:
                partial["files"] = op.files
            if partial:
                deferred_row_writes.append((op.message_id, partial))

        elif op.op == "set_message_annotation":
            if not op.message_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="set_message_annotation requires 'message_id'",
                )
            deferred_row_writes.append((op.message_id, {"annotation": op.annotation}))

        ops_applied.append(op.op)

    updated_at = chat.updated_at

    # Track whether any op other than a bare set_history_current_id ran.
    # Pure currentId pointer changes shouldn't reorder the sidebar; every
    # other body / row mutation bumps updated_at and the other tabs need
    # to know so the chat row moves to the top.
    non_pointer_mutation = any(
        op != "set_history_current_id" for op in ops_applied
    ) or bool(deferred_row_writes)

    if body_dirty:
        body_to_update = chat_body
        # ``get_chat_by_id_and_user_id`` hydrates migrated messages as a read
        # projection. Never feed that projection into a metadata write. The model
        # enforces this again at the storage boundary; stripping it here also
        # avoids serializing a history-sized object between the two layers.
        if getattr(chat, "messages_migrated", 0):
            body_to_update = json.loads(json.dumps(chat_body))
            history = body_to_update.get("history")
            if isinstance(history, dict):
                history.pop("messages", None)

        updated = await Chats.update_chat_by_id(id, body_to_update)
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=ERROR_MESSAGES.DEFAULT(),
            )
        updated_at = updated.updated_at

    for message_id, partial in deferred_row_writes:
        await upsert_message_checked(message_id, partial)

    # Row-only patches (no body op) never went through update_chat_by_id, so
    # the chat row's xmin/updated_at didn't move — a conditional open could
    # then 304 right past a just-written row. Touch the row so the ETag
    # validator rotates whenever any message row changed.
    if not body_dirty and deferred_row_writes:
        try:
            await Chats.touch_updated_at(id)
        except Exception:
            log.debug("touch_updated_at failed for %s", id, exc_info=True)

    if title_change is not None:
        renamed = await Chats.update_chat_title_by_id(id, title_change)
        if renamed is not None:
            updated_at = renamed.updated_at
            _queue_sidebar(
                {
                    "type": "chat:renamed",
                    "data": _chat_row_payload(renamed),
                }
            )

    if tags_changed:
        _queue_sidebar({"type": "chat:tags", "data": {"id": id}})

    # Emit a generic chat:updated for body-mutating ops so other tabs
    # reorder the sidebar even when no title change happened. Skip when
    # the only mutation was a set_history_current_id pointer flip (the
    # active leaf moves but timestamp ordering shouldn't change for that
    # alone) — though update_chat_by_id still bumped updated_at, the
    # sidebar UX matches the rest of the codebase which doesn't reorder
    # on pointer-only changes.
    if non_pointer_mutation and (body_dirty or deferred_row_writes):
        updated_chat_for_payload = await Chats.get_chat_by_id(id)
        _queue_sidebar(
            {
                "type": "chat:updated",
                "data": (
                    _chat_row_payload(updated_chat_for_payload)
                    if updated_chat_for_payload
                    else {"id": id, "updated_at": updated_at}
                ),
            }
        )

    skip_sid = _skip_sid(request)
    for event in sidebar_events:
        await broadcast_sidebar_event(user.id, event, skip_sid=skip_sid)

    # G3: push the authoritative queue to the user's other tabs/devices so a newly
    # queued or steered message (or a reorder/edit) shows in their chip strip live,
    # not only when it drains or on reload. skip_sid omits the originating tab, which
    # already applied the change optimistically.
    if queue_changed and not str(id).startswith("local:"):
        try:
            from open_webui.utils.chat_queue import broadcast_queue_state

            await broadcast_queue_state(
                user.id, id, event_type="chat:queue:updated", skip_sid=skip_sid
            )
        except Exception:
            log.debug("queue-change broadcast failed for %s", id, exc_info=True)

    return {"updated_at": updated_at, "ops_applied": ops_applied}


@router.post("/{id}/queue/drain")
async def drain_chat_queue_now(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
):
    """Immediately drain the next queued message for a chat (the "Send now"
    affordance for a queue the user paused by pressing Stop). Pops the head and
    starts its generation server-side; the rest chain on clean completion. The
    drain ownership marker makes this a no-op if a generation is already in
    flight, so it can't spawn a concurrent turn. ``finished_response_id`` is None
    here (no preceding completion), so the user-stop suppression in
    maybe_drain_queue does NOT apply — this is an explicit, intentional resume."""
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    if str(id).startswith("local:"):
        return {"started": False}
    from open_webui.utils.chat_queue import maybe_drain_queue

    response_message_id = await maybe_drain_queue(
        request.app, user, id, finished_response_id=None
    )
    return {
        "started": response_message_id is not None,
        "response_message_id": response_message_id,
    }
