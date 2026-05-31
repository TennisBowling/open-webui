"""Tests for B12's outlet-filter merge.

Two unconditional invariants (NO fail-safe):
  1. Structural blocks (reasoning, tool_calls, subagent_launch,
     subagent_launch, ...) are preserved byte-identical regardless of
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


# -- Elided-marker text POSITIONING (the fix this PR adds) -------------------


def _text_block_positions(result):
    """Return list of (index_in_result, content) for text blocks."""
    return [(i, b["content"]) for i, b in enumerate(result) if b.get("type") == "text"]


def test_elided_marker_preserves_text_position_around_surviving_struct():
    """When the filter elides a <details> marker but keeps the
    surrounding text intact, the text that was BEFORE the elided
    marker in the original must remain in the slot BEFORE the
    surviving structural that was previously AFTER it — not collapsed
    into the slot AFTER it. This is the precise UX wart B12 leaves
    behind and what this PR fixes."""
    blocks = [
        {"type": "text", "content": "before"},
        {"type": "reasoning", "content": "r1", "duration": 1},
        {"type": "text", "content": "middle"},
        {
            "type": "tool_calls",
            "content": [
                {"id": "c1", "function": {"name": "t", "arguments": {"a": 1}}}
            ],
            "results": [{"tool_call_id": "c1", "content": "ok"}],
        },
        {"type": "text", "content": "after"},
    ]
    orig = _serialize(blocks)
    # Filter elides ONLY the reasoning marker; keeps tool_calls marker
    # and surrounding text intact.
    filt = re.sub(
        r'<details type="reasoning".*?</details>\n?', "", orig, flags=re.DOTALL
    )
    result = _apply_outlet_text_to_blocks(blocks, orig, filt)

    # Find positions of structurals + assert "before" lives BEFORE the
    # reasoning block in the result (which itself sits BEFORE
    # tool_calls).
    reasoning_idx = next(i for i, b in enumerate(result) if b.get("type") == "reasoning")
    tool_idx = next(i for i, b in enumerate(result) if b.get("type") == "tool_calls")
    assert reasoning_idx < tool_idx, "Structural order must be preserved"

    # "before" must appear in a text block at an index < reasoning_idx.
    before_locations = [
        i for i, b in enumerate(result)
        if b.get("type") == "text" and "before" in b["content"]
    ]
    assert before_locations, "'before' text dropped"
    assert min(before_locations) < reasoning_idx, (
        f"'before' must be positioned BEFORE the surviving reasoning block, "
        f"got at index {before_locations} vs reasoning at {reasoning_idx}; "
        f"result={result}"
    )

    # "middle" must appear between reasoning and tool_calls.
    middle_locations = [
        i for i, b in enumerate(result)
        if b.get("type") == "text" and "middle" in b["content"]
    ]
    assert middle_locations, "'middle' text dropped"
    assert any(reasoning_idx < i < tool_idx for i in middle_locations), (
        f"'middle' must be between reasoning and tool_calls; result={result}"
    )

    # "after" must appear after tool_calls.
    after_locations = [
        i for i, b in enumerate(result)
        if b.get("type") == "text" and "after" in b["content"]
    ]
    assert after_locations, "'after' text dropped"
    assert max(after_locations) > tool_idx, (
        f"'after' must be after tool_calls; result={result}"
    )


def test_elided_marker_with_intact_surrounding_text_three_text_slots():
    """Simpler version: one structural in the middle is the SECOND of
    two structurals; the first is elided. Text positioning around the
    SURVIVOR must reflect original semantics."""
    blocks = [
        {"type": "text", "content": "ALPHA"},
        {"type": "reasoning", "content": "r", "duration": 1},  # elided by filter
        {"type": "text", "content": "BETA"},
        {
            "type": "tool_calls",
            "content": [
                {"id": "c", "function": {"name": "t", "arguments": {}}}
            ],
            "results": [{"tool_call_id": "c", "content": "ok"}],
        },
        {"type": "text", "content": "GAMMA"},
    ]
    orig = _serialize(blocks)
    filt = re.sub(
        r'<details type="reasoning".*?</details>\n?', "", orig, flags=re.DOTALL
    )
    result = _apply_outlet_text_to_blocks(blocks, orig, filt)
    # Structural blocks preserved byte-identical.
    assert any(b == blocks[1] for b in result)  # reasoning
    assert any(b == blocks[3] for b in result)  # tool_calls
    # ALPHA before reasoning; BETA between reasoning and tool_calls;
    # GAMMA after tool_calls.
    reasoning_idx = next(i for i, b in enumerate(result) if b.get("type") == "reasoning")
    tool_idx = next(i for i, b in enumerate(result) if b.get("type") == "tool_calls")
    alpha_idx = next(
        i for i, b in enumerate(result)
        if b.get("type") == "text" and "ALPHA" in b["content"]
    )
    beta_idx = next(
        i for i, b in enumerate(result)
        if b.get("type") == "text" and "BETA" in b["content"]
    )
    gamma_idx = next(
        i for i, b in enumerate(result)
        if b.get("type") == "text" and "GAMMA" in b["content"]
    )
    assert alpha_idx < reasoning_idx < beta_idx < tool_idx < gamma_idx


def test_elided_marker_with_partially_rewritten_text_invariants_hold():
    """Filter elides a marker AND rewrites the surrounding text so
    substring anchors no longer match. Invariants still hold:
    structural preserved + filter text present somewhere."""
    blocks = [
        {"type": "text", "content": "before"},
        {"type": "reasoning", "content": "r", "duration": 1},
        {"type": "text", "content": "after"},
    ]
    orig = _serialize(blocks)
    # Drop reasoning marker AND rewrite both surrounding texts so
    # neither "before" nor "after" appears in filter output.
    filt = re.sub(
        r'<details type="reasoning".*?</details>\n?', "", orig, flags=re.DOTALL
    )
    filt = filt.replace("before", "FOO").replace("after", "BAR")
    result = _apply_outlet_text_to_blocks(blocks, orig, filt)
    # Reasoning preserved byte-identical.
    reasoning_blocks = [b for b in result if b.get("type") == "reasoning"]
    assert len(reasoning_blocks) == 1
    assert reasoning_blocks[0] == blocks[1]
    # Filter text present somewhere.
    text_content = "".join(b["content"] for b in result if b.get("type") == "text")
    assert "FOO" in text_content
    assert "BAR" in text_content


def test_multiple_elided_markers_in_sequence_positions_respected():
    """Two structurals in a row, BOTH elided by the filter. A third
    structural survives. Text positioning around the survivor still
    holds; the elided pair's structural blocks are preserved."""
    blocks = [
        {"type": "text", "content": "P0"},
        {"type": "reasoning", "content": "r1", "duration": 1},  # elided
        {"type": "text", "content": "P1"},
        {"type": "reasoning", "content": "r2", "duration": 2},  # elided
        {"type": "text", "content": "P2"},
        {
            "type": "tool_calls",
            "content": [
                {"id": "c", "function": {"name": "t", "arguments": {}}}
            ],
            "results": [{"tool_call_id": "c", "content": "ok"}],
        },
        {"type": "text", "content": "P3"},
    ]
    orig = _serialize(blocks)
    filt = re.sub(
        r'<details type="reasoning".*?</details>\n?', "", orig, flags=re.DOTALL
    )
    result = _apply_outlet_text_to_blocks(blocks, orig, filt)

    # All structural blocks preserved, in order.
    struct_types_in_order = [
        b for b in result if b.get("type") in ("reasoning", "tool_calls")
    ]
    assert struct_types_in_order == [blocks[1], blocks[3], blocks[5]]

    # P3 must be after tool_calls.
    tool_idx = next(i for i, b in enumerate(result) if b.get("type") == "tool_calls")
    p3_idx = next(
        i for i, b in enumerate(result)
        if b.get("type") == "text" and "P3" in b["content"]
    )
    assert p3_idx > tool_idx

    # P0/P1/P2 all appear somewhere before tool_calls (their relative
    # positioning across two elided markers is best-effort; the strong
    # guarantee is they land in the pre-tool_calls region, not after).
    pre_tool_text = "".join(
        b["content"] for b in result[:tool_idx] if b.get("type") == "text"
    )
    assert "P0" in pre_tool_text
    assert "P1" in pre_tool_text
    assert "P2" in pre_tool_text
