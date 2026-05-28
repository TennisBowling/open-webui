from types import SimpleNamespace

import pytest

from open_webui.utils.subagent import (
    SubagentRerunBlockedError,
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
