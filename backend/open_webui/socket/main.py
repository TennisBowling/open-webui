import asyncio
import copy
import hashlib
import json
import os
import random
import tempfile
from collections import OrderedDict, deque

import socketio
import logging
import sys
import time
from typing import Dict, Set, Optional
from redis import asyncio as aioredis
import pycrdt as Y

from open_webui.models.users import Users, UserNameResponse
from open_webui.models.channels import Channels
from open_webui.models.chats import Chats
from open_webui.models.notes import Notes, NoteUpdateForm
from open_webui.utils.redis import (
    get_sentinels_from_env,
    get_sentinel_url_from_env,
)

from open_webui.env import (
    ENABLE_WEBSOCKET_SUPPORT,
    WEBSOCKET_MANAGER,
    WEBSOCKET_REDIS_URL,
    WEBSOCKET_REDIS_CLUSTER,
    WEBSOCKET_REDIS_LOCK_TIMEOUT,
    WEBSOCKET_SENTINEL_PORT,
    WEBSOCKET_SENTINEL_HOSTS,
    WEBSOCKET_MAX_MESSAGE_SIZE,
    REDIS_KEY_PREFIX,
    STREAM_STATE_TTL_SECONDS,
    STREAM_PROTOCOL_VERSION,
    STREAM_DELTA_BATCH_ENABLED,
    STREAM_DELTA_BATCH_WINDOW_MS,
    STREAM_DELTA_BATCH_MAX_DELAY_MS,
    STREAM_DELTA_FIRST_TOKEN_IMMEDIATE,
    STREAM_VERSION_STORE_FLUSH_EVERY,
    STREAM_REPLAY_BUFFER_MAX_EVENTS,
    STREAM_REPLAY_BUFFER_MAX_BYTES,
    STREAM_REPLAY_BUFFER_TTL_SECONDS,
    STREAM_CLIENT_ACK_INTERVAL_MS,
    STREAM_CLIENT_LAG_MAX_VERSIONS,
    STREAM_HIDDEN_CATCHUP_MS,
    STREAM_RUNTIME_METRICS,
    STREAM_TOOL_RESULT_BODY_MAX_BYTES,
    STREAM_TOOL_RESULT_BODY_MAX_BYTES_PER_MESSAGE,
    STREAM_TOOL_RESULT_BODY_SPILL_DIR,
)
from open_webui.utils.auth import decode_token
from open_webui.socket.utils import RedisDict, RedisLock, YdocManager
from open_webui.socket.serializer import orjson_serializer
from open_webui.tasks import (
    create_task,
    stop_item_tasks,
    set_pending_model_switch,
    set_pending_service_tier,
    set_pending_tool_selection,
    list_generation_task_ids_by_item,
)
from open_webui.utils.live_tool_selection import normalize_live_tool_selection
from open_webui.utils.redis import get_redis_connection
from open_webui.utils.access_control import has_access, get_users_with_access
from open_webui.models.token_usage import token_groups


from open_webui.env import (
    GLOBAL_LOG_LEVEL,
    SRC_LOG_LEVELS,
)


logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["SOCKET"])


REDIS = None


# Engine.IO keepalive tuning (metered/slow-link downlink efficiency). The default
# ~25s ping interval means each idle socket exchanges a ping/pong every 25s — a
# steady background trickle that also spins up the radio on cellular. Widening the
# interval cuts that keepalive chatter; dead-connection detection is slower but
# the reconnect backstops (stream:subscribe re-sync, /snapshot + /deltas replay,
# resume-poll) recover a stranded stream regardless of how fast the socket layer
# notices the drop. Configurable via env; defaults widen 25s -> 45s.
def _sio_ping_kwargs() -> dict:
    try:
        ping_interval = int(os.environ.get("WEBSOCKET_PING_INTERVAL", "45") or "45")
    except (TypeError, ValueError):
        ping_interval = 45
    try:
        ping_timeout = int(os.environ.get("WEBSOCKET_PING_TIMEOUT", "25") or "25")
    except (TypeError, ValueError):
        ping_timeout = 25
    return {
        "ping_interval": max(1, ping_interval),
        "ping_timeout": max(1, ping_timeout),
    }


_SIO_PING_KWARGS = _sio_ping_kwargs()

if WEBSOCKET_MANAGER == "redis":
    if WEBSOCKET_SENTINEL_HOSTS:
        mgr = socketio.AsyncRedisManager(
            get_sentinel_url_from_env(
                WEBSOCKET_REDIS_URL, WEBSOCKET_SENTINEL_HOSTS, WEBSOCKET_SENTINEL_PORT
            )
        )
    else:
        mgr = socketio.AsyncRedisManager(WEBSOCKET_REDIS_URL)
    sio = socketio.AsyncServer(
        cors_allowed_origins=[],
        async_mode="asgi",
        transports=(["websocket"] if ENABLE_WEBSOCKET_SUPPORT else ["polling"]),
        allow_upgrades=ENABLE_WEBSOCKET_SUPPORT,
        always_connect=True,
        client_manager=mgr,
        max_http_buffer_size=WEBSOCKET_MAX_MESSAGE_SIZE,
        json=orjson_serializer,
        **_SIO_PING_KWARGS,
    )
else:
    sio = socketio.AsyncServer(
        cors_allowed_origins=[],
        async_mode="asgi",
        transports=(["websocket"] if ENABLE_WEBSOCKET_SUPPORT else ["polling"]),
        allow_upgrades=ENABLE_WEBSOCKET_SUPPORT,
        always_connect=True,
        max_http_buffer_size=WEBSOCKET_MAX_MESSAGE_SIZE,
        json=orjson_serializer,
        **_SIO_PING_KWARGS,
    )


# Timeout duration in seconds
TIMEOUT_DURATION = 3

# Dictionary to maintain the user pool

if WEBSOCKET_MANAGER == "redis":
    log.debug("Using Redis to manage websockets.")
    REDIS = get_redis_connection(
        redis_url=WEBSOCKET_REDIS_URL,
        redis_sentinels=get_sentinels_from_env(
            WEBSOCKET_SENTINEL_HOSTS, WEBSOCKET_SENTINEL_PORT
        ),
        redis_cluster=WEBSOCKET_REDIS_CLUSTER,
        async_mode=True,
    )

    redis_sentinels = get_sentinels_from_env(
        WEBSOCKET_SENTINEL_HOSTS, WEBSOCKET_SENTINEL_PORT
    )
    SESSION_POOL = RedisDict(
        f"{REDIS_KEY_PREFIX}:session_pool",
        redis_url=WEBSOCKET_REDIS_URL,
        redis_sentinels=redis_sentinels,
        redis_cluster=WEBSOCKET_REDIS_CLUSTER,
    )
    USER_POOL = RedisDict(
        f"{REDIS_KEY_PREFIX}:user_pool",
        redis_url=WEBSOCKET_REDIS_URL,
        redis_sentinels=redis_sentinels,
        redis_cluster=WEBSOCKET_REDIS_CLUSTER,
    )
    PRIMARY_SESSION_PER_USER = RedisDict(
        f"{REDIS_KEY_PREFIX}:primary_session_per_user",
        redis_url=WEBSOCKET_REDIS_URL,
        redis_sentinels=redis_sentinels,
        redis_cluster=WEBSOCKET_REDIS_CLUSTER,
    )

    # Token usage tracking data structures
    TOKEN_GROUPS = RedisDict(
        "open-webui:token_groups",
        redis_url=WEBSOCKET_REDIS_URL,
        redis_sentinels=redis_sentinels,
    )
    TOKEN_USAGE = RedisDict(
        "open-webui:token_usage",
        redis_url=WEBSOCKET_REDIS_URL,
        redis_sentinels=redis_sentinels,
    )

    # Stream v2.1 state: per-message version counter, slim content snapshot, and
    # tool result cache. All are keyed by message_id. Cleared on chat:done /
    # error / cancel. Snapshot endpoint (B1) reads from here.
    STREAM_VERSION = RedisDict(
        f"{REDIS_KEY_PREFIX}:stream_version",
        redis_url=WEBSOCKET_REDIS_URL,
        redis_sentinels=redis_sentinels,
        redis_cluster=WEBSOCKET_REDIS_CLUSTER,
    )
    TOOL_RESULTS = RedisDict(
        f"{REDIS_KEY_PREFIX}:tool_results",
        redis_url=WEBSOCKET_REDIS_URL,
        redis_sentinels=redis_sentinels,
        redis_cluster=WEBSOCKET_REDIS_CLUSTER,
    )
    STREAM_STATE = RedisDict(
        f"{REDIS_KEY_PREFIX}:stream_state",
        redis_url=WEBSOCKET_REDIS_URL,
        redis_sentinels=redis_sentinels,
        redis_cluster=WEBSOCKET_REDIS_CLUSTER,
    )
    PRIMARY_SESSION_PER_USER = RedisDict(
        f"{REDIS_KEY_PREFIX}:primary_session_per_user",
        redis_url=WEBSOCKET_REDIS_URL,
        redis_sentinels=redis_sentinels,
        redis_cluster=WEBSOCKET_REDIS_CLUSTER,
    )

    clean_up_lock = RedisLock(
        redis_url=WEBSOCKET_REDIS_URL,
        lock_name=f"{REDIS_KEY_PREFIX}:usage_cleanup_lock",
        timeout_secs=WEBSOCKET_REDIS_LOCK_TIMEOUT,
        redis_sentinels=redis_sentinels,
        redis_cluster=WEBSOCKET_REDIS_CLUSTER,
    )
    aquire_func = clean_up_lock.aquire_lock
    renew_func = clean_up_lock.renew_lock
    release_func = clean_up_lock.release_lock
else:
    SESSION_POOL = {}
    USER_POOL = {}
    PRIMARY_SESSION_PER_USER = {}

    # Token usage tracking data structures (in-memory)
    TOKEN_GROUPS = {}
    TOKEN_USAGE = {}

    STREAM_VERSION = {}
    TOOL_RESULTS = {}
    STREAM_STATE = {}
    PRIMARY_SESSION_PER_USER = {}

    aquire_func = release_func = renew_func = lambda: True


YDOC_MANAGER = YdocManager(
    redis=REDIS,
    redis_key_prefix=f"{REDIS_KEY_PREFIX}:ydoc:documents",
)


app = socketio.ASGIApp(
    sio,
    socketio_path="/ws/socket.io",
)


def get_models_in_use():
    return []


def get_active_user_ids():
    """Get the list of active user IDs."""
    return list(USER_POOL.keys())


def get_user_active_status(user_id):
    """Check if a user is currently active."""
    return user_id in USER_POOL


def get_user_id_from_session_pool(sid):
    user = SESSION_POOL.get(sid)
    if user:
        return user["id"]
    return None


def _unique_session_ids(session_ids):
    unique = []
    seen = set()
    for session_id in session_ids or []:
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        unique.append(session_id)
    return unique


def _register_user_session(user, sid):
    SESSION_POOL[sid] = user.model_dump(exclude=["date_of_birth", "bio", "gender"])

    current_sessions = USER_POOL.get(user.id, []) or []
    if isinstance(current_sessions, str):
        current_sessions = [current_sessions]

    USER_POOL[user.id] = _unique_session_ids(
        [
            session_id
            for session_id in [*current_sessions, sid]
            if session_id == sid or session_id in SESSION_POOL
        ]
    )

    return _elect_primary_session(user.id, sid)


def _elect_primary_session(user_id, sid):
    """Set sid as the user's primary session if no live primary is currently
    recorded. Returns the resulting primary sid for this user.

    Atomic against concurrent callers when backed by Redis: a single
    HSETNX claims the slot, and if it's already claimed we only attempt
    to replace it via compare_and_swap when the recorded session has
    actually disappeared from SESSION_POOL. This closes the race where
    two workers (or a connect + disconnect on different workers) could
    both read `current = None` and both write themselves as primary."""
    # Fast path: try to atomically claim the slot if nothing is recorded.
    setnx = getattr(PRIMARY_SESSION_PER_USER, "setnx", None)
    if setnx is not None:
        if setnx(user_id, sid):
            return sid
    else:
        # In-memory fallback (single worker, no real race).
        if user_id not in PRIMARY_SESSION_PER_USER:
            PRIMARY_SESSION_PER_USER[user_id] = sid
            return sid

    current = PRIMARY_SESSION_PER_USER.get(user_id)
    if current and current in SESSION_POOL:
        return current

    # Recorded primary is stale (session gone). Try to atomically swap
    # it for ourselves; if another caller swapped first we accept their
    # winner rather than overwriting it.
    cas = getattr(PRIMARY_SESSION_PER_USER, "compare_and_swap", None)
    if cas is not None:
        if cas(user_id, current, sid):
            return sid
        return PRIMARY_SESSION_PER_USER.get(user_id) or sid

    PRIMARY_SESSION_PER_USER[user_id] = sid
    return sid


def is_primary_session(user_id, sid) -> bool:
    return PRIMARY_SESSION_PER_USER.get(user_id) == sid


def get_primary_session(user_id):
    return PRIMARY_SESSION_PER_USER.get(user_id)


def get_session_ids_from_room(room):
    """Get all session IDs from a specific room."""
    active_session_ids = sio.manager.get_participants(
        namespace="/",
        room=room,
    )
    return [session_id[0] for session_id in active_session_ids]


def get_user_ids_from_room(room):
    active_session_ids = get_session_ids_from_room(room)

    active_user_ids = list(
        set([SESSION_POOL.get(session_id)["id"] for session_id in active_session_ids])
    )
    return active_user_ids


def get_active_status_by_user_id(user_id):
    if user_id in USER_POOL:
        return True
    return False


async def get_token_groups():
    """Get all token groups"""
    return await token_groups.get_token_groups()


async def set_token_group(
    group_name: str,
    models: list,
    limit: int = None,
    reset_time: str = "00:00",
    reset_timezone: str = "UTC",
    window_duration: int = None,
):
    """Set a token group"""
    # Try to update first, if not found create new
    if not await token_groups.update_token_group(
        group_name, models, limit, window_duration
    ):
        return await token_groups.create_token_group(
            group_name, models, limit or 0, reset_time, reset_timezone, window_duration
        )


async def update_token_group(
    group_name: str, models: list = None, limit: int = None, window_duration: int = None
):
    """Update an existing token group"""
    return await token_groups.update_token_group(
        group_name, models, limit, window_duration
    )


async def delete_token_group(group_name: str):
    """Delete a token group"""
    return await token_groups.delete_token_group(group_name)


async def get_token_usage():
    """Get current token usage for all groups from database"""
    # Import here to avoid circular imports
    from open_webui.models.token_usage import token_groups as db_token_groups

    groups = await db_token_groups.get_token_groups()
    return {name: group_data["usage"] for name, group_data in groups.items()}


async def _caller_owns_task(task_id, user_id, role=None) -> bool:
    """True if `task_id`'s owning chat belongs to `user_id` (admins always True).

    A task's item id is the bare chat id (a generation) or
    `subagent-rerun:{chat}:{entry}` (a rerun). Used to authorize the socket
    model-switch / service-tier-switch handlers, which otherwise trust a
    client-supplied task_id and let any user steer another user's generation."""
    if role == "admin":
        return True
    if not task_id or not user_id:
        return False
    try:
        from open_webui.tasks import get_task_item_id

        item_id = await get_task_item_id(REDIS, task_id)
    except Exception:
        item_id = None
    if not item_id:
        return False
    chat_id = item_id
    if isinstance(item_id, str) and item_id.startswith("subagent-rerun:"):
        parts = item_id.split(":")
        chat_id = parts[1] if len(parts) > 1 else None
    if not chat_id:
        return False
    try:
        from open_webui.models.chats import Chats

        return await Chats.get_chat_by_id_and_user_id(chat_id, user_id) is not None
    except Exception:
        return False


@sio.on("usage")
async def usage(sid, data):
    if sid not in SESSION_POOL or not isinstance(data, dict):
        return
    user = SESSION_POOL.get(sid)
    user_id = user.get("id") if isinstance(user, dict) else None
    model_id = data.get("model")
    chat_id = data.get("chat_id")
    if not user_id or not model_id:
        return

    # P3_1: this handler writes SHARED accounting (rate-limit token groups, per-chat
    # conversation totals, embedded USD cost, daily/model aggregates) from
    # client-supplied model/usage/cost/chat_id. The server already accounts usage
    # from the trusted provider stream (routers/openai.py) and NO legitimate client
    # emits 'usage', so harden it: require the caller to OWN chat_id (no corrupting a
    # victim's totals / rebinding the row's user_id), and NEVER take cost from the
    # client (the rate card is the only trusted source of USD).
    if chat_id:
        try:
            from open_webui.models.chats import Chats

            if await Chats.get_chat_by_id_and_user_id(chat_id, user_id) is None:
                log.warning(
                    "[socket:usage] dropping usage for non-owned chat %s (user %s)",
                    chat_id,
                    user_id,
                )
                return
        except Exception:
            return

    usage_data = data.get("usage", {})
    if isinstance(usage_data, dict) and "cost" in usage_data:
        usage_data = {k: v for k, v in usage_data.items() if k != "cost"}

    await process_token_usage(model_id, usage_data, chat_id=chat_id, user_id=user_id)


@sio.on("model-switch")
async def model_switch(sid, data):
    """
    Handle model switch requests during an active agentic loop.
    The switch will be applied at the next iteration of the tool call loop.
    """
    if sid not in SESSION_POOL:
        return {"status": False, "message": "Session not found"}

    _u = SESSION_POOL.get(sid) or {}
    _uid = _u.get("id") if isinstance(_u, dict) else None
    _role = _u.get("role") if isinstance(_u, dict) else None

    chat_id = data.get("chat_id")
    new_model_id = data.get("model_id")
    task_id = data.get("task_id")

    if not new_model_id:
        return {"status": False, "message": "No model_id provided"}

    log.info(
        f"Model switch request: chat_id={chat_id}, task_id={task_id}, new_model={new_model_id}"
    )

    # C3: only the OWNER (or an admin) may steer a task/chat. Without this, a
    # client-supplied task_id lets any user force a victim's running generation
    # (and its subagents) onto an arbitrary/expensive/inaccessible model.
    if task_id and not await _caller_owns_task(task_id, _uid, _role):
        return {"status": False, "message": "Not authorized for this task"}

    # If a specific task_id is provided, switch for that task
    if task_id:
        result = await set_pending_model_switch(task_id, new_model_id)

        # Emit event to notify frontend that model switch is pending
        await sio.emit(
            "events",
            {
                "chat_id": chat_id,
                "message_id": data.get("message_id"),
                "data": {
                    "type": "model-switch:pending",
                    "data": {
                        "task_id": task_id,
                        "model_id": new_model_id,
                    },
                },
            },
            to=sid,
        )

        return result

    # If no task_id provided, try to find active tasks for the chat
    if chat_id:
        # C3: require chat ownership before steering all of its tasks.
        if _role != "admin":
            try:
                from open_webui.models.chats import Chats

                if await Chats.get_chat_by_id_and_user_id(chat_id, _uid) is None:
                    return {"status": False, "message": "Not authorized for this chat"}
            except Exception:
                return {"status": False, "message": "Not authorized for this chat"}
        task_ids = await list_generation_task_ids_by_item(REDIS, chat_id)
        if task_ids:
            results = []
            for tid in task_ids:
                result = await set_pending_model_switch(tid, new_model_id)
                results.append(result)

            # Emit event for each task
            await sio.emit(
                "events",
                {
                    "chat_id": chat_id,
                    "message_id": data.get("message_id"),
                    "data": {
                        "type": "model-switch:pending",
                        "data": {
                            "task_ids": task_ids,
                            "model_id": new_model_id,
                        },
                    },
                },
                to=sid,
            )

            return {
                "status": True,
                "message": f"Model switch queued for {len(task_ids)} active task(s)",
            }
        else:
            return {"status": False, "message": "No active tasks found for this chat"}

    return {"status": False, "message": "No chat_id or task_id provided"}


@sio.on("service-tier-switch")
async def service_tier_switch(sid, data):
    """
    Handle service_tier change requests during an active agentic loop.
    The change will be applied at the next iteration of the tool call loop
    so the next outbound LLM request uses the new tier.
    """
    if sid not in SESSION_POOL:
        return {"status": False, "message": "Session not found"}

    _u = SESSION_POOL.get(sid) or {}
    _uid = _u.get("id") if isinstance(_u, dict) else None
    _role = _u.get("role") if isinstance(_u, dict) else None

    chat_id = data.get("chat_id")
    new_tier = data.get("service_tier")
    task_id = data.get("task_id")

    if not new_tier:
        return {"status": False, "message": "No service_tier provided"}

    log.info(
        f"service_tier change request: chat_id={chat_id}, task_id={task_id}, new_tier={new_tier}"
    )

    # C3: only the owner (or an admin) may steer a task/chat.
    if task_id and not await _caller_owns_task(task_id, _uid, _role):
        return {"status": False, "message": "Not authorized for this task"}
    if not task_id and chat_id and _role != "admin":
        try:
            from open_webui.models.chats import Chats

            if await Chats.get_chat_by_id_and_user_id(chat_id, _uid) is None:
                return {"status": False, "message": "Not authorized for this chat"}
        except Exception:
            return {"status": False, "message": "Not authorized for this chat"}

    # If a specific task_id is provided, change tier for that task
    if task_id:
        result = await set_pending_service_tier(task_id, new_tier)

        await sio.emit(
            "events",
            {
                "chat_id": chat_id,
                "message_id": data.get("message_id"),
                "data": {
                    "type": "service-tier-switch:pending",
                    "data": {
                        "task_id": task_id,
                        "service_tier": new_tier,
                    },
                },
            },
            to=sid,
        )

        return result

    # If no task_id provided, queue for all active tasks on this chat
    if chat_id:
        task_ids = await list_generation_task_ids_by_item(REDIS, chat_id)
        if task_ids:
            for tid in task_ids:
                await set_pending_service_tier(tid, new_tier)

            await sio.emit(
                "events",
                {
                    "chat_id": chat_id,
                    "message_id": data.get("message_id"),
                    "data": {
                        "type": "service-tier-switch:pending",
                        "data": {
                            "task_ids": task_ids,
                            "service_tier": new_tier,
                        },
                    },
                },
                to=sid,
            )

            return {
                "status": True,
                "message": f"service_tier change queued for {len(task_ids)} active task(s)",
            }
        else:
            return {"status": False, "message": "No active tasks found for this chat"}

    return {"status": False, "message": "No chat_id or task_id provided"}


@sio.on("tool-selection:update")
async def tool_selection_update(sid, data):
    """Queue a complete tool selection for an active agentic generation.

    The operation is acknowledged only after its latest-value snapshot is
    stored. The owning agentic loop atomically consumes it immediately before
    the next provider round, where it rebuilds both callable tools and provider
    schemas.
    """

    if sid not in SESSION_POOL:
        return {"status": False, "message": "Session not found"}
    if not isinstance(data, dict):
        return {"status": False, "message": "Invalid operation payload"}

    session_user = SESSION_POOL.get(sid) or {}
    user_id = session_user.get("id") if isinstance(session_user, dict) else None
    role = session_user.get("role") if isinstance(session_user, dict) else None
    chat_id = data.get("chat_id")
    task_id = data.get("task_id")

    if not task_id:
        return {"status": False, "message": "No task_id provided"}
    if not await _caller_owns_task(task_id, user_id, role):
        return {"status": False, "message": "Not authorized for this task"}

    try:
        # Bound nested direct-server specs before retaining them in Redis.
        if len(json.dumps(data.get("selection", {}), separators=(",", ":"))) > 1_000_000:
            return {"status": False, "message": "Tool selection payload is too large"}
        selection = normalize_live_tool_selection(data.get("selection"))
    except (TypeError, ValueError) as exc:
        return {"status": False, "message": str(exc)}

    result = await set_pending_tool_selection(REDIS, str(task_id), selection)
    if result.get("status"):
        log.info(
            "Tool selection update acknowledged: chat_id=%s task_id=%s operation_id=%s",
            chat_id,
            task_id,
            selection.get("operation_id"),
        )
    return result


def usage_has_data(usage_data: dict | None) -> bool:
    """Return True only if a provider `usage` payload carries real information.

    Some providers (notably the bare-id "C" gemini provider) emit a fully
    zero-filled `usage` object on MANY intermediate streaming chunks, not just
    the final one — e.g. ``{"prompt_tokens": 0, "completion_tokens": 0,
    "total_tokens": 0, "prompt_tokens_details": {"cached_tokens": 0}, ...}``.
    These payloads are syntactically non-empty (so a bare ``if usage:`` is
    truthy) but contain no countable tokens and no cost. Recording them:
      * pollutes ``token_usage_event`` with ~37% all-zero junk rows,
      * clobbers ``conversation_token_usage.last_input_tokens`` to 0 — which is
        exactly what the in-chat pill shows as "Latest Input", so the input
        counter reads 0 after an agentic/tool-call turn,
      * overwrites the per-message ``meta.usage`` and emits a zero live delta.

    A payload counts as meaningful if it reports any prompt/completion/total
    tokens, any cached input tokens, or any cost. Everything else is dropped.
    """
    if not usage_data:
        return False
    if (
        usage_data.get("prompt_tokens")
        or usage_data.get("completion_tokens")
        or usage_data.get("total_tokens")
    ):
        return True
    details = usage_data.get("prompt_tokens_details") or {}
    if details.get("cached_tokens"):
        return True
    # A pure reasoning (or image) step can report 0 visible completion tokens
    # while still doing real work — keep it so the reasoning count isn't lost.
    # The zero-filled "C" gemini junk has reasoning_tokens=0, so it stays rejected.
    completion_details = usage_data.get("completion_tokens_details") or {}
    if completion_details.get("reasoning_tokens") or completion_details.get(
        "image_tokens"
    ):
        return True
    # The broken bare-id "C" gateway reports reasoning at the TOP LEVEL instead of
    # nested under completion_tokens_details — a pure-reasoning chunk is still real.
    if usage_data.get("reasoning_tokens"):
        return True
    # OpenRouter-routed payloads can bill a non-zero cost; preserve those even
    # if (rarely) the token counts are absent, so cost accounting is intact.
    cost = usage_data.get("cost")
    if cost:
        return True
    cost_details = usage_data.get("cost_details") or {}
    if cost_details.get("upstream_inference_cost"):
        return True
    return False


def normalize_provider_usage(usage_data: dict | None) -> dict | None:
    """Reshape a broken "C" gateway usage payload into the canonical OpenAI shape.

    The bare-id "C" gateway intermittently reports reasoning tokens at the TOP
    LEVEL (``usage["reasoning_tokens"]``) and EXCLUDES them from
    ``completion_tokens`` — so ``total == prompt + completion + reasoning`` while
    ``prompt + completion != total``. Every canonical provider instead folds
    reasoning INTO ``completion_tokens`` and reports the breakdown under
    ``completion_tokens_details.reasoning_tokens``. Left unnormalized, our OUTPUT
    counts are short by the reasoning amount on these calls — every surface that
    reads ``completion_tokens`` (process_token_usage, rebuild_token_analytics, the
    in-chat pill, embedded cost) undercounts.

    Returns a corrected COPY shaped exactly like a canonical payload: reasoning is
    folded into ``completion_tokens`` and mirrored into the nested
    ``completion_tokens_details.reasoning_tokens`` slot where readers expect it; the
    misplaced top-level key is dropped. The input dict is left untouched.

    Guarded so it NEVER touches a correct payload:
      * only fires when a positive TOP-LEVEL ``reasoning_tokens`` is present,
      * skips when reasoning is already nested (canonical) — avoids double counting,
      * requires the arithmetic to PROVE reasoning is excluded
        (``total == prompt + completion + reasoning`` AND ``total != prompt + completion``).
    Idempotent: after folding, ``total == prompt + completion`` and the top-level key
    is gone, so a second pass is a no-op. This is exactly why gpt-5.5 "C" (canonical:
    reasoning already inside completion, no top-level key) is never modified.
    """
    if not isinstance(usage_data, dict):
        return usage_data
    top_reasoning = int(usage_data.get("reasoning_tokens") or 0)
    if top_reasoning <= 0:
        return usage_data
    completion_details = usage_data.get("completion_tokens_details") or {}
    if int(completion_details.get("reasoning_tokens") or 0) > 0:
        # Reasoning already in the canonical slot — completion is assumed to
        # include it; folding the top-level value too would double count.
        return usage_data
    prompt = int(usage_data.get("prompt_tokens") or 0)
    completion = int(usage_data.get("completion_tokens") or 0)
    total = int(usage_data.get("total_tokens") or 0)
    # Only fold when the provider total proves reasoning sits OUTSIDE completion.
    if not (
        total
        and total == prompt + completion + top_reasoning
        and total != prompt + completion
    ):
        return usage_data
    fixed = dict(usage_data)
    fixed["completion_tokens"] = completion + top_reasoning
    details = dict(completion_details)
    details["reasoning_tokens"] = top_reasoning
    fixed["completion_tokens_details"] = details
    fixed.pop("reasoning_tokens", None)
    return fixed


async def process_token_usage(
    model_id: str,
    usage_data: dict,
    chat_id: str = None,
    user_id: str = None,
    source_chat_id: str = None,
    message_id: str = None,
    parent_message_id: str = None,
    source_type: str = None,
):
    """
    Process token usage data and update all tracking tables.

    Updates:
    1. Token group usage (existing rate limiting feature)
    2. Conversation token usage (new - for per-chat tracking)
    3. Daily token usage (new - for heatmaps)
    4. Model token usage (new - for model breakdowns)

    Args:
        model_id: The model being used
        usage_data: Dict containing prompt_tokens, completion_tokens, etc.
        chat_id: Optional attributed chat ID for conversation tracking
        user_id: Optional user ID for user-level analytics
        source_chat_id: Chat that actually ran the model call (hidden subagent chat for subagents)
        message_id: Assistant message receiving this usage payload
        parent_message_id: Parent assistant message for subagent runs
        source_type: chat, subagent, task, proxy, etc.
    """
    log.info(
        f"📊 [process_token_usage] Called with model={model_id}, chat_id={chat_id}, user_id={user_id}"
    )
    log.info(f"📊 [process_token_usage] usage_data={usage_data}")

    if not usage_data:
        log.info(f"📊 [process_token_usage] No usage_data, returning early")
        return

    # Drop fully-empty usage payloads (no tokens, no cost) BEFORE touching any
    # tracking surface. Providers emit these zero-filled `usage` objects on
    # intermediate streaming chunks; recording them clobbers the per-chat
    # "last input" counter to 0 and floods the event table with junk rows.
    # See usage_has_data() for the full rationale.
    if not usage_has_data(usage_data):
        log.debug(
            f"📊 [process_token_usage] Skipping empty usage payload for model={model_id}"
        )
        return

    # Reshape the broken bare-id "C" gateway payload — which reports reasoning at
    # the top level and EXCLUDES it from completion_tokens — into canonical shape,
    # so OUT (completion_tokens) includes reasoning on every analytics surface and
    # the stored raw_usage is internally consistent. No-op for canonical payloads.
    usage_data = normalize_provider_usage(usage_data)

    # Extract token counts with safe defaults
    prompt_tokens = usage_data.get("prompt_tokens", 0)
    completion_tokens = usage_data.get("completion_tokens", 0)

    # Extract provider prompt-cache read tokens.
    # `completion_tokens` already includes reasoning tokens for OpenAI/OpenRouter-style
    # usage payloads; use provider `total_tokens` when available so totals match billing.
    prompt_tokens_details = usage_data.get("prompt_tokens_details", {}) or {}
    cache_read_tokens = int(prompt_tokens_details.get("cached_tokens", 0) or 0)

    # IN = prompt_tokens for this request/context
    # OUT = completion_tokens for this request (reasoning included by provider if billed)
    # TOTAL = provider-reported total_tokens, falling back to IN + OUT
    token_in = int(prompt_tokens or 0)
    token_out = int(completion_tokens or 0)
    token_total = int(usage_data.get("total_tokens", token_in + token_out) or 0)

    # Authoritative per-call USD cost for OpenRouter-routed payloads (None for
    # rate-card rows, which are priced at read time). Computed once here so the
    # analytics read path never has to parse raw_usage JSON.
    from open_webui.utils.pricing import embedded_cost as _embedded_cost

    row_embedded_cost = _embedded_cost(usage_data)

    log.info(
        f"📊 [process_token_usage] Calculated: in={token_in}, out={token_out}, total={token_total}"
    )

    # 1. Update existing group-based token tracking (for rate limiting)
    await token_groups.update_token_usage(model_id, token_in, token_out, token_total)

    # 2-4. Update analytics tables for "Wrapped" feature
    try:
        from open_webui.models.analytics import Analytics

        event_source_chat_id = source_chat_id or chat_id
        event_source_type = source_type or (
            "subagent"
            if event_source_chat_id and chat_id and event_source_chat_id != chat_id
            else "chat"
        )

        # 2. Store immutable per-request event first. This is the source of
        # truth for future rebuilds and precise subagent analytics.
        if chat_id and user_id:
            await Analytics.record_token_usage_event(
                user_id=user_id,
                source_chat_id=event_source_chat_id,
                attributed_chat_id=chat_id,
                message_id=message_id,
                parent_message_id=parent_message_id,
                model_id=model_id,
                prompt_tokens=token_in,
                completion_tokens=token_out,
                total_tokens=token_total,
                cache_read_tokens=cache_read_tokens,
                source_type=event_source_type,
                raw_usage=usage_data,
                embedded_cost=row_embedded_cost,
            )

        # 3. Update conversation token usage (per-visible-chat tracking)
        if chat_id and user_id:
            log.info(
                f"📊 [process_token_usage] Updating conversation token usage for chat={chat_id}, user={user_id}"
            )
            result = await Analytics.update_conversation_token_usage(
                chat_id=chat_id,
                user_id=user_id,
                model_id=model_id,
                token_in=token_in,
                token_out=token_out,
                token_total=token_total,
                cache_read_tokens=cache_read_tokens,
                # Subagent calls roll their tokens into the parent chat's totals
                # but must not advance the parent pill's "Latest Input" snapshot —
                # that should reflect the visible chat's own last turn.
                is_own_turn=(event_source_type == "chat"),
            )
            log.info(f"📊 [process_token_usage] Conversation update result: {result}")

            # Push the refreshed authoritative totals to the sessions viewing this
            # chat so the in-chat token pill stays live without an HTTP poll. This
            # is the ONLY live path that reflects subagent token roll-up on the
            # parent pill (subagent usage events never reach the parent's own
            # op=usage delta), and it carries the exact cumulative numbers a reload
            # would read — so it also corrects the optimistic per-round delta path,
            # which undercounts multi-round agentic turns.
            if result is not None:
                await push_chat_token_stats(user_id, chat_id, result)
        else:
            log.info(
                f"📊 [process_token_usage] Skipping conversation update - chat_id={chat_id}, user_id={user_id}"
            )

        # 4. Update daily token usage (for heatmaps)
        if user_id:
            log.info(
                f"📊 [process_token_usage] Updating daily token usage for user={user_id}"
            )
            await Analytics.update_daily_token_usage(
                user_id=user_id,
                token_in=token_in,
                token_out=token_out,
                token_total=token_total,
                chat_id=chat_id,
                cache_read_tokens=cache_read_tokens,
            )

        # 5. Update model token usage (for model breakdowns)
        log.info(
            f"📊 [process_token_usage] Updating model token usage for model={model_id}"
        )
        await Analytics.update_model_token_usage(
            user_id=user_id,
            model_id=model_id,
            token_in=token_in,
            token_out=token_out,
            token_total=token_total,
            cache_read_tokens=cache_read_tokens,
        )

        log.info(
            f"📊 [process_token_usage] SUCCESS: model={model_id}, chat={chat_id}, user={user_id}, tokens={token_total}"
        )
    except Exception as e:
        log.error(
            f"📊 [process_token_usage] ERROR updating analytics: {e}", exc_info=True
        )

    # Push refreshed token-usage groups so the frontend doesn't have to poll
    # (Wire Contract #6). Trailing-coalesced: a burst of per-round usage events
    # collapses to at most one push per TOKEN_USAGE_PUSH_MIN_INTERVAL, and the
    # delayed push reads the totals AFTER the burst, so the bar always converges
    # on the final numbers. Scheduled even without a user_id — the counters are
    # instance-global, so any usage event moves every viewer's bar.
    schedule_token_usage_push()

    # Subscription-provider bars move on the provider's clock, not ours — a
    # completed generation is the moment their reported usage actually
    # advances, so kick a debounced refresh of the usage endpoints now instead
    # of waiting out the poller interval. The delay gives the provider a beat
    # to register the request; no-op when no connection is flagged.
    try:
        from open_webui.utils.subscription_usage import maybe_schedule_refresh

        maybe_schedule_refresh(delay=2.0)
    except Exception:
        log.exception("subscription usage refresh scheduling failed")


# Live token-usage bar wire (Wire Contract #6). Token groups are INSTANCE-global
# (the token_usage table has no user column) and GET /api/usage/groups is
# get_verified_user, so the push goes to every authenticated session — the old
# primary-session-only emit meant a phone chatting while another device held
# primary NEVER saw the bar move (the BroadcastChannel fan-out that was supposed
# to cover non-primary receivers cannot cross devices). Data-frugal for slow
# cellular: trailing-coalesced (≤1 push per interval, converging on post-burst
# totals), carries ONLY groups whose payload changed since the last push, plus
# the full name list so client-side merges can drop deleted groups, and skips
# the emit entirely when nothing changed (including the no-groups-configured
# case).
TOKEN_USAGE_PUSH_MIN_INTERVAL = 2.0
_token_usage_push_last_at = 0.0
_token_usage_push_task: Optional[asyncio.Task] = None
_token_usage_push_last_sent: dict = {}


def schedule_token_usage_push():
    global _token_usage_push_task
    if _token_usage_push_task is not None and not _token_usage_push_task.done():
        # A push is already pending; it reads totals after the burst settles,
        # so this event's contribution is covered.
        return
    try:
        _token_usage_push_task = asyncio.get_running_loop().create_task(
            _token_usage_push_soon()
        )
    except RuntimeError:
        # No running loop (sync test context) — the next mount-time GET covers it.
        pass


async def _token_usage_push_soon():
    global _token_usage_push_last_at
    try:
        wait = TOKEN_USAGE_PUSH_MIN_INTERVAL - (
            time.monotonic() - _token_usage_push_last_at
        )
        if wait > 0:
            await asyncio.sleep(wait)
        _token_usage_push_last_at = time.monotonic()
        groups = await token_groups.get_token_groups()
        changed = {
            name: group
            for name, group in groups.items()
            if _token_usage_push_last_sent.get(name) != group
        }
        removed = [name for name in _token_usage_push_last_sent if name not in groups]
        if not changed and not removed:
            return
        _token_usage_push_last_sent.clear()
        _token_usage_push_last_sent.update(groups)
        await push_token_usage_update(changed, list(groups.keys()))
    except Exception:
        log.exception("📊 [process_token_usage] token-usage:update push failed")


async def push_token_usage_update(groups: dict, names: Optional[list] = None):
    session_ids = list(SESSION_POOL.keys())
    if not session_ids:
        return
    payload = {
        "chat_id": None,
        "message_id": None,
        "data": {
            "type": "token-usage:update",
            # Unique id so a tab that receives both the direct emit and a
            # sibling tab's BroadcastChannel rebroadcast dedups the copy.
            "event_id": f"token-usage:{int(time.time() * 1000)}",
            "data": {
                "groups": groups,
                # Full catalog of current group names: lets the client merge
                # `groups` partially AND drop groups deleted server-side.
                "names": names if names is not None else list(groups.keys()),
            },
        },
    }
    await asyncio.gather(
        *[sio.emit("events", payload, to=sid) for sid in session_ids],
        return_exceptions=True,
    )


# Subscription-provider usage push. Same instance-global, every-session
# delivery as push_token_usage_update (the flagged connections are admin-level
# config, so every viewer's bar reads the same snapshot). Callers only invoke
# this on actual change (refresh_subscription_usage diffs first), and the
# snapshot is a handful of windows per connection, so the wire carries the
# whole authoritative map — full replace, no merge protocol.
async def push_subscription_usage_update(subscriptions: dict):
    session_ids = list(SESSION_POOL.keys())
    if not session_ids:
        return
    payload = {
        "chat_id": None,
        "message_id": None,
        "data": {
            "type": "subscription-usage:update",
            "event_id": f"subscription-usage:{int(time.time() * 1000)}",
            "data": {"subscriptions": subscriptions},
        },
    }
    await asyncio.gather(
        *[sio.emit("events", payload, to=sid) for sid in session_ids],
        return_exceptions=True,
    )


# Per-chat throttle for the live token-usage pill push (chat:token-usage). The
# push carries CUMULATIVE authoritative totals, so dropping an intermediate
# update is always safe: the next push — or the post-run reconcile fetch the
# frontend fires on `done` — carries the latest totals. This bounds the added
# socket volume under heavy multi-round / high-subagent-fanout runs (important
# for a phone on a limited data plan) while keeping the pill effectively live.
CHAT_TOKEN_PUSH_MIN_INTERVAL = 0.5
_chat_token_push_last: dict[str, float] = {}


def _should_push_chat_token_stats(chat_id: str) -> bool:
    if not chat_id:
        return False
    now = time.monotonic()
    last = _chat_token_push_last.get(chat_id)
    if last is not None and (now - last) < CHAT_TOKEN_PUSH_MIN_INTERVAL:
        return False
    _chat_token_push_last[chat_id] = now
    # Opportunistic prune so a long-lived worker doesn't keep one entry per chat
    # forever. Cheap: runs only when the map grows past a generous cap.
    if len(_chat_token_push_last) > 1024:
        cutoff = now - 300.0
        for cid in [c for c, t in _chat_token_push_last.items() if t < cutoff]:
            _chat_token_push_last.pop(cid, None)
    return True


async def _compute_chat_cost(chat_id: str) -> float:
    """Authoritative per-chat USD cost — the SAME value a reload / HTTP reconcile
    computes (``Analytics.get_chat_cost`` folds embedded + rate-card spend and
    subagent roll-up via ``attributed_chat_id``).

    Runs the sync aggregation off the event loop through ``_AsyncAnalyticsProxy``
    (``attributed_chat_id`` is indexed, so it is a cheap grouped read). Factored
    out as a module-level coroutine so the live-push path — and its tests — can
    stub it. NEVER raises: a pricing/DB hiccup must degrade to $0 (segment hidden)
    without breaking the token push it rides alongside.
    """
    if not chat_id or str(chat_id).startswith("local:"):
        return 0.0
    try:
        from open_webui.models.analytics import Analytics

        cost = await Analytics.get_chat_cost(chat_id)
        return float(cost or 0.0)
    except Exception as e:
        log.debug(f"📊 [_compute_chat_cost] failed for chat {chat_id}: {e}")
        return 0.0


async def push_chat_token_stats(user_id: str, chat_id: str, stats) -> None:
    """Push authoritative per-chat token totals + cost to the sessions viewing ``chat_id``.

    ``stats`` is the ConversationTokenUsageResponse returned by
    ``update_conversation_token_usage`` — the SAME cumulative numbers a chat
    reload reads from the DB. Emitting it as a tiny stream-scoped ``chat:token-usage``
    event keeps the in-chat token pill live without an HTTP poll, and because the
    totals are attributed to the visible/parent ``chat_id`` it also surfaces
    subagent token roll-up live (the subagent's own usage events never reach the
    parent pill — see process_token_usage attribution).

    Cost rides the SAME push: it is recomputed at read time (rate card) via
    ``_compute_chat_cost`` after the throttle gate passes, so the $ segment updates
    live and appears on a brand-new chat's first turn — instead of only after a
    full reload. Previously cost was omitted here and reached the pill solely via
    the debounced HTTP reconcile, which the frontend ``staleResponse`` guard could
    drop whenever live token totals were transiently ahead of the DB read, pinning
    the $ segment at its stale/zero value until the user reloaded.
    """
    if not (user_id and chat_id and stats is not None):
        return
    if str(chat_id).startswith("local:"):
        return
    if not _should_push_chat_token_stats(chat_id):
        return
    try:
        # Only priced AFTER the throttle gate passes, so throttled-out pushes add
        # no query load. Same authoritative number a reload computes.
        cost = await _compute_chat_cost(chat_id)
        payload = {
            "chat_id": chat_id,
            "message_id": None,
            "data": {
                "type": "chat:token-usage",
                "data": {
                    "chat_id": getattr(stats, "chat_id", chat_id),
                    "total_input_tokens": int(
                        getattr(stats, "total_input_tokens", 0) or 0
                    ),
                    "total_output_tokens": int(
                        getattr(stats, "total_output_tokens", 0) or 0
                    ),
                    "total_tokens": int(getattr(stats, "total_tokens", 0) or 0),
                    "total_cache_read_tokens": int(
                        getattr(stats, "total_cache_read_tokens", 0) or 0
                    ),
                    "last_input_tokens": int(
                        getattr(stats, "last_input_tokens", 0) or 0
                    ),
                    "last_output_tokens": int(
                        getattr(stats, "last_output_tokens", 0) or 0
                    ),
                    "last_cache_read_tokens": int(
                        getattr(stats, "last_cache_read_tokens", 0) or 0
                    ),
                    "message_count": int(getattr(stats, "message_count", 0) or 0),
                    "cost": float(cost or 0.0),
                },
            },
        }
        await emit_to_primary(user_id, payload)
    except Exception as e:
        log.debug(f"📊 [push_chat_token_stats] emit failed for chat {chat_id}: {e}")


@sio.event
async def connect(sid, environ, auth):
    user = None
    if auth and "token" in auth:
        data = decode_token(auth["token"])

        if data is not None and "id" in data:
            user = await Users.get_user_by_id(data["id"])

        if user:
            _register_user_session(user, sid)


@sio.on("user-join")
async def user_join(sid, data):

    auth = data["auth"] if "auth" in data else None
    if not auth or "token" not in auth:
        return

    data = decode_token(auth["token"])
    if data is None or "id" not in data:
        return

    user = await Users.get_user_by_id(data["id"])
    if not user:
        return

    primary_sid = _register_user_session(user, sid)

    # Join all the channels
    channels = await Channels.get_channels_by_user_id(user.id)
    log.debug(f"{channels=}")
    for channel in channels:
        await sio.enter_room(sid, f"channel:{channel.id}")
    return {"id": user.id, "name": user.name, "primary_session_id": primary_sid}


@sio.on("stream:subscribe")
async def stream_subscribe(sid, data):
    user = SESSION_POOL.get(sid)
    if not user:
        return {"status": False, "error": "Session not found"}

    chat_id = (data or {}).get("chat_id")
    if not chat_id:
        return {"status": False, "error": "Missing chat_id"}

    if not await Chats.user_owns_chat(chat_id, user.get("id")):
        return {"status": False, "error": "Chat not found"}

    await sio.enter_room(sid, stream_room(chat_id))
    capabilities = (data or {}).get("capabilities") or {}
    if not isinstance(capabilities, dict):
        capabilities = {}
    STREAM_SUBSCRIPTION_STATE.setdefault(chat_id, {})[sid] = {
        "visible": bool((data or {}).get("visible", True)),
        "capabilities": capabilities,
        "updated_at": time.time(),
    }
    return {
        "status": True,
        "streams": get_active_streams_for_chat(chat_id),
        "runtime": {
            "ack_interval_ms": STREAM_CLIENT_ACK_INTERVAL_MS,
            "lag_max_versions": STREAM_CLIENT_LAG_MAX_VERSIONS,
        },
    }


@sio.on("stream:unsubscribe")
async def stream_unsubscribe(sid, data):
    chat_id = (data or {}).get("chat_id")
    if not chat_id:
        return {"status": False, "error": "Missing chat_id"}
    try:
        await sio.leave_room(sid, stream_room(chat_id))
    except Exception:
        pass
    try:
        subscribers = STREAM_SUBSCRIPTION_STATE.get(chat_id) or {}
        subscribers.pop(sid, None)
        if not subscribers:
            STREAM_SUBSCRIPTION_STATE.pop(chat_id, None)
    except Exception:
        pass
    return {"status": True}


@sio.on("stream:visibility")
async def stream_visibility(sid, data):
    user = SESSION_POOL.get(sid)
    if not user:
        return {"status": False, "error": "Session not found"}
    chat_id = (data or {}).get("chat_id")
    if not chat_id:
        return {"status": False, "error": "Missing chat_id"}
    if not await Chats.user_owns_chat(chat_id, user.get("id")):
        return {"status": False, "error": "Chat not found"}
    subscribers = STREAM_SUBSCRIPTION_STATE.setdefault(chat_id, {})
    state = subscribers.setdefault(sid, {"capabilities": {}, "visible": True})
    state["visible"] = bool((data or {}).get("visible", True))
    state["updated_at"] = time.time()
    return {"status": True}


@sio.on("stream:ack")
async def stream_ack(sid, data):
    user = SESSION_POOL.get(sid)
    if not user:
        return {"status": False, "error": "Session not found"}
    chat_id = (data or {}).get("chat_id")
    message_id = (data or {}).get("message_id")
    if not chat_id or not message_id:
        return {"status": False, "error": "Missing chat_id or message_id"}
    try:
        version = int((data or {}).get("version") or 0)
    except Exception:
        version = 0
    STREAM_CLIENT_ACKS.setdefault(sid, {})[str(message_id)] = max(0, version)
    STREAM_SYNC_REQUIRED_SENT.discard((sid, str(message_id)))
    subscribers = STREAM_SUBSCRIPTION_STATE.setdefault(chat_id, {})
    state = subscribers.setdefault(sid, {"capabilities": {}, "visible": True})
    state["updated_at"] = time.time()
    return {"status": True}


@sio.on("join-channels")
async def join_channel(sid, data):
    auth = data["auth"] if "auth" in data else None
    if not auth or "token" not in auth:
        return

    data = decode_token(auth["token"])
    if data is None or "id" not in data:
        return

    user = await Users.get_user_by_id(data["id"])
    if not user:
        return

    # Join all the channels
    channels = await Channels.get_channels_by_user_id(user.id)
    log.debug(f"{channels=}")
    for channel in channels:
        await sio.enter_room(sid, f"channel:{channel.id}")


@sio.on("join-note")
async def join_note(sid, data):
    auth = data["auth"] if "auth" in data else None
    if not auth or "token" not in auth:
        return

    token_data = decode_token(auth["token"])
    if token_data is None or "id" not in token_data:
        return

    user = await Users.get_user_by_id(token_data["id"])
    if not user:
        return

    note = await Notes.get_note_by_id(data["note_id"])
    if not note:
        log.error(f"Note {data['note_id']} not found for user {user.id}")
        return

    if (
        user.role != "admin"
        and user.id != note.user_id
        and not has_access(user.id, type="read", access_control=note.access_control)
    ):
        log.error(f"User {user.id} does not have access to note {data['note_id']}")
        return

    log.debug(f"Joining note {note.id} for user {user.id}")
    await sio.enter_room(sid, f"note:{note.id}")


@sio.on("events:channel")
async def channel_events(sid, data):
    room = f"channel:{data['channel_id']}"
    participants = sio.manager.get_participants(
        namespace="/",
        room=room,
    )

    sids = [sid for sid, _ in participants]
    if sid not in sids:
        return

    event_data = data["data"]
    event_type = event_data["type"]

    if event_type == "typing":
        await sio.emit(
            "events:channel",
            {
                "channel_id": data["channel_id"],
                "message_id": data.get("message_id", None),
                "data": event_data,
                "user": UserNameResponse(**SESSION_POOL[sid]).model_dump(),
            },
            room=room,
        )


@sio.on("ydoc:document:join")
async def ydoc_document_join(sid, data):
    """Handle user joining a document"""
    user = SESSION_POOL.get(sid)

    try:
        document_id = data["document_id"]

        if document_id.startswith("note:"):
            note_id = document_id.split(":")[1]
            note = await Notes.get_note_by_id(note_id)
            if not note:
                log.error(f"Note {note_id} not found")
                return

            if (
                user.get("role") != "admin"
                and user.get("id") != note.user_id
                and not has_access(
                    user.get("id"), type="read", access_control=note.access_control
                )
            ):
                log.error(
                    f"User {user.get('id')} does not have access to note {note_id}"
                )
                return

        user_id = data.get("user_id", sid)
        user_name = data.get("user_name", "Anonymous")
        user_color = data.get("user_color", "#000000")

        log.info(f"User {user_id} joining document {document_id}")
        await YDOC_MANAGER.add_user(document_id=document_id, user_id=sid)

        # Join Socket.IO room
        await sio.enter_room(sid, f"doc_{document_id}")

        active_session_ids = get_session_ids_from_room(f"doc_{document_id}")

        # Get the Yjs document state
        ydoc = Y.Doc()
        updates = await YDOC_MANAGER.get_updates(document_id)
        for update in updates:
            ydoc.apply_update(bytes(update))

        # Encode the entire document state as an update
        state_update = ydoc.get_update()
        await sio.emit(
            "ydoc:document:state",
            {
                "document_id": document_id,
                "state": list(state_update),  # Convert bytes to list for JSON
                "sessions": active_session_ids,
            },
            room=sid,
        )

        # Notify other users about the new user
        await sio.emit(
            "ydoc:user:joined",
            {
                "document_id": document_id,
                "user_id": user_id,
                "user_name": user_name,
                "user_color": user_color,
            },
            room=f"doc_{document_id}",
            skip_sid=sid,
        )

        log.info(f"User {user_id} successfully joined document {document_id}")

    except Exception as e:
        log.error(f"Error in yjs_document_join: {e}")
        await sio.emit("error", {"message": "Failed to join document"}, room=sid)


async def document_save_handler(document_id, data, user):
    if document_id.startswith("note:"):
        note_id = document_id.split(":")[1]
        note = await Notes.get_note_by_id(note_id)
        if not note:
            log.error(f"Note {note_id} not found")
            return

        if (
            user.get("role") != "admin"
            and user.get("id") != note.user_id
            and not has_access(
                user.get("id"), type="read", access_control=note.access_control
            )
        ):
            log.error(f"User {user.get('id')} does not have access to note {note_id}")
            return

        await Notes.update_note_by_id(note_id, NoteUpdateForm(data=data))


@sio.on("ydoc:document:state")
async def yjs_document_state(sid, data):
    """Send the current state of the Yjs document to the user"""
    try:
        document_id = data["document_id"]
        room = f"doc_{document_id}"

        active_session_ids = get_session_ids_from_room(room)

        if sid not in active_session_ids:
            log.warning(f"Session {sid} not in room {room}. Cannot send state.")
            return

        if not await YDOC_MANAGER.document_exists(document_id):
            log.warning(f"Document {document_id} not found")
            return

        # Get the Yjs document state
        ydoc = Y.Doc()
        updates = await YDOC_MANAGER.get_updates(document_id)
        for update in updates:
            ydoc.apply_update(bytes(update))

        # Encode the entire document state as an update
        state_update = ydoc.get_update()

        await sio.emit(
            "ydoc:document:state",
            {
                "document_id": document_id,
                "state": list(state_update),  # Convert bytes to list for JSON
                "sessions": active_session_ids,
            },
            room=sid,
        )
    except Exception as e:
        log.error(f"Error in yjs_document_state: {e}")


@sio.on("ydoc:document:update")
async def yjs_document_update(sid, data):
    """Handle Yjs document updates"""
    try:
        document_id = data["document_id"]

        try:
            await stop_item_tasks(REDIS, document_id)
        except:
            pass

        user_id = data.get("user_id", sid)

        update = data["update"]  # List of bytes from frontend

        await YDOC_MANAGER.append_to_updates(
            document_id=document_id,
            update=update,  # Convert list of bytes to bytes
        )

        # Broadcast update to all other users in the document
        await sio.emit(
            "ydoc:document:update",
            {
                "document_id": document_id,
                "user_id": user_id,
                "update": update,
                "socket_id": sid,  # Add socket_id to match frontend filtering
            },
            room=f"doc_{document_id}",
            skip_sid=sid,
        )

        async def debounced_save():
            await asyncio.sleep(0.5)
            await document_save_handler(
                document_id, data.get("data", {}), SESSION_POOL.get(sid)
            )

        if data.get("data"):
            await create_task(REDIS, debounced_save(), document_id)

    except Exception as e:
        log.error(f"Error in yjs_document_update: {e}")


@sio.on("ydoc:document:leave")
async def yjs_document_leave(sid, data):
    """Handle user leaving a document"""
    try:
        document_id = data["document_id"]
        user_id = data.get("user_id", sid)

        log.info(f"User {user_id} leaving document {document_id}")

        # Remove user from the document
        await YDOC_MANAGER.remove_user(document_id=document_id, user_id=sid)

        # Leave Socket.IO room
        await sio.leave_room(sid, f"doc_{document_id}")

        # Notify other users
        await sio.emit(
            "ydoc:user:left",
            {"document_id": document_id, "user_id": user_id},
            room=f"doc_{document_id}",
        )

        if (
            await YDOC_MANAGER.document_exists(document_id)
            and len(await YDOC_MANAGER.get_users(document_id)) == 0
        ):
            log.info(f"Cleaning up document {document_id} as no users are left")
            await YDOC_MANAGER.clear_document(document_id)

    except Exception as e:
        log.error(f"Error in yjs_document_leave: {e}")


@sio.on("ydoc:awareness:update")
async def yjs_awareness_update(sid, data):
    """Handle awareness updates (cursors, selections, etc.)"""
    try:
        document_id = data["document_id"]
        user_id = data.get("user_id", sid)
        update = data["update"]

        # Broadcast awareness update to all other users in the document
        await sio.emit(
            "ydoc:awareness:update",
            {"document_id": document_id, "user_id": user_id, "update": update},
            room=f"doc_{document_id}",
            skip_sid=sid,
        )

    except Exception as e:
        log.error(f"Error in yjs_awareness_update: {e}")


@sio.event
async def disconnect(sid):
    try:
        for chat_id, subscribers in list(STREAM_SUBSCRIPTION_STATE.items()):
            subscribers.pop(sid, None)
            if not subscribers:
                STREAM_SUBSCRIPTION_STATE.pop(chat_id, None)
        STREAM_CLIENT_ACKS.pop(sid, None)
        for key in list(STREAM_SYNC_REQUIRED_SENT):
            if key[0] == sid:
                STREAM_SYNC_REQUIRED_SENT.discard(key)
    except Exception:
        pass

    if sid in SESSION_POOL:
        user = SESSION_POOL[sid]
        del SESSION_POOL[sid]

        user_id = user["id"]
        USER_POOL[user_id] = [_sid for _sid in USER_POOL[user_id] if _sid != sid]

        if len(USER_POOL[user_id]) == 0:
            del USER_POOL[user_id]
            cad = getattr(PRIMARY_SESSION_PER_USER, "compare_and_delete", None)
            if cad is not None:
                cad(user_id, sid)
            elif PRIMARY_SESSION_PER_USER.get(user_id) == sid:
                del PRIMARY_SESSION_PER_USER[user_id]
        elif PRIMARY_SESSION_PER_USER.get(user_id) == sid:
            # Primary disappeared but other sessions of this user remain.
            # Immediately elect a replacement so server-side primary-only
            # emits (e.g. token-usage:update) don't fall back to fan-out and
            # defeat the dedup design. Pick the first remaining sid for
            # determinism. Use CAS so a concurrent connect on another worker
            # that already promoted itself isn't overwritten.
            replacement = USER_POOL[user_id][0]
            cas = getattr(PRIMARY_SESSION_PER_USER, "compare_and_swap", None)
            if cas is not None:
                cas(user_id, sid, replacement)
            else:
                PRIMARY_SESSION_PER_USER[user_id] = replacement

        await YDOC_MANAGER.remove_user_from_all_documents(sid)
    else:
        pass
        # print(f"Unknown session ID {sid} disconnected")


_GENERATION_SCOPED_EVENT_TYPES = {
    "chat:completion",
    "chat:done",
    "chat:message:error",
    "chat:tasks:cancel",
}


def _stamp_generation_identity(event_data, request_info):
    """Attach request ownership to events that can settle a message lifecycle."""
    if not isinstance(event_data, dict):
        return event_data
    if event_data.get("type") not in _GENERATION_SCOPED_EVENT_TYPES:
        return event_data
    generation_id = request_info.get("generation_id")
    turn_id = request_info.get("turn_id")
    if not generation_id and not turn_id:
        return event_data
    stamped = dict(event_data)
    inner = dict(stamped.get("data")) if isinstance(stamped.get("data"), dict) else {}
    if generation_id:
        inner.setdefault("generation_id", generation_id)
    if turn_id:
        inner.setdefault("turn_id", turn_id)
    stamped["data"] = inner
    return stamped


def get_event_emitter(request_info, update_db=True):
    async def __event_emitter__(event_data):
        event_data = _stamp_generation_identity(event_data, request_info)
        user_id = request_info["user_id"]

        session_ids = list(
            set(
                USER_POOL.get(user_id, [])
                + (
                    [request_info.get("session_id")]
                    if request_info.get("session_id")
                    else []
                )
            )
        )

        chat_id = request_info.get("chat_id", None)
        message_id = request_info.get("message_id", None)

        envelope = {
            "chat_id": chat_id,
            "message_id": message_id,
            "session_id": request_info.get("session_id"),
            "data": event_data,
        }

        if _is_stream_scoped_payload(envelope):
            await emit_to_primary(user_id, envelope)
        else:
            emit_tasks = [
                sio.emit(
                    "events",
                    {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "data": event_data,
                    },
                    to=session_id,
                )
                for session_id in session_ids
            ]

            await asyncio.gather(*emit_tasks)
        if (
            update_db
            and message_id
            and not request_info.get("chat_id", "").startswith("local:")
        ):
            if "type" in event_data and event_data["type"] == "status":
                await Chats.add_message_status_to_chat_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                    event_data.get("data", {}),
                )

            if "type" in event_data and event_data["type"] == "message":
                message = await Chats.get_message_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                )

                if message:
                    content = message.get("content", "")
                    content += event_data.get("data", {}).get("content", "")

                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        request_info["chat_id"],
                        request_info["message_id"],
                        {
                            "content": content,
                        },
                        return_model=False,
                    )

            if "type" in event_data and event_data["type"] == "replace":
                content = event_data.get("data", {}).get("content", "")

                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                    {
                        "content": content,
                    },
                    return_model=False,
                )

            if "type" in event_data and event_data["type"] == "embeds":
                message = await Chats.get_message_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                )

                if message is None:
                    message = {}
                embeds = event_data.get("data", {}).get("embeds", [])
                embeds.extend(message.get("embeds", []))

                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                    {
                        "embeds": embeds,
                    },
                    return_model=False,
                )

            if "type" in event_data and event_data["type"] == "data_viz:override":
                message = await Chats.get_message_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                )

                payload = event_data.get("data", {}) or {}
                key = payload.get("key")
                widget_code = payload.get("widget_code")

                if key and isinstance(widget_code, str) and message is not None:
                    overrides = message.get("dataVizOverrides") or {}
                    if not isinstance(overrides, dict):
                        overrides = {}
                    overrides[key] = widget_code

                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        request_info["chat_id"],
                        request_info["message_id"],
                        {
                            "dataVizOverrides": overrides,
                        },
                        return_model=False,
                    )

            if "type" in event_data and event_data["type"] == "files":
                # Container output imports already persist the descriptor into
                # message.files themselves, then emit this {type:"files"} event; a
                # blind extend re-appended the SAME descriptor (id + content), so
                # the reloaded message carried every generated file twice. Merge
                # with id + content-identity dedup instead (also correct for the
                # multi-emit tool_result_files case: new files accumulate, repeats
                # collapse).
                from open_webui.utils.container_workspace import _merge_files

                message = await Chats.get_message_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                )

                if message is None:
                    message = {}
                incoming = event_data.get("data", {}).get("files", []) or []
                files = _merge_files(message.get("files", []), incoming)

                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                    {
                        "files": files,
                    },
                    return_model=False,
                )

            if event_data.get("type") in ["source", "citation"]:
                data = event_data.get("data", {})
                if data.get("type") == None:
                    message = await Chats.get_message_by_id_and_message_id(
                        request_info["chat_id"],
                        request_info["message_id"],
                    )

                    if message is None:
                        message = {}
                    sources = message.get("sources", [])
                    sources.append(data)

                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        request_info["chat_id"],
                        request_info["message_id"],
                        {
                            "sources": sources,
                        },
                        return_model=False,
                    )

    return __event_emitter__


async def broadcast_sidebar_event(user_id, event_data, skip_sid=None):
    """Fan out a sidebar-affecting event to every active session of `user_id`,
    excluding `skip_sid` (the originating tab's socket.id). Sent on the same
    "events" channel the frontend already listens on; the envelope's chat_id
    is null because these events are not chat-scoped — they update the
    sidebar list, pinned/folder/tag state, etc."""
    event_data = dict(event_data or {})
    event_data.setdefault(
        "event_id",
        f"sidebar:{user_id}:{time.time_ns()}:{random.getrandbits(32):08x}",
    )
    event_data.setdefault("emitted_at", time.time())
    if skip_sid:
        event_data.setdefault("source_session_id", skip_sid)

    session_ids = _unique_session_ids(
        [
            sid
            for sid in USER_POOL.get(user_id, [])
            if sid and sid != skip_sid and sid in SESSION_POOL
        ]
    )

    if not session_ids:
        return

    await asyncio.gather(
        *[
            sio.emit(
                "events",
                {
                    "chat_id": None,
                    "message_id": None,
                    "data": event_data,
                },
                to=sid,
            )
            for sid in session_ids
        ]
    )


async def broadcast_queue_event(user_id, chat_id, event_data, skip_sid=None):
    """Fan out a message-queue event (chat:queue:updated / chat:queue:drained)
    to every active session of ``user_id``. Unlike sidebar events, the envelope
    carries ``chat_id`` so the frontend can route it to the right chat's
    reflector. Used so all tabs (and a later-opened tab) reflect the queue
    state changing under server-driven draining."""
    event_data = dict(event_data or {})
    session_ids = _unique_session_ids(
        [
            sid
            for sid in USER_POOL.get(user_id, [])
            if sid and sid != skip_sid and sid in SESSION_POOL
        ]
    )
    if not session_ids:
        return
    await asyncio.gather(
        *[
            sio.emit(
                "events",
                {
                    "chat_id": chat_id,
                    "message_id": None,
                    "data": event_data,
                },
                to=sid,
            )
            for sid in session_ids
        ]
    )


async def broadcast_video_job_event(user_id, job: dict):
    """Fan out video-ingest progress to every active session of ``user_id``.

    Video jobs are not owned by a chat (one can be started before the chat
    exists) so this deliberately fans out per-user rather than per-chat, and
    carries the whole job document rather than a delta — a tab that just woke
    up applies the same payload as one that watched the whole run. The DB row
    is authoritative; missing one of these events only costs freshness, since
    clients re-read active jobs on mount."""
    session_ids = _unique_session_ids(
        [sid for sid in USER_POOL.get(user_id, []) if sid and sid in SESSION_POOL]
    )
    if not session_ids:
        return
    await asyncio.gather(
        *[
            sio.emit(
                "events",
                {
                    "chat_id": job.get("chat_id"),
                    "message_id": None,
                    "data": {"type": "video:job", "data": job},
                },
                to=sid,
            )
            for sid in session_ids
        ]
    )


def is_primary_session(user_id, sid) -> bool:
    """B8 helper. Defensive fallback: if no primary is recorded for the user,
    treat every session as primary (so v2.1 emission still reaches the client
    before B8 lands)."""
    try:
        primary = PRIMARY_SESSION_PER_USER.get(user_id)
    except Exception:
        primary = None
    if not primary:
        return True
    return primary == sid


async def _touch_stream_state_ttl():
    """Refresh TTL on the stream-state Redis hashes. Called ONCE per stream
    at stream_version_init (not on every delta) — with a 48h default TTL,
    any reasonable stream completes within the window from start. The TTL
    only serves to reap orphans from crashed/killed workers.

    Note: TTL is on the WHOLE hash (one key per RedisDict), not per
    message_id field. That's fine here — these hashes are cleared explicitly
    on chat:done/error/cancel."""
    if REDIS is None:
        return
    for key_suffix in ("stream_version", "tool_results", "stream_state"):
        try:
            await REDIS.expire(
                f"{REDIS_KEY_PREFIX}:{key_suffix}", STREAM_STATE_TTL_SECONDS
            )
        except Exception:
            pass


def _schedule_stream_state_ttl_refresh() -> None:
    """Fire-and-forget TTL refresh from synchronous helpers. Invoked once per
    stream at stream_version_init (including per-subagent inner streams).
    Skipped when no event loop is running (e.g. module import or sync test
    context)."""
    if REDIS is None:
        return
    try:
        asyncio.ensure_future(_touch_stream_state_ttl())
    except RuntimeError:
        # No running event loop — drop silently; the next write under a loop
        # will set the TTL.
        pass
    except Exception:
        pass


# Active stream indexes. In the common single-worker deployment these are
# in-process RAM and are the authoritative source for reload-mid-generation
# snapshots. Redis-backed deployments still use the RedisDict stores above, but
# this code path is intentionally optimized for the single-worker case the app
# runs by default.
STREAM_MESSAGE_TO_CHAT: Dict[str, str] = {}
STREAM_ACTIVE_BY_CHAT: Dict[str, Set[str]] = {}
STREAM_SUBSCRIPTION_STATE: Dict[str, Dict[str, dict]] = {}
STREAM_CLIENT_ACKS: Dict[str, Dict[str, int]] = {}
STREAM_SYNC_REQUIRED_SENT: Set[tuple[str, str]] = set()
# Multi-client sync (G1): debounced periodic "catch up" nudges for stream-room
# subscribers whose tab is document-hidden. Keyed by (sid, message_id) -> pending
# asyncio.Task. While a hidden subscriber keeps getting its live token deltas
# suppressed by the visibility gate, at most one coalesced chat:stream:sync_required
# per STREAM_HIDDEN_CATCHUP_MS is emitted to it (the task clears its own key on
# fire, so the next suppressed delta re-arms it). Cancelled on stream teardown /
# disconnect so a finished stream stops nudging.
_HIDDEN_CATCHUP_TASKS: Dict[tuple[str, str], asyncio.Task] = {}
STREAM_REPLAY_BUFFERS: Dict[str, deque] = {}
STREAM_REPLAY_BUFFER_BYTES: Dict[str, int] = {}
STREAM_FIRST_DELTA_SENT: Set[str] = set()
STREAM_VERSION_LOCAL: Dict[str, int] = {}
STREAM_VERSION_LAST_STORED: Dict[str, int] = {}
# Stream RUN id (epoch): a per-(message_id, generation-run) identity. A retry /
# "Continue Response" reuses the SAME message_id but resets the version space to
# 0 and wipes the replay buffer — without a run id on the wire, a client still
# holding the old run's high version silently drops every delta of the new run
# as "stale" (frozen/empty response), or worse, splices new-run deltas onto
# old-run blocks. The run id makes version-space resets explicit: clients reset
# their mirror when the run advances and ignore events from older runs. Minted
# monotonically (never goes backwards even under clock adjustment) at
# stream_version_init.
STREAM_RUN_LOCAL: Dict[str, int] = {}
STREAM_METRICS: Dict[str, int] = {}
# Full large tool bodies are kept out of the socket hot path. Keyed by
# message_id -> tool_call_id -> original result dict. In single-worker mode this
# gives immediate lazy expansion during/after generation; final DB checkpoints
# persist the same map for normal reloads.
TOOL_RESULT_BODIES: Dict[str, Dict[str, dict]] = {}
TOOL_RESULT_BODY_SIZES: Dict[str, Dict[str, int]] = {}
TOOL_RESULT_BODY_ORDER: OrderedDict[tuple[str, str], int] = OrderedDict()
TOOL_RESULT_BODY_SPILLS: Dict[str, Dict[str, dict]] = {}
TOOL_RESULT_BODY_TOTAL_BYTES = 0
_STREAM_CLEANUP_TASKS: Dict[str, asyncio.Task] = {}
STREAM_DONE_GRACE_SECONDS = 300
STREAM_ROOM_PREFIX = "stream:chat:"
STREAM_SCOPED_TYPES = frozenset(
    {
        "chat:delta",
        "chat:delta:batch",
        "chat:delta:batch2",
        "tool_call:result",
        "chat:subagent:update",
        "browser:frame",
        "chat:stream:sync_required",
        "chat:done",
        "chat:message:error",
        "chat:tasks:cancel",
        # Cross-device prompt sync: co-deliver a newly-submitted user message to
        # other devices in this chat's stream room (see emit_chat_user_message).
        # Stream-scoped so it reaches exactly the same-user sessions viewing this
        # chat that already receive the assistant token stream — never the
        # USER_POOL fanout. Not batchable/replayable (absent from
        # _BATCHABLE_TYPES), so it bypasses the delta batch/ack machinery, and
        # not a live-delta payload, so it is NOT suppressed to hidden tabs.
        "chat:user-message",
        # Live per-chat token-usage pill: authoritative cumulative totals pushed
        # after each conversation_token_usage write (see push_chat_token_stats).
        # Stream-scoped so it lands only on the same-user sessions viewing this
        # chat. Carries the SAME numbers a chat reload reads from the DB, so it
        # keeps the in-chat token pill live (including subagent roll-up) without
        # any HTTP poll. Not batchable/replayable — it is a tiny standalone push.
        "chat:token-usage",
    }
)


def stream_room(chat_id: str) -> str:
    return f"{STREAM_ROOM_PREFIX}{chat_id}"


def stream_metric(name: str, delta: int = 1) -> None:
    if not STREAM_RUNTIME_METRICS:
        return
    try:
        STREAM_METRICS[name] = int(STREAM_METRICS.get(name, 0) or 0) + int(delta)
    except Exception:
        pass


def get_stream_runtime_metrics() -> dict:
    return dict(STREAM_METRICS)


def _payload_type(payload) -> Optional[str]:
    try:
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            return data.get("type")
    except Exception:
        pass
    return None


def _is_stream_scoped_payload(payload) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("chat_id")
        and _payload_type(payload) in STREAM_SCOPED_TYPES
    )


def _cancel_stream_cleanup(message_id: str) -> None:
    task = _STREAM_CLEANUP_TASKS.pop(message_id, None)
    if task and not task.done():
        task.cancel()


def _index_stream(message_id: str, state: dict) -> None:
    chat_id = state.get("chat_id")
    if not chat_id:
        return
    STREAM_MESSAGE_TO_CHAT[message_id] = chat_id
    status = state.get("status", "in_progress")
    bucket = STREAM_ACTIVE_BY_CHAT.setdefault(chat_id, set())
    if status == "in_progress":
        bucket.add(message_id)
    else:
        bucket.discard(message_id)
        if not bucket:
            STREAM_ACTIVE_BY_CHAT.pop(chat_id, None)


def _delete_stream_state_now(message_id: str) -> None:
    clear_tool_result_bodies(message_id)
    _cancel_stream_cleanup(message_id)
    _cancel_hidden_catchup_for_message(message_id)
    chat_id = STREAM_MESSAGE_TO_CHAT.pop(message_id, None)
    if chat_id:
        bucket = STREAM_ACTIVE_BY_CHAT.get(chat_id)
        if bucket:
            bucket.discard(message_id)
            if not bucket:
                STREAM_ACTIVE_BY_CHAT.pop(chat_id, None)
    STREAM_REPLAY_BUFFERS.pop(message_id, None)
    STREAM_REPLAY_BUFFER_BYTES.pop(message_id, None)
    STREAM_FIRST_DELTA_SENT.discard(message_id)
    STREAM_VERSION_LOCAL.pop(message_id, None)
    STREAM_VERSION_LAST_STORED.pop(message_id, None)
    STREAM_RUN_LOCAL.pop(message_id, None)
    if REDIS is not None:
        try:
            asyncio.ensure_future(_delete_redis_stream_replay_keys(message_id))
        except Exception:
            pass
    for ack_map in list(STREAM_CLIENT_ACKS.values()):
        try:
            ack_map.pop(message_id, None)
        except Exception:
            pass
    for key in list(STREAM_SYNC_REQUIRED_SENT):
        if key[1] == message_id:
            STREAM_SYNC_REQUIRED_SENT.discard(key)
    for store in (STREAM_VERSION, TOOL_RESULTS, STREAM_STATE):
        try:
            if message_id in store:
                del store[message_id]
        except Exception:
            pass


def _block_snapshot_signature(block) -> str:
    try:
        return json.dumps(block, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return repr(block)


def _build_content_blocks_snapshot(existing: dict, blocks, dirty_from=None) -> dict:
    incoming = list(blocks or [])
    prev = (
        existing.get("content_blocks_snapshot") if isinstance(existing, dict) else None
    )
    prev_blocks = prev.get("blocks") if isinstance(prev, dict) else None
    prev_sigs = prev.get("signatures") if isinstance(prev, dict) else None
    if not isinstance(prev_blocks, list) or not isinstance(prev_sigs, list):
        prev_blocks = []
        prev_sigs = []

    if dirty_from is None:
        dirty_from = 0
        max_scan = min(len(prev_blocks), len(prev_sigs), len(incoming))
        while dirty_from < max_scan:
            sig = _block_snapshot_signature(incoming[dirty_from])
            if sig != prev_sigs[dirty_from]:
                break
            dirty_from += 1
        if len(incoming) != len(prev_blocks):
            dirty_from = min(dirty_from, len(incoming), len(prev_blocks))
    else:
        try:
            dirty_from = int(dirty_from)
        except Exception:
            dirty_from = 0
        dirty_from = max(
            0, min(dirty_from, len(incoming), len(prev_blocks), len(prev_sigs))
        )

    next_blocks = list(prev_blocks[:dirty_from])
    next_sigs = list(prev_sigs[:dirty_from])
    for block in incoming[dirty_from:]:
        frozen = copy.deepcopy(block)
        next_blocks.append(frozen)
        next_sigs.append(_block_snapshot_signature(frozen))

    return {
        "format": "blocks-v2.1",
        "blocks": next_blocks,
        "signatures": next_sigs,
    }


def _materialize_stream_state(state: dict) -> dict:
    if not isinstance(state, dict):
        return {}
    snapshot = state.pop("content_blocks_snapshot", None)
    if "content_blocks" not in state and isinstance(snapshot, dict):
        blocks = snapshot.get("blocks")
        state["content_blocks"] = blocks if isinstance(blocks, list) else []
    return state


async def _delete_stream_state_later(message_id: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        _delete_stream_state_now(message_id)
    except asyncio.CancelledError:
        return
    except Exception:
        log.exception("stream state cleanup failed")


def stream_version_init(
    message_id,
    *,
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    content_blocks=None,
) -> int:
    _cancel_stream_cleanup(str(message_id))
    STREAM_FIRST_DELTA_SENT.discard(str(message_id))
    STREAM_REPLAY_BUFFERS.pop(str(message_id), None)
    STREAM_REPLAY_BUFFER_BYTES.pop(str(message_id), None)
    STREAM_VERSION_LOCAL[str(message_id)] = 0
    STREAM_VERSION_LAST_STORED[str(message_id)] = 0
    # Mint the run id for this generation run. max() keeps runs strictly
    # monotonic per message even if the wall clock steps backwards, and the
    # double-init at run start (register_stream_start_if_needed + the v2.1
    # emitter wrapper) stays within the same instant — clients treat any
    # newer run as "reset your mirror", so a same-start rotation is harmless
    # (no deltas are emitted between the two inits).
    prev_run = stream_run_get(message_id)
    STREAM_RUN_LOCAL[str(message_id)] = max(prev_run + 1, int(time.time() * 1000))
    if REDIS is not None:
        try:
            asyncio.ensure_future(_delete_redis_stream_replay_keys(str(message_id)))
        except Exception:
            pass
    try:
        STREAM_VERSION[message_id] = 0
    except Exception:
        pass
    state = {
        "chat_id": chat_id,
        "user_id": user_id,
        "session_id": session_id,
        "content_blocks_snapshot": _build_content_blocks_snapshot(
            {}, content_blocks or []
        ),
        "status": "in_progress",
        "run": STREAM_RUN_LOCAL[str(message_id)],
        "updated_at": time.time(),
    }
    try:
        STREAM_STATE[message_id] = state
    except Exception:
        pass
    _index_stream(str(message_id), state)
    _schedule_stream_state_ttl_refresh()
    return 0


def stream_version_incr(message_id) -> int:
    key = str(message_id)
    if key in STREAM_VERSION_LOCAL:
        current = STREAM_VERSION_LOCAL.get(key, 0) or 0
    else:
        try:
            current = STREAM_VERSION.get(message_id, 0) or 0
        except Exception:
            current = 0
    nxt = int(current) + 1
    STREAM_VERSION_LOCAL[key] = nxt
    if (
        nxt - int(STREAM_VERSION_LAST_STORED.get(key, 0) or 0)
        >= STREAM_VERSION_STORE_FLUSH_EVERY
    ):
        stream_version_flush(key)
    return nxt


def stream_version_flush(message_id) -> int:
    key = str(message_id)
    if key in STREAM_VERSION_LOCAL:
        value = int(STREAM_VERSION_LOCAL.get(key, 0) or 0)
    else:
        try:
            value = int(STREAM_VERSION.get(message_id, 0) or 0)
        except Exception:
            value = 0
    try:
        STREAM_VERSION[message_id] = value
        STREAM_VERSION_LAST_STORED[key] = value
        stream_metric("version.flush")
    except Exception:
        pass
    return value


def stream_version_get(message_id) -> int:
    key = str(message_id)
    if key in STREAM_VERSION_LOCAL:
        return int(STREAM_VERSION_LOCAL.get(key, 0) or 0)
    try:
        return int(STREAM_VERSION.get(message_id, 0) or 0)
    except Exception:
        return 0


def stream_run_get(message_id) -> int:
    """Current run id (epoch) for a message's stream, 0 when unknown.

    Prefers the in-process map; falls back to the RAM stream state so a
    grace-period (recently finished) stream still reports the run its terminal
    snapshot was stamped with."""
    key = str(message_id)
    if key in STREAM_RUN_LOCAL:
        return int(STREAM_RUN_LOCAL.get(key, 0) or 0)
    try:
        state = STREAM_STATE.get(message_id, {}) or {}
        if isinstance(state, dict):
            return int(state.get("run") or 0)
    except Exception:
        pass
    return 0


def set_stream_state(message_id, patch: dict) -> None:
    try:
        existing = STREAM_STATE.get(message_id, {}) or {}
        if not isinstance(existing, dict):
            existing = {}
        # Shallow-merge into a NEW dict so we never mutate the stored snapshot
        # in place (a RedisDict get returns a fresh object, but the in-memory
        # store returns the live object). The previous snapshot's values are
        # carried by reference — only keys present in `patch` are replaced, and
        # those replacements are deep-copied below, so no stale value is shared.
        merged = dict(existing)
        if isinstance(patch, dict):
            # Deep-copy ONLY the patch. Callers (e.g. the per-token flush) pass
            # `_strip_tool_results(content_blocks)`, whose text/reasoning blocks
            # are shared by reference with the still-mutating live content_blocks
            # tail. Copying the patch freezes the snapshot at this version and
            # decouples it from subsequent in-place appends. Deep-copying the
            # *accumulated* state too (the old behavior) re-copied the whole
            # growing blocks list every token for nothing — O(N) per token,
            # O(N^2) per stream — since `update` immediately overwrote it.
            content_blocks = patch.get("content_blocks")
            dirty_from = patch.get("content_blocks_dirty_from")
            for key, value in patch.items():
                if key in {"content_blocks", "content_blocks_dirty_from"}:
                    continue
                merged[key] = copy.deepcopy(value)
            if content_blocks is not None:
                merged.pop("content_blocks", None)
                merged["content_blocks_snapshot"] = _build_content_blocks_snapshot(
                    existing, content_blocks, dirty_from=dirty_from
                )
        merged["updated_at"] = time.time()
        STREAM_STATE[message_id] = merged
        _index_stream(str(message_id), merged)
    except Exception:
        pass


def get_stream_state(message_id) -> dict:
    try:
        existing = STREAM_STATE.get(message_id, {}) or {}
        state = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        return _materialize_stream_state(state)
    except Exception:
        return {}


def get_active_streams_for_chat(chat_id: str) -> list[dict]:
    out = []
    for message_id in list(STREAM_ACTIVE_BY_CHAT.get(chat_id, set()) or []):
        state = get_stream_state(message_id)
        if not state or state.get("status") != "in_progress":
            continue
        out.append(
            {
                "message_id": message_id,
                "version": stream_version_get(message_id),
                "run": stream_run_get(message_id),
                "status": state.get("status", "in_progress"),
                "updated_at": state.get("updated_at"),
            }
        )
    out.sort(key=lambda item: item.get("updated_at") or 0)
    return out


def _stream_replay_key(message_id: str) -> str:
    return f"{REDIS_KEY_PREFIX}:stream_replay:{message_id}"


def _stream_replay_size_key(message_id: str) -> str:
    return f"{REDIS_KEY_PREFIX}:stream_replay_size:{message_id}"


def _stream_replay_bytes_key(message_id: str) -> str:
    return f"{REDIS_KEY_PREFIX}:stream_replay_bytes:{message_id}"


async def _delete_redis_stream_replay_keys(message_id: str) -> None:
    if REDIS is None:
        return
    try:
        await REDIS.delete(
            _stream_replay_key(message_id),
            _stream_replay_size_key(message_id),
            _stream_replay_bytes_key(message_id),
        )
    except Exception:
        pass


def _coerce_replay_entry_size(size_value, raw_entry) -> int:
    try:
        return max(0, int(size_value or 0))
    except Exception:
        pass
    try:
        if raw_entry is None:
            return 0
        if isinstance(raw_entry, bytes):
            return len(raw_entry)
        return len(str(raw_entry).encode("utf-8", "replace"))
    except Exception:
        return 0


async def _append_redis_stream_replay_event(
    message_id: str, encoded: str, encoded_size: int
) -> None:
    key = _stream_replay_key(message_id)
    track_bytes = STREAM_REPLAY_BUFFER_MAX_BYTES > 0
    size_key = _stream_replay_size_key(message_id)
    bytes_key = _stream_replay_bytes_key(message_id)

    await REDIS.rpush(key, encoded)
    total_bytes = 0
    if track_bytes:
        await REDIS.rpush(size_key, int(encoded_size))
        total_bytes = int(await REDIS.incrby(bytes_key, int(encoded_size)) or 0)

    length = int(await REDIS.llen(key) or 0)
    trim_count = 0

    if not track_bytes:
        if (
            STREAM_REPLAY_BUFFER_MAX_EVENTS > 0
            and length > STREAM_REPLAY_BUFFER_MAX_EVENTS
        ):
            trim_count = length - STREAM_REPLAY_BUFFER_MAX_EVENTS
            await REDIS.ltrim(key, -STREAM_REPLAY_BUFFER_MAX_EVENTS, -1)
        if STREAM_REPLAY_BUFFER_TTL_SECONDS > 0:
            await REDIS.expire(key, STREAM_REPLAY_BUFFER_TTL_SECONDS)
        if trim_count:
            stream_metric("replay.trim", trim_count)
        return

    while (
        STREAM_REPLAY_BUFFER_MAX_EVENTS > 0 and length > STREAM_REPLAY_BUFFER_MAX_EVENTS
    ) or (
        STREAM_REPLAY_BUFFER_MAX_BYTES > 0
        and total_bytes > STREAM_REPLAY_BUFFER_MAX_BYTES
    ):
        old_raw = await REDIS.lpop(key)
        old_size = await REDIS.lpop(size_key)
        if old_raw is None and old_size is None:
            length = 0
            total_bytes = 0
            break
        total_bytes = max(0, total_bytes - _coerce_replay_entry_size(old_size, old_raw))
        length = max(0, length - 1)
        trim_count += 1

    await REDIS.set(bytes_key, total_bytes)
    if STREAM_REPLAY_BUFFER_TTL_SECONDS > 0:
        ttl = STREAM_REPLAY_BUFFER_TTL_SECONDS
        await REDIS.expire(key, ttl)
        await REDIS.expire(size_key, ttl)
        await REDIS.expire(bytes_key, ttl)
    if trim_count:
        stream_metric("replay.trim", trim_count)


def _stream_event_message_id(payload) -> Optional[str]:
    try:
        data = payload.get("data") if isinstance(payload, dict) else None
        inner = data.get("data") if isinstance(data, dict) else None
        if isinstance(inner, dict):
            return (
                str(inner.get("message_id") or payload.get("message_id") or "") or None
            )
        return str(payload.get("message_id") or "") or None
    except Exception:
        return None


async def append_stream_replay_event(payload) -> None:
    if STREAM_REPLAY_BUFFER_MAX_EVENTS <= 0:
        return
    event_type = _payload_type(payload)
    if event_type not in {"chat:delta", "tool_call:result"}:
        return
    message_id = _stream_event_message_id(payload)
    if not message_id:
        return
    try:
        data = payload.get("data") if isinstance(payload, dict) else {}
        inner = data.get("data") if isinstance(data, dict) else {}
        if not isinstance(inner, dict):
            return
        replay_version = inner.get("version")
        if not isinstance(replay_version, int):
            replay_version = stream_version_get(message_id)
        entry = {
            "type": event_type,
            "data": inner,
            "replay_version": int(replay_version or 0),
            "ts": time.time(),
        }
        encoded = json.dumps(entry, ensure_ascii=False, default=str)
        encoded_size = len(encoded.encode("utf-8", "replace"))
        while True:
            entry["_size"] = encoded_size
            encoded = json.dumps(entry, ensure_ascii=False, default=str)
            next_size = len(encoded.encode("utf-8", "replace"))
            if next_size == encoded_size:
                break
            encoded_size = next_size
        if REDIS is not None:
            await _append_redis_stream_replay_event(message_id, encoded, encoded_size)
        else:
            buf = STREAM_REPLAY_BUFFERS.get(message_id)
            if buf is None:
                buf = deque()
                STREAM_REPLAY_BUFFERS[message_id] = buf
            buf.append(entry)
            STREAM_REPLAY_BUFFER_BYTES[message_id] = (
                int(STREAM_REPLAY_BUFFER_BYTES.get(message_id, 0) or 0) + encoded_size
            )
            while (
                STREAM_REPLAY_BUFFER_MAX_EVENTS > 0
                and len(buf) > STREAM_REPLAY_BUFFER_MAX_EVENTS
            ):
                old = buf.popleft()
                STREAM_REPLAY_BUFFER_BYTES[message_id] = max(
                    0,
                    int(STREAM_REPLAY_BUFFER_BYTES.get(message_id, 0) or 0)
                    - int(old.get("_size") or 0),
                )
                stream_metric("replay.trim")
            while (
                STREAM_REPLAY_BUFFER_MAX_BYTES > 0
                and buf
                and int(STREAM_REPLAY_BUFFER_BYTES.get(message_id, 0) or 0)
                > STREAM_REPLAY_BUFFER_MAX_BYTES
            ):
                old = buf.popleft()
                STREAM_REPLAY_BUFFER_BYTES[message_id] = max(
                    0,
                    int(STREAM_REPLAY_BUFFER_BYTES.get(message_id, 0) or 0)
                    - int(old.get("_size") or 0),
                )
                stream_metric("replay.trim")
        stream_metric("replay.append")
    except Exception:
        log.exception("stream replay append failed")


async def get_stream_replay_events(
    message_id: str, after_version: int, run: Optional[int] = None
) -> dict:
    after_version = int(after_version or 0)
    current_version = stream_version_get(message_id)
    current_run = stream_run_get(message_id)

    # Run mismatch = the client's version space belongs to a DIFFERENT
    # generation run of this message (retry/continue resets versions to 0 and
    # wipes the buffer). Its after_version is meaningless here — replaying
    # would splice new-run ops onto old-run content. Force a snapshot.
    if run and current_run and int(run) != current_run:
        stream_metric("replay.run_mismatch")
        return {
            "status": "miss",
            "snapshot_required": True,
            "run": current_run,
            "from_version": after_version,
            "to_version": current_version,
            "events": [],
        }
    # A client claiming to be AHEAD of the live counter is holding a stale
    # run's version (legacy client without a run id, or the run map was
    # cleaned). "ok, no events" here would freeze it forever — force a
    # snapshot instead.
    if after_version > current_version:
        stream_metric("replay.stale_epoch")
        return {
            "status": "miss",
            "snapshot_required": True,
            "run": current_run,
            "from_version": after_version,
            "to_version": current_version,
            "events": [],
        }
    try:
        if REDIS is not None:
            raw_entries = await REDIS.lrange(_stream_replay_key(message_id), 0, -1)
            entries = [json.loads(raw) for raw in raw_entries]
        else:
            entries = list(STREAM_REPLAY_BUFFERS.get(str(message_id), deque()))
    except Exception:
        log.exception("stream replay read failed")
        entries = []

    versioned = [
        entry
        for entry in entries
        if isinstance(entry, dict) and int(entry.get("replay_version") or 0) > 0
    ]
    if versioned:
        current_version = max(
            current_version,
            max(int(entry.get("replay_version") or 0) for entry in versioned),
        )
    if after_version < current_version:
        delta_versions = sorted(
            int(entry.get("replay_version") or 0)
            for entry in versioned
            if (entry.get("type") == "chat:delta")
        )
        if not delta_versions or delta_versions[0] > after_version + 1:
            stream_metric("replay.miss")
            return {
                "status": "miss",
                "snapshot_required": True,
                "run": current_run,
                "from_version": after_version,
                "to_version": current_version,
                "events": [],
            }

    events = [
        {"type": entry.get("type"), "data": entry.get("data")}
        for entry in versioned
        if int(entry.get("replay_version") or 0) > after_version
    ]
    stream_metric("replay.hit")
    return {
        "status": "ok",
        "snapshot_required": False,
        "run": current_run,
        "from_version": after_version,
        "to_version": current_version,
        "events": events,
    }


def _tool_body_size(body) -> int:
    try:
        return len(
            json.dumps(body, ensure_ascii=False, default=str).encode("utf-8", "replace")
        )
    except Exception:
        return 0


def _tool_body_key(message_id, tool_call_id) -> tuple[str, str]:
    return (str(message_id), str(tool_call_id))


def _spill_path(message_id: str, tool_call_id: str) -> str:
    digest = hashlib.sha256(
        f"{message_id}:{tool_call_id}".encode("utf-8", "replace")
    ).hexdigest()
    return os.path.join(STREAM_TOOL_RESULT_BODY_SPILL_DIR, f"{digest}.json")


def _spill_tool_result_body(message_id: str, tool_call_id: str, body: dict) -> None:
    if not STREAM_TOOL_RESULT_BODY_SPILL_DIR:
        return
    try:
        os.makedirs(STREAM_TOOL_RESULT_BODY_SPILL_DIR, exist_ok=True)
        path = _spill_path(message_id, tool_call_id)
        fd, tmp = tempfile.mkstemp(
            prefix="tool-body-",
            suffix=".json.tmp",
            dir=STREAM_TOOL_RESULT_BODY_SPILL_DIR,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, default=str)
            os.replace(tmp, path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass
        TOOL_RESULT_BODY_SPILLS.setdefault(message_id, {})[tool_call_id] = {
            "path": path,
            "size": _tool_body_size(body),
        }
        stream_metric("tool_body.spill")
    except Exception:
        log.exception("tool result body spill failed")


def _read_spilled_tool_result_body(message_id: str, tool_call_id: str):
    info = (TOOL_RESULT_BODY_SPILLS.get(str(message_id)) or {}).get(str(tool_call_id))
    path = info.get("path") if isinstance(info, dict) else None
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        stream_metric("tool_body.spill_miss")
        return None


def _remove_spilled_tool_result_body(message_id: str, tool_call_id: str) -> None:
    info = (TOOL_RESULT_BODY_SPILLS.get(str(message_id)) or {}).pop(
        str(tool_call_id), None
    )
    if not (TOOL_RESULT_BODY_SPILLS.get(str(message_id)) or {}):
        TOOL_RESULT_BODY_SPILLS.pop(str(message_id), None)
    path = info.get("path") if isinstance(info, dict) else None
    if path:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except Exception:
            log.debug("failed to remove spilled tool result body", exc_info=True)


def _drop_live_tool_result_body(
    message_id: str, tool_call_id: str, *, spill: bool = True
) -> None:
    global TOOL_RESULT_BODY_TOTAL_BYTES
    mid, tcid = str(message_id), str(tool_call_id)
    body = (TOOL_RESULT_BODIES.get(mid) or {}).pop(tcid, None)
    size = (TOOL_RESULT_BODY_SIZES.get(mid) or {}).pop(tcid, 0)
    if not (TOOL_RESULT_BODIES.get(mid) or {}):
        TOOL_RESULT_BODIES.pop(mid, None)
    if not (TOOL_RESULT_BODY_SIZES.get(mid) or {}):
        TOOL_RESULT_BODY_SIZES.pop(mid, None)
    TOOL_RESULT_BODY_ORDER.pop((mid, tcid), None)
    TOOL_RESULT_BODY_TOTAL_BYTES = max(0, TOOL_RESULT_BODY_TOTAL_BYTES - int(size or 0))
    if spill and isinstance(body, dict):
        _spill_tool_result_body(mid, tcid, body)


def _enforce_tool_result_body_caps(message_id: str) -> None:
    if STREAM_TOOL_RESULT_BODY_MAX_BYTES_PER_MESSAGE > 0:
        sizes = TOOL_RESULT_BODY_SIZES.get(str(message_id)) or {}
        while (
            sum(sizes.values()) > STREAM_TOOL_RESULT_BODY_MAX_BYTES_PER_MESSAGE
            and sizes
        ):
            victim = next(
                (
                    (mid, tcid)
                    for (mid, tcid) in TOOL_RESULT_BODY_ORDER.keys()
                    if mid == str(message_id)
                ),
                None,
            )
            if victim is None:
                break
            _drop_live_tool_result_body(victim[0], victim[1], spill=True)
            sizes = TOOL_RESULT_BODY_SIZES.get(str(message_id)) or {}

    if STREAM_TOOL_RESULT_BODY_MAX_BYTES > 0:
        while (
            TOOL_RESULT_BODY_TOTAL_BYTES > STREAM_TOOL_RESULT_BODY_MAX_BYTES
            and TOOL_RESULT_BODY_ORDER
        ):
            victim_mid, victim_tcid = next(iter(TOOL_RESULT_BODY_ORDER.keys()))
            _drop_live_tool_result_body(victim_mid, victim_tcid, spill=True)


def set_tool_result_body(message_id, tool_call_id, result) -> None:
    global TOOL_RESULT_BODY_TOTAL_BYTES
    try:
        mid, tcid = str(message_id), str(tool_call_id)
        if not mid or not tcid or not isinstance(result, dict):
            return
        _remove_spilled_tool_result_body(mid, tcid)
        existing_size = (TOOL_RESULT_BODY_SIZES.get(mid) or {}).get(tcid, 0)
        body = copy.deepcopy(result)
        size = _tool_body_size(body)
        by_message = TOOL_RESULT_BODIES.setdefault(mid, {})
        by_message[tcid] = body
        TOOL_RESULT_BODY_SIZES.setdefault(mid, {})[tcid] = size
        TOOL_RESULT_BODY_ORDER.pop((mid, tcid), None)
        TOOL_RESULT_BODY_ORDER[(mid, tcid)] = size
        TOOL_RESULT_BODY_TOTAL_BYTES = (
            max(0, TOOL_RESULT_BODY_TOTAL_BYTES - int(existing_size or 0)) + size
        )
        stream_metric("tool_body.store")
        stream_metric("tool_body.bytes", size)
        _enforce_tool_result_body_caps(mid)
    except Exception:
        pass


def get_tool_result_body(message_id, tool_call_id):
    try:
        mid, tcid = str(message_id), str(tool_call_id)
        result = TOOL_RESULT_BODIES.get(mid, {}).get(tcid)
        if isinstance(result, dict):
            TOOL_RESULT_BODY_ORDER.move_to_end((mid, tcid), last=True)
            return copy.deepcopy(result)
        spilled = _read_spilled_tool_result_body(mid, tcid)
        return copy.deepcopy(spilled) if isinstance(spilled, dict) else None
    except Exception:
        return None


def get_tool_result_bodies(message_id, *, deep_copy: bool = True) -> dict:
    """Return the accumulated large tool-result bodies for a message.

    ``deep_copy=True`` (default) copies so external callers can't mutate the
    live store. The per-round agentic loop passes ``deep_copy=False``: at round K
    the store holds K accumulated web-page/file bodies, and a deepcopy on every
    round is O(total tool output) × rounds = O(N²) of large-data copying on the
    event loop. The two hot-path callers (the checkpoint builder and the
    in-flight assistant assembly) consume the result strictly read-only — the
    checkpoint spreads it into a new dict, and ``_hydrate_tool_result_refs`` only
    reads body values (and copies-on-write into fresh result dicts) — and the
    outbound conversion runs to completion synchronously before the store can
    change again, so sharing the live reference is safe."""
    try:
        bodies = TOOL_RESULT_BODIES.get(str(message_id), {}) or {}
        spills = TOOL_RESULT_BODY_SPILLS.get(str(message_id), {}) or {}
        if spills:
            merged = dict(bodies)
            for tool_call_id in list(spills.keys()):
                spilled = _read_spilled_tool_result_body(str(message_id), tool_call_id)
                if isinstance(spilled, dict):
                    merged[tool_call_id] = spilled
            bodies = merged
        if not deep_copy:
            return bodies
        return copy.deepcopy(bodies)
    except Exception:
        return {}


def clear_tool_result_bodies(message_id) -> None:
    try:
        mid = str(message_id)
        for tool_call_id in list((TOOL_RESULT_BODIES.get(mid) or {}).keys()):
            _drop_live_tool_result_body(mid, tool_call_id, spill=False)
        for tool_call_id in list((TOOL_RESULT_BODY_SPILLS.get(mid) or {}).keys()):
            _remove_spilled_tool_result_body(mid, tool_call_id)
    except Exception:
        pass


def set_tool_result(message_id, tool_call_id, result) -> None:
    try:
        existing = copy.deepcopy(TOOL_RESULTS.get(message_id, {}) or {})
        if not isinstance(existing, dict):
            existing = {}
        existing[tool_call_id] = copy.deepcopy(result)
        TOOL_RESULTS[message_id] = existing
        set_stream_state(message_id, {"tool_results_updated_at": time.time()})
    except Exception:
        pass


def get_tool_results(message_id) -> dict:
    try:
        existing = TOOL_RESULTS.get(message_id, {}) or {}
        return copy.deepcopy(existing) if isinstance(existing, dict) else {}
    except Exception:
        return {}


def clear_stream_state(message_id, delay: float = STREAM_DONE_GRACE_SECONDS) -> None:
    """Retain terminal stream state briefly so a reload racing with chat:done
    can still reconcile from RAM instead of falling back to a potentially stale
    DB read. Passing delay<=0 deletes immediately."""
    if delay <= 0:
        _delete_stream_state_now(str(message_id))
        return
    _cancel_stream_cleanup(str(message_id))
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            _STREAM_CLEANUP_TASKS[str(message_id)] = loop.create_task(
                _delete_stream_state_later(str(message_id), delay)
            )
            return
    except Exception:
        pass
    # If no loop is available, keep state rather than risking reload races.


# Event types whose inner data gets stamped with the stream run id. These are
# exactly the events a client uses to advance/finalize a per-message stream
# mirror — the run id lets it detect a version-space reset (retry/continue on
# the same message id) instead of misclassifying fresh deltas as stale.
_RUN_STAMPED_TYPES = {
    "chat:delta",
    "chat:done",
    "chat:message:error",
    "chat:tasks:cancel",
    "tool_call:result",
}


def _stamp_stream_run(payload) -> None:
    """Attach the current run id to a stream event's inner data, in place.

    Single chokepoint: every stream-scoped event flows through emit_to_primary
    before batching/replay-buffering, so stamping here covers the live wire,
    chat:delta:batch items, AND the replay buffer (which stores the same inner
    dict by reference)."""
    try:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or data.get("type") not in _RUN_STAMPED_TYPES:
            return
        inner = data.get("data")
        if not isinstance(inner, dict) or "run" in inner:
            return
        message_id = inner.get("message_id") or payload.get("message_id")
        if not message_id:
            return
        run = stream_run_get(message_id)
        if run:
            inner["run"] = run
    except Exception:
        pass


async def emit_to_primary(user_id, payload):
    """Emit a single events payload. Stream-scoped v2.1 events route to the
    current chat room plus origin socket; unrelated tabs do not receive token
    deltas. Non-stream events retain the primary/fallback behavior."""
    _stamp_stream_run(payload)
    # v2.1 batching layer: collapse per-tick chat:delta / tool_call:result
    # emissions into a single envelope per (user, chat). Batching by user only
    # can mix two chats and route a combined batch to the wrong stream room.
    if (
        STREAM_DELTA_BATCH_ENABLED
        and STREAM_PROTOCOL_VERSION == "v2.1"
        and user_id
        and _is_batchable_payload(payload)
    ):
        await append_stream_replay_event(payload)
        await _enqueue_delta(user_id, payload)
        return
    if STREAM_PROTOCOL_VERSION == "v2.1" and user_id and _is_batchable_payload(payload):
        await append_stream_replay_event(payload)
    # Non-batchable / terminal event: drain this chat's pending batch first so
    # order is preserved (deltas before the terminal envelope), then emit.
    if STREAM_DELTA_BATCH_ENABLED and STREAM_PROTOCOL_VERSION == "v2.1" and user_id:
        await _flush_delta_buffers_for_payload(user_id, payload)
    await _emit_to_primary_raw(user_id, payload)


async def emit_chat_user_message(
    user_id,
    chat_id,
    session_id,
    assistant_message_id,
    user_message,
    leaf_message_id=None,
):
    """Co-deliver a newly-persisted USER message to other devices in this chat.

    The assistant token stream already reaches every device in
    ``stream_room(chat_id)`` (they joined via ``stream:subscribe``), but the
    user's prompt was never broadcast — so a second device viewing the same chat
    saw the answer appear with no question in front of it, parented to a stale
    node. This emits the persisted user-message row (``user_message``, carrying
    portable file urls) as a stream-scoped ``chat:user-message`` event so those
    same devices can surgically insert the prompt bubble and parent the assistant
    under it.

    The envelope ``message_id`` is the ASSISTANT id (matching the stream) and the
    payload also carries ``assistant_message_id`` so a receiver can re-parent the
    assistant regardless of whether this event or the first ``chat:delta`` lands
    first.

    Because ``chat:user-message`` is in ``STREAM_SCOPED_TYPES``, routing through
    ``emit_to_primary`` -> ``_emit_to_primary_raw`` fans it out to
    ``stream_room(chat_id)`` members + the origin sid ONLY (same user, same
    chat — the room is gated by ``Chats.user_owns_chat`` at subscribe time). It
    is idempotent on the client (keyed by ``user_message.id``), so the origin
    device receiving its own event is a harmless no-op.
    """
    if not (
        user_id
        and chat_id
        and isinstance(user_message, dict)
        and user_message.get("id")
    ):
        return False
    if str(chat_id).startswith("local:"):
        return False
    payload = {
        "chat_id": chat_id,
        "message_id": assistant_message_id,
        "session_id": session_id,
        "data": {
            "type": "chat:user-message",
            "user_message": user_message,
            "assistant_message_id": assistant_message_id,
            "leaf_message_id": leaf_message_id,
        },
    }
    # Keep the broadcast frame bounded: a screen-capture (canvas.toDataURL) image
    # rides as an un-uploaded ``data:`` base64 url and can be several MB. Unlike
    # chat:delta this event carries the user's attachments AND bypasses the
    # batch-size splitter, so an oversized frame would be one giant un-batched
    # emit. When that happens, skip the surgical insert and let the receiver's
    # loadChat() backstop sync the prompt (it is already persisted) instead.
    #
    # Returns True iff the bubble was actually delivered. The queue-drain caller
    # uses this to decide whether to clear the queue chip now (atomic with the
    # bubble) or keep it until the loadChat backstop — so an undeliverable
    # (oversized) drained bubble never leaves the message in neither place.
    try:
        size = _socket_payload_size(payload)
        if size > SOCKET_BATCH_MAX_BYTES:
            log.info(
                "chat:user-message payload too large (%d bytes) for chat %s; "
                "deferring cross-device sync to the loadChat backstop",
                size,
                chat_id,
            )
            return False
    except Exception:
        pass
    try:
        await emit_to_primary(user_id, payload)
        return True
    except Exception:
        log.exception("emit_chat_user_message failed")
        return False


async def emit_user_fanout(user_id, payload):
    """Deliver an events payload to EVERY session of ``user_id`` (+ the origin
    session, if any), bypassing stream-scoped routing.

    ``emit_to_primary`` / ``_emit_to_primary_raw`` target only the elected
    primary session and stream-room subscribers. That is correct for a live
    parent generation (the visible tab is provably in the stream room), but a
    DETACHED subagent rerun has no active parent stream: primary-election +
    stream-room membership are unreliable, so those live updates can miss the
    user's background / non-subscriber tabs and leave a rerun card stuck. This
    helper fans out to all of the user's sessions directly so a rerun's updates
    always reach whichever tab the user is actually looking at. It mirrors the
    wire shape of every other events emit (chat_id / message_id / data)."""
    chat_id = payload.get("chat_id") if isinstance(payload, dict) else None
    message_id = payload.get("message_id") if isinstance(payload, dict) else None
    data = payload.get("data") if isinstance(payload, dict) else None
    origin_sid = payload.get("session_id") if isinstance(payload, dict) else None
    sids = list(
        {
            *(USER_POOL.get(user_id, []) or []),
            *([origin_sid] if origin_sid else []),
        }
    )
    emit_calls = [
        sio.emit(
            "events",
            {"chat_id": chat_id, "message_id": message_id, "data": data},
            to=sid,
        )
        for sid in sids
        if sid
    ]
    if emit_calls:
        await asyncio.gather(*emit_calls)


_COMPACT_OP_CODES = {
    "text_append": "t",
    "block_open": "o",
    "block_close": "c",
    "tool_call_add": "a",
    "tool_call_args_append": "g",
    "reasoning_detail_merge": "r",
    "sources": "s",
    "selected_model_id": "m",
    "usage": "u",
    "replace": "p",
    "snapshot": "x",
}


def _stream_payload_message_id(payload) -> Optional[str]:
    try:
        data = payload.get("data") if isinstance(payload, dict) else None
        inner = data.get("data") if isinstance(data, dict) else None
        if isinstance(inner, dict) and inner.get("message_id"):
            return str(inner.get("message_id"))
        if payload.get("message_id"):
            return str(payload.get("message_id"))
    except Exception:
        pass
    return None


def _stream_payload_version(payload) -> Optional[int]:
    try:
        data = payload.get("data") if isinstance(payload, dict) else None
        inner = data.get("data") if isinstance(data, dict) else None
        if isinstance(inner, dict) and isinstance(inner.get("version"), int):
            return int(inner.get("version"))
    except Exception:
        pass
    return None


def _stream_payload_versions(payload) -> list[tuple[str, int]]:
    event_type = _payload_type(payload)
    if event_type == "chat:delta":
        message_id = _stream_payload_message_id(payload)
        version = _stream_payload_version(payload)
        return [(message_id, version)] if message_id and version is not None else []
    if event_type == "chat:delta:batch":
        out: list[tuple[str, int]] = []
        for item in (payload.get("data") or {}).get("batch") or []:
            data = item.get("data") if isinstance(item, dict) else None
            inner = data.get("data") if isinstance(data, dict) else None
            if not isinstance(inner, dict) or data.get("type") != "chat:delta":
                continue
            mid = str(inner.get("message_id") or item.get("message_id") or "")
            version = inner.get("version")
            if mid and isinstance(version, int):
                out.append((mid, version))
        return out
    if event_type == "chat:delta:batch2":
        out: list[tuple[str, int]] = []
        for group in (payload.get("data") or {}).get("groups") or []:
            mid = str(group.get("message_id") or "") if isinstance(group, dict) else ""
            base_version = (
                int(group.get("base_version") or 0) if isinstance(group, dict) else 0
            )
            offset_versions = (
                bool(group.get("version_mode") == "offset")
                if isinstance(group, dict)
                else False
            )
            for delta in group.get("deltas") or []:
                if (
                    mid
                    and isinstance(delta, list)
                    and delta
                    and isinstance(delta[0], int)
                ):
                    version = (
                        base_version + int(delta[0])
                        if offset_versions
                        else int(delta[0])
                    )
                    out.append((mid, version))
        return out
    return []


def _subscriber_state(chat_id: str, sid: str) -> dict:
    return (STREAM_SUBSCRIPTION_STATE.get(chat_id) or {}).get(sid) or {
        "visible": True,
        "capabilities": {},
    }


def _subscriber_capabilities(chat_id: str, sid: str) -> dict:
    caps = _subscriber_state(chat_id, sid).get("capabilities") or {}
    return caps if isinstance(caps, dict) else {}


def _is_visible_subscriber(chat_id: str, sid: str) -> bool:
    return bool(_subscriber_state(chat_id, sid).get("visible", True))


def _is_batch_payload(payload) -> bool:
    return _payload_type(payload) in {"chat:delta:batch", "chat:delta:batch2"}


def _is_live_delta_payload(payload) -> bool:
    return (
        _is_batchable_payload(payload)
        or _is_batch_payload(payload)
        or _payload_type(payload) == "browser:frame"
    )


def _inner_event_is_terminal(inner_event) -> bool:
    if not isinstance(inner_event, dict):
        return False
    itype = inner_event.get("type")
    if itype in ("chat:done", "chat:message:error", "chat:tasks:cancel"):
        return True
    if itype == "chat:completion":
        idata = inner_event.get("data")
        return isinstance(idata, dict) and idata.get("done") is True
    return False


def _subagent_update_is_terminal(data) -> bool:
    if not isinstance(data, dict) or data.get("type") != "chat:subagent:update":
        return False
    inner = data.get("data")
    if not isinstance(inner, dict):
        return False
    return _inner_event_is_terminal(inner.get("inner_event"))


def _payload_has_terminal_subagent_event(payload) -> bool:
    """True when the payload carries a TERMINAL subagent update (its inner_event is
    chat:done / chat:message:error / chat:tasks:cancel / a done chat:completion),
    either as a single chat:subagent:update or inside a chat:delta:batch.

    Terminal subagent state must reach EVERY same-user tab — including a
    backgrounded/hidden one — because, unlike the main chat stream, a subagent's
    live card terminal is NOT in the stream replay buffer and is NOT carried by the
    snapshot/resume endpoint. If the visibility gate suppresses it for a hidden
    tab, that tab's card spins 'Researching…' forever once the user returns. The
    non-terminal token stream stays suppressed for hidden tabs (handled below)."""
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    ptype = data.get("type")
    if ptype == "chat:subagent:update":
        return _subagent_update_is_terminal(data)
    if ptype in ("chat:delta:batch", "chat:delta:batch2"):
        for item in data.get("batch") or []:
            idata = item.get("data") if isinstance(item, dict) else None
            if _subagent_update_is_terminal(idata):
                return True
    return False


def _make_sync_required_payload(payload, message_id: str, version: int) -> dict:
    return {
        "chat_id": payload.get("chat_id"),
        "message_id": message_id,
        "session_id": payload.get("session_id"),
        "data": {
            "type": "chat:stream:sync_required",
            "data": {"message_id": message_id, "version": version},
        },
    }


async def _emit_sync_required_once(
    sid: str, payload, message_id: str, version: int
) -> None:
    key = (sid, message_id)
    if key in STREAM_SYNC_REQUIRED_SENT:
        return
    STREAM_SYNC_REQUIRED_SENT.add(key)
    stream_metric("backpressure.sync_required")
    await sio.emit(
        "events", _make_sync_required_payload(payload, message_id, version), to=sid
    )


async def _hidden_catchup_worker(sid: str, chat_id: str, message_id: str) -> None:
    """After a debounce window, nudge a still-hidden subscriber to pull the current
    snapshot for ``message_id`` (see STREAM_HIDDEN_CATCHUP_MS). Guarded so it only
    fires while the socket is live, the subscriber is still hidden and subscribed to
    this chat, and the stream is still in progress — otherwise it is a no-op."""
    try:
        await asyncio.sleep(max(0.05, STREAM_HIDDEN_CATCHUP_MS / 1000.0))
        if not sid or sid not in SESSION_POOL:
            return
        if _is_visible_subscriber(chat_id, sid):
            return
        if sid not in (STREAM_SUBSCRIPTION_STATE.get(chat_id) or {}):
            return
        state = get_stream_state(message_id)
        if not state or state.get("status") != "in_progress":
            return
        version = int(stream_version_get(message_id) or 0)
        stream_metric("visibility.hidden_catchup")
        await sio.emit(
            "events",
            _make_sync_required_payload(
                {"chat_id": chat_id, "message_id": message_id, "session_id": None},
                message_id,
                version,
            ),
            to=sid,
        )
    except asyncio.CancelledError:
        return
    except Exception:
        pass
    finally:
        _HIDDEN_CATCHUP_TASKS.pop((sid, message_id), None)


def _schedule_hidden_catchup(sid: str, chat_id: str, message_id: str) -> None:
    """Debounced: arm at most one pending catch-up nudge per (sid, message_id).
    The worker clears its own key on completion, so the next suppressed delta
    re-arms it — yielding ~one nudge per STREAM_HIDDEN_CATCHUP_MS while suppression
    continues, and none once the stream ends (deltas stop → no re-arm)."""
    if STREAM_HIDDEN_CATCHUP_MS <= 0 or not (sid and chat_id and message_id):
        return
    key = (sid, message_id)
    existing = _HIDDEN_CATCHUP_TASKS.get(key)
    if existing is not None and not existing.done():
        return
    try:
        _HIDDEN_CATCHUP_TASKS[key] = asyncio.ensure_future(
            _hidden_catchup_worker(sid, chat_id, message_id)
        )
    except Exception:
        pass


def _cancel_hidden_catchup_for_message(message_id: str) -> None:
    for key in list(_HIDDEN_CATCHUP_TASKS):
        if key[1] == message_id:
            task = _HIDDEN_CATCHUP_TASKS.pop(key, None)
            if task is not None and not task.done():
                task.cancel()


async def _should_emit_stream_payload_to_sid(sid: str, payload) -> bool:
    chat_id = str(payload.get("chat_id") or "") if isinstance(payload, dict) else ""
    if not chat_id:
        return True
    if (
        _is_live_delta_payload(payload)
        and not _is_visible_subscriber(chat_id, sid)
        and not _payload_has_terminal_subagent_event(payload)
    ):
        stream_metric("visibility.hidden_suppressed")
        # G1 (multi-client): the tab is document-hidden so the high-frequency token
        # stream is suppressed for perf — but rather than freeze it until chat:done,
        # arm a coalesced catch-up so a passively-watched second screen / phone stays
        # near-live (it pulls a fresh snapshot ~once per STREAM_HIDDEN_CATCHUP_MS).
        mids = {mid for mid, _ in _stream_payload_versions(payload) if mid}
        env_mid = payload.get("message_id") if isinstance(payload, dict) else None
        if env_mid:
            mids.add(str(env_mid))
        for mid in mids:
            _schedule_hidden_catchup(sid, chat_id, mid)
        return False
    caps = _subscriber_capabilities(chat_id, sid)
    if not caps.get("ack"):
        return True
    versions = _stream_payload_versions(payload)
    if not versions:
        return True
    for message_id, version in versions:
        acked = int((STREAM_CLIENT_ACKS.get(sid) or {}).get(message_id) or 0)
        if version - acked > STREAM_CLIENT_LAG_MAX_VERSIONS:
            await _emit_sync_required_once(sid, payload, message_id, version)
            return False
    return True


def _make_delta_batch2_envelope(batch: list) -> Optional[dict]:
    head = batch[0] if isinstance(batch[0], dict) else {}
    groups: dict[str, dict] = {}
    for item in batch:
        data = item.get("data") if isinstance(item, dict) else None
        if not isinstance(data, dict) or data.get("type") not in {
            "chat:delta",
            "tool_call:result",
        }:
            return None
        inner = data.get("data") or {}
        if not isinstance(inner, dict):
            return None
        message_id = str(inner.get("message_id") or item.get("message_id") or "")
        if not message_id:
            return None
        group = groups.setdefault(
            message_id, {"message_id": message_id, "deltas": [], "tool_results": []}
        )
        # Compact frames drop the per-delta dict (and its run stamp) — carry the
        # run once per message group so the client can still run-gate the batch.
        run = inner.get("run")
        if run and "run" not in group:
            group["run"] = run
        if data.get("type") == "chat:delta":
            version = int(inner.get("version") or 0)
            if "base_version" not in group:
                group["base_version"] = max(0, version - 1) if version > 0 else 0
                group["version_mode"] = "offset"
            frame_version = version - int(group.get("base_version") or 0)
            group["deltas"].append(
                _compact_delta_frame(
                    frame_version,
                    inner.get("op") or "",
                    inner.get("payload") or {},
                )
            )
        else:
            group["tool_results"].append(inner)

    compact_groups = []
    for group in groups.values():
        group = dict(group)
        if not group.get("deltas"):
            group.pop("deltas", None)
        if not group.get("tool_results"):
            group.pop("tool_results", None)
        compact_groups.append(group)

    return {
        "chat_id": head.get("chat_id"),
        "message_id": head.get("message_id"),
        "session_id": head.get("session_id"),
        "data": {
            "type": "chat:delta:batch2",
            "format": "owui.stream.v2.1",
            "groups": compact_groups,
        },
    }


def _compact_delta_frame(version: int, op: str, payload: dict) -> list:
    code = _COMPACT_OP_CODES.get(op, op or "")
    payload = payload if isinstance(payload, dict) else {}
    if op == "text_append":
        return [version, code, payload.get("block_idx"), payload.get("text") or ""]
    if op == "block_open":
        return [
            version,
            code,
            payload.get("block_idx"),
            payload.get("type"),
            payload.get("attrs") or {},
        ]
    if op == "block_close":
        return [
            version,
            code,
            payload.get("block_idx"),
            payload.get("duration"),
            payload.get("output"),
            payload.get("ended"),
            payload.get("results") or [],
            payload.get("ended_at"),
        ]
    if op == "tool_call_add":
        return [version, code, payload.get("block_idx"), payload.get("tool_call")]
    if op == "tool_call_args_append":
        return [
            version,
            code,
            payload.get("tool_call_id"),
            payload.get("args_delta") or "",
        ]
    if op == "reasoning_detail_merge":
        return [version, code, payload.get("detail") or {}]
    if op == "sources":
        return [version, code, payload.get("sources") or []]
    if op == "selected_model_id":
        return [version, code, payload.get("model_id")]
    if op == "usage":
        return [version, code, payload.get("usage") or {}]
    if op == "replace":
        return [
            version,
            code,
            payload.get("block_idx", 0),
            payload.get("content_blocks") or [],
        ]
    if op == "snapshot":
        return [version, code]
    return [version, code, payload]


def _payload_for_sid(chat_id: str, sid: str, payload):
    if _payload_type(payload) != "chat:delta:batch":
        return payload
    caps = _subscriber_capabilities(chat_id, sid)
    if not caps.get("compact_batch"):
        return payload
    batch = (
        ((payload.get("data") or {}).get("batch") or [])
        if isinstance(payload, dict)
        else []
    )
    compact = _make_delta_batch2_envelope(batch)
    if compact:
        stream_metric("compact.batch2")
    return compact or payload


async def _emit_to_primary_raw(user_id, payload):
    try:
        primary = PRIMARY_SESSION_PER_USER.get(user_id)
    except Exception:
        primary = None

    origin_sid = None
    try:
        origin_sid = payload.get("session_id") if isinstance(payload, dict) else None
    except Exception:
        origin_sid = None

    targets = []

    if _is_stream_scoped_payload(payload):
        # Stream subscribers are tabs currently viewing this chat. This avoids
        # sending every token to every tab while preserving reload/resume: Chat
        # subscribes before requesting active snapshots.
        try:
            for sid in get_session_ids_from_room(stream_room(payload.get("chat_id"))):
                if sid and sid not in targets:
                    targets.append(sid)
        except Exception:
            pass
        # Only treat origin_sid as a target when it is still a LIVE session. A
        # stale launch-time origin — e.g. a long-running subagent whose viewing
        # tab reconnected and got a NEW sid mid-run — must NOT be appended: a dead
        # sid would make `targets` non-empty and SKIP the fan-out fallback below,
        # so the actually-viewing tab is dropped. That is the
        # chat:subagent:update black-hole: the tab still got the fan-out
        # chat:subagent:start (so the card appears + the timer ticks), but every
        # forwarded content update routed only to the dead origin sid, leaving the
        # card empty for the whole run.
        origin_live = False
        try:
            origin_live = bool(origin_sid) and origin_sid in SESSION_POOL
        except Exception:
            origin_live = bool(origin_sid)
        if origin_live and origin_sid not in targets:
            targets.append(origin_sid)
        # No live stream-room subscriber and no live origin. Rather than drop the
        # event, fan out to all of the user's connected sessions — parity with how
        # chat:subagent:start is delivered (which is why the start always lands) —
        # so the viewing tab still receives it; then the elected primary as a final
        # backstop. The per-sid visibility gate in _should_emit_stream_payload_to_sid
        # still suppresses genuinely-hidden background tabs, so this is not a blanket
        # fan-out of every token.
        if not targets:
            try:
                for sid in USER_POOL.get(user_id, []) or []:
                    if sid and sid not in targets:
                        targets.append(sid)
            except Exception:
                pass
            if primary and primary not in targets:
                targets.append(primary)
    else:
        if primary:
            targets.append(primary)
        if origin_sid and origin_sid not in targets:
            targets.append(origin_sid)
        if not targets:
            for sid in USER_POOL.get(user_id, []) or []:
                if sid and sid not in targets:
                    targets.append(sid)

    if not targets:
        return

    emit_calls = []
    chat_id = str(payload.get("chat_id") or "") if isinstance(payload, dict) else ""
    for sid in targets:
        if not await _should_emit_stream_payload_to_sid(sid, payload):
            continue
        emit_calls.append(
            sio.emit("events", _payload_for_sid(chat_id, sid, payload), to=sid)
        )
    if emit_calls:
        await asyncio.gather(*emit_calls)


# --- chat:delta batching --------------------------------------------------
# Per-(user, chat) FIFO of pending envelopes. Drained at the end of the current
# asyncio tick via loop.call_soon. Chat scoping prevents simultaneous streams in
# two chats from being coalesced into one room-routed batch.
_BATCHABLE_TYPES = frozenset({"chat:delta", "tool_call:result", "chat:subagent:update"})
DeltaBatchKey = tuple[str, str]
_pending_delta_buffer: Dict[DeltaBatchKey, list] = {}
_pending_delta_buffer_sizes: Dict[DeltaBatchKey, int] = {}
_pending_delta_scheduled: Set[DeltaBatchKey] = set()
SOCKET_BATCH_MAX_BYTES = max(65536, min(1_000_000, WEBSOCKET_MAX_MESSAGE_SIZE // 2))
SOCKET_BATCH_ENVELOPE_OVERHEAD_BYTES = 256


def _socket_payload_size(payload) -> int:
    try:
        return len(
            json.dumps(payload, ensure_ascii=False, default=str).encode(
                "utf-8", "replace"
            )
        )
    except Exception:
        return SOCKET_BATCH_MAX_BYTES + 1


def _make_delta_batch_envelope(batch: list) -> dict:
    head = batch[0] if isinstance(batch[0], dict) else {}
    return {
        "chat_id": head.get("chat_id"),
        "message_id": head.get("message_id"),
        "session_id": head.get("session_id"),
        "data": {
            "type": "chat:delta:batch",
            "batch": batch,
        },
    }


def _split_delta_batch(buf: list) -> list[dict]:
    envelopes: list[dict] = []
    current: list = []
    current_size = SOCKET_BATCH_ENVELOPE_OVERHEAD_BYTES

    for item in buf:
        item_size = _socket_payload_size(item) + 2
        if current and current_size + item_size > SOCKET_BATCH_MAX_BYTES:
            envelopes.append(_make_delta_batch_envelope(current))
            current = [item]
            current_size = SOCKET_BATCH_ENVELOPE_OVERHEAD_BYTES + item_size
        else:
            current.append(item)
            current_size += item_size

    if current:
        envelopes.append(_make_delta_batch_envelope(current))
    return envelopes


def _is_batchable_payload(payload) -> bool:
    try:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return False
        return data.get("type") in _BATCHABLE_TYPES
    except Exception:
        return False


def _delta_batch_key(user_id, payload) -> DeltaBatchKey:
    chat_id = ""
    try:
        chat_id = str(payload.get("chat_id") or "") if isinstance(payload, dict) else ""
    except Exception:
        chat_id = ""
    return (str(user_id), chat_id)


async def _enqueue_delta(user_id, payload) -> None:
    try:
        key = _delta_batch_key(user_id, payload)
        buf = _pending_delta_buffer.get(key)
        if buf is None:
            buf = []
            _pending_delta_buffer[key] = buf
            _pending_delta_buffer_sizes[key] = SOCKET_BATCH_ENVELOPE_OVERHEAD_BYTES

        message_id = _stream_payload_message_id(payload)
        if (
            STREAM_DELTA_FIRST_TOKEN_IMMEDIATE
            and _payload_type(payload) == "chat:delta"
            and message_id
            and message_id not in STREAM_FIRST_DELTA_SENT
            and not buf
        ):
            STREAM_FIRST_DELTA_SENT.add(message_id)
            stream_metric("batch.first_delta_immediate")
            await _emit_to_primary_raw(user_id, payload)
            return

        if message_id:
            STREAM_FIRST_DELTA_SENT.add(message_id)
        buf.append(payload)
        _pending_delta_buffer_sizes[key] = (
            _pending_delta_buffer_sizes.get(key, SOCKET_BATCH_ENVELOPE_OVERHEAD_BYTES)
            + _socket_payload_size(payload)
            + 2
        )
        if _pending_delta_buffer_sizes[key] >= SOCKET_BATCH_MAX_BYTES:
            asyncio.create_task(_flush_delta_buffer(key))
            return
        if key in _pending_delta_scheduled:
            return
        _pending_delta_scheduled.add(key)
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is None or not loop.is_running():
            # No running loop (shouldn't happen in async context). Drop the
            # scheduling marker; the next call will try again. Avoid losing
            # buffered events by emitting synchronously as a fallback task.
            _pending_delta_scheduled.discard(key)
            try:
                asyncio.create_task(_flush_delta_buffer(key))
            except Exception:
                pass
            return
        delay = (
            max(0, min(STREAM_DELTA_BATCH_WINDOW_MS, STREAM_DELTA_BATCH_MAX_DELAY_MS))
            / 1000
        )
        if delay <= 0:
            loop.call_soon(_schedule_flush, key)
        else:
            loop.call_later(delay, _schedule_flush, key)
    except Exception:
        log.exception("delta batch enqueue failed")
        # Fallback: drop the batch path and emit immediately so the event
        # is never lost.
        try:
            asyncio.create_task(_emit_to_primary_raw(user_id, payload))
        except Exception:
            pass


def _schedule_flush(key: DeltaBatchKey) -> None:
    try:
        asyncio.create_task(_flush_delta_buffer(key))
    except Exception:
        log.exception("delta batch flush schedule failed")
        _pending_delta_scheduled.discard(key)


async def _flush_delta_buffers_for_payload(user_id, payload) -> None:
    if isinstance(payload, dict) and payload.get("chat_id"):
        await _flush_delta_buffer(_delta_batch_key(user_id, payload))
        return
    for key in list(_pending_delta_buffer.keys()):
        if key[0] == str(user_id):
            await _flush_delta_buffer(key)


async def _flush_delta_buffer(key: DeltaBatchKey) -> None:
    # Pop the buffer (preserve order) and clear the scheduled marker BEFORE
    # awaiting so a new enqueue during the await can reschedule.
    buf = _pending_delta_buffer.pop(key, None)
    _pending_delta_buffer_sizes.pop(key, None)
    _pending_delta_scheduled.discard(key)
    if not buf:
        return
    stream_metric("batch.flush")
    stream_metric("batch.events", len(buf))
    user_id = key[0]
    if len(buf) == 1:
        # Single envelope — no point wrapping in a batch.
        await _emit_to_primary_raw(user_id, buf[0])
        return
    # Inner envelopes carry their own ids and are replayed individually on the
    # frontend. Split by serialized size so enormous responses never exceed the
    # Engine.IO/Socket.IO packet limit as a single `chat:delta:batch` frame.
    for envelope in _split_delta_batch(buf):
        await _emit_to_primary_raw(user_id, envelope)


def get_event_call(request_info):
    async def __event_caller__(event_data):
        try:
            response = await sio.call(
                "events",
                {
                    "chat_id": request_info.get("chat_id", None),
                    "message_id": request_info.get("message_id", None),
                    "data": event_data,
                },
                to=request_info["session_id"],
            )
            return response
        except Exception as e:
            log.error(f"Error calling socket event: {e}", exc_info=True)
            return {"status": False, "error": str(e)}

    return __event_caller__


get_event_caller = get_event_call


def get_headless_event_call(request_info):
    """Event caller for request-free (headless) generations — e.g. the
    autonomous queue drain, which runs with no originating socket session.

    The normal ``get_event_call`` does ``sio.call("events", ..., to=session_id)``
    and AWAITS a client ack. With ``session_id=None`` that would broadcast to
    everyone and block forever waiting for a reply nobody owns. A headless run
    can't prompt a human, so we decline interactive callbacks immediately. The
    only caller is the ``direct_tool`` (client-executed tool) path, which treats
    a falsy ``status`` as "not handled" and falls through gracefully. Server-side
    tools don't use the caller at all and run normally."""

    async def __headless_event_caller__(event_data):
        return {"status": False, "headless": True}

    return __headless_event_caller__
