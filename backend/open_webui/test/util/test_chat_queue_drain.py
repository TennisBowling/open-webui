"""Unit tests for the autonomous message-queue drain model layer.

Exercises the atomic queue helpers on a real (Postgres) database, validating
the invariants the server-driven drain depends on:

* exactly-once pop of the queue head,
* the ownership guard (a stale/duplicate completion of a superseded turn can't
  pop a second item),
* idempotent marker clearing,
* atomic append/remove (no whole-array clobber).

These are pure model-layer tests (no FastAPI / sockets / Redis) so they run fast
and without Docker — BUT the helpers use Postgres-only SQL (``jsonb_set``,
``SELECT ... FOR UPDATE``, ``jsonb_typeof``), so the module REQUIRES a real
Postgres: ``configure_test_database(required=True)`` skips the whole module at
import time unless ``POSTGRES_TEST_DATABASE_URL`` (or a ``postgresql+asyncpg``
``DATABASE_URL``) is set. A "passing" run with no Postgres URL means the module
was SKIPPED, not validated — wire CI to provide the URL so this SQL actually runs.
"""

import uuid
import asyncio

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
        lambda it: {
            "item_id": it["id"],
            "response_message_id": "resp-1",
            "started_at": 1,
        },
        expected_finished_response_id=None,
    )

    res = Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda it: {
            "item_id": it["id"],
            "response_message_id": "resp-X",
            "started_at": 2,
        },
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
        lambda it: {
            "item_id": it["id"],
            "response_message_id": "resp-1",
            "started_at": 1,
        },
        expected_finished_response_id=None,
    )
    res = Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda it: {
            "item_id": it["id"],
            "response_message_id": "resp-2",
            "started_at": 2,
        },
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
        lambda it: {
            "item_id": it["id"],
            "response_message_id": "resp-1",
            "started_at": 1,
        },
        expected_finished_response_id=None,
    )
    res = Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda it: {
            "item_id": it["id"],
            "response_message_id": "resp-2",
            "started_at": 2,
        },
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
        lambda it: {
            "item_id": it["id"],
            "response_message_id": "resp-1",
            "started_at": 1,
        },
        expected_finished_response_id=None,
    )
    # Stale errored turn (resp-0) tries to clear — must NOT wipe resp-1.
    Chats.clear_draining_by_id(chat_id, expected_finished_response_id="resp-0")
    assert (
        Chats.get_queue_state_by_id(chat_id)["draining"]["response_message_id"]
        == "resp-1"
    )

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


def test_peek_steer_is_nondestructive(chat_id):
    """peek must return steers WITHOUT removing them (deferred-consume / C01)."""
    Chats.append_queue_item_by_id(chat_id, _moded_item("s1", "steer"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("a1", "after_final"))
    peeked = Chats.peek_steer_items_by_id(chat_id)
    assert [p["id"] for p in peeked] == ["s1"]
    state = Chats.get_queue_state_by_id(chat_id)
    assert [q["id"] for q in state["queue"]] == ["s1", "a1"], "peek must not remove"


def test_remove_steer_items_by_ids(chat_id):
    Chats.append_queue_item_by_id(chat_id, _moded_item("s1", "steer"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("s2", "steer"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("a1", "after_final"))
    Chats.remove_steer_items_by_ids(chat_id, ["s1", "s2"])
    state = Chats.get_queue_state_by_id(chat_id)
    assert [q["id"] for q in state["queue"]] == ["a1"]


def test_remove_steer_items_empty_ids_is_noop(chat_id):
    Chats.append_queue_item_by_id(chat_id, _moded_item("s1", "steer"))
    Chats.remove_steer_items_by_ids(chat_id, [])
    assert [q["id"] for q in Chats.get_queue_state_by_id(chat_id)["queue"]] == ["s1"]


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
    assert modes == [
        ("s1", "after_final"),
        ("a1", "after_final"),
        ("s2", "after_final"),
    ]
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


def test_update_queue_item_preserves_position_and_merges(chat_id):
    """Editing a queued item must NOT move it to the tail (the old remove+append
    edit path reordered the queue / a steer's injection slot — C20). The merge
    preserves fields the caller omits (e.g. mode)."""
    Chats.append_queue_item_by_id(chat_id, _moded_item("a", "after_final"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("b", "steer"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("c", "after_final"))

    # Edit the MIDDLE item's text; position and mode must survive.
    Chats.update_queue_item_by_id(
        chat_id, {"id": "b", "sendSpec": {"model": "m", "content": "EDITED"}}
    )

    state = Chats.get_queue_state_by_id(chat_id)
    assert [q["id"] for q in state["queue"]] == ["a", "b", "c"], "edit must not reorder"
    b = next(q for q in state["queue"] if q["id"] == "b")
    assert b["sendSpec"]["content"] == "EDITED"
    assert b["mode"] == "steer"  # field the edit omitted is preserved


def test_update_queue_item_unknown_id_is_noop(chat_id):
    Chats.append_queue_item_by_id(
        chat_id,
        {
            "id": "a",
            "mode": "after_final",
            "sendSpec": {"model": "m", "content": "ORIG"},
        },
    )
    Chats.update_queue_item_by_id(
        chat_id, {"id": "ghost", "sendSpec": {"content": "BLED"}}
    )
    state = Chats.get_queue_state_by_id(chat_id)
    assert [q["id"] for q in state["queue"]] == ["a"]
    # 'a' untouched — a non-matching edit must not bleed into it (distinct
    # original content so the assertion actually catches a bleed).
    assert state["queue"][0]["sendSpec"]["content"] == "ORIG"


def test_pop_head_prefers_steer_over_earlier_after_final(chat_id):
    """A steer that fell back to the drain (arrived after the last tool boundary)
    should drain AHEAD of an earlier after_final follow-up — "steer" means do
    this next, urgently (C21)."""
    Chats.append_queue_item_by_id(chat_id, _moded_item("f", "after_final"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("s", "steer"))

    res = Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda it: {"item_id": it["id"], "response_message_id": "r1"},
        expected_finished_response_id=None,
    )
    assert res["item"]["id"] == "s", "steer preferred over the earlier after_final"
    assert [q["id"] for q in res["queue"]] == ["f"]


def test_pop_head_plain_fifo_without_steer(chat_id):
    Chats.append_queue_item_by_id(chat_id, _moded_item("f1", "after_final"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("f2", "after_final"))
    res = Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id, lambda it: {"item_id": it["id"], "response_message_id": "r1"}
    )
    assert res["item"]["id"] == "f1"  # plain head pop when no steer present


def test_clear_draining_returns_cleared_and_converted(chat_id):
    """clear_draining_by_id reports whether it cleared the marker and how many
    steers it downgraded, doing both atomically in the same locked write (C22)."""
    Chats.append_queue_item_by_id(chat_id, _moded_item("s1", "steer"))
    Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id,
        lambda it: {"item_id": it["id"], "response_message_id": "r1"},
        expected_finished_response_id=None,
    )
    # pop consumed s1; queue two more steers behind the marker.
    Chats.append_queue_item_by_id(chat_id, _moded_item("s2", "steer"))
    Chats.append_queue_item_by_id(chat_id, _moded_item("s3", "steer"))

    # A stale OLD turn must NOT clear the newer marker nor convert its steers.
    res_stale = Chats.clear_draining_by_id(chat_id, expected_finished_response_id="old")
    assert res_stale["cleared"] is False
    assert res_stale["converted"] == 0
    assert (
        Chats.get_queue_state_by_id(chat_id)["draining"]["response_message_id"] == "r1"
    )

    # The owning turn clears the marker AND downgrades both steers in one shot.
    res_own = Chats.clear_draining_by_id(chat_id, expected_finished_response_id="r1")
    assert res_own["cleared"] is True
    assert res_own["converted"] == 2
    state = Chats.get_queue_state_by_id(chat_id)
    assert state["draining"] is None
    assert all(q.get("mode") == "after_final" for q in state["queue"])


def _is_armed(chat_id):
    return any(c["id"] == chat_id for c in Chats.get_armed_queue_chats(limit=500))


def test_queue_armed_flag_tracks_the_queue(chat_id):
    """`queue_armed_at` is the indexed restatement of "this chat is owed a
    drain", and the reconciler trusts it INSTEAD of reading every chat blob (a
    jsonb scan over the real table costs ~6.5k buffers a pass). It is maintained
    in one place — the write that persists the queue — so it cannot disagree
    with the queue it summarises. This pins that: every mutator arms while items
    remain and disarms the moment the queue empties."""
    assert not _is_armed(chat_id), "a chat with no queue is never a candidate"

    Chats.append_queue_item_by_id(chat_id, {"id": "a"})
    assert _is_armed(chat_id), "enqueue arms"

    Chats.append_queue_item_by_id(chat_id, {"id": "b"})
    Chats.remove_queue_item_by_id(chat_id, "a")
    assert _is_armed(chat_id), "still armed while an item remains"

    Chats.remove_queue_item_by_id(chat_id, "b")
    assert not _is_armed(chat_id), "emptying the queue disarms"


def test_queue_armed_flag_clears_when_the_last_item_is_popped(chat_id):
    """The pop that starts the last queued generation must disarm too, or the
    chat stays a reconciler candidate forever and gets re-examined every pass."""
    Chats.append_queue_item_by_id(chat_id, {"id": "a"})
    assert _is_armed(chat_id)

    Chats.pop_queue_head_and_mark_draining_by_id(
        chat_id, lambda it: {"item_id": it["id"], "response_message_id": "r1"}
    )
    assert not _is_armed(chat_id), "queue is empty now — nothing is owed"


def test_queue_armed_flag_survives_a_whole_blob_write(chat_id):
    """The reason this is a COLUMN and not another key in the chat blob: routine
    autosaves rewrite the whole `chat` column from a client snapshot, and
    update_chat_by_id has to re-inject live queue/draining/question_states by
    hand to survive that. A column is out of reach of the clobber entirely."""
    Chats.append_queue_item_by_id(chat_id, {"id": "a"})
    chat = Chats.get_chat_by_id(chat_id)
    body = dict(chat.chat)
    body.pop("queue", None)  # a stale snapshot that predates the enqueue
    body["title"] = "renamed by an autosave"
    Chats.update_chat_by_id(chat_id, body)

    assert _is_armed(chat_id), "the flag outlives a whole-column blob write"
    assert [q["id"] for q in Chats.get_queue_state_by_id(chat_id)["queue"]] == ["a"]
