"""Built-in ``read_tool_result`` tool — the escape hatch for compacted context.

COMPACTION.md §7. When a conversation is compacted the tool bodies are NOT
deleted: they stay in the message's ``tool_result_bodies`` map, and the
``<tool_calls>`` index in the compacted context carries a ``ref`` for every one
of them. This tool is a model-facing wrapper over the machinery
``GET /chats/{id}/messages/{message_id}/tool-results/{tool_call_id}`` already
uses (``utils/lazy_blocks.py`` owns that contract): live in-memory stream state
while the generation is active → the persisted message's ``tool_result_bodies``
→ inline block content for rows persisted before the canonical slim form.

**The index is the load-bearing part; this tool is the escape hatch.** MemGPT's
own paper reports the model "will often stop paging through retriever results
before exhausting the database", and only 2 of ~12 systems surveyed have any
read-back at all. Nothing here assumes the model will choose to call it.

## Why the ref is a bare tool_call_id

The endpoint's path is ``{message_id}/{tool_call_id}``, but the tool index is
built by ``utils/compaction.py`` from two message shapes — the tree-walked
internal one and the already-converted API one — and only the first carries a
message id. A ref that differed between them would change the envelope's bytes
between a mid-turn assembly and the next turn's, silently breaking prompt
caching. So the ref is the tool_call_id alone and the owning message is resolved
here, server-side. The two-part form is still accepted, because it is the shape
a model that has seen the endpoint's URL will guess.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel

from open_webui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

# A recovered body is fed straight back to the model as a tool result, so it is
# bounded the same way any other tool output is. Bodies above this are truncated
# with an explicit marker rather than silently cut — a model that cannot tell it
# got a partial answer will confidently reason from half a document.
READ_TOOL_RESULT_MAX_CHARS = 200_000


def _body_text(body: Any) -> str:
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    if isinstance(body, str):
        return body
    return ""


def _inline_from_blocks(content_blocks: Any, ref: str) -> Optional[str]:
    """Rows persisted before the canonical slim form still carry full bodies
    inline. Same backward-compatibility branch the HTTP endpoint has."""
    for block in content_blocks or []:
        if not isinstance(block, dict) or block.get("type") != "tool_calls":
            continue
        for result in block.get("results") or []:
            if isinstance(result, dict) and result.get("tool_call_id") == ref:
                content = result.get("content")
                if isinstance(content, str) and content:
                    return content
    return None


def _block_owns_ref(content_blocks: Any, ref: str) -> bool:
    for block in content_blocks or []:
        if not isinstance(block, dict) or block.get("type") != "tool_calls":
            continue
        for call in block.get("content") or []:
            if isinstance(call, dict) and str(call.get("id") or "") == ref:
                return True
    return False


async def resolve_tool_result_body(
    chat_id: str, ref: str, hint_message_id: Optional[str] = None
) -> Optional[str]:
    """Resolve ``ref`` to a tool result body within ``chat_id``, or None.

    Resolution order mirrors the HTTP endpoint (routers/chats.py): live stream
    state first — a mid-turn compaction means the model may want to read back a
    result from earlier in the SAME still-streaming message, whose body has not
    been written to the row yet — then the persisted side map, then inline.
    """
    from open_webui.models.chats import Chats
    from open_webui.socket.main import get_stream_state, get_tool_result_body

    message_id: Optional[str] = None
    tool_call_id = ref
    if "/" in ref:
        # The two-part form a model may guess from the endpoint URL.
        message_id, tool_call_id = ref.split("/", 1)

    # 1. Live stream state, for the message that is generating right now.
    for candidate in (message_id, hint_message_id):
        if not candidate:
            continue
        state = get_stream_state(candidate)
        if state and state.get("chat_id") == chat_id:
            body = get_tool_result_body(candidate, tool_call_id)
            text = _body_text(body)
            if text:
                return text
            inline = _inline_from_blocks(state.get("content_blocks"), tool_call_id)
            if inline:
                return inline

    # 2/3. Persisted side map, then inline. When the ref carries no message id we
    # have to find the owner; the messages map is one read and a chat has at most
    # a few thousand rows, against a tool the model calls a handful of times.
    candidates: list[dict] = []
    if message_id:
        row = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
        if isinstance(row, dict):
            candidates.append(row)
    else:
        messages_map = await Chats.get_messages_map_by_chat_id(chat_id) or {}
        candidates = [m for m in messages_map.values() if isinstance(m, dict)]

    for row in candidates:
        bodies = row.get("tool_result_bodies")
        if isinstance(bodies, dict) and tool_call_id in bodies:
            text = _body_text(bodies[tool_call_id])
            if text:
                return text
        inline = _inline_from_blocks(row.get("content_blocks"), tool_call_id)
        if inline:
            return inline

    # The call exists but produced no recoverable body (cancelled subagent, a
    # result whose out-of-line body was lost). Distinguish that from "no such
    # ref" so the model doesn't retry a call that will never resolve.
    for row in candidates:
        if _block_owns_ref(row.get("content_blocks"), tool_call_id):
            return ""
    return None


class ReadToolResultTools:
    """Builtin tool collection for reading back compacted tool output."""

    class Valves(BaseModel):
        """Placeholder; no per-instance configuration."""

        pass

    def __init__(self):
        self.valves = self.Valves()

    async def read_tool_result(
        self,
        ref: str,
        __metadata__: Optional[dict] = None,
    ) -> str:
        """Retrieve the full output of an earlier tool call that was summarized away.

        Earlier parts of this conversation may have been replaced by a
        <compacted_context> summary. The original tool outputs still exist. Use the
        `ref` from the <tool_calls> index inside that summary to read one back in full.

        :param ref: The ref attribute of a <call> entry in the compacted context's tool index.
        :return: The full original tool output, or an error message.
        """
        metadata = __metadata__ or {}
        chat_id = str(metadata.get("chat_id") or "")
        ref = (ref or "").strip()

        if not ref:
            return "Error: ref is required"
        if not chat_id or chat_id.startswith("local:"):
            return "Error: this conversation has no stored tool results to read back"

        try:
            body = await resolve_tool_result_body(
                chat_id, ref, hint_message_id=str(metadata.get("message_id") or "")
            )
        except Exception as exc:
            log.exception("read_tool_result failed for ref %r", ref)
            return f"Error: failed to read tool result: {exc}"

        if body is None:
            return (
                f"Error: no tool result found for ref '{ref}'. Refs come from the "
                "<tool_calls> index in the compacted context; use one of those exactly."
            )
        if body == "":
            return (
                f"The tool call '{ref}' produced no recoverable output (it was "
                "cancelled, or its stored body is gone). Call the tool again if the "
                "data is needed."
            )
        if len(body) > READ_TOOL_RESULT_MAX_CHARS:
            return (
                body[:READ_TOOL_RESULT_MAX_CHARS]
                + f"\n\n[Truncated at {READ_TOOL_RESULT_MAX_CHARS} characters. The "
                "stored result is longer than this.]"
            )
        return body


_read_tool_result_tools_instance: Optional[ReadToolResultTools] = None


def get_read_tool_result_tools_instance() -> ReadToolResultTools:
    global _read_tool_result_tools_instance
    if _read_tool_result_tools_instance is None:
        _read_tool_result_tools_instance = ReadToolResultTools()
    return _read_tool_result_tools_instance
