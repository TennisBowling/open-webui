from types import SimpleNamespace

import pytest

import open_webui.utils.subagent as subagent_module
from open_webui.utils.subagent import (
    SubagentRerunBlockedError,
    _sync_parent_subagent_placeholder,
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


def test_sync_placeholder_matches_tool_call_id_field(monkeypatch):
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
    updates = []

    class FakeChats:
        @staticmethod
        def get_message_by_id_and_message_id(chat_id, message_id):
            return message

        @staticmethod
        def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, update):
            updates.append(update)
            message.update(update)

    monkeypatch.setattr(subagent_module, "Chats", FakeChats)

    _sync_parent_subagent_placeholder(
        "pc1",
        "pm1",
        {
            **_run("tc1", "sa1"),
            "entry_key": "sa1",
            "status": "done",
            "final_text": "new answer",
        },
    )

    assert len(updates) == 1
    blocks = updates[0]["content_blocks"]
    assert len(blocks) == 1
    assert blocks[0]["results"][0]["tool_call_id"] == "tc1"


def test_sync_placeholder_can_disable_append_for_rerun(monkeypatch):
    message = {
        "content": "",
        "content_blocks": [_subagent_call_block("other", "other")],
    }
    updates = []

    class FakeChats:
        @staticmethod
        def get_message_by_id_and_message_id(chat_id, message_id):
            return message

        @staticmethod
        def upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, update):
            updates.append(update)

    monkeypatch.setattr(subagent_module, "Chats", FakeChats)

    _sync_parent_subagent_placeholder(
        "pc1",
        "pm1",
        {
            **_run("tc1", "sa1"),
            "entry_key": "sa1",
            "status": "done",
            "final_text": "new answer",
        },
        allow_append=False,
    )

    assert updates == []


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


def test_parent_guard_still_blocks_real_later_subagent_call():
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

    with pytest.raises(SubagentRerunBlockedError, match="already continued"):
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
