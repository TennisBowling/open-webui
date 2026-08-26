"""Streamed-content accumulation: text/reasoning block growth and reasoning_details fragment merging.

Extracted verbatim from utils/middleware.py (2026-08-02 de-spaghettification).
The byte-exactness rules here are load-bearing: persisted content is
replayed to the provider on later turns, so any deviation from what the
model generated corrupts text AND breaks prompt-cache reuse. See
merge_streamed_field for the one permitted dedupe (cumulative full-prefix
resend).
"""

from open_webui.utils.tool_calling import merge_streamed_field


class _StreamTextAccumulator:
    """O(1)-amortized accumulator for a streaming text/reasoning block's growing
    `content` string.

    The streaming hot path used to grow the tail block with
    ``block["content"] = block["content"] + value`` every token. That is a
    dict-subscript concatenation: CPython's in-place ``+=`` optimization never
    applies (it is limited to local-variable targets, and per-token snapshot
    sharing held extra references anyway), so it reallocated a length-N string
    every token — O(N^2) per stream, multiple seconds of pure event-loop block
    on long responses, which starved the socket delta flush and produced the
    "trickle, long stall, burst at completion" symptom.

    This accumulator keeps appended chunks in a list and joins lazily:
      * ``append(value)``     — O(len(value)); never touches the joined string.
      * ``take_appended()``   — O(new chars); returns text appended since the
                                last call, for emitting one ``text_append`` delta.
      * ``materialize()``     — O(current length); folds the list into one string
                                and returns it. Call this only when a reader
                                actually needs the whole string (snapshot,
                                checkpoint, block boundary, finalize), NOT per
                                token — the cadence keeps it K-bounded.

    Invariants (verified in tests):
      materialize()                         == every value append()ed, in order.
      concat of all take_appended() results == materialize()  (no loss / dup).
    """

    __slots__ = ("_parts", "_emit_idx", "_len")

    def __init__(self, initial: str = ""):
        self._parts: list[str] = [initial] if initial else []
        # Index into _parts of the first chunk NOT yet returned by take_appended.
        # `initial` represents content already present on the block at stream
        # start (already known to the client mirror / snapshot), so it begins
        # life as already-emitted: the cursor starts PAST it. Contract:
        #   initial + concat(take_appended() calls) == materialize()
        self._emit_idx: int = len(self._parts)
        self._len: int = len(initial)

    def append(self, value: str) -> None:
        if not value:
            return
        self._parts.append(value)
        self._len += len(value)

    def take_appended(self) -> str:
        """Return text appended since the last take_appended(), advancing the
        emit cursor. Used to ship exactly one delta's worth of new text."""
        if self._emit_idx >= len(self._parts):
            return ""
        appended = "".join(self._parts[self._emit_idx :])
        self._emit_idx = len(self._parts)
        return appended

    @property
    def has_unemitted(self) -> bool:
        return self._emit_idx < len(self._parts)

    @property
    def pending_len(self) -> int:
        """Char count appended since the last take_appended() (NOT consuming).
        Used by the flush-time coalescing gate to decide whether enough tokens
        have accumulated to be worth one versioned text_append delta. O(pending
        parts), which the coalesce window keeps small."""
        if self._emit_idx >= len(self._parts):
            return 0
        return sum(len(p) for p in self._parts[self._emit_idx :])

    def materialize(self) -> str:
        """Fold to a single string and return it. Collapses the parts list so
        future appends stay cheap, while PRESERVING the emit cursor's logical
        position — critical because a reader (e.g. a checkpoint) can materialize
        AFTER a token was appended but BEFORE the flush emitted it as a delta.
        Collapsing that un-emitted tail to 'emitted' would drop it from the wire
        (live text loss); collapsing to 'un-emitted' would re-ship already-sent
        text (duplication). So we split at the cursor into [emitted, unemitted]."""
        if not self._parts:
            return ""
        if self._emit_idx <= 0:
            joined = "".join(self._parts)
            self._parts = [joined] if joined else []
            self._emit_idx = 0
            return joined
        if self._emit_idx >= len(self._parts):
            joined = "".join(self._parts)
            self._parts = [joined] if joined else []
            self._emit_idx = len(self._parts)
            return joined
        emitted = "".join(self._parts[: self._emit_idx])
        unemitted = "".join(self._parts[self._emit_idx :])
        self._parts = []
        if emitted:
            self._parts.append(emitted)
        self._emit_idx = len(self._parts)
        if unemitted:
            self._parts.append(unemitted)
        return emitted + unemitted

    def __len__(self) -> int:
        return self._len


def _append_reasoning_delta(acc: "_StreamTextAccumulator", chunk: str) -> int:
    """Append one streamed reasoning delta to the accumulator, defending
    against providers that resend cumulative content (the full reasoning-so-far
    in each chunk). Returns the number of chars actually appended, for
    checkpoint accounting.

    A cumulative resend is, by definition, at least as long as the content
    accumulated so far and starts with it byte-for-byte — so anything shorter
    is a true incremental delta and is appended VERBATIM. Partial
    suffix/prefix-overlap dedupe is forbidden here: it cannot be distinguished
    from legitimately repeated text and silently eats real characters
    ("bana"+"na", doubled letters/punctuation split across tokens). The
    persisted reasoning is replayed to the provider on later turns, so a
    single dropped character both corrupts the visible text and breaks
    prompt-cache reuse for the rest of the conversation. See
    ``merge_streamed_field`` — the same rule applied to every other streamed
    field.

    Cost: the full-prefix check materializes the accumulator, but the
    ``len(chunk) >= len(acc)`` guard means an incremental provider only hits
    it near round start (O(1)); a cumulative provider's resend is O(len)
    to read anyway, and materialize() collapses parts so repeated calls stay
    linear per chunk."""
    if not chunk:
        return 0
    if len(chunk) < len(acc):
        acc.append(chunk)
        return len(chunk)
    existing = acc.materialize()
    merged = merge_streamed_field(existing, chunk)
    appended = merged[len(existing) :]
    if appended:
        acc.append(appended)
    return len(appended)


def _apply_reasoning_detail_delta(reasoning_details: list, detail: dict) -> None:
    """Merge ONE streamed ``reasoning_details`` fragment into the accumulated
    list, in place.

    Match by (id, type), adopting an id-less entry of the same (type, index)
    when the provider only emits ``id`` on a later chunk; id-less fragments
    match on (type, index). text/data/summary accumulate via
    ``merge_streamed_field``; id/signature/format/index adopt the latest
    non-null value; ``type`` is part of the match key and is never overwritten.
    Unmatched fragments append.

    Mirrored by the frontend (``mergeReasoningDetail`` in stream-protocol.ts,
    used by both the v2.1 op reducer and the v1 path in Chat.svelte) — keep
    them in lockstep. See utils/REASONING_DETAILS.md §2 (wire protocol) and
    §6 Bug A (why this isn't matched on id alone)."""
    detail_id = detail.get("id")
    detail_type = detail.get("type")
    detail_idx = detail.get("index", 0)

    def _find(pred):
        return next((d for d in reasoning_details if pred(d)), None)

    if detail_id is not None:
        existing = _find(
            lambda d: d.get("id") == detail_id and d.get("type") == detail_type
        )
        if existing is None:
            existing = _find(
                lambda d: d.get("id") is None
                and d.get("type") == detail_type
                and d.get("index") == detail_idx
            )
    else:
        existing = _find(
            lambda d: d.get("type") == detail_type and d.get("index") == detail_idx
        )

    if existing is None:
        reasoning_details.append({**detail})
        return

    for key in ("text", "data", "summary"):
        if detail.get(key):
            existing[key] = merge_streamed_field(existing.get(key) or "", detail[key])
    for key in ("id", "signature", "format", "index"):
        if detail.get(key) is not None:
            existing[key] = detail[key]

class TailAccumulator:
    """Owns the O(1)-amortized accumulation for the CURRENT tail text/reasoning
    block of a streaming turn.

    The active block's ``content`` grows one token per chunk;
    ``block["content"] += value`` per token is a dict-subscript concat — O(N)
    per token, O(N^2) per stream, seconds of pure event-loop block on long
    responses. Instead, appends go to a ``_StreamTextAccumulator`` bound to the
    tail block by IDENTITY, and ``materialize()`` folds the buffer back into
    ``block["content"]`` only at boundaries/readers (checkpoint, snapshot,
    serialize, finalizers). Rebinding to a different block materializes the
    previous one first, so no text is ever stranded in the buffer.
    """

    __slots__ = ("acc", "block")

    def __init__(self):
        self.acc = None
        self.block = None

    def materialize(self):
        """Fold buffered tail text back into its block's ``content`` so every
        cold reader sees the full string. Cheap, idempotent no-op when nothing
        is buffered."""
        if self.acc is not None and self.block is not None:
            self.block["content"] = self.acc.materialize()

    def bind(self, block):
        """Bind the accumulator to ``block`` (the current tail), materializing
        any previously-bound block first. Seeds with the block's existing
        content as already-emitted (the mirror/snapshot already know it —
        contract of ``_StreamTextAccumulator``)."""
        if self.block is block and self.acc is not None:
            return
        self.materialize()
        self.acc = _StreamTextAccumulator(block.get("content", "") or "")
        self.block = block

    def append_text(self, block, value):
        """Append a pure-text delta to the tail block in O(len(value))."""
        if self.block is not block or self.acc is None:
            self.bind(block)
        self.acc.append(value)

    def append_reasoning(self, block, chunk):
        """Append a reasoning delta via ``_append_reasoning_delta`` (byte-exact;
        see its docstring). Returns new-char count for checkpoint accounting."""
        if self.block is not block or self.acc is None:
            self.bind(block)
        return _append_reasoning_delta(self.acc, chunk)
