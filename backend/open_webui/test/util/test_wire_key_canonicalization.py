"""Outbound wire bytes must not depend on dict construction history.

The live agentic loop builds tool_calls / reasoning_details dicts in provider
wire order; the next turn replays them from chat_message.meta (jsonb), which
re-sorts object keys. Identical values then serialized to different bytes and
broke the provider prompt cache at every turn boundary (found via [cache-fp]
chains in chat 35314654…: msg hashes flipped exactly at live->replay).
canonicalize_wire_key_order makes both sides byte-identical.
"""

from open_webui.utils import fast_json as json
from open_webui.utils.payload import (
    cache_prefix_fingerprint,
    canonicalize_wire_key_order,
)


def dumps(v):
    return json.dumps(v, ensure_ascii=False)


def test_wire_and_jsonb_orders_canonicalize_identically():
    # Provider wire order (as the streaming loop builds them).
    live = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"q":"x"}'},
            }
        ],
        "reasoning_details": [
            {"type": "reasoning.text", "text": "think", "format": "unknown", "index": 0}
        ],
    }
    # Postgres jsonb order (length, then bytewise) — what a replay reads back.
    replayed = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "index": 0,
                "function": {"name": "web_search", "arguments": '{"q":"x"}'},
            }
        ],
        "reasoning_details": [
            {"text": "think", "type": "reasoning.text", "index": 0, "format": "unknown"}
        ],
    }
    assert dumps(live) != dumps(replayed)  # the disease
    assert dumps(canonicalize_wire_key_order(live)) == dumps(
        canonicalize_wire_key_order(replayed)
    )


def test_canonical_order_is_jsonb_order():
    # Deploy-safety: canon == jsonb order, so replayed history bytes are
    # unchanged by canonicalization (only live-round bytes move).
    replayed_detail = {
        "text": "t",
        "type": "reasoning.text",
        "index": 0,
        "format": "unknown",
    }
    assert dumps(canonicalize_wire_key_order(replayed_detail)) == dumps(
        replayed_detail
    )


def test_canonicalization_recurses_lists_and_preserves_scalars():
    v = [{"b": 1, "a": [{"zz": 1, "y": 2}]}, "leave-strings-alone", 7]
    out = canonicalize_wire_key_order(v)
    assert dumps(out) == '[{"a":[{"y":2,"zz":1}],"b":1},"leave-strings-alone",7]'
    # arguments-style JSON strings are opaque and untouched
    args = {"function": {"arguments": '{"b":1,"a":2}', "name": "f"}}
    out = canonicalize_wire_key_order(args)
    assert out["function"]["arguments"] == '{"b":1,"a":2}'
    assert list(out["function"].keys()) == ["name", "arguments"]


def test_fingerprint_stable_across_construction_order():
    p1 = {"messages": [{"role": "user", "content": "hi"}], "tools": [{"b": 1, "a": 2}]}
    p2 = {"messages": [{"role": "user", "content": "hi"}], "tools": [{"a": 2, "b": 1}]}
    assert cache_prefix_fingerprint(
        {**p1, "tools": canonicalize_wire_key_order(p1["tools"])}
    ) == cache_prefix_fingerprint(
        {**p2, "tools": canonicalize_wire_key_order(p2["tools"])}
    )
