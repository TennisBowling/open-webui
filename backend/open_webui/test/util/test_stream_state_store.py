"""Unit tests for the in-memory stream-state store copy-on-write semantics in
``socket.main.set_stream_state`` / ``get_stream_state``.

The per-token streaming flush calls ``set_stream_state(msg_id, {"content_blocks":
_strip_tool_results(content_blocks), ...})`` on every flushed delta. The patch's
text/reasoning blocks are SHARED BY REFERENCE with the live ``content_blocks``
list, whose tail block the loop keeps mutating in place. Two invariants the store
must uphold, exercised here:

1. **Snapshot immutability:** once a state is stored, a later in-place mutation of
   the live ``content_blocks`` (or its tail block) must NOT change the stored
   snapshot. Otherwise a mid-generation reload could read a snapshot whose content
   races ahead of its version and silently drop buffered deltas.
2. **No needless deep-copy of the accumulated state:** the optimization removed
   the per-write deepcopy of the *previous* snapshot (it was overwritten by the
   patch anyway). The accumulated, unpatched keys are carried forward by value.

``socket.main`` binds the DB engine at import, so DATABASE_URL is pointed at a
throwaway copy of the migrated dev DB before importing (same pattern as
``test_chat_queue_drain.py``). No Redis is configured, so the stores are plain
in-memory dicts — exactly the single-worker deployment under optimization.
"""

import copy
import os
import asyncio
from pathlib import Path

from test.util.db import configure_test_database

configure_test_database()
# Ensure no Redis → in-memory stores (the single-worker path under test).
os.environ.pop("WEBSOCKET_REDIS_URL", None)

from open_webui.socket.main import (  # noqa: E402
    set_stream_state,
    get_stream_state,
    STREAM_STATE,
    set_tool_result_body,
    get_tool_result_body,
    get_tool_result_bodies,
    stream_version_init,
    stream_version_incr,
    stream_version_get,
    stream_version_flush,
    append_stream_replay_event,
    get_stream_replay_events,
    get_active_streams_for_chat,
)
from open_webui.socket import main as socket_main  # noqa: E402


def _mk_message_id(name: str) -> str:
    # Keep ids unique per test so the module-level store doesn't bleed across
    # tests (the store is process-global by design).
    return f"test-stream-{name}"


class _FakeReplayRedis:
    def __init__(self):
        self.lists = {}
        self.values = {}
        self.expirations = []
        self.lrange_calls = 0

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(str(value))
        return len(self.lists[key])

    async def incrby(self, key, amount):
        self.values[key] = int(self.values.get(key, 0) or 0) + int(amount)
        return self.values[key]

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def lpop(self, key):
        values = self.lists.get(key, [])
        if not values:
            return None
        return values.pop(0)

    async def ltrim(self, key, start, end):
        values = self.lists.get(key, [])
        n = len(values)
        start = n + start if start < 0 else start
        end = n + end if end < 0 else end
        start = max(0, start)
        end = min(n - 1, end)
        self.lists[key] = values[start : end + 1] if start <= end and start < n else []
        return True

    async def lrange(self, key, start, end):
        self.lrange_calls += 1
        values = self.lists.get(key, [])
        n = len(values)
        start = n + start if start < 0 else start
        end = n + end if end < 0 else end
        start = max(0, start)
        end = min(n - 1, end)
        return values[start : end + 1] if start <= end and start < n else []

    async def set(self, key, value):
        self.values[key] = int(value)
        return True

    async def expire(self, key, ttl):
        self.expirations.append((key, ttl))
        return True

    async def delete(self, *keys):
        for key in keys:
            self.lists.pop(key, None)
            self.values.pop(key, None)
        return len(keys)


def test_stored_snapshot_is_immutable_under_live_tail_mutation():
    mid = _mk_message_id("immutable")
    tail = {"type": "text", "content": "hello"}
    live_blocks = [{"type": "text", "content": "intro"}, tail]

    set_stream_state(mid, {"content_blocks": live_blocks, "status": "in_progress"})

    # Simulate the streaming loop: keep appending to the live tail in place.
    tail["content"] = "hello world"
    live_blocks.append({"type": "text", "content": "more"})

    snap = get_stream_state(mid)
    assert snap["content_blocks"][1]["content"] == "hello"  # frozen at store time
    assert len(snap["content_blocks"]) == 2  # the later append is not reflected


def test_accumulated_keys_carry_forward_without_patch():
    mid = _mk_message_id("carryforward")
    set_stream_state(mid, {"chat_id": "c1", "content_blocks": [{"type": "text", "content": "a"}]})
    # A second patch that does NOT include chat_id must preserve it.
    set_stream_state(mid, {"status": "in_progress"})

    snap = get_stream_state(mid)
    assert snap["chat_id"] == "c1"
    assert snap["status"] == "in_progress"
    assert snap["content_blocks"][0]["content"] == "a"


def test_later_patch_does_not_mutate_caller_patch_object():
    mid = _mk_message_id("patchsafe")
    patch = {"content_blocks": [{"type": "text", "content": "x"}], "status": "in_progress"}
    set_stream_state(mid, patch)

    # Mutating the original patch object after the call must not affect the store
    # (the store deep-copied the patch).
    patch["content_blocks"][0]["content"] = "MUTATED"

    snap = get_stream_state(mid)
    assert snap["content_blocks"][0]["content"] == "x"


def test_headless_stream_registration_is_reattachable():
    """A headless drain registers its stream with session_id=None. The active
    stream must still be discoverable for the chat (so a tab that didn't start
    the generation can reattach via /active + /snapshot)."""
    chat_id = "test-headless-chat"
    mid = _mk_message_id("headless-reattach")

    stream_version_init(
        mid,
        chat_id=chat_id,
        user_id="user-1",
        session_id=None,
        content_blocks=[],
    )

    active = get_active_streams_for_chat(chat_id)
    assert any(s["message_id"] == mid for s in active), (
        "headless stream not discoverable for its chat"
    )
    snap = get_stream_state(mid)
    assert snap["chat_id"] == chat_id
    assert snap["status"] == "in_progress"


def test_startup_stream_registration_is_active_before_first_delta():
    chat_id = "test-startup-chat"
    mid = _mk_message_id("startup-active")

    stream_version_init(
        mid,
        chat_id=chat_id,
        user_id="user-1",
        session_id="sid-1",
        content_blocks=[],
    )

    active = get_active_streams_for_chat(chat_id)
    assert any(s["message_id"] == mid and s["version"] == 0 for s in active)
    snap = get_stream_state(mid)
    assert snap["content_blocks"] == []
    assert snap["status"] == "in_progress"


def test_stream_version_increments_use_local_cursor_with_bounded_store_flush(monkeypatch):
    mid = _mk_message_id("version-cursor")
    stream_version_init(mid, chat_id="version-chat", user_id="user-1", session_id="sid-1")
    monkeypatch.setattr(socket_main, "STREAM_VERSION_STORE_FLUSH_EVERY", 3)

    assert stream_version_incr(mid) == 1
    assert stream_version_incr(mid) == 2
    assert stream_version_get(mid) == 2
    assert socket_main.STREAM_VERSION.get(mid) == 0

    assert stream_version_incr(mid) == 3
    assert socket_main.STREAM_VERSION.get(mid) == 3

    assert stream_version_incr(mid) == 4
    assert socket_main.STREAM_VERSION.get(mid) == 3
    assert stream_version_flush(mid) == 4
    assert socket_main.STREAM_VERSION.get(mid) == 4


def test_stream_version_flush_without_local_cursor_does_not_clobber_store():
    mid = _mk_message_id("version-no-local")
    stream_version_init(mid, chat_id="version-chat", user_id="user-1", session_id="sid-1")
    socket_main.STREAM_VERSION[mid] = 9
    socket_main.STREAM_VERSION_LOCAL.pop(mid, None)
    socket_main.STREAM_VERSION_LAST_STORED.pop(mid, None)

    assert stream_version_flush(mid) == 9
    assert socket_main.STREAM_VERSION.get(mid) == 9


def test_replay_buffer_returns_deltas_after_requested_version():
    mid = _mk_message_id("replay-hit")
    chat_id = "test-replay-chat"
    stream_version_init(mid, chat_id=chat_id, user_id="user-1", session_id="sid-1")
    v1 = stream_version_incr(mid)
    v2 = stream_version_incr(mid)

    async def _run():
        for version, text in ((v1, "a"), (v2, "b")):
            await append_stream_replay_event(
                {
                    "chat_id": chat_id,
                    "message_id": mid,
                    "data": {
                        "type": "chat:delta",
                        "data": {
                            "message_id": mid,
                            "version": version,
                            "op": "text_append",
                            "payload": {"block_idx": 0, "text": text},
                        },
                    },
                }
            )
        return await get_stream_replay_events(mid, 1)

    replay = asyncio.run(_run())
    assert replay["status"] == "ok"
    assert replay["from_version"] == 1
    assert replay["to_version"] == 2
    assert [e["data"]["version"] for e in replay["events"]] == [2]


def test_replay_buffer_reports_miss_when_gap_is_not_covered():
    mid = _mk_message_id("replay-miss")
    chat_id = "test-replay-chat"
    stream_version_init(mid, chat_id=chat_id, user_id="user-1", session_id="sid-1")
    stream_version_incr(mid)
    v2 = stream_version_incr(mid)

    async def _run():
        await append_stream_replay_event(
            {
                "chat_id": chat_id,
                "message_id": mid,
                "data": {
                    "type": "chat:delta",
                    "data": {
                        "message_id": mid,
                        "version": v2,
                        "op": "text_append",
                        "payload": {"block_idx": 0, "text": "b"},
                    },
                },
            }
        )
        return await get_stream_replay_events(mid, 0)

    replay = asyncio.run(_run())
    assert replay["status"] == "miss"
    assert replay["snapshot_required"] is True


def test_redis_replay_append_trims_by_accounted_bytes_without_scanning(monkeypatch):
    mid = _mk_message_id("redis-replay-byte-trim")
    chat_id = "test-redis-replay-chat"
    stream_version_init(mid, chat_id=chat_id, user_id="user-1", session_id="sid-1")

    fake = _FakeReplayRedis()
    monkeypatch.setattr(socket_main, "REDIS", fake)
    monkeypatch.setattr(socket_main, "STREAM_REPLAY_BUFFER_MAX_EVENTS", 50)
    monkeypatch.setattr(socket_main, "STREAM_REPLAY_BUFFER_MAX_BYTES", 1200)
    monkeypatch.setattr(socket_main, "STREAM_REPLAY_BUFFER_TTL_SECONDS", 60)

    async def _run():
        for version in range(1, 12):
            await append_stream_replay_event(
                {
                    "chat_id": chat_id,
                    "message_id": mid,
                    "data": {
                        "type": "chat:delta",
                        "data": {
                            "message_id": mid,
                            "version": version,
                            "op": "text_append",
                            "payload": {"block_idx": 0, "text": "x" * 160},
                        },
                    },
                }
            )

    asyncio.run(_run())

    key = socket_main._stream_replay_key(mid)
    size_key = socket_main._stream_replay_size_key(mid)
    bytes_key = socket_main._stream_replay_bytes_key(mid)
    entries = fake.lists[key]
    sizes = [int(value) for value in fake.lists[size_key]]

    assert fake.lrange_calls == 0
    assert len(entries) == len(sizes)
    assert fake.values[bytes_key] == sum(sizes)
    assert fake.values[bytes_key] <= socket_main.STREAM_REPLAY_BUFFER_MAX_BYTES
    assert all(size == len(raw.encode("utf-8", "replace")) for size, raw in zip(sizes, entries))
    assert len(entries) < 11


def test_replay_buffer_trims_by_byte_cap(monkeypatch):
    mid = _mk_message_id("replay-byte-cap")
    chat_id = "test-replay-chat"
    monkeypatch.setattr(socket_main, "STREAM_REPLAY_BUFFER_MAX_EVENTS", 100)
    monkeypatch.setattr(socket_main, "STREAM_REPLAY_BUFFER_MAX_BYTES", 360)
    stream_version_init(mid, chat_id=chat_id, user_id="user-1", session_id="sid-1")

    async def _run():
        for text in ("a" * 80, "b" * 80, "c" * 80):
            version = stream_version_incr(mid)
            await append_stream_replay_event(
                {
                    "chat_id": chat_id,
                    "message_id": mid,
                    "data": {
                        "type": "chat:delta",
                        "data": {
                            "message_id": mid,
                            "version": version,
                            "op": "text_append",
                            "payload": {"block_idx": 0, "text": text},
                        },
                    },
                }
            )
        return await get_stream_replay_events(mid, 0)

    replay = asyncio.run(_run())
    assert replay["status"] == "miss"
    assert len(socket_main.STREAM_REPLAY_BUFFERS[mid]) < 3
    assert socket_main.STREAM_REPLAY_BUFFER_BYTES[mid] <= socket_main.STREAM_REPLAY_BUFFER_MAX_BYTES


def test_replay_to_version_uses_replay_entries_when_shared_version_lags():
    mid = _mk_message_id("replay-store-lag")
    chat_id = "test-replay-chat"
    stream_version_init(mid, chat_id=chat_id, user_id="user-1", session_id="sid-1")
    v1 = stream_version_incr(mid)
    v2 = stream_version_incr(mid)

    async def _run():
        for version in (v1, v2):
            await append_stream_replay_event(
                {
                    "chat_id": chat_id,
                    "message_id": mid,
                    "data": {
                        "type": "chat:delta",
                        "data": {
                            "message_id": mid,
                            "version": version,
                            "op": "text_append",
                            "payload": {"block_idx": 0, "text": str(version)},
                        },
                    },
                }
            )
        # Simulate another worker reading Redis replay entries while its local
        # cursor is absent and shared STREAM_VERSION has not reached the latest
        # replay entry yet.
        socket_main.STREAM_VERSION_LOCAL.pop(mid, None)
        socket_main.STREAM_VERSION[mid] = 1
        return await get_stream_replay_events(mid, 1)

    replay = asyncio.run(_run())
    assert replay["status"] == "ok"
    assert replay["to_version"] == 2
    assert [e["data"]["version"] for e in replay["events"]] == [2]


def test_compact_batch2_groups_deltas_and_tool_results():
    batch = [
        {
            "chat_id": "c1",
            "message_id": "m1",
            "data": {
                "type": "chat:delta",
                "data": {
                    "message_id": "m1",
                    "version": 1,
                    "op": "text_append",
                    "payload": {"block_idx": 0, "text": "hi"},
                },
            },
        },
        {
            "chat_id": "c1",
            "message_id": "m1",
            "data": {
                "type": "tool_call:result",
                "data": {"message_id": "m1", "tool_call_id": "tc1", "result": "ok"},
            },
        },
    ]

    compact = socket_main._make_delta_batch2_envelope(batch)
    assert compact["data"]["type"] == "chat:delta:batch2"
    assert compact["data"]["format"] == "owui.stream.v2.1"
    groups = compact["data"]["groups"]
    assert len(groups) == 1
    assert groups[0]["message_id"] == "m1"
    assert groups[0]["base_version"] == 0
    assert groups[0]["version_mode"] == "offset"
    assert groups[0]["deltas"] == [[1, "t", 0, "hi"]]
    assert groups[0]["tool_results"][0]["tool_call_id"] == "tc1"
    assert socket_main._stream_payload_versions(compact) == [("m1", 1)]


def test_first_delta_immediate_awaits_emit_before_return(monkeypatch):
    emitted = []
    mid = _mk_message_id("first-immediate")
    key = ("user-1", "c1")
    socket_main._pending_delta_buffer.pop(key, None)
    socket_main._pending_delta_buffer_sizes.pop(key, None)
    socket_main._pending_delta_scheduled.discard(key)
    socket_main.STREAM_FIRST_DELTA_SENT.discard(mid)

    async def fake_emit(user_id, payload):
        emitted.append((user_id, payload["data"]["data"]["version"]))

    monkeypatch.setattr(socket_main, "_emit_to_primary_raw", fake_emit)

    payload = {
        "chat_id": "c1",
        "message_id": mid,
        "data": {
            "type": "chat:delta",
            "data": {
                "message_id": mid,
                "version": 1,
                "op": "text_append",
                "payload": {"block_idx": 0, "text": "a"},
            },
        },
    }

    asyncio.run(socket_main._enqueue_delta("user-1", payload))
    assert emitted == [("user-1", 1)]
    assert socket_main._pending_delta_buffer.get(key) in (None, [])


def test_browser_frames_are_suppressed_for_hidden_stream_subscribers(monkeypatch):
    emitted = []
    chat_id = "browser-chat"

    monkeypatch.setattr(
        socket_main,
        "get_session_ids_from_room",
        lambda _room: ["visible-sid", "hidden-sid"],
    )

    async def fake_emit(_event, payload, to=None):
        emitted.append((to, payload["data"]["type"]))

    monkeypatch.setattr(socket_main.sio, "emit", fake_emit)
    socket_main.STREAM_SUBSCRIPTION_STATE[chat_id] = {
        "visible-sid": {"visible": True, "capabilities": {}},
        "hidden-sid": {"visible": False, "capabilities": {}},
    }

    payload = {
        "chat_id": chat_id,
        "message_id": "m1",
        "data": {
            "type": "browser:frame",
            "data": {"frame": "data:image/jpeg;base64,AAA", "done": False},
        },
    }

    try:
        asyncio.run(socket_main._emit_to_primary_raw("user-1", payload))
        assert emitted == [("visible-sid", "browser:frame")]
    finally:
        socket_main.STREAM_SUBSCRIPTION_STATE.pop(chat_id, None)


def test_store_does_not_deepcopy_accumulated_state_each_write():
    """Guards the optimization intent: a write should deep-copy only the patch,
    not re-deepcopy the entire previously-stored content_blocks. We assert this
    behaviorally — the unpatched accumulated 'content_blocks' object identity is
    preserved across a patch that doesn't touch it (proving it wasn't cloned)."""
    mid = _mk_message_id("nocopy")
    blocks_patch = [{"type": "text", "content": "big"}]
    set_stream_state(mid, {"content_blocks": blocks_patch})
    stored_blocks_obj = STREAM_STATE[mid]["content_blocks_snapshot"]["blocks"]

    # A patch that updates only status must leave the stored content_blocks list
    # object identity intact (carried by reference, not re-copied).
    set_stream_state(mid, {"status": "done"})
    assert STREAM_STATE[mid]["content_blocks_snapshot"]["blocks"] is stored_blocks_obj


def test_dirty_tail_snapshot_reuses_unchanged_prefix_blocks():
    mid = _mk_message_id("dirty-tail")
    set_stream_state(
        mid,
        {
            "content_blocks": [
                {"type": "text", "content": "stable"},
                {"type": "text", "content": "a"},
            ]
        },
    )
    first_snapshot = STREAM_STATE[mid]["content_blocks_snapshot"]
    first_prefix = first_snapshot["blocks"][0]
    first_tail = first_snapshot["blocks"][1]

    set_stream_state(
        mid,
        {
            "content_blocks": [
                {"type": "text", "content": "stable"},
                {"type": "text", "content": "ab"},
            ],
            "content_blocks_dirty_from": 1,
        },
    )

    second_blocks = STREAM_STATE[mid]["content_blocks_snapshot"]["blocks"]
    assert second_blocks[0] is first_prefix
    assert second_blocks[1] is not first_tail
    assert get_stream_state(mid)["content_blocks"][1]["content"] == "ab"


# -- A1: tool-result bodies accessor copy semantics ---------------------------


def test_get_tool_result_bodies_default_deep_copies():
    """Default callers get an isolated copy: mutating the returned dict must not
    corrupt the live store (the external-caller safety contract)."""
    mid = _mk_message_id("bodies-copy")
    set_tool_result_body(mid, "c1", {"tool_call_id": "c1", "content": "page body"})

    got = get_tool_result_bodies(mid)
    got["c1"]["content"] = "MUTATED"

    fresh = get_tool_result_bodies(mid)
    assert fresh["c1"]["content"] == "page body"


def test_get_tool_result_bodies_no_copy_shares_reference():
    """The agentic hot path passes deep_copy=False to avoid O(N²) large-data
    copying. It returns the live store reference (read-only by contract). At
    300 rounds this is the difference between copying the whole growing bodies
    dict 300× and not copying it at all."""
    mid = _mk_message_id("bodies-nocopy")
    set_tool_result_body(mid, "c1", {"tool_call_id": "c1", "content": "page body"})

    shared = get_tool_result_bodies(mid, deep_copy=False)
    # Same underlying object as the store (no clone).
    from open_webui.socket.main import TOOL_RESULT_BODIES

    assert shared is TOOL_RESULT_BODIES.get(mid)


def test_tool_result_body_spills_when_message_cap_is_exceeded(monkeypatch, tmp_path):
    mid = _mk_message_id("body-spill")
    monkeypatch.setattr(socket_main, "STREAM_TOOL_RESULT_BODY_MAX_BYTES", 10_000)
    monkeypatch.setattr(socket_main, "STREAM_TOOL_RESULT_BODY_MAX_BYTES_PER_MESSAGE", 220)
    monkeypatch.setattr(socket_main, "STREAM_TOOL_RESULT_BODY_SPILL_DIR", str(tmp_path))

    set_tool_result_body(mid, "old", {"tool_call_id": "old", "content": "x" * 100})
    set_tool_result_body(mid, "new", {"tool_call_id": "new", "content": "y" * 100})

    assert "old" not in socket_main.TOOL_RESULT_BODIES.get(mid, {})
    assert "new" in socket_main.TOOL_RESULT_BODIES.get(mid, {})
    spilled = get_tool_result_body(mid, "old")
    assert spilled["content"] == "x" * 100
    assert list(Path(tmp_path).glob("*.json")), "expected spilled tool body file"

    all_bodies = get_tool_result_bodies(mid)
    assert all_bodies["old"]["content"] == "x" * 100
    assert all_bodies["new"]["content"] == "y" * 100

    socket_main.clear_tool_result_bodies(mid)
    assert mid not in socket_main.TOOL_RESULT_BODIES
    assert mid not in socket_main.TOOL_RESULT_BODY_SPILLS
    assert not list(Path(tmp_path).glob("*.json"))
