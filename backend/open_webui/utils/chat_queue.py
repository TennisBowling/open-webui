"""Autonomous server-driven message-queue drain.

When a chat generation finishes cleanly, the next queued follow-up message
should start automatically — even with zero browser tabs open. This module owns
that drain: it pops the head of ``chat.chat["queue"]`` under a per-chat lock,
marks the chat as ``draining``, and spawns the next generation via
``start_generation`` (the request-free entrypoint in ``main.py``).

Design invariants:

* **Drain only on CLEAN completion.** Stop (CancelledError) and genuine errors
  do NOT trigger a drain — the queue PAUSES so the user can intervene. The
  trigger lives in the success path of ``process_chat_response``; error/cancel
  paths bypass it by construction.
* **Exactly-once pop.** A per-chat lock (Redis ``SET NX EX`` when a websocket
  Redis manager is configured, else an in-process ``asyncio.Lock``) plus the
  ``draining`` marker guarantee that two workers observing the same completion,
  or a duplicate completion event, pop at most one item.
* **Self-healing.** A sweeper (``sweep_orphaned_drains``) recovers chats whose
  ``draining`` marker was set but whose generation task died before clearing it
  (e.g. a worker crash between marking and spawning).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

from open_webui.env import SRC_LOG_LEVELS, REDIS_KEY_PREFIX
from open_webui.models.chats import Chats
from open_webui.tasks import GenerationCancelledError
from open_webui.utils.compaction import is_compact_command

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))


# Per-chat in-process locks for the single-worker (no-Redis) deployment. A
# single worker is already serialized by the event loop, so a plain asyncio
# lock per chat is sufficient there. Keyed by chat_id; created on demand.
_LOCAL_DRAIN_LOCKS: dict[str, asyncio.Lock] = {}

# How long the drain lock is held before auto-expiring (Redis only). The lock
# only guards the pop+mark+spawn handoff (sub-second), never the generation, so
# a short TTL is safe and self-heals a crash that never releases.
_DRAIN_LOCK_TTL_SECONDS = 30

# Grace before the sweeper reclaims a draining marker (Redis multi-worker only).
# A freshly-spawned generation sets `started_at` but registers its task/active
# stream a beat later; reclaiming inside this window would double-pop. Genuine
# orphans simply get caught on a later sweep once they age out.
_DRAIN_ORPHAN_GRACE_SECONDS = 60


# ---- Pending-queue reconciler (sweep_pending_queues) -----------------------
# How many chats to look at per pass. The lookup is index-only over
# chat.queue_armed_at, so this is a sanity bound, not a cost control.
_PENDING_QUEUE_SCAN_LIMIT = 50
# How many drains to actually START per pass. Each one is a real generation, so
# a backlog (e.g. several chats stranded by the same restart) is spread over
# passes instead of arriving as a burst.
_PENDING_QUEUE_MAX_STARTS_PER_PASS = 2
# Fuse, not policy. In normal operation a queue is either drained within seconds
# or deliberately left pending by a Stop/error, so an armed flag should never be
# days old; if one is, something is wrong in a way that firing an ancient
# message would only make worse. The user's own rule — "I queue things, leave,
# and expect them done when I get back" — is what the reconciler exists to
# honour, so this is set far beyond any plausible "back later".
_PENDING_QUEUE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _local_lock(chat_id: str) -> asyncio.Lock:
    lock = _LOCAL_DRAIN_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCAL_DRAIN_LOCKS[chat_id] = lock
    return lock


def _drain_lock_key(chat_id: str) -> str:
    return f"{REDIS_KEY_PREFIX}:chat:drain_lock:{chat_id}"


def _draining_set_key() -> str:
    return f"{REDIS_KEY_PREFIX}:chat:draining_chats"


class _DrainLock:
    """Async context-manager-ish lock that uses Redis when available and an
    in-process asyncio.Lock otherwise. ``acquire`` returns True iff the lock was
    obtained (Redis NX); a False return means another worker is mid-drain for
    this chat and the caller should bail out."""

    def __init__(self, redis, chat_id: str):
        self.redis = redis
        self.chat_id = chat_id
        self.token = str(uuid.uuid4())
        self._local: Optional[asyncio.Lock] = None
        self._have_local = False

    async def acquire(self) -> bool:
        if self.redis is not None:
            try:
                got = await self.redis.set(
                    _drain_lock_key(self.chat_id),
                    self.token,
                    nx=True,
                    ex=_DRAIN_LOCK_TTL_SECONDS,
                )
                return bool(got)
            except Exception:
                log.exception("drain lock acquire (redis) failed for %s", self.chat_id)
                # Fall through to local lock so a transient Redis blip doesn't
                # wedge the queue forever.
        self._local = _local_lock(self.chat_id)
        # Non-blocking: if someone else holds it, treat as "another drain in
        # progress" rather than queueing up a second drain.
        if self._local.locked():
            return False
        await self._local.acquire()
        self._have_local = True
        return True

    async def release(self) -> None:
        if self.redis is not None:
            try:
                # Owner-checked delete so we never drop someone else's lock.
                val = await self.redis.get(_drain_lock_key(self.chat_id))
                if val == self.token:
                    await self.redis.delete(_drain_lock_key(self.chat_id))
            except Exception:
                log.exception("drain lock release (redis) failed for %s", self.chat_id)
        if self._have_local and self._local is not None:
            try:
                self._local.release()
            except RuntimeError:
                pass
            self._have_local = False


def _item_spec(item: dict) -> dict:
    """Return the dict that actually carries a queue item's send payload.

    The frontend persists each item with the payload NESTED under ``sendSpec``
    (``{id, prompt, files, createdAt, sendSpec: {model, content, models, ...}}``)
    — see ``captureQueueSendSpec`` / ``enqueueMessage`` in ``Chat.svelte``. Older
    items and test fixtures may be FLAT (fields at the top level). Read through
    ``sendSpec`` when present, else fall back to the item itself. Without this,
    ``item.get("content")`` / ``item.get("model")`` are ``None`` on real items, so
    a drained turn shows an empty user bubble with no model and never generates.
    """
    if not isinstance(item, dict):
        return {}
    spec = item.get("sendSpec")
    return spec if isinstance(spec, dict) else item


async def _current_leaf_id(chat_id: str) -> Optional[str]:
    """Return the chat's current head message id (history.currentId), read from
    the raw blob (the message table is peeled out but currentId stays). This is
    the parent the next queued user message hangs off of."""
    try:
        chat = await Chats.get_chat_by_id(chat_id)
        if not chat or not isinstance(chat.chat, dict):
            return None
        history = chat.chat.get("history") or {}
        return history.get("currentId")
    except Exception:
        return None


async def _resolve_drain_parent(
    chat_id: str,
    finished_response_id: Optional[str],
    queued_parent_message_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve the parent for a drained follow-up's user message.

    Prefer the branch head snapshotted when the user queued the follow-up. This
    is deterministic for multi-model turns: whichever sibling happens to finish
    last must not silently choose the follow-up's branch. Legacy queue items fall
    back to ``finished_response_id``, then the current live leaf."""
    if queued_parent_message_id:
        try:
            msg = await Chats.get_message_by_id_and_message_id(
                chat_id, queued_parent_message_id
            )
            if msg:
                return queued_parent_message_id
        except Exception:
            pass
    if finished_response_id:
        try:
            msg = await Chats.get_message_by_id_and_message_id(
                chat_id, finished_response_id
            )
            if msg:
                return finished_response_id
        except Exception:
            pass
    return await _current_leaf_id(chat_id)


async def _build_send_spec_from_item(
    item: dict,
    response_message_id: str,
    new_user_message: dict,
) -> dict:
    """Translate a persisted queue item into a ``start_generation`` send_spec.

    The queue item is self-contained (snapshotted at enqueue time). We map its
    fields onto the form the request-free entrypoint expects, stamping the fresh
    assistant ``response_message_id`` and freshly-built user message. The
    assembly leaf is that USER message itself, not its parent assistant: the user
    row is persisted immediately before this function runs, so walking from its
    parent would omit the queued prompt and send a request ending in a model turn.
    The payload is read through ``_item_spec`` because the frontend nests it under
    ``sendSpec`` (flat items / fixtures still work).
    """
    spec_src = _item_spec(item)
    spec: dict = {
        "model": spec_src.get("model"),
        "leaf_message_id": new_user_message["id"],
        "new_user_message": new_user_message,
        "response_message_id": response_message_id,
        "stream": True,
    }
    for key in (
        "params",
        "tool_ids",
        "tool_servers",
        "filter_ids",
        "features",
        "variables",
        "files",
        "reasoning",
        "service_tier",
        "background_tasks",
        "model_item",
        "stream_options",
        "timezone",
    ):
        if spec_src.get(key) is not None:
            spec[key] = spec_src[key]
    return spec


async def _register_draining(redis, chat_id: str) -> None:
    if redis is None:
        return
    try:
        await redis.sadd(_draining_set_key(), chat_id)
    except Exception:
        log.debug("register_draining sadd failed for %s", chat_id, exc_info=True)


async def _unregister_draining(redis, chat_id: str) -> None:
    if redis is None:
        return
    try:
        await redis.srem(_draining_set_key(), chat_id)
    except Exception:
        log.debug("unregister_draining srem failed for %s", chat_id, exc_info=True)


async def broadcast_queue_state(
    user_id: str,
    chat_id: str,
    event_type: str = "chat:queue:updated",
    skip_sid: str = None,
    **extra,
) -> None:
    """Emit the current queue + draining state for a chat to all of the user's
    tabs so the reflector UI updates. ``event_type`` is ``chat:queue:updated``
    for plain queue mutations or ``chat:queue:drained`` when a queued item just
    started generating (``extra`` then carries item_id / response_message_id).
    ``skip_sid`` omits one originating socket (e.g. the tab that just enqueued via
    PATCH already applied the change optimistically)."""
    if not user_id or not chat_id or str(chat_id).startswith("local:"):
        return
    try:
        state = await Chats.get_queue_state_by_id(chat_id) or {}
        from open_webui.socket.main import broadcast_queue_event

        await broadcast_queue_event(
            user_id,
            chat_id,
            {
                "type": event_type,
                "data": {
                    "chat_id": chat_id,
                    "queue": state.get("queue") or [],
                    "queue_length": len(state.get("queue") or []),
                    "draining": state.get("draining"),
                    **extra,
                },
            },
            skip_sid=skip_sid,
        )
    except Exception:
        log.debug("broadcast_queue_state failed for %s", chat_id, exc_info=True)


async def clear_draining(
    redis,
    chat_id: str,
    finished_response_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Clear the in-flight marker for a chat. When ``finished_response_id`` is
    given, only clear if the marker belongs to that generation (so an
    errored/cancelled turn never wipes a newer turn's marker). Called ONLY from
    error/cancel/failure paths (the CLEAN path goes through ``maybe_drain_queue``
    instead). Idempotent.

    Because this is the terminal-without-clean-drain path, also downgrade any
    pending ``mode == "steer"`` items to ``after_final``: a steer was meant for a
    tool-call boundary of the response that just died, so leaving it as a steer
    would leak it into the NEXT unrelated generation's boundary. Downgrading
    turns it into a normal visible follow-up (same destination an unconsumed
    steer reaches when the model finishes with no more tools). The downgrade is
    done ATOMICALLY inside ``clear_draining_by_id`` (in the same locked
    transaction that clears the marker, and only when we actually own/clear it),
    so a concurrent append can't resurrect the marker and trick us into skipping
    it. ``user_id`` (when known) lets us broadcast the resulting chip-strip
    change to the user's tabs.
    """
    result = await Chats.clear_draining_by_id(chat_id, finished_response_id)
    # ``cleared`` is authoritative: the model method itself reports whether it
    # cleared our marker (vs. left a newer generation's marker intact), so we
    # never rely on a racy separate re-read of the blob.
    cleared = bool(result and result.get("cleared"))
    # ``missing`` = the chat row is gone (deleted). ``result is None`` (without
    # missing) means a TRANSIENT error where the marker may still exist — do NOT
    # unregister then, or the multi-worker sweep (which enumerates only the redis
    # set) loses the chat and its lingering marker wedges every future drain.
    missing = bool(result and result.get("missing"))
    converted = int(result.get("converted") or 0) if result else 0

    # Drop from the cross-worker draining set only on a confirmed clear or a
    # confirmed-deleted chat (a newer generation may still own the marker, and a
    # transient error must keep the chat tracked for the sweep).
    if cleared or missing:
        await _unregister_draining(redis, chat_id)

    # If steers were downgraded, reflect the chip-strip change to the user's
    # tabs. Best-effort; never break the caller.
    if cleared and converted and not str(chat_id).startswith("local:"):
        try:
            resolved_user_id = user_id
            if resolved_user_id is None:
                chat = await Chats.get_chat_by_id(chat_id)
                resolved_user_id = getattr(chat, "user_id", None)
            if resolved_user_id:
                await broadcast_queue_state(resolved_user_id, chat_id)
        except Exception:
            log.debug(
                "steer→after_final broadcast in clear_draining failed for %s",
                chat_id,
                exc_info=True,
            )


async def _finalize_failed_headless_drain(
    redis,
    user,
    chat_id: str,
    response_message_id: str,
    user_message_id: Optional[str],
    generation_id: str,
    turn_id: str,
    item: dict,
    error: Exception,
) -> None:
    """A headless drain failed BEFORE process_chat_response ran (e.g. the queued
    model was removed / access revoked, or conversation assembly raised). The
    user message is persisted before startup, so surface an error on the
    assistant message — otherwise the queued turn silently shows a user bubble
    with no response and no explanation. Then clear our draining marker and tell
    tabs to reload. All best-effort; never raises."""
    detail = getattr(error, "detail", None) or str(error) or "Generation failed"
    try:
        await Chats.update_generation_message_if_current(
            chat_id,
            response_message_id,
            generation_id,
            turn_id,
            {
                "role": "assistant",
                "model": _item_spec(item).get("model"),
                "parentId": user_message_id,
                "content": "",
                "error": {"content": str(detail)},
                "done": True,
            },
            create_if_missing=True,
        )
    except Exception:
        log.debug(
            "failed-drain error-message upsert failed for %s",
            chat_id,
            exc_info=True,
        )
    try:
        await clear_draining(redis, chat_id, finished_response_id=response_message_id)
    except Exception:
        log.debug(
            "clear_draining after headless failure failed for %s",
            chat_id,
            exc_info=True,
        )
    # Broadcast chat:queue:drained so tabs reload (the error row appears) AND the
    # queue chip clears: this event carries the now-popped queue, and since the
    # early chip-shrink was removed from maybe_drain_queue this is what clears the
    # chip on this failure path (the item is shown as a chip until the failure
    # resolves, then becomes the error row — never a "neither chip nor message" gap).
    try:
        await broadcast_queue_state(
            getattr(user, "id", None),
            chat_id,
            event_type="chat:queue:drained",
            response_message_id=response_message_id,
            user_message_id=user_message_id,
            generation_id=generation_id,
            turn_id=turn_id,
        )
    except Exception:
        log.debug("failed-drain broadcast failed for %s", chat_id, exc_info=True)


async def _finalize_cancelled_headless_drain(
    redis,
    user,
    chat_id: str,
    response_message_id: str,
    user_message_id: Optional[str],
    generation_id: str,
    turn_id: str,
    item: dict,
) -> None:
    """A headless drain was CANCELLED (user Stop) in the window BEFORE process_chat
    entered its own try/except — i.e. during conversation assembly, the route-level
    chat:user-message/chip-clear emit, or process_chat_payload. In that window
    process_chat's CancelledError handler never runs, so without this the durable
    user message is left as a dangling bubble with NO assistant row and NO
    chat:queue:drained — invisible (history.currentId stuck on the user message)
    until a manual reload. Mirror process_chat's cancel handling: write a terminal
    STOPPED assistant row (parented to the new user message), clear our draining
    marker, and broadcast chat:queue:drained so every tab renders it as a stopped
    turn instead of a half-rendered one.

    Idempotent + best-effort. The identity-checked write is safe on a DETACHED
    task: if a later retry has already claimed this assistant id, this old
    finalizer cannot overwrite it."""
    try:
        await Chats.update_generation_message_if_current(
            chat_id,
            response_message_id,
            generation_id,
            turn_id,
            {
                "role": "assistant",
                "model": _item_spec(item).get("model"),
                "parentId": user_message_id,
                "done": True,
                "userStopped": True,
            },
            create_if_missing=True,
        )
    except Exception:
        log.debug(
            "cancelled-drain terminal upsert failed for %s", chat_id, exc_info=True
        )
    try:
        await clear_draining(
            redis,
            chat_id,
            finished_response_id=response_message_id,
            user_id=getattr(user, "id", None),
        )
    except Exception:
        log.debug(
            "clear_draining after headless cancel failed for %s",
            chat_id,
            exc_info=True,
        )
    try:
        await broadcast_queue_state(
            getattr(user, "id", None),
            chat_id,
            event_type="chat:queue:drained",
            response_message_id=response_message_id,
            user_message_id=user_message_id,
            generation_id=generation_id,
            turn_id=turn_id,
        )
    except Exception:
        log.debug("cancelled-drain broadcast failed for %s", chat_id, exc_info=True)


async def _was_user_stopped(chat_id: str, response_id: Optional[str]) -> bool:
    """True if the given (finished) assistant message was explicitly user-stopped.

    ``userStopped`` is a durable field set by the identity-checked backend Stop
    path (and read by ``stream_state.terminal_status_from_message`` for reload
    status). Consulting it here lets the drain honor a Stop as a PAUSE even when
    the cancellation signal never reached the owning worker. Without this, such
    a Stop could complete and auto-drain the next item. The normal single-worker
    case still pauses via the CancelledError → clear_draining path; this is the
    durable backstop. Best-effort: never raises."""
    if not response_id:
        return False
    try:
        msg = await Chats.get_message_by_id_and_message_id(chat_id, response_id)
        if not msg:
            return False
        if msg.get("userStopped") is True:
            return True
        from open_webui.utils.stream_state import terminal_status_from_message

        return terminal_status_from_message(msg) == "cancelled"
    except Exception:
        return False


async def _run_queued_compaction(app, user, chat_id: str, spec_src: dict) -> None:
    """Run a `/compact` that reached the drain. Best-effort, never raises.

    Uses the same ``HeadlessRequest`` carrier ``start_generation`` uses, because
    the summarizer call goes through the normal completion pipeline and needs a
    request-shaped app handle.
    """
    from open_webui.utils.compaction import (
        NothingToCompactError,
        compact_chat_now,
    )
    from open_webui.utils.headless_request import HeadlessRequest

    try:
        models = getattr(getattr(app, "state", None), "MODELS", None) or {}
        model = models.get(spec_src.get("model")) if spec_src.get("model") else None
        if model is None:
            model = models.get(next(iter(spec_src.get("models") or []), None))
        if model is None:
            log.warning(
                "queued /compact for chat %s has no resolvable model; skipping",
                chat_id,
            )
            return
        await compact_chat_now(
            HeadlessRequest(app),
            user,
            chat_id=chat_id,
            model=model,
        )
    except NothingToCompactError:
        log.info("queued /compact for chat %s: nothing to compact", chat_id)
    except Exception:
        log.exception("queued /compact failed for chat %s", chat_id)


async def maybe_drain_queue(
    app, user, chat_id: str, finished_response_id: Optional[str] = None
) -> Optional[str]:
    """Attempt to pop the next queued message for ``chat_id`` and start its
    generation. Call this ONLY from a clean-completion path.

    ``finished_response_id`` is the message id of the generation that just
    completed (it owns the current ``draining`` marker). The pop is allowed only
    when the stored marker is absent or belongs to this finished generation,
    which makes duplicate/stale completions no-ops.

    Returns the new generation's ``response_message_id`` if one was started,
    else None (nothing queued, already superseded, or lock contended). Never
    raises — drain failures must not break the generation that triggered them.
    """
    if not chat_id or str(chat_id).startswith("local:"):
        return None

    redis = getattr(getattr(app, "state", None), "redis", None)
    lock = _DrainLock(redis, chat_id)
    if not await lock.acquire():
        # Another worker / coroutine is draining this chat right now.
        return None

    reserved_operation: Optional[dict] = None
    reservation_handed_off = False
    popped_item: Optional[dict] = None
    new_response_id: Optional[str] = None
    new_user_message_id: Optional[str] = None

    try:
        # Honor an explicit user Stop even on a "clean" completion: if the
        # finishing generation's message was user-stopped, the user asked to
        # PAUSE the queue — don't auto-drain the item they meant to abandon. The
        # queue is left intact (clear_draining only clears the marker + downgrades
        # any orphaned steers), so the user resumes it manually (or via "Send
        # now").
        if finished_response_id and await _was_user_stopped(
            chat_id, finished_response_id
        ):
            await clear_draining(
                redis,
                chat_id,
                finished_response_id=finished_response_id,
                user_id=getattr(user, "id", None),
            )
            return None

        # Manual / sweep drain (no finishing generation): guard against a LIVE
        # generation before popping. The draining-marker ownership check inside
        # the pop only catches another DRAIN — a NORMAL user turn sets no marker,
        # so without this a "Send now" issued while a normal generation is still
        # running (e.g. the client optimistically paused after Stop but the
        # backend cancel never landed) would pop the head and spawn a SECOND
        # concurrent generation. The normal clean-completion drain passes a
        # finished_response_id (its own task is momentarily still registered), so
        # this guard is intentionally scoped to the finished_response_id is None
        # case only — mirroring the sweep's own liveness check.
        if finished_response_id is None:
            try:
                from open_webui.tasks import has_active_generation_operations
                from open_webui.socket.main import get_active_streams_for_chat

                if await has_active_generation_operations(redis, chat_id) or (
                    get_active_streams_for_chat(chat_id)
                ):
                    return None
            except Exception:
                log.debug(
                    "drain in-flight guard check failed for %s",
                    chat_id,
                    exc_info=True,
                )

        # Reserve the generation BEFORE popping the item. The operation registry
        # owns the per-chat turn lease, so a normal user send and a queue drain
        # cannot both pass separate liveness checks and start concurrent turns.
        # A clean-completion caller releases its completed operation immediately
        # before entering here; any still-running multi-model sibling keeps the
        # old turn lease and therefore prevents an early queue advance.
        new_response_id = str(uuid.uuid4())
        new_user_message_id = str(uuid.uuid4())
        generation_id = str(uuid.uuid4())
        reserved_operation = {
            "generation_id": generation_id,
            "chat_id": chat_id,
            "message_id": new_response_id,
            "turn_id": new_user_message_id,
            "task_id": "",
        }
        from open_webui.tasks import (
            register_generation_operation,
            unregister_generation_operation,
        )

        registration = await register_generation_operation(redis, reserved_operation)
        if registration != "acquired":
            reserved_operation = None
            return None

        def _marker(item: dict) -> dict:
            return {
                "item_id": item.get("id"),
                "response_message_id": new_response_id,
                "started_at": int(time.time()),
            }

        result = await Chats.pop_queue_head_and_mark_draining_by_id(
            chat_id, _marker, expected_finished_response_id=finished_response_id
        )
        if not result or result.get("item") is None:
            # Already superseded, empty queue, or chat gone — nothing to do.
            # If the queue is now empty the marker was cleared by the pop helper;
            # reflect that in the cross-worker set.
            if result is not None and result.get("draining") is None:
                await _unregister_draining(redis, chat_id)
            await unregister_generation_operation(redis, reserved_operation)
            reserved_operation = None
            return None

        item = result["item"]
        popped_item = item
        await _register_draining(redis, chat_id)
        spec_src = _item_spec(item)

        # `/compact` is a command, not a prompt. It reaches the drain two ways:
        # queued explicitly (Alt+Enter), or as a steer the agentic loop never got
        # to consume because the model finished without another tool round. Either
        # way it must NOT become a user turn — without this the model is sent the
        # literal string "/compact" and answers it.
        if is_compact_command(spec_src.get("content") or item.get("prompt") or ""):
            await unregister_generation_operation(redis, reserved_operation)
            reserved_operation = None
            try:
                await _run_queued_compaction(app, user, chat_id, spec_src)
            finally:
                # Release the marker we took above. We are the clean path but
                # started no generation, so nothing else will ever clear it.
                await clear_draining(
                    redis,
                    chat_id,
                    finished_response_id=new_response_id,
                    user_id=getattr(user, "id", None),
                )
                await broadcast_queue_state(getattr(user, "id", None), chat_id)
                # Anything still queued behind the command has to wait for a new
                # drive: this call holds the drain lock until it returns, so it
                # cannot recurse. The detached re-drive usually wins; when it
                # loses the lock race `sweep_pending_queues` (15s) is the backstop
                # — the queue reconciler exists for exactly this shape of gap.
                asyncio.ensure_future(maybe_drain_queue(app, user, chat_id))
            return None

        # Resolve the new user message's parent at DRAIN TIME, not enqueue time.
        # When the user queued this item, the previous turn's assistant message
        # didn't exist yet — so the queue item can't know its parent. The parent
        # is the chat's current head (history.currentId), which the just-finished
        # generation stamped to its own assistant message. This mirrors the old
        # client dequeueAndSend, which parented the queued user message off
        # history.currentId at send time.
        # Resolve the new user message's parent. Prefer the response that just
        # completed and triggered this drain (finished_response_id) over the live
        # history.currentId, which a mid-stream branch switch can move onto an
        # unrelated subtree — mis-parenting the follow-up. Falls back to the live
        # leaf for a manual "Send now" / sweep re-drive (finished_response_id None).
        leaf_message_id = await _resolve_drain_parent(
            chat_id,
            finished_response_id,
            spec_src.get("queued_parent_message_id"),
        )

        # Build the new user message fresh: snapshot content/files/models from the
        # queue item, assign a fresh id + the resolved parent. Read through
        # _item_spec because the frontend nests the payload under `sendSpec`.
        # `files` is OMITTED (not written as []) when the item has none — an
        # empty array is truthy in the UI's `message.files` render guard and
        # produced phantom attachment rows on messages sent with nothing.
        new_user_message = {
            "id": new_user_message_id,
            "parentId": leaf_message_id,
            "childrenIds": [],
            "role": "user",
            "content": spec_src.get("content") or item.get("prompt") or "",
            "models": spec_src.get("models")
            or ([spec_src.get("model")] if spec_src.get("model") else []),
            "timestamp": int(time.time()),
        }
        item_files = spec_src.get("files")
        if isinstance(item_files, list) and item_files:
            new_user_message["files"] = item_files

        # Persist the queued user row before starting generation. Assembly is
        # intentionally allowed to observe an already-durable row (that is also
        # how ordinary browser sends work), while startup failure/cancellation
        # can now always attach its terminal assistant to a real parent.
        await Chats.upsert_message_to_chat_by_id_and_message_id(
            chat_id,
            new_user_message_id,
            new_user_message,
            return_model=False,
        )

        send_spec = await _build_send_spec_from_item(
            item, new_response_id, new_user_message
        )
        send_spec["generation_id"] = generation_id
        send_spec["turn_id"] = new_user_message_id
        # Carry the attach ids so the generation can fire `chat:queue:drained`
        # AFTER it has persisted the user message + assistant placeholder, rather
        # than here (which raced ahead of persistence and left tabs blank).
        send_spec["queue_drained_broadcast"] = {
            "item_id": item.get("id"),
            "user_message_id": new_user_message_id,
            "response_message_id": new_response_id,
            "generation_id": generation_id,
            "turn_id": new_user_message_id,
        }

        # Lazy import to avoid a circular import at module load (main imports the
        # router layer which transitively could reach here).
        from open_webui.main import start_generation
        from open_webui.tasks import create_task

        async def _drive_generation():
            # Wrapper so a failure in the detached generation clears OUR draining
            # marker (best-effort) instead of wedging the queue. clear_draining is
            # idempotent + ownership-checked, so it only clears this generation's
            # marker. process_chat's own error/cancel handlers also clear it; this
            # is defense in depth for failures BEFORE the pipeline gets that far
            # (e.g. the queued model was removed / access revoked, or assembly
            # raised) — in which case process_chat_response never ran, so we also
            # surface an error on the assistant message so the queued turn doesn't
            # silently vanish (the user message was already persisted by assembly).
            try:
                # NOTE: this wrapper deliberately does NOT own the reservation —
                # see the create_task call below.
                response = await start_generation(
                    chat_id,
                    send_spec,
                    user,
                    generation_operation=reserved_operation,
                )
                if isinstance(response, dict) and response.get("cancelled"):
                    await _finalize_cancelled_headless_drain(
                        redis,
                        user,
                        chat_id,
                        new_response_id,
                        new_user_message_id,
                        generation_id,
                        new_user_message_id,
                        item,
                    )
            except asyncio.CancelledError:
                # Cancelled (user Stop) BEFORE process_chat entered its own
                # try/except — i.e. during chat_completion's assembly / route-level
                # emit / process_chat_payload, which are outside process_chat's
                # CancelledError handler. Without this the drained turn is left as a
                # dangling user bubble with no assistant row and no chat:queue:drained
                # (invisible until reload). Run the terminal-stopped finalize on a
                # DETACHED task so THIS task's cancellation can't abort the cleanup,
                # then re-raise so the task unwinds. If the cancel actually landed
                # inside process_chat, its own handler already wrote the terminal row
                # and the finalize no-ops on its done-row guard.
                try:
                    asyncio.ensure_future(
                        _finalize_cancelled_headless_drain(
                            redis,
                            user,
                            chat_id,
                            new_response_id,
                            new_user_message_id,
                            generation_id,
                            new_user_message_id,
                            item,
                        )
                    )
                except Exception:
                    log.debug(
                        "scheduling cancelled-drain finalize failed for %s",
                        chat_id,
                        exc_info=True,
                    )
                raise
            except Exception as e:
                log.exception(
                    "headless start_generation failed for chat %s (response %s)",
                    chat_id,
                    new_response_id,
                )
                await _finalize_failed_headless_drain(
                    redis,
                    user,
                    chat_id,
                    new_response_id,
                    new_user_message_id,
                    generation_id,
                    new_user_message_id,
                    item,
                    e,
                )
            finally:
                # Release the reservation IF nobody downstream claimed it. This
                # is ownership-checked on task_id, so once the real generation
                # task has bound itself (the registry copy carries ITS task id,
                # ours is still "") this is a no-op — it only fires for the paths
                # where the generation never got far enough to take over, which
                # would otherwise leave a reservation holding the chat's turn
                # lease forever (every later send answering 409).
                try:
                    from open_webui.tasks import unregister_generation_operation

                    await unregister_generation_operation(redis, reserved_operation)
                except Exception:
                    log.debug(
                        "releasing unclaimed drain reservation failed for %s",
                        chat_id,
                        exc_info=True,
                    )

        # Spawn a detached task keyed by chat_id so task-id controls and
        # diagnostics can resolve it.
        #
        # WITHOUT `generation_operation=`, deliberately. Passing it here BOUND the
        # reservation to this wrapper task — and the wrapper is not the thing that
        # runs the generation. The chain was: this bind stamps the operation with
        # the wrapper's task id, `_chat_completion_with_operation` reads that id
        # back onto its own copy, and then the create_task that spawns the ACTUAL
        # generation tries to bind a different task id to an operation that
        # already has one, which `bind_generation_operation_task` refuses —
        # `RuntimeError("generation operation was lost before task registration")`,
        # surfaced to the user as a failed queued/steer message. Every real drain
        # hit it; the orchestration tests did not because they mock create_task,
        # so nothing ever bound anything (see the regression test that now uses
        # the real one). The wrapper also outlives its usefulness immediately —
        # _chat_completion_impl spawns the generation and returns — so binding the
        # lease to it would have released the operation while the generation was
        # still running anyway.
        #
        # The reservation therefore stays unbound here and is claimed by whichever
        # task actually generates; the `finally` above returns it if none does.
        await create_task(
            redis,
            _drive_generation(),
            id=chat_id,
        )
        reservation_handed_off = True

        log.info(
            "drained queue item %s for chat %s -> response %s",
            item.get("id"),
            chat_id,
            new_response_id,
        )

        # NOTE: we deliberately do NOT shrink the chip strip here anymore. The
        # chip is cleared later — atomically with the drained user message
        # becoming a real chat bubble — once assembly has persisted that user
        # message and `chat_completion` emits the paired `chat:user-message` +
        # chip-clear (see main.py, the `queue_drained_broadcast` branch right
        # after the cross-device emit). Shrinking the chip here (the old
        # behavior) opened a gap where the queued message was in NEITHER the chip
        # strip nor the transcript until the much-later `chat:queue:drained` ->
        # loadChat() landed — and for a non-streaming upstream that ATTACH is
        # withheld until the entire response is ready, so the queued message
        # appeared to vanish for the whole turn. Keeping the chip until the bubble
        # exists means the message is always visible somewhere. The failure/cancel
        # paths (`_finalize_failed_headless_drain`, the headless CancelledError
        # handler) each broadcast `chat:queue:drained` carrying the popped queue,
        # so the chip still clears if the generation never reaches assembly.
        return new_response_id
    except GenerationCancelledError:
        # create_task raises GenerationCancelledError here when Stop won before
        # its start gate opened. The popped item is already a real turn, so make
        # its terminal stopped row visible instead of silently losing it.
        if popped_item is not None and new_response_id is not None:
            await _finalize_cancelled_headless_drain(
                redis,
                user,
                chat_id,
                new_response_id,
                new_user_message_id,
                generation_id,
                new_user_message_id,
                popped_item,
            )
        if reserved_operation is not None and not reservation_handed_off:
            try:
                from open_webui.tasks import unregister_generation_operation

                await unregister_generation_operation(redis, reserved_operation)
            except Exception:
                pass
        return new_response_id
    except asyncio.CancelledError:
        # Cancellation of the caller itself (for example Stop arriving while a
        # finishing response is handing off its queue) must keep propagating.
        # Preserve queue visibility with detached cleanup, then unwind normally.
        if popped_item is not None and new_response_id is not None:
            asyncio.ensure_future(
                _finalize_cancelled_headless_drain(
                    redis,
                    user,
                    chat_id,
                    new_response_id,
                    new_user_message_id,
                    generation_id,
                    new_user_message_id,
                    popped_item,
                )
            )
        if reserved_operation is not None and not reservation_handed_off:
            try:
                from open_webui.tasks import unregister_generation_operation

                await asyncio.shield(
                    unregister_generation_operation(redis, reserved_operation)
                )
            except Exception:
                pass
        raise
    except Exception as error:
        log.exception("maybe_drain_queue failed for chat %s", chat_id)
        if reserved_operation is not None and not reservation_handed_off:
            try:
                from open_webui.tasks import unregister_generation_operation

                await unregister_generation_operation(redis, reserved_operation)
            except Exception:
                pass
        if popped_item is not None and new_response_id is not None:
            await _finalize_failed_headless_drain(
                redis,
                user,
                chat_id,
                new_response_id,
                new_user_message_id,
                generation_id,
                new_user_message_id,
                popped_item,
                error,
            )
            return None
        # Best-effort: clear the marker we may have set so the queue isn't
        # wedged. The sweeper is the backstop if even this fails.
        try:
            await clear_draining(redis, chat_id)
        except Exception:
            pass
        # We removed the early chip-shrink broadcast (the chip is normally cleared
        # atomically with the drained bubble after assembly). If we already popped
        # an item but failed before the generation could run — so neither the
        # success path nor the _finalize/cancel paths will broadcast — reflect the
        # now-popped queue so the chip doesn't linger on a phantom item.
        try:
            await broadcast_queue_state(getattr(user, "id", None), chat_id)
        except Exception:
            pass
        return None
    finally:
        await lock.release()


async def _last_turn_outcome(chat_id: str) -> tuple[str, Optional[str]]:
    """Classify how this chat's current head turn ended, for the reconciler.

    Returns ``(outcome, leaf_id)`` where outcome is one of:

    * ``"clean"``    – head is a finished assistant message with no error and no
      Stop. The queue behind it is owed a drain.
    * ``"paused"``   – head is an assistant that errored or was stopped. The
      queue is pending BY DESIGN: Stop and errors pause the queue so the user
      can intervene, and that decision must survive the reconciler.
    * ``"unsettled"``– head is still (or forever) mid-flight, or is a user
      message with no reply, or can't be read. Draining now would either race a
      live turn or hang the follow-up off a user message; leave it alone.

    This reads the same durable fields every other reconcile path trusts
    (``done`` / ``error`` / ``userStopped``), which is why the reconciler needs
    no pause flag of its own: a flag would have to be written correctly by every
    present and future pause path, whereas the outcome is simply observed.
    """
    leaf_id = await _current_leaf_id(chat_id)
    if not leaf_id:
        return "unsettled", None
    try:
        msg = await Chats.get_message_by_id_and_message_id(chat_id, leaf_id)
    except Exception:
        return "unsettled", leaf_id
    if not msg:
        return "unsettled", leaf_id
    if msg.get("role") != "assistant":
        return "unsettled", leaf_id

    from open_webui.utils.stream_state import terminal_status_from_message

    status = terminal_status_from_message(msg)
    if status == "done":
        return "clean", leaf_id
    if status in ("error", "cancelled"):
        return "paused", leaf_id
    return "unsettled", leaf_id


async def sweep_pending_queues(app) -> int:
    """Start the drain for chats that are owed one and have nothing running.

    The clean-completion trigger in ``process_chat_response`` is a single event,
    and it can be missed: the worker can restart mid-turn, the per-chat drain
    lock or the turn lease can be held at that instant (both make
    ``maybe_drain_queue`` return None, silently and permanently), or the
    completion can take a path that never reaches the trigger. Any of those left
    the queue in the blob with nothing that would ever look at it again —
    ``sweep_orphaned_drains`` only sees chats with a ``draining`` MARKER, i.e.
    drains that died in flight, never a queue that was never drained at all.

    So the queue does not depend on the event any more. This makes the durable
    state converge instead: while a chat has queued items and nothing is running
    for it, the server owes the user a generation, and it keeps re-deciding that
    every pass until it is true. Which is what "queue it and walk away" has to
    mean — the browser is not part of the contract.

    Deliberately NOT here: resuming a queue the user stopped or one whose last
    turn errored (see ``_last_turn_outcome``), and anything that is merely slow
    (the liveness guards inside ``maybe_drain_queue`` own that race).

    Returns the number of drains started.
    """
    candidates = await Chats.get_armed_queue_chats(limit=_PENDING_QUEUE_SCAN_LIMIT)
    if not candidates:
        return 0

    from open_webui.tasks import has_active_generation_operations
    from open_webui.socket.main import get_active_streams_for_chat
    from open_webui.models.users import Users

    redis = getattr(getattr(app, "state", None), "redis", None)
    started = 0
    now = time.time()

    for candidate in candidates:
        if started >= _PENDING_QUEUE_MAX_STARTS_PER_PASS:
            break
        chat_id = candidate.get("id")
        if not chat_id:
            continue
        try:
            state = await Chats.get_queue_state_by_id(chat_id)
            if state is None:
                # Chat deleted out from under the flag.
                await Chats.disarm_queue_by_id(chat_id)
                continue
            if not state.get("queue"):
                # Flag standing over an empty queue: either a pre-flag row or a
                # queue emptied by a path that predates it. Self-heal so it
                # stops being a candidate.
                await Chats.disarm_queue_by_id(chat_id)
                continue
            if state.get("draining") is not None:
                # A drain is in flight (or wedged — sweep_orphaned_drains owns
                # deciding which, and clears the marker if it is dead).
                continue

            armed_at = candidate.get("queue_armed_at") or 0
            if armed_at and (now - armed_at) > _PENDING_QUEUE_MAX_AGE_SECONDS:
                continue

            if await has_active_generation_operations(
                redis, chat_id
            ) or get_active_streams_for_chat(chat_id):
                continue

            outcome, leaf_id = await _last_turn_outcome(chat_id)
            if outcome != "clean":
                log.debug(
                    "pending-queue sweep: skipping chat %s (last turn %s, leaf %s)",
                    chat_id,
                    outcome,
                    leaf_id,
                )
                continue

            user_id = candidate.get("user_id")
            user = await Users.get_user_by_id(user_id) if user_id else None
            if user is None:
                continue

            log.info(
                "pending-queue sweep: chat %s has %d queued item(s) and nothing "
                "running — starting the drain it never got",
                chat_id,
                len(state.get("queue") or []),
            )
            if await maybe_drain_queue(app, user, chat_id):
                started += 1
        except Exception:
            log.exception("pending-queue sweep: error handling chat %s", chat_id)

    return started


async def sweep_orphaned_drains(app) -> int:
    """Recover chats whose ``draining`` marker is set but whose generation is
    no longer running (worker crash between marking and spawning, or the task
    died without clearing). For each orphan: clear the marker, which unwedges
    the chat — a stale marker fails every future drain's ownership guard, so
    until it goes nothing can advance. Restarting the queue behind it is
    ``sweep_pending_queues``' job (same tick, one policy). Returns the number of
    orphans handled.

    Safe to call periodically. Bounded by the ``draining_chats`` Redis set so it
    never scans the whole chat table.
    """
    redis = getattr(getattr(app, "state", None), "redis", None)

    # Lazy imports — these reach into the socket/task layer.
    from open_webui.tasks import has_active_generation_operations
    from open_webui.socket.main import get_active_streams_for_chat

    if redis is not None:
        try:
            chat_ids = list(await redis.smembers(_draining_set_key()))
        except Exception:
            log.exception("sweep: failed to read draining set")
            return 0
    else:
        # Single worker (no cross-process set): enumerate chats whose draining
        # marker is set directly from the DB. Bounded — only chats mid-drain carry
        # an object marker — and the per-chat orphan test below (started_at grace
        # + no live task + no active stream) prevents reclaiming a genuinely live
        # drain. This recovers a queue wedged by a crash between marking and the
        # generation's terminal handler: the in-process task registry is empty in
        # the restarted process, but the DB marker persisted, so the queue would
        # otherwise be stuck forever (the stale marker fails every future drain's
        # ownership guard).
        try:
            chat_ids = await Chats.get_chat_ids_with_draining_marker()
        except Exception:
            log.exception("sweep: failed to enumerate draining chats (no redis)")
            return 0

    handled = 0
    for chat_id in chat_ids:
        try:
            state = await Chats.get_queue_state_by_id(chat_id)
            if not state or state.get("draining") is None:
                # Marker already cleared; drop from the set.
                await _unregister_draining(redis, chat_id)
                continue

            marker = state.get("draining") or {}

            # Staleness grace: a generation that JUST started has its marker set
            # but may not have registered its task / active stream yet (the
            # detached task is scheduled, assembly + stream_version_init run a
            # beat later). Reclaiming it here would double-pop the queue. Skip
            # markers younger than the grace window — a genuine orphan is still
            # caught on the next sweep once it has aged out.
            started_at = marker.get("started_at")
            if isinstance(started_at, (int, float)) and (
                time.time() - started_at < _DRAIN_ORPHAN_GRACE_SECONDS
            ):
                continue

            has_active_generation = await has_active_generation_operations(
                redis, chat_id
            )
            active_streams = get_active_streams_for_chat(chat_id)
            if has_active_generation or active_streams:
                # A generation really is running for this chat — not orphaned.
                continue

            # Orphan: marker set, but no task and no active stream. Clear and
            # re-drive if anything is still queued.
            log.warning(
                "sweep: recovering orphaned drain for chat %s (item %s)",
                chat_id,
                marker.get("item_id"),
            )
            # UNWEDGE ONLY. Whether the queue behind this dead drain should now
            # run is not this sweep's call — it is exactly the question
            # sweep_pending_queues answers, for every stranded queue, by the same
            # rules (nothing running, last turn ended cleanly, not ancient). It
            # runs in the same tick right after this one, so recovery is not
            # delayed by deferring to it; what is avoided is a second, subtly
            # different resume policy. The previous one here ("only if the marker
            # is under 10 minutes old") also directly contradicted the point of
            # the feature: a queue is meant to survive you closing the app and
            # coming back later, and a crash is not the user changing their mind.
            await clear_draining(redis, chat_id)
            handled += 1
        except Exception:
            log.exception("sweep: error handling chat %s", chat_id)
    return handled
