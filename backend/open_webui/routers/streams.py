import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.chats import Chats
from open_webui.socket.main import (
    STREAM_VERSION,
    clear_stream_state,
    get_active_streams_for_chat,
    get_stream_runtime_metrics,
    get_stream_replay_events,
    get_stream_state,
    get_tool_results,
    set_stream_state,
    stream_run_get,
    stream_version_get,
)
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.response_durability import content_blocks_from_message
from open_webui.utils.stream_state import terminal_status_from_message


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

router = APIRouter()


@router.get("/metrics")
async def get_stream_metrics(user=Depends(get_admin_user)):
    return {"metrics": get_stream_runtime_metrics()}


def _clear_stale_active_stream(chat_id: str, message_id: str, message: Optional[dict]) -> Optional[str]:
    """Clear RAM stream state when DB already has a terminal message.

    A browser stop or provider/setup error can persist ``done``/``error`` before
    the v2.1 stream cleanup path runs. Without this guard, reloads keep seeing the
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


async def _find_chat_id_for_message(message_id: str, user_id: str) -> Optional[str]:
    # No direct index from message_id to chat_id, so walk the user's recent
    # chats. Snapshot fetch is rare (reconnect/reload), and callers pass
    # chat_id on the hot path to skip this scan.
    for chat in await Chats.get_chat_list_by_user_id(user_id, include_archived=True):
        msg = await Chats.get_message_by_id_and_message_id(chat.id, message_id)
        if msg:
            return chat.id
    return None


async def collect_active_streams(chat_id: str) -> list[dict]:
    """Active streams for a chat, stale-pruned against persisted terminal
    state. Single source for BOTH the /active endpoint and the chat-open
    embed (routers/chats.py bundles this into the open response so the
    client doesn't pay a second round-trip for it). Idle chats cost zero DB
    reads here — the per-stream persisted lookup only runs for reported
    streams."""
    streams = []
    for stream in get_active_streams_for_chat(chat_id):
        message_id = stream.get("message_id")
        persisted = (
            await Chats.get_message_by_id_and_message_id(chat_id, message_id)
            if message_id
            else None
        )
        if message_id and _clear_stale_active_stream(chat_id, message_id, persisted):
            continue
        streams.append(stream)
    return streams


@router.get("/chat/{chat_id}/active")
async def get_active_streams(
    chat_id: str,
    user=Depends(get_verified_user),
):
    chat = await Chats.get_chat_by_id_and_user_id(chat_id, user.id)
    if chat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found",
        )
    return {"streams": await collect_active_streams(chat_id)}


@router.get("/{message_id}/snapshot")
async def get_stream_snapshot(
    message_id: str,
    chat_id: Optional[str] = None,
    user=Depends(get_verified_user),
):
    """Wire Contract #2 — stream v2.1 snapshot endpoint.

    Returns the current state of an in-flight (or completed) assistant
    message so the client can reconcile a missed delta window. Reads the
    version counter + tool-result bodies from Redis (populated by B9's
    v2.1 emitter); content_blocks come from Redis STREAM_STATE when the
    in-memory snapshot is authoritative, otherwise from the persisted
    chat_message row written by realtime save.
    """

    resolved_chat_id = chat_id
    if resolved_chat_id:
        chat = await Chats.get_chat_by_id_and_user_id(resolved_chat_id, user.id)
        if chat is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat not found",
            )
    else:
        resolved_chat_id = await _find_chat_id_for_message(message_id, user.id)
        if resolved_chat_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Message not found",
            )

    persisted = await Chats.get_message_by_id_and_message_id(
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
        content_blocks = content_blocks_from_message(persisted)
    elif (
        not content_blocks
        and in_flight_state.get("status") != "in_progress"
        and persisted
    ):
        # Non-streaming/legacy completions may have reached storage before
        # content_blocks existed. A terminal snapshot must still carry the
        # durable answer instead of turning it into an empty finished bubble.
        content_blocks = content_blocks_from_message(persisted)

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
    model = (persisted or {}).get("model") or in_flight_state.get("model")

    # The assistant row's true parent (the user message it answers). Lets a client
    # materializing a not-yet-loaded assistant (e.g. attaching to a server-drained
    # generation, or one whose `chat:user-message` event was missed) parent it
    # under the right user node instead of the tab's stale `history.currentId`.
    parent_id = (persisted or {}).get("parentId") or in_flight_state.get("parentId")

    response: dict = {
        "version": version,
        "status": msg_status,
        "content_blocks": content_blocks,
        "tool_results": tool_results,
    }
    # Run id (epoch) of the run this snapshot describes. Lets the client decide
    # authority exactly: a snapshot from a NEWER run always replaces the local
    # mirror (retry/continue reset the version space); same-run snapshots
    # compare by version. Omitted when unknown (e.g. terminal DB fallback after
    # cleanup) — clients then fall back to version comparison.
    snapshot_run = in_flight_state.get("run") or stream_run_get(message_id)
    if snapshot_run:
        response["run"] = int(snapshot_run)
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
    if parent_id is not None:
        response["parentId"] = parent_id
    return response


@router.get("/{message_id}/deltas")
async def get_stream_deltas(
    message_id: str,
    chat_id: Optional[str] = None,
    after_version: int = 0,
    run: Optional[int] = None,
    user=Depends(get_verified_user),
):
    resolved_chat_id = chat_id
    if resolved_chat_id:
        chat = await Chats.get_chat_by_id_and_user_id(resolved_chat_id, user.id)
        if chat is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat not found",
            )
    else:
        resolved_chat_id = await _find_chat_id_for_message(message_id, user.id)
        if resolved_chat_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Message not found",
            )

    in_flight_state = get_stream_state(message_id)
    state_chat_id = in_flight_state.get("chat_id") if in_flight_state else None
    if state_chat_id and state_chat_id != resolved_chat_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )
    persisted = await Chats.get_message_by_id_and_message_id(resolved_chat_id, message_id)
    if not persisted and not in_flight_state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )

    return await get_stream_replay_events(message_id, after_version, run=run)


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
    available. Payloads come from ``browser_live_payload`` (same shape as the SSE
    ``browser:frame`` event — including the verification handoff surface).
    """
    from open_webui.utils.container_workspace import read_browser_live_sessions

    resolved_chat_id = chat_id
    if resolved_chat_id:
        chat = await Chats.get_chat_by_id_and_user_id(resolved_chat_id, user.id)
        if chat is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found"
            )
    else:
        resolved_chat_id = await _find_chat_id_for_message(message_id, user.id)
        if resolved_chat_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Message not found"
            )

    data_root = str(
        getattr(request.app.state.config, "CONTAINER_DATA_ROOT", "") or ""
    )
    if not data_root:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return await _browser_live_response(data_root, resolved_chat_id)


@router.get("/browser/live")
async def get_browser_live(
    request: Request,
    chat_id: str,
    user=Depends(get_verified_user),
):
    """Lightweight live browser state for the panel's verification poller.

    The panel polls this while a human-verification handoff is armed so it can
    watch for the daemon's auto-clear signal WITHOUT round-tripping a POST
    handoff through the container (the old 1.5s POST-snapshot loop contended on
    the daemon's per-session mutex with the user's own clicks and re-ran
    detection on every refresh). Reads host files only — zero container cost.

    Response is the same browser_live_payload shape as the reattach endpoint
    (top-level single frame + ``sessions`` for every tab). 204 when nothing is
    available.
    """
    from open_webui.utils.container_workspace import read_browser_live_sessions

    chat = await Chats.get_chat_by_id_and_user_id(chat_id, user.id)
    if chat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found"
        )
    data_root = str(
        getattr(request.app.state.config, "CONTAINER_DATA_ROOT", "") or ""
    )
    if not data_root:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return await _browser_live_response(data_root, chat_id)


async def _browser_live_response(data_root: str, chat_id: str) -> Response:
    """Shared tail for the reattach + live endpoints: read every session's live
    pair and shape it through browser_live_payloads (ONE builder, no drift)."""
    from open_webui.utils.container_workspace import (
        browser_live_payloads,
        read_browser_live_sessions,
    )

    sessions = await read_browser_live_sessions(data_root, chat_id)
    session_payloads = browser_live_payloads(sessions)

    if not session_payloads:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Back-compat top-level fields: pick the most-recently-started session as the
    # single frame for older callers. The full set rides in `sessions`.
    primary = max(session_payloads, key=lambda p: p.get("startedAt", 0))
    return {**primary, "sessions": session_payloads}
