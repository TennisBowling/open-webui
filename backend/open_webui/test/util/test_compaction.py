"""Conversation compaction — the invariants worth locking in forever.

Pure data. No HTTP, no DB, no event loop (the two async paths are driven with
``asyncio.run`` inside plain sync tests, because this repo has no pytest-asyncio
and an ``async def test_`` would be silently SKIPPED rather than failed).

The properties under test, in the order they'd hurt if they broke:

1. **The cut lands only at a completed ``tool_calls`` boundary**, so an outbound
   payload can never dangle a ``tool_use`` (COMPACTION.md §2 — upstream's issue
   #27035).
2. **The dense invariant survives the insert** (no holes, ever —
   ``open_webui_content_blocks_dense_invariant``).
3. **Live and replay produce byte-identical bytes.** The same conversation state
   assembled from the tree walk (turn start) and from the already-converted list
   plus the in-flight assistant (between rounds) must serialise identically or
   prompt caching dies silently.
4. **The mechanical sections are lossless across N compactions.**
5. **The trigger never fires on an unknown context window.**
6. **The summarizer's output is validated** — never ``reasoning_content``, never
   a truncated response (upstream's #27604).
"""

import asyncio

from open_webui.utils.compaction import (
    COMPACTION_BLOCK_TYPE,
    COMPACTION_CARRY_KEY,
    capture_compaction_envelope,
    CompactionError,
    apply_compaction_to_messages,
    collect_tool_index,
    collect_user_instructions,
    conversation_has_compacted_context,
    escape_envelope_delimiters,
    extract_summary,
    find_cut_index,
    find_last_compaction,
    generate_compaction_narrative,
    has_uncompacted_span,
    insert_compaction_block,
    make_compaction_block,
    render_compacted_context,
    should_compact,
    strip_compaction_carry,
    usage_total_tokens,
)
from open_webui.utils.messages import blocks_to_api_messages


def _tool_calls_block(call_id, name, args, result, **result_extra):
    return {
        "type": "tool_calls",
        "content": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        ],
        "results": [
            {"tool_call_id": call_id, "content": result, **result_extra}
        ],
    }


def _anchor(narrative="## Findings\nfound things", at="2026-07-30T15:22:41Z"):
    return make_compaction_block(narrative, covers=12, compacted_at=at)


# ---------------------------------------------------------------------------
# 1. The cut boundary
# ---------------------------------------------------------------------------


def test_cut_index_lands_immediately_after_the_last_tool_calls_block():
    blocks = [
        {"type": "reasoning", "content": "thinking"},
        _tool_calls_block("c1", "web_search", "{}", "r1"),
        {"type": "text", "content": "commentary"},
        _tool_calls_block("c2", "web_fetch", "{}", "r2"),
        {"type": "text", "content": ""},
    ]
    assert find_cut_index(blocks) == 4


def test_cut_index_for_a_completed_message_lands_at_the_end():
    """A finished turn's last block closes its last emission, so the write head —
    the place the compaction actually happened — is the END. Cutting after the
    last tool round instead drew the divider ABOVE the answer the user had just
    read, and dropped that answer from the payload."""
    blocks = [
        {"type": "reasoning", "content": "thinking"},
        _tool_calls_block("c1", "web_search", "{}", "r1"),
        {"type": "text", "content": "the final answer"},
    ]
    assert find_cut_index(blocks, completed=False) == 2
    assert find_cut_index(blocks, completed=True) == 3

    out = insert_compaction_block(blocks, _anchor(), completed=True)
    assert [b["type"] for b in out] == [
        "reasoning",
        "tool_calls",
        "text",
        COMPACTION_BLOCK_TYPE,
    ]


def test_cut_index_for_an_in_flight_message_stops_at_the_last_tool_round():
    """Mid-turn the END is NOT a boundary: the round that just streamed has
    already written its reasoning block while its tool_calls block is still
    queued, so cutting at the end would split that emission and slide
    `reasoning_details_per_round` out of step with the emissions it indexes."""
    blocks = [
        _tool_calls_block("c1", "web_search", "{}", "r1"),
        {"type": "reasoning", "content": "next round's thinking, tool call pending"},
    ]
    assert find_cut_index(blocks) == 1
    out = insert_compaction_block(blocks, _anchor())
    assert [b["type"] for b in out] == [
        "tool_calls",
        COMPACTION_BLOCK_TYPE,
        "reasoning",
    ]


def test_completed_cut_still_never_splits_a_tool_calls_block():
    blocks = [
        _tool_calls_block("c1", "web_search", "{}", "r1"),
        _tool_calls_block("c2", "web_fetch", "{}", "r2"),
        {"type": "text", "content": "done"},
    ]
    out = insert_compaction_block(blocks, _anchor(), completed=True)
    api = blocks_to_api_messages([{"role": "assistant", "content_blocks": out}])
    issued = {
        call["id"]
        for m in api
        if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or [])
    }
    answered = {m["tool_call_id"] for m in api if m.get("role") == "tool"}
    assert issued == answered


def test_cut_index_is_zero_when_there_is_no_tool_call_to_dangle():
    assert find_cut_index([{"type": "text", "content": "just an answer"}]) == 0
    assert find_cut_index([]) == 0
    assert find_cut_index(None) == 0


def test_cut_index_steps_over_an_existing_anchor():
    """Last anchor wins by block index, so a new anchor must never be inserted
    IN FRONT of an older one — that would hand the win back to the stale
    narrative, silently."""
    blocks = [
        _tool_calls_block("c1", "web_search", "{}", "r1"),
        _anchor("old"),
        {"type": "text", "content": ""},
    ]
    assert find_cut_index(blocks) == 2
    out = insert_compaction_block(blocks, _anchor("new"))
    mi_bi = find_last_compaction([{"role": "assistant", "content_blocks": out}])
    assert mi_bi is not None
    assert out[mi_bi[1]]["narrative"] == "new"


def test_cut_never_splits_a_tool_calls_block():
    """The parallel batch (calls AND results) lives in ONE block, so a cut at a
    block boundary can't separate a tool_use from its result. Assert the payload
    directly: every emitted tool_call id has a matching tool message."""
    blocks = [
        _tool_calls_block("c1", "web_search", "{}", "r1"),
        _tool_calls_block("c2", "web_fetch", "{}", "r2"),
        {"type": "text", "content": "done"},
    ]
    out = insert_compaction_block(blocks, _anchor())
    api = blocks_to_api_messages([{"role": "assistant", "content_blocks": out}])
    issued = {
        call["id"]
        for m in api
        if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or [])
    }
    answered = {m["tool_call_id"] for m in api if m.get("role") == "tool"}
    assert issued == answered


# ---------------------------------------------------------------------------
# 2. Dense invariant
# ---------------------------------------------------------------------------


def test_insert_keeps_the_block_list_dense():
    poisoned = [None, {"type": "text", "content": "hi"}, None]
    out = insert_compaction_block(poisoned, _anchor())
    assert all(isinstance(b, dict) for b in out)
    assert len(out) == 2


def test_insert_does_not_mutate_the_caller_list():
    blocks = [_tool_calls_block("c1", "web_search", "{}", "r1")]
    before = len(blocks)
    insert_compaction_block(blocks, _anchor())
    assert len(blocks) == before


# ---------------------------------------------------------------------------
# 3. Assembly
# ---------------------------------------------------------------------------


def _compacted_conversation():
    """system → user → assistant(2 tool rounds, anchor after them, then text)
    → user → assistant."""
    return [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "Research how X works"},
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", '{"q":"x"}', "Found 10 results\nbody"),
                _tool_calls_block("c2", "web_fetch", '{"url":"u"}', "page body"),
                _anchor(),
                {"type": "text", "content": "Here is the answer."},
            ],
        },
        {"role": "user", "content": "now compare against W"},
    ]


def test_assembly_drops_everything_before_the_anchor_and_keeps_the_system_prompt():
    out = blocks_to_api_messages(_compacted_conversation())
    assert out[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert out[1]["role"] == "user"
    assert out[1]["content"].startswith("<compacted_context")
    # The pre-cut tool rounds are gone from the payload entirely.
    assert not any(m.get("role") == "tool" for m in out)
    assert not any(m.get("tool_calls") for m in out)
    # Everything after the anchor survives verbatim.
    assert out[2] == {"role": "assistant", "content": "Here is the answer."}
    assert out[3] == {"role": "user", "content": "now compare against W"}


def test_assembly_is_a_noop_without_an_anchor():
    plain = [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "hi"},
    ]
    assert apply_compaction_to_messages(plain) is None
    assert blocks_to_api_messages(plain) == plain


def test_system_prompt_is_byte_identical_to_an_uncompacted_request():
    """COMPACTION.md §1: the summary is injected at the cut, NOT appended to the
    system prompt (which is what upstream does) — so the system+tools prefix
    stays cached and only the bytes from the cut forward are invalidated."""
    compacted = blocks_to_api_messages(_compacted_conversation())
    plain = blocks_to_api_messages(
        [{"role": "system", "content": "SYSTEM PROMPT"}, {"role": "user", "content": "x"}]
    )
    assert compacted[0] == plain[0]


def test_last_anchor_wins_across_repeated_compactions():
    convo = [
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{}", "r1"),
                _anchor("FIRST NARRATIVE"),
                _tool_calls_block("c2", "web_fetch", "{}", "r2"),
                _anchor("SECOND NARRATIVE"),
                {"type": "text", "content": "tail"},
            ],
        },
    ]
    out = blocks_to_api_messages(convo)
    envelope = out[0]["content"]
    assert "SECOND NARRATIVE" in envelope
    assert "FIRST NARRATIVE" not in envelope
    # c1 predates the second anchor and is still indexed — nothing is lost.
    assert 'ref="c1"' in envelope and 'ref="c2"' in envelope


def test_an_anchor_on_a_sibling_branch_is_not_applied():
    """Branch scoping is inherited from the tree walk: the caller hands us the
    path from the leaf, so an anchor that isn't an ancestor simply isn't here."""
    branch_without_anchor = [
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content_blocks": [_tool_calls_block("c1", "web_search", "{}", "r1")],
        },
    ]
    assert apply_compaction_to_messages(branch_without_anchor) is None


def test_reasoning_slots_are_sliced_with_the_blocks():
    """`reasoning_details_per_round` is indexed by EMISSION. Dropping the blocks
    of two rounds without dropping their reasoning slots would hand round 0's
    `rs_*` items to a post-cut emission."""
    convo = [
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{}", "r1"),
                _tool_calls_block("c2", "web_fetch", "{}", "r2"),
                _anchor(),
                _tool_calls_block("c3", "web_search", "{}", "r3"),
                {"type": "text", "content": "answer"},
            ],
            "reasoning_details_per_round": [
                [{"type": "reasoning.encrypted", "data": "A", "id": "rs_0"}],
                [{"type": "reasoning.encrypted", "data": "B", "id": "rs_1"}],
                [{"type": "reasoning.encrypted", "data": "C", "id": "rs_2"}],
                [],
            ],
        }
    ]
    out = blocks_to_api_messages(convo)
    attached = [
        d["id"]
        for m in out
        for d in (m.get("reasoning_details") or [])
        if isinstance(d, dict) and d.get("id")
    ]
    # Only round 3's reasoning survives, and it lands on round 3's emission.
    assert attached == ["rs_2"]
    tool_call_msg = next(m for m in out if m.get("tool_calls"))
    assert tool_call_msg["tool_calls"][0]["id"] == "c3"
    assert tool_call_msg["reasoning_details"][0]["id"] == "rs_2"


def test_no_duplicate_reasoning_ids_survive_a_compaction():
    """The one hard rule of REASONING_DETAILS.md, asserted through the cut."""
    convo = [
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{}", "r1"),
                _anchor(),
                _tool_calls_block("c2", "web_fetch", "{}", "r2"),
                {"type": "text", "content": "answer"},
            ],
            "reasoning_details_per_round": [
                [{"type": "reasoning.encrypted", "data": "A", "id": "rs_0"}],
                [{"type": "reasoning.encrypted", "data": "B", "id": "rs_1"}],
                [{"type": "reasoning.encrypted", "data": "B", "id": "rs_1"}],
            ],
        }
    ]
    out = blocks_to_api_messages(convo)
    ids = [
        d["id"]
        for m in out
        for d in (m.get("reasoning_details") or [])
        if isinstance(d, dict) and d.get("id")
    ]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# 4. Mechanical sections
# ---------------------------------------------------------------------------


def test_user_instructions_include_steers_and_exclude_synthetic_observations():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Research how X works"},
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{}", "r"),
                {"type": "user_steer", "content": "  actually focus on Y  "},
                {"type": "text", "content": "ok"},
            ],
        },
        {"role": "user", "name": "view_image_tool", "content": "tool observation"},
        {"role": "user", "content": "also compare against W"},
    ]
    assert collect_user_instructions(messages) == [
        "Research how X works",
        "actually focus on Y",
        "also compare against W",
    ]


def test_user_instructions_read_identically_from_both_message_shapes():
    """The steer block and the `{"role": "user"}` message it expands into must
    produce the same entry — this is what keeps a mid-turn assembly and the next
    turn's assembly byte-identical."""
    internal = [
        {"role": "user", "content": "do the thing"},
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{}", "r"),
                {"type": "user_steer", "content": "no, the other thing"},
                {"type": "text", "content": "ok"},
            ],
        },
    ]
    api_shape = blocks_to_api_messages(internal)
    assert collect_user_instructions(internal) == collect_user_instructions(api_shape)


def test_tool_index_reads_identically_from_both_message_shapes():
    internal = [
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block(
                    "c1",
                    "web_search",
                    '{"query":"openai compaction api"}',
                    "Found 10 results\n" + ("x" * 19000),
                ),
                _tool_calls_block("c2", "web_fetch", '{"url":"u"}', "y" * 43000),
                {"type": "text", "content": "done"},
            ],
        }
    ]
    api_shape = blocks_to_api_messages(internal)
    assert collect_tool_index(internal) == collect_tool_index(api_shape)


def test_tool_index_reads_the_size_of_a_slimmed_result_from_its_descriptor():
    """A persisted result is SLIM (content="") with a `summary` descriptor. The
    index must report the same size it would for the hydrated body."""
    hydrated = "Found 10 results\n" + ("x" * 19000)
    slim = [
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block(
                    "c1",
                    "web_search",
                    "{}",
                    "",
                    result_ref="c1",
                    result_lazy=True,
                    size=len(hydrated.encode()),
                    summary={
                        "kind": "web_search",
                        "size": len(hydrated.encode()),
                        "result_count": 10,
                    },
                )
            ],
        }
    ]
    inline = [
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{}", hydrated)
            ],
        }
    ]
    assert collect_tool_index(slim) == collect_tool_index(inline)
    assert collect_tool_index(slim)[0]["descriptor"] == "10 results, 18.6KB"


def test_tool_index_normalizes_a_doubled_provider_tool_name():
    """The API shape has already had `dedupe_repeated_tool_name` applied; the
    internal block still carries the raw name. Without the mirror in
    compaction.py the two shapes would disagree."""
    internal = [
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_searchweb_search", "{}", "r")
            ],
        }
    ]
    assert collect_tool_index(internal)[0]["name"] == "web_search"
    assert collect_tool_index(internal) == collect_tool_index(
        blocks_to_api_messages(internal)
    )


def test_mechanical_sections_are_lossless_across_two_compactions():
    """The fifth compaction still carries every user instruction and every tool
    call from the entire session (COMPACTION.md §4). Assert it for the second."""
    convo = [
        {"role": "user", "content": "first ask"},
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{}", "r1"),
                _anchor("N1"),
                {"type": "text", "content": "a1"},
            ],
        },
        {"role": "user", "content": "second ask"},
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c2", "web_fetch", "{}", "r2"),
                _anchor("N2"),
                {"type": "text", "content": "a2"},
            ],
        },
    ]
    envelope = blocks_to_api_messages(convo)[0]["content"]
    assert "<message>first ask</message>" in envelope
    assert "<message>second ask</message>" in envelope
    assert 'ref="c1"' in envelope and 'ref="c2"' in envelope
    assert "N2" in envelope and "N1" not in envelope


def test_mechanical_sections_survive_a_second_compaction_on_the_already_cut_list():
    """Path B: the between-rounds gate no longer sees the raw pre-first-cut
    history — the private carrier is what keeps it lossless."""
    turn_start = blocks_to_api_messages(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first ask"},
            {
                "role": "assistant",
                "content_blocks": [
                    _tool_calls_block("c1", "web_search", "{}", "r1"),
                    _anchor("N1"),
                    {"type": "text", "content": ""},
                ],
            },
        ]
    )
    assert any(COMPACTION_CARRY_KEY in m for m in turn_start)

    # A later round appends a new tool round and a second anchor in flight.
    mid_turn = blocks_to_api_messages(
        [
            *turn_start,
            {
                "role": "assistant",
                "content_blocks": [
                    _tool_calls_block("c2", "web_fetch", "{}", "r2"),
                    _anchor("N2"),
                    {"type": "text", "content": ""},
                ],
            },
        ]
    )
    envelope = next(
        m["content"] for m in mid_turn if str(m.get("content", "")).startswith("<compacted")
    )
    assert "<message>first ask</message>" in envelope  # carried, not re-walked
    assert 'ref="c1"' in envelope and 'ref="c2"' in envelope
    assert "N2" in envelope


def test_mid_turn_insert_lands_before_the_trailing_stream_target():
    """The agentic loop parks an empty `text` block after every tool round as the
    next round's stream target. The anchor goes in FRONT of it, so the tail stays
    the stream target (and the UI's tail cursor keeps working)."""
    blocks = [
        {"type": "reasoning", "content": "thinking", "ended_at": 1},
        _tool_calls_block("c1", "web_search", "{}", "r1"),
        {"type": "text", "content": ""},
    ]
    out = insert_compaction_block(blocks, _anchor())
    assert [b["type"] for b in out] == [
        "reasoning",
        "tool_calls",
        COMPACTION_BLOCK_TYPE,
        "text",
    ]


def test_mid_turn_shape_produces_a_valid_payload():
    """The exact wiring of the between-rounds gate: the already-cut list from
    turn start, plus the in-flight assistant carrying a fresh anchor."""
    turn_start = blocks_to_api_messages(
        [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "research this"},
        ]
    )
    in_flight = {
        "role": "assistant",
        "content_blocks": [
            _tool_calls_block("c1", "web_search", "{}", "r1"),
            _tool_calls_block("c2", "web_fetch", "{}", "r2"),
            _anchor(),
            {"type": "text", "content": ""},
        ],
    }
    out = blocks_to_api_messages([*turn_start, in_flight])

    assert out[0] == {"role": "system", "content": "SYSTEM"}
    assert out[1]["content"].startswith("<compacted_context")
    # Both pre-cut rounds are indexed, and neither leaves a dangling tool_use.
    assert 'ref="c1"' in out[1]["content"] and 'ref="c2"' in out[1]["content"]
    issued = {
        call["id"]
        for m in out
        if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or [])
    }
    answered = {m["tool_call_id"] for m in out if m.get("role") == "tool"}
    assert issued == answered == set()


def test_capture_returns_the_exact_outbound_bytes_and_its_anchor():
    """What the UI shows must be a copy of the wire payload, not a re-render."""
    convo = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first ask"},
        {
            "id": "msg-a",
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{}", "r1"),
                _anchor("N1"),
                {"type": "text", "content": "done"},
            ],
        },
    ]
    out = blocks_to_api_messages(convo)
    capture = capture_compaction_envelope(out)

    assert capture["message_id"] == "msg-a"
    assert capture["block_index"] == 1
    # Byte-identical to the injected message, and it survives the carrier strip.
    assert capture["envelope"] == out[1]["content"]
    assert capture["envelope"] == strip_compaction_carry(out)[1]["content"]


def test_capture_withholds_the_message_id_for_an_in_flight_anchor():
    """A mid-turn anchor lives on the API-shaped in-flight assistant, which has
    no id. `record_sent_envelope` keys off that to stay out of the way of the
    stream handler's own checkpoint — a whole-list write from the router would
    truncate blocks the round appended in between."""
    turn_start = blocks_to_api_messages(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "ask"}]
    )
    out = blocks_to_api_messages(
        [
            *turn_start,
            {
                "role": "assistant",
                "content_blocks": [
                    _tool_calls_block("c1", "web_search", "{}", "r1"),
                    _anchor("N1"),
                    {"type": "text", "content": ""},
                ],
            },
        ]
    )
    capture = capture_compaction_envelope(out)
    assert capture["envelope"].startswith("<compacted_context")
    assert capture["message_id"] is None


def test_capture_is_none_without_an_anchor():
    assert capture_compaction_envelope([{"role": "user", "content": "hi"}]) is None
    assert capture_compaction_envelope(None) is None


def test_stored_envelope_is_projected_out_of_the_read_path():
    """The bytes stay on the stored row (the endpoint reads them there) but never
    ship with the chat — the tail budget is why the lazy-body contract exists."""
    from open_webui.utils.lazy_blocks import slim_content_blocks_for_read

    envelope = "<compacted_context>" + ("x" * 4096) + "</compacted_context>"
    blocks = [{**_anchor("N1"), "envelope": envelope}]
    slim = slim_content_blocks_for_read(blocks)

    assert "envelope" not in slim[0]
    assert slim[0]["envelope_lazy"] is True
    assert slim[0]["envelope_size"] == len(envelope.encode("utf-8"))
    assert slim[0]["narrative"] == "N1"
    # Untouched anchors are returned by identity.
    plain = [_anchor("N1")]
    assert slim_content_blocks_for_read(plain) is plain


def test_record_sent_envelope_writes_once_and_only_onto_a_real_anchor(monkeypatch):
    """The write-back runs at the HTTP boundary, so it must be idempotent: one
    cut spans every round of an agentic turn and each round re-sends the same
    envelope."""
    from collections import OrderedDict

    from open_webui.models import chats as chats_module
    from open_webui.utils import compaction as compaction_module

    stored = {
        "msg-a": {"content_blocks": [{"type": "text", "content": "hi"}, _anchor("N1")]},
    }
    writes = []

    class _FakeChats:
        async def get_message_by_id_and_message_id(self, chat_id, message_id):
            return stored.get(message_id)

        async def upsert_message_to_chat_by_id_and_message_id(
            self, chat_id, message_id, data, return_model=True
        ):
            writes.append((message_id, data))
            stored[message_id] = {**stored[message_id], **data}

    monkeypatch.setattr(chats_module, "Chats", _FakeChats())
    monkeypatch.setattr(compaction_module, "_ENVELOPE_WRITTEN", OrderedDict())

    capture = {"message_id": "msg-a", "block_index": 1, "envelope": "<compacted_context>x"}
    run = lambda **kw: asyncio.run(  # noqa: E731
        compaction_module.record_sent_envelope(kw.pop("chat_id", "chat-1"), kw.get("capture", capture))
    )

    assert run() is True
    assert stored["msg-a"]["content_blocks"][1]["envelope"] == "<compacted_context>x"
    # Same bytes again on the next round: no read, no write.
    assert run() is False
    assert len(writes) == 1

    # A capture whose anchor is not a compaction block never writes.
    assert run(capture={**capture, "block_index": 0}) is False
    # Nor does one with no durable anchor (mid-turn) or a local-only chat.
    assert run(capture={**capture, "message_id": None}) is False
    assert run(chat_id="local:abc") is False
    assert len(writes) == 1


def test_assembled_chain_carries_the_anchor_message_id():
    """REGRESSION: `assemble_conversation_from_leaf` rebuilds each assistant from
    a whitelist, and originally dropped `id` — so every turn-start compaction
    produced a carrier with `anchor_message_id: None` and the envelope was never
    recorded. Verified against a real chat: an anchor written by a backend that
    HAD the capture still stored no envelope.

    This asserts the shape that assembly emits, and that the id does not reach
    the wire (it is expanded away by `_expand_assistant`)."""
    forwarded = {
        "role": "assistant",
        "id": "msg-a",
        "content_blocks": [
            _tool_calls_block("c1", "web_search", "{}", "r1"),
            _anchor("N1"),
            {"type": "text", "content": "done"},
        ],
    }
    out = blocks_to_api_messages(
        [{"role": "user", "content": "ask"}, forwarded]
    )
    assert capture_compaction_envelope(out)["message_id"] == "msg-a"
    assert not any("id" in m for m in strip_compaction_carry(out))


def test_compact_command_matches_only_the_bare_command():
    """A message that merely STARTS with /compact is a real instruction. Eating
    it because it shares a prefix is worse than requiring a bare command."""
    from open_webui.utils.compaction import is_compact_command

    assert is_compact_command("/compact")
    assert is_compact_command("  /compact  ")
    assert is_compact_command("/COMPACT")
    assert not is_compact_command("/compact the logs into one file")
    assert not is_compact_command("please /compact")
    assert not is_compact_command("compact")
    assert not is_compact_command("")
    assert not is_compact_command(None)


def test_carry_is_stripped_at_the_http_boundary():
    out = blocks_to_api_messages(_compacted_conversation())
    assert any(COMPACTION_CARRY_KEY in m for m in out)
    stripped = strip_compaction_carry(out)
    assert not any(COMPACTION_CARRY_KEY in m for m in stripped)
    # No-op lists are returned by identity so the hot path allocates nothing.
    plain = [{"role": "user", "content": "hi"}]
    assert strip_compaction_carry(plain) is plain


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_repeated_assembly_of_the_same_state_is_byte_identical():
    convo = _compacted_conversation()
    first = blocks_to_api_messages(convo)
    second = blocks_to_api_messages(_compacted_conversation())
    assert first == second


def test_gate_is_idempotent_over_its_own_output():
    """`blocks_to_api_messages` runs twice on the assemble → gate path. The
    second pass must not change a single byte."""
    once = blocks_to_api_messages(_compacted_conversation())
    twice = blocks_to_api_messages(once)
    assert once == twice


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


def test_escape_neutralizes_only_the_exact_closing_delimiters():
    text = "use </user_instructions> to close and </message> too"
    escaped = escape_envelope_delimiters(text)
    assert "</user_instructions>" not in escaped
    assert "</message>" not in escaped
    assert "&lt;/user_instructions>" in escaped


def test_escape_leaves_ordinary_code_byte_for_byte():
    code = "if (a < b && c > d) { return <Foo bar='x'/>; }"
    assert escape_envelope_delimiters(code) == code


def test_a_user_message_quoting_the_envelope_cannot_break_it():
    convo = [
        {"role": "user", "content": "what does </user_instructions> mean?"},
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{}", "r"),
                _anchor(),
                {"type": "text", "content": "ok"},
            ],
        },
    ]
    envelope = blocks_to_api_messages(convo)[0]["content"]
    assert envelope.count("</user_instructions>") == 1


def test_tool_args_with_a_quote_do_not_break_the_attribute():
    convo = [
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{\"q\":\"it's fine & good\"}", "r"),
                _anchor(),
                {"type": "text", "content": "ok"},
            ],
        }
    ]
    envelope = blocks_to_api_messages(convo)[0]["content"]
    line = next(ln for ln in envelope.splitlines() if ln.startswith("<call "))
    assert line.count("args='") == 1
    assert line.endswith("</call>")
    assert "&#39;" in line and "&amp;" in line


# ---------------------------------------------------------------------------
# 5. Trigger
# ---------------------------------------------------------------------------


def test_unknown_context_window_never_compacts():
    assert should_compact(999_999, None, 0.80) is False
    assert should_compact(999_999, 0, 0.80) is False


def test_threshold_boundary():
    assert should_compact(80_000, 100_000, 0.80) is True
    assert should_compact(79_999, 100_000, 0.80) is False


def test_trigger_ignores_missing_or_zero_usage():
    assert should_compact(None, 100_000, 0.80) is False
    assert should_compact(0, 100_000, 0.80) is False


def test_usage_total_tokens_prefers_the_reported_total():
    assert usage_total_tokens({"total_tokens": 1234}) == 1234
    # Some gateways omit the total; fall back to the sum of the two halves.
    assert usage_total_tokens({"prompt_tokens": 1000, "completion_tokens": 34}) == 1034
    assert usage_total_tokens({"prompt_tokens": 0, "completion_tokens": 0}) is None
    assert usage_total_tokens(None) is None


def test_anti_thrash_guard():
    """The trigger reads a number measured BEFORE the previous cut took effect,
    so the round right after a compaction still looks over-threshold."""
    just_compacted = [
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{}", "r"),
                _anchor(),
                {"type": "text", "content": ""},
            ],
        }
    ]
    assert has_uncompacted_span(just_compacted) is False

    a_round_later = [
        {
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{}", "r"),
                _anchor(),
                {"type": "text", "content": ""},
                _tool_calls_block("c2", "web_fetch", "{}", "r"),
                {"type": "text", "content": ""},
            ],
        }
    ]
    assert has_uncompacted_span(a_round_later) is True

    # A new user turn after the anchor's message is also new content.
    next_turn = [*just_compacted, {"role": "user", "content": "and now this"}]
    assert has_uncompacted_span(next_turn) is True

    # Never compacted at all.
    assert has_uncompacted_span([{"role": "user", "content": "hi"}]) is True


def test_read_back_tool_gating_marker():
    assert conversation_has_compacted_context(
        blocks_to_api_messages(_compacted_conversation())
    )
    assert not conversation_has_compacted_context(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]
    )


# ---------------------------------------------------------------------------
# 6. Summarizer validation
# ---------------------------------------------------------------------------


def test_extract_summary_takes_message_content():
    assert (
        extract_summary(
            {"choices": [{"finish_reason": "stop", "message": {"content": " done "}}]}
        )
        == "done"
    )


def test_extract_summary_refuses_reasoning_content():
    """Upstream #27604: they stored the truncated reasoning_content as somebody's
    conversation summary. Content-or-nothing."""
    try:
        extract_summary(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "",
                            "reasoning_content": "**Considering the summary**...",
                        },
                    }
                ]
            }
        )
    except CompactionError:
        pass
    else:
        raise AssertionError("empty content must not fall back to reasoning_content")


def test_extract_summary_refuses_a_truncated_response():
    try:
        extract_summary(
            {"choices": [{"finish_reason": "length", "message": {"content": "half a "}}]}
        )
    except CompactionError:
        pass
    else:
        raise AssertionError("finish_reason=length must be refused")


def test_extract_summary_refuses_junk_shapes():
    for bad in (None, {}, {"choices": []}, {"choices": [{"message": {}}]}):
        try:
            extract_summary(bad)
        except CompactionError:
            continue
        raise AssertionError(f"expected refusal for {bad!r}")


def test_summarizer_degrades_then_fails_visibly():
    """COMPACTION.md §6: degrade (strip the oldest tool bodies), then raise —
    never silently continue with full history."""
    seen: list[list] = []

    async def fake_generate(request, form_data, user, bypass_filter=False):
        seen.append(form_data["messages"])
        raise RuntimeError("context_length_exceeded")

    import open_webui.utils.chat as chat_module

    original = chat_module.generate_chat_completion
    chat_module.generate_chat_completion = fake_generate
    try:
        api_messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "A" * 5000}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c2"}]},
            {"role": "tool", "tool_call_id": "c2", "content": [{"type": "text", "text": "B" * 5000}]},
        ]
        try:
            asyncio.run(
                generate_compaction_narrative(
                    None, None, model_id="m", api_messages=api_messages
                )
            )
        except CompactionError:
            pass
        else:
            raise AssertionError("a failed summarizer must raise, not return")
    finally:
        chat_module.generate_chat_completion = original

    assert len(seen) == 2, "expected one full attempt and one degraded retry"
    first_tool_text = seen[0][2]["content"][0]["text"]
    second_tool_text = seen[1][2]["content"][0]["text"]
    assert first_tool_text.startswith("AAAA")
    assert "omitted from this summarization request" in second_tool_text
    # The NEWER tool body is kept — degradation is oldest-first.
    assert seen[1][4]["content"][0]["text"].startswith("BBBB")
    # The instruction is appended to the RAW conversation (cache-hit shape).
    assert seen[0][-1]["role"] == "user"
    assert "Retain maximum detail" in seen[0][-1]["content"]


# ---------------------------------------------------------------------------
# Turn-start orchestration
# ---------------------------------------------------------------------------


class _RecordingChats:
    def __init__(self):
        self.writes = []

    async def upsert_message_to_chat_by_id_and_message_id(
        self, chat_id, message_id, message, return_model=True
    ):
        self.writes.append((chat_id, message_id, message))
        return None


def _run_turn_start(chain, *, model, narrative="## Findings\nn", chats=None):
    """Drive `maybe_compact_at_turn_start` with the model call and the message
    store stubbed out. Returns (fired, chats_stub)."""
    import open_webui.models.chats as chats_module
    import open_webui.utils.chat as chat_module
    from open_webui.utils.compaction import maybe_compact_at_turn_start

    chats = chats or _RecordingChats()

    async def fake_generate(request, form_data, user, bypass_filter=False):
        return {
            "choices": [
                {"finish_reason": "stop", "message": {"content": narrative}}
            ]
        }

    original_chats = chats_module.Chats
    original_generate = chat_module.generate_chat_completion
    chats_module.Chats = chats
    chat_module.generate_chat_completion = fake_generate
    try:
        fired = asyncio.run(
            maybe_compact_at_turn_start(
                None,
                None,
                chat_id="chat-1",
                model=model,
                api_messages=[
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "go"},
                ],
                chain=chain,
            )
        )
    finally:
        chats_module.Chats = original_chats
        chat_module.generate_chat_completion = original_generate
    return fired, chats


def _chain_over_threshold():
    return [
        {"id": "u0", "role": "user", "content": "research this"},
        {
            "id": "a0",
            "role": "assistant",
            "content_blocks": [
                _tool_calls_block("c1", "web_search", "{}", "r1"),
                {"type": "text", "content": "answer"},
            ],
            "usage": {"prompt_tokens": 90_000, "completion_tokens": 1_000,
                      "total_tokens": 91_000},
        },
        {"id": "u1", "role": "user", "content": "and now this"},
    ]


def test_turn_start_compacts_and_writes_the_anchor_into_the_last_assistant():
    fired, chats = _run_turn_start(
        _chain_over_threshold(), model={"id": "m", "context_length": 100_000}
    )
    assert fired is True
    assert len(chats.writes) == 1
    chat_id, message_id, payload = chats.writes[0]
    assert (chat_id, message_id) == ("chat-1", "a0")
    types = [b["type"] for b in payload["content_blocks"]]
    # The target turn is FINISHED, so the anchor goes at its END — the divider
    # renders between that turn and the new user prompt, which is where the
    # compaction actually happened. It used to land after the last tool round and
    # therefore ABOVE the answer the user had just read.
    assert types == ["tool_calls", "text", COMPACTION_BLOCK_TYPE]
    assert payload["content_blocks"][-1]["narrative"].startswith("## Findings")
    assert payload["content_blocks"][-1]["tokens"] == 91_000
    assert payload["content_blocks"][-1]["context_length"] == 100_000


def test_turn_start_never_compacts_without_a_declared_context_window():
    """llama-swap declares no window; None must mean 'do nothing', never 'zero'."""
    fired, chats = _run_turn_start(_chain_over_threshold(), model={"id": "m"})
    assert fired is False
    assert chats.writes == []


def test_turn_start_does_not_compact_below_the_threshold():
    fired, chats = _run_turn_start(
        _chain_over_threshold(), model={"id": "m", "context_length": 1_000_000}
    )
    assert fired is False
    assert chats.writes == []


def test_turn_start_skips_a_regenerate_where_the_chain_ends_on_the_assistant():
    """No newer message after the assistant means the turn about to run will
    rewrite that message in place — anchoring into it would race the stream."""
    chain = _chain_over_threshold()[:-1]
    fired, chats = _run_turn_start(
        chain, model={"id": "m", "context_length": 100_000}
    )
    assert fired is False
    assert chats.writes == []


def test_turn_start_respects_the_anti_thrash_guard():
    chain = _chain_over_threshold()
    # Already compacted at the tail of the assistant, with nothing new since.
    chain[1]["content_blocks"] = [
        _tool_calls_block("c1", "web_search", "{}", "r1"),
        _anchor(),
    ]
    chain = chain[:2]
    fired, chats = _run_turn_start(
        chain, model={"id": "m", "context_length": 100_000}
    )
    assert fired is False
    assert chats.writes == []


def test_turn_start_skips_a_local_only_chat():
    import open_webui.utils.chat as chat_module
    from open_webui.utils.compaction import maybe_compact_at_turn_start

    called = []

    async def fake_generate(*a, **k):
        called.append(1)
        raise AssertionError("must not call the summarizer")

    original = chat_module.generate_chat_completion
    chat_module.generate_chat_completion = fake_generate
    try:
        fired = asyncio.run(
            maybe_compact_at_turn_start(
                None,
                None,
                chat_id="local:abc",
                model={"id": "m", "context_length": 100_000},
                api_messages=[],
                chain=_chain_over_threshold(),
            )
        )
    finally:
        chat_module.generate_chat_completion = original
    assert fired is False
    assert called == []


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


def test_envelope_matches_the_documented_shape():
    envelope = render_compacted_context(
        narrative="## Findings\nx",
        compacted_at="2026-07-30T15:22:41Z",
        instructions=["Research how X works", "actually focus on Y instead"],
        tool_index=[
            {
                "ref": "m3/c001",
                "name": "web_search",
                "args": '{"query":"openai compaction api"}',
                "descriptor": "10 results, 18.8KB",
            }
        ],
    )
    assert envelope.startswith(
        '<compacted_context compacted_at="2026-07-30T15:22:41Z">'
    )
    assert envelope.endswith("</compacted_context>")
    assert "<note>" in envelope and "read_tool_result(ref)" in envelope
    assert '<user_instructions verbatim="true">' in envelope
    assert "<message>Research how X works</message>" in envelope
    assert (
        "<call ref=\"m3/c001\" name=\"web_search\" "
        "args='{\"query\":\"openai compaction api\"}'>10 results, 18.8KB</call>"
    ) in envelope
    # Narrative LAST, so Current State / Next Steps sit next to the live turns.
    assert envelope.index("<narrative>") > envelope.index("</tool_calls>")


def test_envelope_omits_empty_sections():
    envelope = render_compacted_context(
        narrative="n", compacted_at="t", instructions=[], tool_index=[]
    )
    assert "<user_instructions" not in envelope
    assert "<tool_calls>" not in envelope
    assert "<narrative>" in envelope
