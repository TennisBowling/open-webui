import asyncio
import uuid

from test.util.db import configure_test_database

configure_test_database(required=True)

from open_webui.models.chats import ChatForm, Chats  # noqa: E402


def test_stale_stop_cannot_mark_a_reused_assistant_message():
    async def run():
        message_id = str(uuid.uuid4())
        chat = await Chats.insert_new_chat(
            f"user-{uuid.uuid4()}",
            ChatForm(
                chat={
                    "title": "generation identity",
                    "history": {
                        "currentId": message_id,
                        "messages": {
                            message_id: {
                                "id": message_id,
                                "parentId": None,
                                "role": "assistant",
                                "content": "",
                                "generation_id": "generation-old",
                                "turn_id": "turn-old",
                                "done": False,
                            }
                        },
                    },
                }
            ),
        )
        try:
            assert await Chats.mark_generation_stopped_if_current(
                chat.id,
                message_id,
                "generation-old",
                "turn-old",
            )

            await Chats.upsert_message_to_chat_by_id_and_message_id(
                chat.id,
                message_id,
                {
                    "generation_id": "generation-new",
                    "turn_id": "turn-new",
                    "done": False,
                    "userStopped": False,
                },
                return_model=False,
            )

            assert not await Chats.mark_generation_stopped_if_current(
                chat.id,
                message_id,
                "generation-old",
                "turn-old",
            )
            assert not await Chats.update_generation_message_if_current(
                chat.id,
                message_id,
                "generation-old",
                "turn-old",
                {
                    "role": "assistant",
                    "done": True,
                    "error": {"content": "stale detached finalizer"},
                },
                create_if_missing=True,
            )
            current = await Chats.get_message_by_id_and_message_id(chat.id, message_id)
            assert current["generation_id"] == "generation-new"
            assert current["turn_id"] == "turn-new"
            assert current["done"] is False
            assert current["userStopped"] is False
            assert current.get("error") is None

            assert await Chats.mark_generation_stopped_if_current(
                chat.id,
                message_id,
                "generation-new",
                "turn-new",
            )

            missing_message_id = str(uuid.uuid4())
            assert await Chats.update_generation_message_if_current(
                chat.id,
                missing_message_id,
                "generation-queued",
                "turn-queued",
                {
                    "role": "assistant",
                    "parentId": message_id,
                    "done": True,
                    "userStopped": True,
                },
                create_if_missing=True,
            )
            queued = await Chats.get_message_by_id_and_message_id(
                chat.id, missing_message_id
            )
            assert queued["generation_id"] == "generation-queued"
            assert queued["turn_id"] == "turn-queued"
            assert queued["userStopped"] is True
        finally:
            await Chats.delete_chat_by_id(chat.id)

    asyncio.run(run())
