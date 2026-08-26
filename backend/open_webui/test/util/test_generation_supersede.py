import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from test.util.db import configure_test_database

configure_test_database()

import open_webui.main as main  # noqa: E402


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))


def _user():
    return SimpleNamespace(id="user-1", role="user")


def _form():
    return {
        "chat_id": "chat-1",
        "id": "assistant-new",
        "generation_id": "generation-new",
        "turn_id": "turn-new",
        "supersede_active_turn": True,
    }


def _install_fakes(monkeypatch, *, pending_task_ids=()):
    events = []
    displaced = {
        "generation_id": "generation-old",
        "chat_id": "chat-1",
        "message_id": "assistant-old",
        "turn_id": "turn-old",
        "task_id": "task-old",
    }

    async def get_owned_chat(*_args, **_kwargs):
        return SimpleNamespace(id="chat-1")

    async def supersede(_redis, operation):
        events.append(("claim", dict(operation)))
        return {"registration": "acquired", "displaced": [displaced]}

    async def persist(_chat_id, operations):
        events.append(("persist", [dict(operation) for operation in operations]))

    async def stop_and_wait(_redis, task_ids, **_kwargs):
        events.append(("stop", list(task_ids)))
        return list(pending_task_ids)

    async def process(_request, form_data, _user, *, generation_operation):
        events.append(("process", dict(form_data), dict(generation_operation)))
        return {"status": True, "started": True}

    async def mark_cancelled(_redis, chat_id, generation_id):
        events.append(("cancel-new", chat_id, generation_id))

    async def finish_supersede(_redis, chat_id, turn_id):
        events.append(("finish", chat_id, turn_id))

    async def unregister(_redis, operation):
        events.append(("unregister", dict(operation)))

    monkeypatch.setattr(main.Chats, "get_chat_by_id_and_user_id", get_owned_chat)
    monkeypatch.setattr(main, "supersede_generation_operation", supersede)
    monkeypatch.setattr(main, "_persist_stopped_generation_operations", persist)
    monkeypatch.setattr(main, "stop_tasks_and_wait", stop_and_wait)
    monkeypatch.setattr(main, "_chat_completion_impl", process)
    monkeypatch.setattr(main, "mark_generation_cancelled", mark_cancelled)
    monkeypatch.setattr(main, "finish_generation_supersede", finish_supersede)
    monkeypatch.setattr(main, "unregister_generation_operation", unregister)
    return events


def test_redo_waits_for_displaced_task_before_provider_work(monkeypatch):
    async def run():
        events = _install_fakes(monkeypatch)
        form = _form()

        result = await main._chat_completion_with_operation(_request(), form, _user())

        assert result == {"status": True, "started": True}
        assert [event[0] for event in events] == [
            "claim",
            "persist",
            "stop",
            "persist",
            "finish",
            "process",
            "unregister",
        ]
        assert events[2] == ("stop", ["task-old"])
        assert events[4] == ("finish", "chat-1", "turn-new")
        assert "supersede_active_turn" not in events[5][1]
        assert "supersede_active_turn" not in form

    asyncio.run(run())


def test_redo_fails_closed_when_displaced_task_does_not_stop(monkeypatch):
    async def run():
        events = _install_fakes(monkeypatch, pending_task_ids=["task-old"])

        with pytest.raises(HTTPException) as raised:
            await main._chat_completion_with_operation(_request(), _form(), _user())

        assert raised.value.status_code == 503
        assert raised.value.detail["code"] == "turn_supersede_timeout"
        assert not any(event[0] == "process" for event in events)
        assert ("cancel-new", "chat-1", "generation-new") in events

    asyncio.run(run())
