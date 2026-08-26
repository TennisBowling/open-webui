"""End-to-end orchestration tests for the queue drain engine (maybe_drain_queue).

Unlike test_chat_queue_drain.py (which tests the model-layer atomics), these
exercise the full maybe_drain_queue path with start_generation / create_task /
the socket broadcast mocked out, validating:

* a clean completion pops the head, marks draining, and spawns ONE generation,
* CONCURRENT maybe_drain_queue calls for the same chat pop EXACTLY ONCE
  (lock + ownership guard), even with no Redis (in-process asyncio lock),
* a stale completion (wrong finished_response_id) is a no-op,
* the chained drain (owning completion → next item) advances correctly.

The DB is a throwaway copy of the migrated dev DB (see the env setup below).
"""

import asyncio
import uuid

import pytest

from test.util.db import configure_test_database

configure_test_database(required=True)

from open_webui.models.chats import Chats, ChatForm  # noqa: E402


class _SyncChats:
    def __init__(self, target):
        self._target = target

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def _call(*args, **kwargs):
            return asyncio.run(attr(*args, **kwargs))

        return _call


Chats = _SyncChats(Chats)

import open_webui.utils.chat_queue as cq  # noqa: E402
import open_webui.main as main_mod  # noqa: E402
import open_webui.tasks as tasks_mod  # noqa: E402

# Captured BEFORE the autouse fixture swaps it out: the reservation-ownership
# test below has to run the real thing, because mocking create_task is precisely
# what hid a bug where the drain bound the generation operation to its own
# wrapper task and the real generation could then never bind it.
_REAL_CREATE_TASK = tasks_mod.create_task


class _App:
    class state:
        redis = None  # exercise the in-process asyncio-lock fallback


class _User:
    id = "user-1"


def _make_chat(queue_items, current_id="m0"):
    chat = Chats.insert_new_chat(
        f"user-{uuid.uuid4()}",
        ChatForm(
            chat={
                "title": "drain e2e",
                "history": {
                    "currentId": current_id,
                    "messages": {
                        current_id: {
                            "id": current_id,
                            "role": "assistant",
                            "parentId": None,
                        }
                    },
                },
                "queue": queue_items,
            }
        ),
    )
    return chat.id


def _item(item_id, content="hello"):
    """Real frontend shape: the send payload is NESTED under `sendSpec` (see
    captureQueueSendSpec / enqueueMessage in Chat.svelte). The backend must read
    through it — a flat {"model","content"} item is the legacy/fallback shape,
    covered separately by _flat_item below."""
    return {
        "id": item_id,
        "prompt": content,
        "createdAt": 0,
        "sendSpec": {"model": "m", "content": content, "models": ["m"]},
    }


def _flat_item(item_id, content="hello"):
    """Legacy/fallback shape: fields at the top level (no sendSpec wrapper)."""
    return {"id": item_id, "model": "m", "content": content}


async def _release_fake_operation(kwargs):
    operation = kwargs.get("generation_operation")
    if operation:
        await tasks_mod.unregister_generation_operation(None, operation)


async def _run_and_release_fake(coro, kwargs):
    try:
        return await coro
    finally:
        await _release_fake_operation(kwargs)


@pytest.fixture(autouse=True)
def _mock_generation(monkeypatch):
    """Stub start_generation (no real LLM) and create_task, and silence the
    socket broadcast. We record spawns at the create_task level because
    start_generation returns a coroutine that create_task would normally await;
    here we close it unawaited to avoid running a real generation."""
    tasks_mod.generation_operations.clear()
    tasks_mod.item_generation_operations.clear()
    spawned = []

    def _fake_start_generation(chat_id, send_spec, user, **kw):
        # Return a coroutine (matches the real async signature) that create_task
        # receives. Recording happens in _fake_create_task so it fires whether
        # or not the coroutine is awaited.
        async def _noop():
            return {"status": True}

        return _noop()

    async def _fake_create_task(redis, coro, id=None, **kwargs):
        # RUN the wrapper coroutine (everything it calls is mocked, so this is
        # cheap) instead of closing it unawaited. The wrapper is where the drain
        # hands its reservation over — or hands it back if nothing claimed it —
        # and skipping it is what let "the drain binds the operation to its own
        # wrapper task" go unnoticed until it broke every real queued message.
        spawned.append({"chat_id": id})
        await _release_fake_operation(kwargs)
        try:
            await coro
        except Exception:
            pass
        return (str(uuid.uuid4()), None)

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(
        main_mod, "start_generation", _fake_start_generation, raising=True
    )
    monkeypatch.setattr(tasks_mod, "create_task", _fake_create_task, raising=True)
    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)
    yield spawned
    tasks_mod.generation_operations.clear()
    tasks_mod.item_generation_operations.clear()


def test_clean_completion_pops_and_spawns_one(_mock_generation):
    chat_id = _make_chat([_item("a"), _item("b")])

    async def _run():
        return await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )

    resp_id = asyncio.run(_run())
    assert resp_id is not None
    assert len(_mock_generation) == 1
    # 'a' popped; 'b' remains; chat marked draining with the new response id.
    state = Chats.get_queue_state_by_id(chat_id)
    assert [q["id"] for q in state["queue"]] == ["b"]
    assert state["draining"]["response_message_id"] == resp_id


def test_concurrent_drains_pop_exactly_once(_mock_generation):
    chat_id = _make_chat([_item("a"), _item("b"), _item("c")])

    async def _run():
        # Fire many concurrent drains for the SAME finished turn. Only one may
        # pop; the rest must see the lock/ownership guard and no-op.
        results = await asyncio.gather(
            *[
                cq.maybe_drain_queue(_App, _User(), chat_id, finished_response_id=None)
                for _ in range(8)
            ]
        )
        return results

    results = asyncio.run(_run())
    started = [r for r in results if r is not None]
    assert len(started) == 1, f"expected exactly one drain to start, got {len(started)}"
    assert len(_mock_generation) == 1
    state = Chats.get_queue_state_by_id(chat_id)
    # Exactly one item popped.
    assert [q["id"] for q in state["queue"]] == ["b", "c"]


def test_stale_completion_is_noop(_mock_generation):
    chat_id = _make_chat([_item("a")])

    async def _run():
        # First drain (no marker yet) pops 'a' and sets marker resp-1-ish.
        first = await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )
        # A stale completion for a DIFFERENT (already-superseded) turn must not
        # pop anything.
        stale = await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id="some-old-resp"
        )
        return first, stale

    first, stale = asyncio.run(_run())
    assert first is not None
    assert stale is None
    assert len(_mock_generation) == 1  # only the first started a generation


def test_chained_drain_advances_to_next(_mock_generation):
    chat_id = _make_chat([_item("a"), _item("b")])

    async def _run():
        first = await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )
        # The owning generation (first) finishes cleanly and drains again.
        second = await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=first
        )
        return first, second

    first, second = asyncio.run(_run())
    assert first is not None and second is not None and first != second
    assert len(_mock_generation) == 2
    state = Chats.get_queue_state_by_id(chat_id)
    assert state["queue"] == []
    assert state["draining"]["response_message_id"] == second


def test_empty_queue_is_noop(_mock_generation):
    chat_id = _make_chat([])

    async def _run():
        return await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )

    assert asyncio.run(_run()) is None
    assert len(_mock_generation) == 0


def test_drain_defers_all_broadcasts_to_the_generation(monkeypatch):
    """The drain itself must emit NOTHING — neither `chat:queue:updated` nor
    `chat:queue:drained`. The chip-clear is deliberately deferred: it fires
    atomically with the drained user message becoming a real chat bubble (the
    `queue_drained_broadcast` threaded through the send_spec; see the
    maybe_drain_queue comment and main.py's queue_drained_broadcast branch).
    Shrinking the chip at drain time (the old behavior) opened a gap where the
    queued message was in NEITHER the chip strip nor the transcript until the
    much-later attach landed — for a non-streaming upstream, the whole turn."""
    events = []

    def _fake_start_generation(chat_id, send_spec, user, **kw):
        # Capture that the attach ids were threaded into the send_spec so the
        # generation can fire the deferred broadcast.
        events.append({"send_spec_broadcast": send_spec.get("queue_drained_broadcast")})

        async def _noop():
            return {"status": True}

        return _noop()

    async def _fake_create_task(redis, coro, id=None, **kwargs):
        # Run the `_drive_generation` wrapper so it calls start_generation (which
        # is where the threaded send_spec is observable).
        await _run_and_release_fake(coro, kwargs)
        return (str(uuid.uuid4()), None)

    async def _capture_broadcast(
        user_id, chat_id, event_type="chat:queue:updated", **extra
    ):
        events.append({"event_type": event_type, "extra": extra})

    monkeypatch.setattr(
        main_mod, "start_generation", _fake_start_generation, raising=True
    )
    monkeypatch.setattr(tasks_mod, "create_task", _fake_create_task, raising=True)
    monkeypatch.setattr(cq, "broadcast_queue_state", _capture_broadcast, raising=True)

    chat_id = _make_chat([_item("a")])

    async def _run():
        return await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )

    resp_id = asyncio.run(_run())
    assert resp_id is not None

    broadcast_events = [e for e in events if "event_type" in e]
    assert broadcast_events == [], (
        "drain must not broadcast queue state itself — the chip-clear is "
        f"deferred to the generation's atomic bubble+clear emit: {broadcast_events}"
    )

    # The attach ids must be threaded to the generation for the deferred fire.
    spec_events = [e for e in events if "send_spec_broadcast" in e]
    assert len(spec_events) == 1
    spec = spec_events[0]["send_spec_broadcast"]
    assert spec is not None
    assert spec["response_message_id"] == resp_id
    assert spec["item_id"] == "a"
    assert spec["user_message_id"]


def test_drained_item_populates_content_and_model(monkeypatch):
    """REGRESSION (the invisible-drain bug): the frontend nests the send payload
    under `sendSpec`, but the backend used to read item.get("content")/("model")
    at the TOP level → empty user message + no model → a drained turn showed only
    a date divider (no user bubble, no generation). Capture the send_spec +
    new_user_message the drain builds and assert they carry content + model from
    the nested shape. Also prove the flat fallback shape still works."""
    captured = []

    def _fake_start_generation(chat_id, send_spec, user, **kw):
        captured.append(send_spec)

        async def _noop():
            return {"status": True}

        return _noop()

    async def _run_create_task(redis, coro, id=None, **kwargs):
        await _run_and_release_fake(
            coro, kwargs
        )  # run the wrapper so start_generation (capture) fires
        return (str(uuid.uuid4()), None)

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(
        main_mod, "start_generation", _fake_start_generation, raising=True
    )
    monkeypatch.setattr(tasks_mod, "create_task", _run_create_task, raising=True)
    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)

    # Nested (real) shape.
    chat_id = _make_chat([_item("a", content="steer me")])

    async def _run():
        return await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )

    resp_id = asyncio.run(_run())
    assert resp_id is not None
    assert len(captured) == 1
    spec = captured[0]
    assert spec["model"] == "m", "model must be read through sendSpec, not None"
    nu = spec["new_user_message"]
    assert nu["content"] == "steer me", "content must be read through sendSpec, not ''"
    assert nu["models"] == ["m"]
    assert nu["role"] == "user"
    assert nu["parentId"] == "m0"
    assert spec["leaf_message_id"] == nu["id"], (
        "assembly must walk through the queued user row; using its assistant "
        "parent makes the provider request end with a model turn"
    )
    from open_webui.utils.chat import assemble_conversation_from_leaf

    assembled = asyncio.run(
        assemble_conversation_from_leaf(
            chat_id,
            spec["leaf_message_id"],
            new_user_message=nu,
        )
    )
    assert assembled[-1]["role"] == "user"
    assert assembled[-1]["content"] == "steer me"

    # Flat (legacy/fallback) shape: same result via the top-level fields.
    captured.clear()
    chat_id2 = _make_chat([_flat_item("a", content="legacy")])

    async def _run2():
        return await cq.maybe_drain_queue(
            _App, _User(), chat_id2, finished_response_id=None
        )

    resp_id2 = asyncio.run(_run2())
    assert resp_id2 is not None
    assert len(captured) == 1
    assert captured[0]["model"] == "m"
    assert captured[0]["new_user_message"]["content"] == "legacy"


def test_draining_cleared_when_generation_fails(monkeypatch):
    """If the spawned headless generation raises, the `_drive_generation` wrapper
    must clear THIS chat's draining marker so the queue isn't wedged. We run the
    wrapper coroutine for real (instead of closing it) and let start_generation
    raise."""

    def _boom_start_generation(chat_id, send_spec, user, **kw):
        async def _raise():
            raise RuntimeError("provider exploded")

        return _raise()

    spawned_coros = []

    async def _run_create_task(redis, coro, id=None, **kwargs):
        # Actually run the wrapper coroutine so its except-branch fires.
        spawned_coros.append(asyncio.ensure_future(_run_and_release_fake(coro, kwargs)))
        return (str(uuid.uuid4()), None)

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(
        main_mod, "start_generation", _boom_start_generation, raising=True
    )
    monkeypatch.setattr(tasks_mod, "create_task", _run_create_task, raising=True)
    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)

    chat_id = _make_chat([_item("a")])

    async def _run():
        resp_id = await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )
        # Let the spawned wrapper run to completion (it should clear draining).
        await asyncio.gather(*spawned_coros)
        return resp_id

    resp_id = asyncio.run(_run())
    assert resp_id is not None
    state = Chats.get_queue_state_by_id(chat_id)
    # Marker cleared by the wrapper's failure handler; queue already popped 'a'.
    assert state["draining"] is None
    # And the failure was surfaced on the assistant message (not silently
    # vanished) so a reload shows an error row, not a user bubble with no reply.
    msg = Chats.get_message_by_id_and_message_id(chat_id, resp_id)
    assert msg is not None
    parent = Chats.get_message_by_id_and_message_id(chat_id, msg.get("parentId"))
    assert parent is not None
    assert parent.get("role") == "user"
    assert msg.get("error")
    assert "provider exploded" in str(msg["error"])
    assert msg.get("done") is True


def test_failed_drain_still_allows_next_drain(monkeypatch):
    """After a failed headless drain clears its marker, a SUBSEQUENT drain (e.g.
    the user's next interactive completion) must be able to pop the next item —
    proving the ownership guard isn't left wedged by the failure."""

    calls = {"n": 0}

    def _start_gen(chat_id, send_spec, user, **kw):
        async def _maybe_raise():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first item model gone")
            return {"status": True}

        return _maybe_raise()

    spawned = []

    async def _run_create_task(redis, coro, id=None, **kwargs):
        spawned.append(asyncio.ensure_future(_run_and_release_fake(coro, kwargs)))
        return (str(uuid.uuid4()), None)

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(main_mod, "start_generation", _start_gen, raising=True)
    monkeypatch.setattr(tasks_mod, "create_task", _run_create_task, raising=True)
    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)

    chat_id = _make_chat([_item("a"), _item("b")])

    async def _run():
        first = await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )
        await asyncio.gather(*spawned)  # first fails, clears marker
        # A fresh drain (no owner) must now pop 'b'.
        second = await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )
        await asyncio.gather(*spawned)
        return first, second

    first, second = asyncio.run(_run())
    assert first is not None and second is not None and first != second
    state = Chats.get_queue_state_by_id(chat_id)
    assert state["queue"] == []  # both items consumed
    assert state["draining"]["response_message_id"] == second


def test_clear_draining_downgrades_steers_when_marker_clear(monkeypatch):
    """A cancelled/errored generation's clear_draining downgrades pending steers
    to after_final (so they don't leak into the next unrelated response) — but
    ONLY when no newer generation owns the draining marker."""

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)

    # Queue a steer + an after_final; no draining marker set (clean-ish cancel).
    chat_id = _make_chat(
        [
            {"id": "s1", "mode": "steer", "sendSpec": {"model": "m", "content": "x"}},
            {
                "id": "a1",
                "mode": "after_final",
                "sendSpec": {"model": "m", "content": "y"},
            },
        ]
    )

    async def _run():
        await cq.clear_draining(
            None, chat_id, finished_response_id="resp-z", user_id="user-1"
        )

    asyncio.run(_run())
    state = Chats.get_queue_state_by_id(chat_id)
    modes = {q["id"]: q.get("mode") for q in state["queue"]}
    assert modes == {"s1": "after_final", "a1": "after_final"}


def test_clear_draining_keeps_steers_when_newer_generation_owns_marker(monkeypatch):
    """If a NEWER generation owns the draining marker (this clear_draining is a
    stale/superseded completion), the steers belong to that live generation and
    MUST NOT be downgraded — it will consume them at its own boundary."""

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)

    chat_id = _make_chat(
        [
            {"id": "s1", "mode": "steer", "sendSpec": {"model": "m", "content": "x"}},
        ]
    )
    # A newer generation 'resp-NEW' owns the marker.
    Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda item: {"item_id": item["id"], "response_message_id": "resp-NEW"},
        expected_finished_response_id=None,
    )
    # Re-add a steer (the pop above consumed s1 as the "head").
    Chats.append_queue_item_by_id(
        chat_id,
        {"id": "s2", "mode": "steer", "sendSpec": {"model": "m", "content": "z"}},
    )

    async def _run():
        # An OLD generation 'resp-OLD' cancels — must NOT clear the newer marker
        # nor downgrade the steer.
        await cq.clear_draining(
            None, chat_id, finished_response_id="resp-OLD", user_id="user-1"
        )

    asyncio.run(_run())
    state = Chats.get_queue_state_by_id(chat_id)
    # Marker still owned by resp-NEW; the steer is left intact for it.
    assert state["draining"] is not None
    assert state["draining"]["response_message_id"] == "resp-NEW"
    s2 = [q for q in state["queue"] if q["id"] == "s2"]
    assert s2 and s2[0]["mode"] == "steer"


def test_drain_head_pop_consumes_steer_as_followup_fallback(_mock_generation):
    """No-boundary fallback: when a generation finishes cleanly with a steer item
    still at the queue head (the loop never reached another tool boundary to
    inject it), the drain pops it as a normal follow-up generation. This is the
    approved 'steer with no boundary -> send as follow-up' behavior. The send_spec
    builder ignores `mode`, so it generates correctly."""
    chat_id = _make_chat(
        [
            {
                "id": "s1",
                "mode": "steer",
                "sendSpec": {"model": "m", "content": "steered late"},
            },
        ]
    )

    async def _run():
        return await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )

    resp_id = asyncio.run(_run())
    assert resp_id is not None  # steer popped + generation started as a follow-up
    assert len(_mock_generation) == 1
    state = Chats.get_queue_state_by_id(chat_id)
    assert state["queue"] == []


def test_stop_intent_pauses_drain_on_clean_completion(_mock_generation):
    """C03: if the finishing response was user-stopped, a 'clean' completion must
    NOT auto-drain the queue (the wedged/cross-worker stop case) — it pauses,
    leaving the queue intact."""
    chat_id = _make_chat([_item("a")])
    # Persist the finishing assistant message as user-stopped (the durable flag
    # the backend Stop path writes and _was_user_stopped reads).
    Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        "resp-stopped",
        {"role": "assistant", "done": True, "userStopped": True},
        return_model=False,
    )

    async def _run():
        return await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id="resp-stopped"
        )

    resp_id = asyncio.run(_run())
    assert resp_id is None, "a user-stopped completion must not drain"
    assert len(_mock_generation) == 0
    state = Chats.get_queue_state_by_id(chat_id)
    assert [q["id"] for q in state["queue"]] == ["a"], "queue left intact (paused)"
    assert state["draining"] is None


def test_clean_completion_not_stopped_still_drains(_mock_generation):
    """Control for the above: a clean completion whose message is NOT user-stopped
    drains normally."""
    chat_id = _make_chat([_item("a")])
    Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        "resp-ok",
        {"role": "assistant", "done": True},
        return_model=False,
    )

    async def _run():
        return await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id="resp-ok"
        )

    assert asyncio.run(_run()) is not None
    assert len(_mock_generation) == 1


def test_manual_drain_blocked_by_inflight_generation(monkeypatch):
    """C02 in-flight guard: a manual drain (finished_response_id=None, e.g. Send
    now) must NOT pop+spawn while a normal generation is live for the chat — a
    normal turn sets no draining marker, so the marker ownership guard can't see
    it."""
    import open_webui.socket.main as socket_main

    spawned = []

    def _fake_start_generation(chat_id, send_spec, user, **kw):
        async def _noop():
            return {"status": True}

        return _noop()

    async def _fake_create_task(redis, coro, id=None, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        await _release_fake_operation(kwargs)
        spawned.append(id)
        return (str(uuid.uuid4()), None)

    async def _noop_broadcast(*a, **k):
        return None

    async def _has_live_generation(redis, cid):
        return True

    monkeypatch.setattr(
        main_mod, "start_generation", _fake_start_generation, raising=True
    )
    monkeypatch.setattr(tasks_mod, "create_task", _fake_create_task, raising=True)
    monkeypatch.setattr(
        tasks_mod,
        "has_active_generation_operations",
        _has_live_generation,
        raising=True,
    )
    monkeypatch.setattr(
        socket_main, "get_active_streams_for_chat", lambda cid: [], raising=True
    )
    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)

    chat_id = _make_chat([_item("a")])

    async def _run():
        return await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )

    assert asyncio.run(_run()) is None, "manual drain must not pop while a gen is live"
    assert spawned == []
    assert [q["id"] for q in Chats.get_queue_state_by_id(chat_id)["queue"]] == ["a"]


def test_manual_drain_proceeds_when_idle(monkeypatch):
    """Control: with no live task/stream, a manual drain pops + spawns."""
    import open_webui.socket.main as socket_main

    spawned = []

    def _fake_start_generation(chat_id, send_spec, user, **kw):
        async def _noop():
            return {"status": True}

        return _noop()

    async def _fake_create_task(redis, coro, id=None, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        await _release_fake_operation(kwargs)
        spawned.append(id)
        return (str(uuid.uuid4()), None)

    async def _noop_broadcast(*a, **k):
        return None

    async def _no_generation(redis, cid):
        return False

    monkeypatch.setattr(
        main_mod, "start_generation", _fake_start_generation, raising=True
    )
    monkeypatch.setattr(tasks_mod, "create_task", _fake_create_task, raising=True)
    monkeypatch.setattr(
        tasks_mod,
        "has_active_generation_operations",
        _no_generation,
        raising=True,
    )
    monkeypatch.setattr(
        socket_main, "get_active_streams_for_chat", lambda cid: [], raising=True
    )
    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)

    chat_id = _make_chat([_item("a")])

    async def _run():
        return await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )

    assert asyncio.run(_run()) is not None
    assert len(spawned) == 1


# ---------------------------------------------------------------------------
# sweep_pending_queues: the reconciler that makes a queue independent of the
# completion event that was supposed to drain it.
# ---------------------------------------------------------------------------


def _arm_queue(chat_id, *items):
    """Enqueue through the real mutator so `queue_armed_at` is set the way the
    running system sets it (the reconciler finds chats by that column)."""
    for item in items:
        Chats.append_queue_item_by_id(chat_id, item)


def _set_leaf(chat_id, message_id, **fields):
    """Persist the chat's head assistant message with the given terminal state
    and point history.currentId at it."""
    Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id, message_id, {"role": "assistant", **fields}, return_model=False
    )
    Chats.set_history_current_id_atomic(chat_id, message_id)


def _clear_all_armed():
    """Start each sweep test from a clean slate. The reconciler is deliberately
    bounded to a couple of starts per pass (a backlog is spread over passes
    rather than arriving as a burst), so chats armed by EARLIER tests in the
    same database would otherwise consume this pass's budget."""
    from open_webui.internal.db import get_db, run_sync_db
    from sqlalchemy import text as _text

    def _clear():
        with get_db() as _db:
            _db.execute(_text("UPDATE chat SET queue_armed_at = NULL"))
            _db.commit()

    asyncio.run(run_sync_db(_clear))


def _patch_sweep_env(monkeypatch, user=None):
    import open_webui.socket.main as socket_main
    import open_webui.models.users as users_mod

    _clear_all_armed()

    async def _no_generation(redis, chat_id):
        return False

    async def _get_user(_uid):
        return user if user is not None else _User()

    monkeypatch.setattr(
        tasks_mod, "has_active_generation_operations", _no_generation, raising=True
    )
    monkeypatch.setattr(
        socket_main, "get_active_streams_for_chat", lambda cid: [], raising=True
    )
    monkeypatch.setattr(users_mod.Users, "get_user_by_id", _get_user, raising=False)


def test_pending_sweep_drains_a_queue_whose_trigger_was_missed(
    _mock_generation, monkeypatch
):
    """THE regression: a completed, clean turn with a queued follow-up that never
    got drained (worker restarted mid-handoff, drain lock contended, completion
    took a path that skipped the trigger). Nothing else in the system will ever
    look at that queue again — the orphan sweep only sees chats with a draining
    MARKER. The reconciler must start it."""
    _patch_sweep_env(monkeypatch)
    chat_id = _make_chat([])
    _arm_queue(chat_id, _item("a"))
    _set_leaf(chat_id, "resp-done", done=True)

    started = asyncio.run(cq.sweep_pending_queues(_App))

    assert started == 1
    assert len(_mock_generation) == 1
    state = Chats.get_queue_state_by_id(chat_id)
    assert state["queue"] == [], "the queued item was popped and started"


def test_pending_sweep_does_not_resume_a_stopped_turn(_mock_generation, monkeypatch):
    """Stop means stop. The reconciler must not undo the deliberate pause a user
    Stop leaves behind — it reads the same durable outcome (userStopped) the rest
    of the system does, which is why it needs no pause flag of its own."""
    _patch_sweep_env(monkeypatch)
    chat_id = _make_chat([])
    _arm_queue(chat_id, _item("a"))
    _set_leaf(chat_id, "resp-stopped", done=True, userStopped=True)

    assert asyncio.run(cq.sweep_pending_queues(_App)) == 0
    assert len(_mock_generation) == 0
    assert [q["id"] for q in Chats.get_queue_state_by_id(chat_id)["queue"]] == ["a"]


def test_pending_sweep_does_not_resume_an_errored_turn(_mock_generation, monkeypatch):
    """Same for a turn that failed: the queue pauses so the user can intervene
    instead of the failure cascading through every queued message."""
    _patch_sweep_env(monkeypatch)
    chat_id = _make_chat([])
    _arm_queue(chat_id, _item("a"))
    _set_leaf(chat_id, "resp-err", done=True, error={"content": "boom"})

    assert asyncio.run(cq.sweep_pending_queues(_App)) == 0
    assert len(_mock_generation) == 0


def test_pending_sweep_waits_for_an_unfinished_turn(_mock_generation, monkeypatch):
    """A head message that isn't done yet means a turn is (or was) in flight;
    draining now would either race it or parent the follow-up wrongly."""
    _patch_sweep_env(monkeypatch)
    chat_id = _make_chat([])
    _arm_queue(chat_id, _item("a"))
    _set_leaf(chat_id, "resp-live", done=False)

    assert asyncio.run(cq.sweep_pending_queues(_App)) == 0
    assert len(_mock_generation) == 0


def test_pending_sweep_skips_a_chat_with_a_live_drain_marker(
    _mock_generation, monkeypatch
):
    """A drain already in flight owns the chat; the orphan sweep decides whether
    that marker is dead, not this one."""
    _patch_sweep_env(monkeypatch)
    chat_id = _make_chat([])
    _arm_queue(chat_id, _item("a"), _item("b"))
    _set_leaf(chat_id, "resp-done", done=True)
    Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id, lambda item: {"item_id": item["id"], "response_message_id": "other"}
    )

    assert asyncio.run(cq.sweep_pending_queues(_App)) == 0
    assert len(_mock_generation) == 0


def test_pending_sweep_self_heals_a_flag_over_an_empty_queue(monkeypatch):
    """Belt and braces: if the flag is ever left standing over an empty queue it
    must disarm itself rather than making the chat a permanent candidate."""
    _patch_sweep_env(monkeypatch)
    chat_id = _make_chat([])
    _arm_queue(chat_id, _item("a"))
    Chats.remove_queue_item_by_id(chat_id, "a")
    # Re-arm by raw SQL to simulate drift: a row armed while its queue is empty
    # (a pre-flag row, or a queue emptied by a path written before the flag).
    from open_webui.internal.db import get_db, run_sync_db
    from sqlalchemy import text as _text

    def _force_arm():
        with get_db() as _db:
            _db.execute(
                _text("UPDATE chat SET queue_armed_at = 1 WHERE id = :id"),
                {"id": chat_id},
            )
            _db.commit()

    asyncio.run(run_sync_db(_force_arm))
    assert any(c["id"] == chat_id for c in Chats.get_armed_queue_chats(limit=500))

    asyncio.run(cq.sweep_pending_queues(_App))

    assert not any(c["id"] == chat_id for c in Chats.get_armed_queue_chats(limit=500))


def test_drain_leaves_the_reservation_for_the_real_generation(monkeypatch):
    """REGRESSION: "generation operation was lost before task registration".

    The drain reserves a generation operation and then spawns a wrapper task
    that calls start_generation. It must NOT bind that reservation to the
    wrapper: `_chat_completion_with_operation` copies whatever task id the
    registry holds onto its own operation, and the create_task that spawns the
    ACTUAL generation then tries to bind a second task id —
    `bind_generation_operation_task` refuses (a bound operation cannot be
    rebound), create_task raises, and the queued/steer message surfaces that
    RuntimeError as its answer. Every real drain hit this; the other tests in
    this file mock create_task, so nothing ever bound anything.

    Runs the REAL create_task and asserts the operation is still unclaimed
    (task_id == "") by the time start_generation is reached — i.e. the
    generation is free to take ownership of it.
    """
    monkeypatch.setattr(tasks_mod, "create_task", _REAL_CREATE_TASK, raising=True)

    seen = {}

    async def _fake_start_generation(chat_id, send_spec, user, **kw):
        operation = kw.get("generation_operation") or {}
        generation_id = operation.get("generation_id")
        live = tasks_mod.generation_operations.get(generation_id)
        seen["generation_id"] = generation_id
        seen["registered"] = live is not None
        seen["task_id"] = str((live or {}).get("task_id") or "")
        return {"status": True}

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(
        main_mod, "start_generation", _fake_start_generation, raising=True
    )
    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)

    chat_id = _make_chat([_item("a")])

    async def _run():
        resp_id = await cq.maybe_drain_queue(
            _App, _User(), chat_id, finished_response_id=None
        )
        # let the spawned wrapper task run to completion
        for _ in range(50):
            if "task_id" in seen:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)
        return resp_id

    resp_id = asyncio.run(_run())

    assert resp_id is not None
    assert seen.get("registered") is True, "the reservation must still be registered"
    assert seen.get("task_id") == "", (
        "the drain's wrapper task must not claim the reservation — the real "
        "generation task is what binds it"
    )
    # And once nobody claimed it, the wrapper hands it back rather than leaving
    # the chat's turn lease held forever.
    assert seen["generation_id"] not in tasks_mod.generation_operations
