from types import SimpleNamespace

import asyncio
import inspect
from unittest.mock import patch

import pytest

import open_webui.utils.subagent as subagent_module
from open_webui.utils.chat import (
    ActiveSubagentRerunError,
    active_detached_subagent_rerun_entries,
)
from open_webui.utils.subagent import (
    SubagentRerunBlockedError,
    _apply_subagent_placeholder_patch,
    _current_completed_subagent_result,
    _validate_parent_subagent_result_unconsumed,
    _validate_subagent_turn_is_latest,
)


def _chat(current_id, messages):
    return SimpleNamespace(chat={"history": {"currentId": current_id, "messages": messages}})


def _subagent_call_block(call_id="tc1", subagent_id="sa1", result_content=None):
    block = {
        "type": "tool_calls",
        "content": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "subagent_launch", "arguments": "{}"},
            }
        ],
    }
    if result_content is not None:
        block["results"] = [
            {
                "tool_call_id": call_id,
                "content": result_content,
                "subagent_id": subagent_id,
            }
        ]
    return block


def _run(call_id="tc1", subagent_id="sa1", assistant_msg_id="sa_asst"):
    return {
        "tool_call_id": call_id,
        "subagent_id": subagent_id,
        "chat_id": subagent_id,
        "assistant_msg_id": assistant_msg_id,
    }


def test_sync_placeholder_matches_tool_call_id_field():
    message = {
        "content": "",
        "content_blocks": [
            {
                "type": "tool_calls",
                "content": [
                    {
                        "tool_call_id": "tc1",
                        "type": "function",
                        "function": {"name": "subagent_launch", "arguments": "{}"},
                    }
                ],
                "results": [],
            }
        ],
    }

    update_data: dict = {}
    _apply_subagent_placeholder_patch(
        message,
        {
            **_run("tc1", "sa1"),
            "entry_key": "sa1",
            "status": "done",
            "final_text": "new answer",
        },
        update_data,
    )

    blocks = update_data["content_blocks"]
    assert len(blocks) == 1
    assert blocks[0]["results"][0]["tool_call_id"] == "tc1"
    assert blocks[0]["results"][0]["content"] == "new answer"


def test_sync_placeholder_can_disable_append_for_rerun():
    message = {
        "content": "",
        "content_blocks": [_subagent_call_block("other", "other")],
    }

    update_data: dict = {}
    _apply_subagent_placeholder_patch(
        message,
        {
            **_run("tc1", "sa1"),
            "entry_key": "sa1",
            "status": "done",
            "final_text": "new answer",
        },
        update_data,
        allow_append=False,
    )

    # No matching block for this subagent + allow_append=False → nothing written.
    assert update_data == {}


def test_sync_placeholder_does_not_revert_concurrent_parent_blocks():
    # Regression for the placeholder clobber: the patch must operate on the
    # message it was GIVEN (the fresh read under the lock) and only touch the
    # matching block, leaving any newer sibling blocks intact.
    message = {
        "content": "",
        "content_blocks": [
            {
                "type": "tool_calls",
                "content": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "subagent_launch", "arguments": "{}"},
                    }
                ],
                "results": [],
            },
            {"type": "text", "content": "parent streamed this AFTER the launch"},
        ],
    }
    update_data: dict = {}
    _apply_subagent_placeholder_patch(
        message,
        {**_run("tc1", "sa1"), "entry_key": "sa1", "status": "done", "final_text": "ans"},
        update_data,
    )
    blocks = update_data["content_blocks"]
    # The trailing parent text block survives; only the launch block got a result.
    assert blocks[-1] == {"type": "text", "content": "parent streamed this AFTER the launch"}
    assert blocks[0]["results"][0]["content"] == "ans"


def test_sync_placeholder_success_clears_stale_result_error_flags():
    message = {
        "content": "",
        "content_blocks": [
            {
                **_subagent_call_block("tc1", "sa1"),
                "results": [
                    {
                        "tool_call_id": "tc1",
                        "subagent_id": "sa1",
                        "content": "old failure",
                        "error": True,
                        "error_reason": "provider failed",
                    }
                ],
            }
        ],
    }
    update_data: dict = {}
    _apply_subagent_placeholder_patch(
        message,
        {
            **_run("tc1", "sa1"),
            "entry_key": "sa1",
            "status": "done",
            "final_text": "replacement answer",
        },
        update_data,
    )

    result = update_data["content_blocks"][0]["results"][0]
    assert result["content"] == "replacement answer"
    assert "error" not in result
    assert "error_reason" not in result


def test_current_completed_subagent_result_follows_selected_rewind_branch():
    hidden = SimpleNamespace(
        chat={
            "history": {
                "currentId": "replacement",
                "messages": {
                    "failed": {
                        "id": "failed",
                        "role": "assistant",
                        "done": False,
                        "error": {"content": "old failure"},
                    },
                    "replacement": {
                        "id": "replacement",
                        "role": "assistant",
                        "done": True,
                        "error": None,
                        "userStopped": False,
                        "timestamp": 123,
                        "content": "",
                        "content_blocks": [
                            {"type": "reasoning", "content": "work"},
                            {"type": "text", "content": "fresh repaired answer"},
                        ],
                    },
                },
            }
        }
    )

    message_id, text, timestamp = _current_completed_subagent_result(hidden)
    assert message_id == "replacement"
    assert text == "fresh repaired answer"
    assert timestamp == 123


def test_current_completed_subagent_result_requires_clicked_turn_sibling():
    hidden = SimpleNamespace(
        chat={
            "history": {
                "currentId": "later-answer",
                "messages": {
                    "clicked-user": {
                        "id": "clicked-user",
                        "role": "user",
                        "content": "clicked turn",
                    },
                    "later-user": {
                        "id": "later-user",
                        "role": "user",
                        "content": "later continuation",
                    },
                    "later-answer": {
                        "id": "later-answer",
                        "parentId": "later-user",
                        "role": "assistant",
                        "done": True,
                        "error": None,
                        "content": "answer to the later continuation",
                    },
                },
            }
        }
    )

    with pytest.raises(ValueError, match="different subagent turn"):
        _current_completed_subagent_result(
            hidden,
            run_entry={
                "user_msg_id": "clicked-user",
                "assistant_msg_id": "original-clicked-answer",
            },
        )


def test_current_completed_subagent_result_accepts_repaired_assistant_sibling():
    hidden = SimpleNamespace(
        chat={
            "history": {
                "currentId": "replacement",
                "messages": {
                    "clicked-user": {"id": "clicked-user", "role": "user"},
                    "original": {
                        "id": "original",
                        "parentId": "clicked-user",
                        "role": "assistant",
                        "done": True,
                        "error": {"content": "old failure"},
                    },
                    "replacement": {
                        "id": "replacement",
                        "parentId": "clicked-user",
                        "role": "assistant",
                        "done": True,
                        "error": None,
                        "content": "repaired sibling",
                    },
                },
            }
        }
    )

    message_id, text, _timestamp = _current_completed_subagent_result(
        hidden,
        run_entry={
            "user_msg_id": "clicked-user",
            "assistant_msg_id": "original",
        },
    )
    assert message_id == "replacement"
    assert text == "repaired sibling"


def test_prepare_rewind_adopt_branch_updates_all_runs_and_tool_results_together():
    source = {
        "id": "source",
        "parentId": "user1",
        "role": "assistant",
        "model": "parent-model",
        "modelName": "Parent Model",
        "modelIdx": 0,
        "content_blocks": [
            {
                "type": "tool_calls",
                "content": [
                    {
                        "id": "tc1",
                        "function": {"name": "subagent_launch", "arguments": "{}"},
                    },
                    {
                        "id": "tc2",
                        "function": {"name": "subagent_launch", "arguments": "{}"},
                    },
                ],
                "results": [
                    {
                        "tool_call_id": "tc1",
                        "subagent_id": "sa1",
                        "content": "old error 1",
                        "error": True,
                    },
                    {
                        "tool_call_id": "tc2",
                        "subagent_id": "sa2",
                        "content": "old error 2",
                        "error": True,
                    },
                ],
            },
            {"type": "text", "content": "old parent continuation"},
        ],
        "subagent_runs": {
            "sa1": {
                **_run("tc1", "sa1", "old-a1"),
                "entry_key": "sa1",
                "user_msg_id": "u-sa1",
                "status": "error",
                "final_text": "old error 1",
            },
            "sa2": {
                **_run("tc2", "sa2", "old-a2"),
                "entry_key": "sa2",
                "user_msg_id": "u-sa2",
                "status": "error",
                "final_text": "old error 2",
            },
        },
    }

    branch = subagent_module._prepare_rewind_adopt_branch(
        source_message=source,
        source_message_id="source",
        branch_message_id="branch",
        resolved_entries=[
            ("sa1", source["subagent_runs"]["sa1"]),
            ("sa2", source["subagent_runs"]["sa2"]),
        ],
        completed_results={
            "sa1": ("new-a1", "repaired answer 1", 100),
            "sa2": ("new-a2", "repaired answer 2", 200),
        },
        operation_id="operation",
    )

    assert branch["id"] == "branch"
    assert branch["done"] is True
    assert branch["content_blocks"][-1] == {"type": "text", "content": ""}
    assert "old parent continuation" not in str(branch["content_blocks"])
    results = branch["content_blocks"][0]["results"]
    assert [result["content"] for result in results] == [
        "repaired answer 1",
        "repaired answer 2",
    ]
    assert all("error" not in result for result in results)
    for key, expected in (("sa1", "repaired answer 1"), ("sa2", "repaired answer 2")):
        run = branch["subagent_runs"][key]
        assert run["status"] == "done"
        assert run["error"] is None
        assert run["final_text"] == expected
        assert run["parent_message_id"] == "branch"
    # The pure builder cannot mutate the old sibling it is preserving.
    assert source["content_blocks"][0]["results"][0]["content"] == "old error 1"
    assert source["subagent_runs"]["sa1"]["status"] == "error"


def test_prepare_rewind_rerun_branch_is_coherent_terminal_checkpoint():
    source, _children = _atomic_rewind_fixture()
    source_message = source.chat["history"]["messages"]["source"]

    branch = subagent_module._prepare_rewind_rerun_branch(
        source_message=source_message,
        source_message_id="source",
        branch_message_id="branch",
        resolved_entries=[
            ("sa1", source_message["subagent_runs"]["sa1"]),
            ("sa2", source_message["subagent_runs"]["sa2"]),
        ],
        operation_id="operation",
    )

    assert branch["id"] == "branch"
    assert branch["done"] is True
    assert branch["rewind_operation"] == {
        "id": "operation",
        "source_message_id": "source",
        "kind": "subagent_rerun",
    }
    assert branch["content_blocks"][-1] == {"type": "text", "content": ""}
    assert "already continued" not in str(branch["content_blocks"])
    # Prior answers remain coherent until each detached rerun atomically claims
    # and replaces its own key.
    assert [r["content"] for r in branch["content_blocks"][0]["results"]] == [
        "old 1",
        "old 2",
    ]
    assert {
        run["parent_message_id"] for run in branch["subagent_runs"].values()
    } == {"branch"}


def _atomic_rewind_fixture(second_child_error=False):
    runs = {
        "sa1": {
            **_run("tc1", "sa1", "old-a1"),
            "entry_key": "sa1",
            "user_msg_id": "u-sa1",
            "status": "error",
        },
        "sa2": {
            **_run("tc2", "sa2", "old-a2"),
            "entry_key": "sa2",
            "user_msg_id": "u-sa2",
            "status": "error",
        },
    }
    parent = SimpleNamespace(
        id="parent1",
        chat={
            "history": {
                "currentId": "source",
                "messages": {
                    "source": {
                        "id": "source",
                        "_rev": "parent-message-rev",
                        "parentId": "parent-user",
                        "role": "assistant",
                        "done": True,
                        "model": "parent-model",
                        "content_blocks": [
                            {
                                "type": "tool_calls",
                                "content": [
                                    {
                                        "id": "tc1",
                                        "function": {
                                            "name": "subagent_launch",
                                            "arguments": "{}",
                                        },
                                    },
                                    {
                                        "id": "tc2",
                                        "function": {
                                            "name": "subagent_launch",
                                            "arguments": "{}",
                                        },
                                    },
                                ],
                                "results": [
                                    {
                                        "tool_call_id": "tc1",
                                        "subagent_id": "sa1",
                                        "content": "old 1",
                                        "error": True,
                                    },
                                    {
                                        "tool_call_id": "tc2",
                                        "subagent_id": "sa2",
                                        "content": "old 2",
                                        "error": True,
                                    },
                                ],
                            },
                            {"type": "text", "content": "already continued"},
                        ],
                        "subagent_runs": runs,
                    }
                },
            }
        },
    )

    children = {}
    for number, child_id in enumerate(("sa1", "sa2"), start=1):
        leaf_id = f"new-a{number}"
        children[child_id] = SimpleNamespace(
            id=child_id,
            subagent_of="parent1",
            meta={"subagent_of": "parent1"},
            chat={
                "history": {
                    "currentId": leaf_id,
                    "messages": {
                        leaf_id: {
                            "id": leaf_id,
                            "_rev": f"leaf-rev-{number}",
                            "parentId": f"u-sa{number}",
                            "role": "assistant",
                            "done": True,
                            "error": (
                                {"content": "still failed"}
                                if second_child_error and child_id == "sa2"
                                else None
                            ),
                            "content": f"repaired {number}",
                            "timestamp": number,
                        }
                    },
                }
            },
        )
    return parent, children


def test_rewind_source_must_be_on_selected_branch():
    parent = _chat(
        "selected-answer",
        {
            "root-user": {
                "id": "root-user",
                "parentId": None,
                "childrenIds": ["selected-source", "other-source"],
            },
            "selected-source": {
                "id": "selected-source",
                "parentId": "root-user",
                "childrenIds": ["selected-user"],
            },
            "selected-user": {
                "id": "selected-user",
                "parentId": "selected-source",
                "childrenIds": ["selected-answer"],
            },
            "selected-answer": {
                "id": "selected-answer",
                "parentId": "selected-user",
                "childrenIds": [],
            },
            "other-source": {
                "id": "other-source",
                "parentId": "root-user",
                "childrenIds": [],
            },
        },
    )

    assert (
        subagent_module._rewind_current_id_for_source(parent, "selected-source")
        == "selected-answer"
    )
    with pytest.raises(SubagentRerunBlockedError) as exc:
        subagent_module._rewind_current_id_for_source(parent, "other-source")
    assert exc.value.code == "rewind_source_off_branch"


def test_atomic_rewind_adopt_sends_one_complete_guarded_commit(monkeypatch):
    parent, children = _atomic_rewind_fixture()
    captured = []

    async def fake_get_chat(chat_id, user_id):
        return parent if chat_id == "parent1" else children.get(chat_id)

    async def fake_validator(chat_id, user_id):
        if chat_id == "parent1":
            return {"xmin": "parent-chat-rev", "current_id": "source"}
        number = "1" if chat_id == "sa1" else "2"
        return {"xmin": f"child-chat-rev-{number}", "current_id": f"new-a{number}"}

    async def fake_commit(chat_id, source_message_id, branch, **kwargs):
        captured.append((chat_id, source_message_id, branch, kwargs))
        return {"message": branch, "idempotent": False, "updated_at": 999}

    monkeypatch.setattr(
        subagent_module.Chats, "get_chat_by_id_and_user_id", fake_get_chat
    )
    monkeypatch.setattr(
        subagent_module.Chats, "get_chat_open_validator", fake_validator
    )
    monkeypatch.setattr(
        subagent_module.Chats, "append_rewind_branch_atomic", fake_commit
    )

    result = asyncio.run(
        subagent_module.rewind_adopt_subagent_current_results(
            user=SimpleNamespace(id="user1"),
            parent_chat_id="parent1",
            source_parent_message_id="source",
            branch_message_id="branch",
            entry_keys=["sa1", "sa2"],
            operation_id="operation",
        )
    )

    assert len(captured) == 1
    chat_id, source_id, branch, guards = captured[0]
    assert (chat_id, source_id) == ("parent1", "source")
    assert guards["expected_source_rev"] == "parent-message-rev"
    assert guards["expected_current_id"] == "source"
    assert {
        (guard["chat_id"], guard["chat_rev"], guard["message_rev"])
        for guard in guards["expected_related_leaves"]
    } == {
        ("sa1", "child-chat-rev-1", "leaf-rev-1"),
        ("sa2", "child-chat-rev-2", "leaf-rev-2"),
    }
    assert branch["subagent_runs"]["sa1"]["final_text"] == "repaired 1"
    assert branch["subagent_runs"]["sa2"]["final_text"] == "repaired 2"
    assert result["updated_at"] == 999
    assert len(result["adoptions"]) == 2


def test_atomic_rewind_adopt_accepts_source_ancestor_on_selected_branch(
    monkeypatch,
):
    parent, children = _atomic_rewind_fixture()
    messages = parent.chat["history"]["messages"]
    messages["source"]["childrenIds"] = ["later-user"]
    messages["later-user"] = {
        "id": "later-user",
        "parentId": "source",
        "childrenIds": ["later-answer"],
        "role": "user",
        "content": "later question",
    }
    messages["later-answer"] = {
        "id": "later-answer",
        "parentId": "later-user",
        "childrenIds": [],
        "role": "assistant",
        "done": True,
        "content": "later work that must remain on the old branch",
    }
    parent.chat["history"]["currentId"] = "later-answer"
    captured = []

    async def fake_get_chat(chat_id, user_id):
        return parent if chat_id == "parent1" else children.get(chat_id)

    async def fake_validator(chat_id, user_id):
        if chat_id == "parent1":
            return {
                "xmin": "parent-chat-rev",
                "current_id": "later-answer",
            }
        number = "1" if chat_id == "sa1" else "2"
        return {
            "xmin": f"child-chat-rev-{number}",
            "current_id": f"new-a{number}",
        }

    async def fake_commit(chat_id, source_message_id, branch, **kwargs):
        captured.append((chat_id, source_message_id, branch, kwargs))
        return {"message": branch, "idempotent": False, "updated_at": 1001}

    monkeypatch.setattr(
        subagent_module.Chats, "get_chat_by_id_and_user_id", fake_get_chat
    )
    monkeypatch.setattr(
        subagent_module.Chats, "get_chat_open_validator", fake_validator
    )
    monkeypatch.setattr(
        subagent_module.Chats, "append_rewind_branch_atomic", fake_commit
    )

    asyncio.run(
        subagent_module.rewind_adopt_subagent_current_results(
            user=SimpleNamespace(id="user1"),
            parent_chat_id="parent1",
            source_parent_message_id="source",
            branch_message_id="branch",
            entry_keys=["sa1", "sa2"],
            operation_id="operation",
        )
    )

    assert len(captured) == 1
    assert captured[0][3]["expected_current_id"] == "later-answer"


def test_atomic_rewind_adopt_failed_preflight_creates_no_branch(monkeypatch):
    parent, children = _atomic_rewind_fixture(second_child_error=True)
    commit_called = False

    async def fake_get_chat(chat_id, user_id):
        return parent if chat_id == "parent1" else children.get(chat_id)

    async def forbidden_commit(*args, **kwargs):
        nonlocal commit_called
        commit_called = True
        raise AssertionError("commit must not run after failed preflight")

    monkeypatch.setattr(
        subagent_module.Chats, "get_chat_by_id_and_user_id", fake_get_chat
    )
    monkeypatch.setattr(
        subagent_module.Chats, "append_rewind_branch_atomic", forbidden_commit
    )

    with pytest.raises(ValueError, match="selected answer failed"):
        asyncio.run(
            subagent_module.rewind_adopt_subagent_current_results(
                user=SimpleNamespace(id="user1"),
                parent_chat_id="parent1",
                source_parent_message_id="source",
                branch_message_id="branch",
                entry_keys=["sa1", "sa2"],
            )
        )
    assert commit_called is False


def test_atomic_rewind_rerun_sends_one_guarded_commit(monkeypatch):
    parent, _children = _atomic_rewind_fixture()
    captured = []

    async def fake_get_chat(chat_id, user_id):
        return parent if chat_id == "parent1" else None

    async def fake_validator(chat_id, user_id):
        return {"xmin": "parent-chat-rev", "current_id": "source"}

    async def fake_commit(chat_id, source_message_id, branch, **kwargs):
        captured.append((chat_id, source_message_id, branch, kwargs))
        return {"message": branch, "idempotent": False, "updated_at": 1000}

    monkeypatch.setattr(
        subagent_module.Chats, "get_chat_by_id_and_user_id", fake_get_chat
    )
    monkeypatch.setattr(
        subagent_module.Chats, "get_chat_open_validator", fake_validator
    )
    monkeypatch.setattr(
        subagent_module.Chats, "append_rewind_branch_atomic", fake_commit
    )

    result = asyncio.run(
        subagent_module.rewind_subagent_runs_for_rerun(
            user=SimpleNamespace(id="user1"),
            parent_chat_id="parent1",
            source_parent_message_id="source",
            branch_message_id="branch",
            entry_keys=["sa1", "sa2"],
            operation_id="operation",
        )
    )

    assert len(captured) == 1
    chat_id, source_id, branch, guards = captured[0]
    assert (chat_id, source_id) == ("parent1", "source")
    assert guards["expected_source_rev"] == "parent-message-rev"
    assert guards["expected_current_id"] == "source"
    assert "expected_related_leaves" not in guards
    assert branch["rewind_operation"]["kind"] == "subagent_rerun"
    assert result["parent_message_id"] == "branch"
    assert result["entry_keys"] == ["sa1", "sa2"]
    assert result["updated_at"] == 1000


def test_atomic_rewind_commit_has_one_transaction_boundary():
    """Keep the DB primitive structurally all-or-nothing.

    This is deliberately a source-level invariant test: exercising the
    Postgres SQL against a shared developer database would require creating and
    deleting real chats, while the property at risk is accidental insertion of
    an intermediate commit between the sibling/result write and currentId
    update.
    """
    implementation = subagent_module.Chats._impl.append_rewind_branch_atomic
    source = inspect.getsource(implementation)
    assert source.count("db.commit()") == 1
    assert source.count("db.rollback()") >= 1
    commit_at = source.index("db.commit()")
    assert source.index("INSERT INTO chat_message") < commit_at
    assert source.index("history[\"currentId\"] = branch_message_id") < commit_at
    assert source.index("\"UPDATE chat SET \"") < commit_at
    # Retrying an already-committed operation after another tab moved the
    # selected branch must conflict. Otherwise the response would install a
    # local currentId that the database no longer holds.
    assert "rewind_operation_superseded" in source
    assert source.index("rewind_operation_superseded") < source.index(
        "The parent chat moved to another branch before the repair committed."
    )


def test_launch_updates_authoritative_chat_meta_not_body_shadow():
    source = inspect.getsource(subagent_module.run_subagent_launch)
    assert "Chats.update_chat_meta_by_id(" in source
    assert "Chats.update_chat_by_id(" not in source
    # Final identity comes from the atomic parent-slot reservation; a parallel
    # launch may have changed the provisional name/number.
    assert source.index("register_parent_task = asyncio.create_task(") < source.index(
        "await Chats.update_chat_meta_by_id("
    )
    assert "_upsert_subagent_run(" in source
    assert "await Chats.update_chat_title_by_id(" in source
    assert "_delete_hidden_chat_to_completion(" in source
    # Hidden-row creation and parent reservation are both settled through
    # repeated cancellation, eliminating unknown-commit orphan/dangling windows.
    assert "_wait_shielded_task_to_completion(" in source


def test_hidden_turn_preparation_uses_guarded_row_level_primitive(monkeypatch):
    captured = {}

    async def fake_prepare(chat_id, user_message, assistant_message, **kwargs):
        captured.update(
            {
                "chat_id": chat_id,
                "user": user_message,
                "assistant": assistant_message,
                "kwargs": kwargs,
            }
        )
        return {"current_id": assistant_message["id"]}

    monkeypatch.setattr(
        subagent_module.Chats, "prepare_subagent_turn_atomic", fake_prepare
    )

    asyncio.run(
        subagent_module._append_history_for_inner_run(
            "hidden1",
            "research this",
            "user-new",
            "assistant-new",
            "model1",
            history_transition={
                "expected_current_id": "assistant-old",
                "revert_user_message_id": "user-old",
                "revert_assistant_message_id": "assistant-old",
            },
        )
    )

    assert captured["chat_id"] == "hidden1"
    assert captured["user"]["content"] == "research this"
    assert captured["assistant"]["model"] == "model1"
    assert captured["kwargs"] == {
        "expected_current_id": "assistant-old",
        "reset_history": False,
        "revert_user_message_id": "user-old",
        "revert_assistant_message_id": "assistant-old",
    }
    helper_source = inspect.getsource(
        subagent_module._append_history_for_inner_run
    )
    assert "Chats.update_chat_by_id(" not in helper_source


def test_hidden_turn_db_transition_is_single_commit_and_targeted():
    implementation = subagent_module.Chats._impl.prepare_subagent_turn_atomic
    source = inspect.getsource(implementation)

    assert source.count("db.commit()") == 1
    assert source.count("db.rollback()") >= 1
    assert "_sync_messages_to_table" not in source
    assert "with_for_update()" in source
    assert "live_current_id != expected_current_id" in source
    assert "INSERT INTO chat_message" in source
    assert 'history["currentId"] = assistant_message_id' in source
    assert source.index("INSERT INTO chat_message") < source.index("db.commit()")


def test_rerun_never_uses_separate_reset_or_revert_writes():
    source = inspect.getsource(subagent_module.rerun_subagent_turn)
    assert "_reset_inner_history" not in source
    assert "_revert_subagent_history" not in source
    assert '"reset_history": True' in source
    assert '"revert_user_message_id"' in source
    assert "history_transition=history_transition" in source


def test_parent_assembly_detects_only_active_detached_reruns():
    messages = [
        {
            "id": "parent",
            "subagent_runs": {
                "active": {
                    "entry_key": "active",
                    "status": "running",
                    "ended_at": None,
                    "rerun": True,
                    "rerun_id": "generation-2",
                },
                "inline": {
                    "entry_key": "inline",
                    "status": "running",
                    "ended_at": None,
                },
                "finished": {
                    "entry_key": "finished",
                    "status": "done",
                    "ended_at": 123,
                    "rerun": True,
                    "rerun_id": "generation-1",
                },
            },
        }
    ]

    assert active_detached_subagent_rerun_entries(messages) == ["active"]
    error = ActiveSubagentRerunError(["active", "active"])
    assert error.entry_keys == ["active"]
    assert "Wait for" in str(error)


def test_detached_rerun_keeps_old_parent_result_until_success_commit():
    source = inspect.getsource(subagent_module.rerun_subagent_turn)
    refreshed_start = source.index("refreshed_claim = await _upsert_subagent_run(")
    refreshed_end = source.index("if not isinstance(refreshed_claim", refreshed_start)
    identity_start = source.index("attempt_identity = await _upsert_subagent_run(")
    identity_end = source.index("if not isinstance(attempt_identity", identity_start)
    success_start = source.index(
        'await _upsert_subagent_run(\n                parent_chat_id,',
        identity_end,
    )
    success_end = source.index("return", success_start)

    # Setup/identity writes change only the run ledger. The canonical old tool
    # result stays in place until the guarded done write replaces it.
    assert "sync_placeholder=False" in source[refreshed_start:refreshed_end]
    assert "sync_placeholder=False" in source[identity_start:identity_end]
    assert "sync_placeholder=False" not in source[success_start:success_end]
    assert "guard_parent_unconsumed=True" in source[success_start:success_end]


def test_atomic_cross_entry_claim_blocks_same_hidden_subagent(monkeypatch):
    target = {
        "id": "target-message",
        "subagent_runs": {
            "target-entry": {
                "subagent_id": "hidden-1",
                "status": "done",
                "ended_at": 10,
            }
        },
    }
    selected_branch = {
        "target-message": target,
        "other-message": {
            "id": "other-message",
            "subagent_runs": {
                "other-entry": {
                    "subagent_id": "hidden-1",
                    "status": "running",
                    "ended_at": None,
                }
            },
        },
    }

    async def fake_targeted(
        _chat_id,
        _message_id,
        _entry_key,
        mutator,
        *,
        cross_message_precondition=None,
    ):
        assert cross_message_precondition is not None
        if not cross_message_precondition(selected_branch):
            return None
        return mutator(target)

    async def go():
        with patch.object(
            subagent_module.Chats,
            "update_message_subagent_run_atomic",
            side_effect=fake_targeted,
        ):
            await subagent_module._upsert_subagent_run(
                "parent",
                "target-message",
                "target-entry",
                {
                    "subagent_id": "hidden-1",
                    "status": "running",
                    "ended_at": None,
                },
                exclusive_running_subagent_id="hidden-1",
            )

    with pytest.raises(SubagentRerunBlockedError) as exc:
        asyncio.run(go())
    assert exc.value.code == "subagent_already_running"
    assert target["subagent_runs"]["target-entry"]["status"] == "done"


def test_cross_entry_claim_is_scoped_to_selected_branch(monkeypatch):
    target = {
        "id": "target-message",
        "subagent_runs": {
            "target-entry": {
                "subagent_id": "hidden-1",
                "status": "done",
                "ended_at": 10,
            }
        },
    }

    async def fake_targeted(
        _chat_id,
        _message_id,
        _entry_key,
        mutator,
        *,
        cross_message_precondition=None,
    ):
        # The model supplies only selected-branch messages. A copied running
        # ledger on an inactive sibling must not block this branch.
        assert cross_message_precondition({"target-message": target})
        return mutator(target)

    async def go():
        with patch.object(
            subagent_module.Chats,
            "update_message_subagent_run_atomic",
            side_effect=fake_targeted,
        ):
            return await subagent_module._upsert_subagent_run(
                "parent",
                "target-message",
                "target-entry",
                {
                    "subagent_id": "hidden-1",
                    "status": "running",
                    "ended_at": None,
                },
                exclusive_running_subagent_id="hidden-1",
            )

    claimed = asyncio.run(go())
    assert claimed["status"] == "running"


def test_adopt_subagent_current_result_uses_selected_child_leaf(monkeypatch):
    run = {
        **_run("tc1", "sa1", assistant_msg_id="failed"),
        "user_msg_id": "hidden-user",
        "entry_key": "sa1",
        "status": "error",
        "error": {"message": "old failure"},
    }
    parent = SimpleNamespace(
        id="parent1",
        chat={
            "history": {
                "currentId": "pm1",
                "messages": {
                    "pm1": {
                        "id": "pm1",
                        "role": "assistant",
                        "parentId": "user1",
                        "childrenIds": [],
                        "content_blocks": [
                            _subagent_call_block("tc1", "sa1", result_content="old failure"),
                            {"type": "text", "content": ""},
                        ],
                        "subagent_runs": {"sa1": run},
                    }
                },
            }
        },
    )
    hidden = SimpleNamespace(
        id="sa1",
        subagent_of="parent1",
        meta={"subagent_of": "parent1"},
        chat={
            "history": {
                "currentId": "replacement",
                "messages": {
                    "replacement": {
                        "id": "replacement",
                        "parentId": "hidden-user",
                        "role": "assistant",
                        "done": True,
                        "error": None,
                        "userStopped": False,
                        "timestamp": 456,
                        "content": "fresh repaired answer",
                        "content_blocks": [
                            {"type": "text", "content": "fresh repaired answer"}
                        ],
                    }
                },
            }
        },
    )

    async def fake_get_chat(chat_id, user_id):
        if chat_id == "parent1":
            return parent
        if chat_id == "sa1":
            return hidden
        return None

    captured = {}

    async def fake_upsert(parent_chat_id, parent_message_id, key, patch, **kwargs):
        captured.update(
            {
                "parent_chat_id": parent_chat_id,
                "parent_message_id": parent_message_id,
                "key": key,
                "patch": patch,
                "kwargs": kwargs,
            }
        )
        return {**run, **patch}

    monkeypatch.setattr(
        subagent_module.Chats, "get_chat_by_id_and_user_id", fake_get_chat
    )
    monkeypatch.setattr(subagent_module, "_upsert_subagent_run", fake_upsert)

    result = asyncio.run(
        subagent_module.adopt_subagent_current_result(
            user=SimpleNamespace(id="user1"),
            parent_chat_id="parent1",
            parent_message_id="pm1",
            entry_key="sa1",
        )
    )

    assert result["parent_message_id"] == "pm1"
    assert result["entry_key"] == "sa1"
    assert result["run"]["status"] == "done"
    assert captured["patch"]["adopted_assistant_msg_id"] == "replacement"
    assert "assistant_msg_id" not in captured["patch"]
    assert captured["patch"]["final_text"] == "fresh repaired answer"
    assert captured["patch"]["error"] is None
    assert captured["kwargs"]["allow_placeholder_append"] is False
    assert captured["kwargs"]["guard_parent_unconsumed"] is True
    assert captured["kwargs"]["require_parent_current"] is True
    assert captured["kwargs"]["require_parent_done"] is True
    assert captured["kwargs"]["touch_chat"] is True
    with pytest.raises(
        SubagentRerunBlockedError, match="not the latest turn"
    ):
        _validate_subagent_turn_is_latest(hidden, result["run"])


def test_guarded_upsert_revalidates_selected_parent_inside_atomic_writer(monkeypatch):
    """The preflight-to-write window cannot rewrite a branch another tab left."""
    run = {
        **_run("tc1", "sa1"),
        "entry_key": "sa1",
        "status": "error",
    }
    message = {
        "id": "pm1",
        "role": "assistant",
        "done": True,
        "childrenIds": [],
        "content_blocks": [
            _subagent_call_block("tc1", "sa1", result_content="old error"),
            {"type": "text", "content": ""},
        ],
        "subagent_runs": {"sa1": run},
    }

    async def fake_atomic_writer(
        chat_id, message_id, entry_key, mutator, *, precondition=None
    ):
        assert (chat_id, message_id, entry_key) == ("parent1", "pm1", "sa1")
        assert precondition is not None
        # Simulate a concurrent branch navigation that committed after HTTP
        # preflight but before this row lock was acquired.
        chat_row = SimpleNamespace(
            chat={"history": {"currentId": "newer-branch"}}
        )
        if not precondition(chat_row, message):
            return None
        return mutator(message)

    monkeypatch.setattr(
        subagent_module.Chats,
        "update_message_subagent_run_atomic",
        fake_atomic_writer,
    )

    with pytest.raises(SubagentRerunBlockedError) as exc:
        asyncio.run(
            subagent_module._upsert_subagent_run(
                "parent1",
                "pm1",
                "sa1",
                {"status": "done", "final_text": "replacement"},
                guard_parent_unconsumed=True,
                require_parent_current=True,
                require_parent_done=True,
            )
        )
    assert exc.value.code == "subagent_parent_moved_on"


def test_guarded_upsert_rejects_parent_generation_started_before_row_lock(monkeypatch):
    """A parent stream that won the lock keeps its consumed result immutable."""
    run = {
        **_run("tc1", "sa1"),
        "entry_key": "sa1",
        "status": "error",
    }
    message = {
        "id": "pm1",
        "role": "assistant",
        "done": False,
        "childrenIds": [],
        "content_blocks": [
            _subagent_call_block("tc1", "sa1", result_content="old error"),
            {"type": "text", "content": ""},
        ],
        "subagent_runs": {"sa1": run},
    }

    async def fake_atomic_writer(
        _chat_id, _message_id, _entry_key, mutator, *, precondition=None
    ):
        chat_row = SimpleNamespace(chat={"history": {"currentId": "pm1"}})
        if not precondition(chat_row, message):
            return None
        return mutator(message)

    monkeypatch.setattr(
        subagent_module.Chats,
        "update_message_subagent_run_atomic",
        fake_atomic_writer,
    )

    with pytest.raises(SubagentRerunBlockedError) as exc:
        asyncio.run(
            subagent_module._upsert_subagent_run(
                "parent1",
                "pm1",
                "sa1",
                {"status": "done", "final_text": "replacement"},
                guard_parent_unconsumed=True,
                require_parent_current=True,
                require_parent_done=True,
            )
        )
    assert exc.value.code == "subagent_parent_running"


def test_parent_guard_allows_unconsumed_cancelled_or_unfinished_subagent():
    parent = _chat(
        "pm1",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": [],
                "content_blocks": [
                    _subagent_call_block(result_content=None),
                    {"type": "text", "content": ""},
                ],
                "reasoning_details_per_round": [[]],
            }
        },
    )

    _validate_parent_subagent_result_unconsumed(parent, "pm1", _run())


def test_subagent_run_gathering_is_scoped_to_current_branch():
    parent = _chat(
        "active_asst",
        {
            "user1": {
                "id": "user1",
                "role": "user",
                "parentId": None,
                "childrenIds": ["old_asst", "active_asst"],
            },
            "old_asst": {
                "id": "old_asst",
                "role": "assistant",
                "parentId": "user1",
                "childrenIds": [],
                "subagent_runs": {
                    "old": {"subagent_id": "old", "name": "same_name"}
                },
            },
            "active_asst": {
                "id": "active_asst",
                "role": "assistant",
                "parentId": "user1",
                "childrenIds": [],
                "subagent_runs": {
                    "active": {"subagent_id": "active", "name": "same_name"}
                },
            },
        },
    )

    runs = subagent_module._gather_all_subagent_runs(parent, "active_asst")
    assert set(runs) == {"active"}


def test_subagent_run_gathering_falls_back_to_all_messages_for_missing_leaf():
    parent = _chat(
        "missing",
        {
            "a": {"subagent_runs": {"sa": {"subagent_id": "sa"}}},
            "b": {"subagent_runs": {"sb": {"subagent_id": "sb"}}},
        },
    )

    runs = subagent_module._gather_all_subagent_runs(parent, "missing")
    assert set(runs) == {"sa", "sb"}


def test_parent_guard_allows_parallel_subagent_retry_before_parent_continues():
    parent = _chat(
        "pm1",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": [],
                "content_blocks": [
                    {
                        "type": "tool_calls",
                        "content": [
                            {
                                "id": "tc1",
                                "type": "function",
                                "function": {"name": "subagent_launch", "arguments": "{}"},
                            },
                            {
                                "id": "tc2",
                                "type": "function",
                                "function": {"name": "subagent_launch", "arguments": "{}"},
                            },
                        ],
                        "results": [
                            {"tool_call_id": "tc1", "content": "done", "subagent_id": "sa1"}
                        ],
                    },
                    {"type": "text", "content": ""},
                ],
                "reasoning_details_per_round": [[]],
            }
        },
    )

    _validate_parent_subagent_result_unconsumed(parent, "pm1", _run("tc1", "sa1"))


def test_parent_guard_ignores_duplicate_placeholder_from_prior_rerun():
    parent = _chat(
        "pm1",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": [],
                "subagent_runs": {
                    "sa1": {
                        **_run("tc1", "sa1"),
                        "entry_key": "sa1",
                    }
                },
                "content_blocks": [
                    _subagent_call_block("tc1", "sa1", result_content="old answer"),
                    {"type": "text", "content": ""},
                    # Older rerun code could append this synthetic placeholder.
                    _subagent_call_block("sa1", "sa1", result_content="new answer"),
                ],
            }
        },
    )

    _validate_parent_subagent_result_unconsumed(parent, "pm1", _run("tc1", "sa1"))


def test_parent_guard_ignores_duplicate_placeholder_for_parallel_sibling():
    parent = _chat(
        "pm1",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": [],
                "subagent_runs": {
                    "sa1": {**_run("tc1", "sa1"), "entry_key": "sa1"},
                    "sa2": {**_run("tc2", "sa2"), "entry_key": "sa2"},
                },
                "content_blocks": [
                    {
                        "type": "tool_calls",
                        "content": [
                            {
                                "id": "tc1",
                                "type": "function",
                                "function": {"name": "subagent_launch", "arguments": "{}"},
                            },
                            {
                                "id": "tc2",
                                "type": "function",
                                "function": {"name": "subagent_launch", "arguments": "{}"},
                            },
                        ],
                        "results": [
                            {"tool_call_id": "tc1", "content": "a", "subagent_id": "sa1"},
                            {"tool_call_id": "tc2", "content": "b", "subagent_id": "sa2"},
                        ],
                    },
                    {"type": "text", "content": ""},
                    _subagent_call_block("sa1", "sa1", result_content="new a"),
                ],
            }
        },
    )

    _validate_parent_subagent_result_unconsumed(parent, "pm1", _run("tc2", "sa2"))


def test_parent_guard_allows_later_subagent_fanout_block():
    # A later block that ONLY launches/continues subagents is a sibling fan-out,
    # NOT the parent model authoring new signed output from this result. The
    # parent-result guard must not treat it as "consumed" — otherwise a
    # `from_launch` redo of a subagent that was later continued (exactly the
    # "restart from beginning" case) could never run. The continuation-dependency
    # invariant is enforced separately, on `this_turn` only, by
    # `_validate_subagent_turn_is_latest` (see
    # test_subagent_guard_blocks_rerunning_launch_after_continuation_exists).
    parent = _chat(
        "pm1",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": [],
                "subagent_runs": {
                    "sa1": {**_run("tc1", "sa1"), "entry_key": "sa1"},
                    "sa1#tc2": {
                        **_run("tc2", "sa1", assistant_msg_id="cont_asst"),
                        "entry_key": "sa1#tc2",
                        "continuation": True,
                    },
                },
                "content_blocks": [
                    _subagent_call_block("tc1", "sa1", result_content="old answer"),
                    {"type": "text", "content": ""},
                    {
                        "type": "tool_calls",
                        "content": [
                            {
                                "id": "tc2",
                                "type": "function",
                                "function": {"name": "subagent_continue", "arguments": "{}"},
                            }
                        ],
                        "results": [
                            {"tool_call_id": "tc2", "content": "continued", "subagent_id": "sa1"}
                        ],
                    },
                ],
            }
        },
    )

    # Must NOT raise: pure subagent fan-out does not consume the launch result.
    _validate_parent_subagent_result_unconsumed(parent, "pm1", _run("tc1", "sa1"))


def test_parent_guard_blocks_when_later_round_signed_reasoning():
    # The fan-out skip explicitly preserves the signed-reasoning invariant: if the
    # parent signed a reasoning round AFTER the target tool round, the encrypted
    # transcript state depends on the old result, so the redo must block.
    parent = _chat(
        "pm1",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": [],
                "content_blocks": [
                    _subagent_call_block("tc1", "sa1", result_content="old answer"),
                ],
                # Target is tool round 1; a signed round 2 exists → unsafe to redo.
                "reasoning_details_per_round": [
                    [{"type": "reasoning.text"}],
                    [{"type": "reasoning.text"}],
                ],
            }
        },
    )

    with pytest.raises(SubagentRerunBlockedError, match="later reasoning turn"):
        _validate_parent_subagent_result_unconsumed(parent, "pm1", _run("tc1", "sa1"))


def test_parent_guard_blocks_after_parent_text_consumed_result():
    parent = _chat(
        "pm1",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": [],
                "content_blocks": [
                    _subagent_call_block(result_content="old answer"),
                    {"type": "text", "content": "Parent answer based on old answer."},
                ],
            }
        },
    )

    with pytest.raises(SubagentRerunBlockedError, match="already continued"):
        _validate_parent_subagent_result_unconsumed(parent, "pm1", _run())


def test_parent_guard_blocks_after_later_parent_reasoning_even_without_text():
    parent = _chat(
        "pm1",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": [],
                "content_blocks": [
                    _subagent_call_block(result_content="old answer"),
                    {"type": "text", "content": ""},
                ],
                # A second parent model round started after the tool result, but
                # the user stopped before visible text was emitted.
                "reasoning_details_per_round": [[], []],
            }
        },
    )

    with pytest.raises(SubagentRerunBlockedError, match="later reasoning"):
        _validate_parent_subagent_result_unconsumed(parent, "pm1", _run())


def test_parent_guard_blocks_when_chat_has_later_user_turns():
    parent = _chat(
        "user2",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": ["user2"],
                "content_blocks": [_subagent_call_block(result_content="old answer")],
            },
            "user2": {"id": "user2", "role": "user", "parentId": "pm1", "childrenIds": []},
        },
    )

    with pytest.raises(SubagentRerunBlockedError, match="moved past"):
        _validate_parent_subagent_result_unconsumed(parent, "pm1", _run())


# ---------------------------------------------------------------------------
# "Parent moved on" error code — distinguishes the four rewind-fixable blocks
# (the "rewind & redo" flow can branch around them) from hard blocks. The
# frontend keys the inline "Rewind & redo" panel on this code.
# ---------------------------------------------------------------------------


def _moved_on_code(parent, run):
    """Run the guard and return the raised error's `.code` (or None if it
    didn't raise)."""
    try:
        _validate_parent_subagent_result_unconsumed(parent, "pm1", run)
    except SubagentRerunBlockedError as e:
        return e.code
    return None


def test_moved_on_code_when_current_id_moved_past():
    parent = _chat(
        "other_leaf",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": [],
                "content_blocks": [_subagent_call_block(result_content="old answer")],
            }
        },
    )
    assert _moved_on_code(parent, _run()) == "subagent_parent_moved_on"


def test_moved_on_code_when_later_user_turns():
    parent = _chat(
        "user2",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": ["user2"],
                "content_blocks": [_subagent_call_block(result_content="old answer")],
            },
            "user2": {"id": "user2", "role": "user", "parentId": "pm1", "childrenIds": []},
        },
    )
    assert _moved_on_code(parent, _run()) == "subagent_parent_moved_on"


def test_moved_on_code_when_parent_continued_with_text():
    parent = _chat(
        "pm1",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": [],
                "content_blocks": [
                    _subagent_call_block(result_content="old answer"),
                    {"type": "text", "content": "Parent answer based on old answer."},
                ],
            }
        },
    )
    assert _moved_on_code(parent, _run()) == "subagent_parent_moved_on"


def test_moved_on_code_when_later_reasoning_round():
    parent = _chat(
        "pm1",
        {
            "pm1": {
                "id": "pm1",
                "role": "assistant",
                "childrenIds": [],
                "content_blocks": [
                    _subagent_call_block(result_content="old answer"),
                    {"type": "text", "content": ""},
                ],
                "reasoning_details_per_round": [[], []],
            }
        },
    )
    assert _moved_on_code(parent, _run()) == "subagent_parent_moved_on"


def test_subagent_leaf_block_is_not_tagged_moved_on():
    # The hidden-chat-leaf guard (a subagent-internal invariant) is NOT
    # rewind-fixable — rewinding the PARENT doesn't make a stale subagent leaf
    # valid — so it must keep the generic code, not the moved-on code.
    subagent = _chat(
        "cont_asst",
        {
            "launch_asst": {"id": "launch_asst", "childrenIds": ["cont_user"]},
            "cont_user": {
                "id": "cont_user",
                "parentId": "launch_asst",
                "childrenIds": ["cont_asst"],
            },
            "cont_asst": {"id": "cont_asst", "parentId": "cont_user", "childrenIds": []},
        },
    )
    try:
        _validate_subagent_turn_is_latest(subagent, _run(assistant_msg_id="launch_asst"))
        raise AssertionError("expected the leaf guard to raise")
    except SubagentRerunBlockedError as e:
        assert e.code != "subagent_parent_moved_on"


def test_parent_guard_passes_for_rewound_branch_shape():
    # The "rewind & redo" flow targets a fresh sibling branch M' whose
    # content_blocks are slice(0, cut) + trailing empty text('') — cut sits right
    # after the subagent's tool_calls block, so in M' that block is the LAST tool
    # round, currentId == M', childrenIds == [], and reasoning_details_per_round is
    # sliced to the retained rounds. Every clause of the guard must pass, even with
    # the carried OLD result body still present (the redo rewrites it later) and an
    # EARLIER kept tool round. This is the invariant the whole feature relies on.
    parent = _chat(
        "mprime",
        {
            "mprime": {
                "id": "mprime",
                "role": "assistant",
                "parentId": "user1",
                "childrenIds": [],
                "subagent_runs": {
                    "saEarly": {**_run("tcEarly", "saEarly"), "entry_key": "saEarly"},
                    "sa1": {**_run("tc1", "sa1"), "entry_key": "sa1"},
                },
                "content_blocks": [
                    _subagent_call_block("tcEarly", "saEarly", result_content="earlier"),
                    {"type": "text", "content": ""},
                    _subagent_call_block("tc1", "sa1", result_content="old answer"),
                    {"type": "text", "content": ""},
                ],
                # Two retained tool rounds; reasoning sliced to match (no later round).
                "reasoning_details_per_round": [[], []],
            },
            "user1": {"id": "user1", "role": "user", "childrenIds": ["mprime"]},
        },
    )

    # Target the LATER subagent (round 2) — must not raise for the M' shape.
    _validate_parent_subagent_result_unconsumed(parent, "mprime", _run("tc1", "sa1"))


def test_subagent_guard_allows_latest_continuation_leaf():
    subagent = _chat(
        "cont_asst",
        {
            "launch_asst": {"id": "launch_asst", "childrenIds": ["cont_user"]},
            "cont_user": {
                "id": "cont_user",
                "parentId": "launch_asst",
                "childrenIds": ["cont_asst"],
            },
            "cont_asst": {
                "id": "cont_asst",
                "parentId": "cont_user",
                "childrenIds": [],
            },
        },
    )

    _validate_subagent_turn_is_latest(subagent, _run(assistant_msg_id="cont_asst"))


def test_subagent_guard_blocks_rerunning_launch_after_continuation_exists():
    subagent = _chat(
        "cont_asst",
        {
            "launch_asst": {"id": "launch_asst", "childrenIds": ["cont_user"]},
            "cont_user": {
                "id": "cont_user",
                "parentId": "launch_asst",
                "childrenIds": ["cont_asst"],
            },
            "cont_asst": {
                "id": "cont_asst",
                "parentId": "cont_user",
                "childrenIds": [],
            },
        },
    )

    with pytest.raises(SubagentRerunBlockedError, match="not the latest turn"):
        _validate_subagent_turn_is_latest(
            subagent, _run(assistant_msg_id="launch_asst")
        )


# ---------------------------------------------------------------------------
# _find_subagent_entry alias tolerance
#
# The live frontend store keys subagent cards by tool_call_id and derives the
# rerun entry_key as `run.entry_key || subagent_id || chat_id || tool_call_id`.
# But the backend keys launch entries by bare subagent_id (and continuations by
# `subagent_id#tool_call_id`). So the value the frontend sends is frequently NOT
# the literal dict key. The old exact-match `entry_key in runs` then raised
# "subagent run entry '<id>' not found" for a launch that was very much present.
# These pin the alias fallback that resolves tool_call_id / chat_id back to the
# canonical key.
# ---------------------------------------------------------------------------


def _parent_with_launch(entry_key, run):
    return _chat(
        "pm1",
        {"pm1": {"id": "pm1", "role": "assistant", "subagent_runs": {entry_key: run}}},
    )


def test_find_subagent_entry_exact_key_match():
    run = _run("tcX", "saX")
    parent = _parent_with_launch("saX", run)
    msg_id, key, entry = subagent_module._find_subagent_entry(parent, "saX")
    assert msg_id == "pm1"
    assert key == "saX"
    assert entry is run


def test_find_subagent_entry_resolves_tool_call_id_alias_to_launch_key():
    # Launch stored under bare subagent_id, but the frontend clicked with the
    # tool_call_id (the store's primary key) — must still resolve, AND return
    # the canonical key (saX) so downstream writes don't fork an orphan.
    run = _run("tcX", "saX")
    parent = _parent_with_launch("saX", run)
    msg_id, key, entry = subagent_module._find_subagent_entry(parent, "tcX")
    assert msg_id == "pm1"
    assert key == "saX"
    assert entry is run


def test_find_subagent_entry_resolves_chat_id_alias():
    run = _run("tcX", "saX")  # chat_id == subagent_id == saX
    parent = _parent_with_launch("saX", run)
    _msg_id, key, entry = subagent_module._find_subagent_entry(parent, "saX")
    assert key == "saX"
    assert entry is run


def test_find_subagent_entry_missing_returns_none():
    parent = _parent_with_launch("saX", _run("tcX", "saX"))
    assert subagent_module._find_subagent_entry(parent, "nonexistent") == (
        None,
        None,
        None,
    )


def test_find_subagent_entry_continuation_tool_call_id_alias():
    # Continuation keyed `subagent_id#tool_call_id`; the store may still click
    # with the bare tool_call_id, which the alias resolver maps back to the
    # canonical composite key.
    cont = {
        "tool_call_id": "tcCont",
        "subagent_id": "saX",
        "chat_id": "saX",
        "continuation": True,
        "assistant_msg_id": "cont_asst",
    }
    parent = _parent_with_launch("saX#tcCont", cont)
    _msg_id, key, entry = subagent_module._find_subagent_entry(parent, "tcCont")
    assert key == "saX#tcCont"
    assert entry is cont


def test_resolve_context_uses_alias_resolved_entry():
    # End-to-end through the resolver the router/background task both call: a
    # tool_call_id click on a this_turn rerun resolves to the launch entry's
    # stored prompt rather than raising "not found", AND normalizes the write
    # key to the canonical map key (saX) so the rerun updates the existing
    # entry instead of forking an orphan under the tool_call_id.
    run = {
        "tool_call_id": "tcX",
        "subagent_id": "saX",
        "chat_id": "saX",
        "assistant_msg_id": "sa_asst",
        "prompt": "do the thing",
    }
    parent = _parent_with_launch("saX", run)
    ctx = subagent_module._resolve_subagent_rerun_context(
        parent, "pm1", "tcX", "this_turn"
    )
    assert ctx["subagent_id"] == "saX"
    assert ctx["inner_prompt"] == "do the thing"
    assert ctx["write_entry_key"] == "saX"


def test_rerun_parent_loader_follows_hidden_subagent_chat_id(monkeypatch):
    # Stale UI state can send parent_chat_id=<hidden subagent chat id>. The hidden
    # chat has no parent subagent_runs, but it carries subagent_of=<real parent>.
    # The rerun path must follow that pointer before resolving the entry.
    run = {
        "tool_call_id": "tcX",
        "subagent_id": "saX",
        "chat_id": "saX",
        "assistant_msg_id": "sa_asst",
        "prompt": "do the thing",
    }
    real_parent = _parent_with_launch("saX", run)
    hidden_chat = SimpleNamespace(
        subagent_of="parent1",
        meta={"subagent_of": "parent1"},
        chat={"history": {"messages": {}}},
    )

    async def fake_get_chat(chat_id, user_id):
        if chat_id == "saX":
            return hidden_chat
        if chat_id == "parent1":
            return real_parent
        return None

    monkeypatch.setattr(
        subagent_module.Chats,
        "get_chat_by_id_and_user_id",
        fake_get_chat,
    )

    async def go():
        parent_id, parent_chat = await subagent_module.load_effective_parent_chat_for_subagent_action(
            "saX", SimpleNamespace(id="user1")
        )
        assert parent_id == "parent1"
        ctx = subagent_module._resolve_subagent_rerun_context(
            parent_chat, "pm1", "saX", "this_turn"
        )
        assert ctx["subagent_id"] == "saX"
        assert ctx["inner_prompt"] == "do the thing"

    asyncio.run(go())


def _rewound_parent_with_both_branches():
    # After a "rewind & redo": the original moved-on message `m` AND the rewound
    # sibling branch `mprime` BOTH carry subagent_runs["sa1"] under the SAME key.
    # `m` is inserted FIRST (lower sequence) so a bare history scan returns it.
    def _run_entry():
        return {
            **_run("tc1", "sa1"),
            "entry_key": "sa1",
            "prompt": "do it",
            "status": "done",
            "ended_at": 100,
        }

    return _chat(
        "mprime",  # currentId points at the rewound branch
        {
            "m": {
                "id": "m",
                "role": "assistant",
                "parentId": "user0",
                "childrenIds": ["userLater"],
                "subagent_runs": {"sa1": _run_entry()},
                "content_blocks": [
                    _subagent_call_block("tc1", "sa1", result_content="old"),
                    {"type": "text", "content": "parent moved on with this text"},
                ],
            },
            "userLater": {
                "id": "userLater",
                "role": "user",
                "parentId": "m",
                "childrenIds": [],
            },
            "mprime": {
                "id": "mprime",
                "role": "assistant",
                "parentId": "user0",
                "childrenIds": [],
                "subagent_runs": {"sa1": _run_entry()},
                "content_blocks": [
                    _subagent_call_block("tc1", "sa1", result_content="old"),
                    {"type": "text", "content": ""},
                ],
            },
            "user0": {"id": "user0", "role": "user", "childrenIds": ["m", "mprime"]},
        },
    )


def test_resolve_context_targets_rewound_branch_not_moved_on_message_this_turn():
    # THE keystone regression for "rewind & redo": the resolver must honor the
    # caller-supplied parent_message_id (mprime) instead of returning the older
    # `m` (which a bare history scan picks first), otherwise the rerun targets the
    # moved-on message and the unconsumed guard 409s forever — the feature dies.
    parent = _rewound_parent_with_both_branches()
    ctx = subagent_module._resolve_subagent_rerun_context(
        parent, "mprime", "sa1", "this_turn"
    )
    assert ctx["parent_message_id"] == "mprime"
    assert ctx["write_msg_id"] == "mprime"
    # And the guard PASSES against the rewound branch.
    _validate_parent_subagent_result_unconsumed(
        parent, ctx["write_msg_id"], ctx["write_entry"]
    )


def test_resolve_context_targets_rewound_branch_not_moved_on_message_from_launch():
    # from_launch resolves the launch via _find_launch_entry_for_subagent, which
    # must ALSO prefer the rewound branch (the launch entry exists on both `m`
    # and `mprime`).
    parent = _rewound_parent_with_both_branches()
    ctx = subagent_module._resolve_subagent_rerun_context(
        parent, "mprime", "sa1", "from_launch"
    )
    assert ctx["write_msg_id"] == "mprime"
    _validate_parent_subagent_result_unconsumed(
        parent, ctx["write_msg_id"], ctx["write_entry"]
    )


# ---------------------------------------------------------------------------
# Stranded-run recovery: a run stuck at status='running' because its rerun task
# died (server restart / crash) after the inner chat finished but before the
# terminal write must NOT block redo forever. The authoritative "is a turn live"
# signal is whether the hidden subagent chat is actively generating.
# ---------------------------------------------------------------------------


def _subagent_chat(current_id, messages):
    return SimpleNamespace(chat={"history": {"currentId": current_id, "messages": messages}})


def test_inner_chat_generating_true_when_leaf_is_unfinished_assistant():
    chat = _subagent_chat(
        "a1", {"a1": {"id": "a1", "role": "assistant", "done": False}}
    )
    assert subagent_module._subagent_inner_chat_generating(chat) is True


def test_inner_chat_generating_false_when_leaf_is_finished_assistant():
    # This is the STRANDED shape: the inner chat finished (done) but the parent
    # run entry stayed 'running' — redo must be allowed, not blocked.
    chat = _subagent_chat(
        "a1", {"a1": {"id": "a1", "role": "assistant", "done": True}}
    )
    assert subagent_module._subagent_inner_chat_generating(chat) is False


def test_inner_chat_generating_false_for_user_leaf_or_missing():
    assert (
        subagent_module._subagent_inner_chat_generating(
            _subagent_chat("u1", {"u1": {"id": "u1", "role": "user"}})
        )
        is False
    )
    assert (
        subagent_module._subagent_inner_chat_generating(_subagent_chat(None, {}))
        is False
    )
    assert subagent_module._subagent_inner_chat_generating(None) is False


def _run_claim_finalizer(
    existing_run,
    claim_id,
    fallback="error",
    *,
    hidden_message=None,
    hidden_text="",
    parent_current_id="pm1",
):
    state = {
        "id": "pm1",
        "role": "assistant",
        "done": True,
        "childrenIds": [],
        "subagent_runs": {"sa1": dict(existing_run)},
        "content_blocks": [
            _subagent_call_block(
                "tc1",
                existing_run.get("subagent_id") or "sa1",
                result_content="prior answer",
            ),
            {"type": "text", "content": ""},
        ],
    }
    writes = []

    async def fake_targeted(
        _chat_id,
        _message_id,
        _entry_key,
        mutator,
        *,
        precondition=None,
        touch_chat=False,
    ):
        assert touch_chat is True
        if precondition is not None:
            chat_row = SimpleNamespace(
                chat={"history": {"currentId": parent_current_id}}
            )
            if not precondition(chat_row, state):
                return None
        partial = mutator(state)
        if partial:
            writes.append(partial)
            state["subagent_runs"] = partial["subagent_runs"]
            if "content_blocks" in partial:
                state["content_blocks"] = partial["content_blocks"]
            if "content" in partial:
                state["content"] = partial["content"]
        return partial

    async def fake_get_message(chat_id, _message_id):
        return state if chat_id == "parent1" else hidden_message

    async def fake_extract(_chat_id, _message_id):
        return hidden_text

    async def go():
        with patch.object(
            subagent_module.Chats,
            "update_message_subagent_run_atomic",
            side_effect=fake_targeted,
        ), patch.object(
            subagent_module.Chats,
            "get_message_by_id_and_message_id",
            side_effect=fake_get_message,
        ), patch.object(
            subagent_module,
            "_extract_final_text",
            side_effect=fake_extract,
        ):
            changed = await subagent_module.finalize_detached_rerun_claim(
                parent_chat_id="parent1",
                parent_message_id="pm1",
                entry_key="sa1",
                rerun_id=claim_id,
                fallback_status=fallback,
                error_message="setup failed",
            )
        return changed

    return asyncio.run(go()), state["subagent_runs"]["sa1"], writes


def test_detached_rerun_finalizer_resolves_matching_running_claim():
    changed, run, writes = _run_claim_finalizer(
        {"status": "running", "ended_at": None, "rerun_id": "attempt-2"},
        "attempt-2",
    )

    assert changed is True
    assert run["status"] == "error"
    assert run["error"]["message"] == "setup failed"
    assert run["ended_at"] is not None
    assert len(writes) == 1


def test_detached_rerun_finalizer_cannot_touch_newer_claim():
    changed, run, writes = _run_claim_finalizer(
        {"status": "running", "ended_at": None, "rerun_id": "attempt-3"},
        "attempt-2",
        fallback="cancelled",
    )

    assert changed is False
    assert run["status"] == "running"
    assert run["rerun_id"] == "attempt-3"
    assert writes == []


def test_detached_rerun_finalizer_never_downgrades_terminal_claim():
    changed, run, writes = _run_claim_finalizer(
        {
            "status": "done",
            "ended_at": 123,
            "rerun_id": "attempt-2",
            "final_text": "fresh answer",
        },
        "attempt-2",
        fallback="cancelled",
    )

    assert changed is False
    assert run["status"] == "done"
    assert run["final_text"] == "fresh answer"
    assert writes == []


def test_detached_rerun_finalizer_recovers_completed_hidden_answer():
    changed, run, writes = _run_claim_finalizer(
        {
            "status": "running",
            "ended_at": None,
            "rerun_id": "attempt-2",
            "subagent_id": "hidden1",
            "assistant_msg_id": "answer1",
            "rerun_assistant_msg_id": "answer1",
        },
        "attempt-2",
        fallback="error",
        hidden_message={
            "role": "assistant",
            "done": True,
            "error": None,
            "userStopped": False,
        },
        hidden_text="recovered answer",
    )

    assert changed is True
    assert run["status"] == "done"
    assert run["final_text"] == "recovered answer"
    assert run["error"] is None
    assert len(writes) == 1
    result = writes[0]["content_blocks"][0]["results"][0]
    assert result["tool_call_id"] == "tc1"
    assert result["content"] == "recovered answer"


def test_completed_rerun_finalizer_closes_claim_if_parent_moved_before_commit():
    changed, run, writes = _run_claim_finalizer(
        {
            "status": "running",
            "ended_at": None,
            "rerun_id": "attempt-2",
            "subagent_id": "hidden1",
            "assistant_msg_id": "old-answer",
            "rerun_assistant_msg_id": "new-answer",
            "final_text": "prior answer",
        },
        "attempt-2",
        fallback="error",
        hidden_message={
            "role": "assistant",
            "done": True,
            "error": None,
            "userStopped": False,
        },
        hidden_text="new answer that is no longer safe to install",
        parent_current_id="new-parent-leaf",
    )

    assert changed is True
    assert run["status"] == "error"
    assert run["final_text"] == "prior answer"
    assert "parent chat changed" in run["error"]["message"]
    assert len(writes) == 1
    result = writes[0]["subagent_runs"]["sa1"]
    assert result["status"] == "error"
    assert result["final_text"] == "prior answer"


def test_rerun_attempt_is_monotonic_even_with_same_second_timestamps():
    assert subagent_module._next_rerun_attempt(None) == 1
    assert subagent_module._next_rerun_attempt({"rerun_attempt": 7}) == 8
    assert subagent_module._next_rerun_attempt({"rerun_attempt": "8"}) == 9
    assert subagent_module._next_rerun_attempt({"rerun_attempt": "bad"}) == 1
