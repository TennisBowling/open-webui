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


def _current_leaf_id(chat_id: str) -> Optional[str]:
    """Return the chat's current head message id (history.currentId), read from
    the raw blob (the message table is peeled out but currentId stays). This is
    the parent the next queued user message hangs off of."""
    try:
        chat = Chats.get_chat_by_id(chat_id)
        if not chat or not isinstance(chat.chat, dict):
            return None
        history = chat.chat.get("history") or {}
        return history.get("currentId")
    except Exception:
        return None


def _build_send_spec_from_item(
    item: dict,
    response_message_id: str,
    leaf_message_id: Optional[str],
    new_user_message: dict,
) -> dict:
    """Translate a persisted queue item into a ``start_generation`` send_spec.

    The queue item is self-contained (snapshotted at enqueue time). We map its
    fields onto the form the request-free entrypoint expects, stamping the fresh
    assistant ``response_message_id``, the drain-time leaf, and the freshly-built
    user message. The payload is read through ``_item_spec`` because the frontend
    nests it under ``sendSpec`` (flat items / fixtures still work).
    """
    spec_src = _item_spec(item)
    spec: dict = {
        "model": spec_src.get("model"),
        "leaf_message_id": leaf_message_id,
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


async def broadcast_queue_state(user_id: str, chat_id: str, event_type: str = "chat:queue:updated", **extra) -> None:
    """Emit the current queue + draining state for a chat to all of the user's
    tabs so the reflector UI updates. ``event_type`` is ``chat:queue:updated``
    for plain queue mutations or ``chat:queue:drained`` when a queued item just
    started generating (``extra`` then carries item_id / response_message_id)."""
    if not user_id or not chat_id or str(chat_id).startswith("local:"):
        return
    try:
        state = Chats.get_queue_state_by_id(chat_id) or {}
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
    steer reaches when the model finishes with no more tools). ``user_id`` (when
    known) lets us broadcast the resulting chip-strip change to the user's tabs.
    """
    Chats.clear_draining_by_id(chat_id, finished_response_id)
    # Only drop from the cross-worker draining set when the chat is no longer
    # marked (a newer generation may have re-marked it).
    state = Chats.get_queue_state_by_id(chat_id)
    marker_now_clear = not state or state.get("draining") is None
    if marker_now_clear:
        await _unregister_draining(redis, chat_id)

    # Downgrade orphaned steers — but ONLY when no generation is currently
    # marked draining for this chat. If a newer generation owns the marker (this
    # terminal call was a stale/superseded completion, ownership-guarded above),
    # that generation is still live and MUST be allowed to consume the steers at
    # its own tool boundary — converting them here would wrongly strip them from
    # an in-flight response. Best-effort; never break the caller.
    if marker_now_clear and not str(chat_id).startswith("local:"):
        try:
            converted = Chats.convert_steer_items_to_after_final_by_id(chat_id)
            if converted:
                resolved_user_id = user_id
                if resolved_user_id is None:
                    chat = Chats.get_chat_by_id(chat_id)
                    resolved_user_id = getattr(chat, "user_id", None)
                if resolved_user_id:
                    await broadcast_queue_state(resolved_user_id, chat_id)
        except Exception:
            log.debug(
                "steer→after_final downgrade in clear_draining failed for %s",
                chat_id,
                exc_info=True,
            )


async def _finalize_failed_headless_drain(
    redis,
    user,
    chat_id: str,
    response_message_id: str,
    user_message_id: Optional[str],
    item: dict,
    error: Exception,
) -> None:
    """A headless drain failed BEFORE process_chat_response ran (e.g. the queued
    model was removed / access revoked, or conversation assembly raised). The
    user message was already persisted by assembly, so surface an error on the
    assistant message — otherwise the queued turn silently shows a user bubble
    with no response and no explanation. Then clear our draining marker and tell
    tabs to reload. All best-effort; never raises."""
    detail = getattr(error, "detail", None) or str(error) or "Generation failed"
    try:
        Chats.upsert_message_to_chat_by_id_and_message_id(
            chat_id,
            response_message_id,
            {
                "role": "assistant",
                "model": item.get("model"),
                "parentId": user_message_id,
                "content": "",
                "error": {"content": str(detail)},
                "done": True,
            },
            return_model=False,
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
    # Nudge tabs to reload so the error row appears (the chip strip is already
    # correct from the earlier chat:queue:updated emitted by maybe_drain_queue).
    try:
        await broadcast_queue_state(
            getattr(user, "id", None),
            chat_id,
            event_type="chat:queue:drained",
            response_message_id=response_message_id,
            user_message_id=user_message_id,
        )
    except Exception:
        log.debug("failed-drain broadcast failed for %s", chat_id, exc_info=True)


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

    try:
        # Stamp a fresh assistant message id for the popped item under the lock.
        new_response_id = str(uuid.uuid4())

        def _marker(item: dict) -> dict:
            return {
                "item_id": item.get("id"),
                "response_message_id": new_response_id,
                "started_at": int(time.time()),
            }

        result = Chats.pop_queue_head_and_mark_draining_by_id(
            chat_id, _marker, expected_finished_response_id=finished_response_id
        )
        if not result or result.get("item") is None:
            # Already superseded, empty queue, or chat gone — nothing to do.
            # If the queue is now empty the marker was cleared by the pop helper;
            # reflect that in the cross-worker set.
            if result is not None and result.get("draining") is None:
                await _unregister_draining(redis, chat_id)
            return None

        item = result["item"]
        await _register_draining(redis, chat_id)

        # Resolve the new user message's parent at DRAIN TIME, not enqueue time.
        # When the user queued this item, the previous turn's assistant message
        # didn't exist yet — so the queue item can't know its parent. The parent
        # is the chat's current head (history.currentId), which the just-finished
        # generation stamped to its own assistant message. This mirrors the old
        # client dequeueAndSend, which parented the queued user message off
        # history.currentId at send time.
        leaf_message_id = _current_leaf_id(chat_id)

        # Build the new user message fresh: snapshot content/files/models from the
        # queue item, assign a fresh id + the resolved parent. Read through
        # _item_spec because the frontend nests the payload under `sendSpec`.
        spec_src = _item_spec(item)
        new_user_message_id = str(uuid.uuid4())
        new_user_message = {
            "id": new_user_message_id,
            "parentId": leaf_message_id,
            "role": "user",
            "content": spec_src.get("content") or "",
            "files": spec_src.get("files") or [],
            "models": spec_src.get("models")
            or ([spec_src.get("model")] if spec_src.get("model") else []),
        }

        send_spec = _build_send_spec_from_item(
            item, new_response_id, leaf_message_id, new_user_message
        )
        # Carry the attach ids so the generation can fire `chat:queue:drained`
        # AFTER it has persisted the user message + assistant placeholder, rather
        # than here (which raced ahead of persistence and left tabs blank).
        send_spec["queue_drained_broadcast"] = {
            "item_id": item.get("id"),
            "user_message_id": new_user_message_id,
            "response_message_id": new_response_id,
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
                await start_generation(chat_id, send_spec, user)
            except Exception as e:
                log.exception(
                    "headless start_generation failed for chat %s (response %s)",
                    chat_id,
                    new_response_id,
                )
                await _finalize_failed_headless_drain(
                    redis, user, chat_id, new_response_id, new_user_message_id, item, e
                )

        # Spawn a NEW detached task keyed by chat_id so getTaskIdsByChatId / stop
        # / resume-polling discover it, and so the finishing task can unwind its
        # own cleanup independently.
        await create_task(
            redis,
            _drive_generation(),
            id=chat_id,
        )

        log.info(
            "drained queue item %s for chat %s -> response %s",
            item.get("id"),
            chat_id,
            new_response_id,
        )

        # Reflect the popped queue to every tab immediately so the queue chip
        # strip shrinks. The `chat:queue:drained` ATTACH signal is fired later by
        # the generation itself (start_generation → process_chat), once the new
        # user message + assistant placeholder + stream state exist — so a tab's
        # loadChat() in response finds real state, not an empty divider.
        await broadcast_queue_state(
            getattr(user, "id", None),
            chat_id,
            event_type="chat:queue:updated",
        )
        return new_response_id
    except Exception:
        log.exception("maybe_drain_queue failed for chat %s", chat_id)
        # Best-effort: clear the marker we may have set so the queue isn't
        # wedged. The sweeper is the backstop if even this fails.
        try:
            await clear_draining(redis, chat_id)
        except Exception:
            pass
        return None
    finally:
        await lock.release()


async def sweep_orphaned_drains(app) -> int:
    """Recover chats whose ``draining`` marker is set but whose generation is
    no longer running (worker crash between marking and spawning, or the task
    died without clearing). For each orphan: clear the marker and re-attempt the
    drain so the queue keeps moving. Returns the number of orphans handled.

    Safe to call periodically. Bounded by the ``draining_chats`` Redis set so it
    never scans the whole chat table.
    """
    redis = getattr(getattr(app, "state", None), "redis", None)

    # Lazy imports — these reach into the socket/task layer.
    from open_webui.tasks import list_task_ids_by_item_id
    from open_webui.socket.main import get_active_streams_for_chat
    from open_webui.models.users import Users

    if redis is not None:
        try:
            chat_ids = list(await redis.smembers(_draining_set_key()))
        except Exception:
            log.exception("sweep: failed to read draining set")
            return 0
    else:
        # Single worker: no cross-process set. The in-process marker is only at
        # risk if THIS worker crashed, in which case nothing is running anyway.
        # We can't cheaply enumerate, so single-worker relies on next-completion
        # / tab-load recovery. Nothing to sweep here.
        return 0

    handled = 0
    for chat_id in chat_ids:
        try:
            state = Chats.get_queue_state_by_id(chat_id)
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

            task_ids = await list_task_ids_by_item_id(redis, chat_id)
            active_streams = get_active_streams_for_chat(chat_id)
            if task_ids or active_streams:
                # A generation really is running for this chat — not orphaned.
                continue

            # Orphan: marker set, but no task and no active stream. Clear and
            # re-drive if anything is still queued.
            log.warning(
                "sweep: recovering orphaned drain for chat %s (item %s)",
                chat_id,
                marker.get("item_id"),
            )
            await clear_draining(redis, chat_id)

            queue = state.get("queue") or []
            if queue:
                # Need the owning user to start a generation. Resolve from the
                # chat row.
                chat = Chats.get_chat_by_id(chat_id)
                user = (
                    Users.get_user_by_id(chat.user_id)
                    if chat and getattr(chat, "user_id", None)
                    else None
                )
                if user is not None:
                    await maybe_drain_queue(app, user, chat_id)
            handled += 1
        except Exception:
            log.exception("sweep: error handling chat %s", chat_id)
    return handled
