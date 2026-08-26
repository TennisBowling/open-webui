"""Single owner of the lazy message-body contract.

Assistant messages carry two classes of bandwidth-heavy display content inside
``content_blocks``: tool results and reasoning text. Neither is needed to paint
a chat — the frontend renders collapsed cards ("Tool Executed", "Thought for N
seconds") and fetches the body on expand via
``GET /chats/{id}/messages/{message_id}/tool-results/{ref}``.

The canonical STORED form of a block is therefore slim: heavy content lives in
the message's ``tool_result_bodies`` side map (accumulate-only, union-merged —
see models/chats.py) keyed by ``result_ref`` (tool results, == tool_call_id) or
``reasoning:{block_index}`` (reasoning). This module provides:

- the WRITE-time splitters used by the streaming persist path
  (``split_tool_result_bodies``, ``split_reasoning_bodies``),
- the READ-time slimmer used by the API projection for rows persisted before
  the canonical form existed (``slim_content_blocks_for_read``),
- the text-only projection of a message's visible content
  (``text_only_content_from_blocks``) — the canonical form of the legacy
  ``content`` column for any message that has ``content_blocks``,
- the shared thresholds/exemptions, so write and read can never disagree about
  what is "heavy".

This module must stay a LEAF (stdlib imports only): it is imported by both
``utils/middleware.py`` and ``models/chats.py``.
"""

import hashlib
import json
import os
import re

# Tiny results are cheaper to keep inline than to stub: the stub costs ~200B of
# ref/sha256/summary plus an avoided round-trip, so results at or under this
# many bytes stay inline. (Previously web tools inlined up to 2KB and everything
# else up to 64KB — on metered links that shipped megabytes of never-expanded
# tool output.)
TOOL_INLINE_RESULT_MAX = max(
    0,
    int(
        os.environ.get(
            "TOOL_INLINE_RESULT_MAX",
            # Back-compat: honor the old generic knob if someone set it.
            os.environ.get("GENERIC_TOOL_INLINE_RESULT_MAX", "512"),
        )
        or "512"
    ),
)
WEB_TOOL_INLINE_RESULT_MAX = TOOL_INLINE_RESULT_MAX
GENERIC_TOOL_INLINE_RESULT_MAX = TOOL_INLINE_RESULT_MAX
# Tools whose cards render the RESULT content directly as interactive UI
# outside the generic lazy-fetch path (ask_user shows the submitted answer in
# the question card). Slimming these would blank the card on reload; their
# bodies are tiny anyway.
LAZY_RESULT_EXEMPT_TOOL_NAMES = {"ask_user"}
# Subagent results keep their PRE-EXISTING 64KB inline threshold: the card +
# model-replay path uses a dual store (block.results vs subagent_runs) with a
# dedicated missing-body recovery, and that machinery has a bug history —
# don't change its observed behavior while making everything else lazy.
SUBAGENT_TOOL_NAMES = {
    "subagent_launch",
    "subagent_continue",
    "subagent_agent_launch",
}
SUBAGENT_INLINE_RESULT_MAX = max(
    4096, int(os.environ.get("SUBAGENT_INLINE_RESULT_MAX", "65536") or "65536")
)

# Reasoning text is display-only (model replay rides the separate
# reasoning_details fields — see utils/messages.py _assistant_content_from_blocks),
# and mostly read while it streams. Persisted reasoning above this many bytes is
# stubbed to "Thought for N seconds" and fetched on expand.
REASONING_INLINE_MAX = max(
    0, int(os.environ.get("REASONING_INLINE_MAX", "512") or "512")
)

REASONING_REF_PREFIX = "reasoning:"


def sanitize_content_blocks(content_blocks):
    """Enforce the structural invariant of ``content_blocks``: a DENSE list of
    block dicts. Drops null/non-dict entries (a client whose stream mirror
    missed early block_opens used to serialize array holes as JSON ``null`` —
    see stream-protocol.ts applyDeltaOp — and a rewind sibling could persist a
    clone of that). Returns the SAME object when nothing changed so callers can
    use identity to skip copies; passes non-list values through untouched (the
    caller decides what absent/None means)."""
    if not isinstance(content_blocks, list):
        return content_blocks
    if all(isinstance(block, dict) for block in content_blocks):
        return content_blocks
    return [block for block in content_blocks if isinstance(block, dict)]


def _tool_call_name_by_id(block):
    out = {}
    for call in block.get("content") or []:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id") or call.get("tool_call_id")
        if call_id:
            out[call_id] = call.get("function", {}).get("name", "")
    return out


def _summarize_tool_result(tool_name: str, content: str) -> dict:
    summary = {
        "kind": tool_name or "tool",
        "size": len((content or "").encode("utf-8")),
    }
    if tool_name == "web_search":
        match = re.search(r"^Found\s+(\d+)\s+results", content or "", flags=re.I | re.M)
        if match:
            summary["result_count"] = int(match.group(1))
    elif tool_name == "web_fetch":
        match = re.search(
            r"^Retrieved content from\s+(\d+)\s+URL", content or "", flags=re.I | re.M
        )
        if match:
            summary["page_count"] = int(match.group(1))
    return summary


def _slim_tool_result(result, tool_name: str = "", *, store_body: bool = False):
    if not isinstance(result, dict):
        return result, None
    content = result.get("content")
    if not isinstance(content, str):
        return result, None

    if tool_name in LAZY_RESULT_EXEMPT_TOOL_NAMES:
        return result, None

    inline_limit = (
        SUBAGENT_INLINE_RESULT_MAX
        if tool_name in SUBAGENT_TOOL_NAMES
        else TOOL_INLINE_RESULT_MAX
    )
    content_size = len(content.encode("utf-8", "replace"))
    if content_size <= inline_limit:
        return result, None

    tool_call_id = result.get("tool_call_id") or ""
    body = dict(result)
    slim = {k: v for k, v in result.items() if k != "content"}
    slim.update(
        {
            "tool_call_id": tool_call_id,
            "content": "",
            "result_ref": tool_call_id,
            "result_lazy": True,
            "size": content_size,
            "sha256": hashlib.sha256(content.encode("utf-8", "replace")).hexdigest(),
            "summary": _summarize_tool_result(tool_name, content),
        }
    )
    return slim, body if store_body else None


def split_tool_result_bodies(content_blocks):
    """Return (slim_blocks, bodies_by_tool_call_id). Large web tool bodies are
    replaced with refs in the message hot path and persisted separately on the
    assistant message as `tool_result_bodies`."""
    bodies = {}
    out = []
    for block in content_blocks or []:
        if not isinstance(block, dict):
            out.append(block)
            continue
        if block.get("type") == "tool_calls" and "results" in block:
            name_by_id = _tool_call_name_by_id(block)
            slim = {k: v for k, v in block.items() if k != "results"}
            slim_results = []
            for r in block.get("results") or []:
                tc_id = r.get("tool_call_id") if isinstance(r, dict) else ""
                slim_r, body = _slim_tool_result(
                    r, name_by_id.get(tc_id, ""), store_body=True
                )
                if body is not None and tc_id:
                    bodies[tc_id] = body
                slim_results.append(slim_r)
            slim["results"] = slim_results
            out.append(slim)
        else:
            out.append(block)
    return out, bodies


def _merge_tool_result_body_maps(*body_maps):
    merged = {}
    for body_map in body_maps:
        if not isinstance(body_map, dict):
            continue
        for tool_call_id, body in body_map.items():
            if tool_call_id and isinstance(body, dict):
                merged[str(tool_call_id)] = body
    return merged


def _strip_tool_results(content_blocks):
    """Mirror state stores block shapes but never heavy web tool result bodies.
    Non-web/small results remain inline for compatibility; large web results
    retain refs/metadata so collapsed cards can render cheaply."""
    return split_tool_result_bodies(content_blocks)[0]


def reasoning_ref_for_index(index: int) -> str:
    return f"{REASONING_REF_PREFIX}{index}"


def parse_reasoning_ref(ref) -> int | None:
    """Return the content_blocks index encoded in a reasoning ref, or None when
    ``ref`` is not a reasoning ref."""
    if not isinstance(ref, str) or not ref.startswith(REASONING_REF_PREFIX):
        return None
    try:
        return int(ref[len(REASONING_REF_PREFIX) :])
    except ValueError:
        return None


def _is_closed_reasoning_block(block) -> bool:
    return bool(block.get("ended_at") or block.get("duration") is not None)


def _stub_reasoning_block(block: dict, index: int, content: str) -> dict:
    slim = {k: v for k, v in block.items() if k != "content"}
    slim.update(
        {
            "content": "",
            "content_ref": block.get("content_ref") or reasoning_ref_for_index(index),
            "content_lazy": True,
            "content_size": len(content.encode("utf-8", "replace")),
        }
    )
    return slim


def split_reasoning_bodies(content_blocks):
    """Return (slim_blocks, bodies_by_reasoning_ref).

    CLOSED reasoning blocks whose text exceeds ``REASONING_INLINE_MAX`` move
    their content to the body map under ``reasoning:{index}``; the block keeps
    its timing metadata plus ``content_ref``/``content_lazy``/``content_size``
    so the frontend can render "Thought for N seconds" and fetch on expand.

    OPEN blocks (still streaming — no ended_at/duration yet) are left inline on
    purpose: the stub and its body land in the same durable write only for
    finished text, so a persisted row can never carry a dangling stub for a
    block that is still growing.
    """
    bodies = {}
    out = []
    for idx, block in enumerate(content_blocks or []):
        if (
            isinstance(block, dict)
            and block.get("type") == "reasoning"
            and not block.get("content_lazy")
            and isinstance(block.get("content"), str)
            and _is_closed_reasoning_block(block)
        ):
            content = block["content"]
            if len(content.encode("utf-8", "replace")) > REASONING_INLINE_MAX:
                slim = _stub_reasoning_block(block, idx, content)
                bodies[slim["content_ref"]] = {"content": content}
                out.append(slim)
                continue
        out.append(block)
    return out, bodies


def slim_content_blocks_for_read(content_blocks):
    """Outbound (read-path) projection of ``content_blocks`` for rows persisted
    before the canonical slim form existed: apply exactly the write-time
    slimming — tool results via ``_slim_tool_result`` (same thresholds and
    exemptions), reasoning via the same stub shape — WITHOUT producing bodies
    (the stored row keeps them; the lazy endpoint's inline fallback serves
    them). Returns the SAME list object when nothing changed, so callers can
    use identity to skip copies. Canonical rows pass through untouched at
    near-zero cost."""
    if not isinstance(content_blocks, list) or not content_blocks:
        return content_blocks
    out = []
    changed = False
    for idx, block in enumerate(content_blocks):
        if not isinstance(block, dict):
            out.append(block)
            continue
        btype = block.get("type")
        if btype == "tool_calls" and isinstance(block.get("results"), list):
            name_by_id = _tool_call_name_by_id(block)
            slim_results = []
            results_changed = False
            for r in block.get("results") or []:
                tc_id = r.get("tool_call_id") if isinstance(r, dict) else ""
                slim_r, _ = _slim_tool_result(r, name_by_id.get(tc_id, ""))
                if slim_r is not r:
                    results_changed = True
                slim_results.append(slim_r)
            if results_changed:
                out.append({**block, "results": slim_results})
                changed = True
            else:
                out.append(block)
        elif (
            btype == "reasoning"
            and not block.get("content_lazy")
            and isinstance(block.get("content"), str)
            and _is_closed_reasoning_block(block)
            and len(block["content"].encode("utf-8", "replace"))
            > REASONING_INLINE_MAX
        ):
            out.append(_stub_reasoning_block(block, idx, block["content"]))
            changed = True
        elif btype == "compaction" and isinstance(block.get("envelope"), str):
            # The exact bytes sent upstream for this cut. Kept on the stored row
            # (the compaction endpoint reads it there) but never shipped with the
            # chat: it restates every verbatim user instruction and the whole
            # tool index, and it is behind a click.
            envelope = block["envelope"]
            slim = {k: v for k, v in block.items() if k != "envelope"}
            slim["envelope_size"] = len(envelope.encode("utf-8", "replace"))
            slim["envelope_lazy"] = True
            out.append(slim)
            changed = True
        else:
            out.append(block)
    return out if changed else content_blocks


def reasoning_body_from_blocks(content_blocks, ref):
    """Resolve a ``reasoning:{index}`` ref against a blocks array (stored row or
    live stream state). Returns ``{"content": ...}`` or None."""
    idx = parse_reasoning_ref(ref)
    if idx is None or not isinstance(content_blocks, list):
        return None
    if idx < 0 or idx >= len(content_blocks):
        return None
    block = content_blocks[idx]
    if not isinstance(block, dict) or block.get("type") != "reasoning":
        return None
    content = block.get("content")
    if isinstance(content, str) and content:
        return {"content": content}
    return None


def text_only_content_from_blocks(content_blocks) -> str:
    """Plain-text projection of a block-bearing message: the visible text the
    user reads (text blocks + user_steer interjections), no tool/reasoning
    markup. This IS the canonical `content` for messages with content_blocks —
    consumed by FTS search indexing, title/tags generation, exports, and any
    legacy reader. Reasoning and tool blocks never appear here."""
    parts = []
    for block in content_blocks or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = (block.get("content") or "").strip()
            if text:
                parts.append(text)
        elif btype == "user_steer":
            text = (block.get("content") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)
