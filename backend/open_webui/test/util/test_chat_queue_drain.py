"""Unit tests for the autonomous message-queue drain model layer.

Exercises the atomic queue helpers on a throwaway SQLite database, validating
the invariants the server-driven drain depends on:

* exactly-once pop of the queue head,
* the ownership guard (a stale/duplicate completion of a superseded turn can't
  pop a second item),
* idempotent marker clearing,
* atomic append/remove (no whole-array clobber).

These are pure model-layer tests (no FastAPI / sockets / Redis) so they run fast
and without Docker.

The DB is bound at import time by ``open_webui.internal.db`` from ``DATABASE_URL``.
We set that env var to a temp copy of the migrated dev DB *before* importing any
open_webui module, so the engine binds to our throwaway file with the correct
(Alembic-migrated) schema and the real dev DB is never touched.
"""

import os
import shutil
import tempfile
import uuid

# --- Bind the DB to a throwaway copy of the migrated dev DB BEFORE imports ----
_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "queue_test.db")
_HERE = os.path.dirname(__file__)
_DEV_DB = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "data", "webui.db"))
if os.path.exists(_DEV_DB):
    shutil.copy(_DEV_DB, _DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

import pytest

from open_webui.internal.db import Base, engine  # noqa: E402
from open_webui.models.chats import Chats, ChatForm  # noqa: E402

# If we couldn't seed from the dev DB, create the schema fresh. (CI without a
# checked-in dev DB falls here; create_all is best-effort for these columns.)
if not os.path.exists(_DEV_DB):
    Base.metadata.create_all(bind=engine)


@pytest.fixture()
def chat_id():
    """Insert a fresh chat with an empty queue and return its id."""
    chat = Chats.insert_new_chat(
        f"user-{uuid.uuid4()}",
        ChatForm(
            chat={
                "title": "queue test",
                "history": {"currentId": "m0", "messages": {}},
                "queue": [],
            }
        ),
    )
    assert chat is not None
    return chat.id


def _item(item_id):
    return {
        "id": item_id,
        "model": "gpt-x",
        "new_user_message": {"id": f"u-{item_id}", "parentId": "m0", "content": "hi"},
    }


def test_append_and_remove_are_atomic(chat_id):
    Chats.append_queue_item_by_id(chat_id, _item("a"))
    Chats.append_queue_item_by_id(chat_id, _item("b"))
    state = Chats.get_queue_state_by_id(chat_id)
    assert [q["id"] for q in state["queue"]] == ["a", "b"]
    assert state["draining"] is None

    Chats.remove_queue_item_by_id(chat_id, "a")
    state = Chats.get_queue_state_by_id(chat_id)
    assert [q["id"] for q in state["queue"]] == ["b"]


def test_pop_marks_draining_and_pops_head(chat_id):
    Chats.append_queue_item_by_id(chat_id, _item("a"))
    Chats.append_queue_item_by_id(chat_id, _item("b"))

    def _marker(item):
        return {"item_id": item["id"], "response_message_id": "resp-1", "started_at": 1}

    res = Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id, _marker, expected_finished_response_id=None
    )
    assert res["item"]["id"] == "a"
    assert res["draining"]["response_message_id"] == "resp-1"
    assert [q["id"] for q in res["queue"]] == ["b"]

    state = Chats.get_queue_state_by_id(chat_id)
    assert state["draining"]["item_id"] == "a"
    assert [q["id"] for q in state["queue"]] == ["b"]


def test_ownership_guard_blocks_duplicate_completion(chat_id):
    """A marker owned by a DIFFERENT in-flight generation must NOT pop again.

    Models the race: generation R1 popped 'a' and set marker(resp-1). A stale/
    duplicate completion for the PREVIOUS turn (finished_response_id=resp-0)
    arrives — it must be a no-op, not pop 'b'."""
    Chats.append_queue_item_by_id(chat_id, _item("a"))
    Chats.append_queue_item_by_id(chat_id, _item("b"))

    Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda it: {"item_id": it["id"], "response_message_id": "resp-1", "started_at": 1},
        expected_finished_response_id=None,
    )

    res = Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda it: {"item_id": it["id"], "response_message_id": "resp-X", "started_at": 2},
        expected_finished_response_id="resp-0",
    )
    assert res["item"] is None, "stale completion must not pop a second item"
    state = Chats.get_queue_state_by_id(chat_id)
    assert [q["id"] for q in state["queue"]] == ["b"]
    assert state["draining"]["response_message_id"] == "resp-1"


def test_owning_completion_advances_to_next(chat_id):
    """When the OWNING generation (resp-1) completes cleanly, it advances: pop
    the next item and set a new marker."""
    Chats.append_queue_item_by_id(chat_id, _item("a"))
    Chats.append_queue_item_by_id(chat_id, _item("b"))

    Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda it: {"item_id": it["id"], "response_message_id": "resp-1", "started_at": 1},
        expected_finished_response_id=None,
    )
    res = Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda it: {"item_id": it["id"], "response_message_id": "resp-2", "started_at": 2},
        expected_finished_response_id="resp-1",
    )
    assert res["item"]["id"] == "b"
    assert res["draining"]["response_message_id"] == "resp-2"
    assert res["queue"] == []


def test_empty_queue_clears_marker_on_owning_completion(chat_id):
    """Owning completion with an empty queue clears the marker."""
    Chats.append_queue_item_by_id(chat_id, _item("a"))
    Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda it: {"item_id": it["id"], "response_message_id": "resp-1", "started_at": 1},
        expected_finished_response_id=None,
    )
    res = Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda it: {"item_id": it["id"], "response_message_id": "resp-2", "started_at": 2},
        expected_finished_response_id="resp-1",
    )
    assert res["item"] is None
    assert res["draining"] is None
    assert Chats.get_queue_state_by_id(chat_id)["draining"] is None


def test_clear_draining_respects_ownership(chat_id):
    """clear_draining_by_id with a finished_response_id only clears its OWN
    marker, never a newer generation's."""
    Chats.append_queue_item_by_id(chat_id, _item("a"))
    Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda it: {"item_id": it["id"], "response_message_id": "resp-1", "started_at": 1},
        expected_finished_response_id=None,
    )
    # Stale errored turn (resp-0) tries to clear — must NOT wipe resp-1.
    Chats.clear_draining_by_id(chat_id, expected_finished_response_id="resp-0")
    assert Chats.get_queue_state_by_id(chat_id)["draining"]["response_message_id"] == "resp-1"

    # The owning turn clears its own marker.
    Chats.clear_draining_by_id(chat_id, expected_finished_response_id="resp-1")
    assert Chats.get_queue_state_by_id(chat_id)["draining"] is None


def _moded_item(item_id, mode):
    return {"id": item_id, "mode": mode, "sendSpec": {"model": "m", "content": "x"}}


def test_pop_steer_items_returns_only_steer_and_leaves_rest(chat_id):
    """pop_steer_items_by_id must remove + return ONLY mode=='steer' items, in
    queue order, leaving after_final and unmarked items untouched."""
    Chats.append_queue_item_by_id(chat_id, _moded_item("s1", "steer"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("a1", "after_final"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("s2", "steer"))
    Chats.append_queue_item_by_id(chat_id, {"id": "legacy"})  # unmarked

    popped = Chats.pop_steer_items_by_id(chat_id)
    assert [p["id"] for p in popped] == ["s1", "s2"]

    state = Chats.get_queue_state_by_id(chat_id)
    assert [q["id"] for q in state["queue"]] == ["a1", "legacy"]


def test_pop_steer_items_empty_when_none(chat_id):
    Chats.append_queue_item_by_id(chat_id, _moded_item("a1", "after_final"))
    assert Chats.pop_steer_items_by_id(chat_id) == []
    state = Chats.get_queue_state_by_id(chat_id)
    assert [q["id"] for q in state["queue"]] == ["a1"]


def test_pop_steer_items_preserves_draining_marker(chat_id):
    """Steering is orthogonal to drain ownership: popping steer items must NOT
    disturb an in-flight draining marker."""
    Chats.append_queue_item_by_id(chat_id, _moded_item("s1", "steer"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("a1", "after_final"))
    # Simulate an in-flight drain marker via the head-pop helper.
    Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id, lambda item: {"item_id": item["id"], "response_message_id": "r9"}
    )
    # Head was 's1' (popped+marked); re-add a steer to pop separately.
    Chats.append_queue_item_by_id(chat_id, _moded_item("s2", "steer"))
    before = Chats.get_queue_state_by_id(chat_id)["draining"]
    assert before is not None

    Chats.pop_steer_items_by_id(chat_id)
    after = Chats.get_queue_state_by_id(chat_id)["draining"]
    assert after == before  # marker untouched


def test_convert_steer_items_to_after_final(chat_id):
    """Stop/error path: pending steers must be downgraded to after_final so they
    don't leak into the next unrelated generation's tool boundary, but become
    visible follow-ups instead. after_final/unmarked items are untouched."""
    Chats.append_queue_item_by_id(chat_id, _moded_item("s1", "steer"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("a1", "after_final"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("s2", "steer"))

    n = Chats.convert_steer_items_to_after_final_by_id(chat_id)
    assert n == 2

    state = Chats.get_queue_state_by_id(chat_id)
    modes = [(q["id"], q.get("mode")) for q in state["queue"]]
    # order preserved; both steers now after_final; a1 unchanged.
    assert modes == [("s1", "after_final"), ("a1", "after_final"), ("s2", "after_final")]
    # idempotent: a second call converts nothing.
    assert Chats.convert_steer_items_to_after_final_by_id(chat_id) == 0


def test_convert_steer_preserves_sendspec(chat_id):
    """The downgrade must keep the rest of the item intact (content/model live in
    sendSpec) so the converted follow-up still generates correctly."""
    Chats.append_queue_item_by_id(chat_id, _moded_item("s1", "steer"))
    Chats.convert_steer_items_to_after_final_by_id(chat_id)
    item = Chats.get_queue_state_by_id(chat_id)["queue"][0]
    assert item["mode"] == "after_final"
    assert item["sendSpec"]["model"] == "m"
    assert item["sendSpec"]["content"] == "x"
