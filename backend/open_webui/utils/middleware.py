import time
import logging
import sys
import os
import base64
import copy

import asyncio
import hashlib
from aiocache import cached
from contextvars import ContextVar
from typing import Any, Optional
import random
from open_webui.utils import fast_json as json
import html
import inspect
import re

from uuid import uuid4


# Set inside `_execute_tool_call` so a tool callable can find out which
# parent-model-issued tool_call_id triggered it. Used by the subagent tool
# (`utils/subagent_tool.py`) to stamp the subagent_id back on the right
# tool call's result entry so the parent UI can render a subagent block in
# place of the generic tool_calls collapsible.
#
# Why a ContextVar instead of `request.state.current_tool_call_id`: when the
# tool-call loop runs parallelizable tool calls via `asyncio.gather`, each
# concurrent task needs its own current_tool_call_id. ContextVars do the right
# thing here — asyncio.Task copies the context at creation, so each branch of
# the gather sees its own value.
current_tool_call_id_var: ContextVar[Optional[str]] = ContextVar(
    "current_tool_call_id", default=None
)


def _merge_streamed_string(existing: str | None, chunk: str | None) -> str:
    """Merge provider tool-call string deltas defensively.

    OpenAI-style streams usually send true deltas, but some compatible
    providers resend cumulative/full fields (notably function.name). Blind
    append turns `web_search` + `web_search` into `web_searchweb_search`.
    """
    if not chunk:
        return existing or ""
    existing = existing or ""
    if not existing:
        return chunk
    if chunk == existing:
        return existing
    if chunk.startswith(existing):
        return chunk
    if existing.endswith(chunk):
        return existing

    max_overlap = min(len(existing), len(chunk))
    for overlap in range(max_overlap, 0, -1):
        if existing.endswith(chunk[:overlap]):
            return existing + chunk[overlap:]
    return existing + chunk


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

    def suffix(self, n: int) -> str:
        """Return the last `n` characters of the accumulated content without
        mutating state (does NOT collapse the parts list or move the cursor).
        Used for bounded-suffix overlap detection on reasoning deltas."""
        if n <= 0:
            return ""
        out: list[str] = []
        need = n
        for part in reversed(self._parts):
            if need <= 0:
                break
            if len(part) <= need:
                out.append(part)
                need -= len(part)
            else:
                out.append(part[-need:])
                need = 0
        return "".join(reversed(out))

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


def _dedupe_repeated_tool_name(name: str | None) -> str:
    if not name:
        return ""
    # Covers web_searchweb_search, subagent_launchsubagent_launch, etc.
    for unit_len in range(1, (len(name) // 2) + 1):
        if len(name) % unit_len == 0:
            unit = name[:unit_len]
            if unit and unit * (len(name) // unit_len) == name:
                return unit
    return name


from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
from starlette.responses import Response, StreamingResponse, JSONResponse


from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.chats import Chats
from open_webui.models.folders import Folders
from open_webui.models.users import Users
from open_webui.socket.main import (
    get_event_call,
    get_event_emitter,
    get_headless_event_call,
    get_active_status_by_user_id,
    process_token_usage,
    is_primary_session,
    stream_version_init,
    stream_version_incr,
    stream_version_get,
    set_stream_state,
    set_tool_result,
    set_tool_result_body,
    get_tool_result_bodies,
    clear_tool_result_bodies,
    clear_stream_state,
    emit_to_primary,
    broadcast_sidebar_event,
)
from open_webui.tasks import (
    get_pending_model_switch,
    clear_pending_model_switch,
    get_pending_service_tier,
    clear_pending_service_tier,
)
from open_webui.routers.tasks import (
    generate_queries,
    generate_title,
    generate_follow_ups,
    generate_image_prompt,
    generate_chat_tags,
)
from open_webui.routers.retrieval import (
    process_web_search,
    SearchForm,
)
from open_webui.routers.images import (
    load_b64_image_data,
    image_generations,
    GenerateImageForm,
    upload_image,
)
from open_webui.routers.pipelines import (
    process_pipeline_inlet_filter,
    process_pipeline_outlet_filter,
)
from open_webui.routers.memories import query_memory, QueryMemoryForm

from open_webui.utils.webhook import post_webhook
from open_webui.utils.files import (
    get_audio_url_from_base64,
    get_file_url_from_base64,
    get_image_url_from_base64,
)


from open_webui.models.users import UserModel
from open_webui.models.functions import Functions
from open_webui.models.models import Models

from open_webui.utils.chat import (
    generate_chat_completion,
    run_outlet_filters_on_completed_stream,
)
from open_webui.utils.task import (
    get_task_model_id,
    rag_template,
    tools_function_calling_generation_template,
)
from open_webui.utils.misc import (
    deep_update,
    extract_urls,
    get_message_list,
    add_or_update_system_message,
    add_or_update_user_message,
    get_last_user_message,
    get_last_user_message_item,
    get_last_assistant_message,
    get_system_message,
    prepend_to_first_user_message_content,
    convert_logit_bias_input_to_json,
    get_content_from_message,
)
from open_webui.utils.tools import (
    get_tools,
    get_web_search_tool_specs,
    resolve_tool_server_headers,
)
from open_webui.utils.plugin import load_function_module_by_id
from open_webui.utils.filter import (
    get_sorted_filter_ids,
    process_filter_functions,
)
from open_webui.utils.payload import apply_system_prompt_to_body
from open_webui.utils.messages import blocks_to_api_messages, blocks_to_plain_text
from open_webui.models.mcp import MCPConnections
from open_webui.utils.mcp.client import (
    MCPClient,
    mcp_tool_alias,
    build_mcp_connect_kwargs,
)
from open_webui.utils.tool_calling import (
    mcp_model_facing_tool_name,
    parse_tool_call_arguments,
)
from open_webui.utils.mcp.connections import (
    build_personal_mcp_connect_kwargs,
    parse_personal_mcp_tool_id,
    tool_allowed_by_policy,
)
from open_webui.utils.container_workspace import (
    is_container_workspace_active,
    import_changed_container_outputs,
    prepare_container_workspace_for_turn,
    browser_progress_poller,
    _normalize_container_server_id,
)


from open_webui.config import (
    CACHE_DIR,
    DEFAULT_TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE,
)
from open_webui.env import (
    SRC_LOG_LEVELS,
    GLOBAL_LOG_LEVEL,
    CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE,
    BYPASS_MODEL_ACCESS_CONTROL,
    ENABLE_REALTIME_CHAT_SAVE,
    ENABLE_QUERIES_CACHE,
    STREAM_PROTOCOL_VERSION,
    AGENTIC_MAX_TOOL_ROUNDS,
    AGENTIC_EMPTY_ROUND_MAX_RETRIES,
    PROFILE_CHAT,
    PROFILE_CHAT_DIR,
)
from open_webui.constants import TASKS


logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


# ---------------------------------------------------------------------------
# Stream v2 delta translator
# ---------------------------------------------------------------------------
#
# The v1 emitter ships the entire `content_blocks` array on every flush (O(N²)
# bytes per turn). v2 ships only what changed since the last emit. To avoid
# rewriting the 1300-line stream loop, we install a translator that diffs the
# incoming content_blocks against a per-message mirror and emits the matching
# `chat:delta` ops. Anything not a content_blocks-bearing `chat:completion`
# event passes through unchanged (status, sources, citations, errors, ...).
#
# Wire Contract #1 (see plan Phase 0) — ops emitted:
#   text_append, block_open, block_close, tool_call_add, replace, sources,
#   selected_model_id. tool_call:result is emitted separately at exec time.

WEB_TOOL_NAMES = {"web_search", "web_fetch"}
WEB_TOOL_INLINE_RESULT_MAX = 2048
GENERIC_TOOL_INLINE_RESULT_MAX = max(
    4096, int(os.environ.get("GENERIC_TOOL_INLINE_RESULT_MAX", "65536") or "65536")
)
STREAM_TEXT_DELTA_MAX_BYTES = max(
    4096, int(os.environ.get("STREAM_TEXT_DELTA_MAX_BYTES", "262144") or "262144")
)
STREAM_DELTA_MAX_BYTES = max(
    STREAM_TEXT_DELTA_MAX_BYTES,
    int(os.environ.get("STREAM_DELTA_MAX_BYTES", "524288") or "524288"),
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


def _as_tool_id_list(tool_ids) -> list[str]:
    if isinstance(tool_ids, str):
        return [tool_ids]
    if isinstance(tool_ids, list):
        return [str(tool_id) for tool_id in tool_ids if tool_id is not None]
    return []


def _model_supports_vision(model: dict | None) -> bool:
    if not isinstance(model, dict):
        return True
    caps = (((model.get("info") or {}).get("meta") or {}).get("capabilities")) or {}
    return caps.get("vision", True) is not False


def _should_enable_view_image_tool(request, model, metadata: dict, tool_ids) -> bool:
    """Return true when view_image should be present in the model tool list."""
    if not _model_supports_vision(model):
        return False

    selected = _as_tool_id_list(tool_ids)
    if "builtin:web_search" in selected:
        return True

    return is_container_workspace_active(request, metadata, selected)


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

    inline_limit = (
        WEB_TOOL_INLINE_RESULT_MAX
        if tool_name in WEB_TOOL_NAMES
        else GENERIC_TOOL_INLINE_RESULT_MAX
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


def _strip_tool_results(content_blocks):
    """Mirror state stores block shapes but never heavy web tool result bodies.
    Non-web/small results remain inline for compatibility; large web results
    retain refs/metadata so collapsed cards can render cheaply."""
    return split_tool_result_bodies(content_blocks)[0]


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


def _finalize_open_agentic_block(content_blocks):
    """Stamp ended_at/duration on a trailing reasoning/tool_calls block that was
    still open when the stream was interrupted (user cancel or terminal error).

    Normal completion already finalizes reasoning (first text token / end of
    stream) and tool_calls (when results attach), so this only matters for the
    cancel/error paths. Without it, the UI's "Working for X" timer would have a
    start but no end on a frozen, persisted message — a dangling clock. Idempotent
    and safe to call on any content_blocks list.
    """
    if not content_blocks:
        return
    block = content_blocks[-1]
    if (
        block.get("type") in ("reasoning", "tool_calls")
        and block.get("started_at") is not None
        and block.get("ended_at") is None
    ):
        block["ended_at"] = time.time()
        block["duration"] = int(block["ended_at"] - block["started_at"])


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


def _wrap_event_emitter_v2(inner_emitter, metadata):
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
        # emitter for v1-shaped payloads; v2 deltas are not persisted on a
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

    async def __v2_emitter__(event_data):
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
        # selected_model_id flush: emit as chat:delta selected_model_id.
        # Some end-of-stream payloads carry both selected_model_id AND
        # content_blocks (see process_chat_response final `data` dict). Only
        # short-circuit when content_blocks is absent — otherwise fall through
        # so the content diff still ships.
        if "selected_model_id" in data and "content_blocks" not in data and message_id:
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
            state_patch = {
                "content_blocks": _strip_tool_results(data["content_blocks"]),
                "status": "done" if data.get("done") else "in_progress",
            }
            if data.get("usage") is not None:
                state_patch["usage"] = data["usage"]
            if data.get("error") is not None:
                state_patch["error"] = data["error"]
                state_patch["status"] = "error"
            set_stream_state(message_id, state_patch)
            awaitables = _emit_delta_for_blocks(
                _emit_raw_primary, message_id, mirror, data["content_blocks"]
            )
            # These ops are versioned and order-dependent. Emitting them via
            # gather lets block_open/text_append races clobber text on the
            # client (text_append creates the block, late block_open resets it).
            # Keep wire order deterministic.
            for awaitable in awaitables:
                await awaitable
            if "selected_model_id" in data:
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
            # Sources arrive in the same payload occasionally
            if data.get("sources"):
                set_stream_state(message_id, {"sources": data["sources"]})
                version = stream_version_incr(message_id)
                await _emit_raw_primary(
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
            # Snapshot-version decoupling invariant (Part C): the content snapshot
            # was written above (before the version bumps), so it already contains
            # everything through the current live version. Stamp snapshot_version
            # to the live version (after ALL bumps in this flush) so the /snapshot
            # endpoint advertises a version consistent with this content —
            # otherwise a stale lower snapshot_version left by a prior native flush
            # would make a reattach replay deltas already folded into the snapshot.
            # selected_model_id/sources ops are idempotent field-sets (not text
            # appends), so even being inside the advertised range is harmless.
            if message_id:
                set_stream_state(
                    message_id, {"snapshot_version": stream_version_get(message_id)}
                )
            return

        # Anything else with content_blocks absent — pass through.
        await inner_emitter(event_data)

    # Expose the mirror so the outer pipeline can emit tool_call:result events
    # and the final chat:done envelope coherently.
    __v2_emitter__._v2_mirror = mirror  # type: ignore[attr-defined]
    __v2_emitter__._inner = inner_emitter  # type: ignore[attr-defined]
    __v2_emitter__._emit_raw_primary = _emit_raw_primary  # type: ignore[attr-defined]
    return __v2_emitter__


def process_tool_result(
    request,
    tool_function_name,
    tool_result,
    tool_type,
    direct_tool=False,
    metadata=None,
    user=None,
    model=None,
):
    tool_result_embeds = []
    tool_result_vision_attachments = []
    # Structured UI metadata that rides alongside (never inside) the string the
    # model sees: {"error": bool, "error_reason": str, "notice": str}. Returned
    # as the 5th tuple element; None when empty. Lets the collapsed tool-call row
    # show an error/notice without the model ever parsing JSON.
    tool_result_meta = {}

    # The built-in web tools (and any tool that wants to surface a structured
    # error/notice to the UI) return a wrapper dict
    #   {"content": <string the model sees>, "_owui_meta": {error?, reason?, notice?}}.
    # Unwrap it up front: the model only ever sees `content`; the metadata travels
    # separately on the result entry.
    if (
        isinstance(tool_result, dict)
        and isinstance(tool_result.get("_owui_meta"), dict)
    ):
        _owui_meta = tool_result.get("_owui_meta") or {}
        if _owui_meta.get("error"):
            tool_result_meta["error"] = True
        if _owui_meta.get("reason"):
            tool_result_meta["error_reason"] = str(_owui_meta.get("reason"))
        if _owui_meta.get("notice"):
            tool_result_meta["notice"] = str(_owui_meta.get("notice"))
        unwrapped = tool_result.get("content")
        tool_result = unwrapped if unwrapped is not None else ""

    if isinstance(tool_result, dict) and isinstance(
        tool_result.get("vision_attachments"), list
    ):
        tool_result_vision_attachments = [
            attachment
            for attachment in tool_result.get("vision_attachments") or []
            if isinstance(attachment, dict) and attachment.get("url")
        ]
        tool_result = (
            tool_result.get("content")
            or tool_result.get("message")
            or "Image attached for visual inspection."
        )

    if isinstance(tool_result, HTMLResponse):
        content_disposition = tool_result.headers.get("Content-Disposition", "")
        if "inline" in content_disposition:
            content = tool_result.body.decode("utf-8", "replace")
            tool_result_embeds.append(content)

            if 200 <= tool_result.status_code < 300:
                tool_result = {
                    "status": "success",
                    "code": "ui_component",
                    "message": f"{tool_function_name}: Embedded UI result is active and visible to the user.",
                }
            elif 400 <= tool_result.status_code < 500:
                tool_result_meta["error"] = True
                tool_result_meta.setdefault(
                    "error_reason", f"HTTP {tool_result.status_code}"
                )
                tool_result = {
                    "status": "error",
                    "code": "ui_component",
                    "message": f"{tool_function_name}: Client error {tool_result.status_code} from embedded UI result.",
                }
            elif 500 <= tool_result.status_code < 600:
                tool_result_meta["error"] = True
                tool_result_meta.setdefault(
                    "error_reason", f"HTTP {tool_result.status_code}"
                )
                tool_result = {
                    "status": "error",
                    "code": "ui_component",
                    "message": f"{tool_function_name}: Server error {tool_result.status_code} from embedded UI result.",
                }
            else:
                tool_result_meta["error"] = True
                tool_result_meta.setdefault(
                    "error_reason", f"HTTP {tool_result.status_code}"
                )
                tool_result = {
                    "status": "error",
                    "code": "ui_component",
                    "message": f"{tool_function_name}: Unexpected status code {tool_result.status_code} from embedded UI result.",
                }
        else:
            tool_result = tool_result.body.decode("utf-8", "replace")

    elif (tool_type == "external" and isinstance(tool_result, tuple)) or (
        direct_tool and isinstance(tool_result, list) and len(tool_result) == 2
    ):
        tool_result, tool_response_headers = tool_result

        try:
            if not isinstance(tool_response_headers, dict):
                tool_response_headers = dict(tool_response_headers)
        except Exception as e:
            tool_response_headers = {}
            log.debug(e)

        if tool_response_headers and isinstance(tool_response_headers, dict):
            content_disposition = tool_response_headers.get(
                "Content-Disposition",
                tool_response_headers.get("content-disposition", ""),
            )

            if "inline" in content_disposition:
                content_type = tool_response_headers.get(
                    "Content-Type",
                    tool_response_headers.get("content-type", ""),
                )
                location = tool_response_headers.get(
                    "Location",
                    tool_response_headers.get("location", ""),
                )

                if "text/html" in content_type:
                    # Display as iframe embed
                    tool_result_embeds.append(tool_result)
                    tool_result = {
                        "status": "success",
                        "code": "ui_component",
                        "message": f"{tool_function_name}: Embedded UI result is active and visible to the user.",
                    }
                elif location:
                    tool_result_embeds.append(location)
                    tool_result = {
                        "status": "success",
                        "code": "ui_component",
                        "message": f"{tool_function_name}: Embedded UI result is active and visible to the user.",
                    }

    tool_result_files = []

    if (
        tool_type == "mcp"
        and isinstance(tool_result, dict)
        and tool_result.get("isError") is True
        and isinstance(tool_result.get("content"), list)
    ):
        # MCP signalled a tool error. Preserve that signal for the UI (it was
        # previously discarded) before unwrapping the content list below.
        tool_result_meta["error"] = True
        tool_result = tool_result.get("content")

    if isinstance(tool_result, list):
        if tool_type == "mcp":  # MCP
            tool_response = []
            for item in tool_result:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text = item.get("text", "")
                        if isinstance(text, str):
                            try:
                                text = json.loads(text)
                            except json.JSONDecodeError:
                                pass
                        tool_response.append(text)
                    elif item.get("type") in ["image", "audio"]:
                        # Persisting the attachment must never abort processing of
                        # the rest of the tool result. A browser_snapshot returns
                        # [text, image]; if the image upload raises (e.g. an upload
                        # helper contract drift), we still want the model to get the
                        # text block — not have the whole turn torn down.
                        file_url = None
                        try:
                            file_url = get_file_url_from_base64(
                                request,
                                f"data:{item.get('mimeType')};base64,{item.get('data', item.get('blob', ''))}",
                                {
                                    "chat_id": metadata.get("chat_id", None),
                                    "message_id": metadata.get("message_id", None),
                                    "session_id": metadata.get("session_id", None),
                                    "result": item,
                                },
                                user,
                            )
                        except Exception:
                            log.exception(
                                "Failed to persist MCP %s attachment for tool %r; "
                                "dropping the attachment and keeping the rest of the result",
                                item.get("type"),
                                tool_function_name,
                            )
                            file_url = None

                        if not file_url:
                            # No usable attachment (helper returned None for an
                            # unsupported mime, or the upload failed above). Don't
                            # emit a {"url": None} file entry or a broken vision
                            # attachment — just skip it.
                            continue

                        tool_result_files.append(
                            {
                                "type": item.get("type", "data"),
                                "url": file_url,
                            }
                        )

                        # An MCP image result (e.g. a browser screenshot) is shown
                        # in the tool card via tool_result_files, but the model
                        # only *sees* it when routed through vision_attachments
                        # (-> _image_observation_message synthetic user image).
                        # Bridge it here, gated on vision support so non-vision
                        # models aren't sent image_url content they can't accept.
                        if (
                            item.get("type") == "image"
                            and _model_supports_vision(model)
                        ):
                            tool_result_vision_attachments.append(
                                {"url": file_url, "detail": "auto"}
                            )
            tool_result = tool_response[0] if len(tool_response) == 1 else tool_response
        else:  # OpenAPI
            for item in tool_result:
                if isinstance(item, str) and item.startswith("data:"):
                    tool_result_files.append(
                        {
                            "type": "data",
                            "content": item,
                        }
                    )
                    tool_result.remove(item)

    if isinstance(tool_result, list):
        tool_result = {"results": tool_result}

    if isinstance(tool_result, dict) or isinstance(tool_result, list):
        tool_result = json.dumps(tool_result, indent=2, ensure_ascii=False)

    return (
        tool_result,
        tool_result_files,
        tool_result_embeds,
        tool_result_vision_attachments,
        tool_result_meta or None,
    )


async def chat_completion_tools_handler(
    request: Request, body: dict, extra_params: dict, user: UserModel, models, tools
) -> tuple[dict, dict]:
    async def get_content_from_response(response) -> Optional[str]:
        content = None
        if hasattr(response, "body_iterator"):
            async for chunk in response.body_iterator:
                data = json.loads(chunk.decode("utf-8", "replace"))
                content = data["choices"][0]["message"]["content"]

            # Cleanup any remaining background tasks if necessary
            if getattr(response, "background", None) is not None:
                await response.background()
        else:
            content = response["choices"][0]["message"]["content"]
        return content

    def get_tools_function_calling_payload(messages, task_model_id, content):
        user_message = get_last_user_message(messages)

        recent_messages = messages[-4:] if len(messages) > 4 else messages
        chat_history = "\n".join(
            f"{message['role'].upper()}: \"\"\"{get_content_from_message(message)}\"\"\""
            for message in recent_messages
        )

        prompt = f"History:\n{chat_history}\nQuery: {user_message}"

        return {
            "model": task_model_id,
            "messages": [
                {"role": "system", "content": content},
                {"role": "user", "content": f"Query: {prompt}"},
            ],
            "stream": False,
            "metadata": {"task": str(TASKS.FUNCTION_CALLING)},
        }

    event_caller = extra_params["__event_call__"]
    event_emitter = extra_params["__event_emitter__"]
    metadata = extra_params["__metadata__"]

    task_model_id = get_task_model_id(
        body["model"],
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    skip_files = False
    sources = []

    specs = [tool["spec"] for tool in tools.values()]
    tools_specs = json.dumps(specs)

    if request.app.state.config.TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE != "":
        template = request.app.state.config.TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE
    else:
        template = DEFAULT_TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE

    tools_function_calling_prompt = tools_function_calling_generation_template(
        template, tools_specs
    )
    payload = get_tools_function_calling_payload(
        body["messages"], task_model_id, tools_function_calling_prompt
    )

    try:
        response = await generate_chat_completion(request, form_data=payload, user=user)
        log.debug(f"{response=}")
        content = await get_content_from_response(response)
        log.debug(f"{content=}")

        if not content:
            return body, {}

        try:
            content = content[content.find("{") : content.rfind("}") + 1]
            if not content:
                raise Exception("No JSON object found in the response")

            result = json.loads(content)

            # Computes the tool result without mutating shared state (sources,
            # body["messages"], skip_files, event emissions). This lets us run
            # parallelizable calls concurrently via asyncio.gather and then
            # apply side effects in input order via _apply_tool_call_result.
            async def _compute_tool_call(tool_call):
                log.debug(f"{tool_call=}")

                tool_function_name = tool_call.get("name", None)
                if tool_function_name not in tools:
                    return None

                tool_function_params = tool_call.get("parameters", {})

                tool = None
                tool_type = ""
                direct_tool = False

                try:
                    tool = tools[tool_function_name]
                    tool_type = tool.get("type", "")
                    direct_tool = tool.get("direct", False)

                    spec = tool.get("spec", {})
                    allowed_params = (
                        spec.get("parameters", {}).get("properties", {}).keys()
                    )
                    tool_function_params = {
                        k: v
                        for k, v in tool_function_params.items()
                        if k in allowed_params
                    }

                    if tool.get("direct", False):
                        tool_result = await event_caller(
                            {
                                "type": "execute:tool",
                                "data": {
                                    "id": str(uuid4()),
                                    "name": tool_function_name,
                                    "params": tool_function_params,
                                    "server": tool.get("server", {}),
                                    "session_id": metadata.get("session_id", None),
                                },
                            }
                        )
                    else:
                        tool_function = tool["callable"]
                        tool_result = await tool_function(**tool_function_params)

                except Exception as e:
                    tool_result = str(e)

                try:
                    (
                        tool_result,
                        tool_result_files,
                        tool_result_embeds,
                        tool_result_vision_attachments,
                        tool_result_meta,
                    ) = process_tool_result(
                        request,
                        tool_function_name,
                        tool_result,
                        tool_type,
                        direct_tool,
                        metadata,
                        user,
                    )
                except Exception as e:
                    # Post-processing a tool result (image persistence, embed/UI
                    # unwrapping, JSON shaping) must NEVER tear down the turn. The
                    # tool itself already ran; degrade to a usable error result so
                    # the loop proceeds instead of crashing the whole generation.
                    log.exception(
                        "process_tool_result failed for tool %r; degrading to an "
                        "error result so the turn can continue",
                        tool_function_name,
                    )
                    tool_result = (
                        f"Tool '{tool_function_name}' ran but its result could not "
                        f"be processed: {e}"
                    )
                    tool_result_files = []
                    tool_result_embeds = []
                    tool_result_vision_attachments = []
                    tool_result_meta = {
                        "error": True,
                        "error_reason": "result post-processing failed",
                    }

                return {
                    "tool_function_name": tool_function_name,
                    "tool_function_params": tool_function_params,
                    "tool_result": tool_result,
                    "tool_result_files": tool_result_files,
                    "tool_result_embeds": tool_result_embeds,
                    "tool_result_vision_attachments": tool_result_vision_attachments,
                }

            async def _apply_tool_call_result(handler_result):
                """Apply side effects from a tool result. MUST be called in input order."""
                nonlocal skip_files

                if handler_result is None:
                    return

                tool_function_name = handler_result["tool_function_name"]
                tool_function_params = handler_result["tool_function_params"]
                tool_result = handler_result["tool_result"]
                tool_result_files = handler_result["tool_result_files"]
                tool_result_embeds = handler_result["tool_result_embeds"]

                if event_emitter:
                    if tool_result_files:
                        await event_emitter(
                            {
                                "type": "files",
                                "data": {
                                    "files": tool_result_files,
                                },
                            }
                        )

                    if tool_result_embeds:
                        await event_emitter(
                            {
                                "type": "embeds",
                                "data": {
                                    "embeds": tool_result_embeds,
                                },
                            }
                        )

                print(
                    f"Tool {tool_function_name} result: {tool_result}",
                    tool_result_files,
                    tool_result_embeds,
                )

                if tool_result:
                    tool = tools[tool_function_name]
                    tool_id = tool.get("tool_id", "")

                    tool_name = (
                        f"{tool_id}/{tool_function_name}"
                        if tool_id
                        else f"{tool_function_name}"
                    )

                    # Citation is enabled for this tool
                    sources.append(
                        {
                            "source": {
                                "name": (f"{tool_name}"),
                            },
                            "document": [str(tool_result)],
                            "metadata": [
                                {
                                    "source": (f"{tool_name}"),
                                    "parameters": tool_function_params,
                                }
                            ],
                            "tool_result": True,
                        }
                    )

                    # Citation is not enabled for this tool
                    body["messages"] = add_or_update_user_message(
                        f"\nTool `{tool_name}` Output: {tool_result}",
                        body["messages"],
                    )

                    if (
                        tools[tool_function_name]
                        .get("metadata", {})
                        .get("file_handler", False)
                    ):
                        skip_files = True

            def _is_parallelizable(tool_call):
                name = tool_call.get("name", None)
                tool = tools.get(name)
                return bool(
                    tool and tool.get("metadata", {}).get("parallelizable", False)
                )

            # check if "tool_calls" in result
            if result.get("tool_calls"):
                tool_calls_list = result.get("tool_calls")
                # Group consecutive parallelizable calls; non-parallelizable calls
                # are barriers. Side effects are applied strictly in input order
                # below, so message/source ordering matches the native path.
                handler_results = [None] * len(tool_calls_list)
                i = 0
                while i < len(tool_calls_list):
                    if _is_parallelizable(tool_calls_list[i]):
                        j = i
                        while j < len(tool_calls_list) and _is_parallelizable(
                            tool_calls_list[j]
                        ):
                            j += 1
                        batch = await asyncio.gather(
                            *[
                                _compute_tool_call(tool_calls_list[k])
                                for k in range(i, j)
                            ]
                        )
                        for offset, hr in enumerate(batch):
                            handler_results[i + offset] = hr
                        i = j
                    else:
                        handler_results[i] = await _compute_tool_call(
                            tool_calls_list[i]
                        )
                        i += 1

                for hr in handler_results:
                    await _apply_tool_call_result(hr)
            else:
                hr = await _compute_tool_call(result)
                await _apply_tool_call_result(hr)

        except Exception as e:
            log.debug(f"Error: {e}")
            content = None
    except Exception as e:
        log.debug(f"Error: {e}")
        content = None

    log.debug(f"tool_contexts: {sources}")

    if skip_files and "files" in body.get("metadata", {}):
        del body["metadata"]["files"]

    return body, {"sources": sources}


async def chat_memory_handler(
    request: Request, form_data: dict, extra_params: dict, user
):
    try:
        results = await query_memory(
            request,
            QueryMemoryForm(
                **{
                    "content": get_last_user_message(form_data["messages"]) or "",
                    "k": 3,
                }
            ),
            user,
        )
    except Exception as e:
        log.debug(e)
        results = None

    user_context = ""
    if results and hasattr(results, "documents"):
        if results.documents and len(results.documents) > 0:
            for doc_idx, doc in enumerate(results.documents[0]):
                created_at_date = "Unknown Date"

                if results.metadatas[0][doc_idx].get("created_at"):
                    created_at_timestamp = results.metadatas[0][doc_idx]["created_at"]
                    created_at_date = time.strftime(
                        "%Y-%m-%d", time.localtime(created_at_timestamp)
                    )

                user_context += f"{doc_idx + 1}. [{created_at_date}] {doc}\n"

    form_data["messages"] = add_or_update_system_message(
        f"User Context:\n{user_context}\n", form_data["messages"], append=True
    )

    return form_data


async def chat_web_search_handler(
    request: Request, form_data: dict, extra_params: dict, user
):
    event_emitter = extra_params["__event_emitter__"]
    await event_emitter(
        {
            "type": "status",
            "data": {
                "action": "web_search",
                "description": "Searching the web",
                "done": False,
            },
        }
    )

    messages = form_data["messages"]
    user_message = get_last_user_message(messages)

    queries = []
    try:
        res = await generate_queries(
            request,
            {
                "model": form_data["model"],
                "messages": messages,
                "prompt": user_message,
                "type": "web_search",
            },
            user,
        )

        response = res["choices"][0]["message"]["content"]

        try:
            bracket_start = response.find("{")
            bracket_end = response.rfind("}") + 1

            if bracket_start == -1 or bracket_end == -1:
                raise Exception("No JSON object found in the response")

            response = response[bracket_start:bracket_end]
            queries = json.loads(response)
            queries = queries.get("queries", [])
        except Exception as e:
            queries = [response]

        if ENABLE_QUERIES_CACHE:
            request.state.cached_queries = queries

    except Exception as e:
        log.exception(e)
        queries = [user_message]

    # Check if generated queries are empty
    if len(queries) == 1 and queries[0].strip() == "":
        queries = [user_message]

    # Check if queries are not found
    if len(queries) == 0:
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "web_search",
                    "description": "No search query generated",
                    "done": True,
                },
            }
        )
        return form_data

    await event_emitter(
        {
            "type": "status",
            "data": {
                "action": "web_search_queries_generated",
                "queries": queries,
                "done": False,
            },
        }
    )

    try:
        results = await process_web_search(
            request,
            SearchForm(queries=queries),
            user=user,
        )

        if results:
            files = form_data.get("files", [])

            if results.get("collection_names"):
                for col_idx, collection_name in enumerate(
                    results.get("collection_names")
                ):
                    files.append(
                        {
                            "collection_name": collection_name,
                            "name": ", ".join(queries),
                            "type": "web_search",
                            "urls": results["filenames"],
                            "queries": queries,
                        }
                    )
            elif results.get("docs"):
                # Invoked when bypass embedding and retrieval is set to True
                docs = results["docs"]
                files.append(
                    {
                        "docs": docs,
                        "name": ", ".join(queries),
                        "type": "web_search",
                        "urls": results["filenames"],
                        "queries": queries,
                    }
                )

            form_data["files"] = files

            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "action": "web_search",
                        "description": "Searched {{count}} sites",
                        "urls": results["filenames"],
                        "items": results.get("items", []),
                        "done": True,
                    },
                }
            )
        else:
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "action": "web_search",
                        "description": "No search results found",
                        "done": True,
                        "error": True,
                    },
                }
            )

    except Exception as e:
        log.exception(e)
        await event_emitter(
            {
                "type": "status",
                "data": {
                    "action": "web_search",
                    "description": "An error occurred while searching the web",
                    "queries": queries,
                    "done": True,
                    "error": True,
                },
            }
        )

    return form_data


async def chat_image_generation_handler(
    request: Request, form_data: dict, extra_params: dict, user
):
    __event_emitter__ = extra_params["__event_emitter__"]
    await __event_emitter__(
        {
            "type": "status",
            "data": {"description": "Creating image", "done": False},
        }
    )

    messages = form_data["messages"]
    user_message = get_last_user_message(messages)

    prompt = user_message
    negative_prompt = ""

    if request.app.state.config.ENABLE_IMAGE_PROMPT_GENERATION:
        try:
            res = await generate_image_prompt(
                request,
                {
                    "model": form_data["model"],
                    "messages": messages,
                },
                user,
            )

            response = res["choices"][0]["message"]["content"]

            try:
                bracket_start = response.find("{")
                bracket_end = response.rfind("}") + 1

                if bracket_start == -1 or bracket_end == -1:
                    raise Exception("No JSON object found in the response")

                response = response[bracket_start:bracket_end]
                response = json.loads(response)
                prompt = response.get("prompt", [])
            except Exception as e:
                prompt = user_message

        except Exception as e:
            log.exception(e)
            prompt = user_message

    system_message_content = ""

    try:
        images = await image_generations(
            request=request,
            form_data=GenerateImageForm(**{"prompt": prompt}),
            user=user,
        )

        await __event_emitter__(
            {
                "type": "status",
                "data": {"description": "Image created", "done": True},
            }
        )

        await __event_emitter__(
            {
                "type": "files",
                "data": {
                    "files": [
                        {
                            "type": "image",
                            "url": image["url"],
                        }
                        for image in images
                    ]
                },
            }
        )

        system_message_content = "<context>User is shown the generated image, tell the user that the image has been generated</context>"
    except Exception as e:
        log.exception(e)
        await __event_emitter__(
            {
                "type": "status",
                "data": {
                    "description": f"An error occurred while generating an image",
                    "done": True,
                },
            }
        )

        system_message_content = "<context>Unable to generate an image, tell the user that an error occurred</context>"

    if system_message_content:
        form_data["messages"] = add_or_update_system_message(
            system_message_content, form_data["messages"]
        )

    return form_data


def apply_params_to_form_data(form_data, model):
    params = form_data.pop("params", {})
    custom_params = params.pop("custom_params", {})

    # Convert reasoning_effort parameter to valid reasoning object
    # Backend needs: reasoning: { effort: 'medium' }
    reasoning = form_data.get("reasoning", None)
    if reasoning and isinstance(reasoning, dict):
        pass
    else:
        reasoning_effort = params.get("reasoning_effort")
        if reasoning_effort:
            form_data["reasoning"] = {"effort": reasoning_effort}
            del params["reasoning_effort"]

    open_webui_params = {
        "stream_response": bool,
        "stream_delta_chunk_size": int,
        "function_calling": str,
        "reasoning_tags": list,
        "system": str,
    }

    if "system" in params:
        form_data["messages"] = add_or_update_system_message(
            str(params["system"]), form_data["messages"]
        )

    for key in list(params.keys()):
        if key in open_webui_params:
            del params[key]

    if custom_params:
        # Attempt to parse custom_params if they are strings
        for key, value in custom_params.items():
            if isinstance(value, str):
                try:
                    # Attempt to parse the string as JSON
                    custom_params[key] = json.loads(value)
                except json.JSONDecodeError:
                    # If it fails, keep the original string
                    pass

        # If custom_params are provided, merge them into params
        params = deep_update(params, custom_params)

    if model.get("owned_by") == "ollama":
        # Ollama specific parameters
        form_data["options"] = params
    else:
        if isinstance(params, dict):
            for key, value in params.items():
                if value is not None:
                    form_data[key] = value

        if "logit_bias" in params and params["logit_bias"] is not None:
            try:
                form_data["logit_bias"] = json.loads(
                    convert_logit_bias_input_to_json(params["logit_bias"])
                )
            except Exception as e:
                log.exception(f"Error parsing logit_bias: {e}")

    return form_data


async def process_chat_payload(request, form_data, user, metadata, model):
    # Pipeline Inlet -> Filter Inlet -> Chat Memory -> Chat Web Search -> Chat Image Generation
    # -> (Default) Chat Tools Function Calling -> Chat Files

    incoming_params = form_data.get("params") if isinstance(form_data, dict) else None
    incoming_subagent_external_tools_enabled = None
    if (
        isinstance(incoming_params, dict)
        and "subagentExternalToolsEnabled" in incoming_params
    ):
        incoming_subagent_external_tools_enabled = bool(
            incoming_params.get("subagentExternalToolsEnabled")
        )

    form_data = apply_params_to_form_data(form_data, model)

    if "subagentExternalToolsEnabled" in form_data:
        metadata.setdefault("params", {})["subagentExternalToolsEnabled"] = bool(
            form_data.get("subagentExternalToolsEnabled")
        )
    elif incoming_subagent_external_tools_enabled is not None:
        metadata.setdefault("params", {})[
            "subagentExternalToolsEnabled"
        ] = incoming_subagent_external_tools_enabled

    # Ensure stream_options.include_usage is enabled for token usage tracking
    if form_data.get("stream", False):
        if "stream_options" not in form_data:
            form_data["stream_options"] = {}
        form_data["stream_options"]["include_usage"] = True

    log.debug(f"form_data: {form_data}")

    system_message = get_system_message(form_data.get("messages", []))
    if system_message:  # Chat Controls/User Settings
        try:
            form_data = apply_system_prompt_to_body(
                system_message.get("content"), form_data, metadata, user, replace=True
            )  # Required to handle system prompt variables
        except:
            pass

    event_emitter = get_event_emitter(metadata)
    event_call = get_event_call(metadata)

    oauth_token = None
    try:
        if request.cookies.get("oauth_session_id", None):
            oauth_token = await request.app.state.oauth_manager.get_oauth_token(
                user.id,
                request.cookies.get("oauth_session_id", None),
            )
    except Exception as e:
        log.error(f"Error getting OAuth token: {e}")

    extra_params = {
        "__event_emitter__": event_emitter,
        "__event_call__": event_call,
        "__user__": user.model_dump() if isinstance(user, UserModel) else {},
        "__metadata__": metadata,
        "__request__": request,
        "__model__": model,
        "__oauth_token__": oauth_token,
    }

    # Initialize events to store additional event to be sent to the client
    # Initialize contexts and citation
    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    task_model_id = get_task_model_id(
        form_data["model"],
        request.app.state.config.TASK_MODEL,
        request.app.state.config.TASK_MODEL_EXTERNAL,
        models,
    )

    events = []
    sources = []

    # Folder "Project" handling
    # Check if the request has chat_id and is inside of a folder
    chat_id = metadata.get("chat_id", None)
    if chat_id and user:
        chat = Chats.get_chat_by_id_and_user_id(chat_id, user.id)
        if chat and chat.folder_id:
            folder = Folders.get_folder_by_id_and_user_id(chat.folder_id, user.id)

            if folder and folder.data:
                if "system_prompt" in folder.data:
                    form_data = apply_system_prompt_to_body(
                        folder.data["system_prompt"], form_data, metadata, user
                    )
                # Folder-level file attachments / knowledge have been removed.
                pass

    variables = form_data.pop("variables", None)

    # Process the form_data through the pipeline
    try:
        form_data = await process_pipeline_inlet_filter(
            request, form_data, user, models
        )
    except Exception as e:
        raise e

    try:
        filter_functions = [
            Functions.get_function_by_id(filter_id)
            for filter_id in get_sorted_filter_ids(
                request, model, metadata.get("filter_ids", [])
            )
        ]

        form_data, flags = await process_filter_functions(
            request=request,
            filter_functions=filter_functions,
            filter_type="inlet",
            form_data=form_data,
            extra_params=extra_params,
        )
    except Exception as e:
        raise Exception(f"{e}")

    # Pop tool_ids early so we can modify it in feature handlers
    tool_ids = form_data.pop("tool_ids", None)

    features = form_data.pop("features", None)
    if features:
        # Memory ships its own user-context retrieval; it has to run inline
        # because it touches the vector store. Everything else is just a
        # config-driven prompt fragment, gathered below and applied once at
        # the end as a single deterministic compose step (keeps the prompt
        # cache stable across turns when feature flags don't change).
        if "memory" in features and features["memory"]:
            form_data = await chat_memory_handler(
                request, form_data, extra_params, user
            )

        feature_prompt_parts: list[str] = []

        if features.get("web_search"):
            if tool_ids is None:
                tool_ids = []
            if "builtin:web_search" not in tool_ids:
                tool_ids.append("builtin:web_search")
            metadata.setdefault("params", {})["function_calling"] = "native"

            web_search_prompt = getattr(
                request.app.state.config, "WEB_SEARCH_SYSTEM_PROMPT", ""
            )
            if web_search_prompt:
                feature_prompt_parts.append(web_search_prompt)
            log.info("Auto-enabled web search tools with native function calling")

        if features.get("study_mode"):
            study_prompt = getattr(
                request.app.state.config, "STUDY_MODE_SYSTEM_PROMPT", ""
            )
            if study_prompt:
                feature_prompt_parts.append(study_prompt)
            log.info("Processed study mode")

        if features.get("data_viz"):
            from open_webui.utils.data_viz_prompts import (
                assemble_data_viz_system_prompt,
            )

            if tool_ids is None:
                tool_ids = []
            if "builtin:data_viz" not in tool_ids:
                tool_ids.append("builtin:data_viz")
            metadata.setdefault("params", {})["function_calling"] = "native"

            data_viz_prompt = assemble_data_viz_system_prompt(request.app.state.config)
            if data_viz_prompt:
                feature_prompt_parts.append(data_viz_prompt)
            log.info(
                "Auto-enabled data visualization tool with native function calling"
            )

        if features.get("subagents"):
            # Subagents are isolated research workers the parent can spawn via
            # `subagent_launch` / `subagent_continue` (see utils/subagent.py +
            # utils/subagent_tool.py). Registering the builtin here exposes the
            # two tools to the parent model and appends the admin-editable
            # parent-side instructions so the model knows when to use them.
            # The inner subagent run explicitly clears features={} so the
            # subagent itself can NOT recursively spawn another subagent.
            if tool_ids is None:
                tool_ids = []
            if "builtin:subagent" not in tool_ids:
                tool_ids.append("builtin:subagent")
            metadata.setdefault("params", {})["function_calling"] = "native"

            subagent_parent_prompt = getattr(
                request.app.state.config, "SUBAGENT_PARENT_PROMPT", ""
            )
            if subagent_parent_prompt:
                feature_prompt_parts.append(subagent_parent_prompt)
            log.info("Auto-enabled subagent tools with native function calling")

        if feature_prompt_parts:
            form_data["messages"] = add_or_update_system_message(
                "\n\n".join(feature_prompt_parts),
                form_data["messages"],
            )

        # OLD WEB SEARCH HANDLER - DISABLED IN FAVOR OF TOOL-BASED APPROACH
        # if "web_search" in features and features["web_search"]:
        #     form_data = await chat_web_search_handler(
        #         request, form_data, extra_params, user
        #     )

        if "image_generation" in features and features["image_generation"]:
            form_data = await chat_image_generation_handler(
                request, form_data, extra_params, user
            )

    container_prompt = await prepare_container_workspace_for_turn(
        request, metadata, form_data, user, tool_ids
    )
    if container_prompt:
        form_data["messages"] = add_or_update_system_message(
            container_prompt, form_data["messages"], append=True
        )

    if _should_enable_view_image_tool(request, model, metadata, tool_ids):
        if tool_ids is None:
            tool_ids = []
        if "builtin:view_image" not in tool_ids:
            tool_ids.append("builtin:view_image")
        metadata.setdefault("params", {})["function_calling"] = "native"
        log.info("Auto-enabled view_image tool with native function calling")

    prompt = get_last_user_message(form_data["messages"])

    metadata = {
        **metadata,
        "tool_ids": tool_ids,
    }
    form_data["metadata"] = metadata
    extra_params["__metadata__"] = metadata

    # Server side tools
    tool_ids = metadata.get("tool_ids", None)
    # Client side tools
    direct_tool_servers = metadata.get("tool_servers", None)

    log.debug(f"{tool_ids=}")
    log.debug(f"{direct_tool_servers=}")

    tools_dict = {}

    mcp_clients = {}
    mcp_tools_dict = {}
    mcp_failures: list[dict] = []

    if tool_ids:
        for tool_id in tool_ids:
            personal_connection_id = parse_personal_mcp_tool_id(tool_id)
            if personal_connection_id:
                original_server_id = f"user:{personal_connection_id}"
                personal_connection = None
                try:
                    personal_connection = (
                        MCPConnections.get_connection_by_id_and_user_id(
                            personal_connection_id, user.id, include_secrets=True
                        )
                    )
                    if not personal_connection or not personal_connection.enabled:
                        mcp_failures.append(
                            {
                                "server_id": original_server_id,
                                "name": personal_connection_id,
                                "reason": "Personal MCP connection not found",
                            }
                        )
                        continue

                    mcp_clients[original_server_id] = MCPClient()
                    connect_kwargs = await build_personal_mcp_connect_kwargs(
                        personal_connection,
                        user=user,
                        metadata=metadata,
                    )
                    await mcp_clients[original_server_id].connect(**connect_kwargs)

                    tool_specs = await mcp_clients[original_server_id].list_tool_specs()
                    for tool_spec in tool_specs or []:
                        if not tool_allowed_by_policy(tool_spec, personal_connection):
                            continue

                        def make_tool_function(client, function_name):
                            async def tool_function(**kwargs):
                                return await client.call_tool(
                                    function_name,
                                    function_args=kwargs,
                                )

                            return tool_function

                        tool_function = make_tool_function(
                            mcp_clients[original_server_id], tool_spec["name"]
                        )
                        alias = mcp_tool_alias(original_server_id, tool_spec["name"])
                        if alias in mcp_tools_dict:
                            alias = mcp_tool_alias(
                                f"{original_server_id}:{tool_spec['name']}",
                                tool_spec["name"],
                            )
                        mcp_tools_dict[alias] = {
                            "spec": {
                                **tool_spec,
                                "name": alias,
                            },
                            "callable": tool_function,
                            "type": "mcp",
                            "client": mcp_clients[original_server_id],
                            "direct": False,
                            "metadata": {
                                "server_id": original_server_id,
                                "original_name": tool_spec["name"],
                                "annotations": tool_spec.get("annotations", {}),
                                "outputSchema": tool_spec.get("outputSchema"),
                                "parallelizable": bool(
                                    (personal_connection.policy or {}).get(
                                        "parallelizable", False
                                    )
                                ),
                            },
                        }
                except Exception as e:
                    log.exception(
                        "Personal MCP connection %r failed during connect/list_tool_specs",
                        original_server_id,
                    )
                    mcp_clients.pop(original_server_id, None)
                    mcp_failures.append(
                        {
                            "server_id": original_server_id,
                            "name": getattr(
                                personal_connection, "name", personal_connection_id
                            ),
                            "reason": f"{type(e).__name__}: {e}",
                        }
                    )
                    continue

            if tool_id.startswith("server:mcp:"):
                # Snapshot the original id BEFORE the oauth_2.1 branch mutates
                # `server_id` to its trailing colon-segment. Use the original
                # for all per-server bookkeeping (mcp_clients key, failure
                # records, error messages) so two servers whose ids share a
                # trailing segment can't silently overwrite each other.
                original_server_id = tool_id[len("server:mcp:") :]
                server_id = original_server_id
                try:
                    mcp_server_connection = None
                    for (
                        server_connection
                    ) in request.app.state.config.TOOL_SERVER_CONNECTIONS:
                        if (
                            server_connection.get("type", "") == "mcp"
                            and server_connection.get("info", {}).get("id") == server_id
                        ):
                            mcp_server_connection = server_connection
                            break

                    if not mcp_server_connection:
                        log.error(f"MCP server with id {server_id} not found")
                        mcp_failures.append(
                            {
                                "server_id": original_server_id,
                                "name": original_server_id,
                                "reason": "Configured MCP server not found",
                            }
                        )
                        continue

                    auth_type = mcp_server_connection.get("auth_type", "")

                    bearer_token: Optional[str] = None
                    if auth_type == "bearer":
                        bearer_token = mcp_server_connection.get("key", "") or None
                    elif auth_type == "none":
                        pass
                    elif auth_type == "session":
                        bearer_token = request.state.token.credentials
                    elif auth_type == "system_oauth":
                        oauth_token = extra_params.get("__oauth_token__", None)
                        if oauth_token:
                            bearer_token = oauth_token.get("access_token", "") or None
                    elif auth_type == "oauth_2.1":
                        try:
                            splits = server_id.split(":")
                            # Keep `oauth_lookup_id` distinct from
                            # `original_server_id`; the OAuth client manager
                            # is keyed on the trailing segment per existing
                            # convention, but our dicts stay keyed by the
                            # full id to avoid collisions.
                            oauth_lookup_id = (
                                splits[-1] if len(splits) > 1 else server_id
                            )

                            oauth_token = await request.app.state.oauth_client_manager.get_oauth_token(
                                user.id, f"mcp:{oauth_lookup_id}"
                            )

                            if oauth_token:
                                bearer_token = (
                                    oauth_token.get("access_token", "") or None
                                )
                        except Exception as e:
                            log.error(f"Error getting OAuth token: {e}")
                            oauth_token = None

                    mcp_clients[original_server_id] = MCPClient()

                    connect_kwargs = build_mcp_connect_kwargs(
                        mcp_server_connection,
                        bearer_token=bearer_token,
                        user=user,
                        metadata=metadata,
                    )

                    await mcp_clients[original_server_id].connect(**connect_kwargs)

                    tool_specs = await mcp_clients[original_server_id].list_tool_specs()
                    for tool_spec in tool_specs:

                        def make_tool_function(client, function_name):
                            async def tool_function(**kwargs):
                                return await client.call_tool(
                                    function_name,
                                    function_args=kwargs,
                                )

                            return tool_function

                        tool_function = make_tool_function(
                            mcp_clients[original_server_id], tool_spec["name"]
                        )

                        # Model-facing names must satisfy provider constraints.
                        # Generic MCP servers use a hashed alias. The configured
                        # container MCP server keeps bash/read/write/edit direct
                        # when possible so the model sees the expected agent
                        # tool surface.
                        alias = mcp_model_facing_tool_name(
                            container_server_id=str(
                                getattr(
                                    request.app.state.config,
                                    "CONTAINER_MCP_SERVER_ID",
                                    "",
                                )
                                or ""
                            ),
                            server_id=original_server_id,
                            tool_name=tool_spec["name"],
                            existing_names=set(mcp_tools_dict.keys()),
                        )
                        log.debug(
                            "MCP tool model name: %s -> server=%s tool=%s",
                            alias,
                            original_server_id,
                            tool_spec["name"],
                        )

                        mcp_tools_dict[alias] = {
                            "spec": {
                                **tool_spec,
                                "name": alias,
                            },
                            "callable": tool_function,
                            "type": "mcp",
                            "client": mcp_clients[original_server_id],
                            "direct": False,
                            "metadata": {
                                "server_id": original_server_id,
                                "original_name": tool_spec["name"],
                                "annotations": tool_spec.get("annotations", {}),
                                "outputSchema": tool_spec.get("outputSchema"),
                                "parallelizable": bool(
                                    mcp_server_connection.get("parallelizable", False)
                                ),
                            },
                        }
                except Exception as e:
                    # Log with traceback at ERROR so MCP failures are
                    # visible without flipping the global log level, and
                    # drop the half-constructed client so the cleanup
                    # finally in process_chat doesn't try to disconnect a
                    # never-connected MCPClient.
                    log.exception(
                        "MCP server %r failed during connect/list_tool_specs",
                        original_server_id,
                    )
                    mcp_clients.pop(original_server_id, None)
                    server_name = (mcp_server_connection or {}).get("info", {}).get(
                        "name"
                    ) or original_server_id
                    mcp_failures.append(
                        {
                            "server_id": original_server_id,
                            "name": server_name,
                            "reason": f"{type(e).__name__}: {e}",
                        }
                    )
                    continue

        # Surface MCP load failures to the user via the chat event stream.
        # Previously these were log.exception-only, so a misconfigured server
        # silently produced an empty tool list and the model never saw the
        # tools the user thought they'd enabled.
        if mcp_failures:
            event_emitter = (
                extra_params.get("__event_emitter__") if extra_params else None
            )
            if event_emitter:
                for fail in mcp_failures:
                    try:
                        await event_emitter(
                            {
                                "type": "status",
                                "data": {
                                    "action": "mcp_server",
                                    "description": (
                                        f"MCP server '{fail['name']}' failed to load: "
                                        f"{fail['reason']}"
                                    ),
                                    "done": True,
                                    "error": True,
                                },
                            }
                        )
                    except Exception:
                        log.exception("Failed to emit MCP failure status")

        tools_dict = await get_tools(
            request,
            tool_ids,
            user,
            {
                **extra_params,
                "__model__": models[task_model_id],
                "__messages__": form_data["messages"],
                "__files__": metadata.get("files", []),
            },
        )
        if mcp_tools_dict:
            if tools_dict:
                rekeyed_mcp_tools_dict = {}
                for name, tool_dict in mcp_tools_dict.items():
                    final_name = name
                    if final_name in tools_dict or final_name in rekeyed_mcp_tools_dict:
                        metadata_dict = tool_dict.get("metadata", {}) or {}
                        server_id = metadata_dict.get("server_id", "")
                        original_name = metadata_dict.get("original_name") or name
                        final_name = mcp_tool_alias(server_id, original_name)
                        collision_idx = 1
                        while (
                            final_name in tools_dict
                            or final_name in rekeyed_mcp_tools_dict
                        ):
                            final_name = mcp_tool_alias(
                                f"{server_id}:{original_name}:{collision_idx}",
                                original_name,
                            )
                            collision_idx += 1

                    if final_name != name:
                        tool_dict = {
                            **tool_dict,
                            "spec": {
                                **(tool_dict.get("spec") or {}),
                                "name": final_name,
                            },
                        }
                    rekeyed_mcp_tools_dict[final_name] = tool_dict
                mcp_tools_dict = rekeyed_mcp_tools_dict

            tools_dict = {**tools_dict, **mcp_tools_dict}
            # Mirror the built-in pattern at L1616/L1642/L1663: any tool source
            # that emits real function-shaped specs needs native function
            # calling, otherwise the gate below routes through
            # chat_completion_tools_handler which never puts `tools=[...]` in
            # the outbound model request -- and the model never sees the MCP
            # tools.
            metadata.setdefault("params", {})["function_calling"] = "native"
            log.info(
                "Auto-enabled native function calling for %d MCP tool(s)",
                len(mcp_tools_dict),
            )

    if direct_tool_servers:
        for tool_server in direct_tool_servers:
            tool_specs = tool_server.pop("specs", [])

            for tool in tool_specs:
                tools_dict[tool["name"]] = {
                    "spec": tool,
                    "direct": True,
                    "server": tool_server,
                    "metadata": {
                        "parallelizable": bool(
                            tool_server.get("parallelizable", False)
                        ),
                    },
                }

    if mcp_clients:
        metadata["mcp_clients"] = mcp_clients

    if tools_dict:
        if metadata.get("params", {}).get("function_calling") == "native":
            # If the function calling is native, then call the tools function calling handler
            metadata["tools"] = tools_dict
            form_data["tools"] = [
                {
                    "type": "function",
                    "function": {
                        key: value
                        for key, value in (tool.get("spec", {}) or {}).items()
                        if key in {"name", "description", "parameters"}
                    },
                }
                for tool in tools_dict.values()
            ]
        else:
            # If the function calling is not native, then call the tools function calling handler
            try:
                form_data, flags = await chat_completion_tools_handler(
                    request, form_data, extra_params, user, models, tools_dict
                )
                sources.extend(flags.get("sources", []))
            except Exception as e:
                log.exception(e)

    # If context is not empty, insert it into the messages
    if len(sources) > 0:
        context_string = ""
        citation_idx_map = {}

        for source in sources:
            if "document" in source:
                for document_text, document_metadata in zip(
                    source["document"], source["metadata"]
                ):
                    source_name = source.get("source", {}).get("name", None)
                    source_id = (
                        document_metadata.get("source", None)
                        or source.get("source", {}).get("id", None)
                        or "N/A"
                    )

                    if source_id not in citation_idx_map:
                        citation_idx_map[source_id] = len(citation_idx_map) + 1

                    context_string += (
                        f'<source id="{citation_idx_map[source_id]}"'
                        + (f' name="{source_name}"' if source_name else "")
                        + f">{document_text}</source>\n"
                    )

        context_string = context_string.strip()
        if prompt is None:
            raise Exception("No user message found")

        if context_string != "":
            form_data["messages"] = add_or_update_user_message(
                rag_template(
                    request.app.state.config.RAG_TEMPLATE,
                    context_string,
                    prompt,
                ),
                form_data["messages"],
                append=False,
            )

    # If there are citations, add them to the data_items
    sources = [
        source
        for source in sources
        if source.get("source", {}).get("name", "")
        or source.get("source", {}).get("id", "")
    ]

    if len(sources) > 0:
        events.append({"sources": sources})

    return form_data, metadata, events


def _get_token_usage_chat_id(metadata: dict | None):
    """Chat id to use for conversation-level token analytics.

    Hidden subagent chats should not get separate conversation_token_usage rows;
    their LLM usage rolls up into the visible parent chat. The parent's later
    follow-up prompt is still counted normally as a separate parent model call.
    """
    if not isinstance(metadata, dict):
        return None
    if metadata.get("subagent_inner") and metadata.get("parent_chat_id"):
        return metadata.get("parent_chat_id")
    return metadata.get("chat_id")


def _chat_title_event_payload(chat_id: str, title: str) -> dict:
    chat = Chats.get_chat_by_id(chat_id) if chat_id else None
    if chat:
        return {
            "id": chat_id,
            "title": title,
            "updated_at": getattr(chat, "updated_at", None),
            "created_at": getattr(chat, "created_at", None),
            "pinned": bool(getattr(chat, "pinned", False) or False),
            "archived": bool(getattr(chat, "archived", False) or False),
            "folder_id": getattr(chat, "folder_id", None),
        }

    return {
        "id": chat_id,
        "title": title,
    }


def _provider_stream_enabled(form_data: dict | None, metadata: dict | None) -> bool:
    if isinstance(metadata, dict) and "provider_stream" in metadata:
        return bool(metadata.get("provider_stream"))
    if isinstance(form_data, dict):
        return bool(form_data.get("stream"))
    return False


def _should_handle_nonstreaming_response_in_agentic_loop(
    response, form_data: dict | None, metadata: dict | None
) -> bool:
    if isinstance(response, StreamingResponse):
        return False
    if not (
        isinstance(metadata, dict)
        and metadata.get("subagent_inner")
        and isinstance(form_data, dict)
        and form_data.get("stream")
    ):
        return False
    if not _provider_stream_enabled(form_data, metadata):
        return True
    return isinstance(response, dict) and "choices" in response


def _nonstreaming_round_length_error(res: dict) -> str | None:
    choices = res.get("choices") if isinstance(res, dict) else None
    if not choices:
        return None
    choice = choices[0] or {}
    if choice.get("finish_reason") != "length":
        return None
    message = choice.get("message") or {}
    if message.get("tool_calls") or message.get("content"):
        return None
    return (
        "Model reached the completion token limit before producing final text "
        "or a tool call. Increase the output token limit or lower reasoning effort."
    )


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


def _visible_nonstreaming_reasoning(message: dict) -> str:
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _visible_reasoning_from_details(message.get("reasoning_details"))


async def process_chat_response(
    request, response, form_data, user, metadata, model, events, tasks
):
    # Opt-in cProfile around the whole streaming handler (PROFILE_CHAT=1). The
    # wrapper is the only thing on the default path — when profiling is off it is
    # a single boolean check before delegating, so there is no measurable cost.
    if not PROFILE_CHAT:
        return await _process_chat_response_impl(
            request, response, form_data, user, metadata, model, events, tasks
        )

    import cProfile
    import time as _time
    from open_webui.utils.chat_profiler import dump_profile

    profiler = cProfile.Profile()
    started = _time.monotonic()
    profiler.enable()
    try:
        return await _process_chat_response_impl(
            request, response, form_data, user, metadata, model, events, tasks
        )
    finally:
        profiler.disable()
        dump_profile(
            profiler,
            PROFILE_CHAT_DIR,
            (metadata or {}).get("chat_id"),
            (metadata or {}).get("message_id"),
            started,
        )


async def _process_chat_response_impl(
    request, response, form_data, user, metadata, model, events, tasks
):
    async def background_tasks_handler():
        message = None
        messages = []

        if "chat_id" in metadata and not metadata["chat_id"].startswith("local:"):
            messages_map = Chats.get_messages_map_by_chat_id(metadata["chat_id"])
            message = messages_map.get(metadata["message_id"]) if messages_map else None

            message_list = get_message_list(messages_map, metadata["message_id"])

            # Remove details tags and files from the messages.
            # as get_message_list creates a new list, it does not affect
            # the original messages outside of this handler

            messages = []
            for message in message_list:
                content = message.get("content", "")
                if isinstance(content, list):
                    for item in content:
                        if item.get("type") == "text":
                            content = item["text"]
                            break

                if isinstance(content, str):
                    content = re.sub(
                        r"<details\b[^>]*>.*?<\/details>|!\[.*?\]\(.*?\)",
                        "",
                        content,
                        flags=re.S | re.I,
                    ).strip()

                messages.append(
                    {
                        **message,
                        "role": message.get(
                            "role", "assistant"
                        ),  # Safe fallback for missing role
                        "content": content,
                    }
                )
        else:
            # Local temp chat, get the model and message from the form_data
            message = get_last_user_message_item(form_data.get("messages", []))
            messages = form_data.get("messages", [])
            if message:
                message["model"] = form_data.get("model")

        if message and "model" in message:
            if tasks and messages:
                if (
                    TASKS.FOLLOW_UP_GENERATION in tasks
                    and tasks[TASKS.FOLLOW_UP_GENERATION]
                ):
                    res = await generate_follow_ups(
                        request,
                        {
                            "model": message["model"],
                            "messages": messages,
                            "message_id": metadata["message_id"],
                            "chat_id": metadata["chat_id"],
                        },
                        user,
                    )

                    if res and isinstance(res, dict):
                        if len(res.get("choices", [])) == 1:
                            response_message = res.get("choices", [])[0].get(
                                "message", {}
                            )

                            follow_ups_string = response_message.get(
                                "content"
                            ) or response_message.get("reasoning_content", "")
                        else:
                            follow_ups_string = ""

                        follow_ups_string = follow_ups_string[
                            follow_ups_string.find("{") : follow_ups_string.rfind("}")
                            + 1
                        ]

                        try:
                            follow_ups = json.loads(follow_ups_string).get(
                                "follow_ups", []
                            )
                            await event_emitter(
                                {
                                    "type": "chat:message:follow_ups",
                                    "data": {
                                        "follow_ups": follow_ups,
                                    },
                                }
                            )

                            if not metadata.get("chat_id", "").startswith("local:"):
                                Chats.upsert_message_to_chat_by_id_and_message_id(
                                    metadata["chat_id"],
                                    metadata["message_id"],
                                    {
                                        "followUps": follow_ups,
                                    }, return_model=False
                                )

                        except Exception as e:
                            pass

                if not metadata.get("chat_id", "").startswith(
                    "local:"
                ):  # Only update titles and tags for non-temp chats
                    if (
                        TASKS.TITLE_GENERATION in tasks
                        and tasks[TASKS.TITLE_GENERATION]
                    ):
                        user_message = get_last_user_message(messages)
                        if user_message and len(user_message) > 100:
                            user_message = user_message[:100] + "..."

                        if tasks[TASKS.TITLE_GENERATION]:

                            res = await generate_title(
                                request,
                                {
                                    "model": message["model"],
                                    "messages": messages,
                                    "chat_id": metadata["chat_id"],
                                },
                                user,
                            )

                            if res and isinstance(res, dict):
                                if len(res.get("choices", [])) == 1:
                                    response_message = res.get("choices", [])[0].get(
                                        "message", {}
                                    )

                                    title_string = (
                                        response_message.get("content")
                                        or response_message.get(
                                            "reasoning_content",
                                        )
                                        or message.get("content", user_message)
                                    )
                                else:
                                    title_string = ""

                                title_string = title_string[
                                    title_string.find("{") : title_string.rfind("}") + 1
                                ]

                                try:
                                    title = json.loads(title_string).get(
                                        "title", user_message
                                    )
                                except Exception as e:
                                    title = ""

                                if not title:
                                    title = messages[0].get("content", user_message)

                                Chats.update_chat_title_by_id(
                                    metadata["chat_id"], title
                                )

                                await event_emitter(
                                    {
                                        "type": "chat:title",
                                        "data": _chat_title_event_payload(
                                            metadata["chat_id"], title
                                        ),
                                    }
                                )
                        elif len(messages) == 2:
                            title = messages[0].get("content", user_message)

                            Chats.update_chat_title_by_id(metadata["chat_id"], title)

                            await event_emitter(
                                {
                                    "type": "chat:title",
                                    "data": _chat_title_event_payload(
                                        metadata["chat_id"], title
                                    ),
                                }
                            )

                    if TASKS.TAGS_GENERATION in tasks and tasks[TASKS.TAGS_GENERATION]:
                        res = await generate_chat_tags(
                            request,
                            {
                                "model": message["model"],
                                "messages": messages,
                                "chat_id": metadata["chat_id"],
                            },
                            user,
                        )

                        if res and isinstance(res, dict):
                            if len(res.get("choices", [])) == 1:
                                response_message = res.get("choices", [])[0].get(
                                    "message", {}
                                )

                                tags_string = response_message.get(
                                    "content"
                                ) or response_message.get("reasoning_content", "")
                            else:
                                tags_string = ""

                            tags_string = tags_string[
                                tags_string.find("{") : tags_string.rfind("}") + 1
                            ]

                            try:
                                tags = json.loads(tags_string).get("tags", [])
                                Chats.update_chat_tags_by_id(
                                    metadata["chat_id"], tags, user
                                )

                                await event_emitter(
                                    {
                                        "type": "chat:tags",
                                        "data": {
                                            "id": metadata["chat_id"],
                                            "tags": tags,
                                        },
                                    }
                                )
                            except Exception as e:
                                pass

    event_emitter = None
    event_caller = None

    # Build the socket emitter/caller when we have a chat+message to target AND
    # either a real originating socket session OR this is a headless run (the
    # autonomous queue drain, which has no session_id by design). For a headless
    # run `get_event_emitter` with session_id=None fans out to ALL of the user's
    # tabs (USER_POOL) and `_wrap_event_emitter_v2` still registers stream state
    # for reattach — so a drained generation streams + persists + is recoverable
    # exactly like a session-bearing one.
    if (
        "chat_id" in metadata
        and metadata["chat_id"]
        and "message_id" in metadata
        and metadata["message_id"]
        and (metadata.get("session_id") or metadata.get("headless"))
    ):
        # Subagent runs install custom emitter/caller in metadata so the inner
        # pipeline's events get forwarded to the parent UI as
        # `chat:subagent:update` events (see utils/subagent.py). For normal
        # chats these keys are absent and we fall back to the default
        # socket-scoped emitter/caller.
        event_emitter = metadata.get("event_emitter_override") or get_event_emitter(
            metadata
        )
        # Headless runs (no session_id) can't await a client ack, so use a
        # non-blocking caller that declines interactive callbacks.
        if metadata.get("event_caller_override"):
            event_caller = metadata["event_caller_override"]
        elif metadata.get("session_id"):
            event_caller = get_event_call(metadata)
        else:
            event_caller = get_headless_event_call(metadata)

        if STREAM_PROTOCOL_VERSION == "v2" and not metadata.get(
            "event_emitter_override"
        ):
            event_emitter = _wrap_event_emitter_v2(event_emitter, metadata)

    model_id = form_data.get("model", "")

    # Queue finalization for the NON-STREAMING completion path. The streaming
    # handler does this at its tail; the non-streaming branches (pipe/function
    # models, or providers that return a single JSON body even when stream:true
    # was requested) must mirror it or a headless drain would WEDGE: the
    # `draining` marker is owned by this response id, and the ownership guard
    # makes EVERY future drain bail until it's cleared. Called from BOTH arms of
    # the event_emitter/response_data check below (including the empty-response
    # case) so no non-streaming completion can leave the queue stuck.
    _ns_finalized = {"done": False}

    async def _finalize_nonstreaming_queue(errored: bool):
        if _ns_finalized["done"]:
            return
        _ns_finalized["done"] = True
        if not (metadata.get("chat_id") and metadata.get("message_id")):
            return
        if str(metadata.get("chat_id", "")).startswith("local:"):
            return
        # Settle the v2 stream store so a reloaded/zero-tab client sees a terminal
        # state (not a perpetual "in_progress" cursor on the headless placeholder
        # registered before the generation).
        if STREAM_PROTOCOL_VERSION == "v2":
            try:
                set_stream_state(
                    metadata["message_id"],
                    {"status": "error" if errored else "done"},
                )
                clear_stream_state(metadata["message_id"])
            except Exception:
                log.exception("non-streaming stream-state settle failed")
        if not errored:
            try:
                Chats.upsert_message_to_chat_by_id_and_message_id(
                    metadata["chat_id"],
                    metadata["message_id"],
                    {"done": True},
                    return_model=False,
                )
            except Exception:
                log.exception("non-streaming done:true persist failed")
        if errored:
            # Genuine error → PAUSE the queue (clear only our own marker).
            try:
                from open_webui.utils.chat_queue import clear_draining

                await clear_draining(
                    getattr(request.app.state, "redis", None),
                    metadata["chat_id"],
                    finished_response_id=metadata.get("message_id"),
                    user_id=metadata.get("user_id"),
                )
            except Exception:
                log.exception("queue clear_draining (non-streaming error) failed")
        else:
            # Clean completion → drain the next queued follow-up.
            try:
                from open_webui.utils.chat_queue import maybe_drain_queue

                await maybe_drain_queue(
                    request.app,
                    user,
                    metadata["chat_id"],
                    finished_response_id=metadata.get("message_id"),
                )
            except Exception:
                log.exception("queue drain after non-streaming completion failed")

    agentic_nonstreaming_response = _should_handle_nonstreaming_response_in_agentic_loop(
        response, form_data, metadata
    )
    if agentic_nonstreaming_response and not (event_emitter and event_caller):
        agentic_nonstreaming_response = False

    # Non-streaming response
    if not isinstance(response, StreamingResponse) and not agentic_nonstreaming_response:
        # First, extract and process reasoning content for ALL non-streaming responses
        # This must happen before the event_emitter check to ensure API responses include reasoning
        response_data = None
        if isinstance(response, dict) or isinstance(response, JSONResponse):
            if isinstance(response, list) and len(response) == 1:
                # If the response is a single-item list, unwrap it #17213
                response = response[0]

            if isinstance(response, JSONResponse) and isinstance(response.body, bytes):
                try:
                    response_data = json.loads(response.body.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    response_data = {"error": {"detail": "Invalid JSON response"}}
            else:
                response_data = response

            # Process reasoning content for ALL responses (not just those with event_emitter)
            if response_data and "choices" in response_data:
                choices = response_data.get("choices", [])
                if choices and choices[0].get("message"):
                    message = response_data["choices"][0]["message"]
                    content = message.get("content", "")
                    reasoning_content = _visible_nonstreaming_reasoning(message)

                    # If reasoning content exists, format it as HTML details tag
                    if reasoning_content:
                        reasoning_display_content = "\n".join(
                            (f"> {line}" if not line.startswith(">") else line)
                            for line in reasoning_content.splitlines()
                        )

                        # Format as HTML details tag for frontend display
                        reasoning_html = f'<details type="reasoning" done="true">\n<summary>Thought</summary>\n{reasoning_display_content}\n</details>\n'

                        # Prepend reasoning before the main content
                        content = f"{reasoning_html}{content}"

                        # Update response_data so reasoning is included in API response
                        response_data["choices"][0]["message"]["content"] = content
                        # Remove separate reasoning fields to avoid confusion
                        response_data["choices"][0]["message"].pop(
                            "reasoning_content", None
                        )
                        response_data["choices"][0]["message"].pop("reasoning", None)
                        response_data["choices"][0]["message"].pop("thinking", None)

                    # Update response object with modified data
                    if isinstance(response, dict):
                        response = response_data
                    elif isinstance(response, JSONResponse):
                        response = JSONResponse(
                            content=response_data,
                            headers=response.headers,
                            status_code=response.status_code,
                        )

        # Now handle event emitter logic (saving to database, etc.)
        if event_emitter and response_data:
            try:
                if "error" in response_data:
                    error = response_data.get("error")

                    if isinstance(error, dict):
                        error = error.get("detail", error)
                    else:
                        error = str(error)

                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            "error": {"content": error},
                        }, return_model=False
                    )
                    if isinstance(error, str) or isinstance(error, dict):
                        await event_emitter(
                            {
                                "type": "chat:message:error",
                                "data": {"error": {"content": error}},
                            }
                        )

                if "selected_model_id" in response_data:
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            "selectedModelId": response_data["selected_model_id"],
                        }, return_model=False
                    )

                # Get content from message (reasoning already processed earlier)
                choices = response_data.get("choices", [])
                if choices and choices[0].get("message"):
                    content = response_data["choices"][0]["message"].get("content", "")

                    if content:
                        # Check for usage data in non-streaming response
                        usage = response_data.get("usage", {})

                        await event_emitter(
                            {
                                "type": "chat:completion",
                                "data": response_data,
                            }
                        )

                        title = Chats.get_chat_title_by_id(metadata["chat_id"])
                        container_output_files = await import_changed_container_outputs(
                            request, metadata, user, content=content
                        )
                        if container_output_files:
                            await event_emitter(
                                {
                                    "type": "files",
                                    "data": {"files": container_output_files},
                                }
                            )

                        # Include usage in the final completion event
                        completion_data = {
                            "done": True,
                            "content": content,
                            "title": title,
                        }
                        if container_output_files:
                            completion_data["files"] = container_output_files
                        if usage:
                            completion_data["usage"] = usage
                            completion_data["selected_model_id"] = model_id

                        await event_emitter(
                            {
                                "type": "chat:completion",
                                "data": completion_data,
                            }
                        )

                        # Save message in the database with reasoning included
                        update_data = {
                            "role": "assistant",
                            "content": content,
                        }

                        if usage:
                            update_data["usage"] = usage
                        if container_output_files:
                            current_message = (
                                Chats.get_message_by_id_and_message_id(
                                    metadata["chat_id"], metadata["message_id"]
                                )
                                or {}
                            )
                            update_data["files"] = current_message.get(
                                "files", container_output_files
                            )

                        reasoning_details = response_data["choices"][0]["message"].get(
                            "reasoning_details"
                        )
                        if reasoning_details:
                            update_data["reasoning_details"] = reasoning_details
                            # Write per_round too so the on-disk shape is
                            # symmetric with streaming. See
                            # utils/REASONING_DETAILS.md §6 Bug C.
                            update_data["reasoning_details_per_round"] = [
                                reasoning_details
                            ]

                        Chats.upsert_message_to_chat_by_id_and_message_id(
                            metadata["chat_id"],
                            metadata["message_id"],
                            update_data, return_model=False
                        )

                        # Send a webhook notification if the user is not active
                        if not get_active_status_by_user_id(user.id):
                            webhook_url = Users.get_user_webhook_url_by_id(user.id)
                            if webhook_url:
                                await post_webhook(
                                    request.app.state.WEBUI_NAME,
                                    webhook_url,
                                    f"{title} - {request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}\n\n{content}",
                                    {
                                        "action": "chat",
                                        "message": content,
                                        "title": title,
                                        "url": f"{request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}",
                                    },
                                )

                        await background_tasks_handler()

                if events and isinstance(events, list):
                    extra_response = {}
                    for event in events:
                        if isinstance(event, dict):
                            extra_response.update(event)
                        else:
                            extra_response[event] = True

                    response_data = {
                        **extra_response,
                        **response_data,
                    }

                if isinstance(response, dict):
                    response = response_data
                if isinstance(response, JSONResponse):
                    response = JSONResponse(
                        content=response_data,
                        headers=response.headers,
                        status_code=response.status_code,
                    )

            except Exception as e:
                log.debug(f"Error occurred while processing request: {e}")
                pass

            await _finalize_nonstreaming_queue(
                isinstance(response_data, dict) and bool(response_data.get("error"))
            )
            return response
        else:
            await _finalize_nonstreaming_queue(False)
            if events and isinstance(events, list) and isinstance(response, dict):
                extra_response = {}
                for event in events:
                    if isinstance(event, dict):
                        extra_response.update(event)
                    else:
                        extra_response[event] = True

                response = {
                    **extra_response,
                    **response,
                }

            return response

    # Non standard response
    if isinstance(response, StreamingResponse):
        response_content_type = response.headers.get("Content-Type", "")
        if not any(
            content_type in response_content_type
            for content_type in ["text/event-stream", "application/x-ndjson"]
        ):
            return response
    elif not agentic_nonstreaming_response:
        return response

    oauth_token = None
    try:
        if request.cookies.get("oauth_session_id", None):
            oauth_token = await request.app.state.oauth_manager.get_oauth_token(
                user.id,
                request.cookies.get("oauth_session_id", None),
            )
    except Exception as e:
        log.error(f"Error getting OAuth token: {e}")

    extra_params = {
        "__event_emitter__": event_emitter,
        "__event_call__": event_caller,
        "__user__": user.model_dump() if isinstance(user, UserModel) else {},
        "__metadata__": metadata,
        "__oauth_token__": oauth_token,
        "__request__": request,
        "__model__": model,
    }
    filter_functions = [
        Functions.get_function_by_id(filter_id)
        for filter_id in get_sorted_filter_ids(
            request, model, metadata.get("filter_ids", [])
        )
    ]

    # Streaming response
    if event_emitter and event_caller:
        task_id = str(uuid4())  # Create a unique task ID.

        def split_content_and_whitespace(content):
            content_stripped = content.rstrip()
            original_whitespace = (
                content[len(content_stripped) :]
                if len(content) > len(content_stripped)
                else ""
            )
            return content_stripped, original_whitespace

        def is_opening_code_block(content):
            backtick_segments = content.split("```")
            # Even number of segments means the last backticks are opening a new block
            return len(backtick_segments) > 1 and len(backtick_segments) % 2 == 0

        # Handle as a background task
        async def response_handler(response, events):
            nonlocal model_id

            def serialize_content_blocks(content_blocks, force=False):
                # Display-only HTML+markdown projection of the structured content_blocks.
                # The API-bound conversion lives in `blocks_to_api_messages`; this is
                # purely what the UI's existing Markdown renderer + native <details>
                # collapsibles consume. Kept for older frontend builds that don't
                # render directly from content_blocks (post-Task 5 frontends do).
                #
                # Hot-path short-circuits (skipped when `force=True`):
                #
                # 1) Subagent inner runs never read the projected `content` string —
                #    `SubagentBlock.svelte` renders the structured `content_blocks`
                #    array directly. Returning empty here turns the per-chunk O(N)
                #    walk into O(1), so backend per-stream work scales linearly
                #    with token count even with many concurrent subagents at 200+
                #    TPS. The subagent chat row's `content` column ends up empty
                #    but the row is hidden from the sidebar and re-renders
                #    correctly from `content_blocks` if the user opens it directly.
                #
                # 2) Regular chats with `ENABLE_REALTIME_CHAT_SAVE=False` (the
                #    default): no per-chunk DB write happens, and modern
                #    frontends render from `content_blocks` (see
                #    `ContentRenderer.svelte`'s per-block keyed-each). The
                #    projected string is only needed once at end-of-stream for
                #    the canonical DB write + legacy clients + exports — those
                #    call sites pass `force=True` to bypass this short-circuit.
                #
                # When `ENABLE_REALTIME_CHAT_SAVE=True`, every per-chunk call
                # falls through and computes normally so the per-chunk DB write
                # at L2836 stores a coherent content column.
                #
                # 3) Under STREAM_PROTOCOL_VERSION="v2" (B9): the wire
                #    translator (`_wrap_event_emitter_v2`) drops the `content`
                #    string entirely and ships `chat:delta` ops derived from
                #    `content_blocks`. Per-chunk DB writes under v2 also skip
                #    the `content` column (see hot-path upsert below). The
                #    `content` column converges at end-of-stream via the
                #    `force=True` call in the success/cancel finalisers, so
                #    legacy clients, exports, and search indexing still get a
                #    populated row once streaming completes.
                if not force:
                    if metadata.get("subagent_inner"):
                        return ""
                    if STREAM_PROTOCOL_VERSION == "v2":
                        return ""
                    if not ENABLE_REALTIME_CHAT_SAVE:
                        return ""

                content = ""

                for block in content_blocks:
                    if block["type"] == "text":
                        block_content = block["content"].strip()
                        if block_content:
                            content = f"{content}{block_content}\n"
                    elif block["type"] == "tool_calls":
                        attributes = block.get("attributes", {})

                        tool_calls = block.get("content", [])
                        results = block.get("results", [])

                        if content and not content.endswith("\n"):
                            content += "\n"

                        # Look up subagent_id either from the completed result
                        # (set by `_execute_tool_call` after the tool returns)
                        # or from the in-flight side channel that the subagent
                        # tool stamps right at the start of its execution
                        # (before it blocks on the inner chat). This way, even
                        # during the long-running window between the parent
                        # model emitting the tool call and the tool returning,
                        # serialize_content_blocks renders a subagent block
                        # instead of a generic "Executing..." tool_call.
                        inflight_subagent_id_by_tcid = {}
                        try:
                            inflight_subagent_id_by_tcid = (
                                getattr(request.state, "subagent_id_by_tool_call", {})
                                or {}
                            )
                        except Exception:
                            inflight_subagent_id_by_tcid = {}

                        def _is_subagent_tool(name: str) -> bool:
                            return name in ("subagent_launch", "subagent_continue")

                        if results:

                            tool_calls_display_content = ""
                            for tool_call in tool_calls:

                                tool_call_id = tool_call.get("id", "")
                                tool_name = tool_call.get("function", {}).get(
                                    "name", ""
                                )
                                tool_arguments = tool_call.get("function", {}).get(
                                    "arguments", ""
                                )

                                tool_result = None
                                tool_result_files = None
                                result_subagent_id = None
                                result_error = False
                                result_error_reason = ""
                                result_notice = ""
                                for result in results:
                                    if tool_call_id == result.get("tool_call_id", ""):
                                        tool_result = result.get("content", None)
                                        tool_result_files = result.get("files", None)
                                        result_subagent_id = result.get("subagent_id")
                                        result_error = bool(result.get("error"))
                                        result_error_reason = result.get(
                                            "error_reason", ""
                                        ) or ""
                                        result_notice = result.get("notice", "") or ""
                                        break

                                # Structured error/notice attributes shared by the
                                # `done="true"` tool_calls writers below. Reload
                                # parses these back into Collapsible attributes so
                                # the collapsed row shows the error/notice exactly
                                # like the live path does.
                                tool_meta_attrs = (
                                    (' error="true"' if result_error else "")
                                    + (
                                        f' error_reason="{html.escape(str(result_error_reason))}"'
                                        if result_error_reason
                                        else ""
                                    )
                                    + (
                                        f' notice="{html.escape(str(result_notice))}"'
                                        if result_notice
                                        else ""
                                    )
                                )

                                if _is_subagent_tool(tool_name):
                                    # Subagent block: lives in `subagentLiveStates`
                                    # keyed by tool_call_id on the frontend; the
                                    # markdown projection here is just a stub the
                                    # `Collapsible.svelte` renderer recognises.
                                    sa_id = (
                                        result_subagent_id
                                        or inflight_subagent_id_by_tcid.get(
                                            tool_call_id
                                        )
                                        or ""
                                    )
                                    if not sa_id and tool_result is not None:
                                        # Malformed subagent call: the tool errored
                                        # BEFORE creating a subagent (e.g. missing
                                        # name/prompt args), so there is no subagent
                                        # to render. Emit a normal tool-result stub
                                        # instead of a subagent stub — otherwise the
                                        # UI shows a perpetual "Researching…/Subagent
                                        # is starting up…" for a call that already
                                        # returned an error.
                                        tool_result_embeds = result.get("embeds", "")
                                        tool_calls_display_content = f'{tool_calls_display_content}<details type="tool_calls" done="true" id="{tool_call_id}" name="{tool_name}" arguments="{html.escape(json.dumps(tool_arguments))}" result="{html.escape(json.dumps(tool_result, ensure_ascii=False))}" files="{html.escape(json.dumps(tool_result_files)) if tool_result_files else ""}" embeds="{html.escape(json.dumps(tool_result_embeds))}"{tool_meta_attrs}>\n<summary>Tool Executed</summary>\n</details>\n'
                                    else:
                                        done_flag = (
                                            "true" if tool_result is not None else "false"
                                        )
                                        tool_calls_display_content = (
                                            f"{tool_calls_display_content}"
                                            f'<details type="subagent_launch" done="{done_flag}" '
                                            f'tool_call_id="{html.escape(tool_call_id)}" '
                                            f'id="{html.escape(sa_id)}" '
                                            f'name="{html.escape(tool_name)}" '
                                            f'arguments="{html.escape(json.dumps(tool_arguments))}">\n'
                                            f"<summary>Subagent</summary>\n"
                                            f"</details>\n"
                                        )
                                elif tool_result is not None:
                                    tool_result_embeds = result.get("embeds", "")
                                    tool_calls_display_content = f'{tool_calls_display_content}<details type="tool_calls" done="true" id="{tool_call_id}" name="{tool_name}" arguments="{html.escape(json.dumps(tool_arguments))}" result="{html.escape(json.dumps(tool_result, ensure_ascii=False))}" files="{html.escape(json.dumps(tool_result_files)) if tool_result_files else ""}" embeds="{html.escape(json.dumps(tool_result_embeds))}"{tool_meta_attrs}>\n<summary>Tool Executed</summary>\n</details>\n'
                                else:
                                    tool_calls_display_content = f'{tool_calls_display_content}<details type="tool_calls" done="false" id="{tool_call_id}" name="{tool_name}" arguments="{html.escape(json.dumps(tool_arguments))}">\n<summary>Executing...</summary>\n</details>\n'

                            content = f"{content}{tool_calls_display_content}"
                        else:
                            tool_calls_display_content = ""

                            for tool_call in tool_calls:
                                tool_call_id = tool_call.get("id", "")
                                tool_name = tool_call.get("function", {}).get(
                                    "name", ""
                                )
                                tool_arguments = tool_call.get("function", {}).get(
                                    "arguments", ""
                                )

                                if _is_subagent_tool(tool_name):
                                    sa_id = (
                                        inflight_subagent_id_by_tcid.get(tool_call_id)
                                        or ""
                                    )
                                    tool_calls_display_content = (
                                        f"{tool_calls_display_content}\n"
                                        f'<details type="subagent_launch" done="false" '
                                        f'tool_call_id="{html.escape(tool_call_id)}" '
                                        f'id="{html.escape(sa_id)}" '
                                        f'name="{html.escape(tool_name)}" '
                                        f'arguments="{html.escape(json.dumps(tool_arguments))}">\n'
                                        f"<summary>Subagent</summary>\n"
                                        f"</details>\n"
                                    )
                                else:
                                    tool_calls_display_content = f'{tool_calls_display_content}\n<details type="tool_calls" done="false" id="{tool_call_id}" name="{tool_name}" arguments="{html.escape(json.dumps(tool_arguments))}">\n<summary>Executing...</summary>\n</details>\n'

                            content = f"{content}{tool_calls_display_content}"

                    elif block["type"] == "reasoning":
                        reasoning_display_content = "\n".join(
                            (f"> {line}" if not line.startswith(">") else line)
                            for line in block["content"].splitlines()
                        )

                        reasoning_duration = block.get("duration", None)

                        if content and not content.endswith("\n"):
                            content += "\n"

                        if reasoning_duration is not None:
                            content = f'{content}<details type="reasoning" done="true" duration="{reasoning_duration}">\n<summary>Thought for {reasoning_duration} seconds</summary>\n{reasoning_display_content}\n</details>\n'
                        else:
                            content = f'{content}<details type="reasoning" done="false">\n<summary>Thinking…</summary>\n{reasoning_display_content}\n</details>\n'
                    elif block["type"] == "user_steer":
                        # A mid-task user interjection (steering). Render as a
                        # labeled blockquote so legacy/export projections read
                        # naturally; modern frontends render it from the
                        # structured block via ContentRenderer.
                        steer_content = str(block.get("content", "")).strip()
                        if steer_content:
                            if content and not content.endswith("\n"):
                                content += "\n"
                            quoted = "\n".join(
                                f"> {line}" for line in steer_content.splitlines()
                            )
                            content = f"{content}**User:**\n{quoted}\n"
                    else:
                        block_content = str(block["content"]).strip()
                        if block_content:
                            content = f"{content}{block['type']}: {block_content}\n"

            message = Chats.get_message_by_id_and_message_id(
                metadata["chat_id"], metadata["message_id"]
            )

            tool_calls = []

            last_assistant_message = None
            try:
                if form_data["messages"][-1]["role"] == "assistant":
                    last_assistant_message = get_last_assistant_message(
                        form_data["messages"]
                    )
            except Exception as e:
                pass

            content = (
                message.get("content", "")
                if message
                else last_assistant_message if last_assistant_message else ""
            )

            response_usage = None  # Initialize response_usage at the top level
            terminal_error = None
            chunk_count = 0  # Initialize chunk_count at the top level for logging
            # Set by _run_round_with_retry: True when it already folded the round's
            # response into content_blocks (streaming or non-streaming), so the
            # caller's dispatch ladder must NOT re-consume it.
            _round_already_consumed = False

            existing_content_blocks = (
                message.get("content_blocks") if isinstance(message, dict) else None
            )
            persisted_tool_result_bodies = (
                copy.deepcopy(message.get("tool_result_bodies") or {})
                if isinstance(message, dict)
                else {}
            )
            if metadata.get("message_id") and isinstance(
                persisted_tool_result_bodies, dict
            ):
                for _tcid, _body in persisted_tool_result_bodies.items():
                    if isinstance(_body, dict):
                        set_tool_result_body(metadata.get("message_id"), _tcid, _body)

            # Retry-last-request can pre-seed the assistant row with completed
            # tool-call rounds. Continue streaming from those structured blocks
            # instead of flattening them to a single text block, otherwise v2
            # would resend the whole agentic turn instead of just the final
            # post-tool request.
            if isinstance(existing_content_blocks, list) and existing_content_blocks:
                content_blocks = copy.deepcopy(existing_content_blocks)
            else:
                # Only pre-populate a text block when there is already content to carry
                # forward (e.g. a tool-call continuation).  An empty initial text block
                # would cause the late-arrival guard below to discard ALL reasoning
                # tokens (because _last_block_type would be "text" from the very first
                # chunk), breaking both reasoning display and streaming UX.
                content_blocks = (
                    [
                        {
                            "type": "text",
                            "content": content,
                        }
                    ]
                    if content
                    else []
                )

            # ── O(1)-amortized tail accumulation (streaming hot path) ───────
            # The active text/reasoning block's `content` grows one token per
            # chunk. `block["content"] += value` per token is a dict-subscript
            # concat: O(N) per token → O(N^2) per stream, multiple seconds of
            # pure event-loop block on long responses (the stall). We accumulate
            # into a `_StreamTextAccumulator` (parts list joined lazily) bound to
            # the current tail block by identity, folding it back into
            # `block["content"]` only at boundaries/readers via
            # `_tail_materialize()`. Defined here in `response_handler` scope (not
            # inside `stream_body_handler`) so the checkpoint/finalize paths can
            # materialize the tail before reading content_blocks. `_tail_state`
            # is a holder because `stream_body_handler` rebinds `content_blocks`
            # via `nonlocal` each round and the bound block changes.
            _tail_state = {"acc": None, "block": None}

            def _tail_materialize():
                """Fold buffered tail text back into its block's `content` so every
                cold reader (checkpoint, snapshot, serialize, block-boundary logic,
                finalizers) sees the full string. Cheap, idempotent no-op when
                nothing is buffered."""
                acc = _tail_state["acc"]
                blk = _tail_state["block"]
                if acc is not None and blk is not None:
                    blk["content"] = acc.materialize()

            def _tail_bind(block):
                """Bind the accumulator to `block` (the current tail),
                materializing any previously-bound block first. Seeds with the
                block's existing content as already-emitted (the mirror/snapshot
                already know it — contract of _StreamTextAccumulator)."""
                if _tail_state["block"] is block and _tail_state["acc"] is not None:
                    return
                _tail_materialize()
                _tail_state["acc"] = _StreamTextAccumulator(
                    block.get("content", "") or ""
                )
                _tail_state["block"] = block

            def _tail_append_text(block, value):
                """Append a pure-text delta to the tail block in O(len(value))."""
                if _tail_state["block"] is not block or _tail_state["acc"] is None:
                    _tail_bind(block)
                _tail_state["acc"].append(value)

            def _tail_append_reasoning(block, chunk):
                """Append a reasoning delta, defending against providers that
                resend cumulative content. Returns new-char count for checkpoint
                accounting.

                Bounded-suffix merge: for true incremental deltas
                (len(chunk) < current length) only the last len(chunk) chars are
                needed to detect a tail overlap — verified byte-identical to a full
                `_merge_streamed_string` over 14k+ randomized cases. Cumulative
                resends (len(chunk) >= current length) fall back to the full merge
                against the materialized string."""
                if _tail_state["block"] is not block or _tail_state["acc"] is None:
                    _tail_bind(block)
                acc = _tail_state["acc"]
                if not chunk:
                    return 0
                cur_len = len(acc)
                # INVARIANT: _merge_streamed_string(existing, chunk) ALWAYS returns
                # a string starting with `existing` (proven over 2M randomized
                # cases — every return path either is `existing`, is `chunk` where
                # chunk.startswith(existing), or is `existing + <suffix>`). So the
                # merge can only ever APPEND to what we already have; it never
                # rewrites a prefix. We rely on that here to compute the appended
                # tail cheaply.
                if len(chunk) >= cur_len:
                    # Possible cumulative resend (provider re-sends the whole
                    # reasoning so far): the full string is needed to detect how
                    # much is new.
                    existing = acc.materialize()
                    merged = _merge_streamed_string(existing, chunk)
                    appended = merged[len(existing) :]
                    if appended:
                        acc.append(appended)
                    return len(appended)
                # True incremental delta: only the last len(chunk) chars of the
                # current content can overlap the chunk (merge's max overlap is
                # len(chunk)), so a bounded suffix is sufficient and equivalent to
                # the full merge — verified byte-identical over 500k cases incl.
                # repeating patterns.
                suffix = acc.suffix(len(chunk))
                merged = _merge_streamed_string(suffix, chunk)
                appended = merged[len(suffix) :]
                if appended:
                    acc.append(appended)
                return len(appended)

            # Per-round reasoning_details, in order. Each stream_body_handler
            # invocation appends one entry: the reasoning_details captured during
            # that round (whether or not the round produced tool_calls). This
            # preserves multi-round reasoning continuity for OpenAI's Responses
            # API on tool-call replays — without this, the saved flat array
            # cannot distinguish which round each reasoning item came from, and
            # earlier rounds get attached to the wrong assistant message on
            # follow-up turns.
            existing_reasoning_per_round = (
                message.get("reasoning_details_per_round")
                if isinstance(message, dict)
                else None
            )
            round_reasoning_details = (
                copy.deepcopy(existing_reasoning_per_round)
                if isinstance(existing_reasoning_per_round, list)
                else []
            )

            if (
                STREAM_PROTOCOL_VERSION == "v2"
                and content_blocks
                and not str(metadata.get("chat_id", "")).startswith("local:")
            ):
                # Existing rows from before lazy web-result refs may still carry
                # full web_fetch bodies inline. Split them once at stream start
                # so subsequent native appends don't repeatedly hash/copy huge
                # tool payloads.
                content_blocks, initial_tool_bodies = split_tool_result_bodies(
                    content_blocks
                )
                if metadata.get("message_id"):
                    for _tcid, _body in initial_tool_bodies.items():
                        set_tool_result_body(metadata.get("message_id"), _tcid, _body)

            if (
                STREAM_PROTOCOL_VERSION == "v2"
                and metadata.get("message_id")
                and content_blocks
            ):
                initial_v2_blocks = copy.deepcopy(_strip_tool_results(content_blocks))
                v2_mirror = getattr(event_emitter, "_v2_mirror", None)
                if v2_mirror is not None:
                    v2_mirror["blocks"] = initial_v2_blocks
                set_stream_state(
                    metadata["message_id"],
                    {
                        "content_blocks": initial_v2_blocks,
                        "status": "in_progress",
                        # Baseline snapshot_version so the /snapshot endpoint never
                        # advertises the live wire counter (which races ahead of the
                        # cadence-written RAM content) before the first cadence
                        # snapshot lands. The content here matches the current
                        # version (no deltas emitted yet this round).
                        "snapshot_version": stream_version_get(
                            metadata["message_id"]
                        ),
                    },
                )

            # Avoid copying the whole growing plain-text response on every SSE
            # chunk. Native provider reasoning fields are rendered from
            # structured `content_blocks`; legacy inline reasoning-tag scanning
            # has been removed. Hidden v2 subagent runs never need the legacy
            # string.
            track_legacy_content = not (
                STREAM_PROTOCOL_VERSION == "v2" and metadata.get("subagent_inner")
            )
            content_parts = [content] if (track_legacy_content and content) else []
            content_dirty = False

            def append_plain_content(value: str):
                nonlocal content, content_parts, content_dirty
                if not value or not track_legacy_content:
                    return
                content_parts.append(value)
                content_dirty = True

            last_checkpoint_at = time.monotonic()
            checkpoint_chars_since = 0
            CHECKPOINT_INTERVAL_SECONDS = 2.0
            CHECKPOINT_CHAR_DELTA = 16_384

            def get_plain_content() -> str:
                nonlocal content, content_dirty
                if not track_legacy_content:
                    return content
                if content_dirty:
                    content = "".join(content_parts)
                    content_dirty = False
                return content

            def _build_checkpoint_update(include_legacy_content: bool = False):
                # Fold any buffered tail text into its block before reading
                # content_blocks, so the checkpoint/snapshot/persist carries the
                # full text (the streaming hot path leaves the tail in an
                # accumulator for O(1) appends).
                _tail_materialize()
                if STREAM_PROTOCOL_VERSION == "v2" and not str(
                    metadata.get("chat_id", "")
                ).startswith("local:"):
                    slim_blocks, split_bodies = split_tool_result_bodies(content_blocks)
                    tool_result_bodies = {
                        **(
                            persisted_tool_result_bodies
                            if isinstance(persisted_tool_result_bodies, dict)
                            else {}
                        ),
                        **get_tool_result_bodies(
                            metadata.get("message_id"), deep_copy=False
                        ),
                        **split_bodies,
                    }
                else:
                    slim_blocks, tool_result_bodies = content_blocks, {}
                update_data = {
                    "content_blocks": slim_blocks,
                }
                if tool_result_bodies:
                    update_data["tool_result_bodies"] = tool_result_bodies
                if include_legacy_content:
                    update_data["content"] = serialize_content_blocks(
                        slim_blocks, force=True
                    )
                if response_usage:
                    update_data["usage"] = response_usage
                if round_reasoning_details:
                    update_data["reasoning_details_per_round"] = round_reasoning_details
                    flat = [
                        item
                        for round_details in round_reasoning_details
                        for item in round_details
                    ]
                    if flat:
                        update_data["reasoning_details"] = flat
                return update_data

            async def checkpoint_stream_state(
                *,
                force: bool = False,
                include_legacy_content: bool = False,
                char_delta: int = 0,
            ):
                """Durable checkpoint for v2 streams. The RAM stream store is
                the live source of truth; DB checkpoints are intentionally
                coarse so high-TPS streams do not commit per token.

                The cheap threshold gate runs on the event loop; only the actual
                SQLite write is offloaded to a worker thread so a checkpoint on
                one stream never freezes the single loop for every other
                concurrent stream. Building the update dict happens on-loop
                (consistent snapshot of this stream's content_blocks, which only
                this coroutine mutates) and the finished dict is handed to the
                thread — `upsert_message_...` opens its own DB session, so there
                is no cross-thread session sharing."""
                nonlocal last_checkpoint_at, checkpoint_chars_since
                if STREAM_PROTOCOL_VERSION != "v2":
                    return
                if not metadata.get("chat_id") or not metadata.get("message_id"):
                    return
                if str(metadata.get("chat_id", "")).startswith("local:"):
                    return

                checkpoint_chars_since += max(0, int(char_delta or 0))
                now = time.monotonic()
                if not force:
                    if (
                        checkpoint_chars_since < CHECKPOINT_CHAR_DELTA
                        and now - last_checkpoint_at < CHECKPOINT_INTERVAL_SECONDS
                    ):
                        return

                update_data = _build_checkpoint_update(include_legacy_content)
                await asyncio.to_thread(
                    Chats.upsert_message_to_chat_by_id_and_message_id,
                    metadata["chat_id"],
                    metadata["message_id"],
                    update_data,
                    return_model=False,
                )
                last_checkpoint_at = now
                checkpoint_chars_since = 0

            try:
                for event in events:
                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": event,
                        }
                    )

                    # Save message in the database
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            **event,
                        }, return_model=False
                    )

                async def stream_body_handler(response, form_data):
                    nonlocal content
                    nonlocal content_blocks
                    nonlocal response_usage
                    nonlocal terminal_error
                    nonlocal chunk_count
                    nonlocal model_id
                    nonlocal round_reasoning_details

                    response_tool_calls = []
                    reasoning_details = []

                    delta_count = 0
                    delta_chunk_size = max(
                        CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE,
                        int(
                            metadata.get("params", {}).get("stream_delta_chunk_size")
                            or 1
                        ),
                    )
                    last_delta_data = None

                    # ── Native v2 fast-path bookkeeping ────────────────────
                    # Under STREAM_PROTOCOL_VERSION=="v2" we emit `chat:delta`
                    # `text_append` ops directly at flush time, sidestepping the
                    # translator's O(N) full-content_blocks diff. We only take
                    # the fast path when the *only* change since the last flush
                    # is an append to the tail block (text or reasoning) — i.e.
                    # no block boundary moved, no other block was mutated. Any
                    # other shape change (new block, reasoning closed, tool
                    # calls, code interpreter, replace) falls back to the
                    # translator so its diff engine handles it correctly.
                    #
                    # We mirror the translator's mirror state when we emit
                    # natively so that subsequent translator-mediated flushes
                    # compute correct diffs.
                    _v2_native = (
                        STREAM_PROTOCOL_VERSION == "v2"
                        and getattr(event_emitter, "_v2_mirror", None) is not None
                        and metadata.get("message_id")
                    )
                    _v2_mirror = (
                        getattr(event_emitter, "_v2_mirror", None)
                        if _v2_native
                        else None
                    )
                    _v2_emit_raw = (
                        getattr(event_emitter, "_emit_raw_primary", None)
                        if _v2_native
                        else None
                    )
                    _v2_message_id = metadata.get("message_id") if _v2_native else None

                    # Throttled event-loop yield. The per-token awaits on the v2
                    # hot path (`_v2_emit_raw` / `event_emitter`) only ENQUEUE into
                    # the socket delta batcher and return without a real suspension
                    # point — the batcher flushes via `loop.call_soon`, which can
                    # only run when the loop next goes idle. When the upstream
                    # provider hands over a buffered burst of SSE chunks (common
                    # after a "thinking" pause), the consumer loop would otherwise
                    # run many iterations back-to-back with NO suspension, so the
                    # batched deltas never flush until the loop finally blocks on
                    # the next network read — producing the "trickle, long stall,
                    # burst at completion" symptom. A bare `await asyncio.sleep(0)`
                    # IS a suspension point and reschedules behind the queued
                    # call_soon, letting the batch flush mid-burst and giving other
                    # chats' coroutines loop time. Throttled to ~once / 5ms so it
                    # is not a per-token tax.
                    _last_yield_at = time.monotonic()

                    async def _maybe_yield(min_interval: float = 0.005):
                        nonlocal _last_yield_at
                        now = time.monotonic()
                        if now - _last_yield_at >= min_interval:
                            _last_yield_at = now
                            await asyncio.sleep(0)

                    # ── Bounded-cadence RAM snapshot (Part C) ──────────────
                    # The per-token native path previously wrote the full
                    # content_blocks snapshot to STREAM_STATE EVERY token
                    # (set_stream_state + _strip_tool_results walk) to satisfy
                    # the reload invariant. With the O(1) accumulator that would
                    # mean materializing the whole tail every token → back to
                    # O(N^2). Instead we snapshot on a bounded cadence and let the
                    # /snapshot endpoint advertise the snapshot's own version
                    # (stored as `snapshot_version`). Invariant preserved:
                    # snapshot content and snapshot_version are written together,
                    # AFTER emitting, so the content always includes every delta
                    # through snapshot_version. Between snapshots the wire version
                    # races ahead but the endpoint advertises the older
                    # snapshot_version, so a reattach gets a consistent
                    # (content, version) pair; deltas > advertised are replayed,
                    # ≤ advertised are already in content.
                    _last_snapshot_at = time.monotonic()
                    _snapshot_chars_since = 0
                    _snapshot_established = False
                    SNAPSHOT_INTERVAL_SECONDS = 0.25
                    SNAPSHOT_CHAR_DELTA = 8192

                    def _write_stream_snapshot(snapshot_version=None):
                        nonlocal _last_snapshot_at, _snapshot_chars_since
                        nonlocal _snapshot_established
                        if not _v2_message_id:
                            return
                        _tail_materialize()
                        patch = {
                            "content_blocks": _strip_tool_results(content_blocks),
                            "status": "in_progress",
                        }
                        if snapshot_version is not None:
                            patch["snapshot_version"] = snapshot_version
                        set_stream_state(_v2_message_id, patch)
                        _last_snapshot_at = time.monotonic()
                        _snapshot_chars_since = 0
                        _snapshot_established = True

                    def _maybe_snapshot_stream_state(snapshot_version=None, char_delta=0):
                        """Write the RAM snapshot if the bounded cadence
                        (time or chars) has elapsed. `snapshot_version` is the last
                        wire version whose content is fully contained in this
                        snapshot. Cheap no-op between cadence points.

                        The FIRST call always writes (force a baseline) so the
                        /snapshot endpoint has a (content, snapshot_version) pair
                        and never falls back to advertising the live wire counter,
                        which races ahead of the cadence-written RAM content."""
                        nonlocal _snapshot_chars_since
                        if not _v2_message_id:
                            return
                        _snapshot_chars_since += max(0, int(char_delta or 0))
                        now = time.monotonic()
                        if (
                            not _snapshot_established
                            or _snapshot_chars_since >= SNAPSHOT_CHAR_DELTA
                            or now - _last_snapshot_at >= SNAPSHOT_INTERVAL_SECONDS
                        ):
                            _write_stream_snapshot(snapshot_version=snapshot_version)

                    def _v2_try_native_append():
                        """Return (block_idx, appended_text, None) if the tail
                        block is a pure append since the last mirror sync AND no
                        earlier block changed; otherwise None to force a
                        translator-mediated full diff. Uses the accumulator's emit
                        cursor (O(appended)) instead of a full-string startswith
                        (which was O(N) per token → O(N^2) per stream)."""
                        if not _v2_native or not content_blocks:
                            return None
                        mirror_blocks = _v2_mirror.get("blocks") or []
                        tail_idx = len(content_blocks) - 1
                        tail = content_blocks[tail_idx]
                        if tail.get("type") not in ("text", "reasoning"):
                            return None
                        # ended_at is set when reasoning closes — that's a
                        # structural change, defer to the translator.
                        if tail.get("type") == "reasoning" and tail.get("ended_at"):
                            return None
                        # New tail block (mirror hasn't seen it yet) — defer.
                        if tail_idx != len(mirror_blocks) - 1:
                            return None
                        old_tail = mirror_blocks[tail_idx]
                        if old_tail.get("type") != tail.get("type"):
                            return None
                        # The accumulator must be bound to THIS tail; otherwise we
                        # can't trust its emit cursor — defer to the translator,
                        # which diffs the materialized strings.
                        if _tail_state["acc"] is None or _tail_state["block"] is not tail:
                            return None
                        appended = _tail_state["acc"].take_appended()
                        if not appended:
                            return None
                        return tail_idx, appended, None


                    async def flush_pending_delta_data(threshold: int = 0):
                        nonlocal delta_count
                        nonlocal last_delta_data

                        if delta_count >= threshold and last_delta_data:
                            if event_emitter is None:
                                log.error(
                                    f"❌ FLUSH ERROR: event_emitter is None! Cannot emit events!"
                                )
                            else:
                                native = (
                                    _v2_try_native_append()
                                    if _v2_native
                                    and "content_blocks" in (last_delta_data or {})
                                    else None
                                )
                                if native is not None:
                                    block_idx, appended, _ = native
                                    last_native_version = None
                                    for text_chunk in _split_text_by_utf8_bytes(
                                        appended
                                    ):
                                        version = stream_version_incr(_v2_message_id)
                                        last_native_version = version
                                        payload = {
                                            "type": "chat:delta",
                                            "data": {
                                                "message_id": _v2_message_id,
                                                "version": version,
                                                "op": "text_append",
                                                "payload": {
                                                    "block_idx": block_idx,
                                                    "text": text_chunk,
                                                },
                                            },
                                        }
                                        await _v2_emit_raw(payload)
                                    # Advance the translator's mirror for this
                                    # block by LENGTH only — never by aliasing or
                                    # concatenating the growing string (that would
                                    # reintroduce the O(N^2) concat). The mirror's
                                    # stale `content` string is reconciled inside
                                    # _emit_delta_for_blocks itself, which honors
                                    # this `_emitted_len` cursor whenever the
                                    # translator path next diffs the block (at any
                                    # call site — round boundaries, fallbacks, etc).
                                    mirror_block = _v2_mirror["blocks"][block_idx]
                                    # Initialize the emitted-length cursor from the
                                    # mirror's current content length when absent
                                    # (e.g. the previous flush on this block went
                                    # through the translator, which syncs `content`
                                    # but not `_emitted_len`). Then advance by the
                                    # bytes we just emitted natively.
                                    if mirror_block.get("_emitted_len") is None:
                                        mirror_block["_emitted_len"] = len(
                                            mirror_block.get("content", "") or ""
                                        )
                                    mirror_block["_emitted_len"] += len(appended)
                                    # Snapshot correctness invariant: a reload that
                                    # fetches /snapshot must get content that
                                    # includes every delta up to the advertised
                                    # version. We write the RAM snapshot AFTER
                                    # emitting (so the content materialized here
                                    # includes `appended`) and stamp it with the
                                    # last version we just emitted. Bounded cadence
                                    # keeps this off the per-token hot path; the
                                    # endpoint advertises this snapshot_version
                                    # (always ≤ the content it carries), so deltas
                                    # the client already holds (version ≤ snapshot)
                                    # are correctly dropped and any newer ones are
                                    # replayed. See Part C of the streaming fix.
                                    _maybe_snapshot_stream_state(
                                        snapshot_version=last_native_version,
                                        char_delta=len(appended),
                                    )
                                else:
                                    # Translator fallback: it diffs the full
                                    # materialized content_blocks, so fold the tail
                                    # buffer back first. The translator itself
                                    # honors the mirror's `_emitted_len` cursor (set
                                    # by prior native flushes) when diffing, so the
                                    # mirror reconciles correctly without a separate
                                    # pass here.
                                    _tail_materialize()
                                    await event_emitter(
                                        {
                                            "type": "chat:completion",
                                            "data": last_delta_data,
                                        }
                                    )
                                    # The translator drained the tail; keep the
                                    # accumulator's emit cursor consistent so a
                                    # subsequent native flush won't re-ship text.
                                    if _tail_state["acc"] is not None:
                                        _tail_state["acc"].take_appended()
                            delta_count = 0
                            last_delta_data = None

                    async for line in response.body_iterator:
                        line = (
                            line.decode("utf-8", "replace")
                            if isinstance(line, bytes)
                            else line
                        )
                        data = line

                        # Skip empty lines
                        if not data.strip():
                            continue

                        # "data:" is the prefix for each event
                        if not data.startswith("data:"):
                            continue

                        # Remove the prefix
                        data = data[len("data:") :].strip()

                        try:
                            data = json.loads(data)
                            chunk_count += 1

                            # Debug logging: print RAW API response
                            if hasattr(request.app.state, "config") and getattr(
                                request.app.state.config,
                                "ENABLE_API_DEBUG_LOGGING",
                                False,
                            ):
                                print(f"[API RAW] {json.dumps(data)}", flush=True)

                            data, _ = await process_filter_functions(
                                request=request,
                                filter_functions=filter_functions,
                                filter_type="stream",
                                form_data=data,
                                extra_params={"__body__": form_data, **extra_params},
                            )

                            if data:
                                if "event" in data:
                                    await event_emitter(data.get("event", {}))

                                if "selected_model_id" in data:
                                    model_id = data["selected_model_id"]
                                    Chats.upsert_message_to_chat_by_id_and_message_id(
                                        metadata["chat_id"],
                                        metadata["message_id"],
                                        {
                                            "selectedModelId": model_id,
                                        }, return_model=False
                                    )
                                    await event_emitter(
                                        {
                                            "type": "chat:completion",
                                            "data": data,
                                        }
                                    )
                                else:
                                    choices = data.get("choices", [])

                                    # 17421
                                    usage = data.get("usage", {}) or {}
                                    usage.update(data.get("timings", {}))  # llama.cpp
                                    if usage:
                                        response_usage = (
                                            usage  # Store for final completion event
                                        )
                                        # Pass chat_id and user_id for analytics tracking
                                        await process_token_usage(
                                            model_id,
                                            usage,
                                            chat_id=_get_token_usage_chat_id(metadata),
                                            user_id=user.id if user else None,
                                            source_chat_id=metadata.get("chat_id"),
                                            message_id=metadata.get("message_id"),
                                            parent_message_id=metadata.get(
                                                "parent_message_id"
                                            ),
                                            source_type=(
                                                "subagent"
                                                if metadata.get("subagent_inner")
                                                else "chat"
                                            ),
                                        )
                                        await event_emitter(
                                            {
                                                "type": "chat:completion",
                                                "data": {
                                                    "usage": usage,
                                                },
                                            }
                                        )

                                    # Detect mid-stream errors: OpenRouter
                                    # sends errors with an `error` field at
                                    # the top level AND choices[0].finish_reason
                                    # set to "error". Check BEFORE inspecting
                                    # choices so the error isn't silently ignored.
                                    chunk_error = data.get("error")
                                    chunk_finish = (
                                        choices[0].get("finish_reason")
                                        if choices
                                        else None
                                    )
                                    if chunk_error or chunk_finish == "error":
                                        error_payload = chunk_error or {
                                            "message": "Provider returned an error during streaming."
                                        }
                                        terminal_error = error_payload
                                        await event_emitter(
                                            {
                                                "type": "chat:completion",
                                                "data": {
                                                    "error": error_payload,
                                                },
                                            }
                                        )
                                        break

                                    if not choices:
                                        continue

                                    delta = choices[0].get("delta", {})
                                    delta_tool_calls = delta.get("tool_calls", None)
                                    delta_reasoning_details = delta.get(
                                        "reasoning_details", None
                                    )

                                    if delta_reasoning_details:
                                        # Merge streaming reasoning_details deltas. Match by
                                        # (id, type) with a (type, index) fallback for id-less
                                        # chunks; concat text/data/summary across fragments.
                                        # See utils/REASONING_DETAILS.md §2 (the wire protocol)
                                        # and §6 Bug A (why this isn't matched on id alone).
                                        for detail in delta_reasoning_details:
                                            detail_id = detail.get("id")
                                            detail_type = detail.get("type")
                                            detail_idx = detail.get("index", 0)

                                            existing = None
                                            if detail_id is not None:
                                                existing = next(
                                                    (
                                                        d
                                                        for d in reasoning_details
                                                        if d.get("id") == detail_id
                                                        and d.get("type") == detail_type
                                                    ),
                                                    None,
                                                )
                                                # Adopt an id-less entry only when the type also
                                                # matches (covers providers that emit `id` only
                                                # on a later chunk of the same logical item).
                                                if existing is None:
                                                    existing = next(
                                                        (
                                                            d
                                                            for d in reasoning_details
                                                            if d.get("id") is None
                                                            and d.get("type")
                                                            == detail_type
                                                            and d.get("index")
                                                            == detail_idx
                                                        ),
                                                        None,
                                                    )
                                            else:
                                                existing = next(
                                                    (
                                                        d
                                                        for d in reasoning_details
                                                        if d.get("type") == detail_type
                                                        and d.get("index") == detail_idx
                                                    ),
                                                    None,
                                                )

                                            if existing is not None:
                                                if detail.get("text"):
                                                    existing["text"] = (
                                                        _merge_streamed_string(
                                                            existing.get("text") or "",
                                                            detail["text"],
                                                        )
                                                    )
                                                if detail.get("data"):
                                                    existing["data"] = (
                                                        _merge_streamed_string(
                                                            existing.get("data") or "",
                                                            detail["data"],
                                                        )
                                                    )
                                                if detail.get("summary"):
                                                    existing["summary"] = (
                                                        _merge_streamed_string(
                                                            existing.get("summary")
                                                            or "",
                                                            detail["summary"],
                                                        )
                                                    )
                                                # `type` is part of the match key and never
                                                # needs overwriting; `summary` is concat'd above.
                                                for k in (
                                                    "id",
                                                    "signature",
                                                    "format",
                                                    "index",
                                                ):
                                                    if detail.get(k) is not None:
                                                        existing[k] = detail[k]
                                            else:
                                                reasoning_details.append({**detail})

                                    if delta_tool_calls:
                                        for delta_tool_call in delta_tool_calls:
                                            tool_call_index = delta_tool_call.get(
                                                "index"
                                            )

                                            if tool_call_index is not None:
                                                # Check if the tool call already exists
                                                current_response_tool_call = None
                                                for (
                                                    response_tool_call
                                                ) in response_tool_calls:
                                                    if (
                                                        response_tool_call.get("index")
                                                        == tool_call_index
                                                    ):
                                                        current_response_tool_call = (
                                                            response_tool_call
                                                        )
                                                        break

                                                if current_response_tool_call is None:
                                                    # Add the new tool call
                                                    delta_tool_call.setdefault(
                                                        "function", {}
                                                    )
                                                    delta_tool_call[
                                                        "function"
                                                    ].setdefault("name", "")
                                                    delta_tool_call[
                                                        "function"
                                                    ].setdefault("arguments", "")
                                                    delta_tool_call["function"][
                                                        "name"
                                                    ] = _dedupe_repeated_tool_name(
                                                        delta_tool_call["function"].get(
                                                            "name", ""
                                                        )
                                                    )
                                                    response_tool_calls.append(
                                                        delta_tool_call
                                                    )
                                                else:
                                                    # Update the existing tool call
                                                    delta_name = delta_tool_call.get(
                                                        "function", {}
                                                    ).get("name")
                                                    delta_arguments = (
                                                        delta_tool_call.get(
                                                            "function", {}
                                                        ).get("arguments")
                                                    )

                                                    if delta_name:
                                                        fn = current_response_tool_call[
                                                            "function"
                                                        ]
                                                        fn["name"] = (
                                                            _dedupe_repeated_tool_name(
                                                                _merge_streamed_string(
                                                                    fn.get("name", ""),
                                                                    delta_name,
                                                                )
                                                            )
                                                        )

                                                    if delta_arguments:
                                                        fn = current_response_tool_call[
                                                            "function"
                                                        ]
                                                        fn["arguments"] = (
                                                            _merge_streamed_string(
                                                                fn.get("arguments", ""),
                                                                delta_arguments,
                                                            )
                                                        )

                                    value = delta.get("content")

                                    # Get reasoning content from various possible fields
                                    # Note: 'reasoning' and 'reasoning_details' often contain the SAME content
                                    # (reasoning_details is just a more structured format), so we should NOT
                                    # concatenate both - use reasoning_details ONLY if direct fields are empty
                                    reasoning_content = (
                                        delta.get("reasoning_content")
                                        or delta.get("reasoning")
                                        or delta.get("thinking")
                                    )

                                    # Only use reasoning_details if we didn't get content from the direct fields
                                    if (
                                        not reasoning_content
                                        and delta_reasoning_details
                                    ):
                                        for detail in delta_reasoning_details:
                                            if detail.get("type") == "reasoning.text":
                                                reasoning_content = (
                                                    reasoning_content or ""
                                                ) + detail.get("text", "")

                                    if reasoning_content:
                                        # Discard late-arriving reasoning tokens once
                                        # the final text response has already started.
                                        # SSE delivery can be out-of-order: a reasoning
                                        # chunk may arrive after text chunks have begun.
                                        # We detect this by checking the last block type:
                                        #   "text" (non-empty) → response in progress → discard
                                        #   "text" (empty)     → placeholder after tool call → allow reasoning
                                        #   "tool_calls"       → between tool-call rounds → allow
                                        #   "reasoning"        → still in thinking phase → append
                                        #   (empty)            → fresh start → create block
                                        # Fold the tail buffer first so the
                                        # "non-empty text in progress" check below
                                        # sees real accumulated content (the hot
                                        # path leaves the tail in an accumulator).
                                        # Only runs when a reasoning delta arrives,
                                        # so it is not on the pure-text hot path.
                                        _tail_materialize()
                                        _last_block_type = (
                                            content_blocks[-1]["type"]
                                            if content_blocks
                                            else None
                                        )
                                        _last_block_content = (
                                            content_blocks[-1].get("content", "")
                                            if content_blocks
                                            else ""
                                        )
                                        if _last_block_type != "text" or (
                                            _last_block_type == "text"
                                            and not _last_block_content
                                        ):
                                            if _last_block_type != "reasoning":
                                                # Remove empty text placeholder if it's the last block
                                                if (
                                                    _last_block_type == "text"
                                                    and not _last_block_content
                                                ):
                                                    content_blocks.pop()

                                                reasoning_block = {
                                                    "type": "reasoning",
                                                    "start_tag": "<think>",
                                                    "end_tag": "</think>",
                                                    "attributes": {
                                                        "type": "reasoning_content"
                                                    },
                                                    "content": "",
                                                    "started_at": time.time(),
                                                }
                                                content_blocks.append(reasoning_block)
                                            else:
                                                reasoning_block = content_blocks[-1]

                                            # O(1) buffered reasoning append (with
                                            # cumulative-resend defense). Returns
                                            # the number of new chars for checkpoint
                                            # accounting. The accumulator keeps
                                            # reasoning_block["content"] lazily
                                            # materialized; _tail_materialize() at
                                            # boundaries/readers folds it back.
                                            _reasoning_added = _tail_append_reasoning(
                                                reasoning_block, reasoning_content
                                            )
                                            # v1 reads the tail synchronously below;
                                            # v2 keeps it buffered (native emit).
                                            if STREAM_PROTOCOL_VERSION != "v2":
                                                _tail_materialize()
                                            await checkpoint_stream_state(
                                                char_delta=_reasoning_added
                                            )

                                            data = {
                                                "content": serialize_content_blocks(
                                                    content_blocks
                                                ),
                                                "content_blocks": content_blocks,
                                            }

                                    # Skip processing 'value' if reasoning_content was already handled
                                    # Some APIs duplicate reasoning tokens to 'content' for backwards compatibility
                                    if value and not reasoning_content:
                                        if (
                                            content_blocks
                                            and content_blocks[-1]["type"]
                                            == "reasoning"
                                        ):
                                            reasoning_block = content_blocks[-1]
                                            # Reasoning is closing — fold its buffer
                                            # back so the closed block carries its
                                            # full text for serialize/checkpoint.
                                            _tail_materialize()
                                            reasoning_block["ended_at"] = time.time()
                                            reasoning_block["duration"] = int(
                                                reasoning_block["ended_at"]
                                                - reasoning_block["started_at"]
                                            )

                                            content_blocks.append(
                                                {
                                                    "type": "text",
                                                    "content": "",
                                                }
                                            )
                                            await checkpoint_stream_state(force=True)

                                        append_plain_content(value)
                                        if not content_blocks:
                                            content_blocks.append(
                                                {
                                                    "type": "text",
                                                    "content": "",
                                                }
                                            )

                                        # O(1) buffered text append (the hot path
                                        # for normal answer streaming). Replaces the
                                        # O(N)-per-token dict-subscript concat.
                                        _tail_append_text(content_blocks[-1], value)
                                        # Under v1 / realtime-save, the tail is read
                                        # synchronously below (DB write + serialize),
                                        # so fold it now. Under v2 it stays buffered
                                        # (native flush emits from the accumulator;
                                        # materializing per token would restore the
                                        # O(N^2)).
                                        if STREAM_PROTOCOL_VERSION != "v2":
                                            _tail_materialize()
                                        await checkpoint_stream_state(char_delta=len(value))

                                        if (
                                            ENABLE_REALTIME_CHAT_SAVE
                                            and STREAM_PROTOCOL_VERSION != "v2"
                                        ):
                                            # Legacy/non-v2 realtime save path.
                                            # v2 uses the in-memory stream
                                            # snapshot for reload/resume and
                                            # periodic/final checkpoints instead
                                            # of committing on every token.
                                            update_data = {
                                                "content_blocks": content_blocks,
                                            }
                                            if STREAM_PROTOCOL_VERSION != "v2":
                                                update_data["content"] = (
                                                    serialize_content_blocks(
                                                        content_blocks
                                                    )
                                                )

                                            # Per-round reasoning lets multi-turn replays attach
                                            # the right round's reasoning to each tool_calls
                                            # message. Flat array kept for backward compat with
                                            # older saved messages.
                                            if round_reasoning_details:
                                                update_data[
                                                    "reasoning_details_per_round"
                                                ] = round_reasoning_details
                                                flat = [
                                                    item
                                                    for round_details in round_reasoning_details
                                                    for item in round_details
                                                ]
                                                if flat:
                                                    update_data["reasoning_details"] = (
                                                        flat
                                                    )

                                            Chats.upsert_message_to_chat_by_id_and_message_id(
                                                metadata["chat_id"],
                                                metadata["message_id"],
                                                update_data, return_model=False
                                            )

                                        # Regardless of realtime DB writes, the
                                        # stream event must carry content_blocks
                                        # so the v2 wrapper can translate this
                                        # chunk into chat:delta ops. The v2
                                        # serializer intentionally returns an
                                        # empty content string on the hot path;
                                        # frontends render from content_blocks.
                                        # (Tail already folded above for v1; under
                                        # v2 it stays buffered by design.)
                                        data = {
                                            "content": serialize_content_blocks(
                                                content_blocks
                                            ),
                                            "content_blocks": content_blocks,
                                        }

                                if delta:
                                    delta_count += 1
                                    last_delta_data = data
                                    if delta_count >= delta_chunk_size:
                                        await flush_pending_delta_data(delta_chunk_size)
                                        await _maybe_yield()
                                else:
                                    await event_emitter(
                                        {
                                            "type": "chat:completion",
                                            "data": data,
                                        }
                                    )
                        except Exception as e:
                            done = "data: [DONE]" in line
                            if done:
                                pass
                            else:
                                log.warning(f"Error parsing SSE chunk: {e}")
                                continue
                    await flush_pending_delta_data()

                    # Fold the tail accumulator into its block before the
                    # end-of-stream cleanup reads/strips content_blocks.
                    _tail_materialize()

                    if content_blocks:
                        # Clean up the last text block
                        if content_blocks[-1]["type"] == "text":
                            content_blocks[-1]["content"] = content_blocks[-1][
                                "content"
                            ].strip()

                            if not content_blocks[-1]["content"]:
                                content_blocks.pop()

                                if not content_blocks:
                                    content_blocks.append(
                                        {
                                            "type": "text",
                                            "content": "",
                                        }
                                    )

                        if content_blocks[-1]["type"] == "reasoning":
                            reasoning_block = content_blocks[-1]
                            if reasoning_block.get("ended_at") is None:
                                reasoning_block["ended_at"] = time.time()
                                reasoning_block["duration"] = int(
                                    reasoning_block["ended_at"]
                                    - reasoning_block["started_at"]
                                )
                                await checkpoint_stream_state(force=True)

                    if response_tool_calls:
                        tool_calls.append(
                            {
                                "tool_calls": response_tool_calls,
                                "reasoning_details": reasoning_details,
                            }
                        )

                    # Track this round's reasoning regardless of whether it
                    # produced tool_calls — append even when empty so per_round
                    # length stays equal to emission count. See
                    # utils/REASONING_DETAILS.md §6 Bug B.
                    round_reasoning_details.append(reasoning_details or [])

                    if response.background:
                        await response.background()

                async def _consume_nonstreaming_round(res):
                    """Fold a non-streaming chat-completion dict into the running
                    content_blocks / tool_calls / round bookkeeping. Mirrors what
                    stream_body_handler does for the streaming shape. Extracted so
                    both the first call and the empty-round retry can run it."""
                    nonlocal response_usage
                    nonlocal terminal_error
                    choice = res["choices"][0]
                    message = choice.get("message", {})

                    reasoning_content = _visible_nonstreaming_reasoning(message)
                    if reasoning_content:
                        content_blocks.append(
                            {
                                "type": "reasoning",
                                "start_tag": "<think>",
                                "end_tag": "</think>",
                                "attributes": {"type": "reasoning_content"},
                                "content": reasoning_content,
                                "started_at": time.time(),
                                "ended_at": time.time(),
                                "duration": 0,
                            }
                        )

                    msg_content = message.get("content")
                    if msg_content:
                        if not content_blocks or content_blocks[-1]["type"] != "text":
                            content_blocks.append({"type": "text", "content": ""})
                        # Fold any buffered streaming tail first, then a plain
                        # in-place append (the accumulator isn't driving this branch).
                        _tail_materialize()
                        content_blocks[-1]["content"] += msg_content
                        append_plain_content(msg_content)

                    res_tool_calls = message.get("tool_calls")
                    length_error = _nonstreaming_round_length_error(res)
                    if length_error:
                        terminal_error = {"content": length_error}

                    if res_tool_calls:
                        tool_calls.append(
                            {
                                "tool_calls": res_tool_calls,
                                "reasoning_details": message.get("reasoning_details"),
                            }
                        )

                    # Per-round bookkeeping (mirrors stream_body_handler). Append
                    # even when empty — see utils/REASONING_DETAILS.md §6 Bug B.
                    round_reasoning_details.append(
                        message.get("reasoning_details") or []
                    )

                    usage = res.get("usage", {})
                    if usage:
                        response_usage = usage
                        await process_token_usage(
                            model_id,
                            usage,
                            chat_id=_get_token_usage_chat_id(metadata),
                            user_id=user.id if user else None,
                            source_chat_id=metadata.get("chat_id"),
                            message_id=metadata.get("message_id"),
                            parent_message_id=metadata.get("parent_message_id"),
                            source_type=(
                                "subagent"
                                if metadata.get("subagent_inner")
                                else "chat"
                            ),
                        )
                        await event_emitter(
                            {
                                "type": "chat:completion",
                                "data": {"usage": usage},
                            }
                        )

                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": {
                                "content": serialize_content_blocks(content_blocks),
                                "content_blocks": content_blocks,
                            },
                        }
                    )

                async def _run_round_with_retry(resp, fd):
                    """Run one model round (streaming or non-streaming) and, if it
                    produced NOTHING usable (no tool calls AND no assistant text)
                    without erroring, re-issue the SAME request up to
                    AGENTIC_EMPTY_ROUND_MAX_RETRIES times. Models sometimes end a
                    turn on a bare reasoning block or an empty completion; without
                    this the agentic loop just stops with no answer.

                    Returns the LAST response object. The caller's existing dispatch
                    (the `if isinstance(res, StreamingResponse) ... elif dict ...
                    else error` ladder) then runs on it — productive rounds are
                    already folded in here, so re-folding is suppressed via the
                    `_round_already_consumed` flag the caller checks.

                    NOT retried: provider errors (set terminal_error or return an
                    error-shaped response) and user cancels (CancelledError
                    propagates out). Subagents inherit this via the shared loop."""
                    nonlocal _round_already_consumed
                    attempt = 0
                    current = resp
                    while True:
                        tc_before = len(tool_calls)
                        text_before = _total_text_block_len(content_blocks)
                        blocks_before = len(content_blocks)
                        rrd_before = len(round_reasoning_details)
                        terminal_before = terminal_error

                        _round_already_consumed = False
                        if isinstance(current, StreamingResponse):
                            await stream_body_handler(current, fd)
                            _round_already_consumed = True
                        elif (
                            isinstance(current, dict)
                            and "choices" in current
                            and len(current["choices"]) > 0
                        ):
                            await _consume_nonstreaming_round(current)
                            _round_already_consumed = True
                        else:
                            # Error / unknown shape — never retried; hand back so the
                            # caller's error-reading branch runs.
                            return current

                        produced = (
                            len(tool_calls) > tc_before
                            or _total_text_block_len(content_blocks) > text_before
                        )
                        errored = terminal_error is not None and terminal_error is not terminal_before
                        if (
                            produced
                            or errored
                            or attempt >= AGENTIC_EMPTY_ROUND_MAX_RETRIES
                        ):
                            return current

                        # Unproductive round with retries left: roll back this
                        # round's empty residue so the retry starts clean.
                        #  - materialize + unbind the tail accumulator so it isn't
                        #    pinned to a block we're about to drop,
                        #  - truncate any empty/partial blocks this round appended
                        #    (the v2 emitter emits a `replace` to resync the client
                        #    mirror on the next flush — it tolerates shrink),
                        #  - trim per-round reasoning bookkeeping so
                        #    len(round_reasoning_details) == emission count holds
                        #    (REASONING_DETAILS.md §6 Bug B).
                        _tail_materialize()
                        _tail_state["acc"] = None
                        _tail_state["block"] = None
                        if blocks_before < len(content_blocks):
                            del content_blocks[blocks_before:]
                        if rrd_before < len(round_reasoning_details):
                            del round_reasoning_details[rrd_before:]
                        attempt += 1
                        log.warning(
                            "empty model round (no tool calls, no text) — retry "
                            f"{attempt}/{AGENTIC_EMPTY_ROUND_MAX_RETRIES} "
                            f"chat={metadata.get('chat_id')} "
                            f"subagent_inner={metadata.get('subagent_inner', False)}"
                        )
                        current = await generate_chat_completion(request, fd, user)

                first_response = await _run_round_with_retry(response, form_data)
                if first_response is not None and not _round_already_consumed:
                    # The retry helper returned an error/unknown-shape response it
                    # didn't consume; surface it the same way the loop's error
                    # branch does.
                    try:
                        if hasattr(first_response, "body"):
                            error_content = (
                                first_response.body.decode("utf-8")
                                if isinstance(first_response.body, bytes)
                                else str(first_response.body)
                            )
                            log.error(f"Initial response error: {error_content}")
                            try:
                                error_json = json.loads(error_content)
                                error_msg = error_json.get("error", error_content)
                                if isinstance(error_msg, dict):
                                    error_msg = error_msg.get(
                                        "message",
                                        error_msg.get("detail", str(error_msg)),
                                    )
                            except Exception:
                                error_msg = error_content
                            terminal_error = {"content": str(error_msg)}
                            await event_emitter(
                                {
                                    "type": "chat:message:error",
                                    "data": {"error": terminal_error},
                                }
                            )
                    except Exception as read_err:
                        log.error(f"Could not read initial error response: {read_err}")

                # Hard cap on agentic tool-call rounds. DISABLED by default
                # (AGENTIC_MAX_TOOL_ROUNDS = 0): the loop runs as many tool
                # rounds as the model wants. If an admin sets a positive cap as
                # an ops backstop, reaching it makes the NEXT model call
                # tool-free (drop `tools` from the payload + feed a notice) so
                # the model must produce a final answer and the loop terminates.
                #
                # Subagents are ALWAYS exempt, even when a parent cap is set: a
                # subagent's whole job is to research as deeply as it needs, so
                # we never cap how many tool rounds its inner pipeline may run.
                effective_max_tool_rounds = (
                    0 if metadata.get("subagent_inner") else AGENTIC_MAX_TOOL_ROUNDS
                )
                tool_round_count = 0
                tool_rounds_capped = False

                def _reconcile_subagent_results():
                    """Make the canonical content_blocks results authoritative by
                    backfilling any missing/empty subagent tool result from the
                    durable subagent_runs mirror. Cheap, called once per round
                    boundary / at finalize — NOT the per-token hot path. No-op for
                    runs without subagents (the DB read returns no subagent_runs)."""
                    if not metadata.get("chat_id") or not metadata.get("message_id"):
                        return
                    if str(metadata.get("chat_id", "")).startswith("local:"):
                        return
                    try:
                        from open_webui.utils.subagent import (
                            reconcile_block_results_from_runs,
                        )

                        msg = (
                            Chats.get_message_by_id_and_message_id(
                                metadata["chat_id"], metadata["message_id"]
                            )
                            or {}
                        )
                        runs = msg.get("subagent_runs")
                        if isinstance(runs, dict) and runs:
                            reconcile_block_results_from_runs(content_blocks, runs)
                    except Exception:
                        log.exception("subagent result reconciliation failed")

                while len(tool_calls) > 0:

                    tool_call_item = tool_calls.pop(0)
                    tool_round_count += 1
                    if (
                        isinstance(tool_call_item, dict)
                        and "tool_calls" in tool_call_item
                    ):
                        response_tool_calls = tool_call_item["tool_calls"]
                        reasoning_details = tool_call_item.get("reasoning_details")
                    else:
                        response_tool_calls = tool_call_item
                        reasoning_details = None

                    content_blocks.append(
                        {
                            "type": "tool_calls",
                            "content": response_tool_calls,
                            "reasoning_details": reasoning_details,
                            "started_at": time.time(),
                        }
                    )

                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": {
                                "content": serialize_content_blocks(content_blocks),
                                "content_blocks": content_blocks,
                            },
                        }
                    )
                    await checkpoint_stream_state(force=True)

                    tools = metadata.get("tools", {})

                    async def _execute_tool_call(tool_call):
                        tool_call_id = tool_call.get("id", "")
                        tool_function_name = tool_call.get("function", {}).get(
                            "name", ""
                        )
                        # Pin the tool_call_id for this branch of the gather so
                        # the tool callable can look it up via
                        # `current_tool_call_id_var.get()` (used by the subagent
                        # tool to wire its subagent_id back to the right
                        # parent tool call).
                        current_tool_call_id_var.set(tool_call_id)
                        tool_args = tool_call.get("function", {}).get("arguments", "{}")

                        tool_function_params = parse_tool_call_arguments(tool_args)
                        if not tool_function_params and str(
                            tool_args or "{}"
                        ).strip() not in {
                            "",
                            "{}",
                        }:
                            log.error(f"Error parsing tool call arguments: {tool_args}")

                        # Mutate the original tool call response params as they are passed back to the passed
                        # back to the LLM via the content blocks. If they are in a json block and are invalid json,
                        # this can cause downstream LLM integrations to fail (e.g. bedrock gateway) where response
                        # params are not valid json.
                        # Main case so far is no args = "" = invalid json.
                        log.debug(
                            f"Parsed args from {tool_args} to {tool_function_params}"
                        )
                        tool_call.setdefault("function", {})["arguments"] = json.dumps(
                            tool_function_params
                        )

                        tool_result = None
                        tool = None
                        tool_type = None
                        direct_tool = False
                        # Set when the tool callable raises; surfaced on the result
                        # entry so the collapsed tool-call row shows an error.
                        tool_exec_error = None

                        if tool_function_name in tools:
                            tool = tools[tool_function_name]
                            spec = tool.get("spec", {})

                            tool_type = tool.get("type", "")
                            direct_tool = tool.get("direct", False)

                            try:
                                allowed_params = (
                                    spec.get("parameters", {})
                                    .get("properties", {})
                                    .keys()
                                )

                                tool_function_params = {
                                    k: v
                                    for k, v in tool_function_params.items()
                                    if k in allowed_params
                                }

                                if direct_tool:
                                    tool_result = await event_caller(
                                        {
                                            "type": "execute:tool",
                                            "data": {
                                                "id": str(uuid4()),
                                                "name": tool_function_name,
                                                "params": tool_function_params,
                                                "server": tool.get("server", {}),
                                                "session_id": metadata.get(
                                                    "session_id", None
                                                ),
                                            },
                                        }
                                    )

                                else:
                                    tool_function = tool["callable"]
                                    # Live browser progress: while a browser_*
                                    # MCP call on the container server runs (it
                                    # blocks here, possibly for many seconds),
                                    # shadow it with a poller that reads the
                                    # daemon's live.jpg/state.json host-side and
                                    # pushes frames + status to the UI. Cancelled
                                    # the instant the call returns. Best-effort:
                                    # never let it affect the tool result.
                                    _browser_poller = None
                                    try:
                                        _tmeta = tool.get("metadata", {}) or {}
                                        _container_id = _normalize_container_server_id(
                                            str(
                                                getattr(
                                                    request.app.state.config,
                                                    "CONTAINER_MCP_SERVER_ID",
                                                    "",
                                                )
                                                or ""
                                            )
                                        )
                                        _is_browser_tool = (
                                            tool.get("type") == "mcp"
                                            and _container_id
                                            and _normalize_container_server_id(
                                                str(_tmeta.get("server_id", ""))
                                            )
                                            == _container_id
                                            and str(
                                                _tmeta.get("original_name", "")
                                            ).startswith("browser_")
                                        )
                                        if _is_browser_tool:
                                            _data_root = str(
                                                getattr(
                                                    request.app.state.config,
                                                    "CONTAINER_DATA_ROOT",
                                                    "",
                                                )
                                                or ""
                                            )
                                            # The daemon writes live.jpg/state.json
                                            # under the workspace keyed by the SAME
                                            # chat_id the container header carries.
                                            # For a subagent that is the PARENT chat
                                            # (container_workspace_chat_id), not the
                                            # subagent's own chat_id — otherwise the
                                            # poller reads an empty dir and the live
                                            # view never updates during subagent
                                            # browsing. For a normal chat the key is
                                            # absent and this falls back to chat_id,
                                            # so main-loop behavior is unchanged.
                                            _poller_chat_id = metadata.get(
                                                "container_workspace_chat_id"
                                            ) or metadata.get("chat_id")
                                            if _data_root and _poller_chat_id:
                                                _browser_poller = asyncio.create_task(
                                                    browser_progress_poller(
                                                        data_root=_data_root,
                                                        chat_id=_poller_chat_id,
                                                        message_id=metadata.get(
                                                            "message_id"
                                                        ),
                                                        session_id=metadata.get(
                                                            "session_id"
                                                        ),
                                                        event_emitter=event_emitter,
                                                        # The per-AGENT browser tab
                                                        # this call drives. Parent =>
                                                        # "main" (browser_session
                                                        # absent); subagent => its
                                                        # subagent_id (set in
                                                        # _subagent_container_shared_context).
                                                        # The poller reads that one
                                                        # session's live files and
                                                        # tags frames with it so the
                                                        # UI groups tabs correctly.
                                                        session=metadata.get(
                                                            "browser_session"
                                                        )
                                                        or "main",
                                                    )
                                                )
                                    except Exception:
                                        _browser_poller = None
                                    try:
                                        tool_result = await tool_function(
                                            **tool_function_params
                                        )
                                    finally:
                                        if _browser_poller is not None:
                                            _browser_poller.cancel()
                                            # Await the poller's terminal emit so
                                            # the final frame + terminal (done)
                                            # status land BEFORE the next tool
                                            # call's poller starts — otherwise a
                                            # late "Loaded" could race ahead of
                                            # the next "Navigating", and pollers
                                            # could pile up across calls. The
                                            # poller swallows its own cancel and
                                            # returns, so this just drains it;
                                            # bounded so a wedged emit can't stall
                                            # the turn.
                                            try:
                                                await asyncio.wait_for(
                                                    _browser_poller, timeout=2.0
                                                )
                                            except (asyncio.TimeoutError, Exception):
                                                # Swallow the poller's own
                                                # timeout/errors. CancelledError
                                                # (BaseException) is intentionally
                                                # NOT caught so an outer cancel of
                                                # the tool call still propagates.
                                                pass

                            except Exception as e:
                                tool_result = str(e)
                                # A raised tool exception is an error the UI
                                # should surface on the collapsed row, not just
                                # bury inside the result content. process_tool_result
                                # may still enrich this (e.g. _owui_meta), so seed
                                # a generic error and let it override the reason.
                                tool_exec_error = str(e)
                        else:
                            # Model emitted a tool name we don't have
                            # registered for this turn. Most common cause:
                            # a saved chat is being replayed and the model
                            # parrots back an old tool name (pre-rename, or
                            # from an MCP server that's been removed). The
                            # request continues with an empty result -- the
                            # model usually adapts -- but log it so this is
                            # debuggable instead of silently degrading.
                            log.warning(
                                "Tool call for unknown function %r; " "known tools: %s",
                                tool_function_name,
                                sorted(tools.keys()),
                            )

                        try:
                            (
                                tool_result,
                                tool_result_files,
                                tool_result_embeds,
                                tool_result_vision_attachments,
                                tool_result_meta,
                            ) = process_tool_result(
                                request,
                                tool_function_name,
                                tool_result,
                                tool_type,
                                direct_tool,
                                metadata,
                                user,
                                model,
                            )
                        except Exception as e:
                            # Post-processing a tool result (image persistence,
                            # embed/UI unwrapping, JSON shaping) must NEVER tear
                            # down the turn or a subagent. The tool itself already
                            # ran (this is exactly what killed the gyms research
                            # chat: a browser_snapshot succeeded, but persisting its
                            # PNG raised and the whole parent turn + 5 subagents
                            # died). Degrade to a usable error result so the loop
                            # proceeds with a per-call error row.
                            log.exception(
                                "process_tool_result failed for tool %r; degrading "
                                "to an error result so the turn can continue",
                                tool_function_name,
                            )
                            tool_result = (
                                f"Tool '{tool_function_name}' ran but its result "
                                f"could not be processed: {e}"
                            )
                            tool_result_files = []
                            tool_result_embeds = []
                            tool_result_vision_attachments = []
                            tool_result_meta = {
                                "error": True,
                                "error_reason": "result post-processing failed",
                            }

                        # Fold a raised-exception error into the structured meta so
                        # the UI shows the error row. An explicit _owui_meta reason
                        # (e.g. from a web tool) takes precedence over the generic
                        # exception string.
                        if tool_exec_error is not None:
                            tool_result_meta = tool_result_meta or {}
                            tool_result_meta["error"] = True
                            tool_result_meta.setdefault(
                                "error_reason", tool_exec_error
                            )
                        tool_result_meta = tool_result_meta or {}

                        # If the tool was a subagent_launch / subagent_continue,
                        # the tool callable stamped its subagent_id into
                        # `request.state.subagent_id_by_tool_call` keyed by
                        # tool_call_id. Surface it on the result entry so
                        # serialize_content_blocks can render a subagent block
                        # rather than the generic tool_calls collapsible, and
                        # so the saved chat row carries the link to the
                        # subagent chat after reload.
                        subagent_id_for_call = None
                        try:
                            subagent_id_for_call = getattr(
                                request.state, "subagent_id_by_tool_call", {}
                            ).get(tool_call_id)
                        except Exception:
                            subagent_id_for_call = None

                        return {
                            "tool_call_id": tool_call_id,
                            "content": tool_result or "",
                            **(
                                {"files": tool_result_files}
                                if tool_result_files
                                else {}
                            ),
                            **(
                                {"embeds": tool_result_embeds}
                                if tool_result_embeds
                                else {}
                            ),
                            **(
                                {"vision_attachments": tool_result_vision_attachments}
                                if tool_result_vision_attachments
                                else {}
                            ),
                            **(
                                {"subagent_id": subagent_id_for_call}
                                if subagent_id_for_call
                                else {}
                            ),
                            **(
                                {"error": True}
                                if tool_result_meta.get("error")
                                else {}
                            ),
                            **(
                                {"error_reason": tool_result_meta["error_reason"]}
                                if tool_result_meta.get("error_reason")
                                else {}
                            ),
                            **(
                                {"notice": tool_result_meta["notice"]}
                                if tool_result_meta.get("notice")
                                else {}
                            ),
                        }

                    def _is_parallelizable(tool_call):
                        name = tool_call.get("function", {}).get("name", "")
                        tool = tools.get(name)
                        return bool(
                            tool
                            and tool.get("metadata", {}).get("parallelizable", False)
                        )

                    # Group consecutive parallelizable tool calls so they run concurrently
                    # via asyncio.gather. A non-parallelizable call acts as a barrier:
                    # everything before it must finish before it runs, and everything after
                    # it waits until it completes. This keeps state-mutating tools strictly
                    # ordered while letting read-only tools (web_search, web_fetch, ...) run
                    # in parallel. Result order matches the tool_calls input order.
                    def _result_for_failed_call(tool_call, exc):
                        """Build a non-empty error tool-result so one failed/cancelled
                        parallel tool call doesn't abandon the whole round. The model
                        sees the error for THAT call and can proceed with its siblings'
                        results (or retry)."""
                        tcid = tool_call.get("id", "") if isinstance(tool_call, dict) else ""
                        name = (
                            tool_call.get("function", {}).get("name", "")
                            if isinstance(tool_call, dict)
                            else ""
                        )
                        msg = (
                            "was cancelled or timed out"
                            if isinstance(exc, asyncio.CancelledError)
                            else f"failed: {exc}"
                        )
                        error_reason = (
                            "cancelled or timed out"
                            if isinstance(exc, asyncio.CancelledError)
                            else "failed"
                        )
                        subagent_id_for_call = None
                        try:
                            subagent_id_for_call = getattr(
                                request.state, "subagent_id_by_tool_call", {}
                            ).get(tcid)
                        except Exception:
                            subagent_id_for_call = None
                        return {
                            "tool_call_id": tcid,
                            "content": f"Tool '{name}' {msg}.",
                            "error": True,
                            "error_reason": error_reason,
                            **(
                                {"subagent_id": subagent_id_for_call}
                                if subagent_id_for_call
                                else {}
                            ),
                        }

                    results = [None] * len(response_tool_calls)
                    i = 0
                    while i < len(response_tool_calls):
                        if _is_parallelizable(response_tool_calls[i]):
                            j = i
                            while j < len(response_tool_calls) and _is_parallelizable(
                                response_tool_calls[j]
                            ):
                                j += 1
                            # return_exceptions=True so ONE parallel call raising
                            # (a subagent self-cancelling on its own timeout, an
                            # inner stream surfacing CancelledError, a tool crash)
                            # does NOT cancel its siblings or tear down the parent
                            # task — which previously left the parent stuck with the
                            # message never finalized and no follow-up request (the
                            # user had to manually type "continue").
                            batch_results = await asyncio.gather(
                                *[
                                    _execute_tool_call(response_tool_calls[k])
                                    for k in range(i, j)
                                ],
                                return_exceptions=True,
                            )
                            # A GENUINE user-stop cancels the parent task itself.
                            # Subagents run inline in this task, so on user-stop the
                            # parent task is `cancelling()` — honor it by re-raising.
                            # Otherwise the CancelledError came from a single child
                            # (its own timeout / a dead inner stream); convert it to
                            # an error result and keep going.
                            _ct = asyncio.current_task()
                            if _ct is not None and _ct.cancelling():
                                raise asyncio.CancelledError()
                            for offset, result in enumerate(batch_results):
                                if isinstance(result, BaseException):
                                    failed_call = response_tool_calls[i + offset]
                                    if isinstance(result, asyncio.CancelledError):
                                        log.warning(
                                            "parallel tool call cancelled in isolation "
                                            "(not a user stop) — converting to an error "
                                            "result so the round can proceed: %s",
                                            failed_call.get("function", {}).get("name", "")
                                            if isinstance(failed_call, dict)
                                            else failed_call,
                                        )
                                    else:
                                        log.error(
                                            "parallel tool call raised: %r",
                                            result,
                                            exc_info=result,
                                        )
                                    results[i + offset] = _result_for_failed_call(
                                        failed_call, result
                                    )
                                else:
                                    results[i + offset] = result
                            i = j
                        else:
                            results[i] = await _execute_tool_call(
                                response_tool_calls[i]
                            )
                            i += 1

                    name_by_id = {
                        tc.get("id"): tc.get("function", {}).get("name", "")
                        for tc in response_tool_calls
                        if isinstance(tc, dict)
                    }
                    msg_id = metadata.get("message_id")
                    slim_results = []
                    allow_lazy_tool_results = (
                        STREAM_PROTOCOL_VERSION == "v2"
                        and not str(metadata.get("chat_id", "")).startswith("local:")
                    )
                    if allow_lazy_tool_results:
                        for r in results:
                            if not r:
                                slim_results.append(r)
                                continue
                            tc_id = r.get("tool_call_id")
                            slim_result, body_result = _slim_tool_result(
                                r, name_by_id.get(tc_id, ""), store_body=True
                            )
                            if body_result is not None and msg_id and tc_id:
                                set_tool_result_body(msg_id, tc_id, body_result)
                            slim_results.append(slim_result)
                    else:
                        slim_results = results

                    # Under v2, keep canonical content_blocks slim after tool
                    # execution. Full web bodies live in tool_result_bodies and
                    # are hydrated only for model replay / explicit UI expansion.
                    tc_block = content_blocks[-1]
                    tc_block["results"] = slim_results
                    if (
                        tc_block.get("started_at") is not None
                        and tc_block.get("ended_at") is None
                    ):
                        tc_block["ended_at"] = time.time()
                        tc_block["duration"] = int(
                            tc_block["ended_at"] - tc_block["started_at"]
                        )
                    content_blocks.append(
                        {
                            "type": "text",
                            "content": "",
                        }
                    )

                    if STREAM_PROTOCOL_VERSION == "v2" and metadata.get("message_id"):
                        msg_id = metadata["message_id"]
                        v2_mirror = getattr(event_emitter, "_v2_mirror", None)
                        emit_raw = getattr(event_emitter, "_emit_raw_primary", None)
                        if v2_mirror is not None and emit_raw is not None:
                            sent = v2_mirror.setdefault("tool_results_sent", set())
                            for slim_result in slim_results:
                                if not slim_result:
                                    continue
                                tc_id = slim_result.get("tool_call_id")
                                if not tc_id or tc_id in sent:
                                    continue
                                set_tool_result(msg_id, tc_id, slim_result)
                                sent.add(tc_id)
                                await emit_raw(
                                    {
                                        "type": "tool_call:result",
                                        "data": {
                                            "message_id": msg_id,
                                            "tool_call_id": tc_id,
                                            "result": slim_result.get("content"),
                                            **(
                                                {
                                                    "result_ref": slim_result[
                                                        "result_ref"
                                                    ]
                                                }
                                                if slim_result.get("result_ref")
                                                else {}
                                            ),
                                            **(
                                                {"result_lazy": True}
                                                if slim_result.get("result_lazy")
                                                else {}
                                            ),
                                            **(
                                                {"size": slim_result["size"]}
                                                if slim_result.get("size") is not None
                                                else {}
                                            ),
                                            **(
                                                {"sha256": slim_result["sha256"]}
                                                if slim_result.get("sha256")
                                                else {}
                                            ),
                                            **(
                                                {"summary": slim_result["summary"]}
                                                if slim_result.get("summary")
                                                else {}
                                            ),
                                            **(
                                                {"files": slim_result["files"]}
                                                if slim_result.get("files")
                                                else {}
                                            ),
                                            **(
                                                {"embeds": slim_result["embeds"]}
                                                if slim_result.get("embeds")
                                                else {}
                                            ),
                                            **(
                                                {
                                                    "subagent_id": slim_result[
                                                        "subagent_id"
                                                    ]
                                                }
                                                if slim_result.get("subagent_id")
                                                else {}
                                            ),
                                            **(
                                                {"error": True}
                                                if slim_result.get("error")
                                                else {}
                                            ),
                                            **(
                                                {
                                                    "error_reason": slim_result[
                                                        "error_reason"
                                                    ]
                                                }
                                                if slim_result.get("error_reason")
                                                else {}
                                            ),
                                            **(
                                                {"notice": slim_result["notice"]}
                                                if slim_result.get("notice")
                                                else {}
                                            ),
                                        },
                                    }
                                )

                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": {
                                "content": serialize_content_blocks(content_blocks),
                                "content_blocks": content_blocks,
                            },
                        }
                    )
                    await checkpoint_stream_state(force=True)

                    try:
                        # Check for pending model switch
                        pending_model = get_pending_model_switch(task_id)
                        if pending_model:
                            old_model_id = model_id
                            model_id = pending_model
                            clear_pending_model_switch(task_id)
                            log.info(
                                f"Model switched from {old_model_id} to {model_id} for task {task_id}"
                            )

                            # Notify frontend that model switch was applied
                            await event_emitter(
                                {
                                    "type": "model-switch:applied",
                                    "data": {
                                        "old_model_id": old_model_id,
                                        "new_model_id": model_id,
                                        "task_id": task_id,
                                    },
                                }
                            )

                        # Check for pending service_tier change. Mutating
                        # form_data here means the next round (and all subsequent
                        # rounds, until changed again) uses the new tier — the
                        # **form_data spread below picks it up.
                        pending_tier = get_pending_service_tier(task_id)
                        if pending_tier:
                            old_tier = form_data.get("service_tier")
                            form_data["service_tier"] = pending_tier
                            clear_pending_service_tier(task_id)
                            log.info(
                                f"service_tier switched from {old_tier} to {pending_tier} for task {task_id}"
                            )
                            await event_emitter(
                                {
                                    "type": "service-tier-switch:applied",
                                    "data": {
                                        "old_service_tier": old_tier,
                                        "new_service_tier": pending_tier,
                                        "task_id": task_id,
                                    },
                                }
                            )

                        # User STEERING: between tool rounds, drain any queued
                        # steer messages and splice them into the in-flight
                        # assistant as `user_steer` blocks. Because the next model
                        # call below sends `content_blocks` inside
                        # `in_flight_assistant` and `blocks_to_api_messages`
                        # expands a `user_steer` block into a real {"role":"user"}
                        # turn, the model sees the steer at its very next step —
                        # mid-task, without restarting. The block also persists +
                        # replays on reload (it lives in content_blocks). Steer
                        # items left unconsumed when the loop ends (model produced
                        # its final answer with no further tools) stay in the queue
                        # and fall through to the post-completion drain as a normal
                        # follow-up generation. Best-effort: never break the loop.
                        if (
                            metadata.get("chat_id")
                            and not metadata.get("subagent_inner")
                            and not str(metadata["chat_id"]).startswith("local:")
                        ):
                            try:
                                from open_webui.utils.chat_queue import (
                                    _item_spec,
                                    broadcast_queue_state,
                                )

                                steer_items = Chats.pop_steer_items_by_id(
                                    metadata["chat_id"]
                                )
                                steer_blocks = []
                                for steer_item in steer_items:
                                    steer_text = (
                                        _item_spec(steer_item).get("content")
                                        or steer_item.get("prompt")
                                        or ""
                                    ).strip()
                                    if steer_text:
                                        steer_blocks.append(
                                            {
                                                "type": "user_steer",
                                                "content": steer_text,
                                            }
                                        )
                                if steer_blocks:
                                    # content_blocks[-1] is the empty text block
                                    # appended after tool execution (the next
                                    # round's stream target, and NOT bound to the
                                    # tail accumulator between rounds). Tuck the
                                    # steer block(s) in BEFORE it so the order is
                                    # assistant tool-calls/results → user steer →
                                    # assistant continues, and the trailing text
                                    # block stays the stream target.
                                    trailing = None
                                    if (
                                        content_blocks
                                        and content_blocks[-1].get("type") == "text"
                                        and not (
                                            content_blocks[-1].get("content") or ""
                                        ).strip()
                                    ):
                                        trailing = content_blocks.pop()
                                    content_blocks.extend(steer_blocks)
                                    content_blocks.append(
                                        trailing
                                        if trailing is not None
                                        else {"type": "text", "content": ""}
                                    )
                                    await event_emitter(
                                        {
                                            "type": "chat:completion",
                                            "data": {
                                                "content": serialize_content_blocks(
                                                    content_blocks
                                                ),
                                                "content_blocks": content_blocks,
                                            },
                                        }
                                    )
                                    await checkpoint_stream_state(force=True)
                                    # Shrink the chip strip on every tab now that
                                    # the steer items left the queue.
                                    await broadcast_queue_state(
                                        metadata.get("user_id"),
                                        metadata["chat_id"],
                                    )
                            except Exception:
                                log.exception(
                                    "steer injection failed for chat %s",
                                    metadata.get("chat_id"),
                                )

                        in_flight_assistant: dict = {
                            "role": "assistant",
                            "content_blocks": content_blocks,
                        }
                        # Backfill any subagent result that finished but whose answer
                        # didn't land in content_blocks (interrupted/partial save), so
                        # the next model round sees every subagent's real output rather
                        # than the "[No output...]" placeholder.
                        _reconcile_subagent_results()
                        tool_result_bodies = get_tool_result_bodies(
                            metadata.get("message_id"), deep_copy=False
                        )
                        if tool_result_bodies:
                            in_flight_assistant["tool_result_bodies"] = (
                                tool_result_bodies
                            )
                        if round_reasoning_details:
                            in_flight_assistant["reasoning_details_per_round"] = list(
                                round_reasoning_details
                            )

                        new_form_data = {
                            **form_data,
                            "model": model_id,
                            "stream": True,
                            "messages": [
                                *form_data["messages"],
                                in_flight_assistant,
                            ],
                        }

                        # Round cap: once the ceiling is reached, force the next
                        # model call to be tool-free so it must produce a final
                        # answer. Drop the `tools`/`tool_choice` from the payload
                        # and append a system notice. The loop then exits because
                        # a tool-free completion adds nothing to `tool_calls`.
                        if (
                            effective_max_tool_rounds > 0
                            and tool_round_count >= effective_max_tool_rounds
                            and not tool_rounds_capped
                        ):
                            tool_rounds_capped = True
                            new_form_data.pop("tools", None)
                            new_form_data.pop("tool_choice", None)
                            new_form_data["messages"] = [
                                *new_form_data["messages"],
                                {
                                    "role": "system",
                                    "content": (
                                        "You have reached the maximum number of "
                                        f"tool-call rounds ({effective_max_tool_rounds}). "
                                        "No further tool calls are available. "
                                        "Provide your best final answer now using "
                                        "the information already gathered."
                                    ),
                                },
                            ]

                        res = await _run_round_with_retry(
                            await generate_chat_completion(
                                request,
                                new_form_data,
                                user,
                            ),
                            new_form_data,
                        )

                        if not _round_already_consumed:
                            # _run_round_with_retry returned a response it did not
                            # consume (an error / unknown shape). Surface it, then
                            # end the loop.
                            log.debug(
                                f"generate_chat_completion returned non-streaming response: {res}"
                            )
                            try:
                                # Attempt to read the error response
                                if hasattr(res, "body"):
                                    error_content = (
                                        res.body.decode("utf-8")
                                        if isinstance(res.body, bytes)
                                        else str(res.body)
                                    )
                                    log.error(
                                        f"Error response content: {error_content}"
                                    )

                                    try:
                                        error_json = json.loads(error_content)
                                        error_msg = error_json.get(
                                            "error", error_content
                                        )
                                        if isinstance(error_msg, dict):
                                            error_msg = error_msg.get(
                                                "message",
                                                error_msg.get("detail", str(error_msg)),
                                            )
                                    except:
                                        error_msg = error_content

                                    await event_emitter(
                                        {
                                            "type": "chat:message:error",
                                            "data": {
                                                "error": {"content": str(error_msg)}
                                            },
                                        }
                                    )
                            except Exception as read_err:
                                log.error(f"Could not read error response: {read_err}")
                            break
                    except Exception as e:
                        log.exception(f"Error in tool loop: {e}")
                        terminal_error = {"content": f"Error in tool loop: {str(e)}"}
                        await event_emitter(
                            {
                                "type": "chat:message:error",
                                "data": {"error": terminal_error},
                            }
                        )
                        break

                if terminal_error is not None:
                    error_content = (
                        terminal_error.get("content")
                        if isinstance(terminal_error, dict)
                        else str(terminal_error)
                    )
                    _finalize_open_agentic_block(content_blocks)
                    update_data = _build_checkpoint_update(include_legacy_content=True)
                    update_data["error"] = {"content": error_content}
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        update_data, return_model=False
                    )
                    if STREAM_PROTOCOL_VERSION == "v2" and metadata.get("message_id"):
                        # Push full final content into the RAM snapshot alongside
                        # the error status so a reload racing this terminal write
                        # sees the complete partial content (not a cadence snapshot
                        # that can lag). _build_checkpoint_update already
                        # materialized the tail above.
                        set_stream_state(
                            metadata["message_id"],
                            {
                                "content_blocks": _strip_tool_results(content_blocks),
                                "status": "error",
                                "error": {"content": error_content},
                                "snapshot_version": stream_version_get(
                                    metadata["message_id"]
                                ),
                            },
                        )
                        clear_tool_result_bodies(metadata["message_id"])
                        clear_stream_state(metadata["message_id"])
                    # Genuine generation error: PAUSE the queue. Clear only this
                    # generation's own draining marker so the user can inspect the
                    # error and resume manually.
                    if metadata.get("chat_id") and metadata.get("message_id"):
                        try:
                            from open_webui.utils.chat_queue import clear_draining

                            await clear_draining(
                                getattr(request.app.state, "redis", None),
                                metadata["chat_id"],
                                finished_response_id=metadata.get("message_id"),
                                user_id=metadata.get("user_id"),
                            )
                        except Exception:
                            log.exception("queue clear_draining on error failed")
                    return

                title = Chats.get_chat_title_by_id(metadata["chat_id"])
                # Canonical end-of-stream persist: ensure the tail accumulator is
                # folded into content_blocks before serialize/split.
                _tail_materialize()
                # Make the durable record authoritative: backfill any subagent
                # result that finished but never made it into content_blocks, so a
                # reload / fresh client / the next turn all see complete results.
                _reconcile_subagent_results()
                if STREAM_PROTOCOL_VERSION == "v2" and not str(
                    metadata.get("chat_id", "")
                ).startswith("local:"):
                    final_slim_blocks, final_split_bodies = split_tool_result_bodies(
                        content_blocks
                    )
                    for _tcid, _body in final_split_bodies.items():
                        set_tool_result_body(metadata.get("message_id"), _tcid, _body)
                else:
                    final_slim_blocks = content_blocks
                final_content = serialize_content_blocks(final_slim_blocks, force=True)

                container_output_files = await import_changed_container_outputs(
                    request,
                    metadata,
                    user,
                    content=final_content,
                    content_blocks=final_slim_blocks,
                )
                if container_output_files:
                    await event_emitter(
                        {
                            "type": "files",
                            "data": {"files": container_output_files},
                        }
                    )

                data = {
                    "done": True,
                    # force=True: end-of-stream final emit. Use slim blocks so
                    # huge web tool bodies do not re-enter the socket hot path.
                    "content": final_content,
                    "content_blocks": final_slim_blocks,
                    "title": title,
                }

                if container_output_files:
                    data["files"] = container_output_files

                # Include usage data if available
                if response_usage:
                    data["usage"] = response_usage
                    data["selected_model_id"] = (
                        model_id  # Include model ID for socket emission
                    )

                if STREAM_PROTOCOL_VERSION == "v2" or not ENABLE_REALTIME_CHAT_SAVE:
                    # Save the final canonical message in the database. v2 no
                    # longer relies on per-token DB writes; the live stream
                    # store is authoritative while generation is active and
                    # this final checkpoint is the durable history record.
                    update_data = _build_checkpoint_update(include_legacy_content=True)
                    # Persist done:true so the terminal state is durable, not
                    # only reconstructed on load. Lets a reloaded/zero-tab client
                    # (and the queue drain's reconciliation) see a definitively
                    # completed message instead of inferring it from active-stream
                    # absence.
                    update_data["done"] = True
                    if container_output_files:
                        current_message = (
                            Chats.get_message_by_id_and_message_id(
                                metadata["chat_id"], metadata["message_id"]
                            )
                            or {}
                        )
                        update_data["files"] = current_message.get(
                            "files", container_output_files
                        )

                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        update_data, return_model=False
                    )
                    if STREAM_PROTOCOL_VERSION == "v2" and metadata.get("message_id"):
                        clear_tool_result_bodies(metadata["message_id"])
                elif response_usage:
                    # Non-v2 realtime-save mode writes content on the hot path;
                    # still persist final usage so opened full subagent chats
                    # and future rebuilds can recover provider/cache details.
                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {"usage": response_usage}, return_model=False
                    )

                # Send a webhook notification if the user is not active.
                # Hidden subagent chats are implementation detail rows; sending
                # one webhook per inner worker would be noisy and would force us
                # to keep a legacy full-text buffer just for those hidden runs.
                if not metadata.get(
                    "subagent_inner"
                ) and not get_active_status_by_user_id(user.id):
                    webhook_url = Users.get_user_webhook_url_by_id(user.id)
                    if webhook_url:
                        plain_content = get_plain_content()
                        await post_webhook(
                            request.app.state.WEBUI_NAME,
                            webhook_url,
                            f"{title} - {request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}\n\n{plain_content}",
                            {
                                "action": "chat",
                                "message": plain_content,
                                "title": title,
                                "url": f"{request.app.state.config.WEBUI_URL}/c/{metadata['chat_id']}",
                            },
                        )

                await event_emitter(
                    {
                        "type": "chat:completion",
                        "data": data,
                    }
                )

                if STREAM_PROTOCOL_VERSION == "v2" and metadata.get("message_id"):
                    msg_id = metadata["message_id"]
                    emit_raw = getattr(event_emitter, "_emit_raw_primary", None)
                    final_blocks = data.get("content_blocks") or content_blocks
                    final_content = data.get("content") or ""
                    final_hash = hashlib.sha256(
                        (final_content or "").encode("utf-8", "replace")
                    ).hexdigest()
                    set_stream_state(
                        msg_id,
                        {
                            "content_blocks": _strip_tool_results(final_blocks),
                            "status": "done",
                            **({"usage": response_usage} if response_usage else {}),
                        },
                    )
                    version = stream_version_incr(msg_id)
                    # Terminal snapshot carries the full final content; stamp
                    # snapshot_version to the terminal version so a reattach within
                    # the done-grace window gets a consistent (content, version).
                    set_stream_state(msg_id, {"snapshot_version": version})
                    chat_obj = None
                    try:
                        chat_obj = Chats.get_chat_by_id(metadata["chat_id"])
                    except Exception:
                        chat_obj = None
                    chat_updated_at = (
                        getattr(chat_obj, "updated_at", None) if chat_obj else None
                    )

                    done_payload = {
                        "type": "chat:done",
                        "data": {
                            "message_id": msg_id,
                            "version": version,
                            "final_content_hash": final_hash,
                            **(
                                {"updated_at": chat_updated_at}
                                if chat_updated_at is not None
                                else {}
                            ),
                            **({"usage": response_usage} if response_usage else {}),
                        },
                    }
                    if emit_raw is not None:
                        await emit_raw(done_payload)
                    else:
                        await event_emitter(done_payload)

                    if user and chat_obj is not None:
                        try:
                            await broadcast_sidebar_event(
                                user.id,
                                {
                                    "type": "chat:updated",
                                    "data": {
                                        "id": metadata["chat_id"],
                                        "updated_at": chat_updated_at,
                                        "created_at": getattr(
                                            chat_obj, "created_at", None
                                        ),
                                        "title": getattr(chat_obj, "title", None),
                                        "pinned": bool(
                                            getattr(chat_obj, "pinned", False) or False
                                        ),
                                        "archived": bool(
                                            getattr(chat_obj, "archived", False)
                                            or False
                                        ),
                                        "folder_id": getattr(
                                            chat_obj, "folder_id", None
                                        ),
                                    },
                                },
                                skip_sid=metadata.get("session_id"),
                            )
                        except Exception as e:
                            log.debug(f"chat:updated broadcast failed: {e}")

                    clear_stream_state(msg_id)
                else:
                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": {"done": True},
                        }
                    )

                # B12: outlet filters run server-side at the tail of the
                # stream. The frontend used to POST /api/chat/completed for
                # this; that route is now a no-op shim. If a filter mutates
                # content, the helper persists it and emits a catch-up event
                # so the frontend mirror updates.
                try:
                    await run_outlet_filters_on_completed_stream(
                        request=request,
                        user=user,
                        metadata=metadata,
                        model=model,
                        model_id=model_id,
                        filter_ids=metadata.get("filter_ids", []),
                        content_blocks=content_blocks,
                        event_emitter=event_emitter,
                        event_caller=event_caller,
                        serialize_content_blocks=serialize_content_blocks,
                    )
                except Exception as e:
                    log.exception(f"Outlet filter run failed: {e}")

                await background_tasks_handler()

                # Autonomous queue drain: this generation finished CLEANLY, so
                # start the next queued follow-up (if any) server-side. Runs only
                # here — the terminal-error `return` above and the CancelledError
                # handler below bypass it, so Stop and genuine errors PAUSE the
                # queue. Best-effort: a drain failure must never break the
                # generation that just succeeded.
                if metadata.get("chat_id") and metadata.get("message_id"):
                    try:
                        from open_webui.utils.chat_queue import maybe_drain_queue

                        await maybe_drain_queue(
                            request.app,
                            user,
                            metadata["chat_id"],
                            finished_response_id=metadata.get("message_id"),
                        )
                    except Exception:
                        log.exception("queue drain after clean completion failed")
            except asyncio.CancelledError:
                log.warning("Task was cancelled!")
                await event_emitter({"type": "chat:tasks:cancel"})

                # Stop pressed mid-stream: PAUSE the queue. Clear only THIS
                # generation's draining marker so a queued follow-up that was
                # already started isn't disturbed; the user resumes manually.
                # clear_draining also downgrades any pending STEER items to
                # after_final (their target response is over — see clear_draining).
                if metadata.get("chat_id") and metadata.get("message_id"):
                    try:
                        from open_webui.utils.chat_queue import clear_draining

                        await clear_draining(
                            getattr(request.app.state, "redis", None),
                            metadata["chat_id"],
                            finished_response_id=metadata.get("message_id"),
                            user_id=metadata.get("user_id"),
                        )
                    except Exception:
                        log.exception("queue clear_draining on cancel failed")

                if STREAM_PROTOCOL_VERSION == "v2" and metadata.get("message_id"):
                    # Fold the tail buffer and push the FULL final content into the
                    # RAM snapshot before flipping to "cancelled", so a reload that
                    # races this terminal transition sees the complete partial
                    # response (not the cadence snapshot, which can lag by up to
                    # SNAPSHOT_CHAR_DELTA). Stamp snapshot_version to the live wire
                    # version (content now includes everything emitted).
                    _tail_materialize()
                    _finalize_open_agentic_block(content_blocks)
                    set_stream_state(
                        metadata["message_id"],
                        {
                            "content_blocks": _strip_tool_results(content_blocks),
                            "status": "cancelled",
                            "snapshot_version": stream_version_get(
                                metadata["message_id"]
                            ),
                        },
                    )
                    clear_stream_state(metadata["message_id"])

                if STREAM_PROTOCOL_VERSION == "v2" or not ENABLE_REALTIME_CHAT_SAVE:
                    # Save message in the database
                    _finalize_open_agentic_block(content_blocks)
                    update_data = _build_checkpoint_update(include_legacy_content=True)
                    # Mark the message TERMINAL so its chat (the subagent's own
                    # hidden chat, or a regular chat) doesn't render as perpetually
                    # generating after a cancel. The frontend treats a message as
                    # finished on `done || error`; without this a cancelled subagent
                    # opened directly shows a forever-spinning, never-"stopped" turn
                    # even though it is terminal. (The clean-completion path sets the
                    # same flag.)
                    update_data["done"] = True

                    Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        update_data, return_model=False
                    )
                    if STREAM_PROTOCOL_VERSION == "v2" and metadata.get("message_id"):
                        clear_tool_result_bodies(metadata["message_id"])

                # Re-raise after cleanup. Catching CancelledError to persist the
                # partial response is fine, but the cancellation MUST propagate so
                # the task actually unwinds and exits. Swallowing it leaves the task
                # "alive" inside anyio's cancel scope, which then reschedules
                # _deliver_cancellation via loop.call_soon every tick FOREVER —
                # pinning a CPU core at idle until the process restarts. (Confirmed
                # by py-spy: ~78% of CPU in _deliver_cancellation with no app frames,
                # reproducing at idle after a Stop/disconnect.) The subagent cancel
                # handlers already re-raise for the same reason.
                raise

            if getattr(response, "background", None) is not None:
                await response.background()

        return await response_handler(response, events)

    else:
        # Fallback to the original response
        async def stream_wrapper(original_generator, events):
            def wrap_item(item):
                return f"data: {item}\n\n"

            for event in events:
                event, _ = await process_filter_functions(
                    request=request,
                    filter_functions=filter_functions,
                    filter_type="stream",
                    form_data=event,
                    extra_params=extra_params,
                )

                if event:
                    yield wrap_item(json.dumps(event))

            async for data in original_generator:
                data, _ = await process_filter_functions(
                    request=request,
                    filter_functions=filter_functions,
                    filter_type="stream",
                    form_data=data,
                    extra_params=extra_params,
                )

                if data:
                    yield data

        return StreamingResponse(
            stream_wrapper(response.body_iterator, events),
            headers=dict(response.headers),
            background=response.background,
        )
