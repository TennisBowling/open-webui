"""Tests for the loss-proofing of large tool-result bodies (stream protocol v2.1).

Background: v2.1 slims large web_search / web_fetch results out of ``content_blocks``
(the slim result keeps ``content:"" + result_ref + summary`` and the full body is
offloaded to a RAM store + the persisted ``tool_result_bodies`` map). A production
incident wiped the RAM store mid-generation; the next checkpoint then REPLACED the
DB's ``tool_result_bodies`` with the near-empty map (shallow ``{**existing,
**incoming}``), and outbound conversion RAISED ``ValueError("Missing tool result
body for ref ...")``, killing the whole turn.

These tests pin the four fix layers that make the body loss-proof:

* Layer 3 — ``upsert_message_to_chat_by_id_and_message_id`` union/never-shrink
  persistence + opt-in prune (``prune_tool_result_bodies``).
* Layer 2 — ``Chats.merge_message_tool_result_bodies`` per-round write-through.
* Layer 4 — ``_hydrate_tool_result_refs`` degrades a lost body to a descriptive
  placeholder instead of raising; ``blocks_to_api_messages`` survives a row shaped
  like the corrupted production message.
* Layer 1 — the generation-local body ledger's layering (tested through
  ``_merge_tool_result_body_maps`` directly, since ``_current_tool_result_bodies``
  is a response_handler closure).

Environment notes (see the module MEMORY): pytest's async plugins are NOT installed,
so async coroutines are driven with ``asyncio.run`` via a small sync wrapper; the
model-layer tests need a real Postgres and self-skip when one isn't configured.
"""

import uuid

import pytest

from test.util.db import configure_test_database

configure_test_database(required=True)

import asyncio  # noqa: E402

from open_webui.models.chats import Chats, ChatForm  # noqa: E402
from open_webui.utils.messages import (  # noqa: E402
    _hydrate_tool_result_refs,
    _lost_body_placeholder,
    blocks_to_api_messages,
)
from open_webui.utils.middleware import _merge_tool_result_body_maps  # noqa: E402
from open_webui.socket.main import (  # noqa: E402
    clear_tool_result_bodies,
    get_tool_result_bodies,
    set_tool_result_body,
)


# ---------------------------------------------------------------------------
# Layer 4 — self-healing hydrate (pure functions, no DB)
# ---------------------------------------------------------------------------


def _tool_calls_block(call_id, name="web_search", results=None):
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


def test_hydrate_missing_web_body_degrades_to_placeholder():
    # A web ref whose out-of-line body is gone must NOT raise; it degrades to a
    # non-empty placeholder that names the kind/size.
    blocks = [
        _tool_calls_block(
            "tool_web_search_1",
            results=[
                {
                    "tool_call_id": "tool_web_search_1",
                    "content": "",
                    "result_ref": "tool_web_search_1",
                    "result_lazy": True,
                    "size": 18831,
                    "summary": {"kind": "web_search", "size": 18831, "result_count": 10},
                }
            ],
        )
    ]
    # bodies map is empty (the store was wiped and nothing persisted).
    out = _hydrate_tool_result_refs(blocks, {})
    result = out[0]["results"][0]
    assert result["content"]  # non-empty
    assert "web_search" in result["content"]
    assert "18831" in result["content"]
    assert "10 results" in result["content"]
    # Input was not mutated (copy-on-write contract).
    assert blocks[0]["results"][0]["content"] == ""


def test_hydrate_present_body_still_merges():
    blocks = [
        _tool_calls_block(
            "tool_web_fetch_2",
            name="web_fetch",
            results=[
                {
                    "tool_call_id": "tool_web_fetch_2",
                    "content": "",
                    "result_ref": "tool_web_fetch_2",
                    "result_lazy": True,
                    "summary": {"kind": "web_fetch", "size": 40, "page_count": 1},
                }
            ],
        )
    ]
    bodies = {"tool_web_fetch_2": {"content": "THE FULL FETCHED PAGE BODY"}}
    out = _hydrate_tool_result_refs(blocks, bodies)
    assert out[0]["results"][0]["content"] == "THE FULL FETCHED PAGE BODY"


def test_carry_bodies_prunes_to_kept_refs_and_prefers_provided():
    # Server-side body carry for rewind/retry siblings (the
    # `copy_tool_result_bodies_from` append_message field): only bodies whose
    # refs survive in the new message's content_blocks are copied, and bodies
    # explicitly present on the new message win per-key.
    from open_webui.models.chats import carry_tool_result_bodies_from_source

    source = {
        "tool_result_bodies": {
            "ref_kept": {"content": "KEPT BODY"},
            "ref_cut": {"content": "CUT BODY"},
            "ref_overridden": {"content": "SOURCE VERSION"},
        }
    }
    new_msg = {
        "content_blocks": [
            _tool_calls_block(
                "call_1",
                results=[
                    {"tool_call_id": "call_1", "content": "", "result_ref": "ref_kept"},
                    {
                        "tool_call_id": "call_2",
                        "content": "",
                        "result_ref": "ref_overridden",
                    },
                ],
            ),
            {"type": "user_steer", "content": "go this way instead"},
            {"type": "text", "content": ""},
        ],
        "tool_result_bodies": {"ref_overridden": {"content": "OP VERSION"}},
    }
    carry_tool_result_bodies_from_source(source, new_msg)
    assert new_msg["tool_result_bodies"] == {
        "ref_kept": {"content": "KEPT BODY"},
        "ref_overridden": {"content": "OP VERSION"},
    }
    # Source map untouched.
    assert set(source["tool_result_bodies"]) == {"ref_kept", "ref_cut", "ref_overridden"}


def test_carry_bodies_noops_without_source_or_refs():
    from open_webui.models.chats import carry_tool_result_bodies_from_source

    # Missing / body-less source → new message untouched.
    new_msg = {"content_blocks": [_tool_calls_block("c1")]}
    carry_tool_result_bodies_from_source(None, new_msg)
    carry_tool_result_bodies_from_source({}, new_msg)
    assert "tool_result_bodies" not in new_msg

    # Source has bodies but the kept blocks reference none of them → no key added.
    carry_tool_result_bodies_from_source(
        {"tool_result_bodies": {"ref_x": {"content": "X"}}},
        {"content_blocks": [{"type": "text", "content": "final answer"}]},
    )
    assert "tool_result_bodies" not in new_msg


def test_hydrate_subagent_missing_body_passes_through_empty():
    # A subagent ref whose body is missing keeps its recovery path (subagent_runs
    # in _expand_assistant) — content stays empty, unchanged behavior, no raise.
    blocks = [
        _tool_calls_block(
            "sa_call_1",
            name="subagent_launch",
            results=[
                {
                    "tool_call_id": "sa_call_1",
                    "content": "",
                    "result_ref": "sa_call_1",
                    "subagent_id": "sa1",
                }
            ],
        )
    ]
    out = _hydrate_tool_result_refs(blocks, {})
    assert out[0]["results"][0].get("content", "") == ""
    assert out[0]["results"][0].get("subagent_id") == "sa1"


def test_lost_body_placeholder_defensive_summary_shapes():
    # summary may be str / None / absent — never crash, always a non-empty message.
    assert _lost_body_placeholder({"summary": None, "size": 5}).strip()
    assert "web_fetch" in _lost_body_placeholder({"summary": "web_fetch"})
    assert _lost_body_placeholder({}).strip()  # nothing to describe still yields text


def test_blocks_to_api_messages_survives_corrupted_production_row():
    # Reproduce the corrupted production message: 6 tool_calls rounds, each with a
    # slimmed result (result_ref + content:"" + summary), but tool_result_bodies
    # holds ONLY the last ref. blocks_to_api_messages must return without raising,
    # and every emitted tool message must have non-empty content.
    content_blocks = []
    for i in range(6):
        cid = f"tool_web_search_{i}"
        content_blocks.append(
            _tool_calls_block(
                cid,
                results=[
                    {
                        "tool_call_id": cid,
                        "content": "",
                        "result_ref": cid,
                        "result_lazy": True,
                        "size": 1000 + i,
                        "summary": {
                            "kind": "web_search",
                            "size": 1000 + i,
                            "result_count": 3,
                        },
                    }
                ],
            )
        )
    content_blocks.append({"type": "text", "content": "final answer text"})

    message = {
        "role": "assistant",
        "content": "final answer text",
        "content_blocks": content_blocks,
        # Only the LAST body survived in the persisted map — the shrink that
        # bricked the turn.
        "tool_result_bodies": {
            "tool_web_search_5": {"content": "REAL BODY FOR THE LAST SEARCH"}
        },
    }

    out = blocks_to_api_messages([message])
    tool_messages = [m for m in out if m.get("role") == "tool"]
    assert len(tool_messages) == 6
    for tm in tool_messages:
        # content is a list-of-text-parts; each must carry non-empty text.
        parts = tm["content"]
        assert isinstance(parts, list) and parts
        text = parts[0].get("text", "")
        assert text and text.strip()
    # The last one recovered its real body; the earlier five got placeholders.
    last = tool_messages[5]["content"][0]["text"]
    assert last == "REAL BODY FOR THE LAST SEARCH"
    first = tool_messages[0]["content"][0]["text"]
    assert "lost from storage" in first


# ---------------------------------------------------------------------------
# Layer 1 — generation-local ledger layering (simulated store wipe)
# ---------------------------------------------------------------------------


def test_merge_layering_survives_store_wipe():
    # Seed the socket store, then wipe it, and prove the generation-local ledger
    # (the layer _current_tool_result_bodies merges) still reconstructs the full
    # map — i.e. a mid-generation wipe can't drop a body the run still needs.
    mid = f"wipe-msg-{uuid.uuid4()}"
    b1 = {"content": "body one"}
    b2 = {"content": "body two"}
    set_tool_result_body(mid, "t1", b1)
    set_tool_result_body(mid, "t2", b2)
    assert set(get_tool_result_bodies(mid).keys()) == {"t1", "t2"}

    # What the running generation keeps in generation_tool_result_bodies.
    generation_local = {"t1": b1, "t2": b2}

    # External actor wipes the RAM store mid-generation.
    clear_tool_result_bodies(mid)
    assert get_tool_result_bodies(mid) == {}

    # _current_tool_result_bodies layers (persisted, live, generation, extra).
    merged = _merge_tool_result_body_maps(
        {},  # persisted (nothing from DB yet)
        get_tool_result_bodies(mid),  # live store — now empty
        generation_local,  # wipe-immune ledger
        None,  # extra
    )
    assert set(merged.keys()) == {"t1", "t2"}
    assert merged["t1"]["content"] == "body one"
    assert merged["t2"]["content"] == "body two"


# ---------------------------------------------------------------------------
# Layers 2 & 3 — DB-backed persistence (union / prune / write-through)
# ---------------------------------------------------------------------------


class _SyncChats:
    """Drive the async Chats proxy synchronously (pytest-asyncio isn't installed).
    NullPool (DATABASE_POOL_SIZE=0 in the test env) makes each asyncio.run get a
    fresh connection, so no cross-loop engine disposal is needed."""

    def __init__(self, target):
        self._target = target

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def _call(*args, **kwargs):
            return asyncio.run(attr(*args, **kwargs))

        return _call


_Chats = _SyncChats(Chats)


@pytest.fixture()
def chat_id():
    chat = _Chats.insert_new_chat(
        f"user-{uuid.uuid4()}",
        ChatForm(
            chat={
                "title": "tool_result_bodies test",
                "history": {"currentId": None, "messages": {}},
            }
        ),
    )
    return chat.id


def _blocks_referencing(*refs):
    return [
        {
            "type": "tool_calls",
            "content": [
                {
                    "id": r,
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
                for r in refs
            ],
            "results": [{"tool_call_id": r, "result_ref": r, "content": ""} for r in refs],
        }
    ]


def test_upsert_union_never_shrinks(chat_id):
    # Seed with two bodies + content_blocks.
    _Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        "m1",
        {
            "role": "assistant",
            "content": "x",
            "parentId": None,
            "content_blocks": _blocks_referencing("a", "b", "c"),
            "tool_result_bodies": {"a": {"content": "A"}, "b": {"content": "B"}},
        },
        return_model=False,
    )
    # Partial write carrying ONLY {c} must UNION (not replace) → a, b, c.
    _Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        "m1",
        {"tool_result_bodies": {"c": {"content": "C"}}},
        return_model=False,
    )
    msg = _Chats.get_message_by_id_and_message_id(chat_id, "m1")
    assert set(msg["tool_result_bodies"].keys()) == {"a", "b", "c"}

    # A partial with NO tool_result_bodies key must leave the map untouched.
    _Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id, "m1", {"content": "y"}, return_model=False
    )
    msg = _Chats.get_message_by_id_and_message_id(chat_id, "m1")
    assert set(msg["tool_result_bodies"].keys()) == {"a", "b", "c"}
    assert msg["content"] == "y"  # the actual partial field did land


def test_upsert_prune_keeps_only_referenced(chat_id):
    # prune=True + content_blocks referencing only b, c → row keeps exactly {b, c}.
    _Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        "m2",
        {
            "role": "assistant",
            "content": "x",
            "parentId": None,
            "content_blocks": _blocks_referencing("b", "c"),
            "tool_result_bodies": {
                "a": {"content": "A"},
                "b": {"content": "B"},
                "c": {"content": "C"},
            },
        },
        return_model=False,
        prune_tool_result_bodies=True,
    )
    msg = _Chats.get_message_by_id_and_message_id(chat_id, "m2")
    assert set(msg["tool_result_bodies"].keys()) == {"b", "c"}


def test_merge_message_tool_result_bodies_into_existing(chat_id):
    _Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        "m3",
        {
            "role": "assistant",
            "content": "x",
            "parentId": None,
            "tool_result_bodies": {"x": {"content": "X"}},
        },
        return_model=False,
    )
    ok = _Chats.merge_message_tool_result_bodies(chat_id, "m3", {"y": {"content": "Y"}})
    assert ok is True
    msg = _Chats.get_message_by_id_and_message_id(chat_id, "m3")
    assert set(msg["tool_result_bodies"].keys()) == {"x", "y"}


def test_merge_message_tool_result_bodies_creates_key(chat_id):
    # The message exists but its meta has no tool_result_bodies yet.
    _Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        "m4",
        {"role": "assistant", "content": "x", "parentId": None},
        return_model=False,
    )
    ok = _Chats.merge_message_tool_result_bodies(chat_id, "m4", {"z": {"content": "Z"}})
    assert ok is True
    msg = _Chats.get_message_by_id_and_message_id(chat_id, "m4")
    assert set(msg["tool_result_bodies"].keys()) == {"z"}


def test_merge_message_tool_result_bodies_row_missing_inserts(chat_id):
    # The assistant row was never upserted (first round beat the first checkpoint):
    # the targeted UPDATE matches nothing → falls back to a full-upsert INSERT.
    ok = _Chats.merge_message_tool_result_bodies(
        chat_id, "m_missing", {"w": {"content": "W"}}
    )
    assert ok is True
    msg = _Chats.get_message_by_id_and_message_id(chat_id, "m_missing")
    assert msg is not None
    assert set(msg["tool_result_bodies"].keys()) == {"w"}


def test_merge_message_tool_result_bodies_strips_nul(chat_id):
    _Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        "m5",
        {"role": "assistant", "content": "x", "parentId": None},
        return_model=False,
    )
    ok = _Chats.merge_message_tool_result_bodies(
        chat_id, "m5", {"n": {"content": "has\x00nul"}}
    )
    assert ok is True  # NUL did not abort the jsonb write
    msg = _Chats.get_message_by_id_and_message_id(chat_id, "m5")
    assert msg["tool_result_bodies"]["n"]["content"] == "hasnul"


def test_merge_message_tool_result_bodies_rejects_empty(chat_id):
    assert _Chats.merge_message_tool_result_bodies(chat_id, "m6", {}) is False
    assert _Chats.merge_message_tool_result_bodies(chat_id, "m6", None) is False
    # Non-dict values are filtered out; an all-garbage batch is a no-op False.
    assert _Chats.merge_message_tool_result_bodies(chat_id, "m6", {"k": "notadict"}) is False


def test_carry_reasoning_rounds_replaces_stale_client_map():
    # The per-round map is NOT in the v2.1 stream protocol, so a retry/rewind
    # sibling seeded from the client carries a map frozen at chat-open while
    # its content_blocks kept streaming (chat 32dac004: 18-entry map beside 49
    # rounds of blocks). The source ROW's map is server truth and must win.
    from open_webui.models.chats import carry_reasoning_rounds_from_source

    source = {
        "reasoning_details_per_round": [[{"text": f"r{i}"}] for i in range(49)],
        "reasoning_details": [{"text": "flat"}],
    }
    sibling = {
        "content_blocks": [{"type": "tool_calls", "content": []}],
        "reasoning_details_per_round": [[{"text": f"r{i}"}] for i in range(18)],
    }
    carry_reasoning_rounds_from_source(source, sibling)
    assert len(sibling["reasoning_details_per_round"]) == 49
    assert sibling["reasoning_details"] == [{"text": "flat"}]
    # Deep copy: mutating the sibling's map must not reach the source row.
    sibling["reasoning_details_per_round"][0][0]["text"] = "mutated"
    assert source["reasoning_details_per_round"][0][0]["text"] == "r0"


def test_carry_reasoning_rounds_noops_safely():
    from open_webui.models.chats import carry_reasoning_rounds_from_source

    # No blocks on the sibling → nothing to align against, leave untouched.
    sibling = {"reasoning_details_per_round": [["client"]]}
    carry_reasoning_rounds_from_source({"reasoning_details_per_round": [["s"]]}, sibling)
    assert sibling["reasoning_details_per_round"] == [["client"]]

    # Source without a map → keep whatever the client sent (can't improve).
    sibling = {
        "content_blocks": [{"type": "text", "content": "x"}],
        "reasoning_details_per_round": [["client"]],
    }
    carry_reasoning_rounds_from_source({}, sibling)
    assert sibling["reasoning_details_per_round"] == [["client"]]

    # Non-dict inputs never raise.
    carry_reasoning_rounds_from_source(None, sibling)
    carry_reasoning_rounds_from_source({"reasoning_details_per_round": [["s"]]}, None)
