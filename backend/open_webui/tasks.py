# tasks.py
import asyncio
import json
import logging
import time
from typing import Dict, Iterable, List, Literal, Mapping, Optional, TypedDict
from uuid import uuid4

from redis.asyncio import Redis
from fastapi import Request

from open_webui.env import SRC_LOG_LEVELS, REDIS_KEY_PREFIX


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

# A dictionary to keep track of active tasks
tasks: Dict[str, asyncio.Task] = {}
item_tasks = {}

# A dictionary to track pending model switches for active tasks
# Key: task_id, Value: {"model_id": str, "timestamp": float}
pending_model_switches: Dict[str, dict] = {}

# A dictionary to track pending service_tier switches for active tasks
# Key: task_id, Value: {"service_tier": str, "timestamp": float}
pending_service_tier_switches: Dict[str, dict] = {}

# Latest full tool-selection snapshot requested for each active task. Redis is
# authoritative in multi-worker deployments; this dictionary is the no-Redis
# development/test fallback.
pending_tool_selections: Dict[str, dict] = {}
pending_tool_selection_revisions: Dict[str, int] = {}


class GenerationOperation(TypedDict):
    generation_id: str
    chat_id: str
    message_id: str
    turn_id: str
    task_id: str


class GenerationSupersedeTransition(TypedDict):
    turn_id: str
    displaced: List[GenerationOperation]
    expires_at: float


# A generation operation exists for the complete lifetime of one user-visible
# assistant run: before conversation assembly, while its task is being
# registered, and until that task reaches a terminal state.  Task ids alone
# cannot model the first two phases, which is why "list tasks, then stop them"
# used to miss very fast Stops.
generation_operations: Dict[str, GenerationOperation] = {}
item_generation_operations: Dict[str, set[str]] = {}
# NOTE: there is deliberately NO `chat_generation_turns` mirror here. The chat's
# active turn is DERIVED from the live operations above (`_active_chat_turn`).
#
# It used to be a dict maintained by hand, popped as a side effect inside
# `unregister_generation_operation` — behind an early-returning ownership guard.
# Any unregister that took that early return stranded a CHAT-GLOBAL lease with no
# error and no log, and because in-memory operations have no expiry (the Redis
# path has a TTL + heartbeat; this one has neither), the strand was permanent:
# every later send to that chat got `turn_conflict` → HTTP 409 until the process
# restarted. Deriving it means the lease cannot disagree with reality, because it
# is not stored anywhere to disagree.
generation_cancel_intents: Dict[str, float] = {}
generation_turn_cancel_intents: Dict[str, float] = {}
chat_work_blocks: Dict[str, Dict[str, float]] = {}
generation_supersede_transitions: Dict[str, GenerationSupersedeTransition] = {}


REDIS_TASKS_KEY = f"{REDIS_KEY_PREFIX}:tasks"
REDIS_ITEM_TASKS_KEY = f"{REDIS_KEY_PREFIX}:tasks:item"
REDIS_PUBSUB_CHANNEL = f"{REDIS_KEY_PREFIX}:tasks:commands"
REDIS_TASK_LEASE_KEY_PREFIX = f"{REDIS_KEY_PREFIX}:tasks:lease"
REDIS_GENERATION_OPERATION_PREFIX = f"{REDIS_KEY_PREFIX}:tasks:generation"
REDIS_ITEM_GENERATIONS_PREFIX = f"{REDIS_KEY_PREFIX}:tasks:generation:item"
REDIS_CHAT_TURN_PREFIX = f"{REDIS_KEY_PREFIX}:tasks:generation:turn"
REDIS_GENERATION_CANCEL_PREFIX = f"{REDIS_KEY_PREFIX}:tasks:generation:cancel"
REDIS_GENERATION_TURN_CANCEL_PREFIX = f"{REDIS_KEY_PREFIX}:tasks:generation:cancel-turn"
REDIS_CHAT_WORK_BLOCK_PREFIX = f"{REDIS_KEY_PREFIX}:tasks:block-chat"
REDIS_CHAT_WORK_BLOCK_OWNERS_PREFIX = f"{REDIS_KEY_PREFIX}:tasks:block-chat-owners"
REDIS_CHAT_SUPERSEDE_PREFIX = f"{REDIS_KEY_PREFIX}:tasks:generation:supersede"
REDIS_PENDING_TOOL_SELECTION_PREFIX = f"{REDIS_KEY_PREFIX}:tasks:pending-tools"
REDIS_TOOL_SELECTION_REVISION_PREFIX = (
    f"{REDIS_KEY_PREFIX}:tasks:pending-tools-revision"
)

# Redis hashes/sets do not support per-member TTLs. Each task therefore owns a
# small expiring lease key refreshed by its worker. A SIGKILL/restart cannot run
# cleanup, but its lease expires and the next registry read prunes the orphaned
# hash/set members instead of reporting a ghost task forever.
TASK_LEASE_TTL_SECONDS = 180
TASK_LEASE_HEARTBEAT_SECONDS = 30
# Stop before the last lease can expire. Otherwise a worker that keeps running
# through a long Redis outage can overlap a replacement worker after Redis
# returns: the replacement sees no claim while the original still owns the
# provider stream. Short outages remain recoverable; losing the registry for
# this long is a correctness failure and therefore cancels the owner.
TASK_REGISTRY_FAILURE_CANCEL_SECONDS = 120
# Pending assembly has its own lightweight heartbeat. Keeping the operation
# lease aligned with the task lease means a killed worker self-heals in minutes,
# not the old fifteen-minute ghost window, without imposing a time limit on OCR
# or other expensive preprocessing.
GENERATION_OPERATION_TTL_SECONDS = TASK_LEASE_TTL_SECONDS
GENERATION_OPERATION_HEARTBEAT_SECONDS = TASK_LEASE_HEARTBEAT_SECONDS
GENERATION_CANCEL_TTL_SECONDS = 15 * 60


class GenerationCancelledError(asyncio.CancelledError):
    """Raised before a generation task opens its start gate."""


class ChatWorkBlockedError(RuntimeError):
    """Raised when task admission loses a race with chat deletion."""


def _task_lease_key(task_id: str) -> str:
    return f"{REDIS_TASK_LEASE_KEY_PREFIX}:{task_id}"


def _generation_operation_key(generation_id: str) -> str:
    return f"{REDIS_GENERATION_OPERATION_PREFIX}:{generation_id}"


def _item_generations_key(item_id: str) -> str:
    return f"{REDIS_ITEM_GENERATIONS_PREFIX}:{item_id}"


def _chat_turn_key(chat_id: str) -> str:
    return f"{REDIS_CHAT_TURN_PREFIX}:{chat_id}"


def _generation_cancel_identity(chat_id: str, generation_id: str) -> str:
    chat_id = str(chat_id or "")
    generation_id = str(generation_id or "")
    return f"{len(chat_id)}:{chat_id}{generation_id}"


def _generation_cancel_key(chat_id: str, generation_id: str) -> str:
    return (
        f"{REDIS_GENERATION_CANCEL_PREFIX}:"
        f"{_generation_cancel_identity(chat_id, generation_id)}"
    )


def _generation_turn_cancel_identity(chat_id: str, turn_id: str) -> str:
    chat_id = str(chat_id or "")
    turn_id = str(turn_id or "")
    return f"{len(chat_id)}:{chat_id}{turn_id}"


def _generation_turn_cancel_key(chat_id: str, turn_id: str) -> str:
    return (
        f"{REDIS_GENERATION_TURN_CANCEL_PREFIX}:"
        f"{_generation_turn_cancel_identity(chat_id, turn_id)}"
    )


def _chat_work_block_key(chat_id: str) -> str:
    return f"{REDIS_CHAT_WORK_BLOCK_PREFIX}:{chat_id}"


def _chat_work_block_owners_key(chat_id: str) -> str:
    return f"{REDIS_CHAT_WORK_BLOCK_OWNERS_PREFIX}:{chat_id}"


def _chat_supersede_key(chat_id: str) -> str:
    return f"{REDIS_CHAT_SUPERSEDE_PREFIX}:{chat_id}"


def _pending_tool_selection_key(task_id: str) -> str:
    # Both live-selection keys share a Redis Cluster hash tag so the atomic
    # compare-and-set Lua script remains valid on clustered deployments.
    return f"{REDIS_PENDING_TOOL_SELECTION_PREFIX}:{{{task_id}}}"


def _tool_selection_revision_key(task_id: str) -> str:
    return f"{REDIS_TOOL_SELECTION_REVISION_PREFIX}:{{{task_id}}}"


def _normalize_generation_operation(
    operation: Mapping[str, object],
) -> GenerationOperation:
    return {
        "generation_id": str(operation.get("generation_id") or ""),
        "chat_id": str(operation.get("chat_id") or ""),
        "message_id": str(operation.get("message_id") or ""),
        "turn_id": str(operation.get("turn_id") or ""),
        "task_id": str(operation.get("task_id") or ""),
    }


def _generation_operation_json(operation: Mapping[str, object]) -> str:
    return json.dumps(
        _normalize_generation_operation(operation),
        sort_keys=True,
        separators=(",", ":"),
    )


def _same_generation_operation_identity(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> bool:
    left = _normalize_generation_operation(left)
    right = _normalize_generation_operation(right)
    return all(
        left[field] == right[field]
        for field in ("generation_id", "chat_id", "message_id", "turn_id")
    )


def _operation_is_live(operation: Mapping[str, object]) -> bool:
    """Is this in-memory operation still backed by something that can run?

    The Redis registry answers this with a TTL that a heartbeat refreshes, so a
    generation whose owner died stops holding anything once the lease lapses.
    The in-memory registry has no TTL and no heartbeat, so it needs an equally
    authoritative signal — and locally we have a better one than a clock: the
    owning asyncio task itself.

    A bound operation is live exactly while its task is alive. An operation with
    no ``task_id`` yet is in the pre-bind window still owned by the HTTP request,
    which releases it in its own ``finally``; that window is bounded, so it
    counts as live.
    """
    task_id = str(operation.get("task_id") or "")
    if not task_id:
        return True
    task = tasks.get(task_id)
    return task is not None and not task.done()


def _prune_dead_generation_operations() -> None:
    """Drop operations whose owning task is gone.

    This is what makes a missed ``unregister_generation_operation`` survivable
    rather than terminal. It is the local equivalent of the Redis lease simply
    expiring, and it means no bookkeeping slip can wedge a chat forever.
    """
    for generation_id, operation in list(generation_operations.items()):
        if _operation_is_live(operation):
            continue
        log.debug(
            "pruning generation operation %s (owning task %s is gone)",
            generation_id,
            operation.get("task_id"),
        )
        generation_operations.pop(generation_id, None)
        chat_id = str(operation.get("chat_id") or "")
        item_ids = item_generation_operations.get(chat_id)
        if item_ids is not None:
            item_ids.discard(generation_id)
            if not item_ids:
                item_generation_operations.pop(chat_id, None)


def _active_chat_turn(chat_id: str) -> Optional[str]:
    """The turn this chat is currently committed to, derived from live work.

    Sibling responses for one user turn share a ``turn_id``, so any live
    operation answers the question. Returns None when nothing is running, which
    is what lets the next turn in.
    """
    _prune_dead_generation_operations()
    for generation_id in item_generation_operations.get(chat_id, set()):
        operation = generation_operations.get(generation_id)
        if operation is None:
            continue
        turn_id = str(operation.get("turn_id") or "")
        if turn_id:
            return turn_id
    return None


def _generation_cancel_latched(chat_id: str, generation_id: str, turn_id: str) -> bool:
    """The 'this run has been cancelled' predicate, in ONE place.

    The Redis path states it as three EXISTS checks inside its Lua scripts. It
    used to be restated in Python for the in-memory path — and the two drifted:
    the in-memory ``bind_generation_operation_task`` omitted it entirely, so with
    Redis unconfigured a generation the user had already stopped would bind
    successfully. Both in-memory callers now share this one definition, so they
    cannot disagree again.
    """
    _prune_local_cancel_intents()
    return (
        _generation_cancel_identity(chat_id, generation_id) in generation_cancel_intents
        or _generation_turn_cancel_identity(chat_id, turn_id)
        in generation_turn_cancel_intents
        or chat_id in chat_work_blocks
    )


def _prune_local_cancel_intents() -> None:
    now = time.monotonic()
    for identity, expires_at in list(generation_cancel_intents.items()):
        if expires_at <= now:
            generation_cancel_intents.pop(identity, None)
    for identity, expires_at in list(generation_turn_cancel_intents.items()):
        if expires_at <= now:
            generation_turn_cancel_intents.pop(identity, None)
    for chat_id, owners in list(chat_work_blocks.items()):
        for token, expires_at in list(owners.items()):
            if expires_at <= now:
                owners.pop(token, None)
        if not owners:
            chat_work_blocks.pop(chat_id, None)
    for chat_id, transition in list(generation_supersede_transitions.items()):
        if transition["expires_at"] <= now:
            generation_supersede_transitions.pop(chat_id, None)


async def is_generation_cancelled(redis, chat_id: str, generation_id: str) -> bool:
    chat_id = str(chat_id or "")
    generation_id = str(generation_id or "")
    if not chat_id or not generation_id:
        return False
    identity = _generation_cancel_identity(chat_id, generation_id)
    if redis:
        return bool(await redis.exists(_generation_cancel_key(chat_id, generation_id)))
    _prune_local_cancel_intents()
    return identity in generation_cancel_intents


async def mark_generation_cancelled(redis, chat_id: str, generation_id: str) -> None:
    """Latch cancellation before discovering/stopping any registered task.

    Registration checks this latch immediately before opening its task start
    gate. If registration wins first, the task is already visible to the Stop
    operation. Those two orderings close the old pre-registration gap.
    """
    chat_id = str(chat_id or "")
    generation_id = str(generation_id or "")
    if not chat_id or not generation_id:
        return
    identity = _generation_cancel_identity(chat_id, generation_id)
    if redis:
        await redis.set(
            _generation_cancel_key(chat_id, generation_id),
            "1",
            ex=GENERATION_CANCEL_TTL_SECONDS,
        )
        return
    _prune_local_cancel_intents()
    generation_cancel_intents[identity] = (
        time.monotonic() + GENERATION_CANCEL_TTL_SECONDS
    )


async def is_generation_turn_cancelled(redis, chat_id: str, turn_id: str) -> bool:
    chat_id = str(chat_id or "")
    turn_id = str(turn_id or "")
    if not chat_id or not turn_id:
        return False
    identity = _generation_turn_cancel_identity(chat_id, turn_id)
    if redis:
        return bool(await redis.exists(_generation_turn_cancel_key(chat_id, turn_id)))
    _prune_local_cancel_intents()
    return identity in generation_turn_cancel_intents


async def mark_generation_turn_cancelled(redis, chat_id: str, turn_id: str) -> None:
    """Cancel every present or future sibling belonging to one user turn."""
    chat_id = str(chat_id or "")
    turn_id = str(turn_id or "")
    if not chat_id or not turn_id:
        return
    identity = _generation_turn_cancel_identity(chat_id, turn_id)
    if redis:
        await redis.set(
            _generation_turn_cancel_key(chat_id, turn_id),
            "1",
            ex=GENERATION_CANCEL_TTL_SECONDS,
        )
        return
    _prune_local_cancel_intents()
    generation_turn_cancel_intents[identity] = (
        time.monotonic() + GENERATION_CANCEL_TTL_SECONDS
    )


async def is_chat_work_blocked(redis, chat_id: str) -> bool:
    chat_id = str(chat_id or "")
    if not chat_id:
        return False
    if redis:
        return bool(await redis.exists(_chat_work_block_key(chat_id)))
    _prune_local_cancel_intents()
    return chat_id in chat_work_blocks


async def acquire_chat_work_block(redis, chat_id: str) -> str:
    """Prevent new chat work while one destructive operation owns a barrier.

    Each caller receives an ownership token. A failed delete can therefore
    release only its own barrier without reopening the chat underneath another
    concurrent delete attempt.
    """
    chat_id = str(chat_id or "")
    if not chat_id:
        return ""
    token = str(uuid4())
    if redis:
        await redis.eval(
            """
            redis.call('sadd', KEYS[1], ARGV[1])
            redis.call('expire', KEYS[1], ARGV[2])
            redis.call('set', KEYS[2], '1', 'EX', ARGV[2])
            return 1
            """,
            2,
            _chat_work_block_owners_key(chat_id),
            _chat_work_block_key(chat_id),
            token,
            str(GENERATION_CANCEL_TTL_SECONDS),
        )
        return token
    _prune_local_cancel_intents()
    chat_work_blocks.setdefault(chat_id, {})[token] = (
        time.monotonic() + GENERATION_CANCEL_TTL_SECONDS
    )
    return token


async def release_chat_work_block(redis, chat_id: str, token: str) -> None:
    """Release exactly one delete-attempt admission barrier."""
    chat_id = str(chat_id or "")
    token = str(token or "")
    if not chat_id or not token:
        return
    if redis:
        await redis.eval(
            """
            redis.call('srem', KEYS[1], ARGV[1])
            if redis.call('scard', KEYS[1]) == 0 then
                redis.call('del', KEYS[1])
                redis.call('del', KEYS[2])
            else
                redis.call('expire', KEYS[1], ARGV[2])
                redis.call('expire', KEYS[2], ARGV[2])
            end
            return 1
            """,
            2,
            _chat_work_block_owners_key(chat_id),
            _chat_work_block_key(chat_id),
            token,
            str(GENERATION_CANCEL_TTL_SECONDS),
        )
        return
    _prune_local_cancel_intents()
    owners = chat_work_blocks.get(chat_id)
    if owners is None:
        return
    owners.pop(token, None)
    if not owners:
        chat_work_blocks.pop(chat_id, None)


GenerationRegistrationResult = Literal[
    "acquired", "duplicate", "cancelled", "turn_conflict", "id_conflict"
]


class GenerationSupersedeResult(TypedDict):
    registration: GenerationRegistrationResult
    displaced: List[GenerationOperation]


async def register_generation_operation(
    redis,
    operation: Mapping[str, object],
) -> GenerationRegistrationResult:
    """Register a generation before any expensive request preprocessing.

    The chat-turn lease permits sibling model responses for the same user turn,
    while rejecting a genuinely different concurrent turn. With Redis the
    cancellation check, turn claim, operation write, and item index update are
    one Lua transaction.
    """
    operation = _normalize_generation_operation(operation)
    generation_id = operation["generation_id"]
    chat_id = operation["chat_id"]
    message_id = operation["message_id"]
    turn_id = operation["turn_id"]
    if not generation_id or not chat_id or not message_id or not turn_id:
        raise ValueError("generation_id, chat_id, message_id, and turn_id are required")

    encoded = _generation_operation_json(operation)
    if redis:
        result = await redis.eval(
            """
            if redis.call('exists', KEYS[4]) == 1
                or redis.call('exists', KEYS[5]) == 1
                or redis.call('exists', KEYS[6]) == 1 then
                return 'cancelled'
            end
            local existing = redis.call('get', KEYS[1])
            if existing then
                local decoded = cjson.decode(existing)
                if decoded['generation_id'] == ARGV[4]
                    and decoded['chat_id'] == ARGV[5]
                    and decoded['message_id'] == ARGV[6]
                    and decoded['turn_id'] == ARGV[2] then
                    redis.call('set', KEYS[3], ARGV[2], 'EX', ARGV[3])
                    redis.call('expire', KEYS[1], ARGV[3])
                    redis.call('sadd', KEYS[2], ARGV[4])
                    redis.call('expire', KEYS[2], ARGV[3])
                    return 'duplicate'
                end
                return 'id_conflict'
            end
            local active_turn = redis.call('get', KEYS[3])
            if active_turn and active_turn ~= ARGV[2] then
                return 'turn_conflict'
            end
            redis.call('set', KEYS[3], ARGV[2], 'EX', ARGV[3])
            redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[3])
            redis.call('sadd', KEYS[2], ARGV[4])
            redis.call('expire', KEYS[2], ARGV[3])
            return 'acquired'
            """,
            6,
            _generation_operation_key(generation_id),
            _item_generations_key(chat_id),
            _chat_turn_key(chat_id),
            _generation_cancel_key(chat_id, generation_id),
            _generation_turn_cancel_key(chat_id, turn_id),
            _chat_work_block_key(chat_id),
            encoded,
            turn_id,
            str(GENERATION_OPERATION_TTL_SECONDS),
            generation_id,
            chat_id,
            operation["message_id"],
        )
        return _redis_text(result)  # type: ignore[return-value]

    if _generation_cancel_latched(chat_id, generation_id, turn_id):
        return "cancelled"
    existing = generation_operations.get(generation_id)
    if existing is not None:
        return (
            "duplicate"
            if _same_generation_operation_identity(existing, operation)
            else "id_conflict"
        )
    active_turn = _active_chat_turn(chat_id)
    if active_turn is not None and active_turn != turn_id:
        return "turn_conflict"
    generation_operations[generation_id] = operation
    item_generation_operations.setdefault(chat_id, set()).add(generation_id)
    return "acquired"


async def supersede_generation_operation(
    redis,
    operation: Mapping[str, object],
) -> GenerationSupersedeResult:
    """Claim a replacement turn and cancel the turn it displaces atomically.

    A regenerate/redo is not a second independent send. It transfers the chat's
    generation lease to a fresh turn. The transfer, old-turn cancellation
    latches, replacement operation, and per-chat index update happen in one
    registry transaction, so there is no Stop-then-POST gap for another tab to
    race through. The caller receives the displaced operations so it can cancel
    and await their concrete tasks before starting provider work for the new run.
    """
    operation = _normalize_generation_operation(operation)
    generation_id = operation["generation_id"]
    chat_id = operation["chat_id"]
    message_id = operation["message_id"]
    turn_id = operation["turn_id"]
    if not generation_id or not chat_id or not message_id or not turn_id:
        raise ValueError("generation_id, chat_id, message_id, and turn_id are required")

    encoded = _generation_operation_json(operation)
    if redis:
        values = await redis.eval(
            """
            if redis.call('exists', KEYS[4]) == 1
                or redis.call('exists', KEYS[5]) == 1
                or redis.call('exists', KEYS[6]) == 1 then
                return {'cancelled'}
            end

            local existing = redis.call('get', KEYS[1])
            if existing then
                local decoded = cjson.decode(existing)
                if decoded['generation_id'] == ARGV[4]
                    and decoded['chat_id'] == ARGV[5]
                    and decoded['message_id'] == ARGV[6]
                    and decoded['turn_id'] == ARGV[2] then
                    redis.call('set', KEYS[3], ARGV[2], 'EX', ARGV[3])
                    redis.call('expire', KEYS[1], ARGV[3])
                    redis.call('sadd', KEYS[2], ARGV[4])
                    redis.call('expire', KEYS[2], ARGV[3])
                    return {'duplicate'}
                end
                return {'id_conflict'}
            end

            local result = {'acquired'}
            local displaced_operations = {}
            local active_turn = redis.call('get', KEYS[3])
            if active_turn and active_turn ~= ARGV[2] then
                local generation_ids = redis.call('smembers', KEYS[2])
                for _, old_generation_id in ipairs(generation_ids) do
                    local old_encoded = redis.call('get', ARGV[7] .. old_generation_id)
                    if old_encoded then
                        local old_operation = cjson.decode(old_encoded)
                        if old_operation['chat_id'] == ARGV[5]
                            and old_operation['turn_id'] == active_turn then
                            redis.call(
                                'set', ARGV[8] .. old_generation_id,
                                '1', 'EX', ARGV[10]
                            )
                            redis.call(
                                'set', ARGV[9] .. old_operation['turn_id'],
                                '1', 'EX', ARGV[10]
                            )
                            redis.call('srem', KEYS[2], old_generation_id)
                            table.insert(displaced_operations, old_operation)
                            table.insert(result, old_encoded)
                        end
                    else
                        redis.call('srem', KEYS[2], old_generation_id)
                    end
                end
                if #displaced_operations > 0 then
                    redis.call(
                        'set', KEYS[7], cjson.encode({
                            turn_id = ARGV[2],
                            displaced = displaced_operations
                        }), 'EX', ARGV[3]
                    )
                end
            elseif active_turn == ARGV[2] then
                local transition_encoded = redis.call('get', KEYS[7])
                if transition_encoded then
                    local transition = cjson.decode(transition_encoded)
                    if transition['turn_id'] == ARGV[2] then
                        for _, old_operation in ipairs(transition['displaced'] or {}) do
                            table.insert(result, cjson.encode(old_operation))
                        end
                    end
                end
            end

            redis.call('set', KEYS[3], ARGV[2], 'EX', ARGV[3])
            redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[3])
            redis.call('sadd', KEYS[2], ARGV[4])
            redis.call('expire', KEYS[2], ARGV[3])
            return result
            """,
            7,
            _generation_operation_key(generation_id),
            _item_generations_key(chat_id),
            _chat_turn_key(chat_id),
            _generation_cancel_key(chat_id, generation_id),
            _generation_turn_cancel_key(chat_id, turn_id),
            _chat_work_block_key(chat_id),
            _chat_supersede_key(chat_id),
            encoded,
            turn_id,
            str(GENERATION_OPERATION_TTL_SECONDS),
            generation_id,
            chat_id,
            message_id,
            f"{REDIS_GENERATION_OPERATION_PREFIX}:",
            (f"{REDIS_GENERATION_CANCEL_PREFIX}:" f"{len(chat_id)}:{chat_id}"),
            (f"{REDIS_GENERATION_TURN_CANCEL_PREFIX}:" f"{len(chat_id)}:{chat_id}"),
            str(GENERATION_CANCEL_TTL_SECONDS),
        )
        decoded_values = [_redis_text(value) for value in (values or [])]
        registration = decoded_values[0] if decoded_values else "id_conflict"
        displaced: List[GenerationOperation] = []
        for value in decoded_values[1:]:
            try:
                displaced.append(_normalize_generation_operation(json.loads(value)))
            except (TypeError, ValueError, json.JSONDecodeError):
                log.exception("invalid displaced generation operation in Redis")
        return {
            "registration": registration,  # type: ignore[typeddict-item]
            "displaced": displaced,
        }

    if _generation_cancel_latched(chat_id, generation_id, turn_id):
        return {"registration": "cancelled", "displaced": []}
    existing = generation_operations.get(generation_id)
    if existing is not None:
        return {
            "registration": (
                "duplicate"
                if _same_generation_operation_identity(existing, operation)
                else "id_conflict"
            ),
            "displaced": [],
        }

    active_turn = _active_chat_turn(chat_id)
    displaced: List[GenerationOperation] = []
    if active_turn is not None and active_turn != turn_id:
        expires_at = time.monotonic() + GENERATION_CANCEL_TTL_SECONDS
        item_ids = item_generation_operations.get(chat_id, set())
        for old_generation_id in list(item_ids):
            old_operation = generation_operations.get(old_generation_id)
            if old_operation is None:
                item_ids.discard(old_generation_id)
                continue
            if old_operation["turn_id"] != active_turn:
                continue
            displaced.append(dict(old_operation))
            generation_cancel_intents[
                _generation_cancel_identity(chat_id, old_generation_id)
            ] = expires_at
            generation_turn_cancel_intents[
                _generation_turn_cancel_identity(chat_id, active_turn)
            ] = expires_at
            # Transfer the per-chat lease index immediately. The old operation
            # remains addressable by generation id until its own teardown, but it
            # can no longer make the displaced turn look active.
            item_ids.discard(old_generation_id)
        if displaced:
            generation_supersede_transitions[chat_id] = {
                "turn_id": turn_id,
                "displaced": [dict(item) for item in displaced],
                "expires_at": (time.monotonic() + GENERATION_OPERATION_TTL_SECONDS),
            }
    elif active_turn == turn_id:
        transition = generation_supersede_transitions.get(chat_id)
        if transition is not None and transition["turn_id"] == turn_id:
            displaced = [dict(item) for item in transition["displaced"]]

    generation_operations[generation_id] = operation
    item_generation_operations.setdefault(chat_id, set()).add(generation_id)
    return {"registration": "acquired", "displaced": displaced}


async def finish_generation_supersede(redis, chat_id: str, turn_id: str) -> None:
    """Release a replacement-turn teardown barrier after old tasks are gone."""
    chat_id = str(chat_id or "")
    turn_id = str(turn_id or "")
    if not chat_id or not turn_id:
        return
    if redis:
        await redis.eval(
            """
            local transition_encoded = redis.call('get', KEYS[1])
            if not transition_encoded then return 0 end
            local transition = cjson.decode(transition_encoded)
            if transition['turn_id'] ~= ARGV[1] then return 0 end
            redis.call('del', KEYS[1])
            return 1
            """,
            1,
            _chat_supersede_key(chat_id),
            turn_id,
        )
        return
    _prune_local_cancel_intents()
    transition = generation_supersede_transitions.get(chat_id)
    if transition is not None and transition["turn_id"] == turn_id:
        generation_supersede_transitions.pop(chat_id, None)


async def get_generation_operation(
    redis, generation_id: str
) -> Optional[GenerationOperation]:
    generation_id = str(generation_id or "")
    if not generation_id:
        return None
    if redis:
        value = await redis.get(_generation_operation_key(generation_id))
        if value is None:
            return None
        try:
            return _normalize_generation_operation(json.loads(_redis_text(value)))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    operation = generation_operations.get(generation_id)
    return dict(operation) if operation is not None else None


async def list_generation_operations_by_item(
    redis, item_id: str
) -> List[GenerationOperation]:
    item_id = str(item_id or "")
    if not item_id:
        return []
    if redis:
        item_key = _item_generations_key(item_id)
        generation_ids = [_redis_text(v) for v in await redis.smembers(item_key)]
        if not generation_ids:
            return []
        pipe = redis.pipeline()
        for generation_id in generation_ids:
            pipe.get(_generation_operation_key(generation_id))
        values = await pipe.execute()
        out: List[GenerationOperation] = []
        stale: List[str] = []
        for generation_id, value in zip(generation_ids, values):
            if value is None:
                stale.append(generation_id)
                continue
            try:
                out.append(
                    _normalize_generation_operation(json.loads(_redis_text(value)))
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                stale.append(generation_id)
        if stale:
            await redis.srem(item_key, *stale)
        return out
    return [
        dict(generation_operations[generation_id])
        for generation_id in item_generation_operations.get(item_id, set())
        if generation_id in generation_operations
    ]


async def has_active_generation_operations(redis, item_id: str) -> bool:
    return bool(await list_generation_operations_by_item(redis, item_id))


async def list_generation_task_ids_by_item(redis, item_id: str) -> List[str]:
    """Return task ids owned by active generation operations for one chat."""
    return list(
        dict.fromkeys(
            operation["task_id"]
            for operation in await list_generation_operations_by_item(redis, item_id)
            if operation.get("task_id")
        )
    )


async def latch_generation_cancellation(
    redis,
    chat_id: str,
    *,
    generation_ids: Iterable[str] = (),
    turn_ids: Iterable[str] = (),
) -> List[GenerationOperation]:
    """Durably cancel selected runs, then return the closed operation set.

    Turn intent is written before the operation snapshot so a late sibling
    either registers first and appears in the snapshot or observes the latch
    and is rejected. Every discovered sibling receives its own generation
    latch as well.
    """
    chat_id = str(chat_id or "")
    wanted_generations = {
        str(generation_id)
        for generation_id in generation_ids
        if str(generation_id or "")
    }
    wanted_turns = {str(turn_id) for turn_id in turn_ids if str(turn_id or "")}
    if not chat_id or (not wanted_generations and not wanted_turns):
        return []

    for turn_id in wanted_turns:
        await mark_generation_turn_cancelled(redis, chat_id, turn_id)
    for generation_id in wanted_generations:
        await mark_generation_cancelled(redis, chat_id, generation_id)

    matched: List[GenerationOperation] = []
    for operation in await list_generation_operations_by_item(redis, chat_id):
        if (
            operation["generation_id"] not in wanted_generations
            and operation["turn_id"] not in wanted_turns
        ):
            continue
        matched.append(operation)
        if operation["generation_id"] not in wanted_generations:
            wanted_generations.add(operation["generation_id"])
            await mark_generation_cancelled(redis, chat_id, operation["generation_id"])
    return matched


async def bind_generation_operation_task(
    redis,
    operation: Mapping[str, object],
    task_id: str,
) -> bool:
    operation = _normalize_generation_operation({**operation, "task_id": task_id})
    generation_id = operation["generation_id"]
    chat_id = operation["chat_id"]
    turn_id = operation["turn_id"]
    if redis:
        key = _generation_operation_key(generation_id)
        current = await redis.get(key)
        if current is None:
            return False
        current_operation = _normalize_generation_operation(
            json.loads(_redis_text(current))
        )
        if any(
            current_operation[field] != operation[field]
            for field in ("generation_id", "chat_id", "message_id", "turn_id")
        ):
            return False
        if current_operation["task_id"] and (current_operation["task_id"] != task_id):
            return False
        result = await redis.eval(
            """
            if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
            if redis.call('exists', KEYS[4]) == 1
                or redis.call('exists', KEYS[5]) == 1
                or redis.call('exists', KEYS[6]) == 1 then
                return 0
            end
            local active_turn = redis.call('get', KEYS[3])
            if active_turn and active_turn ~= ARGV[3] then return 0 end
            redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[4])
            redis.call('sadd', KEYS[2], ARGV[5])
            redis.call('expire', KEYS[2], ARGV[4])
            redis.call('set', KEYS[3], ARGV[3], 'EX', ARGV[4])
            return 1
            """,
            6,
            key,
            _item_generations_key(chat_id),
            _chat_turn_key(chat_id),
            _generation_cancel_key(chat_id, generation_id),
            _generation_turn_cancel_key(chat_id, turn_id),
            _chat_work_block_key(chat_id),
            _generation_operation_json(current_operation),
            _generation_operation_json(operation),
            turn_id,
            str(GENERATION_OPERATION_TTL_SECONDS),
            generation_id,
        )
        return bool(result)
    current = generation_operations.get(generation_id)
    if current is None:
        return False
    if any(
        str(current.get(field) or "") != operation[field]
        for field in ("generation_id", "chat_id", "message_id", "turn_id")
    ):
        return False
    if current.get("task_id") and str(current["task_id"]) != task_id:
        return False
    # Same three cancellation checks the Redis Lua makes before it binds. Their
    # absence here was a real divergence: without Redis, a generation the user
    # had already stopped bound successfully and only tripped the caller's
    # separate re-check afterwards.
    if _generation_cancel_latched(chat_id, generation_id, turn_id):
        return False
    generation_operations[generation_id] = operation
    return True


async def refresh_generation_operation(redis, operation: Mapping[str, object]) -> bool:
    operation = _normalize_generation_operation(operation)
    generation_id = operation["generation_id"]
    chat_id = operation["chat_id"]
    turn_id = operation["turn_id"]
    if redis:
        result = await redis.eval(
            """
            if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
            if redis.call('exists', KEYS[4]) == 1
                or redis.call('exists', KEYS[5]) == 1
                or redis.call('exists', KEYS[6]) == 1 then
                return 0
            end
            local active_turn = redis.call('get', KEYS[3])
            if active_turn and active_turn ~= ARGV[2] then return 0 end
            redis.call('expire', KEYS[1], ARGV[3])
            redis.call('sadd', KEYS[2], ARGV[4])
            redis.call('expire', KEYS[2], ARGV[3])
            redis.call('set', KEYS[3], ARGV[2], 'EX', ARGV[3])
            return 1
            """,
            6,
            _generation_operation_key(generation_id),
            _item_generations_key(chat_id),
            _chat_turn_key(chat_id),
            _generation_cancel_key(chat_id, generation_id),
            _generation_turn_cancel_key(chat_id, turn_id),
            _chat_work_block_key(chat_id),
            _generation_operation_json(operation),
            turn_id,
            str(GENERATION_OPERATION_TTL_SECONDS),
            generation_id,
        )
        return bool(result)
    return (
        generation_operations.get(generation_id) == operation
        and not await is_generation_cancelled(redis, chat_id, generation_id)
        and not await is_generation_turn_cancelled(redis, chat_id, turn_id)
        and not await is_chat_work_blocked(redis, chat_id)
    )


async def heartbeat_generation_operation_until_bound(
    redis, operation: Mapping[str, object]
) -> None:
    """Refresh a pre-task operation while request preprocessing is in flight.

    Once ``create_task`` binds a task id, the task lease heartbeat becomes the
    sole owner and this coroutine exits. A cancellation latch or deleted/lost
    operation also ends the heartbeat; the task start gate performs the final
    authoritative check before any provider work can begin.
    """
    if not redis:
        return
    while not str(operation.get("task_id") or ""):
        await asyncio.sleep(GENERATION_OPERATION_HEARTBEAT_SECONDS)
        if str(operation.get("task_id") or ""):
            return
        try:
            if not await refresh_generation_operation(redis, operation):
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            # A transient Redis outage is handled by the eventual bind check.
            # Do not keep a second failure timer here; the operation lease itself
            # is the fail-closed deadline for pre-task work.
            log.exception(
                "generation operation preflight heartbeat failed for %s",
                operation.get("generation_id"),
            )


async def unregister_generation_operation(
    redis, operation: Mapping[str, object]
) -> None:
    operation = _normalize_generation_operation(operation)
    generation_id = operation["generation_id"]
    chat_id = operation["chat_id"]
    turn_id = operation["turn_id"]
    if not generation_id or not chat_id:
        return
    if redis:
        await redis.eval(
            """
            local existing = redis.call('get', KEYS[1])
            if not existing then return 0 end
            local decoded = cjson.decode(existing)
            if decoded['generation_id'] ~= ARGV[1]
                or decoded['chat_id'] ~= ARGV[2]
                or decoded['message_id'] ~= ARGV[3]
                or decoded['turn_id'] ~= ARGV[4] then
                return 0
            end
            if decoded['task_id'] ~= ARGV[6] then
                return 0
            end
            redis.call('del', KEYS[1])
            redis.call('srem', KEYS[2], ARGV[1])
            if redis.call('scard', KEYS[2]) == 0 then
                redis.call('del', KEYS[2])
                if redis.call('get', KEYS[3]) == ARGV[4] then
                    redis.call('del', KEYS[3])
                end
            else
                redis.call('expire', KEYS[2], ARGV[5])
            end
            return 1
            """,
            3,
            _generation_operation_key(generation_id),
            _item_generations_key(chat_id),
            _chat_turn_key(chat_id),
            generation_id,
            chat_id,
            operation["message_id"],
            turn_id,
            str(GENERATION_OPERATION_TTL_SECONDS),
            operation["task_id"],
        )
        return
    current = generation_operations.get(generation_id)
    if current is None or not _same_generation_operation_identity(current, operation):
        return
    if str(current.get("task_id") or "") != operation["task_id"]:
        # Ownership check: a stale caller must not evict an operation that has
        # since been rebound to a different task. It stays SILENT-ish by design,
        # but it is logged now — this guard used to be able to strand a chat and
        # left nothing behind to explain it. It can no longer wedge anything:
        # `_prune_dead_generation_operations` reaps the entry once its task ends,
        # and the chat's active turn is derived rather than stored.
        log.debug(
            "skipping unregister of generation %s: owned by task %r, caller had %r",
            generation_id,
            current.get("task_id"),
            operation["task_id"],
        )
        return
    generation_operations.pop(generation_id, None)
    item_ids = item_generation_operations.get(chat_id)
    if item_ids is not None:
        item_ids.discard(generation_id)
        if not item_ids:
            item_generation_operations.pop(chat_id, None)


async def redis_task_command_listener(app):
    redis: Redis = app.state.redis
    pubsub = redis.pubsub()
    await pubsub.subscribe(REDIS_PUBSUB_CHANNEL)

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            command = json.loads(message["data"])
            if command.get("action") == "stop":
                task_id = command.get("task_id")
                local_task = tasks.get(task_id)
                # Idempotent: a repeated Stop (two clicks) or delete-chat firing
                # multiple stop commands publishes several 'stop's. Re-cancelling a
                # task already cancelling delivers a SECOND CancelledError into the
                # task's teardown await, truncating its terminal cleanup (sweep /
                # done-write / cancel emit) and stranding subagent cards 'running'.
                # Only cancel a task that is live and not already cancelling.
                if (
                    local_task is not None
                    and not local_task.done()
                    and local_task.cancelling() == 0
                ):
                    local_task.cancel()
        except Exception as e:
            log.exception(f"Error handling distributed task command: {e}")


### ------------------------------
### REDIS-ENABLED HANDLERS
### ------------------------------


async def redis_save_task(redis: Redis, task_id: str, item_id: Optional[str]):
    pipe = redis.pipeline()
    pipe.hset(REDIS_TASKS_KEY, task_id, item_id or "")
    if item_id:
        pipe.sadd(f"{REDIS_ITEM_TASKS_KEY}:{item_id}", task_id)
    pipe.set(_task_lease_key(task_id), "1", ex=TASK_LEASE_TTL_SECONDS)
    await pipe.execute()


async def redis_register_task(
    redis: Redis,
    task_id: str,
    item_id: Optional[str],
    *,
    admission_chat_id: Optional[str] = None,
) -> bool:
    """Register a task, atomically honoring an optional chat-work barrier."""
    admission_chat_id = str(admission_chat_id or "")
    if not admission_chat_id:
        await redis_save_task(redis, task_id, item_id)
        return True
    return bool(
        await redis.eval(
            """
            if redis.call('exists', KEYS[4]) == 1 then return 0 end
            redis.call('hset', KEYS[1], ARGV[1], ARGV[2])
            if ARGV[2] ~= '' then
                redis.call('sadd', KEYS[2], ARGV[1])
            end
            redis.call('set', KEYS[3], '1', 'EX', ARGV[3])
            return 1
            """,
            4,
            REDIS_TASKS_KEY,
            f"{REDIS_ITEM_TASKS_KEY}:{item_id}",
            _task_lease_key(task_id),
            _chat_work_block_key(admission_chat_id),
            task_id,
            item_id or "",
            str(TASK_LEASE_TTL_SECONDS),
        )
    )


async def redis_cleanup_task(redis: Redis, task_id: str, item_id: Optional[str]):
    pipe = redis.pipeline()
    pipe.hdel(REDIS_TASKS_KEY, task_id)
    if item_id:
        pipe.srem(f"{REDIS_ITEM_TASKS_KEY}:{item_id}", task_id)
    pipe.delete(_task_lease_key(task_id))
    pipe.delete(_pending_tool_selection_key(task_id))
    pipe.delete(_tool_selection_revision_key(task_id))
    await pipe.execute()
    if item_id and await redis.scard(f"{REDIS_ITEM_TASKS_KEY}:{item_id}") == 0:
        await redis.delete(f"{REDIS_ITEM_TASKS_KEY}:{item_id}")


def _redis_text(value) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


async def _redis_filter_live_task_ids(redis: Redis, task_ids: List[str]) -> List[str]:
    """Filter task ids by lease and remove expired registry debris.

    Cleanup resolves each stale task's item key from the global hash, so this
    repairs both data structures even when called from a global listing rather
    than from one known item set.
    """
    normalized = list(
        dict.fromkeys(
            _redis_text(task_id)
            for task_id in (task_ids or [])
            if task_id is not None and _redis_text(task_id)
        )
    )
    if not normalized:
        return []

    # Pipeline individual EXISTS calls instead of MGET: lease keys do not share
    # a Redis Cluster hash slot, while a cluster-aware pipeline can route them.
    lease_pipe = redis.pipeline()
    for task_id in normalized:
        lease_pipe.exists(_task_lease_key(task_id))
    lease_values = await lease_pipe.execute()
    live = [task_id for task_id, lease in zip(normalized, lease_values) if bool(lease)]
    stale = [
        task_id for task_id, lease in zip(normalized, lease_values) if not bool(lease)
    ]
    if stale:
        item_ids = await redis.hmget(REDIS_TASKS_KEY, stale)
        pipe = redis.pipeline()
        for task_id, item_id in zip(stale, item_ids):
            pipe.hdel(REDIS_TASKS_KEY, task_id)
            if item_id:
                pipe.srem(
                    f"{REDIS_ITEM_TASKS_KEY}:{_redis_text(item_id)}",
                    task_id,
                )
            pipe.delete(_task_lease_key(task_id))
            pipe.delete(_pending_tool_selection_key(task_id))
            pipe.delete(_tool_selection_revision_key(task_id))
        await pipe.execute()
    return live


async def redis_list_item_tasks(redis: Redis, item_id: str) -> List[str]:
    return await _redis_filter_live_task_ids(
        redis,
        list(await redis.smembers(f"{REDIS_ITEM_TASKS_KEY}:{item_id}")),
    )


async def redis_send_command(redis: Redis, command: dict):
    await redis.publish(REDIS_PUBSUB_CHANNEL, json.dumps(command))


async def cleanup_task(
    redis,
    task_id: str,
    id=None,
    generation_operation: Optional[dict] = None,
):
    """
    Remove a completed or canceled task from the global `tasks` dictionary.
    """
    if redis:
        await redis_cleanup_task(redis, task_id, id)
        if generation_operation:
            try:
                await unregister_generation_operation(redis, generation_operation)
            except Exception:
                log.exception(
                    "generation operation cleanup failed for %s/%s",
                    generation_operation.get("generation_id"),
                    task_id,
                )
    elif generation_operation:
        await unregister_generation_operation(None, generation_operation)

    tasks.pop(task_id, None)  # Remove the task if it exists

    # Pending model / service_tier switches are keyed by task_id and otherwise only
    # removed by the agentic loop's clear_pending_* on its next iteration. A switch
    # requested then Stopped/finished before the loop iterates again would strand a
    # dict entry under a never-reused uuid for the process lifetime, so reap them
    # here to bound the entry's lifetime to the task's.
    pending_model_switches.pop(task_id, None)
    pending_service_tier_switches.pop(task_id, None)
    pending_tool_selections.pop(task_id, None)
    pending_tool_selection_revisions.pop(task_id, None)

    # If an ID is provided, remove the task from the item_tasks dictionary
    if id and task_id in item_tasks.get(id, []):
        item_tasks[id].remove(task_id)
        if not item_tasks[id]:  # If no tasks left for this ID, remove the entry
            item_tasks.pop(id, None)


async def get_task_item_id(redis, task_id: str) -> Optional[str]:
    """Return the ITEM id a task was registered under (the bare chat id for a chat
    generation, or ``subagent-rerun:{chat}:{entry}`` for a rerun), or ``None`` when
    the task is unknown. Used to authorize a stop request against the task's owner."""
    if redis:
        if not await redis.exists(_task_lease_key(task_id)):
            await _redis_filter_live_task_ids(redis, [task_id])
            return None
        val = await redis.hget(REDIS_TASKS_KEY, task_id)
        if val is None:
            return None
        return val.decode() if isinstance(val, (bytes, bytearray)) else val
    for item_id, ids in item_tasks.items():
        if task_id in (ids or []):
            return item_id
    return None


async def create_task(
    redis,
    coroutine,
    id=None,
    *,
    generation_operation: Optional[dict] = None,
    admission_chat_id: Optional[str] = None,
):
    """
    Create a task only after its registry ownership is visible.

    A Redis-backed task used to start running before ``redis_save_task``
    completed. A very fast rerun could finish and run cleanup first, then the
    delayed save re-added its task id as a permanent ghost. A start gate keeps
    the coroutine suspended until local + Redis registration has succeeded.
    """
    task_id = str(uuid4())  # Generate a unique ID for the task
    start_gate = asyncio.Event()
    bound_generation_operation = (
        _normalize_generation_operation(generation_operation)
        if generation_operation
        else None
    )

    async def _registered_runner():
        started = False
        lease_heartbeat = None
        try:
            await start_gate.wait()
            started = True
            if redis:
                owner_task = asyncio.current_task()
                last_registry_refresh = asyncio.get_running_loop().time()

                async def _heartbeat() -> None:
                    nonlocal last_registry_refresh
                    while True:
                        await asyncio.sleep(TASK_LEASE_HEARTBEAT_SECONDS)
                        try:
                            # Refresh the lease and idempotently reassert both
                            # indexes. If Redis was unavailable past the TTL and
                            # a reader pruned this task, a still-live worker
                            # becomes discoverable again on its next heartbeat.
                            await redis_save_task(redis, task_id, id)
                            if bound_generation_operation:
                                operation_live = await refresh_generation_operation(
                                    redis, bound_generation_operation
                                )
                                if not operation_live:
                                    log.error(
                                        "task %s lost generation operation %s",
                                        task_id,
                                        bound_generation_operation["generation_id"],
                                    )
                                    if (
                                        owner_task is not None
                                        and not owner_task.done()
                                        and owner_task.cancelling() == 0
                                    ):
                                        owner_task.cancel()
                                    return
                            last_registry_refresh = asyncio.get_running_loop().time()
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            log.exception("task lease heartbeat failed for %s", task_id)
                            if (
                                asyncio.get_running_loop().time()
                                - last_registry_refresh
                                >= TASK_REGISTRY_FAILURE_CANCEL_SECONDS
                            ):
                                log.error(
                                    "task %s lost its distributed registry for "
                                    "too long; cancelling before its lease can "
                                    "expire",
                                    task_id,
                                )
                                if (
                                    owner_task is not None
                                    and not owner_task.done()
                                    and owner_task.cancelling() == 0
                                ):
                                    owner_task.cancel()
                                return

                lease_heartbeat = asyncio.create_task(_heartbeat())
            return await coroutine
        except BaseException:
            if not started:
                close = getattr(coroutine, "close", None)
                if callable(close):
                    close()
            raise
        finally:
            if lease_heartbeat is not None:
                lease_heartbeat.cancel()
                try:
                    await lease_heartbeat
                except asyncio.CancelledError:
                    pass

    task = asyncio.create_task(_registered_runner())

    # Add a done callback for cleanup. Also surface a non-cancellation failure:
    # otherwise an exception in a detached task (e.g. a headless queue-drain
    # generation) vanishes silently, leaving callers to wonder why nothing ran.
    def _on_task_done(t: asyncio.Task):
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            exc = None
        if exc is not None:
            log.error(
                "background task %s (id=%s) failed: %r",
                task_id,
                id,
                exc,
                exc_info=exc,
            )
        asyncio.create_task(
            cleanup_task(
                redis,
                task_id,
                id,
                generation_operation=bound_generation_operation,
            )
        )

    tasks[task_id] = task

    # If an ID is provided, associate the task with that ID
    if item_tasks.get(id):
        item_tasks[id].append(task_id)
    else:
        item_tasks[id] = [task_id]

    try:
        # ONE admission path for both backends. These two branches used to be
        # written out separately, and they drifted: the in-memory copy never
        # learned the "bind refused because the run was cancelled" case, so a
        # correct refusal surfaced as an opaque RuntimeError instead of the
        # GenerationCancelledError callers handle. Same class of bug as the
        # duplicated cancellation predicate — the cure is not to sync the copies
        # but to stop having copies.
        if redis:
            if not await redis_register_task(
                redis, task_id, id, admission_chat_id=admission_chat_id
            ):
                raise ChatWorkBlockedError(f"Chat {admission_chat_id} is being deleted")
        elif admission_chat_id and await is_chat_work_blocked(None, admission_chat_id):
            raise ChatWorkBlockedError(f"Chat {admission_chat_id} is being deleted")

        if bound_generation_operation:
            chat_id = bound_generation_operation["chat_id"]
            generation_id = bound_generation_operation["generation_id"]
            turn_id = bound_generation_operation["turn_id"]

            async def _cancel_latched() -> bool:
                return (
                    await is_generation_cancelled(redis, chat_id, generation_id)
                    or await is_generation_turn_cancelled(redis, chat_id, turn_id)
                    or await is_chat_work_blocked(redis, chat_id)
                )

            if not await bind_generation_operation_task(
                redis, bound_generation_operation, task_id
            ):
                # A refused bind is overwhelmingly a Stop that landed while this
                # request was still assembling. Distinguish that from genuine
                # registry loss so the caller can report "cancelled" rather than
                # an internal error.
                if await _cancel_latched():
                    raise GenerationCancelledError()
                raise RuntimeError(
                    "generation operation was lost before task registration"
                )
            bound_generation_operation["task_id"] = task_id
            if isinstance(generation_operation, dict):
                generation_operation["task_id"] = task_id
            # Re-check after binding: a Stop that raced the bind itself must not
            # find a task already past its start gate.
            if await _cancel_latched():
                raise GenerationCancelledError()
    except BaseException:
        tasks.pop(task_id, None)
        if task_id in item_tasks.get(id, []):
            item_tasks[id].remove(task_id)
            if not item_tasks[id]:
                item_tasks.pop(id, None)
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        # If cancellation won before `_registered_runner` received its first
        # event-loop slice, that wrapper never entered its try/except and could
        # not close the still-unawaited payload coroutine itself.
        close = getattr(coroutine, "close", None)
        if callable(close):
            close()
        if redis:
            try:
                await redis_cleanup_task(redis, task_id, id)
            except Exception:
                pass
            if bound_generation_operation:
                try:
                    await unregister_generation_operation(
                        redis, bound_generation_operation
                    )
                except Exception:
                    pass
        elif bound_generation_operation:
            await unregister_generation_operation(None, bound_generation_operation)
        raise

    task.add_done_callback(_on_task_done)
    start_gate.set()
    return task_id, task


async def cancel_all_local_tasks(timeout: float = 60.0):
    """Cancel and await all tasks created through this registry.

    Used during server shutdown so active chat generations get an
    asyncio.CancelledError and can run their cancellation/final checkpoint
    handlers before the DB engine and shared HTTP session are torn down.
    """
    pending = list(tasks.items())
    if not pending:
        return []

    for _task_id, task in pending:
        if not task.done() and task.cancelling() == 0:
            task.cancel()

    results = []
    for task_id, task in pending:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            results.append({"task_id": task_id, "status": "completed"})
        except asyncio.CancelledError:
            results.append({"task_id": task_id, "status": "cancelled"})
        except asyncio.TimeoutError:
            results.append({"task_id": task_id, "status": "timeout"})
        except Exception as e:
            results.append({"task_id": task_id, "status": "error", "error": str(e)})
    return results


async def list_task_ids_by_item_id(redis, id):
    """
    List all tasks associated with a specific ID.
    """
    if redis:
        return await redis_list_item_tasks(redis, id)
    return item_tasks.get(id, [])


async def list_item_task_ids_by_prefix(redis, prefix: str) -> List[str]:
    """List task ids whose ITEM key starts with ``prefix``.

    Surfaces independently-registered work (e.g. a subagent rerun, keyed
    ``subagent-rerun:{chat}:{entry}`` rather than the bare chat id) under a chat
    so the chat's task-status endpoint / resume poller can observe it and reload
    when it finishes. Aggregates across every matching item set. Mirrors
    ``redis_list_item_tasks``' decode regime (returns members verbatim)."""
    out: List[str] = []
    if redis:
        match = f"{REDIS_ITEM_TASKS_KEY}:{prefix}*"
        async for key in redis.scan_iter(match=match):
            key_str = _redis_text(key)
            item_id = key_str[len(f"{REDIS_ITEM_TASKS_KEY}:") :]
            out.extend(await redis_list_item_tasks(redis, item_id))
        return out
    for item_id, ids in item_tasks.items():
        if isinstance(item_id, str) and item_id.startswith(prefix):
            out.extend(ids)
    return out


async def list_item_keys_by_prefix(redis, prefix: str) -> List[str]:
    """List the ITEM KEYS (item ids) whose key starts with ``prefix`` and that
    still have at least one live task member.

    Sibling of :func:`list_item_task_ids_by_prefix`: that one returns the task
    uuids, this one returns the bare item ids (e.g. the
    ``subagent-rerun:{chat}:{entry}`` keys) so the frontend can tell WHICH
    subagent_runs entry a live rerun targets — the task uuids alone don't reveal
    that. Only keys with ≥1 live member are returned (empty sets are pruned on
    cleanup, but guard anyway). Mirrors ``redis_list_item_tasks``' decode regime
    (keys come back as ``str`` when ``decode_responses=True``; tolerate bytes)."""
    out: List[str] = []
    if redis:
        match = f"{REDIS_ITEM_TASKS_KEY}:{prefix}*"
        strip = f"{REDIS_ITEM_TASKS_KEY}:"
        async for key in redis.scan_iter(match=match):
            key_str = _redis_text(key)
            if not key_str.startswith(strip):
                continue
            item_id = key_str[len(strip) :]
            if await redis_list_item_tasks(redis, item_id):
                out.append(item_id)
            elif await redis.scard(key) == 0:
                await redis.delete(key)
        return out
    for item_id, ids in item_tasks.items():
        if isinstance(item_id, str) and item_id.startswith(prefix) and ids:
            out.append(item_id)
    return out


def _task_registry_text(value) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


_DRAINING_UNSET = object()


async def collect_chat_work_state(
    redis, chat_id: str, draining=_DRAINING_UNSET
) -> dict:
    """Return one canonical view of generations, detached subagent reruns, and
    the queue-drain marker.

    Both the dedicated task-status endpoint and the consolidated chat-open
    response consume this helper. Generation operations—not the generic task
    index—are authoritative for parent work; the task index remains the
    execution registry for detached reruns and task-id control endpoints.

    ``draining`` is the durable in-flight marker at ``chat.chat["draining"]``,
    written in the same locked transaction that pops the queue head. It answers
    the one question the generation list cannot: a turn has finished, items are
    still queued, and the client needs to know whether THIS server has committed
    to starting the next one. Without it a client can only guess across that
    handoff—and a guess that the server never confirms or denies is what left
    the composer showing Stop after the answer had finished rendering. Callers
    that already hold the marker pass it in to skip the row read.
    """
    if draining is _DRAINING_UNSET:
        from open_webui.models.chats import Chats

        try:
            queue_state = await Chats.get_queue_state_by_id(chat_id)
        except Exception:
            log.debug("drain marker read failed for %s", chat_id, exc_info=True)
            queue_state = None
        draining = (queue_state or {}).get("draining")

    generation_ops = await list_generation_operations_by_item(redis, chat_id)
    rerun_prefix = f"subagent-rerun:{chat_id}:"
    rerun_task_ids = [
        _task_registry_text(value)
        for value in ((await list_item_task_ids_by_prefix(redis, rerun_prefix)) or [])
    ]
    rerun_item_keys = [
        _task_registry_text(value)
        for value in ((await list_item_keys_by_prefix(redis, rerun_prefix)) or [])
    ]
    return {
        "generations": sorted(
            generation_ops,
            key=lambda operation: operation["generation_id"],
        ),
        "rerun_task_ids": list(dict.fromkeys(rerun_task_ids)),
        "subagent_rerun_entry_keys": list(
            dict.fromkeys(
                [
                    key[len(rerun_prefix) :] if key.startswith(rerun_prefix) else key
                    for key in rerun_item_keys
                ]
            )
        ),
        "draining": draining or None,
    }


async def stop_task(redis, task_id: str):
    """
    Cancel a running task and remove it from the global task list.
    """
    if redis:
        # PUBSUB: All instances check if they have this task, and stop if so.
        await redis_send_command(
            redis,
            {
                "action": "stop",
                "task_id": task_id,
            },
        )
        # Optionally check if task_id still in Redis a few moments later for feedback?
        return {"status": True, "message": f"Stop signal sent for {task_id}"}

    # Keep the task discoverable until it is actually terminal. Multiple
    # replacement siblings can wait on the same displaced task concurrently;
    # popping it before ``await task`` made the second waiter conclude that the
    # provider had stopped while the first waiter was still unwinding it.
    task = tasks.get(task_id)
    if not task:
        return {"status": False, "message": f"Task with ID {task_id} not found."}

    if not task.done() and task.cancelling() == 0:
        task.cancel()  # Request task cancellation exactly once.
    try:
        await task  # Wait for the task to handle the cancellation
    except asyncio.CancelledError:
        # Task successfully canceled
        return {"status": True, "message": f"Task {task_id} successfully stopped."}

    if task.cancelled() or task.done():
        return {"status": True, "message": f"Task {task_id} successfully cancelled."}

    return {"status": True, "message": f"Cancellation requested for {task_id}."}


async def stop_item_tasks(redis: Redis, item_id: str):
    """
    Stop all tasks associated with a specific item ID.
    """
    task_ids = await list_task_ids_by_item_id(redis, item_id)
    if not task_ids:
        return {"status": True, "message": f"No tasks found for item {item_id}."}

    for task_id in task_ids:
        result = await stop_task(redis, task_id)
        if not result["status"]:
            return result  # Return the first failure

    return {"status": True, "message": f"All tasks for item {item_id} stopped."}


async def stop_tasks_and_wait(
    redis,
    task_ids: List[str],
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.05,
) -> List[str]:
    """Request cancellation and wait until the exact tasks leave the registry.

    Redis-backed ``stop_task`` is a pub/sub signal, not an acknowledgement.
    Destructive callers (notably chat deletion) must not delete the parent row
    immediately after publishing while a remote worker is still completing its
    shielded terminal write. The worker's registry cleanup is the completion
    acknowledgement. Returns any task ids still live at the bounded timeout.
    """
    wanted = list(
        dict.fromkeys(
            _redis_text(task_id)
            for task_id in (task_ids or [])
            if task_id is not None and _redis_text(task_id)
        )
    )
    if not wanted:
        return []

    for task_id in wanted:
        await stop_task(redis, task_id)

    deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
    while True:
        if redis:
            remaining = await _redis_filter_live_task_ids(redis, wanted)
        else:
            remaining = [
                task_id
                for task_id in wanted
                if ((task := tasks.get(task_id)) is not None and not task.done())
            ]
        if not remaining:
            return []
        if asyncio.get_running_loop().time() >= deadline:
            return remaining
        await asyncio.sleep(max(0.001, poll_interval))


# ===============================
# PENDING MODEL SWITCH MANAGEMENT
# ===============================


async def set_pending_model_switch(task_id: str, model_id: str) -> dict:
    """
    Set a pending model switch for an active task.
    The agentic loop will pick this up at its next iteration.
    """
    import time

    if task_id not in tasks:
        return {"status": False, "message": f"Task {task_id} not found or not active."}

    pending_model_switches[task_id] = {"model_id": model_id, "timestamp": time.time()}

    log.info(f"Pending model switch set for task {task_id}: switching to {model_id}")
    return {
        "status": True,
        "message": f"Model switch to {model_id} queued for task {task_id}.",
    }


def get_pending_model_switch(task_id: str) -> Optional[str]:
    """
    Check if there's a pending model switch for a task.
    Returns the model_id if there's a pending switch, None otherwise.
    """
    if task_id in pending_model_switches:
        return pending_model_switches[task_id].get("model_id")
    return None


def clear_pending_model_switch(task_id: str) -> None:
    """
    Clear the pending model switch after it has been applied.
    """
    if task_id in pending_model_switches:
        del pending_model_switches[task_id]
        log.info(f"Pending model switch cleared for task {task_id}")


# ===============================
# PENDING SERVICE TIER MANAGEMENT
# ===============================


async def set_pending_service_tier(task_id: str, service_tier: str) -> dict:
    """
    Set a pending service_tier change for an active task.
    The agentic loop will pick this up at its next iteration so the next
    outbound request uses the new tier without restarting the agent.
    """
    import time

    if task_id not in tasks:
        return {"status": False, "message": f"Task {task_id} not found or not active."}

    pending_service_tier_switches[task_id] = {
        "service_tier": service_tier,
        "timestamp": time.time(),
    }

    log.info(
        f"Pending service_tier change set for task {task_id}: switching to {service_tier}"
    )
    return {
        "status": True,
        "message": f"service_tier change to {service_tier} queued for task {task_id}.",
    }


def get_pending_service_tier(task_id: str) -> Optional[str]:
    """
    Check if there's a pending service_tier change for a task.
    Returns the service_tier value if pending, None otherwise.
    """
    if task_id in pending_service_tier_switches:
        return pending_service_tier_switches[task_id].get("service_tier")
    return None


def clear_pending_service_tier(task_id: str) -> None:
    """
    Clear the pending service_tier change after it has been applied.
    """
    if task_id in pending_service_tier_switches:
        del pending_service_tier_switches[task_id]
        log.info(f"Pending service_tier change cleared for task {task_id}")


# =================================
# PENDING TOOL SELECTION MANAGEMENT
# =================================


async def set_pending_tool_selection(
    redis, task_id: str, selection: Mapping[str, object]
) -> dict:
    """Replace the desired tool selection for an active generation task.

    A full snapshot deliberately replaces any older pending snapshot. Rapid
    toggles therefore collapse to the user's latest selection instead of
    replaying stale add/remove operations at successive model rounds.
    """

    if not task_id or await get_task_item_id(redis, task_id) is None:
        return {"status": False, "message": f"Task {task_id} not found or not active."}

    snapshot = dict(selection)
    revision = int(snapshot.get("revision") or 0)
    stored = True
    if redis:
        # Keep a watermark separate from the consumable snapshot. An older
        # socket handler can finish after a newer snapshot has already been
        # popped by the model loop; the watermark prevents that late handler
        # from resurrecting stale state. Equal revisions are idempotent retries.
        stored = bool(
            await redis.eval(
                "local current=redis.call('GET',KEYS[2]);"
                "if current and tonumber(current)>=tonumber(ARGV[2]) then return 0 end;"
                "redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[3]);"
                "redis.call('SET',KEYS[2],ARGV[2],'EX',ARGV[3]);"
                "return 1",
                2,
                _pending_tool_selection_key(task_id),
                _tool_selection_revision_key(task_id),
                json.dumps(snapshot, separators=(",", ":")),
                revision,
                TASK_LEASE_TTL_SECONDS,
            )
        )
    else:
        current_revision = pending_tool_selection_revisions.get(task_id, -1)
        stored = revision > current_revision
        if stored:
            pending_tool_selection_revisions[task_id] = revision
            pending_tool_selections[task_id] = snapshot

    log.info(
        "Pending tool selection %s for task %s at revision %s",
        "replaced" if stored else "ignored as stale",
        task_id,
        revision,
    )
    return {
        "status": True,
        "message": (
            f"Tool selection queued for task {task_id}."
            if stored
            else f"Tool selection for task {task_id} was already superseded."
        ),
        "operation_id": snapshot.get("operation_id"),
        "superseded": not stored,
    }


async def pop_pending_tool_selection(redis, task_id: str) -> Optional[dict]:
    """Atomically consume the newest pending selection for ``task_id``."""

    if not task_id:
        return None
    if redis:
        key = _pending_tool_selection_key(task_id)
        getdel = getattr(redis, "getdel", None)
        if callable(getdel):
            value = await getdel(key)
        else:
            # Compatibility with older Redis clients while retaining atomic
            # consume semantics (one key, so cluster routing is unambiguous).
            value = await redis.eval(
                "local v=redis.call('GET',KEYS[1]);"
                "if v then redis.call('DEL',KEYS[1]) end;"
                "return v",
                1,
                key,
            )
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray)):
            value = value.decode()
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            log.warning("Discarding malformed pending tool selection for %s", task_id)
            return None
        return parsed if isinstance(parsed, dict) else None
    return pending_tool_selections.pop(task_id, None)
