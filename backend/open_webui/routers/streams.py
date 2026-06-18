import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.chats import Chats
from open_webui.socket.main import (
    STREAM_VERSION,
    clear_stream_state,
    get_active_streams_for_chat,
    get_stream_state,
    get_tool_results,
    set_stream_state,
    stream_version_get,
)
from open_webui.utils.auth import get_verified_user
from open_webui.utils.stream_state import terminal_status_from_message


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

router = APIRouter()


def _clear_stale_active_stream(chat_id: str, message_id: str, message: Optional[dict]) -> Optional[str]:
    """Clear RAM stream state when DB already has a terminal message.

    A browser stop or provider/setup error can persist ``done``/``error`` before
    the v2 stream cleanup path runs. Without this guard, reloads keep seeing the
    message in ``/active`` and repeatedly request snapshots for a dead stream.
    """
    status = terminal_status_from_message(message)
    if not status:
        return None

    set_stream_state(
        message_id,
        {
            "chat_id": chat_id,
            "status": status,
            "content_blocks": message.get("content_blocks") or [],
            **({"error": message.get("error")} if message.get("error") else {}),
            **({"usage": message.get("usage")} if message.get("usage") else {}),
        },
    )
    clear_stream_state(message_id, delay=0)
    return status


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
    streams = []
    for stream in get_active_streams_for_chat(chat_id):
        message_id = stream.get("message_id")
        persisted = (
            Chats.get_message_by_id_and_message_id(chat_id, message_id)
            if message_id
            else None
        )
        if message_id and _clear_stale_active_stream(chat_id, message_id, persisted):
            continue
        streams.append(stream)
    return {"streams": streams}


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
    persisted_terminal_status = terminal_status_from_message(persisted)
    if persisted_terminal_status and in_flight_state.get("status") == "in_progress":
        _clear_stale_active_stream(resolved_chat_id, message_id, persisted)
        in_flight_state = {}
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
    #
    # Snapshot-version decoupling (streaming O(N^2) fix, Part C): the per-token
    # native path no longer rewrites the RAM snapshot every token — it snapshots
    # on a bounded cadence and stamps it with `snapshot_version` = the last wire
    # version whose content is fully contained in that snapshot. The live wire
    # version counter (STREAM_VERSION) races ahead between snapshots. We MUST
    # advertise the snapshot's own version, never the live counter, or a reload
    # could receive version=V_live with content that only covers V_snapshot<V_live
    # and then drop the buffered deltas (version<=V_live) that would have filled
    # the gap — permanent missing text. Advertising V_snapshot keeps
    # (content, version) consistent: deltas <= V_snapshot are already in content,
    # deltas > V_snapshot are replayed by the client.
    if (
        in_flight_state
        and in_flight_state.get("status") == "in_progress"
        and in_flight_state.get("snapshot_version") is not None
    ):
        version = int(in_flight_state["snapshot_version"])
    else:
        version = stream_version_get(message_id) if raw_version is not None or in_flight_state else 0
    msg_status = in_flight_state.get("status") or persisted_terminal_status or "done"

    usage = in_flight_state.get("usage") or (persisted or {}).get("usage")
    error = in_flight_state.get("error") or (persisted or {}).get("error")
    sources = in_flight_state.get("sources") or (persisted or {}).get("sources")
    selected_model_id = in_flight_state.get("selected_model_id") or (persisted or {}).get("selectedModelId")
    # The canonical model id for the assistant row. Lets a client that is
    # materializing a not-yet-loaded message (e.g. attaching to a server-drained
    # generation) label it with the RIGHT model rather than guessing from the
    # tab's current selection.
    model = (persisted or {}).get("model")

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
    if model is not None:
        response["model"] = model
    return response


@router.get("/browser/{message_id}/frame")
async def get_browser_frame(
    request: Request,
    message_id: str,
    chat_id: Optional[str] = None,
    user=Depends(get_verified_user),
):
    """Live browser frame(s) for reattach.

    The browser daemon's live frames are fire-and-forget over the socket (not in
    the stream snapshot), so a tab reloading mid-action fetches the current
    frame(s) here to seed the live panel until the next socket frame arrives. Reads
    the daemon-written per-session ``.cam/browser/{live-<s>.jpg,state-<s>.json}``
    (plus the legacy un-suffixed pair for the default session) host-side.

    Response carries BOTH a top-level single frame (the default/most-recent session
    — back-compat for callers that only read one) AND a ``sessions`` array of every
    active tab so the panel can restore all of them. Returns 204 when nothing is
    available.
    """
    from open_webui.utils.container_workspace import read_browser_live_sessions

    resolved_chat_id = chat_id
    if resolved_chat_id:
        chat = Chats.get_chat_by_id_and_user_id(resolved_chat_id, user.id)
        if chat is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found"
            )
    else:
        resolved_chat_id = _find_chat_id_for_message(message_id, user.id)
        if resolved_chat_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Message not found"
            )

    data_root = str(
        getattr(request.app.state.config, "CONTAINER_DATA_ROOT", "") or ""
    )
    if not data_root:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    sessions = read_browser_live_sessions(data_root, resolved_chat_id)
    # Build a per-session payload list; only include sessions that have a frame.
    session_payloads: list[dict] = []
    for session_id_key, live in sessions.items():
        if not live or not live.get("frame"):
            continue
        state = live.get("state") or {}
        # A daemon that restarted (idle-reap / crash) marks the leftover state from
        # a previous turn done+stale. Report it terminal so a reloading tab freezes
        # it instead of showing a months-old "Navigating…" as if it were live.
        is_done = bool(state.get("done")) or bool(state.get("stale"))
        session_payloads.append(
            {
                "session": state.get("session") or session_id_key,
                "frame": live.get("frame"),
                "url": state.get("url", ""),
                "title": state.get("title", ""),
                "phase": "done" if is_done else state.get("phase", ""),
                "action": state.get("action", ""),
                "elapsedMs": state.get("elapsedMs", 0),
                "startedAt": state.get("startedAt", 0),
                "done": is_done,
            }
        )

    if not session_payloads:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Back-compat top-level fields: pick the most-recently-started session as the
    # single frame for older callers. The full set rides in `sessions`.
    primary = max(session_payloads, key=lambda p: p.get("startedAt", 0))
    return {**primary, "sessions": session_payloads}
