"""Content-block inspection/finalization helpers shared by the streaming turn pipeline.

Extracted verbatim from utils/middleware.py (2026-08-02 de-spaghettification).
"""

import time
from typing import Any


def _total_text_block_len(content_blocks) -> int:
    """Sum the length of every ``text`` block's content. Used to detect whether a
    model round produced any visible assistant text (an order-independent signal
    that survives the trailing-empty-text-block cleanup stream_body_handler does).
    """
    total = 0
    for block in content_blocks or []:
        if isinstance(block, dict) and block.get("type") == "text":
            c = block.get("content")
            if isinstance(c, str):
                total += len(c.strip())
    return total


def _finalize_open_agentic_blocks(content_blocks):
    """Stamp ended_at/duration on EVERY reasoning/tool_calls block still open when
    the stream was interrupted (user cancel or terminal error). Returns True when
    something was actually closed, so callers can skip a no-op emit.

    Normal completion already finalizes reasoning (first text token / end of
    stream) and tool_calls (when results attach), so this only matters for the
    cancel/error paths. Without it a reasoning block keeps `duration == null`,
    which is the ONLY thing the UI reads to decide between "Thought for N seconds"
    and a spinning "Thinking…" — see `blocksToDisplayMarkdown` (src/lib/utils) and
    the `attributes.done !== 'true'` shimmer in Collapsible.svelte. A frozen,
    persisted message would spin forever.

    Sweeps the whole list rather than just the tail. The tail is where the open
    block normally is, but "there is exactly one and it is last" is an assumption
    about streaming order, not an invariant — and the cost of being wrong is a
    dangling clock that survives every reload. A block with a `started_at` and no
    `ended_at` after the turn is over is dangling wherever it sits.

    Idempotent and safe to call on any content_blocks list.
    """
    closed = False
    for block in content_blocks or []:
        if not isinstance(block, dict):
            continue
        if (
            block.get("type") in ("reasoning", "tool_calls")
            and block.get("started_at") is not None
            and block.get("ended_at") is None
        ):
            block["ended_at"] = time.time()
            block["duration"] = max(
                0, int(block["ended_at"] - block["started_at"])
            )
            closed = True
    return closed


def _visible_reasoning_from_details(reasoning_details: Any) -> str:
    if not isinstance(reasoning_details, list):
        return ""

    parts: list[str] = []
    for item in reasoning_details:
        if not isinstance(item, dict):
            continue
        for key in ("summary", "text"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return "\n\n".join(parts).strip()


async def _visible_nonstreaming_reasoning(message: dict) -> str:
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _visible_reasoning_from_details(message.get("reasoning_details"))
