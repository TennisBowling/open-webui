"""Unit tests for `utils.messages.blocks_to_api_messages` and `_expand_assistant`.

The invariant under test: every `rs_*` reasoning id appears in at most one
assistant message in the output. OpenAI Responses upstreams reject conversation
histories with duplicate item ids ("Duplicate item found with id rs_<id>") and
OpenRouter surfaces that as a generic 500. The tests below cover every
production permutation that can produce a duplicate before the fix.
"""

from open_webui.utils.messages import _expand_assistant, blocks_to_api_messages


def _enc(rs_id, data="DATA"):
    return {
        "type": "reasoning.encrypted",
        "id": rs_id,
        "data": data,
        "format": "openai-responses-v1",
        "index": 0,
    }


def _summary(text="thinking", index=0):
    return {
        "type": "reasoning.summary",
        "summary": text,
        "format": "openai-responses-v1",
        "index": index,
    }


def _tool_calls_block(call_id="c1", reasoning=None, result="ok"):
    block = {
        "type": "tool_calls",
        "content": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "x", "arguments": "{}"},
            }
        ],
        "results": [{"tool_call_id": call_id, "content": result}],
    }
    if reasoning is not None:
        block["reasoning_details"] = reasoning
    return block


def _text_block(content):
    return {"type": "text", "content": content}


def _ids(message):
    return [
        d.get("id")
        for d in (message.get("reasoning_details") or [])
        if d.get("id")
    ]


def _all_ids(messages):
    ids = []
    for m in messages:
        if m.get("role") == "assistant":
            ids.extend(_ids(m))
    return ids


def _assert_no_dups(messages):
    ids = _all_ids(messages)
    assert len(ids) == len(set(ids)), f"duplicate rs_* ids: {ids}"


# -- Trigger-A scenarios: per-round shorter than emissions ---------------------


def test_round2_emitted_no_reasoning_does_not_duplicate_round1():
    """User's exact reported bug. Pre-fix: msg#5 gets rs_A again via flat
    fallback → 500. Post-fix: dedup catches it → msg#5 has no reasoning."""
    out = blocks_to_api_messages(
        [
            {"role": "user", "content": "ask"},
            {
                "role": "assistant",
                "content_blocks": [
                    _tool_calls_block(reasoning=[_enc("rs_A")]),
                    _text_block("Done."),
                ],
                # Pre-fix persisted shape: round 2 had no reasoning, the
                # `if reasoning_details:` guard skipped it, so per_round
                # length is 1 even though there are two emissions.
                "reasoning_details_per_round": [[_enc("rs_A")]],
                "reasoning_details": [_enc("rs_A")],
            },
            {"role": "user", "content": "thanks"},
        ]
    )
    _assert_no_dups(out)
    # msg#3 keeps rs_A from tool_calls_block.reasoning_details
    assert _ids(out[1]) == ["rs_A"]
    # final-text emission gets nothing (per_round[1] missing, no legacy fallback
    # consumed for non-zero emission)
    assert out[3]["role"] == "assistant"
    assert _ids(out[3]) == []


def test_round2_with_empty_per_round_entry_after_fix():
    """Post-fix persisted shape: round 2 explicitly stored as []."""
    out = blocks_to_api_messages(
        [
            {"role": "user", "content": "ask"},
            {
                "role": "assistant",
                "content_blocks": [
                    _tool_calls_block(reasoning=[_enc("rs_A")]),
                    _text_block("Done."),
                ],
                "reasoning_details_per_round": [[_enc("rs_A")], []],
                "reasoning_details": [_enc("rs_A")],
            },
        ]
    )
    _assert_no_dups(out)
    assert _ids(out[1]) == ["rs_A"]


def test_round2_with_its_own_reasoning_keeps_both():
    """Happy path: each round has its own unique reasoning."""
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    _tool_calls_block(reasoning=[_enc("rs_A")]),
                    _text_block("Done."),
                ],
                "reasoning_details_per_round": [[_enc("rs_A")], [_enc("rs_B")]],
            },
        ]
    )
    _assert_no_dups(out)
    assert _ids(out[0]) == ["rs_A"]
    assert _ids(out[2]) == ["rs_B"]


# -- Trigger-B scenarios: legacy chats with no per_round -----------------------


def test_legacy_flat_only_attaches_to_first_emission_then_dedups():
    """Pre-per_round chats only persisted the flat `reasoning_details`. Legacy
    fallback attaches it to emission 0 (the tool_calls emission), dedup keeps
    its ids off subsequent emissions."""
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    _tool_calls_block(),  # no reasoning_details on the block
                    _text_block("Done."),
                ],
                # per_round absent entirely
                "reasoning_details": [_enc("rs_A"), _enc("rs_B", data="BBB")],
            },
        ]
    )
    _assert_no_dups(out)
    # Both items attach to the first emission (legacy chats had no way to know
    # which round each item came from).
    assert _ids(out[0]) == ["rs_A", "rs_B"]
    # Trailing text gets nothing.
    assert _ids(out[2]) == []


# -- Trigger via legacy passthrough (no content_blocks at all) ----------------


def test_legacy_passthrough_duplicates_are_stripped():
    """A chat persisted before the content_blocks migration may have two
    assistant messages that legitimately carried the same `rs_*` (e.g. because
    a prior version of open-webui copied flat reasoning onto both). The
    passthrough-branch dedup catches them."""
    out = blocks_to_api_messages(
        [
            {"role": "user", "content": "u1"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ],
                "reasoning_details": [_enc("rs_A")],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "r"},
            {
                "role": "assistant",
                "content": "Done.",
                "reasoning_details": [_enc("rs_A")],  # duplicate
            },
            {"role": "user", "content": "u2"},
        ]
    )
    _assert_no_dups(out)
    assert _ids(out[1]) == ["rs_A"]
    # second assistant message gets the dup stripped → reasoning_details
    # absent or empty
    assert _ids(out[3]) == []


# -- Multi-turn / multi-tool-round --------------------------------------------


def test_three_turn_conversation_keeps_each_turns_reasoning():
    out = blocks_to_api_messages(
        [
            {"role": "user", "content": "u1"},
            {
                "role": "assistant",
                "content_blocks": [
                    _tool_calls_block(reasoning=[_enc("rs_A")]),
                    _text_block("Done."),
                ],
                "reasoning_details_per_round": [[_enc("rs_A")], []],
            },
            {"role": "user", "content": "u2"},
            {
                "role": "assistant",
                "content_blocks": [_text_block("Sure.")],
                "reasoning_details_per_round": [[_enc("rs_B")]],
            },
            {"role": "user", "content": "u3"},
        ]
    )
    _assert_no_dups(out)
    assert _ids(out[1]) == ["rs_A"]
    # turn 2's text emission carries rs_B
    assistant_turns = [m for m in out if m["role"] == "assistant"]
    assert _ids(assistant_turns[-1]) == ["rs_B"]


def test_multi_tool_round_each_emission_keeps_its_own_reasoning():
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    _tool_calls_block(call_id="c1", reasoning=[_enc("rs_A")]),
                    _tool_calls_block(call_id="c2", reasoning=[_enc("rs_B")]),
                    _text_block("Done."),
                ],
                "reasoning_details_per_round": [
                    [_enc("rs_A")],
                    [_enc("rs_B")],
                    [_enc("rs_C")],
                ],
            },
        ]
    )
    _assert_no_dups(out)
    asst = [m for m in out if m["role"] == "assistant"]
    assert _ids(asst[0]) == ["rs_A"]
    assert _ids(asst[1]) == ["rs_B"]
    assert _ids(asst[2]) == ["rs_C"]


# -- Defensive: cross-message duplicates that *shouldn't* happen but do --------


def test_global_seen_ids_strips_cross_turn_duplicates():
    """Defensive: even if two distinct assistant turns somehow carry the same
    `rs_*` id (e.g. a corrupted persisted state or a buggy older save path),
    the global dedup keeps it from reaching upstream."""
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [_text_block("Hi.")],
                "reasoning_details_per_round": [[_enc("rs_A")]],
            },
            {"role": "user", "content": "u2"},
            {
                "role": "assistant",
                "content_blocks": [_text_block("Yo.")],
                "reasoning_details_per_round": [[_enc("rs_A")]],  # collides
            },
        ]
    )
    _assert_no_dups(out)


# -- Cancel paths --------------------------------------------------------------


def test_cancel_mid_tool_keeps_tool_call_emission_only():
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    {
                        "type": "tool_calls",
                        "content": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "x", "arguments": "{}"},
                            }
                        ],
                        "reasoning_details": [_enc("rs_A")],
                        # no results — cancelled before tool ran
                    },
                ],
                "reasoning_details_per_round": [[_enc("rs_A")]],
            },
        ]
    )
    _assert_no_dups(out)
    assert out[0]["role"] == "assistant"
    assert _ids(out[0]) == ["rs_A"]
    # The converter guarantees a non-empty placeholder tool message for every
    # assistant tool_call so OpenAI-compatible upstreams accept the replayed
    # history even when cancellation happened before a real tool result landed.
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1"
    assert tool_msg["content"] == [
        {"type": "text", "text": "[No output was produced for this tool call.]"}
    ]


def test_cancel_before_any_reasoning_emits_clean_message():
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [_text_block("Partial...")],
                "reasoning_details_per_round": [],
                "reasoning_details": [],
            },
        ]
    )
    _assert_no_dups(out)
    assert out[0]["content"] == "Partial..."
    assert "reasoning_details" not in out[0]


# -- Summary items (no id) ---------------------------------------------------


def test_summary_only_items_are_never_deduped():
    """Summary items have no `id`. They can legitimately appear in multiple
    messages; the dedup must not touch them."""
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [_text_block("Hi.")],
                "reasoning_details_per_round": [[_summary("first")]],
            },
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content_blocks": [_text_block("Yo.")],
                "reasoning_details_per_round": [[_summary("second")]],
            },
        ]
    )
    # No id-based dups (both summaries have no id, so trivially no dup)
    _assert_no_dups(out)
    asst = [m for m in out if m["role"] == "assistant"]
    assert asst[0]["reasoning_details"][0]["summary"] == "first"
    assert asst[1]["reasoning_details"][0]["summary"] == "second"


# -- Live tool-call loop in-flight assistant ----------------------------------


def test_live_loop_in_flight_assistant_does_not_emit_empty_trailer():
    """The live tool loop appends an empty text_block placeholder to
    content_blocks and recurses into `generate_chat_completion` with per_round
    that still only has round 1's reasoning. The empty trailer must not
    materialise as a malformed assistant message — it has no content, no
    tool_calls, and no reasoning to carry."""
    out = _expand_assistant(
        content_blocks=[
            _tool_calls_block(reasoning=[_enc("rs_A")]),
            _text_block(""),
        ],
        reasoning_details_per_round=[[_enc("rs_A")]],
    )
    # one assistant (the tool_calls) and one tool result, no trailing emission
    assert [m["role"] for m in out] == ["assistant", "tool"]
    _assert_no_dups(out)


# -- Reasoning-only trailing emission (legacy edge) ---------------------------


def test_empty_content_blocks_with_per_round_reasoning_emits_trailing_message():
    """Legacy reasoning-only saves: content_blocks ended up empty but the
    saved per_round still has reasoning. Preserve it as a content="" message."""
    out = _expand_assistant(
        content_blocks=[],
        reasoning_details_per_round=[[_enc("rs_A")]],
    )
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == ""
    assert _ids(out[0]) == ["rs_A"]


# -- Internal carriers stripped on output -------------------------------------


def test_content_blocks_and_per_round_never_leak_to_upstream():
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [_text_block("Hi.")],
                "reasoning_details_per_round": [[_enc("rs_A")]],
                "reasoning_details": [_enc("rs_A")],
            },
        ]
    )
    for m in out:
        assert "content_blocks" not in m
        assert "reasoning_details_per_round" not in m


# -- Tool result content shape ------------------------------------------------


def test_tool_result_is_text_part_list_for_cache_control_compatibility():
    """Tool result content travels as `[{type: "text", text: "..."}]`, not a
    bare string, so the cache_control transform applied to the last message
    during the live loop produces a shape-stable result between live and
    replay."""
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    _tool_calls_block(call_id="c1", result="weather: sunny"),
                    _text_block("Done."),
                ],
                "reasoning_details_per_round": [[], []],
            },
        ]
    )
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1"
    assert tool_msg["content"] == [{"type": "text", "text": "weather: sunny"}]


def test_view_image_tool_result_adds_synthetic_user_image_message():
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    {
                        "type": "tool_calls",
                        "content": [
                            {
                                "id": "call_img",
                                "type": "function",
                                "function": {
                                    "name": "view_image",
                                    "arguments": '{"source":"https://example.com/chart.png"}',
                                },
                            }
                        ],
                        "results": [
                            {
                                "tool_call_id": "call_img",
                                "content": "Image attached for visual inspection: chart.png",
                                "vision_attachments": [
                                    {
                                        "url": "/api/v1/files/file-1/content",
                                        "detail": "high",
                                        "source": "https://example.com/chart.png",
                                        "file_id": "file-1",
                                        "mime_type": "image/png",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    )

    assert [m["role"] for m in out] == ["assistant", "tool", "user"]
    assert out[1]["content"] == [
        {"type": "text", "text": "Image attached for visual inspection: chart.png"}
    ]
    assert out[2]["name"] == "view_image_tool"
    assert out[2]["content"][0]["type"] == "text"
    assert out[2]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "/api/v1/files/file-1/content", "detail": "high"},
    }


def test_view_image_multiple_tool_calls_emit_all_tool_outputs_before_image_message():
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    {
                        "type": "tool_calls",
                        "content": [
                            {
                                "id": "call_fetch",
                                "type": "function",
                                "function": {"name": "web_fetch", "arguments": "{}"},
                            },
                            {
                                "id": "call_img",
                                "type": "function",
                                "function": {"name": "view_image", "arguments": "{}"},
                            },
                        ],
                        "results": [
                            {"tool_call_id": "call_fetch", "content": "page body"},
                            {
                                "tool_call_id": "call_img",
                                "content": "Image attached for visual inspection: plot.png",
                                "vision_attachments": [
                                    {
                                        "url": "/api/v1/files/file-2/content",
                                        "detail": "auto",
                                    }
                                ],
                            },
                        ],
                    },
                    _text_block("The plot shows growth."),
                ],
            }
        ]
    )

    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "user", "assistant"]
    assert out[1]["tool_call_id"] == "call_fetch"
    assert out[2]["tool_call_id"] == "call_img"
    assert out[3]["content"][1]["image_url"]["url"] == "/api/v1/files/file-2/content"
    assert out[4]["content"] == "The plot shows growth."


def test_view_image_attachment_hydrates_from_tool_result_body_store():
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    {
                        "type": "tool_calls",
                        "content": [
                            {
                                "id": "call_img",
                                "type": "function",
                                "function": {"name": "view_image", "arguments": "{}"},
                            }
                        ],
                        "results": [
                            {
                                "tool_call_id": "call_img",
                                "result_ref": "call_img",
                                "result_lazy": True,
                                "content": "",
                            }
                        ],
                    }
                ],
                "tool_result_bodies": {
                    "call_img": {
                        "tool_call_id": "call_img",
                        "content": "Image attached for visual inspection: hydrated.png",
                        "vision_attachments": [
                            {"url": "/api/v1/files/file-3/content", "detail": "low"}
                        ],
                    }
                },
            }
        ]
    )

    assert [m["role"] for m in out] == ["assistant", "tool", "user"]
    assert out[2]["content"][1]["image_url"] == {
        "url": "/api/v1/files/file-3/content",
        "detail": "low",
    }


# -- Non-reasoning models: dedup is a no-op -----------------------------------


def test_non_reasoning_conversation_passes_through_unchanged():
    """For non-reasoning model chats, no `reasoning_details` are ever present,
    so the dedup pass is a no-op."""
    out = blocks_to_api_messages(
        [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "bye"},
        ]
    )
    assert len(out) == 4
    for m in out:
        assert "reasoning_details" not in m


def test_tool_result_ref_hydrates_from_message_body_store():
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    {
                        "type": "tool_calls",
                        "content": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "web_fetch", "arguments": "{}"},
                            }
                        ],
                        "results": [
                            {
                                "tool_call_id": "call_1",
                                "result_ref": "call_1",
                                "result_lazy": True,
                                "content": "",
                            }
                        ],
                    }
                ],
                "tool_result_bodies": {
                    "call_1": {"tool_call_id": "call_1", "content": "full fetched body"}
                },
            }
        ]
    )

    assert out[1]["role"] == "tool"
    assert out[1]["content"] == [{"type": "text", "text": "full fetched body"}]


def test_tool_result_ref_without_body_fails_loudly():
    import pytest

    with pytest.raises(ValueError):
        blocks_to_api_messages(
            [
                {
                    "role": "assistant",
                    "content_blocks": [
                        {
                            "type": "tool_calls",
                            "content": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "web_fetch", "arguments": "{}"},
                                }
                            ],
                            "results": [
                                {
                                    "tool_call_id": "call_1",
                                    "result_ref": "call_1",
                                    "result_lazy": True,
                                    "content": "",
                                }
                            ],
                        }
                    ],
                }
            ]
        )


# -- A2: copy-on-write hydration does not mutate caller input -----------------


def test_hydrate_does_not_mutate_caller_blocks_or_bodies():
    """`_hydrate_tool_result_refs` (and therefore `blocks_to_api_messages`)
    merges out-of-line tool bodies into the result dicts for conversion, but
    must NEVER mutate the caller's `content_blocks` or the `bodies` store. The
    old code deep-copied everything up front to guarantee this; the new
    copy-on-write path only clones the blocks/results it actually touches, so
    this property needs an explicit guard."""
    from open_webui.utils.messages import _hydrate_tool_result_refs

    result = {
        "tool_call_id": "call_1",
        "result_ref": "call_1",
        "result_lazy": True,
        "content": "",
    }
    tool_block = {
        "type": "tool_calls",
        "content": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "web_fetch", "arguments": "{}"},
            }
        ],
        "results": [result],
    }
    text_block = {"type": "text", "content": "Done."}
    blocks = [tool_block, text_block]
    bodies = {"call_1": {"tool_call_id": "call_1", "content": "full fetched body"}}

    hydrated = _hydrate_tool_result_refs(blocks, bodies)

    # The merged output carries the body content.
    hydrated_tool = next(b for b in hydrated if b.get("type") == "tool_calls")
    assert hydrated_tool["results"][0]["content"] == "full fetched body"

    # Caller's original structures are untouched.
    assert result["content"] == ""  # the original result dict not mutated
    assert tool_block["results"][0] is result  # original list entry unchanged
    assert bodies["call_1"]["content"] == "full fetched body"  # store unchanged
    assert "result_ref" not in bodies["call_1"]

    # Untouched blocks pass through by reference (no needless cloning).
    assert hydrated[1] is text_block


def test_hydrate_passes_blocks_through_by_reference_when_nothing_to_merge():
    """When no result needs a body merged in, every block is returned by
    reference — the COW path must not clone gratuitously."""
    from open_webui.utils.messages import _hydrate_tool_result_refs

    tool_block = {
        "type": "tool_calls",
        "content": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "x", "arguments": "{}"},
            }
        ],
        "results": [{"tool_call_id": "c1", "content": "already inline"}],
    }
    text_block = {"type": "text", "content": "Hi."}
    blocks = [tool_block, text_block]

    hydrated = _hydrate_tool_result_refs(blocks, {})

    assert hydrated[0] is tool_block
    assert hydrated[1] is text_block



# -- user_steer (mid-task steering) expansion ---------------------------------


def _steer_block(content):
    return {"type": "user_steer", "content": content}


def test_user_steer_emits_real_user_turn_between_assistant_emissions():
    """A `user_steer` block (the user interjected mid-task) must expand into a
    real {"role":"user"} message, positioned in conversation order:
    assistant(tool) → tool result → assistant text → USER(steer) → assistant."""
    out = _expand_assistant(
        [
            _tool_calls_block(call_id="c1", result="r1"),
            _text_block("Partial progress."),
            _steer_block("actually, focus on the tests"),
            _text_block("Okay, refocusing."),
        ]
    )
    roles = [(m["role"], m.get("content")) for m in out]
    # assistant(tool_calls) , tool , assistant("Partial progress.") , user(steer) , assistant("Okay...")
    assert ("user", "actually, focus on the tests") in roles
    # Order: the steer must come AFTER the partial assistant text and BEFORE the
    # final assistant text.
    steer_idx = next(
        i for i, m in enumerate(out)
        if m["role"] == "user" and m.get("content") == "actually, focus on the tests"
    )
    partial_idx = next(
        i for i, m in enumerate(out)
        if m["role"] == "assistant" and m.get("content") == "Partial progress."
    )
    final_idx = next(
        i for i, m in enumerate(out)
        if m["role"] == "assistant" and m.get("content") == "Okay, refocusing."
    )
    assert partial_idx < steer_idx < final_idx


def test_user_steer_does_not_consume_reasoning_slot():
    """A steer is not a tool round: it must not advance emission_index, so the
    per-round reasoning still aligns to the tool_calls emissions, not the steer."""
    out = _expand_assistant(
        [
            _steer_block("steer before any round"),
            _tool_calls_block(call_id="c1", result="r1"),
            _text_block("done"),
        ],
        reasoning_details_per_round=[[_enc("rs_round0")]],
    )
    _assert_no_dups(out)
    # The single per-round reasoning entry must land on the tool_calls emission
    # (emission_index 0), NOT be skipped by the steer.
    assistant_with_reasoning = [m for m in out if _ids(m)]
    assert len(assistant_with_reasoning) == 1
    assert _ids(assistant_with_reasoning[0]) == ["rs_round0"]
    # And the steer is still a real user turn.
    assert any(
        m["role"] == "user" and m.get("content") == "steer before any round"
        for m in out
    )


def test_multiple_user_steers_each_become_user_turns_in_order():
    out = _expand_assistant(
        [
            _tool_calls_block(call_id="c1", result="r1"),
            _steer_block("first steer"),
            _steer_block("second steer"),
            _text_block("final"),
        ]
    )
    user_contents = [m.get("content") for m in out if m["role"] == "user"]
    assert user_contents == ["first steer", "second steer"]


def test_empty_user_steer_is_dropped():
    out = _expand_assistant(
        [
            _text_block("hi"),
            _steer_block("   "),
            _text_block("bye"),
        ]
    )
    assert all(m["role"] != "user" for m in out)


def test_blocks_to_api_messages_with_user_steer_full_path():
    """End-to-end through the public gate: a saved assistant message carrying a
    user_steer block expands with the user turn inline."""
    out = blocks_to_api_messages(
        [
            {"role": "user", "content": "start"},
            {
                "role": "assistant",
                "content_blocks": [
                    _tool_calls_block(call_id="c1", result="r1"),
                    _text_block("working"),
                    _steer_block("steer me"),
                    _text_block("steered"),
                ],
            },
        ]
    )
    assert {"role": "user", "content": "steer me"} in out
    # content_blocks is an internal carrier — never leaks upstream.
    assert all("content_blocks" not in m for m in out)


# -- Subagent result recovery from the subagent_runs mirror ------------------


def _subagent_tool_block(call_id, name="subagent_launch", results=None):
    return {
        "type": "tool_calls",
        "content": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
        ],
        **({"results": results} if results is not None else {}),
    }


def test_subagent_result_recovers_from_runs_mirror_by_subagent_id():
    # A launch result whose persisted content is empty but carries the subagent_id;
    # the durable subagent_runs has the real answer.
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    _subagent_tool_block(
                        "c1",
                        results=[{"tool_call_id": "c1", "content": "", "subagent_id": "sa1"}],
                    ),
                ],
                "subagent_runs": {
                    "sa1": {
                        "subagent_id": "sa1",
                        "status": "done",
                        "final_text": "the real subagent answer",
                    }
                },
            },
        ]
    )
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1"
    assert tool_msg["content"] == [
        {"type": "text", "text": "the real subagent answer"}
    ]


def test_subagent_result_recovers_by_tool_call_id_when_result_missing():
    # The result row is ENTIRELY missing (no subagent_id available on a result),
    # so only the tool_call_id key can recover it.
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    _subagent_tool_block("c2", results=[]),
                ],
                "subagent_runs": {
                    "sa2": {
                        "subagent_id": "sa2",
                        "tool_call_id": "c2",
                        "status": "done",
                        "final_text": "recovered via tool_call_id",
                    }
                },
            },
        ]
    )
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["content"] == [
        {"type": "text", "text": "recovered via tool_call_id"}
    ]


def test_subagent_continue_recovers_fresh_turn_not_stale_launch():
    # Same subagent_id, two entries: the launch (keyed by sa) and the continue
    # (keyed by sa#cont). The continue's tool_call_id must pull the FRESH answer.
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    _subagent_tool_block(
                        "cont_call",
                        name="subagent_continue",
                        results=[{"tool_call_id": "cont_call", "content": "", "subagent_id": "sa3"}],
                    ),
                ],
                "subagent_runs": {
                    "sa3": {
                        "subagent_id": "sa3",
                        "tool_call_id": "launch_call",
                        "status": "done",
                        "final_text": "STALE launch answer",
                    },
                    "sa3#cont_call": {
                        "subagent_id": "sa3",
                        "tool_call_id": "cont_call",
                        "continuation": True,
                        "status": "done",
                        "final_text": "FRESH continue answer",
                    },
                },
            },
        ]
    )
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["content"] == [
        {"type": "text", "text": "FRESH continue answer"}
    ]


def test_subagent_error_status_still_falls_through_to_placeholder():
    # A run that finished with status != done (or empty final_text) must NOT be
    # used — the empty result falls through to the [No output...] placeholder.
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    _subagent_tool_block(
                        "c4",
                        results=[{"tool_call_id": "c4", "content": "", "subagent_id": "sa4"}],
                    ),
                ],
                "subagent_runs": {
                    "sa4": {
                        "subagent_id": "sa4",
                        "status": "error",
                        "final_text": "",
                    }
                },
            },
        ]
    )
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["content"] == [
        {"type": "text", "text": "[No output was produced for this tool call.]"}
    ]


def test_subagent_runs_absent_keeps_existing_placeholder_behavior():
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    _subagent_tool_block(
                        "c5",
                        results=[{"tool_call_id": "c5", "content": "", "subagent_id": "sa5"}],
                    ),
                ],
            },
        ]
    )
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["content"] == [
        {"type": "text", "text": "[No output was produced for this tool call.]"}
    ]


def test_subagent_nonempty_result_is_not_overridden_by_mirror():
    # If the persisted result already has content, the mirror must not clobber it.
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    _subagent_tool_block(
                        "c6",
                        results=[{"tool_call_id": "c6", "content": "already here", "subagent_id": "sa6"}],
                    ),
                ],
                "subagent_runs": {
                    "sa6": {
                        "subagent_id": "sa6",
                        "status": "done",
                        "final_text": "different mirror text",
                    }
                },
            },
        ]
    )
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["content"] == [{"type": "text", "text": "already here"}]


def test_subagent_reconciliation_does_not_perturb_reasoning_dedup():
    out = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    {
                        "type": "tool_calls",
                        "content": [
                            {
                                "id": "c7",
                                "type": "function",
                                "function": {"name": "subagent_launch", "arguments": "{}"},
                            }
                        ],
                        "reasoning_details": [_enc("rs_S")],
                        "results": [{"tool_call_id": "c7", "content": "", "subagent_id": "sa7"}],
                    },
                ],
                "reasoning_details_per_round": [[_enc("rs_S")]],
                "subagent_runs": {
                    "sa7": {"subagent_id": "sa7", "status": "done", "final_text": "ans"},
                },
            },
        ]
    )
    _assert_no_dups(out)
    assert _ids(out[0]) == ["rs_S"]
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert tool_msg["content"] == [{"type": "text", "text": "ans"}]
