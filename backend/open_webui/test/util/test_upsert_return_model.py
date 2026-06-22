"""Tests for the ``return_model`` fast path on
``Chats.upsert_message_to_chat_by_id_and_message_id``.

The agentic loop writes a message 2× per round plus per checkpoint. The default
return path re-reads every message row and re-validates the whole chat into a
``ChatModel`` (``_hydrate_chat_messages`` → ``_normalize_message_graph`` is up to
O(N²)) — pure waste when the caller discards the result. ``return_model=False``
must skip that work while STILL persisting the write durably.

Binds DATABASE_URL to a throwaway copy of the migrated dev DB before importing
any open_webui module (the engine binds at import). Same pattern as
``test_chat_queue_drain.py``.
"""

import uuid
import asyncio

import pytest  # noqa: E402

from test.util.db import configure_test_database

configure_test_database(required=True)

from open_webui.models.chats import Chats, ChatForm, ChatModel  # noqa: E402


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
    chat = Chats.insert_new_chat(
        f"user-{uuid.uuid4()}",
        ChatForm(
            chat={
                "title": "return_model test",
                "history": {"currentId": None, "messages": {}},
            }
        ),
    )
    return chat.id


def test_return_model_false_returns_none_but_persists(chat_id):
    result = Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        "m1",
        {"role": "user", "content": "hello", "parentId": None},
        return_model=False,
    )
    assert result is None  # fast path returns nothing

    # The write is durable: a fresh read sees the message.
    msg = Chats.get_message_by_id_and_message_id(chat_id, "m1")
    assert msg is not None
    assert msg.get("content") == "hello"


def test_return_model_true_default_returns_model(chat_id):
    result = Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        "m1",
        {"role": "user", "content": "hi", "parentId": None},
    )
    assert isinstance(result, ChatModel)
    # The model reflects the just-written message.
    messages = result.chat.get("history", {}).get("messages", {})
    assert messages.get("m1", {}).get("content") == "hi"


def test_return_model_false_merge_semantics_match_default(chat_id):
    # Seed with the default path.
    Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id, "m1", {"role": "assistant", "content": "draft", "parentId": None}
    )
    # Partial update via the fast path must MERGE (not replace) like the default.
    Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id, "m1", {"content": "final"}, return_model=False
    )
    msg = Chats.get_message_by_id_and_message_id(chat_id, "m1")
    assert msg.get("content") == "final"
    assert msg.get("role") == "assistant"  # untouched field preserved
