import asyncio
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text

from test.util.db import configure_test_database

configure_test_database(required=True)

from open_webui.internal.db import get_db, run_sync_db  # noqa: E402
from open_webui.models.chats import (  # noqa: E402
    ChatForm,
    ChatMessageParentMissingError,
    Chats,
)
from open_webui.routers import chats as chats_router  # noqa: E402
import open_webui.tasks as task_registry  # noqa: E402


async def _new_chat(title: str):
    user_id = f"user-{uuid.uuid4()}"
    chat = await Chats.insert_new_chat(
        user_id,
        ChatForm(
            chat={
                "title": title,
                "history": {"currentId": None, "messages": {}},
            }
        ),
    )
    return user_id, chat


async def _put(chat_id: str, message_id: str, parent_id, role: str):
    await Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        message_id,
        {
            "id": message_id,
            "parentId": parent_id,
            "role": role,
            "content": message_id,
            "childrenIds": [],
            "timestamp": 1,
        },
        return_model=False,
    )


def test_atomic_delete_removes_direct_children_and_relinks_grandchildren():
    async def run():
        _user_id, chat = await _new_chat("atomic graph delete")
        try:
            await _put(chat.id, "u0", None, "user")
            await _put(chat.id, "a0", "u0", "assistant")
            await _put(chat.id, "u1", "a0", "user")
            await _put(chat.id, "a1", "u1", "assistant")
            await _put(chat.id, "sibling", "a0", "user")
            await _put(chat.id, "u2", "a1", "user")
            await _put(chat.id, "a2", "u2", "assistant")
            await Chats.set_history_current_id_atomic(chat.id, "a2")

            result = await Chats.delete_message_with_relink_atomic(chat.id, "u1")

            assert result["deleted_ids"] == ["u1", "a1"]
            assert result["relinked_ids"] == ["u2"]
            assert result["current_id"] == "a2"

            messages = await Chats.get_messages_map_by_chat_id(chat.id)
            assert set(messages) == {"u0", "a0", "sibling", "u2", "a2"}
            assert messages["u2"]["parentId"] == "a0"
            assert messages["a0"]["childrenIds"] == ["sibling", "u2"]
            assert messages["u2"]["childrenIds"] == ["a2"]
            for message in messages.values():
                parent_id = message.get("parentId")
                assert parent_id is None or parent_id in messages
        finally:
            await Chats.delete_chat_by_id(chat.id)

    asyncio.run(run())


def test_delete_route_stops_active_writer_before_graph_transaction(monkeypatch):
    async def run():
        user_id, chat = await _new_chat("delete quiesces writer")
        worker_task = None
        started = asyncio.Event()

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

        try:
            await _put(chat.id, "u0", None, "user")
            await _put(chat.id, "a0", "u0", "assistant")
            await _put(chat.id, "u1", "a0", "user")
            await _put(chat.id, "a1", "u1", "assistant")

            operation = {
                "generation_id": f"generation-{uuid.uuid4()}",
                "chat_id": chat.id,
                "message_id": "a1",
                "turn_id": f"turn-{uuid.uuid4()}",
                "task_id": "",
            }
            assert (
                await task_registry.register_generation_operation(None, operation)
                == "acquired"
            )

            async def active_writer():
                started.set()
                await asyncio.Event().wait()

            _task_id, worker_task = await task_registry.create_task(
                None,
                active_writer(),
                id=chat.id,
                generation_operation=operation,
                admission_chat_id=chat.id,
            )
            await started.wait()

            result = await chats_router.patch_chat_by_id(
                request,
                chat.id,
                chats_router.PatchChatForm(
                    ops=[
                        chats_router.PatchOp(
                            op="delete_message",
                            message_id="u1",
                        )
                    ]
                ),
                user,
            )

            assert result["ops_applied"] == ["delete_message"]
            assert worker_task.cancelled()
            assert not await task_registry.list_generation_operations_by_item(
                None, chat.id
            )
            assert not await task_registry.is_chat_work_blocked(None, chat.id)
            assert await Chats.get_message_by_id_and_message_id(chat.id, "u1") is None
            assert await Chats.get_message_by_id_and_message_id(chat.id, "a1") is None
        finally:
            if worker_task is not None and not worker_task.done():
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass
            await Chats.delete_chat_by_id(chat.id)
            task_registry.generation_operations.clear()
            task_registry.item_generation_operations.clear()
            task_registry.generation_cancel_intents.clear()
            task_registry.generation_turn_cancel_intents.clear()
            task_registry.chat_work_blocks.clear()

    asyncio.run(run())


def test_partial_writer_cannot_extend_an_orphaned_row():
    async def run():
        _user_id, chat = await _new_chat("orphan writes fail closed")
        try:
            await _put(chat.id, "user", None, "user")
            await _put(chat.id, "assistant", "user", "assistant")

            # Simulate corruption imported from an older build. This bypasses
            # application invariants deliberately so the write-boundary check is
            # exercised against an existing orphan, not only a new insert.
            def delete_parent_outside_application_invariants():
                with get_db() as db:
                    db.execute(
                        text(
                            "DELETE FROM chat_message "
                            "WHERE chat_id = :cid AND message_id = 'user'"
                        ),
                        {"cid": chat.id},
                    )
                    db.commit()

            await run_sync_db(delete_parent_outside_application_invariants)

            with pytest.raises(ChatMessageParentMissingError):
                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    chat.id,
                    "assistant",
                    {"content": "late checkpoint"},
                    return_model=False,
                )
        finally:
            await Chats.delete_chat_by_id(chat.id)

    asyncio.run(run())
