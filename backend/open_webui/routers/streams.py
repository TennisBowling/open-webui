import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.chats import Chats
from open_webui.socket.main import (
    STREAM_VERSION,
    get_active_streams_for_chat,
    get_stream_state,
    get_tool_results,
    stream_version_get,
)
from open_webui.utils.auth import get_verified_user


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

router = APIRouter()


def _find_chat_id_for_message(message_id: str, user_id: str) -> Optional[str]:
    # No direct index from message_id to chat_id, so walk the user's recent
    # chats. Snapshot fetch is rare (reconnect/reload), and callers pass
    # chat_id on the hot path to skip this scan.
    for chat in Chats.get_chat_list_by_user_id(user_id, include_archived=True):
        msg = Chats.get_message_by_id_and_message_id(chat.id, message_id)
        if msg:
            return chat.id
    return None


@router.get("/chat/{chat_id}/active")
async def get_active_streams(
    chat_id: str,
    user=Depends(get_verified_user),
):
    chat = Chats.get_chat_by_id_and_user_id(chat_id, user.id)
    if chat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found",
        )
    return {"streams": get_active_streams_for_chat(chat_id)}


@router.get("/{message_id}/snapshot")
async def get_stream_snapshot(
    message_id: str,
    chat_id: Optional[str] = None,
    user=Depends(get_verified_user),
):
    """Wire Contract #2 — stream v2 snapshot endpoint.

    Returns the current state of an in-flight (or completed) assistant
    message so the client can reconcile a missed delta window. Reads the
    version counter + tool-result bodies from Redis (populated by B9's
    v2 emitter); content_blocks come from Redis STREAM_STATE when the
    in-memory snapshot is authoritative, otherwise from the persisted
    chat_message row written by realtime save.
    """

    resolved_chat_id = chat_id
    if resolved_chat_id:
        chat = Chats.get_chat_by_id_and_user_id(resolved_chat_id, user.id)
        if chat is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat not found",
            )
    else:
        resolved_chat_id = _find_chat_id_for_message(message_id, user.id)
        if resolved_chat_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Message not found",
            )

    persisted = Chats.get_message_by_id_and_message_id(
        resolved_chat_id, message_id
    )
    in_flight_state = get_stream_state(message_id)
    state_chat_id = in_flight_state.get("chat_id") if in_flight_state else None
    if state_chat_id and state_chat_id != resolved_chat_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )
    raw_version = STREAM_VERSION.get(message_id)
    if not persisted and raw_version is None and not in_flight_state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )

    content_blocks = in_flight_state.get("content_blocks")
    if content_blocks is None:
        content_blocks = (persisted or {}).get("content_blocks") or []

    tool_results = get_tool_results(message_id)

    # While a stream is active or within its terminal grace period, the RAM
    # stream state is the source of truth. Otherwise fall back to the persisted
    # DB row and advertise v0 as a stable terminal snapshot.
    version = stream_version_get(message_id) if raw_version is not None or in_flight_state else 0
    msg_status = in_flight_state.get("status") or "done"

    usage = in_flight_state.get("usage") or (persisted or {}).get("usage")
    error = in_flight_state.get("error") or (persisted or {}).get("error")
    sources = in_flight_state.get("sources") or (persisted or {}).get("sources")
    selected_model_id = in_flight_state.get("selected_model_id") or (persisted or {}).get("selectedModelId")

    response: dict = {
        "version": version,
        "status": msg_status,
        "content_blocks": content_blocks,
        "tool_results": tool_results,
    }
    if usage is not None:
        response["usage"] = usage
    if error is not None:
        response["error"] = error
    if sources is not None:
        response["sources"] = sources
    if selected_model_id is not None:
        response["selected_model_id"] = selected_model_id
    return response
