import asyncio
from types import SimpleNamespace

import pytest

from test.util.db import configure_test_database

configure_test_database()

from open_webui.models import chats as chat_models  # noqa: E402
from open_webui.routers import chats as chat_router  # noqa: E402


def test_full_sync_accepts_branch_versions():
    chat_models._validate_message_graph_for_sync(
        {
            "root": {
                "id": "root",
                "parentId": None,
                "role": "user",
                "childrenIds": ["answer-a", "answer-b"],
            },
            "answer-a": {
                "id": "answer-a",
                "parentId": "root",
                "role": "assistant",
                "childrenIds": [],
            },
            "answer-b": {
                "id": "answer-b",
                "parentId": "root",
                "role": "assistant",
                "childrenIds": [],
            },
        }
    )


def test_full_sync_rejects_a_partial_snapshot_before_delete(monkeypatch):
    class RecordingDb:
        def __init__(self):
            self.statements = []

        def execute(self, statement, params=None):
            self.statements.append((str(statement), params))

    db = RecordingDb()
    monkeypatch.setattr(chat_models, "_chat_message_table_supported", lambda _db: True)

    with pytest.raises(chat_models.ChatMessageParentMissingError) as error:
        chat_models.ChatTable()._sync_messages_to_table(
            db,
            "chat-1",
            {
                "preserved-answer": {
                    "id": "preserved-answer",
                    "parentId": "missing-prompt",
                    "role": "assistant",
                    "content": "This body must not be deleted.",
                }
            },
        )

    assert error.value.code == "chat_message_parent_missing"
    assert db.statements == []


def test_full_sync_rejects_long_parent_cycles():
    with pytest.raises(chat_models.ChatMessageParentMissingError) as error:
        chat_models._validate_message_graph_for_sync(
            {
                "user": {"id": "user", "parentId": "assistant", "role": "user"},
                "assistant": {
                    "id": "assistant",
                    "parentId": "user",
                    "role": "assistant",
                },
            }
        )

    assert error.value.code == "chat_message_parent_cycle"


def test_migrated_read_rebuilds_stale_children_from_parent_links():
    messages = chat_models._normalize_message_graph(
        {
            "prompt": {
                "id": "prompt",
                "parentId": None,
                "childrenIds": ["gone"],
                "role": "user",
            },
            "answer-a": {
                "id": "answer-a",
                "parentId": "prompt",
                "childrenIds": [],
                "role": "assistant",
            },
            "answer-b": {
                "id": "answer-b",
                "parentId": "prompt",
                "role": "assistant",
            },
        }
    )

    assert messages["prompt"]["childrenIds"] == ["answer-a", "answer-b"]


def _chat_model(*, title="Original", messages_migrated=1):
    return chat_models.ChatModel(
        id="chat-1",
        user_id="user-1",
        title=title,
        chat={
            "title": title,
            "history": {
                "currentId": "answer",
                "messages": {
                    "prompt": {"id": "prompt", "parentId": None, "role": "user"},
                    "answer": {
                        "id": "answer",
                        "parentId": "prompt",
                        "role": "assistant",
                    },
                },
            },
        },
        created_at=1,
        updated_at=2,
        archived=False,
        messages_migrated=messages_migrated,
    )


def test_title_only_route_uses_targeted_writer(monkeypatch):
    original = _chat_model()
    renamed = _chat_model(title="Renamed")
    calls = []

    async def get_owned(_id, _user_id):
        return original

    async def update_title(_id, title):
        calls.append(("title", title))
        return renamed

    async def update_whole_chat(*_args, **_kwargs):
        calls.append(("whole-chat", None))
        return renamed

    async def broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_router.Chats, "get_chat_by_id_and_user_id", get_owned)
    monkeypatch.setattr(chat_router.Chats, "update_chat_title_by_id", update_title)
    monkeypatch.setattr(chat_router.Chats, "update_chat_by_id", update_whole_chat)
    monkeypatch.setattr(chat_router, "broadcast_sidebar_event", broadcast)

    response = asyncio.run(
        chat_router.update_chat_by_id(
            SimpleNamespace(headers={}),
            "chat-1",
            chat_models.ChatForm(chat={"title": "Renamed"}),
            SimpleNamespace(id="user-1"),
        )
    )

    assert response.title == "Renamed"
    assert calls == [("title", "Renamed")]


def test_metadata_update_cannot_resync_hydrated_messages(monkeypatch):
    original = _chat_model()
    captured = {}

    async def get_owned(_id, _user_id):
        return original

    async def update_whole_chat(_id, body):
        captured.update(body)
        return original

    monkeypatch.setattr(chat_router.Chats, "get_chat_by_id_and_user_id", get_owned)
    monkeypatch.setattr(chat_router.Chats, "update_chat_by_id", update_whole_chat)

    asyncio.run(
        chat_router.update_chat_by_id(
            SimpleNamespace(headers={}),
            "chat-1",
            chat_models.ChatForm(chat={"params": {"temperature": 0.2}}),
            SimpleNamespace(id="user-1"),
        )
    )

    assert captured["history"]["currentId"] == "answer"
    assert "messages" not in captured["history"]


def test_migrated_generic_update_rejects_message_replacement(monkeypatch):
    async def get_owned(_id, _user_id):
        return _chat_model(messages_migrated=1)

    monkeypatch.setattr(chat_router.Chats, "get_chat_by_id_and_user_id", get_owned)

    with pytest.raises(chat_router.HTTPException) as error:
        asyncio.run(
            chat_router.update_chat_by_id(
                SimpleNamespace(headers={}),
                "chat-1",
                chat_models.ChatForm(
                    chat={"history": {"currentId": "answer", "messages": {}}}
                ),
                SimpleNamespace(id="user-1"),
            )
        )

    assert error.value.status_code == 409


def test_atomic_version_fork_derives_ancestry_and_preserves_source(monkeypatch):
    chat_row = _chat_model(messages_migrated=0)
    chat_row.chat["history"] = {
        "currentId": "answer",
        "messages": {
            "root": {
                "id": "root",
                "parentId": None,
                "childrenIds": ["prompt"],
                "role": "assistant",
                "content": "earlier",
            },
            "prompt": {
                "id": "prompt",
                "parentId": "root",
                "childrenIds": ["answer"],
                "role": "user",
                "content": "original prompt",
                "models": ["model-a"],
            },
            "answer": {
                "id": "answer",
                "parentId": "prompt",
                "childrenIds": [],
                "role": "assistant",
                "content": "original answer",
            },
        },
    }

    class Query:
        def filter(self, *_args):
            return self

        def with_for_update(self):
            return self

        def first(self):
            return chat_row

    class Db:
        def query(self, *_args):
            return Query()

        def commit(self):
            return None

        def rollback(self):
            return None

    class DbContext:
        def __enter__(self):
            return Db()

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(chat_models, "get_db", lambda: DbContext())
    monkeypatch.setattr(chat_models, "_chat_message_table_supported", lambda _db: False)
    monkeypatch.setattr(chat_models, "_upsert_message_search", lambda *_args: None)

    result = chat_models.ChatTable().fork_message_version_atomic(
        "chat-1",
        "prompt",
        "prompt-v2",
        content="edited prompt",
        files=[],
        models=["model-b"],
    )

    messages = chat_row.chat["history"]["messages"]
    assert messages["prompt"]["content"] == "original prompt"
    assert messages["prompt-v2"] == result["message"]
    assert messages["prompt-v2"]["parentId"] == "root"
    assert messages["prompt-v2"]["role"] == "user"
    assert messages["prompt-v2"]["models"] == ["model-b"]
    assert messages["root"]["childrenIds"] == ["prompt", "prompt-v2"]
    assert chat_row.chat["history"]["currentId"] == "prompt-v2"


def test_version_patch_routes_to_atomic_fork_primitive(monkeypatch):
    calls = []

    async def owns(_id, _user_id):
        return True

    async def fork(chat_id, source_id, message_id, **payload):
        calls.append((chat_id, source_id, message_id, payload))
        return {
            "message": {
                "id": message_id,
                "parentId": None,
                "role": "user",
                "content": payload["content"],
            },
            "updated_at": 9,
            "idempotent": False,
        }

    async def broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_router.Chats, "user_owns_chat", owns)
    monkeypatch.setattr(chat_router.Chats, "fork_message_version_atomic", fork)
    monkeypatch.setattr(chat_router, "broadcast_sidebar_event", broadcast)

    response = asyncio.run(
        chat_router.patch_chat_by_id(
            SimpleNamespace(headers={}),
            "chat-1",
            chat_router.PatchChatForm(
                ops=[
                    chat_router.PatchOp(
                        op="fork_message_version",
                        source_message_id="prompt-v1",
                        message_id="prompt-v2",
                        content="edited",
                        models=["model-a"],
                    )
                ]
            ),
            SimpleNamespace(id="user-1"),
        )
    )

    assert response["ops_applied"] == ["fork_message_version"]
    assert calls == [
        (
            "chat-1",
            "prompt-v1",
            "prompt-v2",
            {
                "content": "edited",
                "files": None,
                "models": ["model-a"],
                "user_id": "user-1",
            },
        )
    ]


def test_atomic_append_commits_chain_models_and_pointer_together(monkeypatch):
    chat_row = _chat_model(messages_migrated=0)
    chat_row.chat["history"] = {"currentId": None, "messages": {}}

    class Query:
        def filter(self, *_args):
            return self

        def with_for_update(self):
            return self

        def first(self):
            return chat_row

    class Db:
        commits = 0

        def query(self, *_args):
            return Query()

        def commit(self):
            self.commits += 1

        def rollback(self):
            return None

    db = Db()

    class DbContext:
        def __enter__(self):
            return db

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(chat_models, "get_db", lambda: DbContext())
    monkeypatch.setattr(chat_models, "_chat_message_table_supported", lambda _db: False)
    monkeypatch.setattr(chat_models, "_upsert_message_search", lambda *_args: None)

    result = chat_models.ChatTable().append_messages_atomic(
        "chat-1",
        [
            {
                "id": "prompt",
                "parentId": None,
                "role": "user",
                "content": "question",
            },
            {
                "id": "answer",
                "parentId": "prompt",
                "role": "assistant",
                "content": "",
                "model": "model-a",
                "generation_id": "generation-1",
                "turn_id": "turn-1",
            },
        ],
        current_id="answer",
        update_current_id=True,
        models=["model-a"],
        update_models=True,
        user_id="user-1",
    )

    history = chat_row.chat["history"]
    assert db.commits == 1
    assert history["messages"]["prompt"]["childrenIds"] == ["answer"]
    assert history["messages"]["answer"]["parentId"] == "prompt"
    assert history["currentId"] == "answer"
    assert chat_row.chat["models"] == ["model-a"]
    assert result["idempotent"] is False


def test_append_patch_routes_directly_to_atomic_storage(monkeypatch):
    calls = []

    async def owns(_id, _user_id):
        return True

    async def append(chat_id, messages, **payload):
        calls.append((chat_id, messages, payload))
        return {"messages": messages, "updated_at": 11, "idempotent": False}

    async def forbidden_hydrate(*_args, **_kwargs):
        raise AssertionError("append patch hydrated the full chat")

    async def broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_router.Chats, "user_owns_chat", owns)
    monkeypatch.setattr(chat_router.Chats, "append_messages_atomic", append)
    monkeypatch.setattr(
        chat_router.Chats, "get_chat_by_id_and_user_id", forbidden_hydrate
    )
    monkeypatch.setattr(chat_router, "broadcast_sidebar_event", broadcast)

    response = asyncio.run(
        chat_router.patch_chat_by_id(
            SimpleNamespace(headers={}),
            "chat-1",
            chat_router.PatchChatForm(
                ops=[
                    chat_router.PatchOp(
                        op="append_message",
                        message_id="answer-v2",
                        parent_id="prompt",
                        role="assistant",
                        content="",
                        model="model-a",
                    ),
                    chat_router.PatchOp(
                        op="set_history_current_id", current_id="answer-v2"
                    ),
                ]
            ),
            SimpleNamespace(id="user-1"),
        )
    )

    assert response["ops_applied"] == [
        "append_message",
        "set_history_current_id",
    ]
    assert calls == [
        (
            "chat-1",
            [
                {
                    "id": "answer-v2",
                    "parentId": "prompt",
                    "childrenIds": [],
                    "role": "assistant",
                    "content": "",
                    "model": "model-a",
                }
            ],
            {
                "current_id": "answer-v2",
                "update_current_id": True,
                "models": None,
                "update_models": False,
                "user_id": "user-1",
            },
        )
    ]


def test_legacy_message_edit_endpoint_cannot_overwrite_source(monkeypatch):
    chat = _chat_model()
    calls = []

    async def get_chat(_id):
        return chat

    async def fork(chat_id, source_id, version_id, **payload):
        calls.append((chat_id, source_id, version_id, payload))
        return {
            "message": {
                "id": version_id,
                "parentId": None,
                "role": "user",
                "content": payload["content"],
            },
            "updated_at": 12,
            "idempotent": False,
        }

    monkeypatch.setattr(chat_router.Chats, "get_chat_by_id", get_chat)
    monkeypatch.setattr(chat_router.Chats, "fork_message_version_atomic", fork)
    monkeypatch.setattr(chat_router, "get_event_emitter", lambda *_args, **_kwargs: None)

    response = asyncio.run(
        chat_router.update_chat_message_by_id(
            "chat-1",
            "prompt",
            chat_router.MessageForm(content="edited through legacy API"),
            SimpleNamespace(id="user-1", role="user"),
        )
    )

    assert response.id == "chat-1"
    assert len(calls) == 1
    chat_id, source_id, version_id, payload = calls[0]
    assert chat_id == "chat-1"
    assert source_id == "prompt"
    assert version_id != source_id
    assert payload == {"content": "edited through legacy API", "user_id": "user-1"}


def test_model_updater_cannot_full_sync_a_migrated_read_projection(monkeypatch):
    chat_row = _chat_model(messages_migrated=1)

    class Query:
        def filter(self, *_args):
            return self

        def with_for_update(self):
            return self

        def first(self):
            return chat_row

    class Db:
        def query(self, *_args):
            return Query()

        def commit(self):
            return None

    class DbContext:
        def __enter__(self):
            return Db()

        def __exit__(self, *_args):
            return None

    def forbidden_sync(*_args, **_kwargs):
        raise AssertionError("migrated update attempted a full graph sync")

    monkeypatch.setattr(chat_models, "get_db", lambda: DbContext())
    monkeypatch.setattr(chat_models, "_chat_message_table_supported", lambda _db: True)
    monkeypatch.setattr(chat_models, "_hydrate_chat_messages", lambda *_args: None)
    monkeypatch.setattr(chat_models.ChatTable, "_sync_messages_to_table", forbidden_sync)
    monkeypatch.setattr(
        chat_models.ChatTable, "_lock_and_read_queue", lambda *_args: None
    )
    monkeypatch.setattr(
        chat_models.ChatTable, "_read_live_question_states", lambda *_args: None
    )

    result = chat_models.ChatTable().update_chat_by_id("chat-1", chat_row.chat)

    assert result is not None
    assert "messages" not in chat_row.chat["history"]


def test_overview_route_returns_all_owned_branch_summaries(monkeypatch):
    rows = [
        {"id": "prompt", "parentId": None, "role": "user", "preview": "Question"},
        {
            "id": "answer",
            "parentId": "prompt",
            "role": "assistant",
            "preview": "Answer",
        },
    ]

    async def owns(_id, _user_id):
        return True

    async def overview(_id):
        return rows

    monkeypatch.setattr(chat_router.Chats, "user_owns_chat", owns)
    monkeypatch.setattr(chat_router.Chats, "get_chat_messages_overview", overview)

    response = asyncio.run(
        chat_router.get_chat_messages_overview(
            "chat-1", SimpleNamespace(id="user-1")
        )
    )

    assert response == rows
