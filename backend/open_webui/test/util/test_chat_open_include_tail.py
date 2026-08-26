"""Contract 2 (consolidated chat open): ``GET /chats/{id}?meta_only=true&
include_tail=N`` must embed, in the meta body:

  * ``branch`` == EXACTLY what the dedicated ``GET /chats/{id}/messages`` branch
    endpoint (``get_chat_messages_paginated`` with ``leaf=<repaired currentId>``)
    would return for the same tail count — no projection fork;
  * ``tags``  == EXACTLY the shape ``GET /chats/{id}/tags`` returns.

This is a DB-backed test (needs POSTGRES_TEST_DATABASE_URL / a live Postgres);
it builds both the embedded and the separate-endpoint responses in-process and
asserts identity. Item 4 (chat-level ``files.data.content`` stripped to '') is
also asserted on the meta body here.
"""

import asyncio
import uuid
from types import SimpleNamespace

from test.util.db import configure_test_database

configure_test_database(required=True)

from fastapi import Response  # noqa: E402

from open_webui.models.chats import Chats, ChatForm  # noqa: E402
from open_webui.models.chats import (
    _SLIM_FILE_CONTENT_THRESHOLD_BYTES as THRESHOLD,
)  # noqa: E402
from open_webui.routers.chats import (  # noqa: E402
    get_chat_by_id,
    get_chat_messages_paginated,
    get_chat_tags_by_id,
)


class _User:
    role = "user"

    def __init__(self, uid):
        self.id = uid


class _Req:
    """Minimal stand-in for fastapi.Request: the conditional-open path reads
    ``headers.get('if-none-match')`` and ``app.state.redis``."""

    def __init__(self):
        self.headers = {}
        self.app = SimpleNamespace(state=SimpleNamespace(redis=None))


def _open(chat_id, user, **kwargs):
    return asyncio.run(
        get_chat_by_id(_Req(), Response(), chat_id, meta_only=True, user=user, **kwargs)
    )


def _run(coro):
    return asyncio.run(coro)


def _make_chat():
    uid = f"user-{uuid.uuid4()}"
    big = "x" * (THRESHOLD + 500)
    # m0 (user) -> m1 (assistant); currentId = m1. Unmigrated JSON-blob chat.
    chat = _run(
        Chats.insert_new_chat(
            uid,
            ChatForm(
                chat={
                    "title": "tail open",
                    "files": [
                        {
                            "type": "file",
                            "id": "f1",
                            "file": {
                                "id": "f1",
                                "filename": "f1.pdf",
                                "data": {"content": big},
                            },
                        }
                    ],
                    "history": {
                        "currentId": "m1",
                        "messages": {
                            "m0": {
                                "id": "m0",
                                "role": "user",
                                "parentId": None,
                                "content": "hi",
                            },
                            "m1": {
                                "id": "m1",
                                "role": "assistant",
                                "parentId": "m0",
                                "content": "hello there",
                            },
                        },
                    },
                }
            ),
        )
    )
    return uid, chat.id, big


def test_include_tail_branch_and_tags_match_dedicated_endpoints():
    uid, chat_id, big = _make_chat()
    user = _User(uid)

    meta = _open(chat_id, user, include_tail=25)

    # branch must be present and identical to the dedicated branch endpoint for
    # the same repaired currentId + tail count.
    assert "branch" in meta
    current_id = meta["history"]["currentId"]
    assert current_id == "m1"
    branch_direct = _run(
        get_chat_messages_paginated(chat_id, leaf=current_id, limit=25, user=user)
    )
    assert meta["branch"] == branch_direct
    # the branch really walked the chain oldest-first
    assert [m["id"] for m in meta["branch"]] == ["m0", "m1"]

    # tags must match the dedicated tags endpoint shape (list).
    tags_direct = _run(get_chat_tags_by_id(chat_id, user=user))
    assert meta["tags"] == tags_direct
    assert isinstance(meta["tags"], list)

    # Contract 2 continued: task/stream state is bundled so the client never
    # pays follow-up round-trips for it. A quiescent chat embeds empty state.
    assert meta["active"] == {
        "generations": [],
        "rerun_task_ids": [],
        "subagent_rerun_entry_keys": [],
        "streams": [],
    }


def test_include_tail_caps_at_60():
    uid, chat_id, _ = _make_chat()
    user = _User(uid)
    # A huge include_tail is capped server-side; branch still returns the (short)
    # actual chain rather than erroring.
    meta = _open(chat_id, user, include_tail=100000)
    assert [m["id"] for m in meta["branch"]] == ["m0", "m1"]


def test_meta_without_include_tail_has_no_branch_or_tags():
    uid, chat_id, _ = _make_chat()
    user = _User(uid)
    meta = _open(chat_id, user)
    assert "branch" not in meta
    assert "tags" not in meta
    assert "active" not in meta  # bundle rides the tail contract only


def test_meta_chat_level_file_content_stripped_to_empty():
    uid, chat_id, big = _make_chat()
    user = _User(uid)
    meta = _open(chat_id, user, include_tail=25)
    data = meta["files"][0]["file"]["data"]
    assert data["content"] == ""
    assert not data["content"]  # falsy — retrieval fallback contract
    assert data["content_stripped"] is True


def test_tail_manifest_returns_versioned_window_without_bodies():
    # Contract 3: tail_manifest swaps the branch page for a lean row-version
    # window — [{id, parentId, role, rev}] oldest-first, NO message bodies —
    # and still bundles tags + active state. (insert_new_chat migrates chats
    # into rows immediately, so every API-created chat takes this path; the
    # legacy blob fallback survives only for pre-migration rows.)
    uid, chat_id, _ = _make_chat()
    user = _User(uid)
    meta = _open(chat_id, user, include_tail=25, tail_manifest=True)
    assert "branch" not in meta
    manifest = meta["branch_manifest"]
    assert [row["id"] for row in manifest] == ["m0", "m1"]
    assert manifest[1]["parentId"] == "m0"
    assert manifest[1]["role"] == "assistant"
    assert all(isinstance(row["rev"], str) and row["rev"] for row in manifest)
    assert all("content" not in row for row in manifest)
    assert isinstance(meta["tags"], list)
    assert meta["active"] == {
        "generations": [],
        "rerun_task_ids": [],
        "subagent_rerun_entry_keys": [],
        "streams": [],
    }
