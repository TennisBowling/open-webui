import html
import json

from open_webui.utils.chat import _merge_outlet_filter_into_content_blocks


def _serialize(blocks):
    # Mirror serialize_content_blocks for the block types this test exercises.
    content = ""
    for block in blocks:
        btype = block["type"]
        if btype == "text":
            bc = block["content"].strip()
            if bc:
                content = f"{content}{bc}\n"
        elif btype == "reasoning":
            rdc = "\n".join(
                (f"> {l}" if not l.startswith(">") else l)
                for l in block["content"].splitlines()
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


def test_filter_prepends_text_updates_only_first_text_block():
    blocks = [{"type": "text", "content": "hello world"}]
    orig = _serialize(blocks)
    filt = "PREFIX: " + orig
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, filt)
    assert result == [{"type": "text", "content": "PREFIX: hello world"}]


def test_filter_appends_after_reasoning_preserves_reasoning():
    blocks = [
        {"type": "text", "content": "hi"},
        {"type": "reasoning", "content": "thinking step", "duration": 2},
        {"type": "text", "content": "world"},
    ]
    orig = _serialize(blocks)
    filt = orig + " EXTRA"
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, filt)
    assert result is not None
    assert result[0] == blocks[0]
    assert result[1] == blocks[1]
    assert result[2]["content"] == "world EXTRA"


def test_filter_wraps_text_updates_multiple_blocks_structure_intact():
    blocks = [
        {"type": "text", "content": "a"},
        {"type": "reasoning", "content": "r", "duration": 1},
        {"type": "text", "content": "b"},
    ]
    orig = _serialize(blocks)
    filt = "[start]\n" + orig + "\n[end]"
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, filt)
    assert result is not None
    assert result[1] == blocks[1]
    assert result[0]["content"] == "[start]\na"
    assert result[2]["content"] == "b\n[end]"


def test_filter_returns_identical_content_is_noop():
    blocks = [
        {"type": "text", "content": "hello"},
        {"type": "reasoning", "content": "r", "duration": 1},
        {"type": "text", "content": "world"},
    ]
    orig = _serialize(blocks)
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, orig)
    assert result == blocks


def test_filter_mutates_structural_marker_returns_none():
    blocks = [
        {"type": "text", "content": "hi"},
        {"type": "reasoning", "content": "r", "duration": 1},
    ]
    orig = _serialize(blocks)
    filt = orig.replace('done="true"', 'done="false"')
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, filt)
    assert result is None


def test_filter_drops_structural_marker_returns_none():
    # Previously-flagged bug: blanking text blocks when filter dropped a
    # structural marker. Must fail safe instead.
    import re

    blocks = [
        {"type": "text", "content": "hi"},
        {"type": "reasoning", "content": "r", "duration": 1},
        {"type": "text", "content": "bye"},
    ]
    orig = _serialize(blocks)
    filt = re.sub(r"<details.*?</details>", "", orig, flags=re.DOTALL).strip()
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, filt)
    assert result is None


def test_filter_invents_text_run_returns_none():
    blocks = [
        {"type": "text", "content": "hi"},
        {"type": "reasoning", "content": "r", "duration": 1},
    ]
    orig = _serialize(blocks)
    filt = orig + "\nappended"
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, filt)
    assert result is None


def test_filter_preserves_tool_calls_block_byte_identical():
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
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, filt)
    assert result is not None
    # Tool calls block must be byte-identical (same object content).
    assert result[1] == blocks[1]
    assert result[2]["content"] == "Summary follows!!!"
    assert result[0] == blocks[0]


def test_filter_blanks_text_block_emits_change():
    # Previously-flagged bug (b): when filter empties a text block whose
    # original had content, the merge must still return a changed block
    # (so the caller emits a replace delta). The actual emit happens in
    # the caller; here we verify the merge produces a different block.
    blocks = [
        {"type": "text", "content": "to be removed"},
        {"type": "reasoning", "content": "r", "duration": 1},
        {"type": "text", "content": "kept"},
    ]
    orig = _serialize(blocks)
    # Filter strips the first text run entirely.
    filt = orig.replace("to be removed\n", "")
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, filt)
    assert result is not None
    assert result[0]["content"] == ""
    assert result[0] != blocks[0]
    assert result[1] == blocks[1]
    assert result[2] == blocks[2]


def test_multiple_consecutive_text_blocks_in_one_run_fail_safe():
    # Two consecutive text blocks would serialize into a single text run;
    # we cannot unambiguously re-attribute a mutation between them.
    blocks = [
        {"type": "text", "content": "a"},
        {"type": "text", "content": "b"},
    ]
    orig = _serialize(blocks)
    filt = orig + " more"
    result = _merge_outlet_filter_into_content_blocks(blocks, orig, filt)
    assert result is None
