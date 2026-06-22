import asyncio
import json
import logging
from typing import Optional


from open_webui.socket.main import (
    broadcast_sidebar_event,
    get_event_emitter,
    get_stream_state,
    get_tool_result_body,
)
from open_webui.models.chats import (
    ChatForm,
    ChatImportForm,
    ChatResponse,
    ChatSearchResponse,
    Chats,
    ChatTitleIdResponse,
    _project_message_slim,
    strip_tool_result_bodies_from_chat_model,
    strip_tool_result_bodies_from_message,
)
from open_webui.models.tags import TagModel, Tags
from open_webui.models.folders import Folders

from open_webui.config import ENABLE_ADMIN_CHAT_ACCESS, ENABLE_ADMIN_EXPORT
from open_webui.constants import ERROR_MESSAGES
from open_webui.env import SRC_LOG_LEVELS
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from typing import Any, List, Literal


from open_webui.utils.auth import get_admin_user, get_verified_user, get_optional_user
from open_webui.utils.access_control import has_permission
from open_webui.utils.cache import etag_response
from open_webui.utils.chat_embedder import aembed_query, CHAT_SEMANTIC_ENABLED

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

router = APIRouter()


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


@router.get("/", response_model=list[ChatTitleIdResponse])
@router.get("/list", response_model=list[ChatTitleIdResponse])
async def get_session_user_chat_list(
    user=Depends(get_verified_user),
    page: Optional[int] = None,
    include_pinned: Optional[bool] = False,
    include_folders: Optional[bool] = False,
):
    try:
        if page is not None:
            limit = 60
            skip = (page - 1) * limit

            return await Chats.get_chat_title_id_list_by_user_id(
                user.id,
                include_folders=include_folders,
                include_pinned=include_pinned,
                skip=skip,
                limit=limit,
            )
        else:
            return await Chats.get_chat_title_id_list_by_user_id(
                user.id, include_folders=include_folders, include_pinned=include_pinned
            )
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
                    and await Tags.get_tag_by_name_and_user_id(tag_name, user.id) is None
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
    if CHAT_SEMANTIC_ENABLED:
        cleaned = " ".join(
            w
            for w in (text or "").split()
            if not w.lower().startswith(("tag:", "folder:", "pinned:", "archived:", "shared:"))
        ).strip()
        if cleaned and not (len(cleaned) < 2 and cleaned.isascii()):
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


@router.get("/all/tags", response_model=list[TagModel])
async def get_all_user_tags(request: Request, user=Depends(get_verified_user)):
    try:
        tags = await Tags.get_tags_by_user_id(user.id)
        content = [
            tag.model_dump() if hasattr(tag, "model_dump") else dict(tag)
            for tag in tags
        ]
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
    return [ChatResponse(**chat.model_dump()) for chat in await Chats.get_chats_with_data()]


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
    if user:
        if user.role == "pending":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )

        if user.role == "user" or (
            user.role == "admin" and not ENABLE_ADMIN_CHAT_ACCESS
        ):
            chat = await Chats.get_chat_by_share_id(share_id)
        elif user.role == "admin" and ENABLE_ADMIN_CHAT_ACCESS:
            chat = await Chats.get_chat_by_id(share_id)
    else:
        chat = await Chats.get_chat_by_share_id(share_id)

    if chat:
        return ChatResponse(
            **strip_tool_result_bodies_from_chat_model(chat).model_dump()
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
    if user:
        if user.role == "pending":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )
        if user.role == "admin" and ENABLE_ADMIN_CHAT_ACCESS:
            chat = await Chats.get_chat_by_id(share_id) or await Chats.get_chat_by_share_id(
                share_id
            )
        else:
            chat = await Chats.get_chat_by_share_id(share_id)
    else:
        chat = await Chats.get_chat_by_share_id(share_id)

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

    for block in message.get("content_blocks", []) if isinstance(message, dict) else []:
        if block.get("type") != "tool_calls":
            continue
        for result in block.get("results") or []:
            if isinstance(result, dict) and result.get("tool_call_id") == tool_call_id:
                return result

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Tool result not found",
    )


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


@router.get("/{id}")
async def get_chat_by_id(
    id: str,
    meta_only: bool = False,
    user=Depends(get_verified_user),
):
    if meta_only:
        meta = await Chats.get_chat_meta_by_id_and_user_id(id, user.id)
        if meta is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )
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
        updated_chat = {**chat.chat, **form_data.chat}
        chat = await Chats.update_chat_by_id(id, updated_chat)
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
        messages = await Chats.get_chat_messages_paginated(id, skip=skip, limit=offset_limit)

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
    """Fetch a large tool result body lazily when the user expands a tool card.

    Stream-v2.1 keeps large web_search/web_fetch bodies out of socket snapshots and
    message content_blocks. The full body is available from the live in-memory
    store while generation is active and from the persisted assistant message's
    `tool_result_bodies` after checkpoints/final save.
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

    live_state = get_stream_state(message_id)
    if live_state:
        if live_state.get("chat_id") != id or live_state.get("user_id") != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tool result not found",
            )
        live_body = get_tool_result_body(message_id, tool_call_id)
        if isinstance(live_body, dict):
            return live_body
    bodies = message.get("tool_result_bodies") if isinstance(message, dict) else None
    body = bodies.get(tool_call_id) if isinstance(bodies, dict) else None
    if isinstance(body, dict):
        return body

    # Backward compatibility: old rows may still have full result bodies inline.
    for block in message.get("content_blocks", []) if isinstance(message, dict) else []:
        if block.get("type") != "tool_calls":
            continue
        for result in block.get("results") or []:
            if isinstance(result, dict) and result.get("tool_call_id") == tool_call_id:
                return result

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Tool result not found",
    )


@router.get("/{id}/messages/{message_id}/siblings")
async def get_chat_message_siblings(
    id: str,
    message_id: str,
    user=Depends(get_verified_user),
):
    """Return the messages that share a parent with ``message_id`` (including
    ``message_id`` itself), with full content. Used by the frontend's
    branch-switch lazy-load path: when the user clicks the prev/next arrow,
    we fetch the sibling branch's leaf message and its peers in one call.
    """
    if not await Chats.user_owns_chat(id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    return [
        strip_tool_result_bodies_from_message(m)
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

    chat = await Chats.upsert_message_to_chat_by_id_and_message_id(
        id,
        message_id,
        {
            "content": form_data.content,
        },
    )

    event_emitter = get_event_emitter(
        {
            "user_id": user.id,
            "chat_id": id,
            "message_id": message_id,
        },
        False,
    )

    if event_emitter:
        await event_emitter(
            {
                "type": "chat:message",
                "data": {
                    "chat_id": id,
                    "message_id": message_id,
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


@router.delete("/{id}", response_model=bool)
async def delete_chat_by_id(request: Request, id: str, user=Depends(get_verified_user)):
    if user.role == "admin":
        chat = await Chats.get_chat_by_id(id)
        payload = _chat_row_payload(chat) if chat else {"id": id}
        for tag in chat.meta.get("tags", []):
            if await Chats.count_chats_by_tag_name_and_user_id(tag, user.id) == 1:
                await Tags.delete_tag_by_name_and_user_id(tag, user.id)

        result = await Chats.delete_chat_by_id(id)

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
        for tag in chat.meta.get("tags", []):
            if await Chats.count_chats_by_tag_name_and_user_id(tag, user.id) == 1:
                await Tags.delete_tag_by_name_and_user_id(tag, user.id)

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

    if user.role == "admin":
        chat = await Chats.get_chat_by_id(id)
    else:
        chat = await Chats.get_chat_by_share_id(id)

    if chat:
        updated_chat = {
            **chat.chat,
            "originalChatId": chat.id,
            "branchPointMessageId": chat.chat["history"]["currentId"],
            "title": f"Clone of {chat.title}",
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
                if await Chats.count_chats_by_tag_name_and_user_id(tag_id, user.id) == 0:
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


@router.post("/{id}/share", response_model=Optional[ChatResponse])
async def share_chat_by_id(request: Request, id: str, user=Depends(get_verified_user)):
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

    if chat:
        if chat.share_id:
            shared_chat = await Chats.update_shared_chat_by_chat_id(chat.id)
            return ChatResponse(
                **strip_tool_result_bodies_from_chat_model(shared_chat).model_dump()
            )

        shared_chat = await Chats.insert_shared_chat_by_chat_id(chat.id)
        if not shared_chat:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=ERROR_MESSAGES.DEFAULT(),
            )
        return ChatResponse(
            **strip_tool_result_bodies_from_chat_model(shared_chat).model_dump()
        )

    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
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

        result = await Chats.delete_shared_chat_by_chat_id(id)
        update_result = await Chats.update_chat_share_id_by_id(id, None)

        return result and update_result != None
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
        await Chats.delete_tag_by_id_and_user_id_and_tag_name(id, user.id, form_data.name)

        if await Chats.count_chats_by_tag_name_and_user_id(form_data.name, user.id) == 0:
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
        "set_question_state",
        "set_tags",
        "set_history_current_id",
        "append_message",
        "update_message_content",
        "set_message_annotation",
        "delete_message",
    ]
    key: Optional[str] = None
    value: Any = None
    message_id: Optional[str] = None
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


async def _delete_message_with_relink(messages: dict, message_id: str) -> Optional[str]:
    """Port of src/lib/components/chat/Messages.svelte:372-407.

    Removes ``message_id`` and its direct children; the grandchildren are
    re-parented to the deleted message's parent so the branch graph stays
    connected. Returns the parent id so callers can fix up
    ``history.currentId`` when the deleted message was the active leaf.
    """
    target = messages.get(message_id)
    if not target:
        return None

    parent_id = target.get("parentId")
    child_ids = list(target.get("childrenIds") or [])

    grandchild_ids: list[str] = []
    for child_id in child_ids:
        child = messages.get(child_id)
        if child:
            grandchild_ids.extend(child.get("childrenIds") or [])

    if parent_id and parent_id in messages:
        parent = messages[parent_id]
        parent_children = [
            cid for cid in (parent.get("childrenIds") or []) if cid != message_id
        ]
        parent_children.extend(grandchild_ids)
        parent["childrenIds"] = parent_children

    for grandchild_id in grandchild_ids:
        if grandchild_id in messages:
            messages[grandchild_id]["parentId"] = parent_id

    for mid in [message_id, *child_ids]:
        messages.pop(mid, None)

    return parent_id


@router.patch("/{id}")
async def patch_chat_by_id(
    request: Request,
    id: str,
    form_data: PatchChatForm,
    user=Depends(get_verified_user),
):
    chat = await Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    # Working copy of chat.chat that we mutate in-memory; flushed via a
    # single update_chat_by_id at the end when any body op ran.
    chat_body = json.loads(json.dumps(chat.chat)) if chat.chat else {}
    body_dirty = False
    message_body_dirty = False

    # Title routes through the O(1) helper that skips the full message
    # table re-sync update_chat_by_id performs.
    title_change: Optional[str] = None
    tags_changed = False

    sidebar_events: list[dict] = []
    ops_applied: list[str] = []

    # Deferred message-row writes: these hit the fast per-row upsert path
    # which would be clobbered if a later body-mutating op forced the
    # final update_chat_by_id to re-sync the message table from our
    # pre-fetch snapshot. Run them AFTER the body flush.
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
            continue

        elif op.op == "remove_queue_item":
            if not op.item_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="remove_queue_item requires 'item_id'",
                )
            await Chats.remove_queue_item_by_id(id, op.item_id)
            ops_applied.append(op.op)
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

        elif op.op == "append_message":
            if not op.message_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="append_message requires 'message_id'",
                )
            new_msg: dict = {
                "id": op.message_id,
                "parentId": op.parent_id,
                "childrenIds": [],
            }
            if op.role is not None:
                new_msg["role"] = op.role
            if op.content is not None:
                new_msg["content"] = op.content
            if op.files is not None:
                new_msg["files"] = op.files
            if op.model is not None:
                new_msg["model"] = op.model

            reserved = {
                "op",
                "key",
                "value",
                "message_id",
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
            }
            extra_fields = getattr(op, "model_extra", {}) or {}
            for k, v in extra_fields.items():
                if k not in reserved:
                    new_msg.setdefault(k, v)
            if isinstance(op.extra, dict):
                for k, v in op.extra.items():
                    new_msg.setdefault(k, v)

            history = chat_body.get("history") or {"messages": {}, "currentId": None}
            messages = history.get("messages") or {}

            if op.parent_id and op.parent_id in messages:
                parent = messages[op.parent_id]
                parent_children = list(parent.get("childrenIds") or [])
                if op.message_id not in parent_children:
                    parent_children.append(op.message_id)
                parent["childrenIds"] = parent_children

            messages[op.message_id] = {**messages.get(op.message_id, {}), **new_msg}
            history["messages"] = messages
            chat_body["history"] = history
            body_dirty = True
            message_body_dirty = True

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

        elif op.op == "delete_message":
            if not op.message_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="delete_message requires 'message_id'",
                )
            history = chat_body.get("history") or {"messages": {}, "currentId": None}
            messages = history.get("messages") or {}
            was_current = history.get("currentId") == op.message_id
            new_leaf = await _delete_message_with_relink(messages, op.message_id)
            if was_current:
                # Matches the frontend's showMessage({id: parentMessageId}) —
                # falls back to None if the deleted node was the root.
                history["currentId"] = new_leaf
            history["messages"] = messages
            chat_body["history"] = history
            body_dirty = True
            message_body_dirty = True

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
        # ``get_chat_by_id_and_user_id`` hydrates migrated chat messages into
        # ``chat.chat.history.messages`` for callers. Body-only patches such as
        # set_queue/set_models/set_history_current_id must not feed that hydrated
        # snapshot back into update_chat_by_id, because update_chat_by_id treats a
        # messages dict as authoritative and re-syncs the chat_message table. A
        # delayed queue save could otherwise overwrite newer streamed rows or a
        # just-appended queued turn.
        if getattr(chat, "messages_migrated", 0) and not message_body_dirty:
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
        await Chats.upsert_message_to_chat_by_id_and_message_id(id, message_id, partial, return_model=False)

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

    return {"updated_at": updated_at, "ops_applied": ops_applied}
