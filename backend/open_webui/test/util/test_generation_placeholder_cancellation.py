import asyncio
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from test.util.db import configure_test_database

configure_test_database(required=True)

from open_webui.models.chats import (  # noqa: E402
    ChatForm,
    ChatMessageParentMissingError,
    Chats,
)
from open_webui.routers import chats as chats_router  # noqa: E402
import open_webui.tasks as task_registry  # noqa: E402


def test_cancel_latch_wins_when_placeholder_patch_commits_later(monkeypatch):
    async def run():
        user_id = f"user-{uuid.uuid4()}"
        generation_id = f"generation-{uuid.uuid4()}"
        turn_id = f"turn-{uuid.uuid4()}"
        message_id = f"assistant-{uuid.uuid4()}"
        chat = await Chats.insert_new_chat(
            user_id,
            ChatForm(
                chat={
                    "title": "placeholder cancellation",
                    "history": {"currentId": None, "messages": {}},
                }
            ),
        )

        async def ignore_sidebar_event(*_args, **_kwargs):
            return None

        monkeypatch.setattr(
            chats_router,
            "broadcast_sidebar_event",
            ignore_sidebar_event,
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(redis=None)),
            headers={},
        )
        user = SimpleNamespace(id=user_id)

        try:
            await task_registry.mark_generation_turn_cancelled(None, chat.id, turn_id)
            await chats_router.patch_chat_by_id(
                request,
                chat.id,
                chats_router.PatchChatForm(
                    ops=[
                        chats_router.PatchOp(
                            op="append_message",
                            message_id=message_id,
                            parent_id=None,
                            role="assistant",
                            content="",
                            generation_id=generation_id,
                            turn_id=turn_id,
                        )
                    ]
                ),
                user,
            )

            message = await Chats.get_message_by_id_and_message_id(chat.id, message_id)
            assert message["generation_id"] == generation_id
            assert message["turn_id"] == turn_id
            assert message["done"] is True
            assert message["userStopped"] is True
        finally:
            await Chats.delete_chat_by_id(chat.id)
            task_registry.generation_cancel_intents.clear()
            task_registry.generation_turn_cancel_intents.clear()

    asyncio.run(run())


def test_patch_rejects_child_until_parent_is_durable(monkeypatch):
    async def run():
        user_id = f"user-{uuid.uuid4()}"
        chat = await Chats.insert_new_chat(
            user_id,
            ChatForm(
                chat={
                    "title": "parent durability",
                    "history": {"currentId": None, "messages": {}},
                }
            ),
        )

        async def ignore_sidebar_event(*_args, **_kwargs):
            return None

        monkeypatch.setattr(
            chats_router, "broadcast_sidebar_event", ignore_sidebar_event
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(redis=None)),
            headers={},
        )
        user = SimpleNamespace(id=user_id)
        child_id = f"child-{uuid.uuid4()}"
        missing_parent_id = f"parent-{uuid.uuid4()}"

        try:
            with pytest.raises(HTTPException) as error:
                await chats_router.patch_chat_by_id(
                    request,
                    chat.id,
                    chats_router.PatchChatForm(
                        ops=[
                            chats_router.PatchOp(
                                op="append_message",
                                message_id=child_id,
                                parent_id=missing_parent_id,
                                role="user",
                                content="continue",
                            )
                        ]
                    ),
                    user,
                )

            assert error.value.status_code == 409
            assert error.value.detail["code"] == "chat_message_parent_missing"
            assert (
                await Chats.get_message_by_id_and_message_id(chat.id, child_id) is None
            )

            with pytest.raises(ChatMessageParentMissingError):
                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    chat.id,
                    child_id,
                    {
                        "id": child_id,
                        "parentId": missing_parent_id,
                        "role": "user",
                        "content": "continue",
                    },
                    return_model=False,
                )
        finally:
            await Chats.delete_chat_by_id(chat.id)

    asyncio.run(run())


def test_patch_accepts_parent_then_child_in_one_ordered_batch(monkeypatch):
    async def run():
        user_id = f"user-{uuid.uuid4()}"
        chat = await Chats.insert_new_chat(
            user_id,
            ChatForm(
                chat={
                    "title": "ordered parent batch",
                    "history": {"currentId": None, "messages": {}},
                }
            ),
        )

        async def ignore_sidebar_event(*_args, **_kwargs):
            return None

        monkeypatch.setattr(
            chats_router, "broadcast_sidebar_event", ignore_sidebar_event
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(redis=None)),
            headers={},
        )
        user = SimpleNamespace(id=user_id)
        parent_id = f"parent-{uuid.uuid4()}"
        child_id = f"child-{uuid.uuid4()}"

        try:
            await chats_router.patch_chat_by_id(
                request,
                chat.id,
                chats_router.PatchChatForm(
                    ops=[
                        chats_router.PatchOp(
                            op="append_message",
                            message_id=parent_id,
                            parent_id=None,
                            role="assistant",
                            content="",
                        ),
                        chats_router.PatchOp(
                            op="append_message",
                            message_id=child_id,
                            parent_id=parent_id,
                            role="user",
                            content="continue",
                        ),
                    ]
                ),
                user,
            )

            child = await Chats.get_message_by_id_and_message_id(chat.id, child_id)
            assert child["parentId"] == parent_id
        finally:
            await Chats.delete_chat_by_id(chat.id)

    asyncio.run(run())
