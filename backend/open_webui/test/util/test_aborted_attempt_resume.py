"""The resume boundary, and the totality of the legacy content projection.

Both invariants here were learned from one chat (aa71be8b, 2026-07-31), the first
time conversation compaction ever fired on a real overflow:

* ``serialize_content_blocks`` dispatched on block type and fell through to
  ``str(block["content"])`` for anything it didn't know. A ``compaction`` anchor
  has no ``content`` key, so the terminal finaliser raised ``KeyError('content')``
  AFTER a complete answer had streamed but BEFORE ``done: True`` was persisted.
  The user saw the error ``'content'``.
* The client auto-retried. The backend seeds ``content_blocks`` from the
  persisted row so completed tool rounds survive a retry — but the row ended on
  the dead attempt's half-written text block, and the stream handler appends into
  a trailing text block. Five attempts concatenated into one 14k-char block, each
  restarting mid-sentence off the stump of the last.

Pure data, no DB, no event loop.
"""

from open_webui.utils.compaction import make_compaction_block
from open_webui.utils.messages import (
    count_assistant_emissions,
    is_aborted_attempt,
    resume_boundary_blocks,
)


def _tool_calls_block(call_id, name="web_search", result="r"):
    return {
        "type": "tool_calls",
        "content": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
        ],
        "results": [{"tool_call_id": call_id, "content": result}],
    }


# ---------------------------------------------------------------------------
# 1. The resume boundary
# ---------------------------------------------------------------------------


def test_the_dead_attempts_half_written_answer_is_dropped():
    """THE bug: without this the retry's fresh answer is appended onto the stump
    of the previous one, inside the same block."""
    blocks = [
        {"type": "reasoning", "content": "thinking"},
        _tool_calls_block("c1"),
        {"type": "reasoning", "content": "more thinking"},
        {"type": "text", "content": "Short version: probably not a bag of cred"},
    ]
    kept = resume_boundary_blocks(blocks)
    assert [b["type"] for b in kept] == ["reasoning", "tool_calls"]


def test_completed_tool_rounds_are_kept_verbatim():
    """Resuming in place is the whole point — the tool results must not be
    re-fetched, and the calls must not be re-issued."""
    blocks = [_tool_calls_block("c1"), _tool_calls_block("c2")]
    kept = resume_boundary_blocks(blocks)
    assert kept is blocks
    assert [b["type"] for b in kept] == ["tool_calls", "tool_calls"]


def test_commentary_before_a_tool_call_survives():
    """Only the TRAILING run of regenerable prose goes. Text the model wrote and
    then acted on is part of the completed work."""
    blocks = [
        {"type": "text", "content": "Let me look that up."},
        _tool_calls_block("c1"),
        {"type": "text", "content": "partial ans"},
    ]
    kept = resume_boundary_blocks(blocks)
    assert [b["type"] for b in kept] == ["text", "tool_calls"]
    assert kept[0]["content"] == "Let me look that up."


def test_a_compaction_anchor_is_never_trimmed():
    """The narrative is generate-once by contract and cost a summarizer call.
    Losing it to a retry would silently re-expand the context AND re-bill it."""
    blocks = [
        _tool_calls_block("c1"),
        make_compaction_block("## Findings\nstuff", covers=66),
        {"type": "text", "content": "partial"},
    ]
    kept = resume_boundary_blocks(blocks)
    assert [b["type"] for b in kept] == ["tool_calls", "compaction"]


def test_a_user_steer_is_never_trimmed():
    """A mid-task interjection is history the user authored; it is not
    regenerable by definition."""
    blocks = [
        _tool_calls_block("c1"),
        {"type": "user_steer", "content": "actually check 2026 not 2023"},
        {"type": "text", "content": "partial"},
    ]
    kept = resume_boundary_blocks(blocks)
    assert [b["type"] for b in kept] == ["tool_calls", "user_steer"]


def test_a_pure_prose_attempt_trims_to_empty():
    """No tool calls means nothing to resume from: the retry starts clean rather
    than continuing a sentence."""
    blocks = [
        {"type": "reasoning", "content": "thinking"},
        {"type": "text", "content": "Short answer: prob"},
    ]
    assert resume_boundary_blocks(blocks) == []


def test_identity_is_preserved_when_nothing_is_trimmed():
    blocks = [_tool_calls_block("c1")]
    assert resume_boundary_blocks(blocks) is blocks
    assert resume_boundary_blocks([]) == []
    assert resume_boundary_blocks(None) == []


# ---------------------------------------------------------------------------
# 2. Which rows are aborted attempts
# ---------------------------------------------------------------------------


def test_a_terminal_error_is_an_aborted_attempt_even_though_it_is_done():
    """The interlock that is easy to get wrong: a terminal error now persists
    `done: true` (terminal means terminal), so `done` alone can no longer tell a
    complete answer from a stump. Miss this and the trim silently stops firing on
    exactly the rows it exists for."""
    assert is_aborted_attempt({"done": True, "error": {"content": "boom"}}) is True
    assert is_aborted_attempt({"done": False}) is True
    assert is_aborted_attempt({}) is True


def test_a_cleanly_finished_row_is_not_an_aborted_attempt():
    """Continue Response re-opens the trailing text block of a finished turn and
    appends to it. Trimming there would delete the answer it is continuing."""
    assert is_aborted_attempt({"done": True}) is False
    assert is_aborted_attempt({"done": True, "error": None}) is False


def test_a_user_stop_is_not_an_aborted_attempt():
    """The user chose that stopping point; Continue Response is supposed to pick
    the sentence back up. Checked before `done` because an interrupted teardown
    can leave a stopped row without it."""
    assert is_aborted_attempt({"done": True, "userStopped": True}) is False
    assert is_aborted_attempt({"done": False, "userStopped": True}) is False


# ---------------------------------------------------------------------------
# 3. Emission accounting (reasoning_details_per_round is indexed by emission)
# ---------------------------------------------------------------------------


def test_emission_count_matches_the_kept_tool_rounds():
    """Trimming blocks without trimming `reasoning_details_per_round` by the same
    amount hands a later emission a previous round's `rs_*` items, which OpenAI
    Responses rejects as a duplicate item id (REASONING_DETAILS.md §6 Bug B)."""
    blocks = [
        {"type": "reasoning", "content": "r1"},
        _tool_calls_block("c1"),
        {"type": "reasoning", "content": "r2"},
        _tool_calls_block("c2"),
        {"type": "reasoning", "content": "r3"},
        {"type": "text", "content": "partial answer"},
    ]
    # Three rounds streamed; the third produced only prose and died.
    kept = resume_boundary_blocks(blocks)
    assert count_assistant_emissions(kept) == 2
    per_round = [["a"], ["b"], ["c"]]
    del per_round[count_assistant_emissions(kept) :]
    assert per_round == [["a"], ["b"]]


# ---------------------------------------------------------------------------
# 4. The legacy projection is TOTAL
# ---------------------------------------------------------------------------


# `serialize_content_blocks` is a closure inside `process_chat_response` (it
# reads `metadata`, the protocol version, and the realtime-save flag), so there
# is no import path to call it directly and a copy of its body here would drift
# away from the original. Assert the property that actually regressed instead:
# no branch in it may subscript a block.
def test_no_branch_in_the_legacy_projection_subscripts_a_block():
    """The crash was `str(block["content"])` in the unknown-type fallback. Any
    `block["..."]` in this function is the same bug waiting for the next block
    type someone adds — and block types get added (compaction, user_steer,
    tool_selection_change all postdate this function)."""
    import inspect
    import io
    import re
    import tokenize

    # Inspect the FUNCTION, not the module: the 2026-08-02 de-spaghettification
    # moved serialize_content_blocks to utils/streaming/serialize.py (middleware
    # re-exports it), and getsource of the function follows it wherever it
    # lives next.
    from open_webui.utils.streaming.serialize import serialize_content_blocks

    body = inspect.getsource(serialize_content_blocks)

    # Strip comments and string literals — this very test's rationale quotes the
    # offending expression, and so does the fix's own comment.
    code = "".join(
        tok.string if tok.type not in (tokenize.COMMENT, tokenize.STRING) else " "
        for tok in tokenize.generate_tokens(io.StringIO(body).readline)
    )

    offenders = re.findall(r'block\s*\[\s*["\'][a-z_]+["\']\s*\]', code)
    assert offenders == [], (
        "serialize_content_blocks must read blocks with .get() only — a block "
        f"type without these keys crashes the terminal persist: {offenders}"
    )


def test_the_compaction_block_shape_that_crashed_has_no_content_key():
    """Lock the premise: if `make_compaction_block` ever grows a `content` key
    this test should fail loudly rather than let the totality tests above pass
    for the wrong reason."""
    block = make_compaction_block("narrative", covers=66, tokens=226239)
    assert "content" not in block
    assert block["type"] == "compaction"
