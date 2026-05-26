import json
import logging
from typing import Optional


from open_webui.socket.main import broadcast_sidebar_event, get_event_emitter
from open_webui.models.chats import (
    ChatForm,
    ChatImportForm,
    ChatResponse,
    ChatSearchResponse,
    Chats,
    ChatTitleIdResponse,
    _project_message_slim,
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
def get_session_user_chat_list(
    user=Depends(get_verified_user),
    page: Optional[int] = None,
    include_pinned: Optional[bool] = False,
    include_folders: Optional[bool] = False,
):
    try:
        if page is not None:
            limit = 60
            skip = (page - 1) * limit

            return Chats.get_chat_title_id_list_by_user_id(
                user.id,
                include_folders=include_folders,
                include_pinned=include_pinned,
                skip=skip,
                limit=limit,
            )
        else:
            return Chats.get_chat_title_id_list_by_user_id(
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
def get_session_user_chat_count(user=Depends(get_verified_user)):
    return Chats.count_chats_by_user_id(user.id)


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

    result = Chats.delete_chats_by_user_id(user.id)
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

    return Chats.get_chat_list_by_user_id(
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
        chat = Chats.insert_new_chat(user.id, form_data)
        await broadcast_sidebar_event(
            user.id,
            {"type": "chat:created", "data": _chat_row_payload(chat)},
            skip_sid=_skip_sid(request),
        )
        return ChatResponse(**chat.model_dump())
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
        chat = Chats.import_chat(user.id, form_data)
        if chat:
            tags = chat.meta.get("tags", [])
            for tag_id in tags:
                tag_id = tag_id.replace(" ", "_").lower()
                tag_name = " ".join([word.capitalize() for word in tag_id.split("_")])
                if (
                    tag_id != "none"
                    and Tags.get_tag_by_name_and_user_id(tag_name, user.id) is None
                ):
                    Tags.insert_new_tag(tag_name, user.id)

            await broadcast_sidebar_event(
                user.id,
                {"type": "chat:created", "data": _chat_row_payload(chat)},
                skip_sid=_skip_sid(request),
            )

        return ChatResponse(**chat.model_dump())
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# GetChats
############################


@router.get("/search", response_model=ChatSearchResponse)
def search_user_chats(
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

    return Chats.search_chats(
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
    )


############################
# GetChatsByFolderId
############################


@router.get("/folder/{folder_id}", response_model=list[ChatResponse])
async def get_chats_by_folder_id(folder_id: str, user=Depends(get_verified_user)):
    folder_ids = [folder_id]
    children_folders = Folders.get_children_folders_by_id_and_user_id(
        folder_id, user.id
    )
    if children_folders:
        folder_ids.extend([folder.id for folder in children_folders])

    return [
        ChatResponse(**chat.model_dump())
        for chat in Chats.get_chats_by_folder_ids_and_user_id(folder_ids, user.id)
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
            for chat in Chats.get_chats_by_folder_id_and_user_id(
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
    rows = Chats.get_pinned_chats_by_user_id(user.id)
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
        for chat in Chats.get_chats_with_data_by_user_id(user.id)
    ]


############################
# GetArchivedChats
############################


@router.get("/all/archived", response_model=list[ChatResponse])
async def get_user_archived_chats(user=Depends(get_verified_user)):
    # Export endpoint — needs full chat JSON.
    return [
        ChatResponse(**chat.model_dump())
        for chat in Chats.get_archived_chats_with_data_by_user_id(user.id)
    ]


############################
# GetAllTags
############################


@router.get("/all/tags", response_model=list[TagModel])
async def get_all_user_tags(request: Request, user=Depends(get_verified_user)):
    try:
        tags = Tags.get_tags_by_user_id(user.id)
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
    return [
        ChatResponse(**chat.model_dump())
        for chat in Chats.get_chats_with_data()
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
        for chat in Chats.get_archived_chat_list_by_user_id(
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
    result = Chats.archive_all_chats_by_user_id(user.id)
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
    result = Chats.unarchive_all_chats_by_user_id(user.id)
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
            chat = Chats.get_chat_by_share_id(share_id)
        elif user.role == "admin" and ENABLE_ADMIN_CHAT_ACCESS:
            chat = Chats.get_chat_by_id(share_id)
    else:
        chat = Chats.get_chat_by_share_id(share_id)

    if chat:
        return ChatResponse(**chat.model_dump())

    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.NOT_FOUND
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
    chats = Chats.get_chat_list_by_user_id_and_tag_name(
        user.id, form_data.name, form_data.skip, form_data.limit
    )
    if len(chats) == 0:
        Tags.delete_tag_by_name_and_user_id(form_data.name, user.id)

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
        meta = Chats.get_chat_meta_by_id_and_user_id(id, user.id)
        if meta is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )
        return meta

    chat = Chats.get_chat_by_id_and_user_id(id, user.id)

    if chat:
        return ChatResponse(**chat.model_dump())

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
    chat = Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        prior_title = chat.title
        updated_chat = {**chat.chat, **form_data.chat}
        chat = Chats.update_chat_by_id(id, updated_chat)
        if chat and chat.title != prior_title:
            await broadcast_sidebar_event(
                user.id,
                {
                    "type": "chat:renamed",
                    "data": {
                        "id": chat.id,
                        "title": chat.title,
                        "updated_at": chat.updated_at,
                    },
                },
                skip_sid=_skip_sid(request),
            )
        return ChatResponse(**chat.model_dump())
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

    ``slim=true`` (default) strips bandwidth-heavy per-message fields
    (``originalContent``, ``reasoning_details_per_round`` for non-leaf,
    oversized ``tool_calls`` results for non-current-branch turns). Callers
    that need the full bodies (admin/export/share) pass ``slim=false``.
    """
    # Lightweight ownership check — avoids hydrating the whole message tree
    # (which would defeat the entire point of paginating).
    if not Chats.user_owns_chat(id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if leaf is not None:
        branch_limit = limit if limit and limit > 0 else 7
        messages = Chats.get_chat_messages_branch(
            id, leaf, before_message_id=before, limit=branch_limit
        )
    else:
        offset_limit = limit if limit and limit > 0 else 100
        messages = Chats.get_chat_messages_paginated(
            id, skip=skip, limit=offset_limit
        )

    if not slim:
        return messages

    leaf_for_projection = current_leaf or leaf
    return [
        _project_message_slim(
            m,
            is_current_leaf=(
                leaf_for_projection is not None
                and m.get("id") == leaf_for_projection
            ),
            is_current_branch=True,
        )
        for m in messages
        if isinstance(m, dict)
    ]


############################
# GetChatMessageSiblings
############################


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
    if not Chats.user_owns_chat(id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )
    return Chats.get_message_siblings(id, message_id)


############################
# UpdateChatMessageById
############################
class MessageForm(BaseModel):
    content: str


@router.post("/{id}/messages/{message_id}", response_model=Optional[ChatResponse])
async def update_chat_message_by_id(
    id: str, message_id: str, form_data: MessageForm, user=Depends(get_verified_user)
):
    chat = Chats.get_chat_by_id(id)

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

    chat = Chats.upsert_message_to_chat_by_id_and_message_id(
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

    return ChatResponse(**chat.model_dump())


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
    chat = Chats.get_chat_by_id(id)

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
        chat = Chats.get_chat_by_id(id)
        for tag in chat.meta.get("tags", []):
            if Chats.count_chats_by_tag_name_and_user_id(tag, user.id) == 1:
                Tags.delete_tag_by_name_and_user_id(tag, user.id)

        result = Chats.delete_chat_by_id(id)

        if result:
            await broadcast_sidebar_event(
                user.id,
                {"type": "chat:deleted", "data": {"id": id}},
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

        chat = Chats.get_chat_by_id(id)
        for tag in chat.meta.get("tags", []):
            if Chats.count_chats_by_tag_name_and_user_id(tag, user.id) == 1:
                Tags.delete_tag_by_name_and_user_id(tag, user.id)

        result = Chats.delete_chat_by_id_and_user_id(id, user.id)
        if result:
            await broadcast_sidebar_event(
                user.id,
                {"type": "chat:deleted", "data": {"id": id}},
                skip_sid=_skip_sid(request),
            )
        return result


############################
# GetPinnedStatusById
############################


@router.get("/{id}/pinned", response_model=Optional[bool])
async def get_pinned_status_by_id(id: str, user=Depends(get_verified_user)):
    chat = Chats.get_chat_by_id_and_user_id(id, user.id)
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
    chat = Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        chat = Chats.toggle_chat_pinned_by_id(id)
        if chat:
            await broadcast_sidebar_event(
                user.id,
                {
                    "type": "chat:pinned",
                    "data": {"id": chat.id, "pinned": bool(chat.pinned)},
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
    chat = Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        updated_chat = {
            **chat.chat,
            "originalChatId": chat.id,
            "branchPointMessageId": chat.chat["history"]["currentId"],
            "title": form_data.title if form_data.title else f"Clone of {chat.title}",
        }

        chat = Chats.import_chat(
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

        return ChatResponse(**chat.model_dump())
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
        chat = Chats.get_chat_by_id(id)
    else:
        chat = Chats.get_chat_by_share_id(id)

    if chat:
        updated_chat = {
            **chat.chat,
            "originalChatId": chat.id,
            "branchPointMessageId": chat.chat["history"]["currentId"],
            "title": f"Clone of {chat.title}",
        }

        chat = Chats.import_chat(
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
        return ChatResponse(**chat.model_dump())
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
    chat = Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        chat = Chats.toggle_chat_archive_by_id(id)

        # Delete tags if chat is archived
        if chat.archived:
            for tag_id in chat.meta.get("tags", []):
                if Chats.count_chats_by_tag_name_and_user_id(tag_id, user.id) == 0:
                    log.debug(f"deleting tag: {tag_id}")
                    Tags.delete_tag_by_name_and_user_id(tag_id, user.id)
        else:
            for tag_id in chat.meta.get("tags", []):
                tag = Tags.get_tag_by_name_and_user_id(tag_id, user.id)
                if tag is None:
                    log.debug(f"inserting tag: {tag_id}")
                    tag = Tags.insert_new_tag(tag_id, user.id)

        await broadcast_sidebar_event(
            user.id,
            {
                "type": "chat:archived",
                "data": _chat_row_payload(chat),
            },
            skip_sid=_skip_sid(request),
        )

        return ChatResponse(**chat.model_dump())
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

    chat = Chats.get_chat_by_id_and_user_id(id, user.id)

    if chat:
        if chat.share_id:
            shared_chat = Chats.update_shared_chat_by_chat_id(chat.id)
            return ChatResponse(**shared_chat.model_dump())

        shared_chat = Chats.insert_shared_chat_by_chat_id(chat.id)
        if not shared_chat:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=ERROR_MESSAGES.DEFAULT(),
            )
        return ChatResponse(**shared_chat.model_dump())

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
    chat = Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        if not chat.share_id:
            return False

        result = Chats.delete_shared_chat_by_chat_id(id)
        update_result = Chats.update_chat_share_id_by_id(id, None)

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
    chat = Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        chat = Chats.update_chat_folder_id_by_id_and_user_id(
            id, user.id, form_data.folder_id
        )
        if chat:
            await broadcast_sidebar_event(
                user.id,
                {
                    "type": "chat:folder",
                    "data": {"id": chat.id, "folder_id": chat.folder_id},
                },
                skip_sid=_skip_sid(request),
            )
        return ChatResponse(**chat.model_dump())
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.DEFAULT()
        )


############################
# GetChatTagsById
############################


@router.get("/{id}/tags", response_model=list[TagModel])
async def get_chat_tags_by_id(id: str, user=Depends(get_verified_user)):
    chat = Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        tags = chat.meta.get("tags", [])
        return Tags.get_tags_by_ids_and_user_id(tags, user.id)
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
    chat = Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        tags = chat.meta.get("tags", [])
        tag_id = form_data.name.replace(" ", "_").lower()

        if tag_id == "none":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT("Tag name cannot be 'None'"),
            )

        if tag_id not in tags:
            Chats.add_chat_tag_by_id_and_user_id_and_tag_name(
                id, user.id, form_data.name
            )

        chat = Chats.get_chat_by_id_and_user_id(id, user.id)
        tags = chat.meta.get("tags", [])
        await broadcast_sidebar_event(
            user.id,
            {"type": "chat:tags", "data": {"id": id}},
            skip_sid=_skip_sid(request),
        )
        return Tags.get_tags_by_ids_and_user_id(tags, user.id)
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
    chat = Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        Chats.delete_tag_by_id_and_user_id_and_tag_name(id, user.id, form_data.name)

        if Chats.count_chats_by_tag_name_and_user_id(form_data.name, user.id) == 0:
            Tags.delete_tag_by_name_and_user_id(form_data.name, user.id)

        chat = Chats.get_chat_by_id_and_user_id(id, user.id)
        tags = chat.meta.get("tags", [])
        await broadcast_sidebar_event(
            user.id,
            {"type": "chat:tags", "data": {"id": id}},
            skip_sid=_skip_sid(request),
        )
        return Tags.get_tags_by_ids_and_user_id(tags, user.id)
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
    chat = Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat:
        Chats.delete_all_tags_by_id_and_user_id(id, user.id)

        for tag in chat.meta.get("tags", []):
            if Chats.count_chats_by_tag_name_and_user_id(tag, user.id) == 0:
                Tags.delete_tag_by_name_and_user_id(tag, user.id)

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
    model: Optional[str] = None
    annotation: Any = None
    extra: Optional[dict] = None


class PatchChatForm(BaseModel):
    ops: List[PatchOp]


def _delete_message_with_relink(messages: dict, message_id: str) -> Optional[str]:
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
    chat = Chats.get_chat_by_id_and_user_id(id, user.id)
    if chat is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    # Working copy of chat.chat that we mutate in-memory; flushed via a
    # single update_chat_by_id at the end when any body op ran.
    chat_body = json.loads(json.dumps(chat.chat)) if chat.chat else {}
    body_dirty = False

    # Title routes through the O(1) helper that skips the full message
    # table re-sync update_chat_by_id performs.
    title_change: Optional[str] = None

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
            chat_body["models"] = op.value if isinstance(op.value, list) else []
            body_dirty = True

        elif op.op == "set_files":
            chat_body["files"] = op.value if isinstance(op.value, list) else []
            body_dirty = True

        elif op.op == "set_queue":
            chat_body["queue"] = op.value if isinstance(op.value, list) else []
            body_dirty = True

        elif op.op == "set_history_current_id":
            history = chat_body.get("history") or {"messages": {}, "currentId": None}
            target_id = op.value if op.value is not None else op.message_id
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
                    "extra",
                }
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
            deferred_row_writes.append(
                (op.message_id, {"annotation": op.annotation})
            )

        elif op.op == "delete_message":
            if not op.message_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="delete_message requires 'message_id'",
                )
            history = chat_body.get("history") or {"messages": {}, "currentId": None}
            messages = history.get("messages") or {}
            was_current = history.get("currentId") == op.message_id
            new_leaf = _delete_message_with_relink(messages, op.message_id)
            if was_current:
                # Matches the frontend's showMessage({id: parentMessageId}) —
                # falls back to None if the deleted node was the root.
                history["currentId"] = new_leaf
            history["messages"] = messages
            chat_body["history"] = history
            body_dirty = True

        ops_applied.append(op.op)

    updated_at = chat.updated_at

    # Track whether any op other than a bare set_history_current_id ran.
    # Pure currentId pointer changes shouldn't reorder the sidebar; every
    # other body / row mutation bumps updated_at and the other tabs need
    # to know so the chat row moves to the top.
    non_pointer_mutation = (
        any(op != "set_history_current_id" for op in ops_applied)
        or bool(deferred_row_writes)
    )

    if body_dirty:
        updated = Chats.update_chat_by_id(id, chat_body)
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=ERROR_MESSAGES.DEFAULT(),
            )
        updated_at = updated.updated_at

    for message_id, partial in deferred_row_writes:
        Chats.upsert_message_to_chat_by_id_and_message_id(id, message_id, partial)

    if title_change is not None:
        renamed = Chats.update_chat_title_by_id(id, title_change)
        if renamed is not None:
            updated_at = renamed.updated_at
            _queue_sidebar(
                {
                    "type": "chat:renamed",
                    "data": {
                        "id": id,
                        "title": renamed.title,
                        "updated_at": renamed.updated_at,
                    },
                }
            )

    # Emit a generic chat:updated for body-mutating ops so other tabs
    # reorder the sidebar even when no title change happened. Skip when
    # the only mutation was a set_history_current_id pointer flip (the
    # active leaf moves but timestamp ordering shouldn't change for that
    # alone) — though update_chat_by_id still bumped updated_at, the
    # sidebar UX matches the rest of the codebase which doesn't reorder
    # on pointer-only changes.
    if non_pointer_mutation and (body_dirty or deferred_row_writes):
        _queue_sidebar(
            {
                "type": "chat:updated",
                "data": {"id": id, "updated_at": updated_at},
            }
        )

    skip_sid = _skip_sid(request)
    for event in sidebar_events:
        await broadcast_sidebar_event(user.id, event, skip_sid=skip_sid)

    return {"updated_at": updated_at, "ops_applied": ops_applied}
