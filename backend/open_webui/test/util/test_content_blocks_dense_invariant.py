"""Structural invariant: ``content_blocks`` is a DENSE list of block dicts.

A client stream mirror that missed early ``block_open`` ops used to assign
past the array end, creating JS array holes that serialize as JSON ``null``;
a rewind sibling then persisted a clone of that (production chat 625192d9:
``[null, null, reasoning, tool_calls, user_steer, text]``), and every
``.get()`` walker downstream crashed with "'NoneType' object has no attribute
'get'" — surfaced to the user as "Failed to assemble conversation".

Two layers under test here:
- ``sanitize_content_blocks`` (utils/lazy_blocks.py): the write-boundary
  enforcement applied by the message upsert + append_message op.
- read-path tolerance: rows persisted BEFORE the invariant existed must still
  convert/degrade instead of failing the whole turn.
"""

from open_webui.utils.lazy_blocks import (
    sanitize_content_blocks,
    split_tool_result_bodies,
)
from open_webui.utils.messages import blocks_to_api_messages, blocks_to_plain_text


def _poisoned_message():
    # Mirrors the real production row: two nulls where round 1 used to be,
    # round 2 kept, then a mid-task steer and the trailing stream target.
    return {
        "role": "assistant",
        "content": "",
        "content_blocks": [
            None,
            None,
            {"type": "reasoning", "content": "thinking...", "duration": 7},
            {
                "type": "tool_calls",
                "content": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": "{}"},
                    }
                ],
                "results": [{"tool_call_id": "c1", "content": "found stuff"}],
            },
            {"type": "user_steer", "content": "KEEP GOING"},
            {"type": "text", "content": ""},
        ],
    }


def test_sanitize_drops_non_dict_entries():
    blocks = _poisoned_message()["content_blocks"]
    clean = sanitize_content_blocks(blocks)
    assert len(clean) == 4
    assert all(isinstance(b, dict) for b in clean)
    assert [b["type"] for b in clean] == [
        "reasoning",
        "tool_calls",
        "user_steer",
        "text",
    ]


def test_sanitize_returns_same_object_when_already_dense():
    blocks = [{"type": "text", "content": "hi"}]
    assert sanitize_content_blocks(blocks) is blocks


def test_sanitize_passes_non_list_through():
    assert sanitize_content_blocks(None) is None
    assert sanitize_content_blocks("nope") == "nope"


def test_outbound_conversion_tolerates_null_blocks():
    """A pre-invariant poisoned row must convert instead of raising — this is
    the exact call chain behind "Failed to assemble conversation"."""
    out = blocks_to_api_messages([_poisoned_message()])
    roles = [m["role"] for m in out]
    # Tool round -> assistant(tool_calls) + tool result, then the steer as a
    # real user turn. Nulls contribute nothing.
    assert "assistant" in roles and "tool" in roles and "user" in roles
    steer = next(m for m in out if m["role"] == "user")
    assert steer["content"] == "KEEP GOING"


def test_plain_text_projection_tolerates_null_blocks():
    msg = _poisoned_message()
    msg["content_blocks"][5]["content"] = "final answer"
    text = blocks_to_plain_text(msg["content_blocks"])
    assert "final answer" in text
    assert "KEEP GOING" in text


def test_split_tool_result_bodies_tolerates_null_blocks():
    blocks = _poisoned_message()["content_blocks"]
    slim, bodies = split_tool_result_bodies(blocks)
    assert len(slim) == len(blocks)  # write-splitter preserves positions
    assert isinstance(bodies, dict)
