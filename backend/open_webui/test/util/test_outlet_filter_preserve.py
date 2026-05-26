"""Tests for B12's outlet-filter merge.

Two unconditional invariants (NO fail-safe):
  1. Structural blocks (reasoning, tool_calls, subagent_launch,
     code_interpreter, ...) are preserved byte-identical regardless of
     what the filter does to their serialized markers.
  2. The filter's textual edits are ALWAYS applied — never silently
     dropped — even when the filter elides or reformats markers.
"""

import html
import json
import re

from open_webui.utils.chat import (
    _apply_outlet_text_to_blocks,
    _merge_outlet_filter_into_content_blocks,
)


def _serialize(blocks):
    """Mirror serialize_content_blocks for the block types this suite uses."""
    content = ""
    for block in blocks:
        btype = block["type"]
        if btype == "text":
            bc = block["content"].strip()
            if bc:
                content = f"{content}{bc}\n"
        elif btype == "reasoning":
            rdc = "\n".join(
                (f"> {line}" if not line.startswith(">") else line)
                for line in block["content"].splitlines()
            )
            if content and not content.endswith("\n"):
                content += "\n"
            dur = block.get("duration")
            if dur is not None:
                content = (
                    f'{content}<details type="reasoning" done="true" '
                    f'duration="{dur}">\n<summary>Thought for {dur} seconds'
                    f"</summary>\n{rdc}\n</details>\n"
                )
            else:
                content = (
                    f'{content}<details type="reasoning" done="false">\n'
                    f"<summary>Thinking…</summary>\n{rdc}\n</details>\n"
                )
        elif btype == "tool_calls":
            if content and not content.endswith("\n"):
                content += "\n"
            for tc in block.get("content", []):
                tcid = tc["id"]
                name = tc["function"]["name"]
                args = tc["function"]["arguments"]
                content += (
                    f'<details type="tool_calls" done="true" id="{tcid}" '
                    f'name="{name}" arguments="{html.escape(json.dumps(args))}" '
                    f'result="..." files="" embeds="">\n'
                    f"<summary>Tool Executed</summary>\n</details>\n"
                )
        elif btype == "code_interpreter":
            if content and not content.endswith("\n"):
                content += "\n"
            content += (
                f'<details type="code_interpreter" done="true" output="x">\n'
                f"<summary>Analyzed</summary>\n```py\n"
                f'{block["content"]}\n```\n</details>\n'
            )
    return content.strip()


# -- Invariant 1: identity ---------------------------------------------------


def test_identical_content_is_noop():
    blocks = [
        {"type": "text", "content": "hello"},
        {"type": "reasoning", "content": "r", "duration": 1},
        {"type": "text", "content": "world"},
    ]
    orig = _serialize(blocks)
    result = _apply_outlet_text_to_blocks(blocks, orig, orig)
    assert result == blocks


# -- Invariant 2: text edits land, structure preserved -----------------------


def test_prepend_text_creates_or_updates_leading_text_block():
    blocks = [{"type": "text", "content": "hello world"}]
    orig = _serialize(blocks)
    filt = "PREFIX: " + orig
    result = _apply_outlet_text_to_blocks(blocks, orig, filt)
    assert result == [{"type": "text", "content": "PREFIX: hello world"}]


def test_append_text_with_reasoning_in_middle():
    blocks = [
        {"type": "text", "content": "hi"},
        {"type": "reasoning", "content": "thinking step", "duration": 2},
        {"type": "text", "content": "world"},
    ]
    orig = _serialize(blocks)
    filt = orig + " EXTRA"
    result = _apply_outlet_text_to_blocks(blocks, orig, filt)
    # Structural block byte-identical.
    assert result[1] is blocks[1] or result[1] == blocks[1]
    assert result[0]["content"] == "hi"
    assert result[2]["content"] == "world EXTRA"


def test_replace_middle_text_keeps_structurals():
    blocks = [
        {"type": "text", "content": "Here are results:"},
        {
            "type": "tool_calls",
            "content": [
                {
                    "id": "call_1",
                    "function": {"name": "web_search", "arguments": {"q": "x"}},
                }
            ],
            "results": [{"tool_call_id": "call_1", "content": "..."}],
        },
        {"type": "text", "content": "Summary follows."},
    ]
    orig = _serialize(blocks)
    filt = orig.replace("Summary follows.", "Summary follows!!!")
    result = _apply_outlet_text_to_blocks(blocks, orig, filt)
    assert result[0] == blocks[0]
    assert result[1] == blocks[1]  # tool_calls byte-identical
    assert result[2]["content"] == "Summary follows!!!"


# -- Invariant 1: structural preservation under filter elision ---------------


def test_filter_elides_one_marker_block_still_preserved():
    """Filter strips the reasoning <details> marker. Reasoning block must
    survive byte-identical; surrounding text reflows around it."""
    blocks = [
        {"type": "text", "content": "before"},
        {"type": "reasoning", "content": "r", "duration": 1},
        {"type": "text", "content": "after"},
    ]
    orig = _serialize(blocks)
    filt = re.sub(r'<details type="reasoning".*?</details>\n?', "", orig, flags=re.DOTALL)
    result = _apply_outlet_text_to_blocks(blocks, orig, filt)
    # Reasoning block is preserved.
    reasoning_blocks = [b for b in result if b.get("type") == "reasoning"]
    assert len(reasoning_blocks) == 1
    assert reasoning_blocks[0] == blocks[1]
    # All filter text is present somewhere in the resulting text blocks.
    text_content = "".join(b["content"] for b in result if b.get("type") == "text")
    assert "before" in text_content
    assert "after" in text_content


def test_filter_elides_all_markers_all_structurals_preserved():
    blocks = [
        {"type": "text", "content": "intro"},
        {"type": "reasoning", "content": "r1", "duration": 1},
        {"type": "text", "content": "mid"},
        {
            "type": "tool_calls",
            "content": [
                {"id": "c1", "function": {"name": "t", "arguments": {"a": 1}}}
            ],
            "results": [{"tool_call_id": "c1", "content": "ok"}],
        },
        {"type": "text", "content": "outro"},
    ]
    orig = _serialize(blocks)
    # Strip every <details ...> marker entirely; replace with a brand-new
    # filter-generated message.
    filt = "FILTER ONLY OUTPUT"
    result = _apply_outlet_text_to_blocks(blocks, orig, filt)
    # All structural blocks preserved, in original order.
    struct = [b for b in result if b.get("type") != "text"]
    assert struct == [blocks[1], blocks[3]]
    # Filter text appears somewhere.
    text_content = "".join(b["content"] for b in result if b.get("type") == "text")
    assert "FILTER ONLY OUTPUT" in text_content


def test_filter_returns_empty_string_blanks_text_keeps_structurals():
    blocks = [
        {"type": "text", "content": "to vanish"},
        {"type": "reasoning", "content": "r", "duration": 1},
        {"type": "text", "content": "also vanishing"},
    ]
    orig = _serialize(blocks)
    result = _apply_outlet_text_to_blocks(blocks, orig, "")
    # Reasoning block preserved.
    reasoning_blocks = [b for b in result if b.get("type") == "reasoning"]
    assert len(reasoning_blocks) == 1
    assert reasoning_blocks[0] == blocks[1]
    # No remaining text content.
    text_content = "".join(b["content"] for b in result if b.get("type") == "text")
    assert text_content == ""


# -- Invariant 2: filter wraps with prefix/suffix ----------------------------


def test_filter_wraps_with_prefix_and_suffix():
    blocks = [
        {"type": "text", "content": "a"},
        {"type": "reasoning", "content": "r", "duration": 1},
        {"type": "text", "content": "b"},
    ]
    orig = _serialize(blocks)
    filt = "[start]\n" + orig + "\n[end]"
    result = _apply_outlet_text_to_blocks(blocks, orig, filt)
    assert result[1] == blocks[1]
    # Prefix lands in first text slot, suffix in last.
    assert result[0]["content"].startswith("[start]")
    assert "a" in result[0]["content"]
    assert result[2]["content"].endswith("[end]")
    assert "b" in result[2]["content"]


# -- Adversarial: filter writes HTML that looks like a structural marker ----


def test_filter_invents_fake_details_marker_treated_as_text():
    """If the filter writes text that LOOKS like a <details type="custom">
    block, it must NOT be parsed as a new structural block — it stays
    in a text block. We only locate ORIGINAL markers, never re-parse
    the filter output for new markers."""
    blocks = [{"type": "text", "content": "hi"}]
    orig = _serialize(blocks)
    filt = orig + '\n<details type="custom">fake</details>'
    result = _apply_outlet_text_to_blocks(blocks, orig, filt)
    # No structural block appears; the fake marker lives inside a text block.
    assert all(b.get("type") == "text" for b in result)
    text_content = "".join(b["content"] for b in result if b.get("type") == "text")
    assert '<details type="custom">fake</details>' in text_content


# -- Backward-compat wrapper: never returns None ----------------------------


def test_compat_wrapper_never_returns_none_under_elision():
    blocks = [
        {"type": "text", "content": "hi"},
        {"type": "reasoning", "content": "r", "duration": 1},
        {"type": "text", "content": "bye"},
    ]
    orig = _serialize(blocks)
    filt = re.sub(r"<details.*?</details>", "", orig, flags=re.DOTALL).strip()
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, filt)
    assert result is not None
    # Reasoning still there.
    assert any(b.get("type") == "reasoning" and b == blocks[1] for b in result)


def test_compat_wrapper_never_returns_none_when_marker_attr_changed():
    blocks = [
        {"type": "text", "content": "hi"},
        {"type": "reasoning", "content": "r", "duration": 1},
    ]
    orig = _serialize(blocks)
    # Filter rewrites a struct marker attribute → original marker not
    # findable verbatim → treated as elided → structural still preserved.
    filt = orig.replace('done="true"', 'done="false"')
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, filt)
    assert result is not None
    assert any(b == blocks[1] for b in result)


# -- Multiple consecutive text blocks (no longer fail-safe) ------------------


def test_multiple_consecutive_text_blocks_collapse_into_first():
    """Two consecutive text blocks share one serialized run. Previously
    this was an ambiguity that triggered fail-safe; now we collapse the
    full run into the FIRST block and blank the others — text is never
    dropped."""
    blocks = [
        {"type": "text", "content": "a"},
        {"type": "text", "content": "b"},
    ]
    orig = _serialize(blocks)
    filt = orig + " more"
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, filt)
    assert result is not None
    # Combined text appears somewhere in the resulting text blocks.
    text_content = "".join(b["content"] for b in result if b.get("type") == "text")
    assert "more" in text_content
    assert "a" in text_content
    assert "b" in text_content


# -- Filter blanks just one text block ---------------------------------------


def test_filter_blanks_one_text_block_other_preserved():
    blocks = [
        {"type": "text", "content": "to be removed"},
        {"type": "reasoning", "content": "r", "duration": 1},
        {"type": "text", "content": "kept"},
    ]
    orig = _serialize(blocks)
    filt = orig.replace("to be removed\n", "")
    result = _apply_outlet_text_to_blocks(blocks, orig, filt)
    # First text block now empty, reasoning preserved, third unchanged.
    assert result[0]["content"] == ""
    assert result[1] == blocks[1]
    assert result[2]["content"] == "kept"
