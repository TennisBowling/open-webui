import asyncio
import random

import socketio
import logging
import sys
import time
from typing import Dict, Set
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
)
from open_webui.utils.auth import decode_token
from open_webui.socket.utils import RedisDict, RedisLock, YdocManager
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
    )
else:
    sio = socketio.AsyncServer(
        cors_allowed_origins=[],
        async_mode="asgi",
        transports=(["websocket"] if ENABLE_WEBSOCKET_SUPPORT else ["polling"]),
        allow_upgrades=ENABLE_WEBSOCKET_SUPPORT,
        always_connect=True,
        max_http_buffer_size=WEBSOCKET_MAX_MESSAGE_SIZE,
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

    # Stream v2 state: per-message version counter + tool result cache. Both
    # are keyed by message_id. Cleared on chat:done / error / cancel. Snapshot
    # endpoint (B1) reads from here.
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


def get_token_groups():
    """Get all token groups"""
    return token_groups.get_token_groups()


def set_token_group(group_name: str, models: list, limit: int = None, reset_time: str = '00:00', reset_timezone: str = 'UTC', window_duration: int = None):
    """Set a token group"""
    # Try to update first, if not found create new
    if not token_groups.update_token_group(group_name, models, limit, window_duration):
        return token_groups.create_token_group(group_name, models, limit or 0, reset_time, reset_timezone, window_duration)


def update_token_group(group_name: str, models: list = None, limit: int = None, window_duration: int = None):
    """Update an existing token group"""
    return token_groups.update_token_group(group_name, models, limit, window_duration)


def delete_token_group(group_name: str):
    """Delete a token group"""
    return token_groups.delete_token_group(group_name)


def get_token_usage():
    """Get current token usage for all groups from database"""
    # Import here to avoid circular imports
    from open_webui.models.token_usage import token_groups as db_token_groups
    groups = db_token_groups.get_token_groups()  # This returns groups WITH usage from DB
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

        log.info(f"📊 [socket:usage] Received from frontend: model={model_id}, chat_id={chat_id}, user_id={user_id}")

        # Process token usage tracking with chat_id and user_id for analytics
        await process_token_usage(model_id, usage_data, chat_id=chat_id, user_id=user_id)


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
    
    log.info(f"Model switch request: chat_id={chat_id}, task_id={task_id}, new_model={new_model_id}")
    
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
                    }
                }
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
                        }
                    }
                },
                to=sid,
            )
            
            return {"status": True, "message": f"Model switch queued for {len(task_ids)} active task(s)"}
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
    user_id: str = None
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
        chat_id: Optional chat ID for conversation tracking
        user_id: Optional user ID for user-level analytics
    """
    log.info(f"📊 [process_token_usage] Called with model={model_id}, chat_id={chat_id}, user_id={user_id}")
    log.info(f"📊 [process_token_usage] usage_data={usage_data}")
    
    if not usage_data:
        log.info(f"📊 [process_token_usage] No usage_data, returning early")
        return

    # Extract token counts with safe defaults
    prompt_tokens = usage_data.get("prompt_tokens", 0)
    completion_tokens = usage_data.get("completion_tokens", 0)

    # Extract reasoning tokens from completion_tokens_details
    completion_tokens_details = usage_data.get("completion_tokens_details", {}) or {}
    reasoning_tokens = completion_tokens_details.get("reasoning_tokens", 0)

    # Calculate IN, OUT, TOTAL according to spec
    # IN = prompt_tokens
    # OUT = completion_tokens + reasoning_tokens
    # TOTAL = IN + OUT
    token_in = prompt_tokens
    token_out = completion_tokens + reasoning_tokens
    token_total = token_in + token_out
    
    log.info(f"📊 [process_token_usage] Calculated: in={token_in}, out={token_out}, total={token_total}")

    # 1. Update existing group-based token tracking (for rate limiting)
    token_groups.update_token_usage(model_id, token_in, token_out, token_total)

    # 2-4. Update analytics tables for "Wrapped" feature
    try:
        from open_webui.models.analytics import Analytics
        
        # 2. Update conversation token usage (per-chat tracking)
        if chat_id and user_id:
            log.info(f"📊 [process_token_usage] Updating conversation token usage for chat={chat_id}, user={user_id}")
            result = Analytics.update_conversation_token_usage(
                chat_id=chat_id,
                user_id=user_id,
                model_id=model_id,
                token_in=token_in,
                token_out=token_out,
                token_total=token_total
            )
            log.info(f"📊 [process_token_usage] Conversation update result: {result}")
        else:
            log.info(f"📊 [process_token_usage] Skipping conversation update - chat_id={chat_id}, user_id={user_id}")
        
        # 3. Update daily token usage (for heatmaps)
        if user_id:
            log.info(f"📊 [process_token_usage] Updating daily token usage for user={user_id}")
            Analytics.update_daily_token_usage(
                user_id=user_id,
                token_in=token_in,
                token_out=token_out,
                token_total=token_total,
                chat_id=chat_id
            )
        
        # 4. Update model token usage (for model breakdowns)
        log.info(f"📊 [process_token_usage] Updating model token usage for model={model_id}")
        Analytics.update_model_token_usage(
            user_id=user_id,
            model_id=model_id,
            token_in=token_in,
            token_out=token_out,
            token_total=token_total
        )
        
        log.info(f"📊 [process_token_usage] SUCCESS: model={model_id}, chat={chat_id}, user={user_id}, tokens={token_total}")
    except Exception as e:
        log.error(f"📊 [process_token_usage] ERROR updating analytics: {e}", exc_info=True)

    # Push refreshed token-usage groups to every active session of this user
    # so the frontend doesn't have to poll. Wire Contract #6.
    if user_id:
        try:
            groups = token_groups.get_token_groups()
            await push_token_usage_update(user_id, groups)
        except Exception as e:
            log.error(f"📊 [process_token_usage] ERROR pushing token-usage:update: {e}", exc_info=True)


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
            user = Users.get_user_by_id(data["id"])

        if user:
            SESSION_POOL[sid] = user.model_dump(
                exclude=["date_of_birth", "bio", "gender"]
            )
            if user.id in USER_POOL:
                USER_POOL[user.id] = USER_POOL[user.id] + [sid]
            else:
                USER_POOL[user.id] = [sid]

            _elect_primary_session(user.id, sid)


@sio.on("user-join")
async def user_join(sid, data):

    auth = data["auth"] if "auth" in data else None
    if not auth or "token" not in auth:
        return

    data = decode_token(auth["token"])
    if data is None or "id" not in data:
        return

    user = Users.get_user_by_id(data["id"])
    if not user:
        return

    SESSION_POOL[sid] = user.model_dump(exclude=["date_of_birth", "bio", "gender"])
    if user.id in USER_POOL:
        USER_POOL[user.id] = USER_POOL[user.id] + [sid]
    else:
        USER_POOL[user.id] = [sid]

    primary_sid = _elect_primary_session(user.id, sid)

    # Join all the channels
    channels = Channels.get_channels_by_user_id(user.id)
    log.debug(f"{channels=}")
    for channel in channels:
        await sio.enter_room(sid, f"channel:{channel.id}")
    return {"id": user.id, "name": user.name, "primary_session_id": primary_sid}


@sio.on("join-channels")
async def join_channel(sid, data):
    auth = data["auth"] if "auth" in data else None
    if not auth or "token" not in auth:
        return

    data = decode_token(auth["token"])
    if data is None or "id" not in data:
        return

    user = Users.get_user_by_id(data["id"])
    if not user:
        return

    # Join all the channels
    channels = Channels.get_channels_by_user_id(user.id)
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

    user = Users.get_user_by_id(token_data["id"])
    if not user:
        return

    note = Notes.get_note_by_id(data["note_id"])
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
            note = Notes.get_note_by_id(note_id)
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
        note = Notes.get_note_by_id(note_id)
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

        Notes.update_note_by_id(note_id, NoteUpdateForm(data=data))


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
                Chats.add_message_status_to_chat_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                    event_data.get("data", {}),
                )

            if "type" in event_data and event_data["type"] == "message":
                message = Chats.get_message_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                )

                if message:
                    content = message.get("content", "")
                    content += event_data.get("data", {}).get("content", "")

                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        request_info["chat_id"],
                        request_info["message_id"],
                        {
                            "content": content,
                        },
                    )

            if "type" in event_data and event_data["type"] == "replace":
                content = event_data.get("data", {}).get("content", "")

                Chats.upsert_message_to_chat_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                    {
                        "content": content,
                    },
                )

            if "type" in event_data and event_data["type"] == "embeds":
                message = Chats.get_message_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                )

                embeds = event_data.get("data", {}).get("embeds", [])
                embeds.extend(message.get("embeds", []))

                Chats.upsert_message_to_chat_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                    {
                        "embeds": embeds,
                    },
                )

            if "type" in event_data and event_data["type"] == "data_viz:override":
                message = Chats.get_message_by_id_and_message_id(
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

                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        request_info["chat_id"],
                        request_info["message_id"],
                        {
                            "dataVizOverrides": overrides,
                        },
                    )

            if "type" in event_data and event_data["type"] == "files":
                message = Chats.get_message_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                )

                files = event_data.get("data", {}).get("files", [])
                files.extend(message.get("files", []))

                Chats.upsert_message_to_chat_by_id_and_message_id(
                    request_info["chat_id"],
                    request_info["message_id"],
                    {
                        "files": files,
                    },
                )

            if event_data.get("type") in ["source", "citation"]:
                data = event_data.get("data", {})
                if data.get("type") == None:
                    message = Chats.get_message_by_id_and_message_id(
                        request_info["chat_id"],
                        request_info["message_id"],
                    )

                    sources = message.get("sources", [])
                    sources.append(data)

                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        request_info["chat_id"],
                        request_info["message_id"],
                        {
                            "sources": sources,
                        },
                    )

    return __event_emitter__


async def broadcast_sidebar_event(user_id, event_data, skip_sid=None):
    """Fan out a sidebar-affecting event to every active session of `user_id`,
    excluding `skip_sid` (the originating tab's socket.id). Sent on the same
    "events" channel the frontend already listens on; the envelope's chat_id
    is null because these events are not chat-scoped — they update the
    sidebar list, pinned/folder/tag state, etc."""
    session_ids = [
        sid for sid in USER_POOL.get(user_id, []) if sid and sid != skip_sid
    ]

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


def is_primary_session(user_id, sid) -> bool:
    """B8 helper. Defensive fallback: if no primary is recorded for the user,
    treat every session as primary (so v2 emission still reaches the client
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


def stream_version_init(message_id) -> int:
    try:
        STREAM_VERSION[message_id] = 0
    except Exception:
        pass
    _schedule_stream_state_ttl_refresh()
    return 0


def stream_version_incr(message_id) -> int:
    try:
        current = STREAM_VERSION.get(message_id, 0) or 0
    except Exception:
        current = 0
    nxt = int(current) + 1
    try:
        STREAM_VERSION[message_id] = nxt
    except Exception:
        pass
    return nxt


def stream_version_get(message_id) -> int:
    try:
        return int(STREAM_VERSION.get(message_id, 0) or 0)
    except Exception:
        return 0


def set_tool_result(message_id, tool_call_id, result) -> None:
    try:
        existing = TOOL_RESULTS.get(message_id, {}) or {}
        if not isinstance(existing, dict):
            existing = {}
        existing[tool_call_id] = result
        TOOL_RESULTS[message_id] = existing
    except Exception:
        pass


def get_tool_results(message_id) -> dict:
    try:
        existing = TOOL_RESULTS.get(message_id, {}) or {}
        return existing if isinstance(existing, dict) else {}
    except Exception:
        return {}


def clear_stream_state(message_id) -> None:
    for store in (STREAM_VERSION, TOOL_RESULTS):
        try:
            if message_id in store:
                del store[message_id]
        except Exception:
            pass


async def emit_to_primary(user_id, payload):
    """Emit a single events payload to the user's primary session only. Falls
    back to all sessions if no primary is registered (defensive — keeps v2
    working before B8 ships the election logic)."""
    # v2 batching layer: collapse per-tick chat:delta / tool_call:result
    # emissions into a single envelope per user. See _flush_delta_buffer.
    if (
        STREAM_DELTA_BATCH_ENABLED
        and STREAM_PROTOCOL_VERSION == "v2"
        and user_id
        and _is_batchable_payload(payload)
    ):
        _enqueue_delta(user_id, payload)
        return
    # Non-batchable / terminal event: drain any pending batch first so order
    # is preserved (deltas before the terminal envelope), then emit directly.
    if STREAM_DELTA_BATCH_ENABLED and STREAM_PROTOCOL_VERSION == "v2" and user_id:
        await _flush_delta_buffer(user_id)
    await _emit_to_primary_raw(user_id, payload)


async def _emit_to_primary_raw(user_id, payload):
    try:
        primary = PRIMARY_SESSION_PER_USER.get(user_id)
    except Exception:
        primary = None

    # Stream-v2 chat responses are initiated by one concrete browser session,
    # but are normally routed through the elected primary session so that the
    # primary tab can BroadcastChannel-relay to sibling tabs. If the submitting
    # tab is not primary (or the primary entry is stale), primary-only delivery
    # makes that tab wait forever. When an envelope carries the originating
    # session_id, deliver to both the primary and the origin. Versioned v2
    # deltas are idempotent on the client, so a same-browser BroadcastChannel
    # replay plus this direct emit will not double-append content.
    origin_sid = None
    try:
        origin_sid = payload.get("session_id") if isinstance(payload, dict) else None
    except Exception:
        origin_sid = None

    targets = []
    if primary:
        targets.append(primary)
    if origin_sid and origin_sid not in targets:
        targets.append(origin_sid)

    if targets:
        await asyncio.gather(*[sio.emit("events", payload, to=sid) for sid in targets])
        return

    session_ids = USER_POOL.get(user_id, []) or []
    if not session_ids:
        return
    await asyncio.gather(
        *[sio.emit("events", payload, to=sid) for sid in session_ids]
    )


# --- chat:delta batching --------------------------------------------------
# Per-user FIFO of pending envelopes. Drained at the end of the current
# asyncio tick via loop.call_soon. Within a tick, multiple synchronous
# emit_to_primary calls accumulate; across ticks each tick flushes once.
_BATCHABLE_TYPES = frozenset({"chat:delta", "tool_call:result", "chat:subagent:update"})
_pending_delta_buffer: Dict[str, list] = {}
_pending_delta_scheduled: Set[str] = set()


def _is_batchable_payload(payload) -> bool:
    try:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return False
        return data.get("type") in _BATCHABLE_TYPES
    except Exception:
        return False


def _enqueue_delta(user_id, payload) -> None:
    try:
        buf = _pending_delta_buffer.get(user_id)
        if buf is None:
            buf = []
            _pending_delta_buffer[user_id] = buf
        buf.append(payload)
        if user_id in _pending_delta_scheduled:
            return
        _pending_delta_scheduled.add(user_id)
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is None or not loop.is_running():
            # No running loop (shouldn't happen in async context). Drop the
            # scheduling marker; the next call will try again. Avoid losing
            # buffered events by emitting synchronously as a fallback task.
            _pending_delta_scheduled.discard(user_id)
            try:
                asyncio.create_task(_flush_delta_buffer(user_id))
            except Exception:
                pass
            return
        loop.call_soon(_schedule_flush, user_id)
    except Exception:
        log.exception("delta batch enqueue failed")
        # Fallback: drop the batch path and emit immediately so the event
        # is never lost.
        try:
            asyncio.create_task(_emit_to_primary_raw(user_id, payload))
        except Exception:
            pass


def _schedule_flush(user_id) -> None:
    try:
        asyncio.create_task(_flush_delta_buffer(user_id))
    except Exception:
        log.exception("delta batch flush schedule failed")
        _pending_delta_scheduled.discard(user_id)


async def _flush_delta_buffer(user_id) -> None:
    # Pop the buffer (preserve order) and clear the scheduled marker BEFORE
    # awaiting so a new enqueue during the await can reschedule.
    buf = _pending_delta_buffer.pop(user_id, None)
    _pending_delta_scheduled.discard(user_id)
    if not buf:
        return
    if len(buf) == 1:
        # Single envelope — no point wrapping in a batch.
        await _emit_to_primary_raw(user_id, buf[0])
        return
    # Use the chat_id/message_id of the first envelope so client-side dedup
    # has scope. Inner envelopes carry their own ids and will be replayed
    # individually on the frontend.
    head = buf[0] if isinstance(buf[0], dict) else {}
    envelope = {
        "chat_id": head.get("chat_id"),
        "message_id": head.get("message_id"),
        "session_id": head.get("session_id"),
        "data": {
            "type": "chat:delta:batch",
            "batch": buf,
        },
    }
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
