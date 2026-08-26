"""The compaction read-back escape hatch (COMPACTION.md §7).

The bodies are never deleted by a compaction — they stay in the message's
``tool_result_bodies`` side map — so this tool is a wrapper over machinery that
already works. What's worth locking in is the resolution ORDER (live stream state
→ persisted side map → inline, the same order the HTTP endpoint uses) and the
three distinguishable outcomes: a body, a call that exists but produced nothing,
and a ref that was never issued. Conflating the last two makes a model retry a
call that can never resolve.

Coroutines are driven with ``asyncio.run`` inside plain sync tests — an
``async def test_`` would be silently SKIPPED here (no pytest-asyncio).
"""

import asyncio

import open_webui.models.chats as chats_module
import open_webui.socket.main as socket_module
from open_webui.utils.read_tool_result_tool import (
    READ_TOOL_RESULT_MAX_CHARS,
    ReadToolResultTools,
    resolve_tool_result_body,
)


class _FakeChats:
    def __init__(self, messages_map):
        self.messages_map = messages_map

    async def get_messages_map_by_chat_id(self, chat_id):
        return self.messages_map

    async def get_message_by_id_and_message_id(self, chat_id, message_id):
        return self.messages_map.get(message_id)


def _install(monkey_messages, *, stream_state=None, live_bodies=None):
    """Swap the two lazily-imported collaborators. Returns a restore callable."""
    original_chats = chats_module.Chats
    original_state = socket_module.get_stream_state
    original_body = socket_module.get_tool_result_body

    chats_module.Chats = _FakeChats(monkey_messages)
    socket_module.get_stream_state = lambda mid: (stream_state or {}).get(mid, {})
    socket_module.get_tool_result_body = lambda mid, tcid: (live_bodies or {}).get(
        (mid, tcid)
    )

    def restore():
        chats_module.Chats = original_chats
        socket_module.get_stream_state = original_state
        socket_module.get_tool_result_body = original_body

    return restore


def _message_with_call(call_id, *, body=None, inline=None):
    msg = {
        "id": "m1",
        "role": "assistant",
        "content_blocks": [
            {
                "type": "tool_calls",
                "content": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "web_search", "arguments": "{}"},
                    }
                ],
                "results": [{"tool_call_id": call_id, "content": inline or ""}],
            }
        ],
    }
    if body is not None:
        msg["tool_result_bodies"] = {call_id: {"content": body}}
    return msg


def test_resolves_from_the_persisted_side_map():
    restore = _install({"m1": _message_with_call("c1", body="THE FULL BODY")})
    try:
        assert (
            asyncio.run(resolve_tool_result_body("chat", "c1")) == "THE FULL BODY"
        )
    finally:
        restore()


def test_resolves_a_bare_tool_call_id_without_knowing_the_message():
    """The ref in the tool index is a bare tool_call_id on purpose (the API-shape
    projection the index is also built from has no message id). The owning
    message is found here."""
    restore = _install(
        {
            "m0": _message_with_call("cX", body="wrong"),
            "m1": {**_message_with_call("c1", body="right"), "id": "m1"},
        }
    )
    try:
        assert asyncio.run(resolve_tool_result_body("chat", "c1")) == "right"
    finally:
        restore()


def test_accepts_the_two_part_endpoint_shaped_ref():
    restore = _install({"m1": _message_with_call("c1", body="THE FULL BODY")})
    try:
        assert (
            asyncio.run(resolve_tool_result_body("chat", "m1/c1"))
            == "THE FULL BODY"
        )
    finally:
        restore()


def test_live_stream_state_wins_over_the_persisted_row():
    """A mid-turn compaction means the model may read back a result from earlier
    in the SAME still-streaming message, whose body hasn't been written yet."""
    restore = _install(
        {"m1": _message_with_call("c1", body="STALE PERSISTED")},
        stream_state={"m1": {"chat_id": "chat", "content_blocks": []}},
        live_bodies={("m1", "c1"): {"content": "LIVE BODY"}},
    )
    try:
        assert (
            asyncio.run(
                resolve_tool_result_body("chat", "c1", hint_message_id="m1")
            )
            == "LIVE BODY"
        )
    finally:
        restore()


def test_live_state_from_another_chat_is_ignored():
    restore = _install(
        {"m1": _message_with_call("c1", body="PERSISTED")},
        stream_state={"m1": {"chat_id": "OTHER", "content_blocks": []}},
        live_bodies={("m1", "c1"): {"content": "LEAKED"}},
    )
    try:
        assert (
            asyncio.run(
                resolve_tool_result_body("chat", "c1", hint_message_id="m1")
            )
            == "PERSISTED"
        )
    finally:
        restore()


def test_falls_back_to_an_inline_body_on_a_pre_slim_row():
    restore = _install({"m1": _message_with_call("c1", inline="OLD INLINE BODY")})
    try:
        assert (
            asyncio.run(resolve_tool_result_body("chat", "c1"))
            == "OLD INLINE BODY"
        )
    finally:
        restore()


def test_distinguishes_no_output_from_no_such_ref():
    restore = _install({"m1": _message_with_call("c1")})
    try:
        # The call was issued but produced nothing recoverable.
        assert asyncio.run(resolve_tool_result_body("chat", "c1")) == ""
        # This ref was never issued at all.
        assert asyncio.run(resolve_tool_result_body("chat", "nope")) is None
    finally:
        restore()


def _run_tool(tool, ref, meta):
    return asyncio.run(tool.read_tool_result(ref, __metadata__=meta))


def test_tool_surface_messages():
    tool = ReadToolResultTools()
    restore = _install({"m1": _message_with_call("c1", body="BODY")})
    try:
        assert _run_tool(tool, "c1", {"chat_id": "chat"}) == "BODY"
        assert "no tool result found" in _run_tool(tool, "zzz", {"chat_id": "chat"})
        assert _run_tool(tool, "", {"chat_id": "chat"}).startswith(
            "Error: ref is required"
        )
        # A local: chat has no durable rows to read back from.
        assert "no stored tool results" in _run_tool(
            tool, "c1", {"chat_id": "local:abc"}
        )
    finally:
        restore()

    # A call that was issued but produced nothing recoverable gets its OWN
    # message, so the model doesn't retry a ref that can never resolve.
    restore = _install({"m1": _message_with_call("c1")})
    try:
        assert "no recoverable output" in _run_tool(tool, "c1", {"chat_id": "chat"})
    finally:
        restore()


def test_oversized_body_is_truncated_with_a_visible_marker():
    huge = "z" * (READ_TOOL_RESULT_MAX_CHARS + 5000)
    tool = ReadToolResultTools()
    restore = _install({"m1": _message_with_call("c1", body=huge)})
    try:
        out = asyncio.run(
            tool.read_tool_result("c1", __metadata__={"chat_id": "chat"})
        )
        assert len(out) < len(huge)
        assert "[Truncated at" in out
    finally:
        restore()
