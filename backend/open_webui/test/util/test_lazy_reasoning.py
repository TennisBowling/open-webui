"""Lazy reasoning bodies: write-time split, read-time projection, prune/carry.

Pure-logic tests (no DB). The contract under test lives in
utils/lazy_blocks.py and its consumers in models/chats.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from open_webui.utils.lazy_blocks import (  # noqa: E402
    REASONING_INLINE_MAX,
    reasoning_body_from_blocks,
    slim_content_blocks_for_read,
    split_reasoning_bodies,
    text_only_content_from_blocks,
)
from open_webui.models.chats import (  # noqa: E402
    _project_message_slim,
    _prune_tool_result_bodies_to_refs,
    carry_tool_result_bodies_from_source,
)

BIG = "x" * (REASONING_INLINE_MAX + 100)


def _blocks():
    return [
        {
            "type": "reasoning",
            "content": BIG,
            "started_at": 1.0,
            "ended_at": 5.0,
            "duration": 4,
        },
        {"type": "text", "content": "answer"},
        {
            "type": "tool_calls",
            "content": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
            "results": [{"tool_call_id": "call_1", "content": "r" * 5000}],
        },
    ]


def test_split_moves_closed_reasoning_to_bodies():
    slim, bodies = split_reasoning_bodies(_blocks())
    assert slim[0]["content"] == ""
    assert slim[0]["content_lazy"] is True
    assert slim[0]["content_ref"] == "reasoning:0"
    assert slim[0]["duration"] == 4  # timing metadata survives the stub
    assert bodies == {"reasoning:0": {"content": BIG}}
    # text and tool blocks untouched by the reasoning splitter
    assert slim[1]["content"] == "answer"
    assert slim[2]["results"][0]["content"] == "r" * 5000


def test_split_leaves_open_and_small_blocks_inline():
    open_block = {"type": "reasoning", "content": BIG, "started_at": 1.0}
    small = {"type": "reasoning", "content": "short", "duration": 1}
    slim, bodies = split_reasoning_bodies([open_block, small])
    assert slim[0]["content"] == BIG  # still streaming — no dangling stub
    assert slim[1]["content"] == "short"
    assert bodies == {}


def test_read_slim_is_idempotent_and_identity_on_canonical():
    once = slim_content_blocks_for_read(_blocks())
    assert once[0]["content"] == "" and once[0]["content_ref"] == "reasoning:0"
    assert once[2]["results"][0]["result_lazy"] is True
    twice = slim_content_blocks_for_read(once)
    assert twice is once  # canonical input passes through by identity


def test_reasoning_body_resolves_from_inline_blocks():
    blocks = _blocks()
    assert reasoning_body_from_blocks(blocks, "reasoning:0") == {"content": BIG}
    assert reasoning_body_from_blocks(blocks, "reasoning:1") is None  # text block
    assert reasoning_body_from_blocks(blocks, "reasoning:9") is None
    assert reasoning_body_from_blocks(slim_content_blocks_for_read(blocks), "reasoning:0") is None


def test_text_only_content_from_blocks():
    blocks = [
        {"type": "reasoning", "content": BIG, "duration": 4, "ended_at": 5.0},
        {"type": "text", "content": "Hello world"},
        {
            "type": "tool_calls",
            "content": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
            "results": [{"tool_call_id": "call_1", "content": "r" * 5000}],
        },
        {"type": "user_steer", "content": "do X instead"},
        {"type": "text", "content": "Second part"},
    ]
    out = text_only_content_from_blocks(blocks)
    assert out == "Hello world\n\ndo X instead\n\nSecond part"
    assert "<details" not in out
    assert BIG not in out


def test_projection_derives_text_only_from_legacy_mirror():
    blocks = _blocks()
    mirror = (
        '<details type="tool_calls" result="BODY">...</details>answer text'
    )
    msg = {
        "id": "m1",
        "role": "assistant",
        "content": mirror,
        "content_blocks": blocks,
    }
    out = _project_message_slim(dict(msg))
    assert "<details" not in out["content"]
    assert out["content"] == text_only_content_from_blocks(blocks)

    plain_msg = {
        "id": "m2",
        "role": "assistant",
        "content": "answer",
        "content_blocks": blocks,
    }
    plain_out = _project_message_slim(dict(plain_msg))
    assert plain_out["content"] == "answer"  # identity — guard must not touch it


def test_prune_keeps_reasoning_refs():
    bodies = {"reasoning:0": {"content": BIG}, "call_1": {"content": "r"}, "orphan": {}}
    blocks, _ = split_reasoning_bodies(_blocks())
    kept = _prune_tool_result_bodies_to_refs(bodies, blocks)
    assert set(kept) == {"reasoning:0", "call_1"}


def test_carry_hydrates_from_source_side_map_and_inline():
    source = {
        "content_blocks": _blocks(),
        # side map has ONLY the reasoning body; the tool body is inline-only
        "tool_result_bodies": {"reasoning:0": {"content": BIG}},
    }
    kept_prefix, _ = split_reasoning_bodies(slim_content_blocks_for_read(_blocks()))
    new_msg = {"content_blocks": kept_prefix}
    carry_tool_result_bodies_from_source(source, new_msg)
    bodies = new_msg["tool_result_bodies"]
    assert bodies["reasoning:0"] == {"content": BIG}
    assert bodies["call_1"]["content"] == "r" * 5000  # hydrated from inline source


def test_projection_strips_reasoning_details_for_non_leaf_only():
    msg = {
        "id": "m1",
        "role": "assistant",
        "content": "",
        "content_blocks": _blocks(),
        "reasoning_details": [{"type": "reasoning.text", "text": "t"}],
        "reasoning_details_per_round": [[{"type": "reasoning.text", "text": "t"}]],
    }
    non_leaf = _project_message_slim(dict(msg), is_current_leaf=False)
    assert "reasoning_details" not in non_leaf
    assert "reasoning_details_per_round" not in non_leaf
    assert non_leaf["content_blocks"][0]["content_lazy"] is True

    leaf = _project_message_slim(dict(msg), is_current_leaf=True)
    assert leaf["reasoning_details"]
    assert leaf["reasoning_details_per_round"]
    # blocks are slimmed regardless of leaf-ness
    assert leaf["content_blocks"][0]["content_lazy"] is True
    assert leaf["content_blocks"][2]["results"][0]["result_lazy"] is True


def test_projection_slims_legacy_subagent_runs_to_modern_shape():
    msg = {
        "id": "m1",
        "role": "assistant",
        "subagent_runs": {
            "sa1": {
                "status": "done",
                "final_text": "answer",
                "content_blocks": [{"type": "text", "content": "y" * 10000}],
            },
            "sa2": {"status": "done", "final_text": "lean"},
        },
    }
    out = _project_message_slim(dict(msg))
    assert "content_blocks" not in out["subagent_runs"]["sa1"]
    assert out["subagent_runs"]["sa1"]["final_text"] == "answer"
    assert out["subagent_runs"]["sa2"] == {"status": "done", "final_text": "lean"}
    # stored row untouched
    assert "content_blocks" in msg["subagent_runs"]["sa1"]
