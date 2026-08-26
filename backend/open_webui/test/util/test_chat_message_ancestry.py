import asyncio
from types import SimpleNamespace

import pytest

from test.util.db import configure_test_database

configure_test_database()

chat_utils = pytest.importorskip("open_webui.utils.chat")


def test_walk_rejects_a_missing_persisted_ancestor():
    messages = {
        "assistant": {
            "id": "assistant",
            "parentId": "missing-user",
            "role": "assistant",
            "content": "answer",
        }
    }

    with pytest.raises(chat_utils.ChatMessageAncestryError) as error:
        chat_utils._walk_messages_from_leaf(messages, "assistant")

    assert error.value.code == "chat_message_ancestor_missing"
    assert error.value.message_id == "missing-user"


def test_walk_rejects_a_persisted_cycle():
    messages = {
        "user": {"id": "user", "parentId": "assistant", "role": "user"},
        "assistant": {
            "id": "assistant",
            "parentId": "user",
            "role": "assistant",
        },
    }

    with pytest.raises(chat_utils.ChatMessageAncestryError) as error:
        chat_utils._walk_messages_from_leaf(messages, "assistant")

    assert error.value.code == "chat_message_ancestry_cycle"


def test_unpersisted_new_user_leaf_still_walks_its_durable_parent(monkeypatch):
    messages = {
        "user-1": {
            "id": "user-1",
            "parentId": None,
            "role": "user",
            "content": "question",
        },
        "assistant-1": {
            "id": "assistant-1",
            "parentId": "user-1",
            "role": "assistant",
            "content": "answer",
        },
    }
    writes = []

    async def fake_messages_map(_chat_id):
        return messages

    async def fake_chat(_chat_id):
        return SimpleNamespace(messages_migrated=1)

    async def fake_upsert(chat_id, message_id, message, **_kwargs):
        writes.append((chat_id, message_id, message))

    monkeypatch.setattr(
        chat_utils.Chats, "get_messages_map_by_chat_id", fake_messages_map
    )
    monkeypatch.setattr(chat_utils.Chats, "get_chat_by_id", fake_chat)
    monkeypatch.setattr(
        chat_utils.Chats,
        "upsert_message_to_chat_by_id_and_message_id",
        fake_upsert,
    )

    result = asyncio.run(
        chat_utils.assemble_conversation_from_leaf(
            "chat-1",
            "user-2",
            new_user_message={
                "id": "user-2",
                "parentId": "assistant-1",
                "role": "user",
                "content": "continue",
            },
        )
    )

    assert [message["role"] for message in result] == ["user", "assistant", "user"]
    assert writes[0][1] == "user-2"
    assert writes[0][2]["parentId"] == "assistant-1"
