"""Stream v2.1 wire protocol: delta translator, op splitting, emitter wrapper.

Extracted verbatim from utils/middleware.py (2026-08-02 de-spaghettification).
The design rationale banner below is the original in-place documentation.
"""

import os

from open_webui.utils import fast_json as json
from open_webui.socket.main import (
    emit_to_primary,
    set_stream_state,
    stream_version_flush,
    stream_version_get,
    stream_version_incr,
    stream_version_init,
)
from open_webui.utils.lazy_blocks import _strip_tool_results
from open_webui.utils.response_durability import (
    is_selection_metadata_only_completion,
)

# ---------------------------------------------------------------------------
# Stream v2.1 delta translator
# ---------------------------------------------------------------------------
#
# The v1 emitter ships the entire `content_blocks` array on every flush (O(N²)
# bytes per turn). v2.1 ships only what changed since the last emit. To avoid
# rewriting the 1300-line stream loop, we install a translator that diffs the
# incoming content_blocks against a per-message mirror and emits the matching
# `chat:delta` ops. Anything not a content_blocks-bearing `chat:completion`
# event passes through unchanged (status, sources, citations, errors, ...).
#
# Wire Contract #1 (see plan Phase 0) — ops emitted:
#   text_append, block_open, block_close, tool_call_add, replace, sources,
#   selected_model_id. tool_call:result is emitted separately at exec time.

STREAM_TEXT_DELTA_MAX_BYTES = max(
    4096, int(os.environ.get("STREAM_TEXT_DELTA_MAX_BYTES", "262144") or "262144")
)
STREAM_DELTA_MAX_BYTES = max(
    STREAM_TEXT_DELTA_MAX_BYTES,
    int(os.environ.get("STREAM_DELTA_MAX_BYTES", "524288") or "524288"),
)

# Flush-time delta coalescing (metered/slow-link downlink efficiency). With
# CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE=1 the native v2.1 fast-path emits one
# versioned `text_append` delta per provider token — ~14-18B of wire framing per
# ~2-4B of content. The socket batcher groups them into one envelope but each
# still costs its own frame. Coalescing merges CONSECUTIVE tail-block text tokens
# into ONE versioned delta *before* the version is assigned, so N tokens consume
# exactly ONE version number (strictly contiguous — the client's
# `version > mirror.version + 1` gap guard never trips, unlike a post-versioning
# merge which would leave gaps and force a snapshot refetch). A tail append is
# held until either MIN_CHARS accumulate OR WINDOW_MS elapses since the last
# native emit (bounds trickle latency); structural changes (new block, tool call,
# reasoning close) and the terminal flush always emit immediately. Set MIN_CHARS
# to 0 to disable (restores per-token emission).
STREAM_TEXT_COALESCE_MIN_CHARS = max(
    0, int(os.environ.get("STREAM_TEXT_COALESCE_MIN_CHARS", "48") or "48")
)
STREAM_TEXT_COALESCE_WINDOW_S = (
    max(0, int(os.environ.get("STREAM_TEXT_COALESCE_WINDOW_MS", "24") or "24")) / 1000.0
)


def _utf8_len(value: str) -> int:
    return len((value or "").encode("utf-8", "replace"))


def _split_text_by_utf8_bytes(text: str, max_bytes: int = STREAM_TEXT_DELTA_MAX_BYTES):
    text = text or ""
    if not text:
        return []
    if _utf8_len(text) <= max_bytes:
        return [text]

    chunks = []
    current = []
    current_bytes = 0
    for char in text:
        char_bytes = _utf8_len(char)
        if current and current_bytes + char_bytes > max_bytes:
            chunks.append("".join(current))
            current = [char]
            current_bytes = char_bytes
        else:
            current.append(char)
            current_bytes += char_bytes
    if current:
        chunks.append("".join(current))
    return chunks


def _json_size_bytes(value) -> int:
    try:
        return _utf8_len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return STREAM_DELTA_MAX_BYTES + 1


def _split_stream_delta_op(op: dict) -> list[dict]:
    if op.get("op") == "text_append" and isinstance(op.get("text"), str):
        return [
            {**op, "text": text_chunk}
            for text_chunk in _split_text_by_utf8_bytes(op.get("text") or "")
        ]

    if _json_size_bytes(op) > STREAM_DELTA_MAX_BYTES:
        return [{"op": "snapshot", "reason": "delta_too_large"}]

    return [op]


def _emit_delta_for_blocks(
    raw_emit, message_id, mirror, new_blocks, extra_payload=None
):
    """Compute & emit the deltas needed to move the client mirror from
    `mirror['blocks']` to `new_blocks`. Returns a list of awaitables."""
    new_blocks = _strip_tool_results(new_blocks)
    # Bind old_blocks to the LIVE mirror list (not a throwaway via `or []`) so
    # newly-opened blocks appended below actually persist in the mirror. With the
    # previous `mirror.get("blocks") or []`, an empty mirror yielded a fresh list,
    # so block_open appends never reached `mirror["blocks"]`; the mirror stayed
    # empty and every subsequent flush re-ran a full diff from scratch (and the
    # native fast-path, gated on a populated mirror, could never engage). Seed the
    # mirror in place when missing/empty.
    if not isinstance(mirror.get("blocks"), list):
        mirror["blocks"] = []
    old_blocks = mirror["blocks"]
    ops = []

    common = min(len(old_blocks), len(new_blocks))
    structural_rewrite = False
    for i in range(common):
        if old_blocks[i].get("type") != new_blocks[i].get("type"):
            structural_rewrite = True
            break

    if structural_rewrite:
        ops.append(
            {
                "op": "replace",
                "block_idx": 0,
                "content_blocks": new_blocks,
            }
        )
        mirror["blocks"] = [dict(b) for b in new_blocks]
    else:
        # Per-block diff for the prefix; new blocks beyond `common` are opened.
        for i in range(common):
            old_b = old_blocks[i]
            new_b = new_blocks[i]
            btype = new_b.get("type")
            # Native fast-path coordination: when the streaming loop emitted text
            # for this block directly (bypassing this translator), it advanced an
            # `_emitted_len` cursor on the mirror block WITHOUT refreshing the
            # mirror's `content` string (refreshing it per token would reintroduce
            # the O(N^2) concat). The client has therefore received exactly
            # `_emitted_len` chars of this block. Trust that cursor over the stale
            # `content` string so we diff against what the client actually has —
            # otherwise we'd re-emit the gap as a duplicate text_append. This makes
            # the native/translator handoff correct at EVERY translator entry point
            # (round-boundary emits, usage/error flushes, etc.), not just the ones
            # that pre-reconcile.
            if btype in ("text", "reasoning"):
                emitted_len = old_b.get("_emitted_len")
                if emitted_len is not None:
                    new_full = new_b.get("content", "") or ""
                    old_b["content"] = (
                        new_full[:emitted_len]
                        if len(new_full) >= emitted_len
                        else new_full
                    )
                    old_b.pop("_emitted_len", None)
            if btype == "text":
                old_text = old_b.get("content", "") or ""
                new_text = new_b.get("content", "") or ""
                if new_text == old_text:
                    continue
                if new_text.startswith(old_text):
                    appended = new_text[len(old_text) :]
                    if appended:
                        ops.append(
                            {
                                "op": "text_append",
                                "block_idx": i,
                                "text": appended,
                            }
                        )
                else:
                    ops.append(
                        {
                            "op": "replace",
                            "block_idx": i,
                            "content_blocks": [new_b],
                        }
                    )
                old_b["content"] = new_text
            elif btype == "reasoning":
                old_text = old_b.get("content", "") or ""
                new_text = new_b.get("content", "") or ""
                if new_text != old_text and new_text.startswith(old_text):
                    appended = new_text[len(old_text) :]
                    if appended:
                        ops.append(
                            {
                                "op": "text_append",
                                "block_idx": i,
                                "text": appended,
                            }
                        )
                elif new_text != old_text:
                    ops.append(
                        {
                            "op": "replace",
                            "block_idx": i,
                            "content_blocks": [new_b],
                        }
                    )
                old_b["content"] = new_text
                # close detection: ended_at gained
                if new_b.get("ended_at") and not old_b.get("ended_at"):
                    ops.append(
                        {
                            "op": "block_close",
                            "block_idx": i,
                            "duration": new_b.get("duration"),
                            "ended_at": new_b.get("ended_at"),
                        }
                    )
                    old_b["ended_at"] = new_b["ended_at"]
                    old_b["duration"] = new_b.get("duration")
            elif btype == "tool_calls":
                # tool_calls block: if the underlying tool_call list grew or
                # results landed, send a replace for the whole slim block.
                if old_b != new_b:
                    ops.append(
                        {
                            "op": "replace",
                            "block_idx": i,
                            "content_blocks": [new_b],
                        }
                    )
                    old_blocks[i] = dict(new_b)
            else:
                if old_b != new_b:
                    ops.append(
                        {
                            "op": "replace",
                            "block_idx": i,
                            "content_blocks": [new_b],
                        }
                    )
                    old_blocks[i] = dict(new_b)

        if len(new_blocks) > common:
            for i in range(common, len(new_blocks)):
                new_b = new_blocks[i]
                # For text/reasoning the content streams via a following
                # text_append; for tool_calls it rides tool_call_add. Any OTHER
                # block type (e.g. `user_steer`, a mid-task user interjection)
                # carries its content as a static attr so the client mirror gets
                # it from the single block_open — there is no follow-up op for it.
                static_attrs = {
                    k: v
                    for k, v in new_b.items()
                    if k not in ("type", "content", "results")
                }
                if new_b.get("type") not in ("text", "reasoning", "tool_calls"):
                    static_attrs["content"] = new_b.get("content", "")
                ops.append(
                    {
                        "op": "block_open",
                        "block_idx": i,
                        "type": new_b.get("type"),
                        "attrs": static_attrs,
                    }
                )
                if new_b.get("type") in ("text", "reasoning"):
                    text = new_b.get("content", "") or ""
                    if text:
                        ops.append(
                            {
                                "op": "text_append",
                                "block_idx": i,
                                "text": text,
                            }
                        )
                elif new_b.get("type") == "tool_calls":
                    for tool_call in new_b.get("content") or []:
                        ops.append(
                            {
                                "op": "tool_call_add",
                                "block_idx": i,
                                "tool_call": tool_call,
                            }
                        )
                    if new_b.get("results"):
                        ops.append(
                            {
                                "op": "block_close",
                                "block_idx": i,
                                "results": new_b.get("results") or [],
                            }
                        )
                old_blocks.append(dict(new_b))
        elif len(new_blocks) < len(old_blocks):
            # truncation — fall back to replace
            ops.append(
                {
                    "op": "replace",
                    "block_idx": 0,
                    "content_blocks": new_blocks,
                }
            )
            mirror["blocks"] = [dict(b) for b in new_blocks]

    awaitables = []
    for op in ops:
        for split_op in _split_stream_delta_op(op):
            version = stream_version_incr(message_id)
            payload = {
                "type": "chat:delta",
                "data": {
                    "message_id": message_id,
                    "version": version,
                    "op": split_op["op"],
                    "payload": {k: v for k, v in split_op.items() if k != "op"},
                },
            }
            awaitables.append(raw_emit(payload))

    if extra_payload:
        version = stream_version_incr(message_id)
        payload = {
            "type": "chat:delta",
            "data": {
                "message_id": message_id,
                "version": version,
                "op": extra_payload["op"],
                "payload": extra_payload.get("payload", {}),
            },
        }
        awaitables.append(raw_emit(payload))

    return awaitables


def _wrap_event_emitter_v21(inner_emitter, metadata):
    """Returns an async event_emitter that translates `chat:completion` flushes
    into compact `chat:delta` ops, leaves non-streaming events untouched, and
    funnels stream events to the user's primary session only (B8 election)."""
    message_id = metadata.get("message_id")
    user_id = metadata.get("user_id")
    chat_id = metadata.get("chat_id")
    session_id = metadata.get("session_id")
    mirror = {"blocks": [], "tool_results_sent": set()}

    if message_id:
        stream_version_init(
            message_id,
            chat_id=chat_id,
            user_id=user_id,
            session_id=session_id,
            content_blocks=[],
        )

    async def _emit_raw_primary(payload):
        # Send a fully-formed `events` envelope to the primary session only.
        # Fallback: if no primary registered, fan to all (handled inside
        # emit_to_primary). DB persistence is already handled by the inner
        # emitter for v1-shaped payloads; v2.1 deltas are not persisted on a
        # per-emit basis (the per-chunk upsert at the call site covers the
        # canonical content).
        if not user_id:
            await inner_emitter(payload["data"] if "data" in payload else payload)
            return
        envelope = {
            "chat_id": chat_id,
            "message_id": message_id,
            "session_id": session_id,
            "data": payload,
        }
        await emit_to_primary(user_id, envelope)

    async def __v21_emitter__(event_data):
        etype = (event_data or {}).get("type")

        # Pass-through events: anything not `chat:completion` flows through
        # the inner emitter unchanged (status, source, citation, message,
        # replace, embeds, files, data_viz, model-switch:applied, errors,
        # chat:tasks:cancel, chat:subagent:*, chat:message:error, ...).
        # Inner emitter also handles its DB side-effects.
        if etype != "chat:completion":
            await inner_emitter(event_data)
            return

        data = event_data.get("data") or {}
        # A genuinely metadata-only selected_model_id flush becomes a compact
        # delta. Non-streaming provider payloads can carry the same field beside
        # choices/content/done; those must fall through intact or the answer and
        # terminal signal disappear.
        if is_selection_metadata_only_completion(data) and message_id:
            set_stream_state(
                message_id, {"selected_model_id": data["selected_model_id"]}
            )
            version = stream_version_incr(message_id)
            await _emit_raw_primary(
                {
                    "type": "chat:delta",
                    "data": {
                        "message_id": message_id,
                        "version": version,
                        "op": "selected_model_id",
                        "payload": {"model_id": data["selected_model_id"]},
                    },
                }
            )
            # Don't drop usage riding on the metadata flush. Surface it as its
            # own op=usage delta (the frontend ignores all-zero usage).
            if data.get("usage") is not None:
                set_stream_state(message_id, {"usage": data["usage"]})
                version = stream_version_incr(message_id)
                await _emit_raw_primary(
                    {
                        "type": "chat:delta",
                        "data": {
                            "message_id": message_id,
                            "version": version,
                            "op": "usage",
                            "payload": {"usage": data["usage"]},
                        },
                    }
                )
            return

        # Usage-only flush
        if set(data.keys()) <= {"usage"} and "usage" in data and message_id:
            set_stream_state(message_id, {"usage": data["usage"]})
            version = stream_version_incr(message_id)
            await _emit_raw_primary(
                {
                    "type": "chat:delta",
                    "data": {
                        "message_id": message_id,
                        "version": version,
                        "op": "usage",
                        "payload": {"usage": data["usage"]},
                    },
                }
            )
            return

        # Error mid-stream
        if "error" in data and message_id:
            set_stream_state(message_id, {"status": "error", "error": data["error"]})
            version = stream_version_incr(message_id)
            await _emit_raw_primary(
                {
                    "type": "chat:message:error",
                    "data": {
                        "message_id": message_id,
                        "version": version,
                        "error": data["error"],
                    },
                }
            )
            return

        # Content-bearing flush
        if "content_blocks" in data and message_id:
            # Snapshot-version coherence invariant (Part C): the /snapshot
            # endpoint must never see content that includes deltas ABOVE the
            # advertised snapshot_version (a reattach mid-flush would then
            # replay those deltas onto content that already contains them —
            # duplicated text; the window is widest on slow links, where each
            # emit await below can block on socket backpressure). So: build ALL
            # versioned delta payloads FIRST (stream_version_incr runs
            # synchronously at build time; _emit_raw_primary(...) only creates
            # a coroutine), then write content + snapshot_version in ONE state
            # patch stamped to the post-bump live version, and only then await
            # the emits. There is no await between the bumps and the state
            # write, so any /snapshot read sees a consistent (content, version)
            # pair: deltas ≤ stamp are already in content, > stamp get replayed.
            awaitables = _emit_delta_for_blocks(
                _emit_raw_primary, message_id, mirror, data["content_blocks"]
            )
            state_patch = {
                "content_blocks": _strip_tool_results(data["content_blocks"]),
                "status": "done" if data.get("done") else "in_progress",
            }
            if data.get("usage") is not None:
                state_patch["usage"] = data["usage"]
            if data.get("error") is not None:
                state_patch["error"] = data["error"]
                state_patch["status"] = "error"
            if "selected_model_id" in data:
                state_patch["selected_model_id"] = data["selected_model_id"]
                version = stream_version_incr(message_id)
                awaitables.append(
                    _emit_raw_primary(
                        {
                            "type": "chat:delta",
                            "data": {
                                "message_id": message_id,
                                "version": version,
                                "op": "selected_model_id",
                                "payload": {"model_id": data["selected_model_id"]},
                            },
                        }
                    )
                )
            # Sources arrive in the same payload occasionally
            if data.get("sources"):
                state_patch["sources"] = data["sources"]
                version = stream_version_incr(message_id)
                awaitables.append(
                    _emit_raw_primary(
                        {
                            "type": "chat:delta",
                            "data": {
                                "message_id": message_id,
                                "version": version,
                                "op": "sources",
                                "payload": {"sources": data["sources"]},
                            },
                        }
                    )
                )
            # selected_model_id/sources field-sets are folded into the SAME
            # patch, so the stamp covers their ops too (they are idempotent
            # field-sets, harmless either side of the advertised range).
            state_patch["snapshot_version"] = stream_version_get(message_id)
            set_stream_state(message_id, state_patch)
            stream_version_flush(message_id)
            # These ops are versioned and order-dependent. Emitting them via
            # gather lets block_open/text_append races clobber text on the
            # client (text_append creates the block, late block_open resets it).
            # Keep wire order deterministic.
            for awaitable in awaitables:
                await awaitable
            return

        # Anything else with content_blocks absent — pass through.
        await inner_emitter(event_data)

    # Expose the mirror so the outer pipeline can emit tool_call:result events
    # and the final chat:done envelope coherently.
    __v21_emitter__._v21_mirror = mirror  # type: ignore[attr-defined]
    __v21_emitter__._inner = inner_emitter  # type: ignore[attr-defined]
    __v21_emitter__._emit_raw_primary = _emit_raw_primary  # type: ignore[attr-defined]
    return __v21_emitter__
