"""Incremental chat-open (Contract 3): row-version manifests + batch fetch.

The client's stored copy carries a per-message ``_rev`` (Postgres ``xmin`` —
bumped by the database on EVERY row update, so every writer past and future is
covered with zero application bookkeeping). On open, a changed chat returns the
branch window as a lean manifest ``[{id, parentId, role, rev}]``; the client
diffs against its local ``_rev`` values and batch-fetches only changed/missing
rows. Deletions fall out as manifest absence.

DB-backed (same conventions as test_tool_result_bodies.py: sync wrapper around
the async Chats proxy, real Postgres required).
"""

import uuid

import pytest

from test.util.db import configure_test_database

configure_test_database(required=True)

import asyncio  # noqa: E402

from open_webui.models.chats import (  # noqa: E402
    Chats,
    ChatForm,
    _split_message_for_table,
)


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


_Chats = _SyncChats(Chats)


@pytest.fixture()
def chat_id():
    chat = _Chats.insert_new_chat(
        f"user-{uuid.uuid4()}",
        ChatForm(
            chat={
                "title": "incremental open test",
                "history": {"currentId": None, "messages": {}},
            }
        ),
    )
    return chat.id


def _seed_branch(chat_id):
    _Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        "m1",
        {"role": "user", "content": "question", "parentId": None},
        return_model=False,
    )
    _Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        "m2",
        {"role": "assistant", "content": "answer", "parentId": "m1"},
        return_model=False,
    )


def test_manifest_shape_and_rev_stability(chat_id):
    _seed_branch(chat_id)

    manifest = _Chats.get_chat_messages_branch_manifest(chat_id, "m2", limit=10)
    assert [row["id"] for row in manifest] == ["m1", "m2"]  # oldest-first
    assert manifest[0]["parentId"] is None
    assert manifest[1]["parentId"] == "m1"
    assert manifest[1]["role"] == "assistant"
    for row in manifest:
        assert isinstance(row["rev"], str) and row["rev"]

    # Re-reading without any write returns identical revs.
    again = _Chats.get_chat_messages_branch_manifest(chat_id, "m2", limit=10)
    assert [r["rev"] for r in again] == [r["rev"] for r in manifest]


def test_rev_changes_only_for_updated_row(chat_id):
    _seed_branch(chat_id)
    before = {
        r["id"]: r["rev"]
        for r in _Chats.get_chat_messages_branch_manifest(chat_id, "m2", limit=10)
    }

    _Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id, "m2", {"content": "edited answer"}, return_model=False
    )

    after = {
        r["id"]: r["rev"]
        for r in _Chats.get_chat_messages_branch_manifest(chat_id, "m2", limit=10)
    }
    assert after["m1"] == before["m1"]  # untouched row keeps its rev
    assert after["m2"] != before["m2"]  # edited row rotated


def test_manifest_limit_and_legacy_none(chat_id):
    _seed_branch(chat_id)
    window = _Chats.get_chat_messages_branch_manifest(chat_id, "m2", limit=1)
    assert [row["id"] for row in window] == ["m2"]

    # Unknown leaf → empty window, not an error.
    assert _Chats.get_chat_messages_branch_manifest(chat_id, "nope", limit=5) == []


def test_messages_by_ids_returns_rows_with_matching_revs(chat_id):
    _seed_branch(chat_id)
    manifest = {
        r["id"]: r["rev"]
        for r in _Chats.get_chat_messages_branch_manifest(chat_id, "m2", limit=10)
    }

    msgs = _Chats.get_messages_by_ids(chat_id, ["m2", "m1", "missing"])
    by_id = {m["id"]: m for m in msgs}
    assert set(by_id) == {"m1", "m2"}  # unknown ids silently dropped
    assert by_id["m2"]["content"] == "answer"
    for mid, msg in by_id.items():
        assert msg["_rev"] == manifest[mid]  # same snapshot, same version


def test_branch_reads_carry_rev(chat_id):
    # Full tail (Contract 2) must seed the client's versions so its NEXT open
    # can go incremental — every branch read carries _rev.
    _seed_branch(chat_id)
    branch = _Chats.get_chat_messages_branch(chat_id, "m2", limit=10)
    assert [m["id"] for m in branch] == ["m1", "m2"]
    assert all(isinstance(m.get("_rev"), str) and m["_rev"] for m in branch)


def test_split_message_for_table_drops_rev():
    # _rev is read-only wire metadata: any message dict that flows back into a
    # row write (upsert merge, whole-chat resync) must shed it — it may neither
    # become a column nor pollute meta.
    cols = _split_message_for_table(
        {
            "id": "m1",
            "role": "assistant",
            "content": "x",
            "_rev": "12345",
            "custom": True,
        }
    )
    assert "_rev" not in (cols.get("meta") or "")
    for v in cols.values():
        if isinstance(v, str):
            assert "_rev" not in v
