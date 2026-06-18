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
import shutil
import tempfile

# --- Bind the DB to a throwaway copy of the migrated dev DB BEFORE imports ----
_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "stream_state_test.db")
_HERE = os.path.dirname(__file__)
_DEV_DB = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "data", "webui.db"))
if os.path.exists(_DEV_DB):
    shutil.copy(_DEV_DB, _DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
# Ensure no Redis → in-memory stores (the single-worker path under test).
os.environ.pop("WEBSOCKET_REDIS_URL", None)

from open_webui.socket.main import (  # noqa: E402
    set_stream_state,
    get_stream_state,
    STREAM_STATE,
    set_tool_result_body,
    get_tool_result_bodies,
    stream_version_init,
    get_active_streams_for_chat,
)


def _mk_message_id(name: str) -> str:
    # Keep ids unique per test so the module-level store doesn't bleed across
    # tests (the store is process-global by design).
    return f"test-stream-{name}"


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


def test_store_does_not_deepcopy_accumulated_state_each_write():
    """Guards the optimization intent: a write should deep-copy only the patch,
    not re-deepcopy the entire previously-stored content_blocks. We assert this
    behaviorally — the unpatched accumulated 'content_blocks' object identity is
    preserved across a patch that doesn't touch it (proving it wasn't cloned)."""
    mid = _mk_message_id("nocopy")
    blocks_patch = [{"type": "text", "content": "big"}]
    set_stream_state(mid, {"content_blocks": blocks_patch})
    stored_blocks_obj = STREAM_STATE[mid]["content_blocks"]

    # A patch that updates only status must leave the stored content_blocks list
    # object identity intact (carried by reference, not re-copied).
    set_stream_state(mid, {"status": "done"})
    assert STREAM_STATE[mid]["content_blocks"] is stored_blocks_obj


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

