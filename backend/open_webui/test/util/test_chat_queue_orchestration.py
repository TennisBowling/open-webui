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
import os
import shutil
import tempfile
import uuid

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "drain_e2e.db")
_HERE = os.path.dirname(__file__)
_DEV_DB = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "data", "webui.db"))
if os.path.exists(_DEV_DB):
    shutil.copy(_DEV_DB, _DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import pytest

from open_webui.internal.db import Base, engine  # noqa: E402
from open_webui.models.chats import Chats, ChatForm  # noqa: E402

if not os.path.exists(_DEV_DB):
    Base.metadata.create_all(bind=engine)

import open_webui.utils.chat_queue as cq  # noqa: E402
import open_webui.main as main_mod  # noqa: E402
import open_webui.tasks as tasks_mod  # noqa: E402


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
                "history": {"currentId": current_id, "messages": {current_id: {"id": current_id, "role": "assistant", "parentId": None}}},
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


@pytest.fixture(autouse=True)
def _mock_generation(monkeypatch):
    """Stub start_generation (no real LLM) and create_task, and silence the
    socket broadcast. We record spawns at the create_task level because
    start_generation returns a coroutine that create_task would normally await;
    here we close it unawaited to avoid running a real generation."""
    spawned = []

    def _fake_start_generation(chat_id, send_spec, user, **kw):
        # Return a coroutine (matches the real async signature) that create_task
        # receives. Recording happens in _fake_create_task so it fires whether
        # or not the coroutine is awaited.
        async def _noop():
            return {"status": True}

        return _noop()

    async def _fake_create_task(redis, coro, id=None):
        # Close the coroutine without running a real generation.
        if hasattr(coro, "close"):
            coro.close()
        spawned.append({"chat_id": id})
        return (str(uuid.uuid4()), None)

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(main_mod, "start_generation", _fake_start_generation, raising=True)
    monkeypatch.setattr(tasks_mod, "create_task", _fake_create_task, raising=True)
    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)
    return spawned


def test_clean_completion_pops_and_spawns_one(_mock_generation):
    chat_id = _make_chat([_item("a"), _item("b")])

    async def _run():
        return await cq.maybe_drain_queue(_App, _User(), chat_id, finished_response_id=None)

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
        first = await cq.maybe_drain_queue(_App, _User(), chat_id, finished_response_id=None)
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
        first = await cq.maybe_drain_queue(_App, _User(), chat_id, finished_response_id=None)
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
        return await cq.maybe_drain_queue(_App, _User(), chat_id, finished_response_id=None)

    assert asyncio.run(_run()) is None
    assert len(_mock_generation) == 0


def test_drain_emits_queue_updated_not_drained(monkeypatch):
    """The drain itself must emit `chat:queue:updated` (so the chip strip shrinks)
    and NOT `chat:queue:drained` — the attach signal is fired later by the
    generation, after it has persisted the user message + assistant placeholder.
    Emitting `drained` here (the old behavior) raced ahead of persistence and
    left attaching tabs showing an empty divider."""
    events = []

    def _fake_start_generation(chat_id, send_spec, user, **kw):
        # Capture that the attach ids were threaded into the send_spec so the
        # generation can fire the deferred broadcast.
        events.append({"send_spec_broadcast": send_spec.get("queue_drained_broadcast")})

        async def _noop():
            return {"status": True}

        return _noop()

    async def _fake_create_task(redis, coro, id=None):
        # Run the `_drive_generation` wrapper so it calls start_generation (which
        # is where the threaded send_spec is observable).
        await coro
        return (str(uuid.uuid4()), None)

    async def _capture_broadcast(user_id, chat_id, event_type="chat:queue:updated", **extra):
        events.append({"event_type": event_type, "extra": extra})

    monkeypatch.setattr(main_mod, "start_generation", _fake_start_generation, raising=True)
    monkeypatch.setattr(tasks_mod, "create_task", _fake_create_task, raising=True)
    monkeypatch.setattr(cq, "broadcast_queue_state", _capture_broadcast, raising=True)

    chat_id = _make_chat([_item("a")])

    async def _run():
        return await cq.maybe_drain_queue(_App, _User(), chat_id, finished_response_id=None)

    resp_id = asyncio.run(_run())
    assert resp_id is not None

    broadcast_events = [e for e in events if "event_type" in e]
    assert [e["event_type"] for e in broadcast_events] == ["chat:queue:updated"]
    assert all(e["event_type"] != "chat:queue:drained" for e in broadcast_events)

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

    async def _run_create_task(redis, coro, id=None):
        await coro  # run the wrapper so start_generation (capture) fires
        return (str(uuid.uuid4()), None)

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(main_mod, "start_generation", _fake_start_generation, raising=True)
    monkeypatch.setattr(tasks_mod, "create_task", _run_create_task, raising=True)
    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)

    # Nested (real) shape.
    chat_id = _make_chat([_item("a", content="steer me")])

    async def _run():
        return await cq.maybe_drain_queue(_App, _User(), chat_id, finished_response_id=None)

    resp_id = asyncio.run(_run())
    assert resp_id is not None
    assert len(captured) == 1
    spec = captured[0]
    assert spec["model"] == "m", "model must be read through sendSpec, not None"
    nu = spec["new_user_message"]
    assert nu["content"] == "steer me", "content must be read through sendSpec, not ''"
    assert nu["models"] == ["m"]
    assert nu["role"] == "user"

    # Flat (legacy/fallback) shape: same result via the top-level fields.
    captured.clear()
    chat_id2 = _make_chat([_flat_item("a", content="legacy")])

    async def _run2():
        return await cq.maybe_drain_queue(_App, _User(), chat_id2, finished_response_id=None)

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

    async def _run_create_task(redis, coro, id=None):
        # Actually run the wrapper coroutine so its except-branch fires.
        spawned_coros.append(asyncio.ensure_future(coro))
        return (str(uuid.uuid4()), None)

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(main_mod, "start_generation", _boom_start_generation, raising=True)
    monkeypatch.setattr(tasks_mod, "create_task", _run_create_task, raising=True)
    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)

    chat_id = _make_chat([_item("a")])

    async def _run():
        resp_id = await cq.maybe_drain_queue(_App, _User(), chat_id, finished_response_id=None)
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

    async def _run_create_task(redis, coro, id=None):
        spawned.append(asyncio.ensure_future(coro))
        return (str(uuid.uuid4()), None)

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(main_mod, "start_generation", _start_gen, raising=True)
    monkeypatch.setattr(tasks_mod, "create_task", _run_create_task, raising=True)
    monkeypatch.setattr(cq, "broadcast_queue_state", _noop_broadcast, raising=True)

    chat_id = _make_chat([_item("a"), _item("b")])

    async def _run():
        first = await cq.maybe_drain_queue(_App, _User(), chat_id, finished_response_id=None)
        await asyncio.gather(*spawned)  # first fails, clears marker
        # A fresh drain (no owner) must now pop 'b'.
        second = await cq.maybe_drain_queue(_App, _User(), chat_id, finished_response_id=None)
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
    chat_id = _make_chat([
        {"id": "s1", "mode": "steer", "sendSpec": {"model": "m", "content": "x"}},
        {"id": "a1", "mode": "after_final", "sendSpec": {"model": "m", "content": "y"}},
    ])

    async def _run():
        await cq.clear_draining(None, chat_id, finished_response_id="resp-z", user_id="user-1")

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

    chat_id = _make_chat([
        {"id": "s1", "mode": "steer", "sendSpec": {"model": "m", "content": "x"}},
    ])
    # A newer generation 'resp-NEW' owns the marker.
    Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda item: {"item_id": item["id"], "response_message_id": "resp-NEW"},
        expected_finished_response_id=None,
    )
    # Re-add a steer (the pop above consumed s1 as the "head").
    Chats.append_queue_item_by_id(
        chat_id, {"id": "s2", "mode": "steer", "sendSpec": {"model": "m", "content": "z"}}
    )

    async def _run():
        # An OLD generation 'resp-OLD' cancels — must NOT clear the newer marker
        # nor downgrade the steer.
        await cq.clear_draining(None, chat_id, finished_response_id="resp-OLD", user_id="user-1")

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
    chat_id = _make_chat([
        {"id": "s1", "mode": "steer", "sendSpec": {"model": "m", "content": "steered late"}},
    ])

    async def _run():
        return await cq.maybe_drain_queue(_App, _User(), chat_id, finished_response_id=None)

    resp_id = asyncio.run(_run())
    assert resp_id is not None  # steer popped + generation started as a follow-up
    assert len(_mock_generation) == 1
    state = Chats.get_queue_state_by_id(chat_id)
    assert state["queue"] == []
