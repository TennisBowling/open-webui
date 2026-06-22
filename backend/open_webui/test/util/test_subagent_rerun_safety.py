from types import SimpleNamespace

import asyncio

import pytest

import open_webui.utils.subagent as subagent_module
from open_webui.utils.subagent import (
    SubagentRerunBlockedError,
    _apply_subagent_placeholder_patch,
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
