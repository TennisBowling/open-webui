import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.chats import Chats
from open_webui.socket.main import (
    STREAM_STATE,
    STREAM_VERSION,
    TOOL_RESULTS,
)
from open_webui.utils.auth import get_verified_user


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

router = APIRouter()


def _coerce_version(raw) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _coerce_dict(raw) -> dict:
    return raw if isinstance(raw, dict) else {}


def _find_chat_id_for_message(message_id: str, user_id: str) -> Optional[str]:
    # No direct index from message_id to chat_id, so walk the user's recent
    # chats. Snapshot fetch is rare (reconnect/reload), and callers pass
    # chat_id on the hot path to skip this scan.
    for chat in Chats.get_chat_list_by_user_id(user_id, include_archived=True):
        msg = Chats.get_message_by_id_and_message_id(chat.id, message_id)
        if msg:
            return chat.id
    return None


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
    raw_version = STREAM_VERSION.get(message_id)
    if not persisted and raw_version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )

    in_flight_state = _coerce_dict(STREAM_STATE.get(message_id))
    content_blocks = in_flight_state.get("content_blocks")
    if content_blocks is None:
        content_blocks = (persisted or {}).get("content_blocks") or []

    tool_results = _coerce_dict(TOOL_RESULTS.get(message_id))

    # An in-flight stream advertises a version > 0 in Redis. Without a Redis
    # entry the persisted row is terminal — report v0 so the client treats
    # the snapshot as a stable final-state.
    version = _coerce_version(raw_version) if raw_version is not None else 0
    msg_status = (
        in_flight_state.get("status", "in_progress")
        if raw_version is not None
        else "done"
    )

    usage = in_flight_state.get("usage") or (persisted or {}).get("usage")
    error = in_flight_state.get("error") or (persisted or {}).get("error")

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
    return response
