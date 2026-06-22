import uuid
import asyncio

from test.util.db import configure_test_database

configure_test_database(required=True)

from open_webui.models.chats import ChatForm, Chats  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_postgres_chat_search_indexes_inserted_message():
    user_id = f"search-user-{uuid.uuid4()}"
    chat = run(
        Chats.insert_new_chat(
            user_id,
            ChatForm(
                chat={
                    "title": "search smoke",
                    "history": {"currentId": None, "messages": {}},
                    "models": ["test-model"],
                }
            ),
        )
    )
    assert chat is not None

    run(
        Chats.upsert_message_to_chat_by_id_and_message_id(
            chat.id,
            "m1",
            {
                "id": "m1",
                "role": "user",
                "content": "the postgres async migration search needle",
                "parentId": None,
            },
            return_model=False,
        )
    )

    result = run(Chats.search_chats(user_id, "migration search needle"))
    assert result.total >= 1
    assert any(hit.id == chat.id for hit in result.hits)


def test_postgres_chat_queue_round_trip():
    user_id = f"queue-user-{uuid.uuid4()}"
    chat = run(
        Chats.insert_new_chat(
            user_id,
            ChatForm(
                chat={
                    "title": "queue smoke",
                    "history": {"currentId": None, "messages": {}},
                    "queue": [],
                }
            ),
        )
    )
    run(Chats.append_queue_item_by_id(chat.id, {"id": "q1", "mode": "after_final"}))
    state = run(Chats.get_queue_state_by_id(chat.id))
    assert state["queue"][0]["id"] == "q1"

    popped = run(Chats.pop_queue_head_and_mark_draining_by_id(chat.id, lambda item: {"item_id": item["id"]}))
    assert popped["item"]["id"] == "q1"
    assert popped["draining"] == {"item_id": "q1"}
