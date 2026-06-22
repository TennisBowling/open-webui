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
    list_task_ids_by_item_id,
)
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


async def periodic_usage_pool_cleanup():
    """Deprecated no-op retained for backward-compat with callers that still
    schedule it at startup. USAGE_POOL was removed; nothing to clean."""
    return


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
    if not await token_groups.update_token_group(group_name, models, limit, window_duration):
        return await token_groups.create_token_group(
            group_name, models, limit or 0, reset_time, reset_timezone, window_duration
        )


async def update_token_group(
    group_name: str, models: list = None, limit: int = None, window_duration: int = None
):
    """Update an existing token group"""
    return await token_groups.update_token_group(group_name, models, limit, window_duration)


async def delete_token_group(group_name: str):
    """Delete a token group"""
    return await token_groups.delete_token_group(group_name)


async def get_token_usage():
    """Get current token usage for all groups from database"""
    # Import here to avoid circular imports
    from open_webui.models.token_usage import token_groups as db_token_groups

    groups = await db_token_groups.get_token_groups()
    return {name: group_data["usage"] for name, group_data in groups.items()}


@sio.on("usage")
async def usage(sid, data):
    if sid in SESSION_POOL:
        model_id = data["model"]
        usage_data = data.get("usage", {})

        # Get user_id from session pool and chat_id from data
        user = SESSION_POOL.get(sid)
        user_id = user.get("id") if user else None
        chat_id = data.get("chat_id")

        log.info(
            f"📊 [socket:usage] Received from frontend: model={model_id}, chat_id={chat_id}, user_id={user_id}"
        )

        # Process token usage tracking with chat_id and user_id for analytics
        await process_token_usage(
            model_id, usage_data, chat_id=chat_id, user_id=user_id
        )


@sio.on("model-switch")
async def model_switch(sid, data):
    """
    Handle model switch requests during an active agentic loop.
    The switch will be applied at the next iteration of the tool call loop.
    """
    if sid not in SESSION_POOL:
        return {"status": False, "message": "Session not found"}

    chat_id = data.get("chat_id")
    new_model_id = data.get("model_id")
    task_id = data.get("task_id")

    if not new_model_id:
        return {"status": False, "message": "No model_id provided"}

    log.info(
        f"Model switch request: chat_id={chat_id}, task_id={task_id}, new_model={new_model_id}"
    )

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
        task_ids = await list_task_ids_by_item_id(REDIS, chat_id)
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

    chat_id = data.get("chat_id")
    new_tier = data.get("service_tier")
    task_id = data.get("task_id")

    if not new_tier:
        return {"status": False, "message": "No service_tier provided"}

    log.info(
        f"service_tier change request: chat_id={chat_id}, task_id={task_id}, new_tier={new_tier}"
    )

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
        task_ids = await list_task_ids_by_item_id(REDIS, chat_id)
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
            )
            log.info(f"📊 [process_token_usage] Conversation update result: {result}")
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

    # Push refreshed token-usage groups to every active session of this user
    # so the frontend doesn't have to poll. Wire Contract #6.
    if user_id:
        try:
            groups = await token_groups.get_token_groups()
            await push_token_usage_update(user_id, groups)
        except Exception as e:
            log.error(
                f"📊 [process_token_usage] ERROR pushing token-usage:update: {e}",
                exc_info=True,
            )


async def push_token_usage_update(user_id: str, groups: dict):
    # Emit to the primary session only; other tabs of the same user pick it up
    # via BroadcastChannel (F9). If no primary is currently elected (e.g. the
    # previous primary just disconnected and nobody is online), skip silently —
    # the next mount will fetch initial state.
    primary_sid = get_primary_session(user_id)
    if not primary_sid:
        return
    await sio.emit(
        "events",
        {
            "chat_id": None,
            "message_id": None,
            "data": {"type": "token-usage:update", "data": {"groups": groups}},
        },
        to=primary_sid,
    )


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


def get_event_emitter(request_info, update_db=True):
    async def __event_emitter__(event_data):
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
                        }, return_model=False
                    )

            if "type" in event_data and event_data["type"] == "replace":
                content = event_data.get("data", {}).get("content", "")

                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                    {
                        "content": content,
                    }, return_model=False
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
                    }, return_model=False
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
                        }, return_model=False
                    )

            if "type" in event_data and event_data["type"] == "files":
                message = await Chats.get_message_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                )

                if message is None:
                    message = {}
                files = event_data.get("data", {}).get("files", [])
                files.extend(message.get("files", []))

                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                    {
                        "files": files,
                    }, return_model=False
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
                        }, return_model=False
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
STREAM_REPLAY_BUFFERS: Dict[str, deque] = {}
STREAM_REPLAY_BUFFER_BYTES: Dict[str, int] = {}
STREAM_FIRST_DELTA_SENT: Set[str] = set()
STREAM_VERSION_LOCAL: Dict[str, int] = {}
STREAM_VERSION_LAST_STORED: Dict[str, int] = {}
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
    prev = existing.get("content_blocks_snapshot") if isinstance(existing, dict) else None
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
        dirty_from = max(0, min(dirty_from, len(incoming), len(prev_blocks), len(prev_sigs)))

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
        "content_blocks_snapshot": _build_content_blocks_snapshot({}, content_blocks or []),
        "status": "in_progress",
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
    if nxt - int(STREAM_VERSION_LAST_STORED.get(key, 0) or 0) >= STREAM_VERSION_STORE_FLUSH_EVERY:
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
        if STREAM_REPLAY_BUFFER_MAX_EVENTS > 0 and length > STREAM_REPLAY_BUFFER_MAX_EVENTS:
            trim_count = length - STREAM_REPLAY_BUFFER_MAX_EVENTS
            await REDIS.ltrim(key, -STREAM_REPLAY_BUFFER_MAX_EVENTS, -1)
        if STREAM_REPLAY_BUFFER_TTL_SECONDS > 0:
            await REDIS.expire(key, STREAM_REPLAY_BUFFER_TTL_SECONDS)
        if trim_count:
            stream_metric("replay.trim", trim_count)
        return

    while (
        (STREAM_REPLAY_BUFFER_MAX_EVENTS > 0 and length > STREAM_REPLAY_BUFFER_MAX_EVENTS)
        or (STREAM_REPLAY_BUFFER_MAX_BYTES > 0 and total_bytes > STREAM_REPLAY_BUFFER_MAX_BYTES)
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
            return str(inner.get("message_id") or payload.get("message_id") or "") or None
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
            while STREAM_REPLAY_BUFFER_MAX_EVENTS > 0 and len(buf) > STREAM_REPLAY_BUFFER_MAX_EVENTS:
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


async def get_stream_replay_events(message_id: str, after_version: int) -> dict:
    after_version = int(after_version or 0)
    current_version = stream_version_get(message_id)
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
        "from_version": after_version,
        "to_version": current_version,
        "events": events,
    }


def _tool_body_size(body) -> int:
    try:
        return len(json.dumps(body, ensure_ascii=False, default=str).encode("utf-8", "replace"))
    except Exception:
        return 0


def _tool_body_key(message_id, tool_call_id) -> tuple[str, str]:
    return (str(message_id), str(tool_call_id))


def _spill_path(message_id: str, tool_call_id: str) -> str:
    digest = hashlib.sha256(f"{message_id}:{tool_call_id}".encode("utf-8", "replace")).hexdigest()
    return os.path.join(STREAM_TOOL_RESULT_BODY_SPILL_DIR, f"{digest}.json")


def _spill_tool_result_body(message_id: str, tool_call_id: str, body: dict) -> None:
    if not STREAM_TOOL_RESULT_BODY_SPILL_DIR:
        return
    try:
        os.makedirs(STREAM_TOOL_RESULT_BODY_SPILL_DIR, exist_ok=True)
        path = _spill_path(message_id, tool_call_id)
        fd, tmp = tempfile.mkstemp(
            prefix="tool-body-", suffix=".json.tmp", dir=STREAM_TOOL_RESULT_BODY_SPILL_DIR
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
    info = (TOOL_RESULT_BODY_SPILLS.get(str(message_id)) or {}).pop(str(tool_call_id), None)
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


def _drop_live_tool_result_body(message_id: str, tool_call_id: str, *, spill: bool = True) -> None:
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
        while sum(sizes.values()) > STREAM_TOOL_RESULT_BODY_MAX_BYTES_PER_MESSAGE and sizes:
            victim = next(
                ((mid, tcid) for (mid, tcid) in TOOL_RESULT_BODY_ORDER.keys() if mid == str(message_id)),
                None,
            )
            if victim is None:
                break
            _drop_live_tool_result_body(victim[0], victim[1], spill=True)
            sizes = TOOL_RESULT_BODY_SIZES.get(str(message_id)) or {}

    if STREAM_TOOL_RESULT_BODY_MAX_BYTES > 0:
        while TOOL_RESULT_BODY_TOTAL_BYTES > STREAM_TOOL_RESULT_BODY_MAX_BYTES and TOOL_RESULT_BODY_ORDER:
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
        TOOL_RESULT_BODY_TOTAL_BYTES = max(0, TOOL_RESULT_BODY_TOTAL_BYTES - int(existing_size or 0)) + size
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


async def emit_to_primary(user_id, payload):
    """Emit a single events payload. Stream-scoped v2.1 events route to the
    current chat room plus origin socket; unrelated tabs do not receive token
    deltas. Non-stream events retain the primary/fallback behavior."""
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
        for item in ((payload.get("data") or {}).get("batch") or []):
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
        for group in ((payload.get("data") or {}).get("groups") or []):
            mid = str(group.get("message_id") or "") if isinstance(group, dict) else ""
            base_version = int(group.get("base_version") or 0) if isinstance(group, dict) else 0
            offset_versions = bool(group.get("version_mode") == "offset") if isinstance(group, dict) else False
            for delta in group.get("deltas") or []:
                if mid and isinstance(delta, list) and delta and isinstance(delta[0], int):
                    version = base_version + int(delta[0]) if offset_versions else int(delta[0])
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
    return _is_batchable_payload(payload) or _is_batch_payload(payload) or _payload_type(payload) == "browser:frame"


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


async def _emit_sync_required_once(sid: str, payload, message_id: str, version: int) -> None:
    key = (sid, message_id)
    if key in STREAM_SYNC_REQUIRED_SENT:
        return
    STREAM_SYNC_REQUIRED_SENT.add(key)
    stream_metric("backpressure.sync_required")
    await sio.emit("events", _make_sync_required_payload(payload, message_id, version), to=sid)


async def _should_emit_stream_payload_to_sid(sid: str, payload) -> bool:
    chat_id = str(payload.get("chat_id") or "") if isinstance(payload, dict) else ""
    if not chat_id:
        return True
    if _is_live_delta_payload(payload) and not _is_visible_subscriber(chat_id, sid):
        stream_metric("visibility.hidden_suppressed")
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
        if not isinstance(data, dict) or data.get("type") not in {"chat:delta", "tool_call:result"}:
            return None
        inner = data.get("data") or {}
        if not isinstance(inner, dict):
            return None
        message_id = str(inner.get("message_id") or item.get("message_id") or "")
        if not message_id:
            return None
        group = groups.setdefault(message_id, {"message_id": message_id, "deltas": [], "tool_results": []})
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
        return [version, code, payload.get("block_idx"), payload.get("type"), payload.get("attrs") or {}]
    if op == "block_close":
        return [
            version,
            code,
            payload.get("block_idx"),
            payload.get("duration"),
            payload.get("output"),
            payload.get("ended"),
            payload.get("results") or [],
        ]
    if op == "tool_call_add":
        return [version, code, payload.get("block_idx"), payload.get("tool_call")]
    if op == "tool_call_args_append":
        return [version, code, payload.get("tool_call_id"), payload.get("args_delta") or ""]
    if op == "reasoning_detail_merge":
        return [version, code, payload.get("detail") or {}]
    if op == "sources":
        return [version, code, payload.get("sources") or []]
    if op == "selected_model_id":
        return [version, code, payload.get("model_id")]
    if op == "usage":
        return [version, code, payload.get("usage") or {}]
    if op == "replace":
        return [version, code, payload.get("block_idx", 0), payload.get("content_blocks") or []]
    if op == "snapshot":
        return [version, code]
    return [version, code, payload]


def _payload_for_sid(chat_id: str, sid: str, payload):
    if _payload_type(payload) != "chat:delta:batch":
        return payload
    caps = _subscriber_capabilities(chat_id, sid)
    if not caps.get("compact_batch"):
        return payload
    batch = ((payload.get("data") or {}).get("batch") or []) if isinstance(payload, dict) else []
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
        if origin_sid and origin_sid not in targets:
            targets.append(origin_sid)
        # If nobody is subscribed yet (e.g. very early stream startup), fall back
        # to the primary so at least one same-user tab can still relay/display.
        if not targets and primary:
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
        emit_calls.append(sio.emit("events", _payload_for_sid(chat_id, sid, payload), to=sid))
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
        _pending_delta_buffer_sizes[key] = _pending_delta_buffer_sizes.get(
            key, SOCKET_BATCH_ENVELOPE_OVERHEAD_BYTES
        ) + _socket_payload_size(payload) + 2
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
        delay = max(0, min(STREAM_DELTA_BATCH_WINDOW_MS, STREAM_DELTA_BATCH_MAX_DELAY_MS)) / 1000
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
