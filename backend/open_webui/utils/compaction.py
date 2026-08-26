"""Conversation compaction — the cut, the envelope, and the mechanical index.

> Read [`COMPACTION.md`](./COMPACTION.md) first. It is the design authority; this
> module is its implementation. Companion contract:
> [`REASONING_DETAILS.md`](./REASONING_DETAILS.md) — compaction sits directly on
> top of ``blocks_to_api_messages``, the single outbound gate, and must not break
> the "one ``rs_*`` id per assistant message" invariant.

Two hard rules from the design, restated because every function here exists to
serve one of them:

1. **Nothing mutates history until the threshold, then one clean break.** The
   compaction record is a ``content_blocks`` entry (``{"type": "compaction"}``)
   inserted at the cut index. Nothing is deleted — the blocks before it stay in
   storage for read-back and for the transcript; only the OUTBOUND payload skips
   them.
2. **Anything that must not be lost is generated mechanically.** The LLM writes
   the ``<narrative>`` and nothing else. ``<user_instructions>`` and
   ``<tool_calls>`` are rebuilt from the message walk on every assembly, so they
   are LOSSLESS across N compactions (the originals never leave the database) —
   see COMPACTION.md §4 for the four codebases that learned this the hard way.

---

## Where the cut is applied

``apply_compaction_to_messages`` is called from the top of
``blocks_to_api_messages`` (utils/messages.py). That function is invoked on
exactly two shapes, and both must produce the same bytes:

* **Path A — turn start.** ``assemble_conversation_from_leaf`` (utils/chat.py)
  walks the message tree from the leaf and hands over INTERNAL messages: user
  rows plus assistant rows carrying ``content_blocks``. The compaction anchor is
  visible as a block. This walk is the lossless one.
* **Path B — between agentic rounds.** ``routers/openai.py``'s gate sees the
  already-converted list from path A plus the in-flight assistant (still
  internal). A mid-turn compaction inserts its anchor into the in-flight
  ``content_blocks``, so the anchor is again visible — but everything before the
  PREVIOUS cut has already been replaced by that cut's envelope.

Path B would therefore lose earlier user instructions / tool calls on a SECOND
compaction. The bridge is ``_CARRY_KEY``: when an envelope is injected it also
carries the collected lists on a private key, which a later pass prepends before
collecting the new span. The key is stripped on the way upstream (see
``blocks_to_api_messages``' passthrough branch). Path A never needs it — it
re-derives everything from the raw tree — which is exactly why the two paths
converge on identical bytes.

## Determinism

Live and replay must serialise byte-identically or upstream prompt caching dies
silently (REASONING_DETAILS.md §11, "Caching not working"). So:

* the narrative is generated ONCE, persisted on the block, and never regenerated;
* ``compacted_at`` is persisted, never recomputed from the clock;
* the mechanical sections are a pure function of persisted state;
* the collectors below are deliberately SHAPE-AGNOSTIC — they read a user
  instruction out of an internal ``user_steer`` block and out of the
  ``{"role": "user"}`` message that block expands into, and they read a tool
  call out of a ``tool_calls`` block and out of the API-shape
  ``assistant.tool_calls`` + ``role: "tool"`` pair. Same input state, same list.

Known divergence, documented rather than papered over: a tool result whose
persisted ``content`` is empty and which ``_expand_assistant`` recovers from
``subagent_runs.final_text`` (or replaces with the "[No output…]" placeholder)
reports a different byte size in the two shapes. That flips the size descriptor
for that one entry, which can cost one cache invalidation at a turn boundary. It
is not a correctness bug and the fix (threading the internal size through the
API shape) would need a second private carrier on every tool message.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional

from open_webui.utils.tool_calling import dedupe_repeated_tool_name

log = logging.getLogger(__name__)

COMPACTION_BLOCK_TYPE = "compaction"

# Private carrier on the injected envelope message. Holds the mechanical lists
# that produced it so a LATER compaction (which no longer sees the raw history —
# path B above) stays lossless.
#
# It must SURVIVE ``blocks_to_api_messages`` (which runs twice on the
# assemble → gate path; stripping it on the first pass would leave the second
# with nothing to read) and is removed at the actual HTTP boundary instead —
# ``strip_compaction_carry``, called in routers/openai.py right after the gate.
# The only other outbound converter, ``convert_messages_openai_to_ollama``,
# rebuilds each message from scratch and cannot leak unknown keys.
COMPACTION_CARRY_KEY = "_owui_compaction_carry"
_CARRY_KEY = COMPACTION_CARRY_KEY


def strip_compaction_carry(messages: Optional[list]) -> list:
    """Remove the private carrier from an outbound message list.

    Returns the SAME list object when nothing had to change, so the hot path
    (every request that has never compacted) allocates nothing.
    """
    if not isinstance(messages, list):
        return messages
    if not any(
        isinstance(m, dict) and COMPACTION_CARRY_KEY in m for m in messages
    ):
        return messages
    return [
        {k: v for k, v in m.items() if k != COMPACTION_CARRY_KEY}
        if isinstance(m, dict)
        else m
        for m in messages
    ]


def capture_compaction_envelope(messages: Optional[list]) -> Optional[dict]:
    """The envelope about to go on the wire, plus the block it belongs to.

    Called at the SAME boundary as ``strip_compaction_carry`` and for the same
    reason: this is the last point where the outbound bytes still exist next to
    the private carrier that says where they came from. Everything the envelope
    is built from — attached-file text folded into user content, a carried
    instruction list inherited from an earlier cut — is already baked in here,
    which is exactly why a re-render from the tree is not a substitute.

    Returns ``None`` on the overwhelmingly common no-anchor path without
    allocating.

    ``message_id`` is None when the anchor sits on the IN-FLIGHT assistant — that
    message is API-shaped between rounds and carries no id. This is load-bearing,
    not incidental: it is what stops ``record_sent_envelope`` from writing to a
    row the stream handler is concurrently checkpointing (a whole-list
    ``content_blocks`` write from here would truncate whatever the round appended
    in the meantime). The mid-turn path stores its own envelope in place instead,
    through that same checkpoint.
    """
    if not isinstance(messages, list):
        return None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        carry = msg.get(COMPACTION_CARRY_KEY)
        if not isinstance(carry, dict):
            continue
        # Only ONE envelope is ever injected (last anchor wins), so the first
        # carrier is the only carrier.
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            return None
        message_id = carry.get("anchor_message_id")
        return {
            "message_id": str(message_id) if message_id else None,
            "block_index": int(carry.get("anchor_block_index") or 0),
            "envelope": content,
        }
    return None


# Process-local record of envelopes already written back. Bounded — an entry per
# distinct (chat, anchor, bytes), and a conversation produces one per compaction.
# Losing it on restart costs exactly one redundant write.
_ENVELOPE_WRITTEN: "OrderedDict[tuple, None]" = OrderedDict()
_ENVELOPE_WRITTEN_MAX = 2048


def _envelope_write_key(chat_id: str, capture: dict) -> tuple:
    digest = hashlib.sha256(capture["envelope"].encode("utf-8", "replace")).hexdigest()
    return (chat_id, capture["message_id"], capture["block_index"], digest)


async def record_sent_envelope(chat_id: Optional[str], capture: Optional[dict]) -> bool:
    """Write the outbound envelope back onto its anchor block. Best-effort.

    The bytes are stored INLINE on the block and projected out of the read path
    by ``slim_content_blocks_for_read`` — the same shape heavy reasoning text
    uses: the stored row keeps the body, the chat-open payload ships a stub, and
    the detail endpoint reads the row. So this costs the tail nothing.

    Guarded by an in-memory seen-set keyed on the bytes, because the envelope is
    stable across the many requests one cut spans; without it every round of a
    200-round agentic turn would re-read and re-write the same anchor.
    """
    if not chat_id or not capture or str(chat_id).startswith("local:"):
        return False
    if not capture.get("message_id"):
        # Anchor is on the in-flight assistant — the stream owns that row.
        return False

    key = _envelope_write_key(chat_id, capture)
    if key in _ENVELOPE_WRITTEN:
        return False

    try:
        return await _record_sent_envelope(chat_id, capture, key)
    except Exception:
        # Detached from the request that produced it: it has no one to raise to.
        log.exception("recording sent compaction envelope failed (chat=%s)", chat_id)
        return False


async def _record_sent_envelope(chat_id: str, capture: dict, key: tuple) -> bool:
    from open_webui.models.chats import Chats

    message = await Chats.get_message_by_id_and_message_id(
        chat_id, capture["message_id"]
    )
    if not message:
        return False
    blocks = message.get("content_blocks")
    index = capture["block_index"]
    if not isinstance(blocks, list) or index < 0 or index >= len(blocks):
        return False
    block = blocks[index]
    if not is_compaction_block(block):
        return False

    _ENVELOPE_WRITTEN[key] = None
    while len(_ENVELOPE_WRITTEN) > _ENVELOPE_WRITTEN_MAX:
        _ENVELOPE_WRITTEN.popitem(last=False)

    if block.get("envelope") == capture["envelope"]:
        return False

    new_blocks = list(blocks)
    new_blocks[index] = {**block, "envelope": capture["envelope"]}
    await Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        capture["message_id"],
        {"content_blocks": new_blocks},
        return_model=False,
    )
    return True


# Tool-call arguments are echoed verbatim into the index so the model can tell
# two calls of the same tool apart. Capped because a single `show_widget` or
# container-exec call can carry kilobytes of code, and COMPACTION.md §9 already
# flags unbounded index growth as the one affordable-but-unsolved cost. The cap
# is a constant, so it is deterministic.
TOOL_INDEX_ARGS_MAX_CHARS = 160

# Observation messages that `utils/messages.py` synthesises for tool-provided
# media. They are `role: "user"` on the wire because Chat Completions only
# allows image/video parts there — but they are NOT user instructions and must
# never be replayed as such.
_SYNTHETIC_USER_NAMES = {"view_image_tool", "view_video_tool"}

_ENVELOPE_OPEN = "<compacted_context"

# Closing delimiters that verbatim user content could accidentally contain. See
# COMPACTION.md §3 "Escaping — for correctness, not security": this instance is
# single-user and trusted, so this is not a prompt-injection boundary. It exists
# because a chat about XML (or this very design document, which quotes the tags
# repeatedly) would otherwise break its own envelope.
_ESCAPED_DELIMITERS = (
    "</message>",
    "</user_instructions>",
    "</tool_calls>",
    "</narrative>",
    "</note>",
    "</compacted_context>",
)

_RESULT_COUNT_RE = re.compile(r"^Found\s+(\d+)\s+results", re.I | re.M)
_PAGE_COUNT_RE = re.compile(r"^Retrieved content from\s+(\d+)\s+URL", re.I | re.M)


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


def is_compaction_block(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == COMPACTION_BLOCK_TYPE


def utc_now_iso() -> str:
    """Second-resolution UTC stamp, ``Z``-suffixed. Persisted on the block at
    creation and never recomputed — the envelope's bytes depend on it."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_compaction_block(
    narrative: str,
    *,
    covers: int = 0,
    tokens: Optional[int] = None,
    context_length: Optional[int] = None,
    compacted_at: Optional[str] = None,
) -> dict:
    """Build the ``content_blocks`` entry that anchors a compaction.

    ``covers`` / ``tokens`` / ``context_length`` are display-only (the divider in
    COMPACTION.md §8 reads "Context compacted · 47 messages → 3.2k tokens").
    ``narrative`` is the ONLY LLM-authored field and the only one the envelope
    reads back.
    """
    block: dict = {
        "type": COMPACTION_BLOCK_TYPE,
        "compacted_at": compacted_at or utc_now_iso(),
        "narrative": narrative or "",
        "covers": int(covers or 0),
    }
    if tokens is not None:
        block["tokens"] = int(tokens)
    if context_length is not None:
        block["context_length"] = int(context_length)
    return block


def find_cut_index(
    content_blocks: Optional[list], *, completed: bool = False
) -> int:
    """Position at which a compaction anchor may be inserted into ``content_blocks``.

    The anchor is not decoration: everything BEFORE it is what the outbound
    payload replaces with the envelope, and the divider renders at exactly this
    index in the transcript. So the index has to be **the write head at the
    moment the compaction ran** — otherwise the line claims the cut happened
    somewhere it didn't — and it has to be an **assistant-emission boundary**, or
    ``_split_at_anchor`` slices ``reasoning_details_per_round`` out of step with
    the emissions it indexes.

    Those two requirements land in different places depending on the message:

    ``completed=True`` — the message is finished (turn-start gate: the anchor
    goes into the PREVIOUS assistant turn; ``/compact``: into the leaf). Its last
    block closes its last emission, so the write head is the END. The divider
    lands after the answer, i.e. between that turn and whatever comes next, which
    is when the compaction actually happened. The old behaviour cut after the
    last ``tool_calls`` block instead, which for any turn that ended in prose
    (nearly all of them) drew the divider ABOVE the answer the user had just
    read, and dropped that answer from the payload while claiming to have kept
    it — the manual path's own docstring says "everything up to and including the
    answer just given is what the user is asking to collapse".

    ``completed=False`` — the message is IN FLIGHT (mid-turn gate). The end is
    NOT a safe boundary here: the round that just streamed has already written
    its ``reasoning`` block while its ``tool_calls`` block is still queued, so
    cutting at the end would split that emission in half. The write head is the
    slot right after the last COMPLETED ``tool_calls`` block — which is also what
    puts the divider between the Working card for the rounds before the cut and
    a fresh one for the rounds after it. COMPACTION.md §2; the same boundary set
    ``getRewindCutIndices`` (src/lib/utils/retryLastRequest.ts) computes.

    With no ``tool_calls`` block at all there is nothing to dangle, so index 0 —
    the whole message survives the cut and everything BEFORE the message is what
    gets dropped.

    An existing anchor sitting at that position is stepped OVER. "Last anchor
    wins" is resolved by block index, so inserting in front of an older anchor
    would hand the win back to the stale narrative — a silent, very hard-to-see
    regression the second time a message is compacted.
    """
    blocks = content_blocks if isinstance(content_blocks, list) else []
    if completed:
        return len(blocks)
    last_tool_calls = -1
    for idx, block in enumerate(blocks):
        if isinstance(block, dict) and block.get("type") == "tool_calls":
            last_tool_calls = idx
    idx = last_tool_calls + 1 if last_tool_calls >= 0 else 0
    while idx < len(blocks) and is_compaction_block(blocks[idx]):
        idx += 1
    return idx


def insert_compaction_block(
    content_blocks: Optional[list], block: dict, *, completed: bool = False
) -> list:
    """Return a NEW dense block list with ``block`` spliced in at the cut index.

    Never mutates the caller's list and never produces a hole — the dense
    invariant (``open_webui_content_blocks_dense_invariant``) is enforced again at
    the write choke in ``models/chats.py``, but a writer that manufactures holes
    upstream of it has already lost. Non-dict entries in a legacy row are dropped
    here for the same reason.
    """
    blocks = [b for b in (content_blocks or []) if isinstance(b, dict)]
    idx = find_cut_index(blocks, completed=completed)
    return blocks[:idx] + [dict(block)] + blocks[idx:]


def find_last_compaction(messages: Optional[list]) -> Optional[tuple[int, int]]:
    """``(message_index, block_index)`` of the LAST compaction anchor, or None.

    "Last wins" is the whole of the branch-invalidation logic (COMPACTION.md §2):
    the caller hands us the already-walked path from the leaf, so an anchor is in
    this list iff its message is an ancestor of the leaf. Rewind before the anchor
    produces a sibling chain that simply lacks it → full history, no bookkeeping.
    """
    if not isinstance(messages, list):
        return None
    found: Optional[tuple[int, int]] = None
    for mi, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        blocks = msg.get("content_blocks")
        if not isinstance(blocks, list):
            continue
        for bi, block in enumerate(blocks):
            if is_compaction_block(block):
                found = (mi, bi)
    return found


def has_uncompacted_span(messages: Optional[list]) -> bool:
    """True when real content exists AFTER the last compaction anchor.

    The anti-thrash guard. The trigger reads the LAST response's
    ``total_tokens``, which is measured BEFORE the cut takes effect — so the
    round immediately after a compaction still sees an over-threshold number and
    would compact again, burning a summarizer call per round and stacking
    anchors that summarize nothing. Empty trailing ``text`` blocks (the stream
    target the loop appends after every tool round) and further anchors don't
    count as content.

    COMPACTION.md §9 lists a proper circuit breaker (Claude Code trips after 3
    refills within 3 turns) as deferred; this is the cheap structural half of it,
    and unlike a counter it cannot get out of sync with the data.
    """
    if not isinstance(messages, list) or not messages:
        return False
    anchor = find_last_compaction(messages)
    if anchor is None:
        return True
    mi, bi = anchor
    if mi < len(messages) - 1:
        return True
    blocks = messages[mi].get("content_blocks") or []
    for block in blocks[bi + 1 :]:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == COMPACTION_BLOCK_TYPE:
            continue
        if btype == "text" and not (block.get("content") or "").strip():
            continue
        return True
    return False


def conversation_has_compacted_context(messages: Optional[list]) -> bool:
    """True when an assembled outbound list already carries an injected envelope.

    Used to decide whether the ``read_tool_result`` escape hatch belongs in the
    tool list for this request. Cheap and allocation-free — it only looks at the
    first characters of user message content.
    """
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        if _CARRY_KEY in msg:
            return True
        content = msg.get("content")
        if isinstance(content, str) and content.startswith(_ENVELOPE_OPEN):
            return True
    return False


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


def should_compact(
    total_tokens: Optional[int],
    context_length: Optional[int],
    threshold: float,
) -> bool:
    """``last response's usage.total_tokens >= threshold * context_length``.

    COMPACTION.md §5. ``total_tokens`` (prompt + completion of the LAST round, not
    a running sum — see the audit note below) is the right quantity because the
    previous round's output becomes part of the next round's input, so it is
    already the floor of what the next request costs.

    **``context_length`` unknown ⇒ never auto-compact.** ``resolve_context_length``
    returns ``None`` rather than ``0`` precisely so this stays decidable; one
    connection on this instance (llama-swap) declares no window at all.

    Token-accounting note: upstream open-webui's ``merge_usage()`` SUMS
    ``input_tokens`` across every call in the agentic tool loop instead of
    overwriting, so their threshold reads a wildly inflated number and compacts
    far too early (issue #27031). This fork has no such helper: ``response_usage``
    in ``utils/middleware.py`` is plain-assigned from each usage chunk, so after
    round N it holds round N's usage and nothing else. Verified 2026-07-30.
    """
    if not isinstance(context_length, int) or context_length <= 0:
        return False
    try:
        used = int(total_tokens or 0)
    except (TypeError, ValueError):
        return False
    if used <= 0:
        return False
    return used >= threshold * context_length


def usage_total_tokens(usage: Any) -> Optional[int]:
    """Read ``total_tokens`` out of a provider usage payload, falling back to
    ``prompt + completion`` when a gateway omits the total (llama.cpp's timings
    blob and a couple of the bare-id proxies do)."""
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    try:
        total_int = int(total)
        if total_int > 0:
            return total_int
    except (TypeError, ValueError):
        pass
    try:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return None
    summed = prompt + completion
    return summed if summed > 0 else None


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


def escape_envelope_delimiters(text: str) -> str:
    """Neutralize ONLY the exact closing delimiters of the envelope.

    COMPACTION.md §3: escaping every ``<``/``>`` would mangle the code snippets the
    model later reads back, defeating the point of keeping user messages verbatim.
    So the rewrite is one substring per delimiter, ``</x>`` → ``&lt;/x>`` — three
    bytes of delta on an exact match, everything else byte-for-byte.
    """
    if not isinstance(text, str) or "</" not in text:
        return text if isinstance(text, str) else ""
    for delimiter in _ESCAPED_DELIMITERS:
        if delimiter in text:
            text = text.replace(delimiter, "&lt;" + delimiter[1:])
    return text


def _escape_attribute(value: str) -> str:
    """Single-quoted XML attribute value. ``&`` first (or the escape below gets
    double-escaped), then the quote that would actually terminate the attribute.
    ``<``/``>`` are left alone on purpose — tool arguments are full of them and no
    parser downstream of here cares."""
    return (value or "").replace("&", "&amp;").replace("'", "&#39;")


# ---------------------------------------------------------------------------
# Mechanical collectors — shape-agnostic by construction
# ---------------------------------------------------------------------------


def _text_of_content(content: Any) -> str:
    """Flatten a message ``content`` (string, or OpenAI parts array) to its text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _is_real_user_message(msg: dict) -> bool:
    if msg.get("role") != "user":
        return False
    if msg.get("name") in _SYNTHETIC_USER_NAMES:
        return False
    content = msg.get("content")
    # A previously-injected envelope is not a user instruction. Its own
    # instructions ride the private carrier instead (see _CARRY_KEY).
    if isinstance(content, str) and content.startswith(_ENVELOPE_OPEN):
        return False
    return True


def collect_user_instructions(messages: Optional[list]) -> list[str]:
    """Every thing the user asked for, in order, verbatim.

    Sources (COMPACTION.md §4 table): ``role: user`` messages and ``user_steer``
    blocks. The two shapes agree because ``_expand_assistant`` turns a
    ``user_steer`` block into exactly ``{"role": "user", "content": <stripped>}``,
    which is what this function reads on the API side.
    """
    out: list[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        blocks = msg.get("content_blocks")
        if msg.get("role") == "assistant" and isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "user_steer":
                    continue
                steer = (block.get("content") or "").strip()
                if steer:
                    out.append(steer)
            continue
        if not _is_real_user_message(msg):
            continue
        text = _text_of_content(msg.get("content")).strip()
        if text:
            out.append(text)
    return out


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    kb = size / 1024.0
    if kb < 1024:
        return f"{kb:.1f}KB"
    return f"{kb / 1024.0:.1f}MB"


def _count_phrase(name: str, content: str, summary: Any) -> str:
    """"10 results" / "1 page" — the human half of the size descriptor.

    Prefers the persisted lazy-body ``summary`` descriptor and falls back to the
    same regexes ``lazy_blocks._summarize_tool_result`` used to build it, so the
    slim (content == "") and hydrated (full text) shapes agree.
    """
    count = None
    unit = None
    if isinstance(summary, dict):
        if summary.get("result_count") is not None:
            count, unit = summary.get("result_count"), "result"
        elif summary.get("page_count") is not None:
            count, unit = summary.get("page_count"), "page"
    if count is None and content:
        if name == "web_search":
            match = _RESULT_COUNT_RE.search(content)
            if match:
                count, unit = int(match.group(1)), "result"
        elif name == "web_fetch":
            match = _PAGE_COUNT_RE.search(content)
            if match:
                count, unit = int(match.group(1)), "page"
    try:
        count_int = int(count)
    except (TypeError, ValueError):
        return ""
    if count_int <= 0 or not unit:
        return ""
    return f"{count_int} {unit}" + ("" if count_int == 1 else "s")


def _descriptor(name: str, content: str, size: Optional[int], summary: Any) -> str:
    if size is None:
        size = len((content or "").encode("utf-8", "replace"))
    parts = []
    phrase = _count_phrase(name, content, summary)
    if phrase:
        parts.append(phrase)
    parts.append(_format_bytes(int(size)))
    return ", ".join(parts)


def _short_args(arguments: Any) -> str:
    if isinstance(arguments, (dict, list)):
        try:
            arguments = json.dumps(arguments, separators=(",", ":"), sort_keys=True)
        except Exception:
            arguments = str(arguments)
    text = (arguments if isinstance(arguments, str) else str(arguments or "")).strip()
    text = text.replace("\n", " ")
    if len(text) > TOOL_INDEX_ARGS_MAX_CHARS:
        text = text[: TOOL_INDEX_ARGS_MAX_CHARS - 1] + "…"
    return text


def collect_tool_index(messages: Optional[list]) -> list[dict]:
    """Every tool call, in order: ``{ref, name, args, descriptor}``.

    ``ref`` is the bare ``tool_call_id``. It is deliberately NOT
    ``{message_id}/{tool_call_id}`` (which is the shape of the read-back
    endpoint's path): the API-shape projection this function also has to read has
    no message id on it, and a ref that differs between the two shapes would
    change the envelope's bytes and break the prompt cache. The read-back tool
    resolves the owning message server-side instead — see
    ``utils/read_tool_result_tool.py``.

    Genuinely untrusted content (fetched pages, search results) never appears
    here — only tool name, arguments, and a size descriptor. COMPACTION.md §3:
    don't widen that later by inlining result snippets.
    """
    out: list[dict] = []
    pending_by_id: dict[str, dict] = {}

    def _emit(call_id: str, name: str, arguments: Any) -> dict:
        entry = {
            "ref": call_id,
            "name": dedupe_repeated_tool_name(name),
            "args": _short_args(arguments),
            "descriptor": "",
        }
        out.append(entry)
        if call_id:
            pending_by_id[call_id] = entry
        return entry

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue

        blocks = msg.get("content_blocks")
        if isinstance(blocks, list) and blocks:
            # Internal shape (path A, and the in-flight assistant on path B).
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_calls":
                    continue
                results_by_id: dict[str, dict] = {}
                for result in block.get("results") or []:
                    if isinstance(result, dict) and result.get("tool_call_id"):
                        results_by_id.setdefault(
                            str(result["tool_call_id"]), result
                        )
                for call in block.get("content") or []:
                    if not isinstance(call, dict):
                        continue
                    call_id = str(call.get("id") or "")
                    fn = call.get("function") or {}
                    entry = _emit(call_id, fn.get("name"), fn.get("arguments"))
                    result = results_by_id.get(call_id) or {}
                    size = result.get("size")
                    try:
                        size = int(size)
                    except (TypeError, ValueError):
                        size = None
                    entry["descriptor"] = _descriptor(
                        entry["name"],
                        result.get("content") or "",
                        size,
                        result.get("summary"),
                    )
            continue

        if msg.get("role") == "assistant" and isinstance(msg.get("tool_calls"), list):
            for call in msg["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                _emit(str(call.get("id") or ""), fn.get("name"), fn.get("arguments"))
            continue

        if msg.get("role") == "tool":
            entry = pending_by_id.get(str(msg.get("tool_call_id") or ""))
            if entry is not None:
                text = _text_of_content(msg.get("content"))
                entry["descriptor"] = _descriptor(entry["name"], text, None, None)
            continue

    return out


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------

_NOTE = (
    "Earlier messages in this conversation were summarized to free context — often\n"
    "in the middle of ongoing work. Nothing is lost: retrieve any tool result in\n"
    "full with read_tool_result(ref).\n"
    "The summary below is a lossy digest written by a separate pass that could not\n"
    "see your unfinished plans, so it may understate what remains. Judge what is\n"
    "still unverified or unexplored against the verbatim user instructions and keep\n"
    "working until the request is actually satisfied — a polished summary is not\n"
    "evidence that the work is done. Do not redo work Findings already covers."
)


def render_compacted_context(
    *,
    narrative: str,
    compacted_at: str,
    instructions: list[str],
    tool_index: list[dict],
) -> str:
    """The ``<compacted_context>`` payload, exactly as COMPACTION.md §3 specifies.

    Deliberately flat. No per-message ``seq`` or timestamp (document order already
    encodes sequence); no steer/queued distinction (both are just user messages by
    the time the model sees them); narrative LAST so ``Current State`` / ``Next
    Steps`` sit adjacent to the live conversation that follows.
    """
    lines: list[str] = [f'<compacted_context compacted_at="{compacted_at}">', ""]
    lines.append("<note>")
    lines.append(_NOTE)
    lines.append("</note>")

    if instructions:
        lines.append("")
        lines.append('<user_instructions verbatim="true">')
        for instruction in instructions:
            lines.append(
                "<message>"
                + escape_envelope_delimiters(instruction)
                + "</message>"
            )
        lines.append("</user_instructions>")

    if tool_index:
        lines.append("")
        lines.append("<tool_calls>")
        for entry in tool_index:
            ref = _escape_attribute(str(entry.get("ref") or ""))
            name = _escape_attribute(str(entry.get("name") or ""))
            args = _escape_attribute(str(entry.get("args") or ""))
            body = escape_envelope_delimiters(str(entry.get("descriptor") or ""))
            lines.append(
                f"<call ref=\"{ref}\" name=\"{name}\" args='{args}'>{body}</call>"
            )
        lines.append("</tool_calls>")

    lines.append("")
    lines.append("<narrative>")
    lines.append(escape_envelope_delimiters(narrative or "").strip())
    lines.append("</narrative>")
    lines.append("")
    lines.append("</compacted_context>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _split_at_anchor(messages: list, anchor: tuple[int, int]) -> tuple[list, list, dict]:
    """Split the walked chain at the anchor into ``(before, after, anchor_block)``.

    ``before`` keeps the anchor message's blocks up to the anchor (they feed the
    mechanical collectors); ``after`` starts with the blocks that follow it.

    ``reasoning_details_per_round`` is indexed by EMISSION, so slicing an
    assistant message's blocks has to slice its reasoning slots by the same
    amount or every post-cut emission would be handed the wrong round's
    ``rs_*`` items. The emission count is derived by running the real
    ``_expand_assistant`` over the dropped prefix and counting the assistant
    messages it produces — an exact answer that cannot drift from the expander's
    own flush rules the way a hand-written mirror would. The flat legacy
    ``reasoning_details`` carrier is dropped for a split message: it is the union
    of ALL rounds, including dropped ones, and would re-attach pre-cut items to a
    post-cut emission.
    """
    from open_webui.utils.messages import _expand_assistant  # local: cycle

    mi, bi = anchor
    anchor_msg = messages[mi]
    blocks = anchor_msg.get("content_blocks") or []
    anchor_block = blocks[bi] if isinstance(blocks[bi], dict) else {}

    before_blocks = [b for b in blocks[:bi] if isinstance(b, dict)]
    after_blocks = [b for b in blocks[bi + 1 :] if isinstance(b, dict)]

    before: list = list(messages[:mi])
    if before_blocks:
        before.append({**anchor_msg, "content_blocks": before_blocks})

    after: list = []
    if after_blocks:
        tail = {**anchor_msg, "content_blocks": after_blocks}
        if before_blocks:
            per_round = anchor_msg.get("reasoning_details_per_round")
            if isinstance(per_round, list):
                dropped = sum(
                    1
                    for m in _expand_assistant(before_blocks)
                    if m.get("role") == "assistant"
                )
                tail["reasoning_details_per_round"] = per_round[dropped:]
            tail.pop("reasoning_details", None)
        after.append(tail)
    after.extend(messages[mi + 1 :])
    return before, after, anchor_block


def apply_compaction_to_messages(messages: Optional[list]) -> Optional[list]:
    """Apply the last applicable compaction to a walked message chain.

    Returns a NEW list — leading system messages verbatim, then the
    ``<compacted_context>`` user message, then everything after the cut — or
    ``None`` when there is no anchor (the overwhelmingly common case, and a
    single scan).

    COMPACTION.md §1: the summary is injected as the FIRST USER MESSAGE, not
    appended to the system prompt (which is what upstream does). The system
    message is the very front of the cache prefix, so appending there invalidates
    system AND tool definitions on every compaction; injecting at the cut
    invalidates only from the cut forward.
    """
    if not isinstance(messages, list) or not messages:
        return None
    anchor = find_last_compaction(messages)
    if anchor is None:
        return None

    before, after, anchor_block = _split_at_anchor(messages, anchor)

    # Leading system messages ride in front of the envelope, byte-identical to an
    # uncompacted request. Only the LEADING run is kept: a system message that
    # appears later in the chain (e.g. the tool-round-cap notice) belongs to the
    # span being summarized.
    system_messages = []
    for msg in before:
        if isinstance(msg, dict) and msg.get("role") == "system":
            system_messages.append(msg)
        else:
            break

    carried_instructions: list[str] = []
    carried_tools: list[dict] = []
    for msg in before:
        carry = msg.get(_CARRY_KEY) if isinstance(msg, dict) else None
        if isinstance(carry, dict):
            carried_instructions = list(carry.get("instructions") or [])
            carried_tools = list(carry.get("tool_index") or [])

    instructions = carried_instructions + collect_user_instructions(before)
    tool_index = carried_tools + collect_tool_index(before)

    envelope = render_compacted_context(
        narrative=anchor_block.get("narrative") or "",
        compacted_at=anchor_block.get("compacted_at") or "",
        instructions=instructions,
        tool_index=tool_index,
    )

    # Where the anchor lives, so the HTTP boundary can persist THESE bytes back
    # onto that block (`capture_compaction_envelope`). The UI has to be able to
    # show what was actually sent, not a re-render that merely ought to match.
    anchor_msg = messages[anchor[0]]
    injected = {
        "role": "user",
        "content": envelope,
        _CARRY_KEY: {
            "instructions": instructions,
            "tool_index": tool_index,
            "anchor_message_id": (
                anchor_msg.get("id") if isinstance(anchor_msg, dict) else None
            ),
            "anchor_block_index": anchor[1],
        },
    }
    return [*system_messages, injected, *after]


# ---------------------------------------------------------------------------
# The summarizer call
# ---------------------------------------------------------------------------

SUMMARIZER_INSTRUCTION = """\
The conversation above is about to be compacted to free context. Write the \
narrative that will replace it.

Retain maximum detail. There is no length limit — err on the side of too much \
detail rather than too little. Everything you leave out is gone.

Do not restate the user's requests as a to-do list: the list of user \
instructions and the index of every tool call are re-attached mechanically and \
do not need to be in your output. Write only the narrative.

Anchor every completed action in the past tense with what was actually done \
("Fetched the OpenRouter reasoning docs and confirmed encrypted items carry an \
rs_ id"), never as an instruction to do it. Work already finished must not read \
as work still pending.

The symmetric rule matters just as much: work still pending must never read as \
finished. If the conversation ends mid-investigation — the last messages are \
tool calls and results rather than a final answer — then Next Steps must list \
the specific searches, fetches, and verifications still open or planned, \
including anything the assistant said it would do and has not yet done. Write \
"compose the final response" as the only next step ONLY if the assistant had \
explicitly concluded its research; when in doubt, list what remains unverified \
instead. A large volume of findings is not evidence that research is complete.

If a tool result appears truncated or empty, that is a display artifact from \
context management: the tool executed fully and its output can be retrieved with \
read_tool_result.

Use exactly these four sections, in this order:

## Findings
What was learned. Concrete facts, file paths, identifiers, numbers, quotes.

## Decisions
What was decided and why, including options that were considered and rejected.

## Current State
Where things stand right now — what exists, what works, what is broken.

## Next Steps
What remains to be done, in order. Imperative present tense ("Verify X", \
"Fetch Y"), never past tense — these are instructions to your future self, not \
a record.

If a previous compaction narrative appears earlier in this conversation, MERGE \
with it rather than appending: preserve detail that is still true, drop what has \
been resolved or superseded, integrate the new facts.

Output the four sections and nothing else — no preamble, no closing remark.\
"""


def build_summarizer_messages(api_messages: list) -> list:
    """Append the summarization instruction to the RAW conversation.

    LibreChat's ``summarizeWithCacheHit()`` shape (COMPACTION.md §6): keeping the
    conversation byte-identical in front of the instruction means the
    system+tools prefix still hits the provider's prompt cache. Only valid
    because the summarizer model IS the chat model — which is our configuration.
    """
    return [*(api_messages or []), {"role": "user", "content": SUMMARIZER_INSTRUCTION}]


class CompactionError(Exception):
    """The summarizer produced nothing usable. Never silently swallowed."""


def _degrade_tool_results(api_messages: list, fraction: float) -> list:
    """Blank the oldest ``fraction`` of tool-result bodies, keeping a marker.

    COMPACTION.md §6 failure handling: do NOT fall back to full history — if we
    are compacting because we are over the limit, an uncompacted request just
    fails upstream anyway. goose retries stripping progressively more tool
    responses from the middle (0/10/20/50/100%) before hard-erroring; this is the
    same idea with one step, oldest-first (Codex's ordering), which is the half
    the narrative is least likely to still need verbatim.
    """
    tool_indices = [
        i
        for i, m in enumerate(api_messages)
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    if not tool_indices:
        return api_messages
    strip_count = int(len(tool_indices) * fraction)
    if strip_count <= 0:
        return api_messages
    strip = set(tool_indices[:strip_count])
    out = []
    for i, msg in enumerate(api_messages):
        if i in strip and isinstance(msg, dict):
            out.append(
                {
                    **msg,
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "[Tool output omitted from this summarization "
                                "request to fit the context window. The full "
                                "output is still stored and retrievable with "
                                "read_tool_result.]"
                            ),
                        }
                    ],
                }
            )
        else:
            out.append(msg)
    return out


async def generate_compaction_narrative(
    request: Any,
    user: Any,
    *,
    model_id: str,
    api_messages: list,
) -> str:
    """Call the chat's CURRENT model to write the narrative. Raises on failure.

    COMPACTION.md §6:

    * **the chat's own model**, so LibreChat's cache-hit trick is valid;
    * **no ``max_tokens``** — the instruction is to retain maximum detail, and a
      cap here is precisely how upstream's #27604 ended up persisting a truncated
      ``reasoning_content`` blob as somebody's conversation summary;
    * non-streaming, because there is nothing to stream to — this call is
      invisible to the transcript;
    * on failure, degrade (strip the oldest half of the tool bodies) and retry
      once, then raise. Never silently continue with full history.
    """
    from open_webui.utils.chat import generate_chat_completion  # local: cycle

    attempts = (
        ("full", api_messages),
        ("degraded", _degrade_tool_results(api_messages, 0.5)),
    )
    last_error: Optional[Exception] = None
    for label, messages in attempts:
        try:
            response = await generate_chat_completion(
                request,
                {
                    "model": model_id,
                    "messages": build_summarizer_messages(messages),
                    "stream": False,
                    # Tag it as a task. `utils/chat.py` merges
                    # `request.state.metadata` (chat_id, message_id, …) into
                    # every call made during a live turn, and `routers/openai.py`
                    # fires background TITLE GENERATION for any request that has
                    # a chat_id and NO task — which this one would otherwise
                    # trip on every compaction. The merge lets request.state win
                    # on collisions, and it never carries a `task`, so this key
                    # survives.
                    "metadata": {"task": "compaction"},
                },
                user,
                bypass_filter=True,
            )
            return extract_summary(response)
        except Exception as exc:  # noqa: BLE001 — recorded and re-raised below
            last_error = exc
            log.warning(
                "compaction summarizer attempt (%s) failed: %s", label, exc
            )
    raise CompactionError(
        f"summarizer failed after degrade retry: {last_error}"
    ) from last_error


async def compact_content_blocks(
    request: Any,
    user: Any,
    *,
    model_id: str,
    api_messages: list,
    content_blocks: Optional[list],
    total_tokens: Optional[int] = None,
    context_length: Optional[int] = None,
    completed: bool = False,
) -> tuple[list, dict]:
    """Run one compaction: summarize, build the anchor, splice it in.

    ``completed`` says whether ``content_blocks`` belongs to a FINISHED assistant
    message (turn-start gate, ``/compact``) or one that is still streaming
    (mid-turn gate). It selects the cut index — see ``find_cut_index``.

    Returns ``(new_content_blocks, block)``. Persistence is the caller's job —
    the anchor has to land through whichever write path already owns this
    message (the streaming checkpoint mid-turn, the message upsert between
    turns) so the dense invariant and the ``tool_result_bodies`` union both stay
    enforced by the machinery that already guarantees them.

    ``covers`` is the size of the span the summarizer was given (non-system API
    messages). It is display-only — the divider in §8 reads "Context compacted ·
    47 messages" — and is deliberately approximate at the tail: a handful of
    messages after the cut point are also in front of the summarizer, because the
    cache-hit invocation shape (§6) hands it the RAW conversation.
    """
    narrative = await generate_compaction_narrative(
        request, user, model_id=model_id, api_messages=api_messages
    )
    covers = sum(
        1
        for m in (api_messages or [])
        if isinstance(m, dict) and m.get("role") != "system"
    )
    block = make_compaction_block(
        narrative,
        covers=covers,
        tokens=total_tokens,
        context_length=context_length,
    )
    new_blocks = insert_compaction_block(content_blocks, block, completed=completed)
    # Return the anchor that is actually IN the list, not the template it was
    # built from — `insert_compaction_block` copies, and callers (the mid-turn
    # envelope capture, the manual command's response) need the real object.
    anchor_index = find_cut_index(
        [b for b in (content_blocks or []) if isinstance(b, dict)],
        completed=completed,
    )
    return new_blocks, new_blocks[anchor_index]


async def maybe_compact_at_turn_start(
    request: Any,
    user: Any,
    *,
    chat_id: str,
    model: Optional[dict],
    api_messages: list,
    chain: Optional[list],
    response_message_id: Optional[str] = None,
) -> bool:
    """The inter-turn half of the gate. Returns True when an anchor was persisted.

    Called from ``main.py`` right after ``assemble_conversation_from_leaf``, on
    the conversation that is ABOUT to be sent. The last response's usage is read
    off the previous assistant message (``meta.usage``, written by the streaming
    finaliser as the LAST round's payload — not a running sum), and the anchor is
    written into that same message so the new user prompt, which comes after it,
    survives the cut.

    Everything here is best-effort by construction: the caller wraps it and a
    failure leaves the turn to proceed uncompacted, where the provider's own
    context-length error is what surfaces.
    """
    from open_webui.env import COMPACTION_THRESHOLD, ENABLE_CONVERSATION_COMPACTION
    from open_webui.models.chats import Chats
    from open_webui.utils.context_window import resolve_context_length

    if not ENABLE_CONVERSATION_COMPACTION:
        return False
    if not chat_id or str(chat_id).startswith("local:"):
        # No durable message row to anchor to; the mid-turn path still applies
        # in-memory for the current turn.
        return False

    context_length = resolve_context_length(model)
    if context_length is None:
        return False

    # Anchor into the last assistant message, and ONLY when something newer
    # follows it in the chain (normally the user prompt that just arrived). If
    # the chain ENDS on an assistant message this is a regenerate/resume, whose
    # message is about to be rewritten in place by the turn we're front-running
    # — anchoring into it would race the stream handler's own block list. The
    # mid-turn gate covers that case on the next round instead.
    chain = list(chain or [])
    target = None
    for idx in range(len(chain) - 2, -1, -1):
        msg = chain[idx]
        if (
            isinstance(msg, dict)
            and msg.get("role") == "assistant"
            and isinstance(msg.get("content_blocks"), list)
            and msg.get("id")
        ):
            target = msg
            break
    if target is None:
        return False

    total_tokens = usage_total_tokens(target.get("usage"))
    if not should_compact(total_tokens, context_length, COMPACTION_THRESHOLD):
        return False
    if not has_uncompacted_span(chain):
        return False

    model_id = (model or {}).get("id")
    if not model_id:
        return False

    # NO progress/completion status for a successful compaction. `status` events
    # are appended to the message's `statusHistory` and PERSISTED, so the pair
    # "Compacting conversation context" / "Compacted 34 messages" became a
    # permanent second copy of information the divider block already carries —
    # sitting next to it in the transcript forever. The anchor block IS the
    # record; it renders `Context compacted · 34 messages · 226k tokens · 17:33`
    # and expands to the exact envelope. Only a FAILURE still emits (below): that
    # is information no block records, because no block gets written.
    try:
        new_blocks, block = await compact_content_blocks(
            request,
            user,
            model_id=model_id,
            api_messages=api_messages,
            content_blocks=target.get("content_blocks"),
            total_tokens=total_tokens,
            context_length=context_length,
            # The target is a COMPLETED assistant message: the cut belongs after
            # everything it contains, which is where the compaction actually
            # happened (between that turn and the one now starting).
            completed=True,
        )
    except Exception:
        await emit_compaction_status(
            user,
            chat_id=chat_id,
            message_id=response_message_id,
            description="Context compaction failed — continuing with full history",
            done=True,
        )
        raise
    await Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        str(target["id"]),
        {"content_blocks": new_blocks},
        return_model=False,
    )
    # The anchor went into the PREVIOUS assistant message. `chat:completion`
    # only ever carries the in-flight one, so without this push an open tab
    # renders that message from its pre-cut copy and never shows the divider.
    await broadcast_compaction_anchor(
        user, chat_id=chat_id, message_id=str(target["id"]), content_blocks=new_blocks
    )
    log.info(
        "compaction (turn start): chat=%s message=%s covers=%s tokens=%s/%s",
        chat_id,
        target.get("id"),
        block.get("covers"),
        total_tokens,
        context_length,
    )
    return True


async def broadcast_compaction_anchor(
    user: Any,
    *,
    chat_id: str,
    message_id: str,
    content_blocks: list,
) -> None:
    """Push a new anchor to the user's open tabs. Best-effort, never raises.

    Needed by every path that writes an anchor into a message the CURRENT stream
    does not own — the manual command (nothing is streaming) and the turn-start
    gate (which anchors into the PREVIOUS assistant, while `chat:completion`
    only ever carries the in-flight one). Without it an open tab keeps rendering
    that message from the copy it loaded before the cut and the divider simply
    never appears; only a reload reveals it.
    """
    try:
        from open_webui.socket.main import get_event_emitter

        emitter = get_event_emitter(
            {
                "user_id": getattr(user, "id", None),
                "chat_id": chat_id,
                "message_id": message_id,
            },
            False,
        )
        if emitter:
            await emitter(
                {
                    "type": "chat:message:compacted",
                    "data": {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "content_blocks": content_blocks,
                    },
                }
            )
    except Exception:
        log.exception("compaction broadcast failed for chat %s", chat_id)


async def emit_compaction_status(
    user: Any,
    *,
    chat_id: str,
    message_id: Optional[str],
    description: str,
    done: bool,
) -> None:
    """Surface a compaction outcome that no block can record.

    Reserved for FAILURE now. A successful compaction writes an anchor block, and
    that divider — `Context compacted · 34 messages · 226k tokens · 17:33`,
    expandable to the exact envelope — is the record. Statuses are appended to the
    message's persisted ``statusHistory``, so narrating a success here left a
    permanent duplicate of the divider parked beside it in the transcript.

    A failure writes nothing, so it has to say so: the turn continues with full
    history and will hit the provider's own context-length error, which is a very
    different thing to be looking at than a cut that worked.

    (Removed with the success statuses: turn-start compaction runs BEFORE the
    first upstream call and summarizing a 350k-token conversation takes ~90
    seconds (measured), which is now 90 seconds of a send with nothing on screen.
    If that reads as hung again, the fix is an EPHEMERAL indicator — not another
    `status` event, which is durable by construction.)
    """
    if not message_id:
        return
    try:
        from open_webui.socket.main import get_event_emitter

        emitter = get_event_emitter(
            {
                "user_id": getattr(user, "id", None),
                "chat_id": chat_id,
                "message_id": message_id,
            },
            True,
        )
        if emitter:
            await emitter(
                {
                    "type": "status",
                    "data": {
                        "action": "compaction",
                        "description": description,
                        "done": done,
                    },
                }
            )
    except Exception:
        log.exception("compaction status emit failed for chat %s", chat_id)


COMPACT_COMMAND = "/compact"


def is_compact_command(text: Optional[str]) -> bool:
    """True when a composer message is the manual compaction command.

    Exact match only, case-insensitive, after stripping. A message that merely
    STARTS with ``/compact`` ("/compact the logs into one file") is an ordinary
    instruction and must be delivered as one — silently eating a real request
    because it shares a prefix is far worse than making the user send a bare
    command.
    """
    return isinstance(text, str) and text.strip().lower() == COMPACT_COMMAND


class NothingToCompactError(Exception):
    """No uncompacted span exists on this branch. Reported, never swallowed."""


async def compact_chat_now(
    request: Any,
    user: Any,
    *,
    chat_id: str,
    model: Optional[dict],
    leaf_id: Optional[str] = None,
) -> dict:
    """Manual compaction — the ``/compact`` command, run against a chat at rest.

    Same cut, same envelope, same anchor as the automatic gate. What it drops is
    the POLICY, and only the policy:

    * the ``should_compact`` threshold — the whole point of asking;
    * an unresolvable ``context_length`` — that number is display-only on the
      block, so not knowing the window is no reason to refuse.

    ``has_uncompacted_span`` is KEPT. It is not policy but arithmetic: with
    nothing after the last anchor there is nothing to summarize, and compacting
    anyway would write a second anchor whose envelope restates the first. The
    caller gets ``NothingToCompactError`` and tells the user, rather than paying
    for a summarizer call that can only produce a duplicate.

    Returns ``{"message_id", "block_index", "block", "content_blocks"}`` so the
    caller can splice the divider into a live view without a reload.
    """
    from open_webui.models.chats import Chats
    from open_webui.utils.chat import _walk_messages_from_leaf
    from open_webui.utils.context_window import resolve_context_length

    if not chat_id or str(chat_id).startswith("local:"):
        raise CompactionError("compaction needs a saved chat")

    model_id = (model or {}).get("id")
    if not model_id:
        raise CompactionError("no model to summarize with")

    # Refuse while a turn is live. This path writes the whole ``content_blocks``
    # list, which would truncate whatever the stream appended in between — the
    # same hazard `record_sent_envelope` sidesteps. A `/compact` during a turn is
    # supposed to arrive as a steer and be taken at a tool-round boundary; only
    # an odd composer state (files in flight) can route one here instead.
    try:
        from open_webui.socket.main import get_active_streams_for_chat
        from open_webui.tasks import has_active_generation_operations

        redis = getattr(getattr(request, "app", None), "state", None)
        if await has_active_generation_operations(
            getattr(redis, "redis", None), chat_id
        ) or get_active_streams_for_chat(chat_id):
            raise CompactionError(
                "a response is still generating — steer /compact instead"
            )
    except CompactionError:
        raise
    except Exception:
        log.debug("compact liveness check failed for %s", chat_id, exc_info=True)

    messages_map = await Chats.get_messages_map_by_chat_id(chat_id) or {}
    if not leaf_id:
        chat = await Chats.get_chat_by_id(chat_id)
        history = (chat.chat or {}).get("history") if chat else None
        leaf_id = (history or {}).get("currentId")
    chain = _walk_messages_from_leaf(messages_map, leaf_id) if leaf_id else []
    if not chain:
        raise CompactionError("chat has no messages to compact")
    if not has_uncompacted_span(chain):
        raise NothingToCompactError()

    # Anchor into the LAST assistant message on the branch. Unlike the turn-start
    # gate there is no "something must follow it" rule: nothing is streaming, so
    # the leaf itself is a safe target and is usually what we want — everything
    # up to and including the answer just given is what the user is asking to
    # collapse.
    target = None
    for msg in reversed(chain):
        if (
            isinstance(msg, dict)
            and msg.get("role") == "assistant"
            and isinstance(msg.get("content_blocks"), list)
            and msg.get("id")
        ):
            target = msg
            break
    if target is None:
        raise CompactionError("no assistant message to anchor a compaction to")

    api_messages = await assemble_for_summary(chat_id, leaf_id, model, request, user)
    new_blocks, block = await compact_content_blocks(
        request,
        user,
        model_id=model_id,
        api_messages=api_messages,
        content_blocks=target.get("content_blocks"),
        total_tokens=usage_total_tokens(target.get("usage")),
        context_length=resolve_context_length(model),
        # Nothing is streaming (the liveness check above refuses otherwise), so
        # the leaf is complete and the cut belongs at its end — which is what the
        # paragraph above already promises: "everything up to and including the
        # answer just given is what the user is asking to collapse".
        completed=True,
    )
    await Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id,
        str(target["id"]),
        {"content_blocks": new_blocks},
        return_model=False,
    )
    log.info(
        "compaction (manual): chat=%s message=%s covers=%s",
        chat_id,
        target.get("id"),
        block.get("covers"),
    )

    await broadcast_compaction_anchor(
        user, chat_id=chat_id, message_id=str(target["id"]), content_blocks=new_blocks
    )

    return {
        "message_id": str(target["id"]),
        "block_index": next(
            i for i, b in enumerate(new_blocks) if b is block
        ),
        "block": block,
        "content_blocks": new_blocks,
    }


async def assemble_for_summary(
    chat_id: str,
    leaf_id: Optional[str],
    model: Optional[dict],
    request: Any,
    user: Any,
) -> list:
    """The conversation as the model would see it, for the summarizer to read.

    Deliberately the SAME assembly the send path uses (§6: the summarizer is
    handed the raw conversation so the system+tools prefix still hits the
    provider's prompt cache), not a bespoke projection.
    """
    from open_webui.utils.chat import assemble_conversation_from_leaf

    return await assemble_conversation_from_leaf(
        chat_id,
        leaf_id,
        new_user_message=None,
        model=model,
        request=request,
        user=user,
    )


def extract_summary(response: Any) -> str:
    """Pull the narrative out of a chat-completion response, or raise.

    Upstream's #27604 is the cautionary tale (COMPACTION.md §6): they hardcoded
    ``max_tokens=1000``, silently accepted ``finish_reason: "length"``, and stored
    the truncated **``reasoning_content``** as the summary. So:

    * message ``content`` ONLY — ``reasoning`` / ``reasoning_content`` are never
      a fallback, no matter how tempting an empty content field makes it;
    * ``finish_reason == "length"`` is a hard refusal (we send no ``max_tokens``,
      so hitting it means the model's own ceiling truncated the narrative);
    * empty or whitespace-only content is a hard refusal.
    """
    if not isinstance(response, dict):
        raise CompactionError(f"summarizer returned {type(response).__name__}")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise CompactionError("summarizer response had no choices")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise CompactionError(
            "summarizer response was truncated (finish_reason=length)"
        )
    message = choice.get("message")
    if not isinstance(message, dict):
        raise CompactionError("summarizer response had no message")
    content = message.get("content")
    if isinstance(content, list):
        content = _text_of_content(content)
    if not isinstance(content, str) or not content.strip():
        # Deliberately NOT falling back to message.get("reasoning_content").
        raise CompactionError("summarizer produced empty message content")
    return content.strip()
