"""Opt-in cProfile wrapper for the streaming response handler.

``process_chat_response`` is the per-response hot path (token streaming, the
agentic tool-call loop, content-block merging, socket emits). When
``PROFILE_CHAT=1`` we profile each invocation and dump a ``.pstats`` file so the
function-level hot spots can be inspected after a real run (e.g. the user's
~5-subagent research chat).

Caveat worth knowing when reading the output: cProfile is *thread-wide*, not
coroutine-scoped. Because the handler awaits constantly and the single event
loop interleaves other coroutines during those awaits, a profile captured here
also includes whatever else ran on the loop during this response. For the
"where is the loop spending CPU" question that is acceptable (often desirable —
you see the whole loop's cost during the response). It is NOT a clean
wall-clock attribution of this one coroutine. Off by default → zero overhead.
"""

import cProfile
import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)


def _safe(part: Optional[str]) -> str:
    if not part:
        return "na"
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(part))[:64]


def dump_profile(
    profiler: cProfile.Profile,
    out_dir: str,
    chat_id: Optional[str],
    message_id: Optional[str],
    started_monotonic: float,
) -> None:
    """Persist a profiler's stats to ``out_dir`` keyed by chat/message id.

    Never raises — profiling must not break a real response. The elapsed wall
    time (seconds) is encoded in the filename so the slowest runs are easy to
    spot in a directory listing.
    """
    try:
        os.makedirs(out_dir, exist_ok=True)
        elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
        # No Date/random in scope-sensitive code; monotonic ns gives a unique,
        # sortable suffix without wall-clock dependency.
        suffix = int(time.monotonic() * 1000) % 10_000_000
        fname = (
            f"chat_{_safe(chat_id)}_{_safe(message_id)}"
            f"_{elapsed_ms}ms_{suffix}.pstats"
        )
        path = os.path.join(out_dir, fname)
        profiler.dump_stats(path)
        log.info("PROFILE_CHAT: dumped %s (%.0fms wall)", path, elapsed_ms)
    except Exception:
        log.exception("PROFILE_CHAT: failed to dump profile")
