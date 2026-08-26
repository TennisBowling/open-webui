"""Subagent runner — orchestrates the inner chat-completion pipeline for a
research subagent spawned by the parent chat model.

Architecture, condensed:

- The parent chat's tool-call loop awaits ``subagent_launch`` /
  ``subagent_continue`` from ``utils.subagent_tool``. Those tool wrappers
  delegate here.
- ``run_subagent_launch`` creates a hidden Chat row (``meta.subagent_of`` set
  atomically so it never appears in the user's sidebar) and calls
  ``_run_inner_chat``.
- ``run_subagent_continue`` looks up an existing subagent chat by name-or-id
  and calls ``_run_inner_chat`` to drive one more turn on top of its history.
- ``_run_inner_chat`` builds inner_form_data + inner_metadata, then re-enters
  the exact same pipeline the regular chat path uses
  (``process_chat_payload`` → ``chat_completion_handler`` →
  ``process_chat_response``). The middleware's tool-call loop, reasoning
  preservation (``REASONING_DETAILS.md`` contract), filter pipelines, etc.
  all work for free because we're inside the same machinery.

Event forwarding:

- The inner pipeline emits events scoped to the subagent's chat_id; the
  parent's frontend would normally drop those (``isVisibleChatEvent``).
- We install an ``event_emitter_override`` on inner_metadata. It (a) still
  calls the default subagent emitter to persist to the subagent's chat row,
  and (b) re-emits whitelisted event types to the *parent's* emitter wrapped
  as ``{type: "chat:subagent:update", data: {subagent_id, num, name,
  parent_message_id, inner_event}}`` — which the frontend routes into the
  ``subagentLiveStates`` store and renders inside the parent message.

Nesting prevention:

- The inner ``features`` dict is empty and inner ``tool_ids`` always include
  ``builtin:web_search``. If the admin gate and per-chat opt-in are both on,
  selected admin external tool servers are inherited too. The subagent tools
  are NOT registered for the inner run, so a subagent can't recursively spawn
  another.

request.state save/restore:

- ``utils/chat.generate_chat_completion`` (called by middleware's tool-call
  loop for each round AFTER the first) merges ``request.state.metadata`` over
  ``form_data["metadata"]`` (parent wins). For the subagent run we need the
  inner metadata to win, so we swap ``request.state.{metadata,model,direct}``
  for the duration of the run and restore on exit. The parent's outer
  pipeline is suspended at ``await tool_function(...)`` so no concurrent
  reader sees the swap.
"""

import asyncio
import copy
import html
import json
import logging
import re
import time
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from fastapi import Request

from open_webui.env import (
    SRC_LOG_LEVELS,
    STREAM_PROTOCOL_VERSION,
    SUBAGENT_RUN_TIMEOUT_SECONDS,
    SUBAGENT_MAX_CONCURRENCY,
    SUBAGENT_PROVIDER_STREAM,
)
from open_webui.models.chats import (
    Chats,
    ChatHistoryConflictError,
    ChatImportForm,
)
from open_webui.models.models import Models
from open_webui.models.users import Users
from open_webui.socket.main import get_event_call, get_event_emitter
from open_webui.utils.chat import generate_chat_completion as chat_completion_handler
from open_webui.utils.messages import blocks_to_api_messages, blocks_to_plain_text
from open_webui.utils.lazy_blocks import sanitize_content_blocks
from open_webui.socket.main import (
    stream_version_init,
    stream_version_incr,
    stream_version_get,
    stream_version_flush,
    set_stream_state,
    clear_stream_state,
    emit_to_primary,
    emit_user_fanout,
)
from open_webui.utils.middleware import (
    _emit_delta_for_blocks,
    _strip_tool_results,
    current_tool_call_id_var,
    current_tool_parent_task_var,
    process_chat_payload,
    process_chat_response,
)
from open_webui.utils.misc import get_message_list

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


async def _wait_shielded_task_to_completion(task: asyncio.Future) -> bool:
    """Wait for one critical child task even through repeated outer cancels.

    ``asyncio.shield`` alone only protects the child from cancellation; a second
    Stop still interrupts the outer await and lets the child run detached. For
    short database state transitions (create/register/terminal write/delete),
    detaching creates an unknown-commit window. Re-await the same protected task
    until it settles and return whether any outer cancellation was observed. The
    caller can then inspect ``task.result()`` and propagate cancellation only
    after restoring a coherent durable state.
    """
    cancellation_seen = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation_seen = True
            continue
    return cancellation_seen


async def _delete_hidden_chat_to_completion(
    chat_id: str, *, reason: str
) -> tuple[bool, bool]:
    """Delete one exact hidden-chat row without an unknown cancellation window.

    Returns ``(outer_cancellation_seen, deleted)``. The caller decides whether
    to propagate cancellation after the invariant-restoring delete settles.
    """
    delete_task = asyncio.create_task(Chats.delete_chat_by_id(chat_id))
    cancellation_seen = await _wait_shielded_task_to_completion(delete_task)
    if delete_task.cancelled():
        log.error("%s: hidden-chat delete task was cancelled for %s", reason, chat_id)
        return cancellation_seen, False
    error = delete_task.exception()
    if error is not None:
        log.error(
            "%s: hidden-chat delete failed for %s",
            reason,
            chat_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        return cancellation_seen, False
    deleted = bool(delete_task.result())
    if not deleted:
        log.error("%s: hidden-chat delete returned false for %s", reason, chat_id)
    return cancellation_seen, deleted


# Bound the number of subagents running concurrently per worker. `subagent_launch`
# is a parallelizable tool, so a single parent turn can otherwise gather an
# unbounded number of full nested pipelines. Lazily created so the semaphore
# binds to the running event loop. None when the bound is disabled.
_subagent_concurrency_sem: Optional[asyncio.Semaphore] = None


def _get_subagent_concurrency_sem() -> Optional[asyncio.Semaphore]:
    global _subagent_concurrency_sem
    if SUBAGENT_MAX_CONCURRENCY <= 0:
        return None
    if _subagent_concurrency_sem is None:
        _subagent_concurrency_sem = asyncio.Semaphore(SUBAGENT_MAX_CONCURRENCY)
    return _subagent_concurrency_sem


def _subagent_cancel_is_from_parent_task() -> bool:
    """True when cancellation came from the parent chat/rerun task.

    Launch/continue tools run under the parent tool loop's ``asyncio.gather`` as
    child tasks. A child task can be cancelled in isolation while the parent
    generation is still alive; treating ``current_task().cancelling()`` as a user
    stop in that case incorrectly records the subagent as ``cancelled``. The
    middleware stamps the parent response task in a ContextVar for gathered tool
    branches; prefer that signal when present. Detached reruns have no parent tool
    task, so they fall back to their own task's cancelling state.
    """
    parent_task = current_tool_parent_task_var.get()
    if parent_task is not None:
        try:
            return bool(parent_task.cancelling())
        except Exception:
            return False
    current_task = asyncio.current_task()
    return bool(current_task is not None and current_task.cancelling())


def _clear_isolated_child_cancellation() -> int:
    """Clear pending cancellation on this child task when the parent is alive.

    Once a child task catches ``CancelledError``, Python keeps a cancellation count
    on that task. If we have classified it as NOT coming from the parent response
    task, we are converting it into a retryable/error path; leaving the count set
    would re-inject ``CancelledError`` into the cleanup/retry awaits and turn the
    recovery path back into a fake stop. Returns how many cancellations were
    consumed, mostly for tests/logging.
    """
    task = asyncio.current_task()
    cleared = 0
    if task is not None and hasattr(task, "uncancel"):
        while task.cancelling():
            task.uncancel()
            cleared += 1
    return cleared


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _branch_message_ids(parent_chat, leaf_message_id: Optional[str] = None) -> list[str]:
    """Return message ids on the active branch ending at ``leaf_message_id``.

    Subagents are part of the model-visible transcript, and the model-visible
    transcript is a single branch (the same walk as ``createMessagesList``). Scanning
    every message in a branched chat lets an inactive branch influence name/id
    resolution and launch numbering, so branch-scoped callers use this helper.

    If the leaf is missing/corrupt, fall back to all messages to preserve the old
    tolerant behavior rather than making subagents disappear entirely.
    """
    messages = _history_messages(parent_chat)
    if not messages:
        return []
    leaf = leaf_message_id or _history_current_id(parent_chat)
    if not leaf or leaf not in messages:
        return list(messages.keys())

    ids: list[str] = []
    seen: set[str] = set()
    current_id = leaf
    while current_id and current_id in messages and current_id not in seen:
        seen.add(current_id)
        ids.append(current_id)
        msg = messages.get(current_id)
        current_id = msg.get("parentId") if isinstance(msg, dict) else None
    ids.reverse()
    return ids


def _gather_all_subagent_runs(parent_chat, leaf_message_id: Optional[str] = None) -> dict:
    """Collect ``subagent_runs`` entries on one branch of the parent chat.

    Returns a flat ``{entry_key: run_dict}`` map. When ``leaf_message_id`` is
    supplied (the normal launch/continue path), only the branch that the parent
    model can currently see is scanned. Without a leaf, this retains the old
    whole-history behavior for defensive/legacy callers.

    This is the source of truth for ``run_subagent_continue`` (name/id resolution)
    and for ``run_subagent_launch`` (collision-disambiguation and num assignment)."""
    if not parent_chat:
        return {}
    messages = _history_messages(parent_chat)
    all_runs: dict = {}
    message_ids = (
        _branch_message_ids(parent_chat, leaf_message_id)
        if leaf_message_id
        else list(messages.keys())
    )
    for msg_id in message_ids:
        msg = messages.get(msg_id)
        if not isinstance(msg, dict):
            continue
        runs = msg.get("subagent_runs")
        if isinstance(runs, dict):
            all_runs.update(runs)
    return all_runs


def _disambiguate_name(name: str, all_runs: dict) -> tuple[str, bool]:
    """If ``name`` is already taken by an existing subagent in this parent
    chat, return ``("name_2", True)`` (or ``_3``, etc.). Otherwise return
    ``(name, False)``. The second tuple element tells the caller whether to
    surface a "Note: renamed to X" line in the tool result.

    Comparison is case-insensitive to catch obvious near-collisions
    (``Berkeley`` vs ``berkeley``); the disambiguator preserves the parent
    model's chosen casing on the base."""
    if not name:
        return ("subagent", False)
    existing_names_lower = {
        (run.get("name") or "").lower()
        for run in all_runs.values()
        if isinstance(run, dict)
    }
    if name.lower() not in existing_names_lower:
        return (name, False)
    n = 2
    while True:
        candidate = f"{name}_{n}"
        if candidate.lower() not in existing_names_lower:
            return (candidate, True)
        n += 1


def _resolve_subagent_model_id(
    request: Request, parent_chat, parent_model: Optional[dict]
) -> Optional[str]:
    """Resolution order: per-chat override → global default → parent model.
    Returns None if no model is resolvable or if the resolved model isn't in
    ``app.state.MODELS`` (caller surfaces an error in that case)."""
    chat_params = ((parent_chat.chat if parent_chat else {}) or {}).get("params") or {}
    candidates = [
        chat_params.get("subagentModel"),
        getattr(request.app.state.config, "SUBAGENT_DEFAULT_MODEL", None) or None,
        (parent_model or {}).get("id"),
    ]
    for cand in candidates:
        if cand and cand in request.app.state.MODELS:
            return cand
    return None


def _resolve_subagent_context_fallback_model(
    request: Request, current_model_id: str
) -> Optional[dict]:
    """Resolve the configured successor for an exhausted subagent model.

    Returning the model object (rather than only its id) lets the guarded
    runner immediately rebuild the complete inner request with the successor's
    model metadata and system prompt. Missing, unavailable, and self-referential
    values all mean "fallback disabled".
    """
    fallback_model_id = (
        getattr(
            request.app.state.config,
            "SUBAGENT_CONTEXT_FALLBACK_MODEL",
            "",
        )
        or ""
    )
    fallback_model_id = str(fallback_model_id).strip()
    if not fallback_model_id or fallback_model_id == current_model_id:
        return None
    model = request.app.state.MODELS.get(fallback_model_id)
    return model if isinstance(model, dict) else None


def _as_tool_id_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def _dedupe_tool_ids(tool_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for tool_id in tool_ids:
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)
        deduped.append(tool_id)
    return deduped


def _is_inheritable_external_tool_id(tool_id: str) -> bool:
    # Admin-added OpenAPI and MCP tool servers use the `server:` namespace.
    # Skip direct_server ids because their specs live in the browser payload,
    # and skip builtins so subagents cannot re-enable nested subagents/data-viz.
    return bool(tool_id and tool_id.startswith("server:"))


def _normalize_container_server_id(server_id: str | None) -> str:
    server_id = (server_id or "").strip()
    if server_id.startswith("server:mcp:"):
        server_id = server_id[len("server:mcp:") :]
    return server_id


def _container_tool_id(request: Request) -> str:
    server_id = _normalize_container_server_id(
        getattr(request.app.state.config, "CONTAINER_MCP_SERVER_ID", "")
    )
    return f"server:mcp:{server_id}" if server_id else ""


def _tool_id_matches(tool_id: str, target: str) -> bool:
    return bool(target and (tool_id == target or tool_id.startswith(f"{target}|")))


def _resolve_subagent_tool_ids(
    request: Request, chat_params: dict, parent_metadata: Optional[dict] = None
) -> list[str]:
    tool_ids = ["builtin:web_search"]

    allow_external_tools = bool(
        getattr(request.app.state.config, "SUBAGENT_ALLOW_EXTERNAL_TOOLS", False)
    )
    if not allow_external_tools:
        return tool_ids

    # Back-compat/power-user hook: explicit subagent extras remain supported,
    # but only while the admin global gate is enabled.
    for tool_id in _as_tool_id_list(chat_params.get("subagentExtraToolIds")):
        # Exclude builtin:subagent (no recursive subagents), builtin:data_viz —
        # show_widget round-trips to a live FRONTEND session to render+confirm,
        # but a subagent runs against a hidden chat with no frontend, so every
        # show_widget would stall the subagent for the full 30s render timeout —
        # and builtin:automations, so a research worker can never leave a
        # recurring schedule behind the user never asked for.
        if (
            tool_id
            not in ("builtin:subagent", "builtin:data_viz", "builtin:automations")
            and not tool_id.startswith("direct_server:")
        ):
            tool_ids.append(tool_id)

    parent_metadata = parent_metadata or {}
    metadata_params = parent_metadata.get("params") or {}
    external_tools_enabled = metadata_params.get(
        "subagentExternalToolsEnabled",
        chat_params.get("subagentExternalToolsEnabled", True),
    )

    if external_tools_enabled:
        current_tool_ids = _as_tool_id_list(parent_metadata.get("tool_ids"))
        selected_tool_ids = current_tool_ids or _as_tool_id_list(
            chat_params.get("selectedToolIds")
        )
        for tool_id in selected_tool_ids:
            if _is_inheritable_external_tool_id(tool_id):
                tool_ids.append(tool_id)

    return _dedupe_tool_ids(tool_ids)


def _subagent_container_shared_context(
    request: Request,
    parent_metadata: dict,
    inner_tool_ids: list[str],
    import_outputs: bool = False,
    subagent_id: Optional[str] = None,
) -> dict:
    container_tool_id = _container_tool_id(request)
    if not any(
        _tool_id_matches(tool_id, container_tool_id) for tool_id in inner_tool_ids
    ):
        return {}

    parent_chat_id = parent_metadata.get("chat_id")
    if not parent_chat_id:
        return {}

    server_id = _normalize_container_server_id(
        getattr(request.app.state.config, "CONTAINER_MCP_SERVER_ID", "")
    )
    # Per-AGENT browser session: a subagent shares the PARENT chat's container (its
    # MCP X-Chat-Id = parent chat_id), so without a per-agent token the daemon
    # would route every subagent's browser action to the SAME page as the parent
    # and they'd clobber each other. We override the {{BROWSER_SESSION}} header
    # source (browser_session in the per-server header context) with the
    # subagent_id, so this subagent drives its OWN page/tab in the shared browser
    # context (still logged in as the same user — cookies are context-level). The
    # parent, by contrast, has no override and uses "main" (see
    # resolve_tool_server_headers). browser_session is also surfaced top-level so
    # the inner pipeline (the live-frame poller) can tag frames by session.
    browser_session = str(subagent_id or "") or None
    header_ctx: dict = {
        "chat_id": parent_chat_id,
        "message_id": parent_metadata.get("message_id"),
        "session_id": parent_metadata.get("session_id"),
    }
    if browser_session:
        header_ctx["browser_session"] = browser_session
    shared: dict = {
        "container_workspace_chat_id": parent_chat_id,
        "container_workspace_message_id": parent_metadata.get("message_id"),
        "container_workspace_output_message_id": parent_metadata.get("message_id"),
        "container_workspace_reuse_existing_inputs": True,
        "container_workspace_import_outputs": import_outputs,
        "tool_server_header_context": {server_id: header_ctx},
    }
    if browser_session:
        shared["browser_session"] = browser_session
    return shared


async def _external_tools_prompt(request: Request, inner_tool_ids: list[str]) -> str:
    has_external_tools = any(
        tool_id != "builtin:web_search" for tool_id in inner_tool_ids
    )
    if not has_external_tools:
        return ""
    return (
        getattr(request.app.state.config, "SUBAGENT_EXTERNAL_TOOLS_PROMPT", "") or ""
    ).strip()


async def _compose_subagent_system_prompt(
    request: Request, subagent_model_id: str, external_tools_prompt: str = ""
) -> str:
    """Return the subagent's system prompt: the model's own admin-set system
    prompt with the optional SUBAGENT_SYSTEM_PROMPT_APPEND appended after a
    blank line. No admin-level preamble replaces the model's persona."""
    model_prompt = ""
    try:
        model_info = await Models.get_model_by_id(subagent_model_id)
        if model_info and model_info.params:
            params = (
                model_info.params.model_dump()
                if hasattr(model_info.params, "model_dump")
                else dict(model_info.params)
            )
            model_prompt = (params.get("system") or "").strip()
    except Exception as e:  # noqa: BLE001
        log.debug(f"could not load model system prompt for {subagent_model_id}: {e}")

    # If the admin set prompt appends, tack them on after the model's own
    # prompt separated by blank lines so the model sees distinct sections.
    append = (
        getattr(request.app.state.config, "SUBAGENT_SYSTEM_PROMPT_APPEND", "") or ""
    ).strip()
    # C12: when the subagent model has NO own system prompt (a base/direct
    # connection on a default install), fall back to the admin-editable
    # SUBAGENT_SYSTEM_PROMPT (default = the research protocol + stop condition)
    # as the base layer — otherwise the subagent runs with an EMPTY system prompt
    # and an admin's edits to SUBAGENT_SYSTEM_PROMPT silently do nothing.
    base = model_prompt
    if not base:
        base = (
            getattr(request.app.state.config, "SUBAGENT_SYSTEM_PROMPT", "") or ""
        ).strip()
    parts = [base, append, external_tools_prompt.strip()]
    return "\n\n".join(part for part in parts if part).strip()


def _subagent_tool_name_for_run(run: dict) -> str:
    return "subagent_continue" if run.get("continuation") else "subagent_launch"


def _subagent_tool_arguments_for_run(run: dict) -> str:
    """Best-effort reconstruction of the parent tool-call arguments.

    This is only for the reload placeholder UI. The actual parent-model replay
    uses the canonical content_blocks generated by middleware whenever those
    are available.
    """
    if run.get("continuation"):
        args = {
            "name_or_id": (
                run.get("name")
                or run.get("subagent_id")
                or run.get("chat_id")
                or ""
            ),
            "prompt": run.get("prompt") or "",
        }
    else:
        args = {
            "name": run.get("name") or "subagent",
            "prompt": run.get("prompt") or "",
        }
        if run.get("background"):
            args["background"] = run.get("background") or ""
    return json.dumps(args, ensure_ascii=False)


def _subagent_run_is_terminal(run: dict) -> bool:
    # C19: a run that is actively re-running is NON-terminal even though the rerun
    # deliberately PRESERVES the prior final_text on the entry (C5). Status wins
    # over the presence of preserved final_text/ended_at. Detached rerun setup
    # does not invoke placeholder sync: the old parent tool-result remains a
    # coherent snapshot until the guarded success write atomically replaces it.
    if run.get("status") == "running":
        return False
    return bool(
        run.get("status") in ("done", "error", "cancelled")
        or run.get("final_text")
        or run.get("error")
        or run.get("ended_at")
    )


def _subagent_placeholder_html(run: dict) -> str:
    tool_call_id = str(
        run.get("tool_call_id")
        or run.get("entry_key")
        or run.get("subagent_id")
        or run.get("chat_id")
        or ""
    )
    subagent_id = str(run.get("subagent_id") or run.get("chat_id") or "")
    tool_name = _subagent_tool_name_for_run(run)
    done_flag = "true" if _subagent_run_is_terminal(run) else "false"
    tool_arguments = _subagent_tool_arguments_for_run(run)
    return (
        f'<details type="subagent_launch" done="{done_flag}" '
        f'tool_call_id="{html.escape(tool_call_id)}" '
        f'id="{html.escape(subagent_id)}" '
        f'name="{html.escape(tool_name)}" '
        f'arguments="{html.escape(json.dumps(tool_arguments, ensure_ascii=False))}">\n'
        f'<summary>Subagent</summary>\n'
        f'</details>'
    )


def _subagent_placeholder_block(run: dict) -> dict:
    tool_call_id = str(
        run.get("tool_call_id")
        or run.get("entry_key")
        or run.get("subagent_id")
        or run.get("chat_id")
        or ""
    )
    subagent_id = str(run.get("subagent_id") or run.get("chat_id") or "")
    tool_name = _subagent_tool_name_for_run(run)
    tool_arguments = _subagent_tool_arguments_for_run(run)
    block = {
        "type": "tool_calls",
        "content": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": tool_arguments,
                },
            }
        ],
    }
    if _subagent_run_is_terminal(run):
        error = run.get("error")
        if isinstance(error, dict):
            error_text = (
                error.get("message") or error.get("content") or json.dumps(error)
            )
        else:
            error_text = str(error) if error else ""
        block["results"] = [
            {
                "tool_call_id": tool_call_id,
                "content": run.get("final_text") or error_text or "",
                "subagent_id": subagent_id,
            }
        ]
    return block


def _final_text_from_blocks_for_parent(content_blocks: list[dict]) -> str:
    """Return only the synthesized trailing text from a subagent transcript.

    Parent-visible subagent updates do not need the full serialized transcript
    (especially not web_fetch bodies inside tool result attributes). Keep the
    hidden subagent chat as the source of truth for the full transcript; the
    parent transport only needs enough text for a final preview.
    """
    if not isinstance(content_blocks, list):
        return ""

    last_text_blocks: list[dict] = []
    for block in reversed(content_blocks):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            last_text_blocks.insert(0, block)
        elif btype == "tool_calls":
            break
        elif btype == "reasoning":
            continue

    return blocks_to_plain_text(last_text_blocks).strip() if last_text_blocks else ""


def _slim_tool_result_for_parent(result: Any) -> dict:
    """Strip heavy tool result bodies before forwarding inner subagent state.

    A subagent's parent-card UI only needs to know that an inner tool call
    finished. The full web_search/web_fetch/tool bodies remain persisted in the
    hidden subagent chat row and are available when the user opens that chat.
    Sending them through chat:subagent:update is pure transport/JSON/socket CPU.
    """
    if not isinstance(result, dict):
        return {"tool_call_id": "", "result_truncated": True}

    slim: dict = {"tool_call_id": result.get("tool_call_id", "")}
    if result.get("subagent_id"):
        slim["subagent_id"] = result.get("subagent_id")
    # Preserve the small error/notice metadata (not the heavy body) so a
    # subagent's failed inner tool call still surfaces in the parent card.
    if result.get("error"):
        slim["error"] = True
    if result.get("error_reason"):
        slim["error_reason"] = result.get("error_reason")
    if result.get("notice"):
        slim["notice"] = result.get("notice")
    if result.get("content"):
        slim["result_truncated"] = True
    if result.get("files"):
        slim["files_truncated"] = True
    if result.get("embeds"):
        slim["embeds_truncated"] = True
    return slim


def _slim_content_blocks_for_parent(content_blocks: list[dict]) -> list[dict]:
    if not isinstance(content_blocks, list):
        return []

    slim_blocks: list[dict] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            slim_blocks.append(block)
            continue

        slim_block = dict(block)
        if block.get("type") == "tool_calls" and isinstance(block.get("results"), list):
            slim_block["results"] = [
                _slim_tool_result_for_parent(result)
                for result in block.get("results") or []
            ]
        slim_blocks.append(slim_block)
    return slim_blocks


async def _slim_inner_event_for_parent(inner_event: dict) -> dict:
    """Remove inner subagent tool bodies from parent-forwarded events only."""
    if not isinstance(inner_event, dict):
        return inner_event

    etype = inner_event.get("type")
    if etype == "tool_call:result":
        data = dict(inner_event.get("data") or {})
        if data.get("result"):
            data["result_truncated"] = True
        if data.get("files"):
            data["files_truncated"] = True
        if data.get("embeds"):
            data["embeds_truncated"] = True
        data["result"] = ""
        data.pop("files", None)
        data.pop("embeds", None)
        return {**inner_event, "data": data}

    if etype == "chat:completion":
        data = dict(inner_event.get("data") or {})
        content_blocks = data.get("content_blocks")
        if isinstance(content_blocks, list):
            data["content_blocks"] = _slim_content_blocks_for_parent(content_blocks)
            if "content" in data:
                data["content"] = (
                    _final_text_from_blocks_for_parent(content_blocks)
                    if data.get("done") is True
                    else ""
                )
        return {**inner_event, "data": data}

    return inner_event


def _apply_subagent_placeholder_patch(
    message: dict, run: dict, update_data: dict, *, allow_append: bool = True
) -> None:
    """Patch the parent assistant message's placeholder for one subagent run.

    Sync, in-memory version of the old ``_sync_parent_subagent_placeholder``.
    It operates on the message dict the atomic mutator ALREADY read under the
    per-message write lock, so it patches the LATEST ``content_blocks`` rather
    than a stale snapshot — closing the race where a sibling's placeholder write
    reverted parent text/blocks that were appended after the sibling's read.

    Any change is written into ``update_data`` (top-level ``content_blocks`` /
    ``content`` keys) for the caller's single merged write. See
    ``_upsert_subagent_run`` for the orchestration.

    User-initiated reruns pass ``allow_append=False`` — the parent message
    already has canonical content_blocks, so appending a synthetic tool_calls
    block would look like the parent model continued.
    """
    tool_call_id = str(
        run.get("tool_call_id")
        or run.get("entry_key")
        or run.get("subagent_id")
        or run.get("chat_id")
        or ""
    )
    if not tool_call_id:
        return

    placeholder_block = _subagent_placeholder_block(run)
    existing_blocks = message.get("content_blocks")
    if isinstance(existing_blocks, list):
        blocks = list(existing_blocks)
        found = False
        blocks_changed = False
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_calls":
                continue
            calls = (
                block.get("content")
                if isinstance(block.get("content"), list)
                else []
            )
            results = (
                block.get("results")
                if isinstance(block.get("results"), list)
                else []
            )
            matched_tool_call_id = ""
            for call in calls:
                if isinstance(call, dict) and _tool_call_id(call) == tool_call_id:
                    matched_tool_call_id = tool_call_id
                    break

            # Old entries can be missing tool_call_id. In that case the
            # safety validator can still find the parent block by the saved
            # result's subagent_id; mirror that fallback here so a redo can
            # update the canonical result instead of appending a duplicate.
            if not matched_tool_call_id:
                wanted_subagent_id = str(
                    run.get("subagent_id") or run.get("chat_id") or ""
                )
                result_call_ids = {
                    str(r.get("tool_call_id") or "")
                    for r in results
                    if isinstance(r, dict)
                    and wanted_subagent_id
                    and r.get("subagent_id") == wanted_subagent_id
                    and r.get("tool_call_id")
                }
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    call_id = _tool_call_id(call)
                    if (
                        call_id
                        and call_id in result_call_ids
                        and _subagent_tool_name(call) in _SUBAGENT_TOOL_NAMES
                    ):
                        matched_tool_call_id = call_id
                        break

            if not matched_tool_call_id:
                continue
            found = True
            # Patch the result/subagent_id into an already-present tool_calls
            # block. This closes the gap where the call was saved while
            # running, but the completion result was persisted via
            # subagent_runs before middleware's final content_blocks save.
            if _subagent_run_is_terminal(run):
                result = dict(placeholder_block.get("results", [{}])[0])
                result["tool_call_id"] = matched_tool_call_id
                replaced = False
                for idx, existing_result in enumerate(results):
                    if (
                        isinstance(existing_result, dict)
                        and existing_result.get("tool_call_id") == matched_tool_call_id
                    ):
                        merged_result = {**existing_result, **result}
                        # A successful rerun/adoption replaces the old terminal
                        # result, not just its text. Drop error-only decorations
                        # left by the failed result or the parent UI/model can
                        # continue treating a real replacement answer as an error.
                        if (
                            run.get("status") == "done"
                            and isinstance(run.get("final_text"), str)
                            and run.get("final_text", "").strip()
                        ):
                            for stale_key in ("error", "error_reason"):
                                merged_result.pop(stale_key, None)
                        if merged_result != existing_result:
                            results[idx] = merged_result
                            blocks_changed = True
                        replaced = True
                        break
                if not replaced:
                    results.append(result)
                    blocks_changed = True
                if blocks_changed:
                    block["results"] = results
            else:
                # Non-terminal: a rerun just flipped this run back to
                # "running". Drop any stale result for this tool call from
                # the canonical block so EVERY reader (reload re-seeding in
                # Chat.svelte, a second browser tab) sees a consistent
                # running-with-no-result state instead of the old answer.
                kept_results = [
                    r
                    for r in results
                    if not (
                        isinstance(r, dict)
                        and r.get("tool_call_id") == matched_tool_call_id
                    )
                ]
                if len(kept_results) != len(results):
                    block["results"] = kept_results
                    blocks_changed = True
            break
        if not found and allow_append:
            blocks.append(placeholder_block)
            blocks_changed = True
        if blocks_changed:
            update_data["content_blocks"] = blocks
    else:
        # If the message has no structured blocks yet (the common reload-
        # while-running gap), install a one-block placeholder. Also do it
        # when the legacy content is itself just/mostly a subagent details
        # placeholder; ResponseMessage prefers content_blocks, so this fixes
        # stale done=false/id="" HTML after reload.
        existing_content_for_check = (
            message.get("content")
            if isinstance(message.get("content"), str)
            else ""
        )
        content_without_subagent_details = re.sub(
            r'<details\b[^>]*type="subagent_launch"[^>]*>[\s\S]*?</details>',
            "",
            existing_content_for_check,
            flags=re.I,
        ).strip()
        if not existing_content_for_check.strip() or not content_without_subagent_details:
            update_data["content_blocks"] = [placeholder_block]

    existing_content = (
        message.get("content") if isinstance(message.get("content"), str) else ""
    )
    if allow_append and tool_call_id not in existing_content:
        placeholder_html = _subagent_placeholder_html(run)
        update_data["content"] = (
            f"{existing_content.rstrip()}\n{placeholder_html}".strip()
        )


async def _upsert_subagent_run(
    parent_chat_id: str,
    parent_message_id: str,
    subagent_id: str,
    patch: dict,
    *,
    sync_placeholder: bool = True,
    allow_placeholder_append: bool = True,
    reserve: Optional[dict] = None,
    cas_block_if_running: bool = False,
    expected_rerun_id: Optional[str] = None,
    require_running: bool = False,
    guard_parent_unconsumed: bool = False,
    require_parent_current: bool = False,
    require_parent_done: bool = False,
    exclusive_running_subagent_id: Optional[str] = None,
    touch_chat: bool = False,
) -> Optional[dict]:
    """Merge ``patch`` into ``parent_message.subagent_runs[subagent_id]``,
    preserving other keys and sibling subagents, and (when ``sync_placeholder``)
    keep the parent message's placeholder block/content in sync — all in ONE
    atomic, per-message-serialized read-modify-write.

    Concurrency: parallel subagent fan-out branches all write to the SAME parent
    assistant message. ``Chats.update_message_fields_atomic`` runs this merge
    under a per-(chat, message) async lock and re-reads the live map inside that
    lock, so a sibling can never clobber another's entry, timestamps, or the
    parent's freshly-streamed ``content_blocks``. Returns the merged run dict
    (so the launch path can read the atomically-assigned ``num``/``name``), or
    ``None`` on skip/failure.

    ``reserve`` (launch only): ``{"desired_name": str, "other_runs": dict}`` —
    when this call creates a BRAND-NEW entry, assign ``num`` + a
    collision-disambiguated ``name`` from the fresh run map under the lock so
    concurrent launches don't all land on the same number/name. Idempotent —
    safe to call multiple times. Skips silently for missing ids / local chats.
    """
    if not parent_chat_id or not parent_message_id or not subagent_id:
        log.warning(
            "_upsert_subagent_run SKIP: missing id(s) — "
            f"chat={parent_chat_id!r} msg={parent_message_id!r} sa={subagent_id!r}"
        )
        return None
    if parent_chat_id.startswith("local:"):
        return None

    holder: dict = {}

    def _atomic_precondition(chat_row, existing: dict) -> bool:
        """Revalidate a user-initiated result replacement under DB row locks."""
        try:
            chat_data = (
                chat_row.chat
                if chat_row is not None and isinstance(chat_row.chat, dict)
                else {}
            )
            history = (
                chat_data.get("history")
                if isinstance(chat_data.get("history"), dict)
                else {}
            )
            if require_parent_current and str(history.get("currentId") or "") != str(
                parent_message_id
            ):
                _rerun_blocked(
                    "Cannot redo subagent: the parent chat has already moved past "
                    "this tool result.",
                    code="subagent_parent_moved_on",
                )
            if require_parent_done and existing.get("done") is not True:
                _rerun_blocked(
                    "Stop the main agent before changing this subagent result.",
                    code="subagent_parent_running",
                )
            runs = existing.get("subagent_runs")
            live_run = (
                runs.get(subagent_id)
                if isinstance(runs, dict) and isinstance(runs.get(subagent_id), dict)
                else None
            )
            if live_run is None:
                _rerun_blocked(
                    "Cannot change subagent safely: its parent run no longer exists.",
                    code="subagent_run_missing",
                )
            _validate_parent_message_subagent_result_unconsumed(existing, live_run)
            return True
        except SubagentRerunBlockedError as error:
            holder["precondition_error"] = error
            return False

    def _mutator(existing: dict) -> Optional[dict]:
        existing_runs = existing.get("subagent_runs")
        existing_runs = dict(existing_runs) if isinstance(existing_runs, dict) else {}
        prior = existing_runs.get(subagent_id)
        prior = prior if isinstance(prior, dict) else {}

        if expected_rerun_id is not None and str(prior.get("rerun_id") or "") != str(
            expected_rerun_id
        ):
            # Detached-rerun cleanup is deliberately claim-scoped. An old task's
            # finally block must never terminalize a newer rerun that reclaimed
            # the same parent entry after the old one finished.
            holder["claim_mismatch"] = True
            return None

        if require_running and (
            prior.get("status") != "running" or prior.get("ended_at") is not None
        ):
            holder["not_running"] = True
            return None

        if (
            cas_block_if_running
            and prior.get("status") == "running"
            and not prior.get("ended_at")
            and not (
                patch.get("rerun_id")
                and str(prior.get("rerun_id") or "")
                == str(patch.get("rerun_id"))
            )
        ):
            # Atomic compare-and-set guard (rerun "claim"): refuse to overwrite an
            # entry that is ACTIVELY running. Runs under the per-(chat, message)
            # write lock with a fresh read, so two concurrent reruns of the same
            # entry serialize here — the first flips terminal->running, the second
            # sees 'running' and is blocked BEFORE it can wipe the shared hidden
            # chat. Signal the caller to raise; perform no write.
            holder["cas_blocked"] = True
            return None

        effective_patch = dict(patch)
        # Atomic slot reservation: only for a brand-new launch entry. Computed
        # from the FRESH run map under the write lock so parallel launches each
        # see prior siblings' reservations and never collide.
        if reserve is not None and not prior:
            other_runs = reserve.get("other_runs") or {}
            combined = {**other_runs, **existing_runs}
            final_name, was_renamed = _disambiguate_name(
                reserve.get("desired_name") or "", combined
            )
            launch_count = sum(
                1
                for r in combined.values()
                if isinstance(r, dict) and not r.get("continuation")
            )
            effective_patch["name"] = final_name
            effective_patch["num"] = launch_count + 1
            holder["was_renamed"] = was_renamed

        merged_run = {**prior, **effective_patch}
        existing_runs[subagent_id] = merged_run
        holder["merged"] = merged_run

        update_data: dict = {"subagent_runs": existing_runs}
        if sync_placeholder:
            _apply_subagent_placeholder_patch(
                existing,
                merged_run,
                update_data,
                allow_append=allow_placeholder_append,
            )
        return update_data

    def _cross_message_precondition(
        selected_messages: dict[str, dict],
    ) -> bool:
        """Enforce one active turn per hidden subagent under the chat lock."""
        if not exclusive_running_subagent_id:
            return True
        # A missing target means selected-branch reconstruction was incomplete
        # or the caller targeted an off-branch message. Fail closed.
        if parent_message_id not in selected_messages:
            holder["exclusive_conflict"] = True
            return False
        for message_id, message in selected_messages.items():
            runs = (
                message.get("subagent_runs")
                if isinstance(message, dict)
                else None
            )
            if not isinstance(runs, dict):
                continue
            for entry_key, run in runs.items():
                if (
                    message_id == parent_message_id
                    and str(entry_key) == str(subagent_id)
                ):
                    continue
                if not isinstance(run, dict):
                    continue
                run_subagent_id = run.get("subagent_id") or run.get("chat_id")
                if str(run_subagent_id or "") != str(
                    exclusive_running_subagent_id
                ):
                    continue
                if run.get("status") == "running" and not run.get("ended_at"):
                    holder["exclusive_conflict"] = True
                    return False
        return True

    # Prefer the targeted single-run write: it serializes ONLY the changed
    # subagent_runs[sid] key via jsonb_set instead of re-dumping the whole
    # N-entry map, removing the O(N²) meta write-amplification on big fan-outs.
    # Retry one no-commit/exception result in a fresh DB call. Every patch is
    # merge-idempotent; the rerun CAS explicitly accepts its own rerun_id, which
    # also covers the uncertain "commit succeeded but acknowledgement failed"
    # boundary without letting another generation steal the claim.
    write_result = None
    for write_attempt in (1, 2):
        try:
            targeted = getattr(Chats, "update_message_subagent_run_atomic", None)
            if targeted is not None:
                writer_kwargs = {"touch_chat": True} if touch_chat else {}
                if exclusive_running_subagent_id:
                    writer_kwargs["cross_message_precondition"] = (
                        _cross_message_precondition
                    )
                if guard_parent_unconsumed:
                    write_result = await targeted(
                        parent_chat_id,
                        parent_message_id,
                        subagent_id,
                        _mutator,
                        precondition=_atomic_precondition,
                        **writer_kwargs,
                    )
                else:
                    write_result = await targeted(
                        parent_chat_id,
                        parent_message_id,
                        subagent_id,
                        _mutator,
                        **writer_kwargs,
                    )
            else:
                if guard_parent_unconsumed or exclusive_running_subagent_id:
                    raise RuntimeError(
                        "atomic subagent precondition is unavailable"
                    )
                write_result = await Chats.update_message_fields_atomic(
                    parent_chat_id, parent_message_id, _mutator
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning(
                "failed to upsert subagent_run %s on %s/%s (attempt %s/2): %s",
                subagent_id,
                parent_chat_id,
                parent_message_id,
                write_attempt,
                e,
            )
            write_result = None
        if write_result:
            break
        if (
            holder.get("cas_blocked")
            or holder.get("claim_mismatch")
            or holder.get("not_running")
            or holder.get("precondition_error")
            or holder.get("exclusive_conflict")
        ):
            break
        if write_attempt == 1:
            await asyncio.sleep(0)
    if holder.get("cas_blocked"):
        # CAS guard tripped (entry already running). Raise so the rerun caller
        # aborts before touching the hidden chat. Resolved from module globals at
        # call time (defined later in this module).
        raise SubagentRerunBlockedError(
            "This subagent turn is already running — wait for it to finish "
            "before re-running it.",
            code="subagent_already_running",
        )
    if holder.get("claim_mismatch") or holder.get("not_running"):
        return None
    if holder.get("exclusive_conflict"):
        raise SubagentRerunBlockedError(
            "This subagent already has another turn running — wait for it to "
            "finish before starting another one.",
            code="subagent_already_running",
        )
    if holder.get("precondition_error"):
        raise holder["precondition_error"]
    if not write_result:
        # The mutator may have populated ``holder`` before a DB commit failed or
        # the target message disappeared. Never report an in-memory merge as
        # durable when the atomic writer returned no committed patch.
        return None
    return holder.get("merged")


async def _emit_subagent_cancel(
    parent_event_emitter: Optional[Callable[[dict], Awaitable[None]]],
    subagent_meta: dict,
    *,
    user_id: Optional[str] = None,
    parent_chat_id: Optional[str] = None,
    parent_message_id: Optional[str] = None,
    force_fanout: bool = False,
) -> None:
    """Best-effort live cancellation update for a parent-visible subagent row.

    Under v2.1 we route through ``emit_to_primary`` so the envelope is delivered
    to the elected primary session only (and gets per-tick batched alongside
    other ``chat:subagent:update`` events). Under v1 we fall back to the
    direct parent_event_emitter fan-out so existing behavior is preserved.

    ``force_fanout`` (DETACHED reruns): route via ``emit_user_fanout`` to every one
    of the user's sessions instead of stream-scoped ``emit_to_primary``. A rerun's
    TERMINAL (cancel/error/blocked) would otherwise be suppressed for a backgrounded
    subscriber by the visibility gate (and a detached rerun has no parent finalizer
    to recover the card), stranding it on 'Researching…' — mirror the routing the
    rerun's PROGRESS events already use."""
    inner_event = {"type": "chat:tasks:cancel"}
    payload_data = {
        "type": "chat:subagent:update",
        "data": {
            **subagent_meta,
            "inner_event": inner_event,
        },
    }
    if (
        STREAM_PROTOCOL_VERSION == "v2.1"
        and user_id
    ):
        envelope = {
            "chat_id": parent_chat_id,
            "message_id": parent_message_id,
            "data": payload_data,
        }
        try:
            if force_fanout:
                await emit_user_fanout(user_id, envelope)
            else:
                await emit_to_primary(user_id, envelope)
            return
        except Exception as e:  # noqa: BLE001
            log.debug(f"subagent cancel emit_to_primary failed; falling back: {e}")
    if parent_event_emitter is None:
        return
    try:
        await parent_event_emitter(payload_data)
    except Exception as e:  # noqa: BLE001
        log.debug(f"subagent cancel emit failed: {e}")


async def _emit_subagent_terminal(
    parent_event_emitter: Optional[Callable[[dict], Awaitable[None]]],
    subagent_meta: dict,
    *,
    status: str,
    message: Optional[str] = None,
    user_id: Optional[str] = None,
    parent_chat_id: Optional[str] = None,
    parent_message_id: Optional[str] = None,
    force_fanout: bool = False,
) -> None:
    """Push a TERMINAL state to the parent live card for a failure/stop that did
    NOT originate from the inner pipeline's own error emission.

    The forwarding emitter only relays events the inner ``process_chat_response``
    actually emits. When a subagent dies via a path that emits no inner terminal
    event — the empty-final-text ``RuntimeError``, a ``SubagentTimeoutError``, a
    model-resolution crash, a blocked/failed rerun — the parent card's
    ``live:true`` store entry would otherwise spin "Researching…" forever until
    reload. This synthesizes the inner terminal event the client's
    ``mergeSubagentPendingIntoRun`` already knows how to fold in
    (``chat:message:error`` / ``chat:tasks:cancel``), so the card resolves live.
    Routing mirrors ``_emit_subagent_cancel``.
    """
    if status == "cancelled":
        inner_event: dict = {"type": "chat:tasks:cancel"}
    elif status == "error":
        inner_event = {
            "type": "chat:message:error",
            "data": {"error": message or "Subagent failed."},
        }
    elif status == "done":
        # The subagent actually FINISHED (e.g. a Stop landed in the done-write
        # window after final_text was produced — see C3). Resolve the live card to
        # 'done'; the client folds chat:done into a terminal done and then fetches
        # the subagent's persisted transcript for the body.
        inner_event = {"type": "chat:done", "data": {}}
    else:
        return
    payload_data = {
        "type": "chat:subagent:update",
        "data": {
            **subagent_meta,
            "inner_event": inner_event,
        },
    }
    if STREAM_PROTOCOL_VERSION == "v2.1" and user_id:
        envelope = {
            "chat_id": parent_chat_id,
            "message_id": parent_message_id,
            "data": payload_data,
        }
        try:
            if force_fanout:
                await emit_user_fanout(user_id, envelope)
            else:
                await emit_to_primary(user_id, envelope)
            return
        except Exception as e:  # noqa: BLE001
            log.debug(f"subagent terminal emit_to_primary failed; falling back: {e}")
    if parent_event_emitter is None:
        return
    try:
        await parent_event_emitter(payload_data)
    except Exception as e:  # noqa: BLE001
        log.debug(f"subagent terminal emit failed: {e}")


async def _build_forwarding_emitter(
    subagent_socket_info: dict,
    parent_event_emitter: Callable[[dict], Awaitable[None]],
    subagent_meta: dict,
    *,
    parent_chat_id: Optional[str] = None,
    parent_message_id: Optional[str] = None,
    force_fanout: bool = False,
) -> Callable[[dict], Awaitable[None]]:
    """Wrap the inner event_emitter and throttle parent-facing live updates.

    The hidden subagent chat still gets its own non-completion events through
    the normal emitter for persistence. The parent UI, however, only needs a
    live-ish latest content snapshot. With dozens of research subagents running,
    raw forwarding turns every inner chunk into a socket event + store
    invalidation. We coalesce non-terminal parent updates to 2Hz per subagent
    and always flush terminal/error/cancel events immediately.

    ``force_fanout`` makes parent-facing updates bypass the v2.1 stream-scoped
    ``emit_to_primary`` path and fan out to every one of the user's sessions
    directly. Reruns set this because they run detached from any parent stream,
    where stream-room/primary-election routing is unreliable (see _emit_parent).
    """
    base_emitter = get_event_emitter(subagent_socket_info)

    FORWARDED_TYPES = {
        "chat:completion",
        "chat:message:error",
        "chat:tasks:cancel",
        "status",
    }
    # These are high-volume/transient for research-heavy subagents. The hidden
    # subagent chat's final assistant row is persisted by the normal middleware
    # save path; persisting every status/source/citation event here creates DB
    # and socket fanout pressure with 50 concurrent workers.
    #
    # ``browser:frame`` is the live browser side-panel feed. When a subagent
    # drives the shared browser it must surface on the PARENT tab, not the
    # hidden subagent chat (which no tab is viewing). We skip the base emitter
    # for it and re-route it top-level to ``parent_event_emitter`` below, so it
    # lands with the parent's chat_id + message_id and reaches the parent UI's
    # ``browser:frame`` handler.
    SKIP_BASE_EMIT_TYPES = {
        "chat:completion",
        "chat:message:delta",
        "status",
        "source",
        "citation",
        "browser:frame",
    }
    FORWARD_FLUSH_INTERVAL_SECONDS = 0.5

    v21_enabled = STREAM_PROTOCOL_VERSION == "v2.1"
    inner_message_id = subagent_socket_info.get("message_id") if isinstance(subagent_socket_info, dict) else None
    # User/parent identifiers for the primary-only emit path (v2.1). The
    # parent_event_emitter path remains as a fallback so any failure in the
    # primary emit (or v1 mode) still reaches sibling sessions via fan-out.
    user_id_for_primary = (
        subagent_socket_info.get("user_id") if isinstance(subagent_socket_info, dict) else None
    )
    parent_chat_id_for_primary = parent_chat_id
    parent_message_id_for_primary = parent_message_id or (
        subagent_meta.get("parent_message_id") if isinstance(subagent_meta, dict) else None
    )
    session_id_for_primary = (
        subagent_socket_info.get("session_id") if isinstance(subagent_socket_info, dict) else None
    )
    # Per-subagent mirror — independent of the parent's v2.1 mirror. Tracks the
    # slim (results-stripped) block shape so we can diff fresh content_blocks
    # into compact chat:delta ops, and remembers which tool_call_ids have
    # already been shipped as tool_call:result inner events.
    v21_mirror: dict = {"blocks": [], "tool_results_sent": set()}
    if v21_enabled and inner_message_id:
        stream_version_init(
            inner_message_id,
            chat_id=subagent_socket_info.get("chat_id") if isinstance(subagent_socket_info, dict) else None,
            user_id=user_id_for_primary,
            session_id=session_id_for_primary,
            content_blocks=[],
        )

    lock = asyncio.Lock()
    latest_completion_event: Optional[dict] = None
    latest_status_event: Optional[dict] = None
    flush_task: Optional[asyncio.Task] = None
    # Once a terminal event has been forwarded, no further parent updates may be
    # queued/flushed (P3_1): a late non-terminal event (e.g. an outlet/status filter
    # event during the parent's own outlet phase) would otherwise schedule a fresh
    # _delayed_flush that wakes ~0.5s later and re-emits a stale status, which B3
    # re-promotion folds into a false 'running' — a card that never resolves.
    terminal_seen = False

    def _is_terminal_event(event: dict) -> bool:
        etype = event.get("type") if isinstance(event, dict) else None
        data = event.get("data") if isinstance(event, dict) else None
        return bool(
            (etype == "chat:completion" and isinstance(data, dict) and data.get("done") is True)
            or etype in {"chat:message:error", "chat:tasks:cancel"}
        )

    async def _emit_parent(inner_event: dict) -> None:
        parent_inner_event = await _slim_inner_event_for_parent(inner_event)
        payload_data = {
            "type": "chat:subagent:update",
            "data": {
                **subagent_meta,
                "inner_event": parent_inner_event,
            },
        }
        # Under v2.1 ship via emit_to_primary so the envelope goes to the
        # elected primary session only (and joins the per-tick batch with
        # chat:delta / tool_call:result emits). Sibling tabs receive the
        # event via the primary tab's BroadcastChannel relay. Under v1 we
        # use the original fan-out emitter so every session keeps getting
        # its own copy directly from the server.
        #
        # EXCEPTION — reruns (force_fanout): a redo runs as a DETACHED
        # background task with no active parent generation. The v2.1
        # emit_to_primary path is stream-scoped: it targets stream-room
        # subscribers + the elected primary session. During a real parent
        # stream the visible tab is provably in that room, but for a detached
        # rerun that routing depends on fragile cross-tab state (primary
        # election + stream-room membership) and can deliver the live updates
        # to the wrong session — leaving the clicked card stuck on "starting
        # up". Fan out directly to every one of the user's sessions instead;
        # the per-token-fanout cost emit_to_primary exists to avoid is
        # irrelevant for a single rerun.
        if v21_enabled and user_id_for_primary and not force_fanout:
            envelope = {
                "chat_id": parent_chat_id_for_primary,
                "message_id": parent_message_id_for_primary,
                "session_id": session_id_for_primary,
                "data": payload_data,
            }
            try:
                await emit_to_primary(user_id_for_primary, envelope)
                return
            except Exception as e:  # noqa: BLE001
                log.debug(
                    f"subagent emit_to_primary failed; falling back to fan-out: {e}"
                )
        # Detached rerun (force_fanout): deliver to EVERY one of the user's
        # sessions directly. The stream-scoped emit_to_primary/parent_event_emitter
        # paths target the stream room + elected primary, which a detached rerun
        # (no active parent stream) can't rely on — so its live updates would miss
        # the user's background / non-subscriber tabs. emit_user_fanout bypasses
        # that routing. Only when we have no user id (or v1) do we fall through to
        # the original fan-out emitter below.
        if force_fanout and user_id_for_primary:
            envelope = {
                "chat_id": parent_chat_id_for_primary,
                "message_id": parent_message_id_for_primary,
                "session_id": session_id_for_primary,
                "data": payload_data,
            }
            try:
                await emit_user_fanout(user_id_for_primary, envelope)
                return
            except Exception as e:  # noqa: BLE001
                log.debug(
                    f"subagent emit_user_fanout failed; falling back to fan-out: {e}"
                )
        try:
            await parent_event_emitter(payload_data)
        except Exception as e:  # noqa: BLE001
            log.debug(f"forwarding to parent UI failed: {e}")

    async def _emit_parent_raw(inner_event: dict) -> None:
        # Adapter passed to `_emit_delta_for_blocks` as `raw_emit`. The
        # middleware helper already builds the {type: chat:delta, data: ...}
        # envelope; we just need to wrap it in our subagent envelope so the
        # parent UI's chat:subagent:update handler routes it correctly.
        await _emit_parent(inner_event)

    async def _emit_v21_deltas_for_completion(completion_event: dict) -> None:
        """Translate a coalesced `chat:completion` (with full content_blocks)
        into compact `chat:delta` inner events, plus separate `tool_call:result`
        inner events for any newly-finished tool calls. Mirrors B9's wrapper
        logic but emits via the subagent envelope instead of `emit_to_primary`.
        """
        if not inner_message_id:
            await _emit_parent(completion_event)
            return
        data = completion_event.get("data") or {}
        content_blocks = data.get("content_blocks")
        # Tool results: emit each NEW result as its own tool_call:result inner
        # event so the per-chunk diff (which strips result bodies) never
        # re-ships the heavy payload.
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_calls":
                    continue
                for r in block.get("results") or []:
                    if not isinstance(r, dict):
                        continue
                    tc_id = r.get("tool_call_id")
                    if not tc_id or tc_id in v21_mirror["tool_results_sent"]:
                        continue
                    v21_mirror["tool_results_sent"].add(tc_id)
                    payload = {
                        "message_id": inner_message_id,
                        "tool_call_id": tc_id,
                        "result": "",
                    }
                    if r.get("subagent_id"):
                        payload["subagent_id"] = r["subagent_id"]
                    # Error/notice are tiny UI metadata (not heavy bodies), so
                    # forward them so a subagent's failed inner tool call still
                    # shows the error row inside the subagent card.
                    if r.get("error"):
                        payload["error"] = True
                    if r.get("error_reason"):
                        payload["error_reason"] = r["error_reason"]
                    if r.get("notice"):
                        payload["notice"] = r["notice"]
                    if r.get("content"):
                        payload["result_truncated"] = True
                    if r.get("files"):
                        payload["files_truncated"] = True
                    if r.get("embeds"):
                        payload["embeds_truncated"] = True
                    await _emit_parent({"type": "tool_call:result", "data": payload})

        if isinstance(content_blocks, list):
            # Snapshot-version coherence (same invariant as the parent's
            # _wrap_event_emitter_v21 content flush): build the versioned delta
            # payloads FIRST (version bumps are synchronous at build time), then
            # write content + snapshot_version in ONE patch, then emit. A viewer
            # of the subagent's own inner chat who fetches /snapshot mid-flush
            # must never get new content advertised at an old version — the
            # replayed deltas would duplicate text already in the snapshot.
            awaitables = _emit_delta_for_blocks(
                _emit_parent_raw, inner_message_id, v21_mirror, content_blocks
            )
            set_stream_state(
                inner_message_id,
                {
                    "content_blocks": _strip_tool_results(content_blocks),
                    "status": "done" if data.get("done") else "in_progress",
                    **({"usage": data["usage"]} if data.get("usage") else {}),
                    **({"sources": data["sources"]} if data.get("sources") else {}),
                    **(
                        {"selected_model_id": data["selected_model_id"]}
                        if data.get("selected_model_id")
                        else {}
                    ),
                    "snapshot_version": stream_version_get(inner_message_id),
                },
            )
            stream_version_flush(inner_message_id)
            # Preserve order for dependent block_open/text_append ops.
            for awaitable in awaitables:
                await awaitable

        # Selected model / sources / usage piggybacks on the final completion
        # event. Mirror B9: emit as separate chat:delta ops.
        if data.get("selected_model_id"):
            version = stream_version_incr(inner_message_id)
            await _emit_parent(
                {
                    "type": "chat:delta",
                    "data": {
                        "message_id": inner_message_id,
                        "version": version,
                        "op": "selected_model_id",
                        "payload": {"model_id": data["selected_model_id"]},
                    },
                }
            )
        if data.get("sources"):
            version = stream_version_incr(inner_message_id)
            await _emit_parent(
                {
                    "type": "chat:delta",
                    "data": {
                        "message_id": inner_message_id,
                        "version": version,
                        "op": "sources",
                        "payload": {"sources": data["sources"]},
                    },
                }
            )
        if data.get("done") is True:
            # Terminal: ship a chat:done so the parent UI can finalize the
            # subagent's mirror without waiting on a snapshot. usage piggybacks.
            done_payload: dict = {
                "message_id": inner_message_id,
                "version": stream_version_incr(inner_message_id),
            }
            if data.get("usage"):
                done_payload["usage"] = data["usage"]
            await _emit_parent({"type": "chat:done", "data": done_payload})

    async def _flush_pending() -> None:
        nonlocal latest_completion_event, latest_status_event, flush_task
        async with lock:
            status_event = latest_status_event
            completion_event = latest_completion_event
            latest_status_event = None
            latest_completion_event = None
            flush_task = None

        # Preserve useful ordering: latest status first, then latest content.
        if status_event is not None:
            await _emit_parent(status_event)
        if completion_event is not None:
            if v21_enabled:
                await _emit_v21_deltas_for_completion(completion_event)
            else:
                await _emit_parent(completion_event)

    async def _delayed_flush() -> None:
        await asyncio.sleep(FORWARD_FLUSH_INTERVAL_SECONDS)
        await _flush_pending()

    async def _schedule_flush() -> None:
        nonlocal flush_task
        async with lock:
            if terminal_seen:
                return
            if flush_task is None or flush_task.done():
                flush_task = asyncio.create_task(_delayed_flush())

    async def _cancel_pending_flush() -> None:
        """Cancel a sleeping _delayed_flush so it can never wake and emit after the
        subagent has gone terminal / the inner run has exited. Called from the
        terminal branch and from _run_inner_chat's finally (exposed below)."""
        nonlocal flush_task
        async with lock:
            pending = flush_task
            flush_task = None
        if pending is not None and not pending.done():
            pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _queue_parent_event(event: dict) -> None:
        nonlocal latest_completion_event, latest_status_event
        etype = event.get("type") if isinstance(event, dict) else None

        async with lock:
            if etype == "chat:completion":
                latest_completion_event = event
            elif etype == "status":
                latest_status_event = event

        await _schedule_flush()

    async def forwarding_emitter(event: dict) -> None:
        nonlocal terminal_seen
        etype = event.get("type") if isinstance(event, dict) else None

        # Persist/emit only non-noisy events to the hidden subagent chat scope.
        # The final assistant content is saved by middleware; status/source/citation
        # are expensive fanout/DB-write noise at high concurrency.
        if etype not in SKIP_BASE_EMIT_TYPES:
            try:
                await base_emitter(event)
            except Exception as e:  # noqa: BLE001
                log.debug(f"subagent base emitter raised: {e}")

        # Live browser side-panel feed: when the subagent drives the shared
        # browser, surface its frames on the PARENT tab. parent_event_emitter is
        # the parent's __event_emitter__ (get_event_emitter(parent_metadata)),
        # which stamps the envelope chat_id/message_id = parent's — so the
        # top-level browser:frame lands on the parent chat and reaches the
        # parent UI's browser:frame handler (its BrowserPanel picks the live
        # state regardless of which assistant message id owns it). Forwarded
        # raw, NOT wrapped in chat:subagent:update, because the handler listens
        # for a top-level browser:frame. Fire-and-forget; never block the run.
        if etype == "browser:frame":
            try:
                await parent_event_emitter(event)
            except Exception as e:  # noqa: BLE001
                log.debug(f"forwarding subagent browser:frame to parent UI failed: {e}")
            return

        if force_fanout and etype == "files":
            try:
                await parent_event_emitter(event)
            except Exception as e:  # noqa: BLE001
                log.debug(f"forwarding subagent files to parent UI failed: {e}")
            return

        if etype not in FORWARDED_TYPES:
            return

        # Already terminal: never forward another parent update (P3_1). A late
        # non-terminal event would re-arm the flush and re-emit stale status.
        if terminal_seen:
            return

        if _is_terminal_event(event):
            # Mark terminal under lock and cancel any sleeping flush so it can't
            # wake post-terminal and re-emit. Then ship the queued snapshot and the
            # terminal event (final state immediate; latest content preserved).
            async with lock:
                terminal_seen = True
            await _cancel_pending_flush()
            await _flush_pending()
            if v21_enabled and etype == "chat:completion":
                # Translate terminal completion into deltas + chat:done.
                await _emit_v21_deltas_for_completion(event)
            else:
                # Error/cancel pass through as-is; the parent UI handles them
                # via the existing terminal-event branch (mergeSubagentPendingIntoRun).
                await _emit_parent(event)
            return

        await _queue_parent_event(event)

    forwarding_emitter.cancel_pending = _cancel_pending_flush  # type: ignore[attr-defined]
    return forwarding_emitter


async def _append_history_for_inner_run(
    subagent_chat_id: str,
    prompt: str,
    user_msg_id: str,
    assistant_msg_id: str,
    model_id: str,
    history_transition: Optional[dict] = None,
) -> None:
    """Prepare a hidden turn through the guarded row-level DB primitive.

    ``history_transition`` identifies the exact hidden leaf this caller saw and
    may request an atomic reset or one-pair revert before the replacement pair
    is inserted. This is deliberately one transaction: migrated chats must
    never pass through ``update_chat_by_id``'s delete-all/resync path here.
    """
    transition = dict(history_transition or {})
    if "expected_current_id" not in transition:
        # Compatibility fallback for direct/internal callers. Production
        # launch/continue/rerun paths always pass an explicit expectation.
        chat = await Chats.get_chat_by_id(subagent_chat_id)
        if not chat:
            raise RuntimeError(f"subagent chat {subagent_chat_id} not found")
        transition["expected_current_id"] = (
            ((chat.chat or {}).get("history") or {}).get("currentId")
        )

    now = int(time.time())

    user_message = {
        "id": user_msg_id,
        "childrenIds": [assistant_msg_id],
        "role": "user",
        "content": prompt,
        "timestamp": now,
    }
    assistant_message = {
        "id": assistant_msg_id,
        "parentId": user_msg_id,
        "childrenIds": [],
        "role": "assistant",
        "content": "",
        "model": model_id,
        "timestamp": now,
    }

    prepare_kwargs = {
        "expected_current_id": transition.get("expected_current_id"),
        "reset_history": bool(transition.get("reset_history")),
        "revert_user_message_id": transition.get("revert_user_message_id"),
        "revert_assistant_message_id": transition.get(
            "revert_assistant_message_id"
        ),
    }
    if transition.get("set_model_id"):
        prepare_kwargs.update(
            {
                "expected_model_id": transition.get("expected_model_id"),
                "set_model_id": transition.get("set_model_id"),
            }
        )

    result = await Chats.prepare_subagent_turn_atomic(
        subagent_chat_id,
        user_message,
        assistant_message,
        **prepare_kwargs,
    )
    if not result:
        raise RuntimeError(
            f"failed to prepare subagent history for {subagent_chat_id}"
        )


async def _load_inner_api_messages(
    subagent_chat_id: str,
    up_to_message_id: str,
    system_prompt: str,
    model_id: Optional[str] = None,
) -> list[dict]:
    """Build the API-shaped message list for the inner run.

    System message first (our composed subagent prompt), then the subagent's
    chat history converted via ``blocks_to_api_messages``. The trailing blank
    assistant message is dropped automatically by ``blocks_to_api_messages``
    (it has no content / tool_calls / reasoning to emit).

    ``model_id`` (the subagent's own model) is forwarded to blocks_to_api_messages
    so the Gemini `$ref` sanitization (sanitize_gemini_tool_result) applies here too
    — the resulting flat messages get re-embedded as this subagent turn's outbound
    history, and re-run again through routers/openai.py's own blocks_to_api_messages
    call, but sanitizing at the source keeps the subagent's own history convert step
    faithful to what will actually be sent, and covers the case where downstream a
    caller only round-trips the already-flat messages without repeating model_id."""
    chat = await Chats.get_chat_by_id(subagent_chat_id)
    if not chat:
        raise RuntimeError(f"subagent chat {subagent_chat_id} not found")
    messages_map = ((chat.chat or {}).get("history") or {}).get("messages") or {}
    ordered = get_message_list(messages_map, up_to_message_id) or []
    api_history = blocks_to_api_messages(ordered, model_id=model_id)
    api_messages: list[dict] = []
    if system_prompt:
        api_messages.append({"role": "system", "content": system_prompt})
    api_messages.extend(api_history)
    return api_messages


async def _extract_final_text(subagent_chat_id: str, assistant_msg_id: str) -> str:
    """Read the final assistant text from the subagent chat row.

    Prefers ``blocks_to_plain_text(content_blocks)`` (canonical, clean) over
    the legacy HTML ``content`` projection. Returns empty string if nothing
    is there — caller decides how to surface that."""
    msg = (
        await Chats.get_message_by_id_and_message_id(subagent_chat_id, assistant_msg_id)
        or {}
    )
    blocks = msg.get("content_blocks")
    if isinstance(blocks, list) and blocks:
        # Only render the trailing TEXT blocks — anything before the final
        # text block is reasoning/tool_calls that we don't want
        # to repeat back into the parent's context. The subagent's job was
        # to synthesize; the parent only needs that synthesis.
        last_text_blocks: list[dict] = []
        for block in reversed(blocks):
            btype = block.get("type") if isinstance(block, dict) else None
            if btype == "text":
                last_text_blocks.insert(0, block)
            elif btype == "tool_calls":
                break
            elif btype == "reasoning":
                continue
        if last_text_blocks:
            text = blocks_to_plain_text(last_text_blocks).strip()
            if text:
                return text
        # No trailing text after the last tool_calls. The subagent ended its turn
        # on a tool_calls/reasoning block rather than a final synthesis (e.g. the
        # model "thought" and then stopped). Before giving up, fall back to the
        # LAST non-empty text block anywhere in the transcript — partial findings
        # the subagent did write out are far more useful to the parent than an
        # empty/error result, and the empty-round retry in the agentic loop is the
        # primary defense against a genuinely text-less run.
        for block in reversed(blocks):
            if isinstance(block, dict) and block.get("type") == "text":
                text = blocks_to_plain_text([block]).strip()
                if text:
                    return text
        # No text anywhere, but the subagent DID work (tool calls / reasoning).
        # Rather than return "" — which makes the caller wipe the whole hidden
        # transcript and re-run from scratch (discarding that work and
        # double-charging) — surface the trailing reasoning as the synthesis.
        # The model's analysis is far more useful to the parent than an empty/
        # error result, and it preserves the research instead of throwing it away.
        last_reasoning: list[dict] = []
        for block in reversed(blocks):
            btype = block.get("type") if isinstance(block, dict) else None
            if btype == "reasoning":
                last_reasoning.insert(0, block)
            elif btype == "tool_calls":
                break
            elif btype == "text":
                continue
        if last_reasoning:
            text = blocks_to_plain_text(last_reasoning).strip()
            if text:
                return text
        # Truly nothing usable — return empty so the caller surfaces it.
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


# ---------------------------------------------------------------------------
# Inner-chat orchestrator (called by both launch and continue)
# ---------------------------------------------------------------------------


async def _run_inner_chat(
    *,
    request: Request,
    user,
    subagent_model: dict,
    subagent_chat_id: str,
    prompt: str,
    user_msg_id: str,
    assistant_msg_id: str,
    parent_metadata: dict,
    parent_event_emitter: Callable,
    parent_event_call: Optional[Callable],
    subagent_meta: dict,
    chat_params: dict,
    history_transition: Optional[dict] = None,
    history_prepared_callback: Optional[Callable[[], Awaitable[None]]] = None,
    force_fanout: bool = False,
) -> str:
    """Drive one inner subagent turn end-to-end. Returns the final assistant
    text. Raises on unrecoverable error (caller decides retry vs. surface).

    ``user_msg_id`` and ``assistant_msg_id`` are generated by the OUTER caller
    (launch / continue / rerun) and passed in so the caller can persist them
    in the subagent_runs entry — the redo button needs those ids to revert
    the right user→assistant pair when "redo this turn" is invoked.

    ``subagent_meta`` is the small dict the forwarding emitter stamps into
    every outbound ``chat:subagent:update`` event so the parent UI can route
    updates to the right subagent block: ``{subagent_id, num, name,
    parent_message_id, tool_call_id}``.

    reasoning_details preservation (the one hard rule from
    ``utils/REASONING_DETAILS.md``: every ``rs_*`` id must appear in at most
    one assistant message in the outbound conversation history):

      The subagent's inner pipeline re-enters the same machinery a regular
      chat uses — ``process_chat_payload`` → ``chat_completion_handler``
      (= ``utils.chat.generate_chat_completion``) →
      ``routers/openai.generate_chat_completion``, whose L1120 call to
      ``blocks_to_api_messages`` is THE outbound gate. Every outbound
      request the subagent makes (its initial turn, every tool-call
      continuation, every continue-turn replay) goes through that gate, so
      ``seen_reasoning_ids`` dedup runs on the subagent's history just like
      a regular chat's. The subagent's chat row stores its own
      ``content_blocks`` + ``reasoning_details_per_round`` on each saved
      assistant message via ``process_chat_response``'s usual upserts (the
      chat_id / message_id resolved from ``inner_metadata`` point at the
      SUBAGENT's chat row, not the parent's — so parent and subagent
      reasoning state are completely isolated, no cross-pollination).

      For ``subagent_continue``: ``_load_inner_api_messages`` rebuilds the
      subagent's full history with ``blocks_to_api_messages``; the dedup
      pass there ensures no duplicate ids slip across prior turns.

      For ``rerun_subagent_turn(scope="this_turn")``: the reverted user→
      assistant pair removes both messages from the subagent chat history
      (including their ``reasoning_details_per_round``), so subsequent
      replays don't reference those ids. A fresh assistant message is then
      created with brand-new ids by the upcoming run.

      For ``rerun_subagent_turn(scope="from_launch")`` the guarded history
      transition replaces the entire transcript and appends the new blank turn
      in one transaction, so the next run starts with no prior reasoning.

      The parent's reasoning is on a different chat row entirely and the
      subagent's final answer comes back to the parent as a plain text
      string — no ``reasoning_details`` crosses the boundary. Parent dedup
      remains the parent's problem.
    """
    subagent_model_id = subagent_model.get("id")
    if not subagent_model_id:
        raise RuntimeError("subagent model has no id")

    # 1. Append the user prompt + blank assistant to the subagent chat history.
    await _append_history_for_inner_run(
        subagent_chat_id=subagent_chat_id,
        prompt=prompt,
        user_msg_id=user_msg_id,
        assistant_msg_id=assistant_msg_id,
        model_id=subagent_model_id,
        history_transition=history_transition,
    )
    if history_prepared_callback is not None:
        await history_prepared_callback()

    # 2. Resolve inner tools, then compose the system prompt. The prompt can
    # mention external tools/shared container only when those tools are really
    # available to this subagent turn.
    inner_tool_ids = _resolve_subagent_tool_ids(request, chat_params, parent_metadata)
    container_shared_context = _subagent_container_shared_context(
        request,
        parent_metadata,
        inner_tool_ids,
        import_outputs=force_fanout,
        # The subagent's hidden chat row id IS its stable per-agent id; use it as
        # the browser session token so this subagent drives its own browser tab.
        subagent_id=subagent_chat_id,
    )
    system_prompt = await _compose_subagent_system_prompt(
        request,
        subagent_model_id,
        external_tools_prompt=await _external_tools_prompt(request, inner_tool_ids),
    )
    api_messages = await _load_inner_api_messages(
        subagent_chat_id, assistant_msg_id, system_prompt, model_id=subagent_model_id
    )

    # 3. Build inner_form_data + inner_metadata.

    # Reasoning effort precedence (lowest priority first):
    #   model default (= no `reasoning_effort` sent) →
    #   config.SUBAGENT_DEFAULT_REASONING_EFFORT (admin global) →
    #   chat_params.subagentReasoningEffort (per-chat override).
    # Empty string at any layer means "skip this layer". The resolved value
    # goes into params.reasoning_effort, and `apply_params_to_form_data` in
    # middleware converts it to the canonical `reasoning: {effort: X}` shape
    # that the upstream provider expects.
    resolved_effort = (
        (chat_params.get("subagentReasoningEffort") or "").strip()
        or (
            getattr(request.app.state.config, "SUBAGENT_DEFAULT_REASONING_EFFORT", "")
            or ""
        ).strip()
    )
    inner_params: dict = {"function_calling": "native"}
    if resolved_effort:
        inner_params["reasoning_effort"] = resolved_effort

    # Service tier precedence — same shape as reasoning_effort above.
    # NOTE: `service_tier` rides at the TOP LEVEL of form_data, not inside
    # params. This matches how the main chat passes it from MessageInput →
    # Chat.svelte → /api/chat/completions. `apply_model_params_to_body_openai`
    # in utils/payload.py explicitly strips `service_tier` from params so that
    # stale model.params can't shadow the request's tier; we obey that
    # contract here by writing to inner_form_data directly below.
    resolved_tier = (
        (chat_params.get("subagentServiceTier") or "").strip()
        or (
            getattr(request.app.state.config, "SUBAGENT_DEFAULT_SERVICE_TIER", "")
            or ""
        ).strip()
    )

    inner_form_data: dict = {
        "model": subagent_model_id,
        "messages": api_messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "tool_ids": inner_tool_ids,
        "features": {},  # MUST be empty — no nesting, no image_gen
        "params": inner_params,
    }
    if resolved_tier:
        inner_form_data["service_tier"] = resolved_tier
    # Optional per-chat output cap (user wanted defaults to be infinite).
    max_out_tokens = chat_params.get("subagentMaxOutputTokens")
    if max_out_tokens:
        try:
            inner_form_data["max_tokens"] = int(max_out_tokens)
        except (TypeError, ValueError):
            pass

    subagent_socket_info = {
        "user_id": user.id,
        "session_id": parent_metadata.get("session_id"),
        "chat_id": subagent_chat_id,
        "message_id": assistant_msg_id,
    }

    forwarding_emitter = await _build_forwarding_emitter(
        subagent_socket_info=subagent_socket_info,
        parent_event_emitter=parent_event_emitter,
        subagent_meta=subagent_meta,
        parent_chat_id=parent_metadata.get("chat_id"),
        parent_message_id=parent_metadata.get("message_id"),
        force_fanout=force_fanout,
    )
    subagent_event_caller = get_event_call(subagent_socket_info)

    inner_metadata: dict = {
        "user_id": user.id,
        "chat_id": subagent_chat_id,
        "message_id": assistant_msg_id,
        "session_id": parent_metadata.get("session_id"),
        "tool_ids": inner_tool_ids,
        "tool_servers": None,
        "files": None,
        "features": {},
        "variables": {},
        "timezone": parent_metadata.get("timezone"),
        "model": subagent_model,
        "direct": False,
        # Keep Open WebUI's local subagent event/tool pipeline active while
        # allowing the upstream provider request to be non-streaming.
        "provider_stream": SUBAGENT_PROVIDER_STREAM,
        # Token analytics attribution: subagent LLM usage should roll up into
        # the visible parent chat, not into the hidden subagent chat row.
        "parent_chat_id": parent_metadata.get("chat_id"),
        "parent_message_id": parent_metadata.get("message_id"),
        "params": {
            "function_calling": "native",
            # Batch 200 content-delta chunks per socket emission for subagents.
            # Regular chats still use the global CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE
            # (default 1); this only affects the subagent's inner pipeline.
            "stream_delta_chunk_size": 200,
        },
        # Override hooks — process_chat_response in middleware respects these
        # in place of the default get_event_emitter(metadata) lookup.
        "event_emitter_override": forwarding_emitter,
        "event_caller_override": subagent_event_caller,
        # Flag so any downstream code can detect "this run is inside a
        # subagent" and avoid nesting / re-triggering features.
        "subagent_inner": True,
        **container_shared_context,
    }

    inner_form_data["metadata"] = inner_metadata

    # 4. Swap request.state for the inner run. Restore on exit.
    saved_state = {
        "metadata": getattr(request.state, "metadata", None),
        "model": getattr(request.state, "model", None),
        "direct": getattr(request.state, "direct", False),
    }
    # process_chat_payload builds and RETURNS a fresh metadata dict (it does NOT
    # mutate inner_metadata); the inner MCP/container clients are attached to THAT
    # returned dict. Track it so the finally disconnects the right one. Default to
    # inner_metadata so an early raise still has a safe (no-op) cleanup target.
    inner_run_metadata = inner_metadata
    try:
        request.state.metadata = inner_metadata
        request.state.model = subagent_model
        request.state.direct = False

        # 5. Re-enter the full chat pipeline for the inner run.
        form_data, metadata, events = await process_chat_payload(
            request, inner_form_data, user, inner_metadata, subagent_model
        )
        inner_run_metadata = metadata  # the dict that actually carries mcp_clients
        response = await chat_completion_handler(request, form_data, user)
        await process_chat_response(
            request,
            response,
            form_data,
            user,
            metadata,
            subagent_model,
            events,
            tasks=None,  # no title/tag/follow-up generation for subagents
        )
        # process_chat_response intentionally catches CancelledError to persist
        # partial chat state and emit chat:tasks:cancel. For subagents we still
        # need cancellation to propagate back to the parent tool call; otherwise
        # pressing Stop/Escape can leave the parent task waiting for/retrying a
        # subagent after the inner response handler swallowed the cancellation.
        if _subagent_cancel_is_from_parent_task():
            raise asyncio.CancelledError()
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            _clear_isolated_child_cancellation()
            raise RuntimeError(
                "subagent inner stream was interrupted (not a parent stop)"
            )
    finally:
        # Restore parent's request.state so the rest of the parent's pipeline
        # sees what it expected.
        for k, v in saved_state.items():
            if v is None and not hasattr(request.state, k):
                continue
            setattr(request.state, k, v)
        # Clean up MCP clients spun up by inner process_chat_payload (mirrors
        # the same cleanup in main.py's process_chat finally block). Read from the
        # RETURNED metadata (inner_run_metadata) — process_chat_payload attaches
        # mcp_clients to the dict it returns, NOT to inner_metadata, so reading the
        # input dict here would never disconnect anything (a per-run leak).
        from open_webui.utils.mcp.client import disconnect_mcp_clients

        await disconnect_mcp_clients(
            (inner_run_metadata or {}).get("mcp_clients"),
            context="subagent cleanup",
        )
        # P3_1: cancel any sleeping forward-flush so it can't wake AFTER the inner
        # run has exited (or raised before a terminal) and re-emit stale content to
        # the parent card. No-op when the run ended with a terminal (already
        # cancelled there).
        try:
            _cp = getattr(forwarding_emitter, "cancel_pending", None)
            if _cp is not None:
                await _cp()
        except Exception:  # noqa: BLE001
            log.debug("subagent forwarding flush cancel failed")
        # P2_1: the forwarding emitter seeded global stream-state for this inner
        # assistant message (stream_version_init). If process_chat_payload /
        # chat_completion_handler RAISED before the inner process_chat_response ran
        # its finalizer (provider 4xx/5xx, auth, model-not-found, connection error),
        # that state is orphaned 'in_progress' forever (and shows as a phantom active
        # stream on the hidden chat). Clear it unconditionally here — a redundant
        # clear of an already-reaped key is a cheap no-op, and the normal-path grace
        # clear in process_chat_response has already run by the time we get here.
        try:
            clear_stream_state(assistant_msg_id)
        except Exception:  # noqa: BLE001
            log.debug("subagent inner stream-state clear failed")

    # 6. The inner generation finalized. If it ended with a HARD error (the
    # request kept failing after AGENTIC_EMPTY_ROUND_MAX_RETRIES retries, or a
    # provider error), surface it as a subagent failure so the parent card shows a
    # clear ERROR — even though _extract_final_text could scrape partial reasoning.
    # (A model that merely "thought but didn't write" sets NO error, so its
    # reasoning fallback still flows through as a normal answer.) The caller's
    # retry/error path then gives it another attempt and, if it STILL fails, marks
    # the run 'error' with this message.
    _inner_msg = (
        await Chats.get_message_by_id_and_message_id(
            subagent_chat_id, assistant_msg_id
        )
        or {}
    )
    _inner_err = _inner_msg.get("error")
    if _inner_err:
        from open_webui.utils.middleware import (
            _is_context_fallback_provider_error,
            _is_nonretryable_provider_error,
            _provider_error_text,
        )

        _err_text = _provider_error_text(_inner_err)
        _err_text = _err_text or "subagent generation failed"
        # Input-context exhaustion has its own type because it is deterministic
        # on this model but can be recovered by the configured long-context
        # successor. Other deterministic failures (notably an empty
        # max-output truncation) remain terminal without changing models.
        if _is_context_fallback_provider_error(_inner_err):
            raise SubagentContextLimitError(_err_text)
        if _is_nonretryable_provider_error(_err_text):
            raise SubagentNonRetryableError(_err_text)
        raise RuntimeError(_err_text)

    # 7. Read the final text out of the subagent chat row.
    return await _extract_final_text(subagent_chat_id, assistant_msg_id)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class SubagentTimeoutError(Exception):
    """Raised when a subagent exceeds SUBAGENT_RUN_TIMEOUT_SECONDS."""


class SubagentNonRetryableError(RuntimeError):
    """Raised when a subagent's inner generation failed with a DETERMINISTIC
    provider error that re-running the identical request cannot fix. Subclasses
    RuntimeError so the outer launch/continue/rerun handlers can skip their
    general-purpose retry."""


class SubagentContextLimitError(SubagentNonRetryableError):
    """A subagent turn exhausted the active model's input context.

    This includes an explicit provider context-limit response and the two
    retry-exhausted terminal shapes known to mask that response (empty replies
    and an upstream connection termination). The guarded inner runner may
    recover either form by handing the same turn to the configured long-context
    model.
    """


class SubagentFallbackExhaustedError(SubagentNonRetryableError):
    """The one configured long-context handoff was attempted but did not
    complete. Marked non-retryable so an outer retry cannot accidentally switch
    the hidden chat back to the original model."""


async def _run_inner_chat_guarded(**kwargs) -> Optional[str]:
    """Run a subagent's inner chat under a concurrency bound and an OPTIONAL
    wall-clock timeout, with one centralized context-fallback handoff.

    A context fallback replaces the failed user→assistant pair and changes the
    hidden chat's canonical model in the same database transaction. This is the
    shared lifecycle for launches, continuations, and manual reruns; successful
    handoff therefore also controls every later continuation without any
    path-specific repair logic.

    The timeout is disabled by default
    (``SUBAGENT_RUN_TIMEOUT_SECONDS`` = 0) so subagents can research without any
    time ceiling; set a positive value only as an ops backstop against a
    subagent whose own tool genuinely hangs, in which case the timeout becomes a
    normal error the retry loop / parent model can handle. The semaphore caps
    how many subagents run at once per worker (subagent_launch is
    parallelizable)."""
    sem = _get_subagent_concurrency_sem()

    async def _run_once(run_kwargs: dict) -> Optional[str]:
        if SUBAGENT_RUN_TIMEOUT_SECONDS and SUBAGENT_RUN_TIMEOUT_SECONDS > 0:
            try:
                return await asyncio.wait_for(
                    _run_inner_chat(**run_kwargs),
                    timeout=SUBAGENT_RUN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                raise SubagentTimeoutError(
                    f"Subagent exceeded the {SUBAGENT_RUN_TIMEOUT_SECONDS}s time "
                    "limit and was stopped."
                )
        return await _run_inner_chat(**run_kwargs)

    async def _run() -> Optional[str]:
        try:
            return await _run_once(kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as primary_error:
            # Most provider failures are handled by the existing outer retry.
            # Only input-context exhaustion is eligible for a model handoff.
            from open_webui.utils.middleware import (
                _is_context_limit_provider_error,
            )

            is_context_limit = isinstance(
                primary_error, SubagentContextLimitError
            ) or _is_context_limit_provider_error(primary_error)
            if not is_context_limit:
                raise

            current_model = kwargs.get("subagent_model") or {}
            current_model_id = str(current_model.get("id") or "")
            fallback_model = _resolve_subagent_context_fallback_model(
                kwargs["request"], current_model_id
            )
            if fallback_model is None:
                raise

            fallback_model_id = str(fallback_model.get("id") or "")
            assistant_msg_id = str(kwargs.get("assistant_msg_id") or "")
            user_msg_id = str(kwargs.get("user_msg_id") or "")
            if not fallback_model_id or not assistant_msg_id or not user_msg_id:
                raise

            retry_meta = {
                **(kwargs.get("subagent_meta") or {}),
                "context_fallback": True,
                "fallback_from_model_id": current_model_id,
                "model_id": fallback_model_id,
            }
            retry_kwargs = {
                **kwargs,
                "subagent_model": fallback_model,
                "subagent_meta": retry_meta,
                # The first preparation callback (used by restart-from-launch
                # reruns) already committed its parent-side stale markers.
                "history_prepared_callback": None,
                "history_transition": {
                    "expected_current_id": assistant_msg_id,
                    "revert_user_message_id": user_msg_id,
                    "revert_assistant_message_id": assistant_msg_id,
                    "expected_model_id": current_model_id,
                    "set_model_id": fallback_model_id,
                },
            }

            log.warning(
                "subagent context exhausted; retrying failed turn on fallback "
                "chat=%s from_model=%s fallback_model=%s",
                kwargs.get("subagent_chat_id"),
                current_model_id,
                fallback_model_id,
            )
            await _reemit_subagent_start_on_retry(
                kwargs.get("parent_event_emitter"), retry_meta
            )
            try:
                fallback_result = await _run_once(retry_kwargs)
                if not fallback_result:
                    raise RuntimeError("subagent produced no final text")
                return fallback_result
            except asyncio.CancelledError:
                raise
            except ChatHistoryConflictError:
                raise
            except Exception as fallback_error:
                raise SubagentFallbackExhaustedError(
                    f"Long-context fallback model {fallback_model_id} failed: "
                    f"{fallback_error}"
                ) from fallback_error

    if sem is None:
        return await _run()
    async with sem:
        return await _run()


async def _reemit_subagent_start_on_retry(
    parent_event_emitter: Optional[Callable], subagent_meta: dict
) -> None:
    """P6_1: re-assert the live subagent card at the start of a retry attempt.

    Attempt 1's forwarded terminal ``chat:message:error`` leaves the frontend card
    on 'error', and the running re-promotion path deliberately excludes 'error' —
    so without a fresh start event a later attempt's live deltas cannot clear the
    red card and recovery would hinge on a single terminal event (drop it during a
    primary re-election / tab close and the card sticks red forever while the run
    is actually 'done'). Re-emitting ``chat:subagent:start`` resets the card to
    running+live (same semantics as the original launch)."""
    if parent_event_emitter is None:
        return
    try:
        await parent_event_emitter(
            {"type": "chat:subagent:start", "data": subagent_meta}
        )
    except Exception as e:  # noqa: BLE001
        log.debug(f"chat:subagent:start (retry) re-emit failed: {e}")


async def run_subagent_launch(
    *,
    request: Optional[Request],
    user_dict: Optional[dict],
    parent_metadata: Optional[dict],
    parent_event_emitter: Optional[Callable],
    parent_event_call: Optional[Callable],
    parent_model: Optional[dict],
    name: str,
    prompt: str,
    background: str = "",
) -> str:
    """Spawn and run a fresh subagent. Returns the formatted tool-result string
    the parent model will see.

    Auto-retries once on unexpected errors (per the agreed design). Returns a
    user-visible error string on retry exhaustion. Propagates
    ``asyncio.CancelledError`` so parent cancellation tears down cleanly.
    """
    if request is None or user_dict is None or parent_metadata is None or parent_event_emitter is None:
        log.warning(
            "run_subagent_launch: missing required context — "
            f"request={request is not None} user={user_dict is not None} "
            f"meta={parent_metadata is not None} emitter={parent_event_emitter is not None}"
        )
        return "Subagent ERROR: tool was invoked without required runtime context"

    user = await Users.get_user_by_id(user_dict.get("id"))
    if user is None:
        return "Subagent ERROR: user context unavailable"

    # The model fully controls these untyped tool-call args. Coerce to str so a
    # non-string (e.g. {"name": 123}) can't raise AttributeError on .strip() before
    # the retry try-block and leak a raw Python exception as the tool result (C23).
    name = name if isinstance(name, str) else ("" if name is None else str(name))
    prompt = prompt if isinstance(prompt, str) else ("" if prompt is None else str(prompt))
    background = (
        background if isinstance(background, str) else ("" if background is None else str(background))
    )

    parent_chat_id = parent_metadata.get("chat_id")
    parent_message_id = parent_metadata.get("message_id")
    if not parent_chat_id or not parent_message_id:
        return "Subagent ERROR: parent chat context unavailable"

    parent_chat = await Chats.get_chat_by_id_and_user_id(parent_chat_id, user.id)
    if parent_chat is None:
        return "Subagent ERROR: parent chat not accessible"

    log.warning(
        f"run_subagent_launch START: name={name!r} "
        f"chat={parent_chat_id} msg={parent_message_id}"
    )

    # Provisional name/num for the subagent chat title (cosmetic). The
    # AUTHORITATIVE num + collision-disambiguated name are assigned atomically
    # under the parent-message write lock by the first `_upsert_subagent_run`
    # below (via `reserve=`), so parallel launches in one fan-out never collide.
    all_runs = _gather_all_subagent_runs(parent_chat, parent_message_id)
    final_name, was_renamed = _disambiguate_name((name or "").strip(), all_runs)
    num = len(all_runs) + 1
    # Runs on OTHER parent messages (stable during this turn) — the reservation
    # combines these with the live siblings on THIS message to number/name us.
    _other_runs = {}
    _pc_messages = _history_messages(parent_chat)
    for _mid in _branch_message_ids(parent_chat, parent_message_id):
        if _mid == parent_message_id:
            continue
        _msg = _pc_messages.get(_mid)
        if not isinstance(_msg, dict):
            continue
        _r = _msg.get("subagent_runs")
        if isinstance(_r, dict):
            _other_runs.update(_r)

    # Resolve subagent model.
    subagent_model_id = _resolve_subagent_model_id(request, parent_chat, parent_model)
    if subagent_model_id is None:
        return f"Subagent {num} ({final_name}) ERROR: no model configured/available"
    subagent_model = request.app.state.MODELS[subagent_model_id]

    # Atomic row create with subagent_of meta — never appears in the main chat
    # list. Run it as one shielded task and re-await through repeated Stops. An
    # async DB call can commit just before cancellation reaches this coroutine;
    # abandoning that unknown-commit window used to leak an unreachable hidden
    # chat because no parent run had been registered yet.
    subagent_chat_title = f"{final_name} (subagent of {parent_chat.title})"
    create_hidden_chat_task = asyncio.create_task(
        Chats.import_chat(
            user.id,
            ChatImportForm(
                **{
                    "chat": {
                        "title": subagent_chat_title,
                        "models": [subagent_model_id],
                        "history": {"messages": {}, "currentId": None},
                        "messages": [],
                        "params": {},
                    },
                    "meta": {
                        "subagent_of": parent_chat_id,
                        # Filled below once the row-generated id is known.
                        "subagent_id": None,
                        "subagent_name": final_name,
                        "subagent_num": num,
                    },
                }
            ),
        )
    )
    create_was_cancelled = await _wait_shielded_task_to_completion(
        create_hidden_chat_task
    )
    if create_hidden_chat_task.cancelled():
        raise asyncio.CancelledError
    create_error = create_hidden_chat_task.exception()
    if create_error is not None:
        log.error(
            "subagent hidden-chat creation failed",
            exc_info=(
                type(create_error),
                create_error,
                create_error.__traceback__,
            ),
        )
        if create_was_cancelled:
            raise asyncio.CancelledError
        return (
            f"Subagent {num} ({final_name}) ERROR: "
            "could not create subagent chat row"
        )
    subagent_chat = create_hidden_chat_task.result()
    if subagent_chat is None:
        if create_was_cancelled:
            raise asyncio.CancelledError
        return f"Subagent {num} ({final_name}) ERROR: could not create subagent chat row"
    subagent_id = subagent_chat.id
    if create_was_cancelled:
        # Creation committed, but cancellation arrived before any durable parent
        # pointer existed. Delete this exact row before propagating the Stop.
        await _delete_hidden_chat_to_completion(
            subagent_id,
            reason="cancelled before parent registration",
        )
        raise asyncio.CancelledError

    # Read the parent tool_call_id from middleware's per-task ContextVar
    # (set in `_execute_tool_call` right before invoking us). The contextvar
    # is the right primitive here: when the parent's tool loop runs
    # parallelizable tools via asyncio.gather, each branch has its own value.
    tool_call_id = current_tool_call_id_var.get() or ""
    started_at = int(time.time())
    base_run_patch = {
        "subagent_id": subagent_id,
        "entry_key": subagent_id,
        "num": num,
        "name": final_name,
        "chat_id": subagent_id,
        "tool_call_id": tool_call_id,
        "prompt": prompt,
        "background": background,
        # Carried on EVERY patch so a terminal write always re-asserts the start
        # time — the timer ("Researched for 7m") needs both bounds, and this
        # makes "Done" (no-duration) impossible for a run that actually ran.
        "started_at": started_at,
    }
    # First write reserves this subagent's slot: num + collision-disambiguated
    # name are assigned ATOMICALLY from the live run map under the parent-message
    # write lock, so parallel launches in one fan-out get distinct numbers/names
    # instead of all computing "Subagent N" off the same stale snapshot. Settle
    # this write through repeated cancellation too: after an ambiguous commit,
    # deleting the hidden row could leave a durable parent run pointing nowhere.
    register_parent_task = asyncio.create_task(
        _upsert_subagent_run(
            parent_chat_id,
            parent_message_id,
            subagent_id,
            {
                **base_run_patch,
                "status": "running",
                "started_at": started_at,
            },
            reserve={
                "desired_name": (name or "").strip(),
                "other_runs": _other_runs,
            },
        )
    )
    registration_was_cancelled = await _wait_shielded_task_to_completion(
        register_parent_task
    )
    if register_parent_task.cancelled():
        await _delete_hidden_chat_to_completion(
            subagent_id,
            reason="parent registration task cancelled",
        )
        raise asyncio.CancelledError
    registration_error = register_parent_task.exception()
    if registration_error is not None:
        log.error(
            "subagent parent registration failed",
            exc_info=(
                type(registration_error),
                registration_error,
                registration_error.__traceback__,
            ),
        )
        merged_run = None
    else:
        merged_run = register_parent_task.result()

    if isinstance(merged_run, dict):
        num = merged_run.get("num", num)
        final_name = merged_run.get("name", final_name)
        base_run_patch["num"] = num
        base_run_patch["name"] = final_name

    if registration_was_cancelled:
        if isinstance(merged_run, dict):
            # The reservation committed. Keep its hidden row and close the exact
            # registered entry as cancelled before propagating Stop; deleting the
            # row now would create a durable dangling parent pointer.
            cancel_registration_task = asyncio.create_task(
                _upsert_subagent_run(
                    parent_chat_id,
                    parent_message_id,
                    subagent_id,
                    {
                        **base_run_patch,
                        "status": "cancelled",
                        "ended_at": int(time.time()),
                    },
                )
            )
            await _wait_shielded_task_to_completion(cancel_registration_task)
            if cancel_registration_task.cancelled():
                log.error(
                    "cancelled launch terminal write was itself cancelled for %s",
                    subagent_id,
                )
            elif cancel_registration_task.exception() is not None:
                terminal_error = cancel_registration_task.exception()
                log.error(
                    "cancelled launch terminal write failed for %s",
                    subagent_id,
                    exc_info=(
                        type(terminal_error),
                        terminal_error,
                        terminal_error.__traceback__,
                    ),
                )
        else:
            await _delete_hidden_chat_to_completion(
                subagent_id,
                reason="cancelled failed parent registration",
            )
        raise asyncio.CancelledError

    if not isinstance(merged_run, dict):
        # The hidden row is not useful without a durable parent-side run entry.
        # Remove the exact row this launch just created instead of leaving an
        # unreachable hidden-chat orphan.
        delete_was_cancelled, _ = await _delete_hidden_chat_to_completion(
            subagent_id,
            reason="failed parent registration",
        )
        if delete_was_cancelled:
            raise asyncio.CancelledError
        return (
            f"Subagent {num} ({final_name}) ERROR: could not register the "
            "subagent on the parent message"
        )

    # Reservation is now durable. Publish the side channel used by middleware
    # and make the hidden row's title/meta match the atomically assigned name
    # and number (parallel launches may have disambiguated both).
    if not hasattr(request.state, "subagent_id_by_tool_call"):
        request.state.subagent_id_by_tool_call = {}
    if tool_call_id:
        request.state.subagent_id_by_tool_call[tool_call_id] = subagent_id
    final_subagent_chat_title = f"{final_name} (subagent of {parent_chat.title})"
    try:
        await Chats.update_chat_meta_by_id(
            subagent_id,
            {
                "subagent_of": parent_chat_id,
                "subagent_id": subagent_id,
                "subagent_name": final_name,
                "subagent_num": num,
            },
        )
        if final_subagent_chat_title != subagent_chat_title:
            await Chats.update_chat_title_by_id(
                subagent_id, final_subagent_chat_title
            )
    except Exception as e:  # noqa: BLE001
        # Parent-side run identity remains authoritative and the hidden row is
        # still correctly hidden by subagent_of; this cosmetic sync is repairable.
        log.warning("subagent hidden-row identity sync failed: %s", e)
    # Only a NON-empty desired name that got changed is a "rename" (collision).
    # A BLANK desired name that the system auto-assigned is NOT a rename — gating
    # on bool(final_name) instead would wrongly tell the parent model "the name you
    # chose was already taken" when it never chose one.
    _desired_name = (name or "").strip()
    was_renamed = bool(_desired_name) and final_name != _desired_name

    subagent_meta = {
        "subagent_id": subagent_id,
        "entry_key": subagent_id,
        "num": num,
        "name": final_name,
        "parent_message_id": parent_message_id,
        "tool_call_id": tool_call_id,
        "chat_id": subagent_id,
        "prompt": prompt,
        "background": background,
        "started_at": started_at,
    }
    try:
        await parent_event_emitter(
            {
                "type": "chat:subagent:start",
                "data": subagent_meta,
            }
        )
    except Exception as e:  # noqa: BLE001
        log.debug(f"chat:subagent:start emit failed: {e}")

    # Compose the inner-run prompt: prompt + optional background block.
    inner_prompt = prompt or ""
    if background:
        inner_prompt = (
            f"{inner_prompt}\n\n<background>\n{background}\n</background>"
        ).strip()

    chat_params = ((parent_chat.chat or {}).get("params")) or {}

    # Auto-retry once on unexpected errors. Cancellations propagate (don't
    # retry on user-stop).
    last_error: Optional[str] = None
    history_transition: dict = {"expected_current_id": None}
    for attempt in (1, 2):
        if attempt > 1:
            await _reemit_subagent_start_on_retry(parent_event_emitter, subagent_meta)
        # Generate fresh message ids per attempt — the retry path wipes
        # subagent history first, so reusing the prior attempt's ids would
        # leave stale references. Persist them on the entry up front so the
        # "Redo this turn" button has the right ids to revert even if the
        # inner chat crashes mid-run.
        user_msg_id = str(uuid4())
        assistant_msg_id = str(uuid4())
        attempt_identity = await _upsert_subagent_run(
            parent_chat_id,
            parent_message_id,
            subagent_id,
            {
                **base_run_patch,
                "user_msg_id": user_msg_id,
                "assistant_msg_id": assistant_msg_id,
            },
        )
        if not isinstance(attempt_identity, dict):
            last_error = "could not persist subagent turn identity before generation"
            await _upsert_subagent_run(
                parent_chat_id,
                parent_message_id,
                subagent_id,
                {
                    **base_run_patch,
                    "status": "error",
                    "ended_at": int(time.time()),
                    "error": {"message": last_error},
                },
            )
            await _emit_subagent_terminal(
                parent_event_emitter,
                subagent_meta,
                status="error",
                message=last_error,
                user_id=user.id,
                parent_chat_id=parent_chat_id,
                parent_message_id=parent_message_id,
            )
            return f"Subagent {num} ({final_name}) ERROR: {last_error}"
        final_text = None
        try:
            final_text = await _run_inner_chat_guarded(
                request=request,
                user=user,
                subagent_model=subagent_model,
                subagent_chat_id=subagent_id,
                prompt=inner_prompt,
                user_msg_id=user_msg_id,
                assistant_msg_id=assistant_msg_id,
                parent_metadata=parent_metadata,
                parent_event_emitter=parent_event_emitter,
                parent_event_call=parent_event_call,
                subagent_meta=subagent_meta,
                chat_params=chat_params,
                history_transition=history_transition,
            )
            if not final_text:
                # Treat empty final text as an error so the retry loop catches
                # it. (Empty tool result back to the parent is a bad UX —
                # tells the parent model nothing.)
                raise RuntimeError("subagent produced no final text")
            await _upsert_subagent_run(
                parent_chat_id,
                parent_message_id,
                subagent_id,
                {
                    **base_run_patch,
                    "status": "done",
                    "ended_at": int(time.time()),
                    "final_text": final_text,
                },
            )
            prefix = f"Subagent {num} ({final_name}) output:\n\n"
            if was_renamed:
                # Surface rename so the parent model uses the right name in
                # subagent_continue later.
                prefix = (
                    f"Note: the name you chose was already taken in this "
                    f"chat; this subagent was registered as '{final_name}'.\n\n"
                    + prefix
                )
            return f"{prefix}{final_text}"
        except asyncio.CancelledError:
            # A CancelledError here is GENUINE (user pressed Stop) only when the
            # parent task itself is being cancelled. Subagents run inline in the
            # parent task via asyncio.gather, so a real user-stop sets
            # `current_task().cancelling()`. If it is NOT cancelling, this is a
            # SPURIOUS per-subagent cancellation — an upstream stream closing /
            # erroring for THIS subagent's inner generate_chat_completion — which
            # previously marked a subagent that did substantial work as "cancelled"
            # (no retry). Treat that as a normal retryable error instead so the
            # subagent gets a clean second attempt and the user doesn't see a
            # bogus "stopped" on a subagent they never stopped.
            genuine_user_stop = _subagent_cancel_is_from_parent_task()
            if genuine_user_stop:
                # C3: a Stop landing in/after the done-write window must NOT discard
                # an answer the subagent already produced. If final_text was computed
                # this attempt, persist it as 'done' (shielded so the cancel can't
                # truncate the terminal write) and resolve the card to 'done' — the
                # answer is then recoverable into the parent via reconcile/sweep on
                # finalize/reload. Only a genuinely-unfinished run records 'cancelled'.
                _completed = isinstance(final_text, str) and bool(final_text.strip())
                try:
                    terminal_write_task = asyncio.create_task(
                        _upsert_subagent_run(
                            parent_chat_id,
                            parent_message_id,
                            subagent_id,
                            {
                                **base_run_patch,
                                "status": "done" if _completed else "cancelled",
                                "ended_at": int(time.time()),
                                **({"final_text": final_text} if _completed else {}),
                            },
                        )
                    )
                    await _wait_shielded_task_to_completion(terminal_write_task)
                    terminal_write = terminal_write_task.result()
                    if not isinstance(terminal_write, dict):
                        raise RuntimeError(
                            "subagent stop terminal write was not committed"
                        )
                    if _completed:
                        await _emit_subagent_terminal(
                            parent_event_emitter,
                            subagent_meta,
                            status="done",
                            user_id=user.id,
                            parent_chat_id=parent_chat_id,
                            parent_message_id=parent_message_id,
                        )
                    else:
                        await _emit_subagent_cancel(
                            parent_event_emitter,
                            subagent_meta,
                            user_id=user.id,
                            parent_chat_id=parent_chat_id,
                            parent_message_id=parent_message_id,
                        )
                except Exception:
                    log.exception("subagent stop terminal write/emit failed")
                raise
            # Spurious per-subagent cancel → retry like any error.
            _clear_isolated_child_cancellation()
            last_error = "subagent inner stream was cancelled (not a user stop)"
            log.warning(
                f"subagent {final_name} attempt {attempt}/2 hit a spurious "
                "CancelledError (parent not stopping) — retrying as an error"
            )
            if attempt == 1:
                history_transition = {
                    "expected_current_id": assistant_msg_id,
                    "reset_history": True,
                }
                continue
            await _upsert_subagent_run(
                parent_chat_id,
                parent_message_id,
                subagent_id,
                {
                    **base_run_patch,
                    "status": "error",
                    "ended_at": int(time.time()),
                    "error": {"message": last_error},
                },
            )
            await _emit_subagent_terminal(
                parent_event_emitter,
                subagent_meta,
                status="error",
                message=last_error,
                user_id=user.id,
                parent_chat_id=parent_chat_id,
                parent_message_id=parent_message_id,
            )
            return (
                f"Subagent {num} ({final_name}) ERROR after retry: {last_error}"
            )
        except Exception as e:  # noqa: BLE001
            last_error = str(e)
            log.exception(
                f"subagent {final_name} attempt {attempt}/2 failed: {e}"
            )
            if attempt == 1 and not isinstance(
                e, (SubagentNonRetryableError, ChatHistoryConflictError)
            ):
                # Reset + replacement append happen together at the start of the
                # next attempt. The expected leaf makes an intervening manual
                # hidden-chat edit a clean conflict instead of something we wipe.
                history_transition = {
                    "expected_current_id": assistant_msg_id,
                    "reset_history": True,
                }
                continue
            await _upsert_subagent_run(
                parent_chat_id,
                parent_message_id,
                subagent_id,
                {
                    **base_run_patch,
                    "status": "error",
                    "ended_at": int(time.time()),
                    "error": {"message": last_error},
                    **(
                        {"user_msg_id": None, "assistant_msg_id": None}
                        if isinstance(e, ChatHistoryConflictError)
                        else {}
                    ),
                },
            )
            await _emit_subagent_terminal(
                parent_event_emitter,
                subagent_meta,
                status="error",
                message=last_error or "unknown error",
                user_id=user.id,
                parent_chat_id=parent_chat_id,
                parent_message_id=parent_message_id,
            )
            return (
                f"Subagent {num} ({final_name}) ERROR after retry: "
                f"{last_error or 'unknown error'}"
            )
    # Defensive — loop always returns. This line shouldn't be reachable.
    return f"Subagent {num} ({final_name}) ERROR: unreachable code path"


async def run_subagent_continue(
    *,
    request: Optional[Request],
    user_dict: Optional[dict],
    parent_metadata: Optional[dict],
    parent_event_emitter: Optional[Callable],
    parent_event_call: Optional[Callable],
    parent_model: Optional[dict],
    name_or_id: str,
    prompt: str,
) -> str:
    """Continue a previously-launched subagent with one more turn."""
    if request is None or user_dict is None or parent_metadata is None or parent_event_emitter is None:
        return "Subagent ERROR: tool was invoked without required runtime context"

    # Coerce untyped model-controlled args to str so {"name_or_id": 7} can't crash
    # `(name_or_id or "").strip()` with a raw exception as the tool result (C23).
    name_or_id = (
        name_or_id if isinstance(name_or_id, str) else ("" if name_or_id is None else str(name_or_id))
    )
    prompt = prompt if isinstance(prompt, str) else ("" if prompt is None else str(prompt))

    user = await Users.get_user_by_id(user_dict.get("id"))
    if user is None:
        return "Subagent ERROR: user context unavailable"

    parent_chat_id = parent_metadata.get("chat_id")
    parent_message_id = parent_metadata.get("message_id")
    if not parent_chat_id or not parent_message_id:
        return "Subagent ERROR: parent chat context unavailable"

    parent_chat = await Chats.get_chat_by_id_and_user_id(parent_chat_id, user.id)
    if parent_chat is None:
        return "Subagent ERROR: parent chat not accessible"

    # Resolve name_or_id against existing subagent_runs.
    all_runs = _gather_all_subagent_runs(parent_chat, parent_message_id)
    if not all_runs:
        return f"Subagent ERROR: no subagent named or numbered '{name_or_id}' exists in this chat"

    target_id: Optional[str] = None
    needle = (name_or_id or "").strip()
    # First try exact id match.
    if needle in all_runs:
        target_id = needle
    else:
        # Then name match — most recently started wins, case-insensitive.
        candidates = [
            (sid, run)
            for sid, run in all_runs.items()
            if isinstance(run, dict)
            and (run.get("name") or "").lower() == needle.lower()
        ]
        if candidates:
            candidates.sort(
                key=lambda kv: int((kv[1] or {}).get("started_at") or 0),
                reverse=True,
            )
            target_id = candidates[0][0]
        else:
            # Last shot: numeric num match.
            try:
                target_num = int(needle)
                for sid, run in all_runs.items():
                    if isinstance(run, dict) and (run.get("num") == target_num):
                        target_id = sid
                        break
            except (TypeError, ValueError):
                pass

    if target_id is None:
        return f"Subagent ERROR: no subagent named '{name_or_id}' found in this chat"

    target_run = all_runs[target_id]
    # `target_id` is an entry_key from _gather_all_subagent_runs — for a
    # CONTINUATION that key is `subagent_id#tool_call_id`, which is NOT a chat-row
    # id. Map it back to the real subagent chat id before the row load and every
    # downstream use; otherwise every name/num continue AFTER the first
    # continuation resolves to the compound key, misses the chat row, and aborts
    # with "subagent chat row missing". Both entry shapes persist subagent_id, so
    # this is lossless (and a no-op for a plain launch key).
    target_id = str(
        target_run.get("subagent_id") or target_run.get("chat_id") or target_id
    )
    target_name = target_run.get("name") or "subagent"
    target_num = target_run.get("num") or 0

    # Load the subagent chat row.
    subagent_chat = await Chats.get_chat_by_id_and_user_id(target_id, user.id)
    if subagent_chat is None:
        return f"Subagent {target_num} ({target_name}) ERROR: subagent chat row missing"

    # Guard (P1_1): refuse to continue a subagent whose hidden transcript is being
    # mutated by ANOTHER live turn right now — a detached redo of this subagent, or
    # any other running turn. (subagent_continue is parallelizable=False so two
    # continues in ONE parent batch already serialize, but a rerun task runs
    # DETACHED and would otherwise race this continue's unlocked history append and
    # clobber one of them.) Re-read the parent fresh to shrink the TOCTOU window;
    # return a clean tool result so the parent model can simply retry later.
    _fresh_parent = (
        await Chats.get_chat_by_id_and_user_id(parent_chat_id, user.id) or parent_chat
    )
    hidden_chat_idle = await _reconcile_idle_running_turns_for_subagent(
        _fresh_parent, subagent_chat, target_id
    )
    if not hidden_chat_idle:
        for _m_id, _msg in _history_messages(_fresh_parent).items():
            _runs = _msg.get("subagent_runs") if isinstance(_msg, dict) else None
            if not isinstance(_runs, dict):
                continue
            for _k, _r in _runs.items():
                if (
                    isinstance(_r, dict)
                    and (
                        _r.get("subagent_id") == target_id
                        or _r.get("chat_id") == target_id
                    )
                    and _r.get("status") == "running"
                    and not _r.get("ended_at")
                ):
                    return (
                        f"Subagent {target_num} ({target_name}) is currently running "
                        f"another turn — wait for it to finish before continuing it."
                    )

    # Resolve subagent model — prefer what the original subagent was using;
    # fall back to per-chat / global / parent.
    subagent_model_id = None
    sa_models = (subagent_chat.chat or {}).get("models") or []
    if sa_models and sa_models[0] in request.app.state.MODELS:
        subagent_model_id = sa_models[0]
    else:
        subagent_model_id = _resolve_subagent_model_id(
            request, parent_chat, parent_model
        )
    if subagent_model_id is None:
        return (
            f"Subagent {target_num} ({target_name}) ERROR: no model available "
            f"to continue this subagent"
        )
    subagent_model = request.app.state.MODELS[subagent_model_id]

    # Side-channel + chat:subagent:start (new tool_call_id, same subagent_id).
    tool_call_id = current_tool_call_id_var.get() or ""
    if not hasattr(request.state, "subagent_id_by_tool_call"):
        request.state.subagent_id_by_tool_call = {}
    if tool_call_id:
        request.state.subagent_id_by_tool_call[tool_call_id] = target_id

    started_at = int(time.time())
    subagent_meta = {
        "subagent_id": target_id,
        "num": target_num,
        "name": target_name,
        "parent_message_id": parent_message_id,
        "tool_call_id": tool_call_id,
        "chat_id": target_id,
        "continuation": True,
        "started_at": started_at,
    }
    # Continuations get their OWN entry under subagent_runs (keyed by
    # tool_call_id when we have one, else falling back to subagent_id with a
    # round suffix). This lets each tool call's collapsible block stay
    # independent in the parent UI.
    continue_entry_key = (
        f"{target_id}#{tool_call_id}" if tool_call_id else f"{target_id}#{started_at}"
    )
    continue_base_run_patch = {
        "subagent_id": target_id,
        "entry_key": continue_entry_key,
        "num": target_num,
        "name": target_name,
        "chat_id": target_id,
        "tool_call_id": tool_call_id,
        "continuation": True,
        "prompt": prompt,
        # See base_run_patch in run_subagent_launch — keep timing on every patch.
        "started_at": started_at,
    }
    try:
        registered_continue = await _upsert_subagent_run(
            parent_chat_id,
            parent_message_id,
            continue_entry_key,
            {
                **continue_base_run_patch,
                "status": "running",
                "started_at": started_at,
            },
            cas_block_if_running=True,
            exclusive_running_subagent_id=target_id,
        )
    except SubagentRerunBlockedError:
        return (
            f"Subagent {target_num} ({target_name}) is currently running "
            f"another turn — wait for it to finish before continuing it."
        )
    if not isinstance(registered_continue, dict):
        return (
            f"Subagent {target_num} ({target_name}) continue ERROR: could not "
            "register the continuation on the parent message"
        )
    subagent_meta["entry_key"] = continue_entry_key
    subagent_meta["prompt"] = prompt
    try:
        await parent_event_emitter(
            {
                "type": "chat:subagent:start",
                "data": subagent_meta,
            }
        )
    except Exception as e:  # noqa: BLE001
        log.debug(f"chat:subagent:start (continue) emit failed: {e}")

    chat_params = ((parent_chat.chat or {}).get("params")) or {}

    last_error: Optional[str] = None
    history_transition: dict = {
        "expected_current_id": (
            ((subagent_chat.chat or {}).get("history") or {}).get("currentId")
        )
    }
    for attempt in (1, 2):
        if attempt > 1:
            await _reemit_subagent_start_on_retry(parent_event_emitter, subagent_meta)
        user_msg_id = str(uuid4())
        assistant_msg_id = str(uuid4())
        attempt_identity = await _upsert_subagent_run(
            parent_chat_id,
            parent_message_id,
            continue_entry_key,
            {
                **continue_base_run_patch,
                "user_msg_id": user_msg_id,
                "assistant_msg_id": assistant_msg_id,
            },
        )
        if not isinstance(attempt_identity, dict):
            last_error = (
                "could not persist subagent continuation identity before generation"
            )
            await _upsert_subagent_run(
                parent_chat_id,
                parent_message_id,
                continue_entry_key,
                {
                    **continue_base_run_patch,
                    "status": "error",
                    "ended_at": int(time.time()),
                    "error": {"message": last_error},
                },
            )
            await _emit_subagent_terminal(
                parent_event_emitter,
                subagent_meta,
                status="error",
                message=last_error,
                user_id=user.id,
                parent_chat_id=parent_chat_id,
                parent_message_id=parent_message_id,
            )
            return (
                f"Subagent {target_num} ({target_name}) continue ERROR: "
                f"{last_error}"
            )
        final_text = None
        try:
            final_text = await _run_inner_chat_guarded(
                request=request,
                user=user,
                subagent_model=subagent_model,
                subagent_chat_id=target_id,
                prompt=prompt or "",
                user_msg_id=user_msg_id,
                assistant_msg_id=assistant_msg_id,
                parent_metadata=parent_metadata,
                parent_event_emitter=parent_event_emitter,
                parent_event_call=parent_event_call,
                subagent_meta=subagent_meta,
                chat_params=chat_params,
                history_transition=history_transition,
            )
            if not final_text:
                raise RuntimeError("subagent produced no final text")
            await _upsert_subagent_run(
                parent_chat_id,
                parent_message_id,
                continue_entry_key,
                {
                    **continue_base_run_patch,
                    "status": "done",
                    "ended_at": int(time.time()),
                    "final_text": final_text,
                },
            )
            return (
                f"Subagent {target_num} ({target_name}) continued — output:\n\n"
                f"{final_text}"
            )
        except asyncio.CancelledError:
            # Genuine user-stop only when the parent task is actually cancelling
            # (see run_subagent_launch for the full rationale). A spurious
            # per-subagent cancel (e.g. the inner stream's aiohttp total timeout)
            # is retried as an error so a continue that did real work isn't marked
            # "stopped" when the user never stopped it.
            if _subagent_cancel_is_from_parent_task():
                # C3: preserve a finished answer if the Stop landed in the
                # done-write window (mirror run_subagent_launch).
                _completed = isinstance(final_text, str) and bool(final_text.strip())
                try:
                    terminal_write_task = asyncio.create_task(
                        _upsert_subagent_run(
                            parent_chat_id,
                            parent_message_id,
                            continue_entry_key,
                            {
                                **continue_base_run_patch,
                                "status": "done" if _completed else "cancelled",
                                "ended_at": int(time.time()),
                                **({"final_text": final_text} if _completed else {}),
                            },
                        )
                    )
                    await _wait_shielded_task_to_completion(terminal_write_task)
                    terminal_write = terminal_write_task.result()
                    if not isinstance(terminal_write, dict):
                        raise RuntimeError(
                            "subagent continue stop terminal write was not committed"
                        )
                    if _completed:
                        await _emit_subagent_terminal(
                            parent_event_emitter,
                            subagent_meta,
                            status="done",
                            user_id=user.id,
                            parent_chat_id=parent_chat_id,
                            parent_message_id=parent_message_id,
                        )
                    else:
                        await _emit_subagent_cancel(
                            parent_event_emitter,
                            subagent_meta,
                            user_id=user.id,
                            parent_chat_id=parent_chat_id,
                            parent_message_id=parent_message_id,
                        )
                except Exception:
                    log.exception("subagent continue stop terminal write/emit failed")
                raise
            _clear_isolated_child_cancellation()
            last_error = "subagent inner stream was cancelled (not a user stop)"
            log.warning(
                f"subagent {target_name} continue attempt {attempt}/2 hit a "
                "spurious CancelledError (parent not stopping) — retrying"
            )
            if attempt == 1:
                history_transition = {
                    "expected_current_id": assistant_msg_id,
                    "revert_user_message_id": user_msg_id,
                    "revert_assistant_message_id": assistant_msg_id,
                }
                continue
            await _upsert_subagent_run(
                parent_chat_id,
                parent_message_id,
                continue_entry_key,
                {
                    **continue_base_run_patch,
                    "status": "error",
                    "ended_at": int(time.time()),
                    "error": {"message": last_error},
                },
            )
            await _emit_subagent_terminal(
                parent_event_emitter,
                subagent_meta,
                status="error",
                message=last_error,
                user_id=user.id,
                parent_chat_id=parent_chat_id,
                parent_message_id=parent_message_id,
            )
            return (
                f"Subagent {target_num} ({target_name}) continue ERROR after "
                f"retry: {last_error}"
            )
        except Exception as e:  # noqa: BLE001
            last_error = str(e)
            log.exception(
                f"subagent {target_name} continue attempt {attempt}/2 failed: {e}"
            )
            if attempt == 1 and not isinstance(
                e, (SubagentNonRetryableError, ChatHistoryConflictError)
            ):
                # For continues, DO NOT wipe the whole history — that'd destroy
                # the prior research the user explicitly wants to keep. Instead
                # revert just THIS failed attempt's user→blank-assistant pair so
                # the second attempt re-runs against the exact state the turn
                # started from. Reverting (rather than leaving the pair in place)
                # keeps the hidden transcript clean: no doubled/blank turns get
                # buried mid-history, so a later "Redo last subagent turn" still
                # reverts a single coherent pair. SKIPPED for a non-retryable
                # provider error (context window exceeded): the retry would just
                # overflow again.
                history_transition = {
                    "expected_current_id": assistant_msg_id,
                    "revert_user_message_id": user_msg_id,
                    "revert_assistant_message_id": assistant_msg_id,
                }
                continue
            await _upsert_subagent_run(
                parent_chat_id,
                parent_message_id,
                continue_entry_key,
                {
                    **continue_base_run_patch,
                    "status": "error",
                    "ended_at": int(time.time()),
                    "error": {"message": last_error},
                    **(
                        {"user_msg_id": None, "assistant_msg_id": None}
                        if isinstance(e, ChatHistoryConflictError)
                        else {}
                    ),
                },
            )
            await _emit_subagent_terminal(
                parent_event_emitter,
                subagent_meta,
                status="error",
                message=last_error or "unknown error",
                user_id=user.id,
                parent_chat_id=parent_chat_id,
                parent_message_id=parent_message_id,
            )
            return (
                f"Subagent {target_num} ({target_name}) continue ERROR after retry: "
                f"{last_error or 'unknown error'}"
            )
    return f"Subagent {target_num} ({target_name}) continue ERROR: unreachable"


# ---------------------------------------------------------------------------
# User-initiated rerun (the "Redo this turn" / "Restart from beginning"
# buttons on a SubagentBlock in the parent chat UI).
# ---------------------------------------------------------------------------


def _find_subagent_entry(
    parent_chat, entry_key: str, preferred_message_id: Optional[str] = None
) -> tuple[Optional[str], Optional[str], Optional[dict]]:
    """Locate ``subagent_runs[entry_key]`` somewhere in the parent chat's
    message history. Returns ``(parent_message_id, canonical_key, entry_dict)``
    or ``(None, None, None)`` if not found.

    ``preferred_message_id`` is the message the caller *wants* rewritten. When
    that exact message carries the entry it is returned FIRST, before the
    whole-chat scan. This is load-bearing for "rewind & redo": after the rewind
    BOTH the original moved-on message AND the new sibling branch carry the SAME
    entry_key, and a bare history scan returns the OLDER (lower-sequence) message
    — which is the moved-on one, so the unconsumed guard re-trips and the rerun
    409s forever. Honoring the hint makes the rerun target the branch. When the
    hint is absent or doesn't carry the entry we fall back to the old tolerant
    behavior (the entry_key is unique enough across the chat to find cleanly even
    if the caller lost track of the message after a reload).

    The frontend derives the clicked ``entry_key`` as
    ``run.entry_key || run.subagent_id || run.chat_id || run.tool_call_id``
    (Chat.svelte / SubagentBlock.svelte), but the backend keys launches by
    bare ``subagent_id`` and continuations by ``subagent_id#tool_call_id``.
    So the value the frontend sends is NOT guaranteed to be the literal dict
    key. After an exact-key miss we fall back to the same alias resolver the
    placeholder-sync path uses (``subagent_id``/``chat_id``/``tool_call_id``
    → canonical key, ambiguity-safe) so a valid rerun never spuriously
    reports 'entry not found'. The returned ``canonical_key`` is ALWAYS the
    real dict key (not the alias the caller passed) so downstream writes
    (``write_entry_key``) land on the existing entry instead of forking a new
    orphan key."""
    history_messages = (
        ((parent_chat.chat if parent_chat else {}) or {}).get("history") or {}
    ).get("messages") or {}
    if not isinstance(history_messages, dict):
        return (None, None, None)

    def _match_in(msg) -> Optional[tuple[str, dict]]:
        """Resolve entry_key (exact key, then alias) within ONE message's run
        map. Returns (canonical_key, entry) or None."""
        runs = msg.get("subagent_runs") if isinstance(msg, dict) else None
        if not isinstance(runs, dict):
            return None
        if entry_key in runs:
            return (entry_key, runs[entry_key])
        canonical = _subagent_run_lookup_by_placeholder_id(msg).get(str(entry_key))
        if canonical and canonical in runs:
            return (canonical, runs[canonical])
        return None

    # Prefer the caller-supplied message when it actually carries the entry.
    if preferred_message_id:
        pref_msg = history_messages.get(preferred_message_id)
        if isinstance(pref_msg, dict):
            hit = _match_in(pref_msg)
            if hit:
                return (preferred_message_id, hit[0], hit[1])

    # Whole-chat exact-key scan first (cheaper + unambiguous)...
    for msg_id, msg in history_messages.items():
        if not isinstance(msg, dict):
            continue
        runs = msg.get("subagent_runs")
        if isinstance(runs, dict) and entry_key in runs:
            return (msg_id, entry_key, runs[entry_key])
    # ...then the alias resolver (subagent_id / chat_id / tool_call_id).
    for msg_id, msg in history_messages.items():
        if not isinstance(msg, dict):
            continue
        runs = msg.get("subagent_runs")
        if not isinstance(runs, dict):
            continue
        canonical = _subagent_run_lookup_by_placeholder_id(msg).get(str(entry_key))
        if canonical and canonical in runs:
            return (msg_id, canonical, runs[canonical])
    return (None, None, None)


def _find_launch_entry_for_subagent(
    parent_chat, subagent_id: str, preferred_message_id: Optional[str] = None
) -> tuple[Optional[str], Optional[str], Optional[dict]]:
    """Find the LAUNCH entry (``continuation`` not truthy) for a given
    ``subagent_id`` across all parent messages. Returns
    ``(parent_message_id, entry_key, entry_dict)`` or all-Nones if there
    isn't one (e.g. someone clicked Restart on a chat whose launch entry was
    somehow deleted — fall back to using the clicked entry's prompt
    instead, handled by the caller).

    ``preferred_message_id`` is honored FIRST for the same reason as
    ``_find_subagent_entry``: after a "rewind & redo" the launch entry exists on
    BOTH the moved-on message and the rewound sibling branch, and a bare scan
    would pick the older (moved-on) one, sending the from_launch redo to the
    wrong message."""
    history_messages = (
        ((parent_chat.chat if parent_chat else {}) or {}).get("history") or {}
    ).get("messages") or {}
    if not isinstance(history_messages, dict):
        return (None, None, None)

    def _launch_in(msg) -> Optional[tuple[str, dict]]:
        runs = msg.get("subagent_runs") if isinstance(msg, dict) else None
        if not isinstance(runs, dict):
            return None
        for k, run in runs.items():
            if not isinstance(run, dict):
                continue
            if run.get("subagent_id") != subagent_id:
                continue
            if not run.get("continuation"):
                return (k, run)
        return None

    if preferred_message_id:
        pref_msg = history_messages.get(preferred_message_id)
        if isinstance(pref_msg, dict):
            hit = _launch_in(pref_msg)
            if hit:
                return (preferred_message_id, hit[0], hit[1])

    for msg_id, msg in history_messages.items():
        hit = _launch_in(msg)
        if hit:
            return (msg_id, hit[0], hit[1])
    return (None, None, None)


class SubagentRerunBlockedError(ValueError):
    """Raised when a user-facing subagent rerun would rewrite history that a
    parent/subagent model turn already depends on.

    This is intentionally separate from generic ValueError so the HTTP router
    can return a clear 409 instead of spawning a background task that silently
    no-ops.
    """

    def __init__(self, message: str, code: str = "subagent_rerun_blocked"):
        super().__init__(message)
        self.code = code


def _rerun_blocked(message: str, code: str = "subagent_rerun_blocked") -> None:
    raise SubagentRerunBlockedError(message, code=code)


_SUBAGENT_SETUP_GRACE_SECONDS = 30


def _running_entry_may_be_in_setup(
    run: Any, *, now: Optional[int] = None
) -> bool:
    """True while a claimed run may not have appended its hidden blank leaf yet.

    There is an unavoidable short interval between the parent entry's atomic
    terminal→running claim and the hidden transcript transaction. Treating
    ``hidden leaf is idle`` as proof of a crash during that interval lets a
    second rerun/continue cancel the live claim and steal it. A bounded grace
    closes that false-stranded window; genuinely crashed entries become
    recoverable after it expires.
    """
    if not isinstance(run, dict):
        return False
    if run.get("status") != "running" or run.get("ended_at"):
        return False
    try:
        started_at = int(run.get("started_at"))
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else int(now)
    return 0 <= current - started_at <= _SUBAGENT_SETUP_GRACE_SECONDS


def _history_messages(parent_chat) -> dict:
    history = ((parent_chat.chat if parent_chat else {}) or {}).get("history") or {}
    messages = history.get("messages") or {}
    return messages if isinstance(messages, dict) else {}


def _block_if_other_running_turn(
    parent_chat, subagent_id: str, exclude_entry_key: str
) -> None:
    """Raise ``SubagentRerunBlockedError`` if ANY turn of ``subagent_id`` other
    than ``exclude_entry_key`` is still running (a launch OR a continuation, under
    a different entry_key, with status='running' and no ended_at).

    Used by ``from_launch`` reruns: replacing the whole hidden transcript while
    a continuation of the same subagent is mid-run pulls the transcript out from
    under that live task. The per-entry CAS protects only the launch entry; this
    covers siblings."""
    for _m_id, msg in _history_messages(parent_chat).items():
        runs = msg.get("subagent_runs") if isinstance(msg, dict) else None
        if not isinstance(runs, dict):
            continue
        for k, run in runs.items():
            if not isinstance(run, dict) or k == exclude_entry_key:
                continue
            if run.get("subagent_id") != subagent_id:
                continue
            if run.get("status") == "running" and not run.get("ended_at"):
                _rerun_blocked(
                    "This subagent still has a turn running — wait for it to "
                    "finish before restarting it from the beginning.",
                    code="subagent_already_running",
                )


def _block_if_recent_setup_turn(parent_chat, subagent_id: str) -> None:
    """Block while any turn has a fresh running claim but no hidden leaf yet."""
    for msg in _history_messages(parent_chat).values():
        runs = msg.get("subagent_runs") if isinstance(msg, dict) else None
        if not isinstance(runs, dict):
            continue
        for run in runs.values():
            if not isinstance(run, dict):
                continue
            if (run.get("subagent_id") or run.get("chat_id")) != subagent_id:
                continue
            if _running_entry_may_be_in_setup(run):
                _rerun_blocked(
                    "This subagent is starting another turn — wait for it to "
                    "finish before redoing it.",
                    code="subagent_already_running",
                )


async def _reconcile_idle_running_turns_for_subagent(
    parent_chat,
    subagent_chat,
    subagent_id: str,
) -> bool:
    """Resolve stale ``running`` entries for one subagent when its hidden chat is idle.

    ``subagent_continue`` must refuse while the hidden transcript is genuinely being
    mutated by another turn, but a crash/restart can leave an old parent
    ``subagent_runs`` entry at ``status='running'`` after the hidden assistant leaf is
    already terminal. Treating that stale row as live wedges future continues until a
    separate task-status poll happens to heal it. This helper performs the same
    liveness check as the rerun/poller paths: if the hidden chat is generating,
    return ``False`` so the caller blocks; otherwise terminalize every stale running
    entry for this subagent and return ``True`` so the new continue can proceed.
    """
    if _subagent_inner_chat_generating(subagent_chat):
        return False

    parent_chat_id = getattr(parent_chat, "id", None)
    if not parent_chat_id or str(parent_chat_id).startswith("local:"):
        return True

    # Do not mistake the claim→hidden-append setup window for a crashed task.
    # Scan first so we never terminalize older siblings and only later discover
    # that one fresh live claim should have blocked this continue altogether.
    for msg in _history_messages(parent_chat).values():
        runs = msg.get("subagent_runs") if isinstance(msg, dict) else None
        if not isinstance(runs, dict):
            continue
        for run in runs.values():
            if not isinstance(run, dict):
                continue
            if (run.get("subagent_id") or run.get("chat_id")) != subagent_id:
                continue
            if _running_entry_may_be_in_setup(run):
                return False

    for message_id, msg in _history_messages(parent_chat).items():
        runs = msg.get("subagent_runs") if isinstance(msg, dict) else None
        if not isinstance(runs, dict):
            continue
        for entry_key, run in runs.items():
            if not isinstance(run, dict):
                continue
            if (run.get("subagent_id") or run.get("chat_id")) != subagent_id:
                continue
            if run.get("status") != "running" or run.get("ended_at"):
                continue

            recovered_text = ""
            assistant_msg_id = run.get("assistant_msg_id")
            if assistant_msg_id:
                try:
                    recovered_text = (
                        await _extract_final_text(subagent_id, assistant_msg_id)
                    ) or ""
                except Exception:
                    recovered_text = ""
            await _terminalize_stranded_entry(
                str(parent_chat_id),
                str(message_id),
                str(entry_key),
                started_at=run.get("started_at"),
                final_text=recovered_text,
            )
    return True


def _history_current_id(parent_chat) -> Optional[str]:
    history = ((parent_chat.chat if parent_chat else {}) or {}).get("history") or {}
    current_id = history.get("currentId") if isinstance(history, dict) else None
    return current_id if isinstance(current_id, str) and current_id else None


async def load_effective_parent_chat_for_subagent_action(
    parent_chat_id: str, user
):
    """Load the visible parent chat targeted by a subagent action.

    The frontend normally sends the visible parent chat id. Older/stale UI state
    can accidentally send the hidden subagent chat id instead; that hidden chat
    has no ``subagent_runs`` map, so the rerun used to fail with
    ``subagent run entry '<subagent_id>' not found`` even though the run exists on
    the real parent. If the supplied chat is itself a subagent chat, follow its
    durable ``subagent_of`` pointer and use the real parent instead.
    """
    parent_chat = await Chats.get_chat_by_id_and_user_id(parent_chat_id, user.id)
    if parent_chat is None:
        raise ValueError("parent chat not accessible")

    subagent_of = getattr(parent_chat, "subagent_of", None) or None
    meta = getattr(parent_chat, "meta", None)
    if not subagent_of and isinstance(meta, dict):
        subagent_of = meta.get("subagent_of")
    if subagent_of:
        actual_parent = await Chats.get_chat_by_id_and_user_id(str(subagent_of), user.id)
        if actual_parent is not None:
            return str(subagent_of), actual_parent

    return parent_chat_id, parent_chat


def _resolve_subagent_rerun_context(
    parent_chat, parent_message_id: str, entry_key: str, scope: str
) -> dict:
    """Resolve the clicked rerun into the concrete parent entry that would be
    rewritten.

    For ``scope='this_turn'`` that is the clicked entry. For
    ``scope='from_launch'`` it is the original launch entry for the same
    subagent (even when the user clicked the menu on a continuation card).
    This helper has no side effects so both the router preflight and the
    background task can use it.
    """
    if scope not in ("this_turn", "from_launch"):
        raise ValueError(f"invalid rerun scope: {scope}")

    located_msg_id, canonical_key, target_entry = _find_subagent_entry(
        parent_chat, entry_key, preferred_message_id=parent_message_id
    )
    if target_entry is None:
        raise ValueError(f"subagent run entry '{entry_key}' not found")
    # Normalize to the real dict key. The frontend may have clicked with an
    # alias (tool_call_id / subagent_id) that is not the literal map key; the
    # ``this_turn`` path writes to ``write_entry_key = entry_key``, so using
    # the alias would fork a new orphan entry instead of updating the existing
    # one.
    if canonical_key:
        entry_key = canonical_key
    # When the located message differs from the caller's hint, prefer the located
    # one (self-heals a stale hint after a reload). The preferred-id lookup above
    # already keeps located_msg_id == parent_message_id whenever the hinted
    # message genuinely carries the entry, so the "rewind & redo" sibling branch
    # is never demoted back to the moved-on message here.
    if located_msg_id and located_msg_id != parent_message_id:
        parent_message_id = located_msg_id

    subagent_id = target_entry.get("subagent_id") or target_entry.get("chat_id")
    if not subagent_id:
        raise ValueError("subagent_id missing from entry")

    if scope == "from_launch":
        launch_msg_id, launch_key, launch_entry = _find_launch_entry_for_subagent(
            parent_chat, subagent_id, preferred_message_id=parent_message_id
        )
        if launch_entry is None:
            # Preserve the pre-existing fallback behavior, but the safety guard
            # below will still require that this exact entry is the latest
            # unresolved subagent turn before any mutation happens.
            launch_msg_id, launch_key, launch_entry = (
                parent_message_id,
                entry_key,
                target_entry,
            )
        write_msg_id = launch_msg_id or parent_message_id
        write_entry_key = launch_key or entry_key
        write_entry = launch_entry
        launch_prompt = (launch_entry.get("prompt") or "") if launch_entry else ""
        launch_background = (
            (launch_entry.get("background") or "") if launch_entry else ""
        )
        inner_prompt = launch_prompt
        if launch_background:
            inner_prompt = (
                f"{inner_prompt}\n\n<background>\n{launch_background}\n</background>"
            ).strip()
    else:
        write_msg_id = parent_message_id
        write_entry_key = entry_key
        write_entry = target_entry
        inner_prompt = (target_entry.get("prompt") or "") or ""
        if not inner_prompt:
            raise ValueError("entry has no stored prompt to re-run")

    return {
        "parent_message_id": parent_message_id,
        "target_entry": target_entry,
        "subagent_id": subagent_id,
        "write_msg_id": write_msg_id,
        "write_entry_key": write_entry_key,
        "write_entry": write_entry,
        "inner_prompt": inner_prompt,
        "scope": scope,
    }


_SUBAGENT_TOOL_NAMES = {"subagent_launch", "subagent_continue", "subagent_agent_launch"}


def _tool_call_id(call: dict) -> str:
    return str(call.get("id") or call.get("tool_call_id") or "")


def _subagent_tool_name(call: dict) -> str:
    return str((call.get("function") or {}).get("name") or "")


def reconcile_block_results_from_runs(content_blocks: list, subagent_runs: dict) -> bool:
    """Backfill missing/empty subagent tool results in ``content_blocks`` from the
    durable ``subagent_runs`` mirror so the canonical (model-bound) results array
    matches the source of truth.

    ``block["results"]`` is written by several racing writers (the live loop save,
    the per-completion placeholder sync, the frontend arrival-order handler). When
    a parent turn fans out many parallel subagents and is interrupted before the
    canonical save lands, the persisted results can be a partial subset even though
    every subagent finished and stamped its ``final_text`` into ``subagent_runs``.
    This makes the backend authoritative: for every subagent tool call whose result
    is missing or empty, patch in the finished run's ``final_text``.

    Mutates ``content_blocks`` in place. Returns True if anything changed.

    Keyed conservatively: a run is only used when it FINISHED (status done) with a
    non-empty ``final_text``. Match a call to a run by tool_call_id first (the only
    key that disambiguates launch vs a later continue on the same subagent), then by
    subagent_id for launch entries."""
    if not isinstance(content_blocks, list) or not isinstance(subagent_runs, dict):
        return False

    by_tool_call: dict = {}
    by_subagent: dict = {}
    for run in subagent_runs.values():
        if not isinstance(run, dict) or run.get("status") != "done":
            continue
        text = run.get("final_text")
        if not (isinstance(text, str) and text.strip()):
            continue
        text = text.strip()
        tcid = str(run.get("tool_call_id") or "")
        if tcid:
            by_tool_call[tcid] = (text, run)
        sid = str(run.get("subagent_id") or run.get("chat_id") or "")
        if sid and not run.get("continuation"):
            by_subagent[sid] = (text, run)
    if not by_tool_call and not by_subagent:
        return False

    changed = False
    for block in content_blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_calls":
            continue
        calls = block.get("content") if isinstance(block.get("content"), list) else []
        results = block.get("results") if isinstance(block.get("results"), list) else []
        result_by_id = {
            str(r.get("tool_call_id") or ""): r
            for r in results
            if isinstance(r, dict)
        }
        for call in calls:
            if not isinstance(call, dict):
                continue
            if _subagent_tool_name(call) not in _SUBAGENT_TOOL_NAMES:
                continue
            call_id = _tool_call_id(call)
            if not call_id:
                continue
            existing = result_by_id.get(call_id)
            existing_content = (
                existing.get("content") if isinstance(existing, dict) else None
            )
            if isinstance(existing_content, str) and existing_content.strip():
                continue  # already has a real answer
            if isinstance(existing, dict) and (
                existing.get("result_ref") or existing.get("result_lazy")
            ):
                # A large answer that was slimmed to a lazy ref — its real body
                # lives in the tool-result-body store, NOT here. The empty inline
                # content is expected; don't clobber it.
                continue

            recovered = by_tool_call.get(call_id)
            if recovered is None and _subagent_tool_name(call) != "subagent_continue":
                # by_subagent holds only LAUNCH answers. Recover a CONTINUATION only
                # by its exact tool_call_id (above), NEVER by subagent_id — else a
                # continuation whose own result was lost would be backfilled with the
                # LAUNCH's final_text, feeding the wrong answer to the parent (C18).
                sid = str(existing.get("subagent_id") or "") if isinstance(existing, dict) else ""
                if sid:
                    recovered = by_subagent.get(sid)
            if recovered is None:
                continue

            text, run = recovered
            if isinstance(existing, dict):
                existing["content"] = text
                if not existing.get("subagent_id") and run.get("subagent_id"):
                    existing["subagent_id"] = run.get("subagent_id")
            else:
                results.append(
                    {
                        "tool_call_id": call_id,
                        "content": text,
                        **(
                            {"subagent_id": run.get("subagent_id")}
                            if run.get("subagent_id")
                            else {}
                        ),
                    }
                )
                result_by_id[call_id] = results[-1]
            changed = True
        if changed:
            block["results"] = results
    return changed


async def sweep_subagent_runs_terminal(
    parent_chat_id: str,
    parent_message_id: str,
    *,
    fallback_status: str = "cancelled",
) -> bool:
    """Flip every NON-terminal ``subagent_runs`` entry on the parent message to a
    terminal status with ``ended_at`` stamped — atomically and idempotently.

    The invariant: once the parent message finalizes (clean / cancel / error), NO
    subagent_runs entry may remain ``status='running'`` (else its card spins
    "Researching…" forever, on this tab and after reload). Under heavy concurrent
    teardown a per-subagent terminal write can be dropped (a 2nd CancelledError
    lands in its await); this finalizer-side sweep is the authoritative backstop.

    Result-aware: prefers ``'done'`` when the run genuinely finished — it carries
    a non-empty ``final_text`` OR the parent ``content_blocks`` already hold a
    non-empty result for its ``tool_call_id`` — so a finished subagent whose own
    terminal write was lost is never mislabeled cancelled/error (and its
    ``final_text`` is backfilled from the result so the entry is self-consistent
    and the later reconcile can mirror it into content_blocks). Otherwise uses
    ``fallback_status`` (``'cancelled'`` on user-stop, ``'error'`` on a terminal
    generation error).

    Runs inside ``update_message_fields_atomic``'s mutator: re-reads the live map
    under the per-(chat,message) lock and only replaces the ``subagent_runs`` key,
    so it composes losslessly with the parent's separate done/error write and any
    in-flight sibling upsert. Call it BEFORE the finalizer's
    ``reconcile_block_results_from_runs`` so newly-``done`` runs get mirrored into
    content_blocks. Returns True if anything changed.
    """
    if not parent_chat_id or not parent_message_id:
        return False
    if str(parent_chat_id).startswith("local:"):
        return False

    _TERMINAL = {"done", "error", "cancelled"}
    holder = {"changed": False}

    def _mutator(existing: dict) -> Optional[dict]:
        runs = existing.get("subagent_runs")
        if not isinstance(runs, dict) or not runs:
            return None

        # A run whose answer already landed in content_blocks (but never flipped
        # its own status — the SHAPE C divergence) counts as finished.
        result_text_by_tcid: dict = {}
        for block in existing.get("content_blocks") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_calls":
                continue
            for r in block.get("results") or []:
                if not isinstance(r, dict):
                    continue
                # Skip error-flagged results: a failed subagent's result is an
                # ERROR STRING, not a real answer — it must NOT drive a 'done'
                # promotion (the legitimate SHAPE-C answer is never error-flagged).
                if r.get("error"):
                    continue
                c = r.get("content")
                if isinstance(c, str) and c.strip():
                    tcid = str(r.get("tool_call_id") or "")
                    if tcid:
                        result_text_by_tcid[tcid] = c.strip()

        now = int(time.time())
        new_runs = dict(runs)
        local_changed = False
        for key, run in runs.items():
            if not isinstance(run, dict):
                continue
            status = run.get("status")

            final_text = run.get("final_text")
            has_final = isinstance(final_text, str) and bool(final_text.strip())
            result_text = result_text_by_tcid.get(str(run.get("tool_call_id") or ""))

            if status not in _TERMINAL and run.get("rerun") is True:
                # Detached reruns have an independent registered task and a
                # generation-guarded terminal finalizer. They can overlap a
                # parent action in another tab, so the parent turn does not own
                # (and must not cancel/error) their live generation.
                continue
            if status not in _TERMINAL:
                # Terminalize a still-running run. It finished if it carries a real
                # final_text OR its answer already landed in content_blocks (the
                # SHAPE C divergence — a running run never has an ERROR result yet;
                # results are only written at completion). Else it's a genuine
                # straggler -> fallback (cancelled / error).
                target = "done" if (has_final or result_text) else fallback_status
            elif status in ("cancelled", "error") and has_final:
                # Promote a wrongly-downgraded FINISHED run back to 'done'. Gate on
                # final_text ONLY — it is set exclusively on the clean-completion
                # path, so it never mislabels a genuine cancel/error. Do NOT use
                # result_text here: an errored subagent's content_blocks result is
                # an ERROR STRING, not a real answer, and must stay 'error'.
                target = "done"
            else:
                # Genuine terminal (done, or a real cancel/error with no answer) —
                # keep the status; only stamp missing timestamps below.
                target = status

            needs_change = (
                status != target
                or run.get("ended_at") is None
                or run.get("started_at") is None
                or (target == "done" and not has_final and bool(result_text))
            )
            if not needs_change:
                continue

            patched = dict(run)
            patched["status"] = target
            if patched.get("ended_at") is None:
                patched["ended_at"] = now
            if patched.get("started_at") is None:
                # Never leave a terminal run without a start (else the card shows
                # "Researched" with no duration). Clamp to ended_at as a floor.
                patched["started_at"] = patched["ended_at"]
            if target == "done" and not has_final and result_text:
                patched["final_text"] = result_text
            new_runs[key] = patched
            local_changed = True

        if not local_changed:
            return None
        holder["changed"] = True
        return {"subagent_runs": new_runs}

    try:
        write_result = await Chats.update_message_fields_atomic(
            parent_chat_id, parent_message_id, _mutator
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "sweep_subagent_runs_terminal failed on "
            f"{parent_chat_id}/{parent_message_id}: {e}"
        )
        return False
    return bool(holder["changed"] and write_result)


async def broadcast_subagent_terminals(
    parent_chat_id: str,
    parent_message_id: str,
    user_id: Optional[str],
) -> None:
    """ROOT GUARANTEE for "a subagent's terminal state always reaches every viewing
    tab without a manual reload".

    The per-subagent live updates (content + the chat:done / error / cancel that
    resolves the card) are forwarded on a STREAM-SCOPED, visibility-gated path that
    a live/backgrounded/reconnected tab can silently miss — leaving the card on a
    permanent false "Researching…". Every prior fix (visibility bypass, robust
    routing, the frontend flip backstop, force_fanout reruns) was compensating for
    the absence of ONE authoritative, reliably-delivered terminal.

    This is that one delivery. It is called from the parent's finalizer (clean /
    error / cancel) right AFTER ``sweep_subagent_runs_terminal`` has made the durable
    ``subagent_runs`` authoritative (every entry terminal; 'done' runs carry
    ``final_text``) and BEFORE the parent's own ``chat:done``. It re-reads that
    truth and FANS each run's terminal out to EVERY one of the user's sessions via
    ``emit_user_fanout`` — which bypasses stream-room membership, primary election,
    the dead-origin window AND the visibility gate. The client's
    ``mergeSubagentPendingIntoRun`` folds each terminal in idempotently (a card the
    live wire already resolved is unaffected), so no card can be left spinning once
    the parent turn ends. Reruns are detached (no parent finalizer) and deliver
    their own force_fanout terminal instead."""
    if not user_id or not parent_chat_id or not parent_message_id:
        return
    if str(parent_chat_id).startswith("local:"):
        return
    if STREAM_PROTOCOL_VERSION != "v2.1":
        # v1 already fan-outs every event directly to the user's sessions, so the
        # per-update terminal isn't stream-scoped there — nothing to backstop.
        return
    try:
        msg = (
            await Chats.get_message_by_id_and_message_id(
                parent_chat_id, parent_message_id
            )
            or {}
        )
    except Exception:
        log.debug("broadcast_subagent_terminals: message read failed")
        return
    runs = msg.get("subagent_runs")
    if not isinstance(runs, dict) or not runs:
        return

    for entry_key, run in runs.items():
        if not isinstance(run, dict):
            continue
        status = run.get("status")
        if status not in ("done", "error", "cancelled"):
            continue
        if status == "done":
            inner_event: dict = {"type": "chat:done", "data": {}}
            ft = run.get("final_text")
            if isinstance(ft, str) and ft.strip():
                # Carry the answer so a card that missed every content update can
                # show it immediately without a fallback fetch.
                inner_event["data"]["final_text"] = ft
        elif status == "error":
            err = run.get("error")
            err_msg = (
                (err.get("message") if isinstance(err, dict) else None)
                or (err if isinstance(err, str) else None)
                or "Subagent failed."
            )
            inner_event = {"type": "chat:message:error", "data": {"error": err_msg}}
        else:  # cancelled
            inner_event = {"type": "chat:tasks:cancel"}

        subagent_meta = {
            "subagent_id": run.get("subagent_id") or run.get("chat_id"),
            "entry_key": run.get("entry_key") or entry_key,
            "parent_message_id": parent_message_id,
            "num": run.get("num"),
            "name": run.get("name"),
            "tool_call_id": run.get("tool_call_id"),
            "chat_id": run.get("chat_id") or run.get("subagent_id"),
            "continuation": bool(run.get("continuation")),
        }
        envelope = {
            "chat_id": parent_chat_id,
            "message_id": parent_message_id,
            "data": {
                "type": "chat:subagent:update",
                "data": {**subagent_meta, "inner_event": inner_event},
            },
        }
        try:
            await emit_user_fanout(user_id, envelope)
        except Exception as e:  # noqa: BLE001
            log.debug(f"subagent terminal broadcast failed for {entry_key}: {e}")


def _stranded_running_candidates(
    parent_chat, rerun_keys: set, live_rerun_sids: set = frozenset()
) -> list[dict]:
    """Pure scan: collect every ``subagent_runs`` entry across the parent chat's
    messages that is stuck ``status='running'`` (no ``ended_at``) and is NOT covered
    by a live detached rerun. The hidden-chat liveness check (an async DB read) is
    deferred to the orchestrator — this stays pure so it is trivially unit-testable.

    A live rerun is excluded TWO ways: by its literal ``entry_key`` (``rerun_keys``)
    AND by its ``subagent_id`` (``live_rerun_sids``). The second is load-bearing: a
    redo is registered in redis under the CLICKED key (which may be a CONTINUATION
    key ``{sid}#{tcid}``), but ``rerun_subagent_turn`` flips a DIFFERENT entry — the
    LAUNCH entry, keyed by the bare ``sid`` — to 'running' and wipes the shared hidden
    chat (``currentId=None``) during a multi-await setup window. Keying the skip only
    on the literal key would miss that live launch entry and the inner-chat-idle gate
    also passes mid-wipe, so reconcile would terminalize a genuinely in-flight rerun.
    A subagent's hidden chat is its single serialization point, so ANY live rerun of a
    subagent protects ALL of that subagent's entries. Returns descriptors with the ids
    the orchestrator needs to confirm staleness and recover the answer."""
    out: list[dict] = []
    for m_id, msg in _history_messages(parent_chat).items():
        runs = msg.get("subagent_runs") if isinstance(msg, dict) else None
        if not isinstance(runs, dict):
            continue
        for key, run in runs.items():
            if not isinstance(run, dict):
                continue
            if run.get("status") != "running" or run.get("ended_at"):
                continue
            if key in rerun_keys:
                continue
            sid = run.get("subagent_id") or run.get("chat_id")
            if not sid:
                continue
            if sid in live_rerun_sids:
                # A rerun of THIS subagent is live (under some entry key) — its
                # hidden chat is mid-mutation; never terminalize any of its entries.
                continue
            out.append(
                {
                    "message_id": m_id,
                    "entry_key": key,
                    "subagent_id": sid,
                    "assistant_msg_id": run.get("assistant_msg_id"),
                    "started_at": run.get("started_at"),
                }
            )
    return out


async def _terminalize_stranded_entry(
    parent_chat_id: str,
    parent_message_id: str,
    entry_key: str,
    *,
    started_at,
    final_text: str,
) -> bool:
    """Conditionally flip ONE stranded ``subagent_runs`` entry to terminal under the
    per-(chat, message) write lock. Re-reads the entry inside the lock and writes
    ONLY when it is STILL ``running`` with no ``ended_at`` AND ``started_at`` is
    unchanged — so a rerun that re-claimed the entry between the caller's idle check
    and this write (a fresh ``started_at``) always wins and is never stomped.

    Resolves to ``'done'`` (carrying the answer) when EITHER the freshly-scraped
    hidden-chat ``final_text`` OR the entry's OWN preserved ``final_text`` is
    non-empty, else ``'cancelled'`` (a truthful, durable "Stopped"). Consulting the
    entry's own ``final_text`` is load-bearing: a ``from_launch`` redo preserves the
    prior answer on the entry (C5) but WIPES the hidden chat, so after a task death
    the scrape returns '' while the real answer still lives on the entry — scraping
    alone would mislabel it 'cancelled' and hide an answer the user already had. This
    mirrors ``sweep_subagent_runs_terminal``'s ``has_final`` promotion and is safe
    because ``final_text`` is only ever written on a clean-completion path (a truly
    failed run carries none), so this can only promote a run that genuinely answered.
    Returns True if it wrote."""
    holder = {"changed": False}

    def _mutator(existing: dict) -> Optional[dict]:
        runs = existing.get("subagent_runs")
        if not isinstance(runs, dict):
            return None
        run = runs.get(entry_key)
        if not isinstance(run, dict):
            return None
        if run.get("status") != "running" or run.get("ended_at"):
            return None
        if started_at is not None and run.get("started_at") != started_at:
            # A rerun re-claimed this entry (new started_at) — let it run.
            return None
        # Prefer the freshly-scraped answer; fall back to the entry's own preserved
        # final_text (a from_launch redo that wiped the hidden chat keeps it here).
        existing_ft = run.get("final_text")
        effective_ft = (
            final_text
            if isinstance(final_text, str) and final_text.strip()
            else (existing_ft if isinstance(existing_ft, str) else "")
        )
        completed = bool(effective_ft.strip())
        new_runs = dict(runs)
        merged = {
            **run,
            "status": "done" if completed else "cancelled",
            "ended_at": int(time.time()),
        }
        if completed:
            merged["final_text"] = effective_ft
        new_runs[entry_key] = merged
        holder["changed"] = True
        update_data = {"subagent_runs": new_runs}
        if completed:
            # A recovered answer is not merely card metadata. Replace the
            # matching parent tool result in this same commit so reload and the
            # parent model cannot observe run=done with an older/error result.
            _apply_subagent_placeholder_patch(
                existing,
                merged,
                update_data,
                allow_append=False,
            )
        return update_data

    try:
        write_result = await Chats.update_message_subagent_run_atomic(
            parent_chat_id,
            parent_message_id,
            entry_key,
            _mutator,
            touch_chat=True,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            f"stranded subagent terminalize failed for "
            f"{parent_chat_id}/{parent_message_id}/{entry_key}: {e}"
        )
        return False
    return bool(holder.get("changed") and write_result)


async def reconcile_stranded_subagent_runs(
    parent_chat,
    *,
    parent_live: bool,
    live_rerun_entry_keys,
    user_id: Optional[str],
) -> int:
    """Durably self-heal ``subagent_runs`` entries stranded at ``status='running'``.

    A subagent entry stays 'running' forever when the task that owned it DIED before
    writing its terminal state — a server restart/crash (uncatchable), or historically
    a cancel that truncated an un-shielded terminal write. A DETACHED rerun has NO
    parent finalizer sweep, so nothing on the backend ever resolves it; the frontend
    only DISPLAYS it as "Stopped" (the isParentLive downgrade) without recovering the
    real answer or making the state durable. This is the missing server-side heal — it
    runs from the active-tasks poller (the natural liveness hook the client already
    calls on chat load).

    Liveness-gated so a genuinely in-flight run is NEVER stomped:
      - returns immediately when the parent turn is generating (``parent_live``) — its
        inline subagents are live and its own finalizer will sweep them;
      - skips an entry whose detached rerun task is live
        (``entry_key in live_rerun_entry_keys``);
      - skips an entry whose hidden chat is still generating
        (``_subagent_inner_chat_generating``).

    A confirmed-stranded entry is terminalized to ``'done'`` with the recovered
    ``final_text`` when its hidden chat actually produced an answer (so the answer is
    no longer lost), else ``'cancelled'``. Each healed terminal is broadcast to the
    user's tabs so a stuck card resolves without a manual reload. Returns the count
    healed."""
    if parent_chat is None or parent_live:
        return 0
    parent_chat_id = getattr(parent_chat, "id", None)
    if not parent_chat_id or str(parent_chat_id).startswith("local:"):
        return 0

    candidates = _stranded_running_candidates(
        parent_chat,
        set(live_rerun_entry_keys or []),
        # A redo's redis id (hence live_rerun_entry_keys) is the CLICKED key, but the
        # rerun may flip a DIFFERENT entry of the same subagent to 'running' (a
        # from_launch redo clicked on a continuation card writes the LAUNCH entry).
        # Derive the live subagent_ids so EVERY entry of a subagent with a live rerun
        # is protected — a continuation key is `{sid}#{tcid}`, a launch key is bare
        # `sid`, so the id is the part before '#'.
        {
            str(k).split("#", 1)[0]
            for k in (live_rerun_entry_keys or [])
            if k
        },
    )
    if not candidates:
        return 0

    healed = 0
    touched_message_ids: set = set()
    for cand in candidates:
        subagent_id = cand["subagent_id"]
        recovered_text = ""
        try:
            subagent_chat = await Chats.get_chat_by_id(subagent_id)
        except Exception:
            subagent_chat = None
        if subagent_chat is not None:
            if _subagent_inner_chat_generating(subagent_chat):
                # Genuinely live (its hidden chat is mid-turn) — not stranded.
                continue
            if cand.get("assistant_msg_id"):
                try:
                    recovered_text = (
                        await _extract_final_text(
                            subagent_id, cand["assistant_msg_id"]
                        )
                        or ""
                    )
                except Exception:
                    recovered_text = ""
        # subagent_chat is None ⇒ the hidden chat was deleted; terminalize to
        # 'cancelled' so the orphaned card still resolves.
        if await _terminalize_stranded_entry(
            parent_chat_id,
            cand["message_id"],
            cand["entry_key"],
            started_at=cand["started_at"],
            final_text=recovered_text,
        ):
            healed += 1
            touched_message_ids.add(cand["message_id"])

    if user_id:
        for mid in touched_message_ids:
            try:
                await broadcast_subagent_terminals(parent_chat_id, mid, user_id)
            except Exception:
                log.debug("stranded reconcile broadcast failed for %s", mid)
    return healed


async def reconcile_stranded_subagent_runs_by_chat_id(
    parent_chat_id: str,
    *,
    parent_live: bool,
    live_rerun_entry_keys,
    user_id: Optional[str],
) -> int:
    """Targeted chat-id entry point shared by both reload/task-state APIs."""
    if not parent_chat_id or parent_live:
        return 0
    messages = await Chats.get_messages_with_subagent_runs_by_chat_id(parent_chat_id)
    if not messages:
        return 0
    parent_chat = SimpleNamespace(
        id=parent_chat_id,
        chat={"history": {"messages": messages}},
    )
    return await reconcile_stranded_subagent_runs(
        parent_chat,
        parent_live=parent_live,
        live_rerun_entry_keys=live_rerun_entry_keys,
        user_id=user_id,
    )


def _run_placeholder_ids(entry_key: str, run: dict) -> set[str]:
    """Identifiers a synthetic placeholder may use for one subagent run."""
    ids = {
        str(v)
        for v in (
            entry_key,
            run.get("entry_key"),
            run.get("tool_call_id"),
        )
        if v
    }
    # Launch entries are keyed by subagent_id. Continuations share the same
    # subagent_id, so never use bare subagent_id as a continuation alias here.
    if not run.get("continuation"):
        ids.update(
            str(v)
            for v in (run.get("subagent_id"), run.get("chat_id"))
            if v
        )
    return ids


def _subagent_run_lookup_by_placeholder_id(parent_message: dict) -> dict[str, str]:
    runs = (
        parent_message.get("subagent_runs")
        if isinstance(parent_message, dict)
        else None
    )
    if not isinstance(runs, dict):
        return {}

    lookup: dict[str, str] = {}
    ambiguous: set[str] = set()
    for entry_key, run in runs.items():
        if not isinstance(run, dict):
            continue
        for identifier in _run_placeholder_ids(str(entry_key), run):
            if identifier in lookup and lookup[identifier] != str(entry_key):
                ambiguous.add(identifier)
                continue
            lookup[identifier] = str(entry_key)
    for identifier in ambiguous:
        lookup.pop(identifier, None)
    return lookup


def _duplicate_subagent_placeholder_block(
    parent_message: dict, prior_blocks: list[Any], block: Any
) -> bool:
    """True for rerun-created duplicate subagent placeholders.

    Older code could append a synthetic subagent tool_calls block after the
    canonical parent tool-call block. That block is not parent-model output and
    must not make future redos look consumed.
    """
    if not isinstance(block, dict) or block.get("type") != "tool_calls":
        return False
    calls = block.get("content") if isinstance(block.get("content"), list) else []
    if not calls:
        return False

    lookup = _subagent_run_lookup_by_placeholder_id(parent_message)
    if not lookup:
        return False

    prior_run_keys: set[str] = set()
    for prior in prior_blocks:
        if not isinstance(prior, dict) or prior.get("type") != "tool_calls":
            continue
        prior_calls = (
            prior.get("content") if isinstance(prior.get("content"), list) else []
        )
        for call in prior_calls:
            if not isinstance(call, dict):
                continue
            if _subagent_tool_name(call) not in _SUBAGENT_TOOL_NAMES:
                continue
            run_key = lookup.get(_tool_call_id(call))
            if run_key:
                prior_run_keys.add(run_key)

    if not prior_run_keys:
        return False

    for call in calls:
        if not isinstance(call, dict):
            return False
        if _subagent_tool_name(call) not in _SUBAGENT_TOOL_NAMES:
            return False
        run_key = lookup.get(_tool_call_id(call))
        if not run_key or run_key not in prior_run_keys:
            return False
    return True


def _block_is_pure_subagent_fanout(block: Any) -> bool:
    """True for a tool-call block that ONLY launches/continues subagents.

    During a parallel fan-out the parent emits one such block per subagent and
    never produces text/reasoning between them. A later sibling fan-out block is
    NOT the parent "continuing" from an earlier subagent's result — it carries no
    parent-authored text and (for non-reasoning rounds) no signed transcript
    state. Rewriting an earlier subagent result therefore stays valid, so these
    blocks must not make an earlier subagent look consumed.

    Signed parent reasoning is handled separately by the
    ``reasoning_details_per_round`` check in
    ``_validate_parent_subagent_result_unconsumed`` — if the parent signed any
    round after the target block, that guard still blocks the redo.
    """
    if not isinstance(block, dict) or block.get("type") != "tool_calls":
        return False
    calls = block.get("content") if isinstance(block.get("content"), list) else []
    if not calls:
        return False
    for call in calls:
        if not isinstance(call, dict):
            return False
        if _subagent_tool_name(call) not in _SUBAGENT_TOOL_NAMES:
            return False
    return True


def _block_has_meaningful_parent_output(block: Any) -> bool:
    """True when a block after the target tool-call block proves the parent
    model has already continued from that tool result.

    Empty text placeholders are created by the tool loop before the next parent
    model request; those are safe and intentionally ignored. Any real text,
    reasoning, code, or later tool-call block means the old tool result has
    become part of the parent transcript and must not be rewritten in place.
    """
    if not isinstance(block, dict):
        return bool(block)

    btype = block.get("type")
    content = block.get("content")

    if btype in {"text", "reasoning"}:
        if isinstance(content, str) and content.strip():
            return True
        return False

    if btype == "tool_calls":
        return bool(block.get("content") or block.get("results"))

    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, (list, dict)):
        return bool(content)
    return content is not None


def _find_parent_tool_call_block(
    parent_message: dict, run_entry: dict
) -> Optional[tuple[int, int]]:
    """Return ``(block_index, tool_round_number)`` for the parent tool-call
    block that owns this subagent run, or None when we cannot verify it.

    ``tool_round_number`` is 1-based and counts model emissions that produced
    tool calls up through the matched block. It lets us compare against
    ``reasoning_details_per_round`` to catch parent reasoning that started
    after the tool result even if no visible text was emitted before Stop.
    """
    blocks = (
        parent_message.get("content_blocks")
        if isinstance(parent_message, dict)
        else None
    )
    if not isinstance(blocks, list):
        return None

    wanted_tool_call_id = str(run_entry.get("tool_call_id") or "")
    wanted_subagent_id = str(
        run_entry.get("subagent_id") or run_entry.get("chat_id") or ""
    )
    fallback_match: Optional[tuple[int, int]] = None
    tool_round_number = 0

    for idx, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != "tool_calls":
            continue
        tool_round_number += 1
        calls = block.get("content") if isinstance(block.get("content"), list) else []
        results = block.get("results") if isinstance(block.get("results"), list) else []
        result_subagent_ids = {
            str(r.get("subagent_id") or "")
            for r in results
            if isinstance(r, dict) and r.get("subagent_id")
        }

        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = _tool_call_id(call)
            tool_name = ((call.get("function") or {}).get("name") or "")
            if wanted_tool_call_id and call_id == wanted_tool_call_id:
                return (idx, tool_round_number)
            if (
                not wanted_tool_call_id
                and tool_name in _SUBAGENT_TOOL_NAMES
                and wanted_subagent_id
                and wanted_subagent_id in result_subagent_ids
            ):
                if fallback_match is not None:
                    # Ambiguous without a tool_call_id; refuse to guess.
                    return None
                fallback_match = (idx, tool_round_number)

    return fallback_match


def _validate_parent_message_subagent_result_unconsumed(
    parent_message: dict, run_entry: dict
) -> None:
    """Guard message-local evidence that a subagent result was consumed.

    Keeping this part pure lets the atomic parent-message writer run the exact
    same validation against the row it has locked immediately before replacing
    a result. The chat-level wrapper below adds the selected-branch check used
    by ordinary preflight reads.
    """
    children = parent_message.get("childrenIds")
    if isinstance(children, list) and len(children) > 0:
        _rerun_blocked(
            "Cannot redo subagent: later chat turns already depend on this parent response.",
            code="subagent_parent_moved_on",
        )

    found = _find_parent_tool_call_block(parent_message, run_entry)
    if found is None:
        _rerun_blocked(
            "Cannot redo subagent safely: could not verify the parent tool-call position."
        )

    block_idx, tool_round_number = found
    blocks = parent_message.get("content_blocks") or []
    prior_blocks = blocks[: block_idx + 1]
    for later_block in blocks[block_idx + 1 :]:
        if _duplicate_subagent_placeholder_block(
            parent_message, prior_blocks, later_block
        ):
            continue
        # A later block that ONLY launches/continues subagents is a sibling
        # fan-out round, not the parent model continuing from this result. It
        # carries no parent-authored text and no signed transcript state, so it
        # must not make this earlier subagent look consumed. Signed parent
        # reasoning is still caught by the reasoning_details_per_round check
        # below.
        if _block_is_pure_subagent_fanout(later_block):
            continue
        if _block_has_meaningful_parent_output(later_block):
            _rerun_blocked(
                "Cannot redo subagent: the parent model already continued after this result.",
                code="subagent_parent_moved_on",
            )

    reasoning_rounds = parent_message.get("reasoning_details_per_round")
    if isinstance(reasoning_rounds, list) and len(reasoning_rounds) > tool_round_number:
        _rerun_blocked(
            "Cannot redo subagent: the parent model already started a later reasoning turn.",
            code="subagent_parent_moved_on",
        )


def _validate_parent_subagent_result_unconsumed(
    parent_chat, parent_message_id: str, run_entry: dict
) -> None:
    """Guard the provider transcript invariant.

    A subagent's final answer/error is the parent tool result. Rewriting that
    result in place is only safe while it is still the latest unresolved parent
    tool result. Once the parent model has produced any later output (or the
    user has continued the chat from that assistant message), closed providers'
    encrypted reasoning/signature state refers to the old result and must be
    preserved.
    """
    messages = _history_messages(parent_chat)
    parent_message = messages.get(parent_message_id)
    if not isinstance(parent_message, dict):
        _rerun_blocked("Cannot redo subagent: parent message is no longer available.")

    current_id = _history_current_id(parent_chat)
    if current_id and current_id != parent_message_id:
        _rerun_blocked(
            "Cannot redo subagent: the parent chat has already moved past this tool result.",
            code="subagent_parent_moved_on",
        )

    _validate_parent_message_subagent_result_unconsumed(
        parent_message, run_entry
    )


def _validate_subagent_turn_is_latest(subagent_chat, run_entry: dict) -> None:
    """Guard the hidden subagent transcript invariant.

    ``subagent_continue`` mutates the hidden subagent chat. Re-running an
    earlier launch/continue in place would invalidate every later continuation
    in that hidden transcript. Therefore the entry we are about to rewrite must
    be the hidden subagent chat's current leaf.
    """
    assistant_msg_id = run_entry.get("assistant_msg_id")
    if not assistant_msg_id:
        _rerun_blocked(
            "Cannot redo subagent safely: this run is missing retry metadata."
        )

    chat_data = (subagent_chat.chat if subagent_chat else {}) or {}
    history = chat_data.get("history") or {}
    messages = history.get("messages") or {}
    current_id = history.get("currentId")

    if current_id and current_id != assistant_msg_id:
        _rerun_blocked(
            "Cannot redo subagent: this is not the latest turn in the subagent conversation."
        )

    msg = messages.get(assistant_msg_id) if isinstance(messages, dict) else None
    if isinstance(msg, dict) and msg.get("childrenIds"):
        _rerun_blocked(
            "Cannot redo subagent: later subagent continuations already depend on this turn."
        )


def _validate_subagent_rerun_context(parent_chat, subagent_chat, ctx: dict) -> None:
    # C9: the hidden subagent chat MUST actually belong to THIS parent. Cloning a
    # chat copies subagent_runs verbatim (their subagent_id still references the
    # ORIGINAL parent's hidden chat), and a hand-crafted chat save can point an
    # entry's subagent_id at any hidden chat the user owns. Ownership alone
    # (get_chat_by_id_and_user_id) is NOT enough — without a parentage check a redo
    # from the clone would replace the ORIGINAL's transcript, corrupting another
    # legitimate chat. Assert subagent_chat.subagent_of == parent.
    parent_id = getattr(parent_chat, "id", None)
    sub_parent = getattr(subagent_chat, "subagent_of", None) if subagent_chat else None
    if not sub_parent and subagent_chat is not None:
        _meta = getattr(subagent_chat, "meta", None)
        if isinstance(_meta, dict):
            sub_parent = _meta.get("subagent_of")
    if not sub_parent or (parent_id and str(sub_parent) != str(parent_id)):
        _rerun_blocked(
            "Cannot redo subagent: this subagent does not belong to this chat "
            "(it may be a copy from a cloned conversation).",
            code="subagent_parent_mismatch",
        )
    write_entry = ctx.get("write_entry") or {}
    write_msg_id = ctx.get("write_msg_id") or ""
    _validate_parent_subagent_result_unconsumed(parent_chat, write_msg_id, write_entry)
    # The hidden-chat leaf guard only applies to ``this_turn``, which rewrites a
    # single user→assistant pair IN PLACE and would corrupt any later
    # continuation built on top of it. ``from_launch`` wipes the entire hidden
    # subagent history and marks sibling continuations stale, so requiring the
    # launch turn to still be the leaf is
    # self-contradictory — a launch that was later continued can never be its
    # own chat's leaf, yet restarting from it is exactly what the user asked for
    # and is safe after the wipe.
    if ctx.get("scope") != "from_launch":
        _validate_subagent_turn_is_latest(subagent_chat, write_entry)


def _subagent_inner_chat_generating(subagent_chat) -> bool:
    """True when the subagent's hidden chat is ACTIVELY generating right now — its
    current leaf is an assistant message that has not finished (``done`` falsy).

    The hidden chat is the transcript serialization point for a subagent: the
    launch and every continuation share it, and only one prepared turn can
    generate at a time. A freshly claimed rerun has a short setup interval before
    its blank assistant leaf is committed, so this signal must be combined with
    ``_block_if_recent_setup_turn`` rather than treated as authoritative by itself.
    Together they distinguish a genuinely in-flight rerun (which must be blocked)
    from one STRANDED at ``status='running'`` by a task death — a server restart / crash
    (an uncatchable SIGKILL) after the inner chat finished but before the parent
    run entry's terminal write. A detached rerun has no parent finalize sweep, so
    that stale 'running' never self-heals on the backend and would otherwise wedge
    redo FOREVER ("already running") even though nothing is running. When the inner
    chat is idle, the redo reconciles the stale entry instead of refusing."""
    chat_data = (subagent_chat.chat if subagent_chat else {}) or {}
    history = chat_data.get("history") if isinstance(chat_data, dict) else {}
    if not isinstance(history, dict):
        return False
    messages = history.get("messages")
    current_id = history.get("currentId")
    if not current_id or not isinstance(messages, dict):
        return False
    leaf = messages.get(current_id)
    if not isinstance(leaf, dict):
        return False
    return leaf.get("role") == "assistant" and not leaf.get("done")


async def validate_subagent_rerun_allowed(
    *, user, parent_chat_id: str, parent_message_id: str, entry_key: str, scope: str
) -> dict:
    """Public preflight used by the HTTP router before it creates the rerun
    background task. ``rerun_subagent_turn`` calls the same validation again
    immediately before mutating state so races are still caught.
    """
    parent_chat_id, parent_chat = await load_effective_parent_chat_for_subagent_action(
        parent_chat_id, user
    )
    ctx = _resolve_subagent_rerun_context(
        parent_chat, parent_message_id, entry_key, scope
    )
    subagent_chat = await Chats.get_chat_by_id_and_user_id(ctx["subagent_id"], user.id)
    if subagent_chat is None:
        raise ValueError("subagent chat not accessible")

    write_entry = ctx.get("write_entry") or ctx.get("target_entry") or {}
    # The "already running" / sibling-running guards are only meaningful when the
    # subagent's hidden chat is ACTUALLY generating. A 'running' entry whose inner
    # chat is idle is STRANDED (a task died before its terminal write — server
    # restart/crash, no finalize sweep for a detached rerun); refusing it would
    # wedge redo forever. When idle we fall through and let the redo reconcile the
    # stale entry under the CAS lock in rerun_subagent_turn.
    if _subagent_inner_chat_generating(subagent_chat):
        # Reject a redo of an entry that is genuinely mid-run (a second tab, or a
        # redo fired before the first finished) so two reruns can't interleave on
        # the same hidden chat and corrupt its history.
        if (
            isinstance(write_entry, dict)
            and write_entry.get("status") == "running"
            and not write_entry.get("ended_at")
        ):
            _rerun_blocked(
                "This subagent is currently running — wait for it to finish before redoing.",
                code="subagent_already_running",
            )
        # C7/P1_1: a redo mutates the subagent's shared hidden transcript
        # (from_launch WIPES it; this_turn REVERTS+re-appends a turn), so refuse it
        # while ANY OTHER turn of this subagent (a continuation, or a detached
        # rerun, under a different entry_key) is still running. Mirrors the atomic
        # guard in rerun_subagent_turn so the router can 409 before spawning the
        # task.
        _block_if_other_running_turn(
            parent_chat, ctx["subagent_id"], ctx.get("write_entry_key") or entry_key
        )
    else:
        _block_if_recent_setup_turn(parent_chat, ctx["subagent_id"])
    _validate_subagent_rerun_context(parent_chat, subagent_chat, ctx)
    return {
        "parent_chat_id": parent_chat_id,
        "parent_message_id": ctx.get("parent_message_id") or parent_message_id,
        "write_message_id": ctx.get("write_msg_id") or parent_message_id,
        "write_entry_key": ctx.get("write_entry_key") or entry_key,
        "subagent_id": ctx.get("subagent_id"),
    }


async def finalize_detached_rerun_claim(
    *,
    parent_chat_id: str,
    parent_message_id: str,
    entry_key: str,
    rerun_id: str,
    fallback_status: str,
    error_message: Optional[str] = None,
) -> bool:
    """Terminalize a detached rerun only if its exact claim is still running.

    The router invokes this from a shielded ``finally``. It covers cancellation
    or an unexpected exception in the small setup window after the parent entry
    was claimed but before the inner run's own terminal handlers became active.
    ``rerun_id`` makes cleanup generation-safe: an old task can never cancel a
    newer rerun that has already reclaimed the same entry.
    """
    if fallback_status not in ("error", "cancelled"):
        raise ValueError("invalid detached rerun fallback status")

    # If the hidden assistant actually completed and only the parent terminal
    # write was lost, recover that exact answer instead of downgrading a paid,
    # successful rerun to error/cancelled.
    recovered_final_text = ""
    try:
        parent_message = (
            await Chats.get_message_by_id_and_message_id(
                parent_chat_id, parent_message_id
            )
            or {}
        )
        run = (
            (parent_message.get("subagent_runs") or {}).get(entry_key)
            if isinstance(parent_message, dict)
            else None
        )
        if (
            isinstance(run, dict)
            and str(run.get("rerun_id") or "") == str(rerun_id)
            and run.get("status") == "running"
            and run.get("ended_at") is None
        ):
            subagent_id = run.get("subagent_id") or run.get("chat_id")
            # Only an assistant explicitly attached to THIS rerun claim can be
            # promoted. The ordinary assistant_msg_id may still point at the
            # prior answer during an early setup failure.
            assistant_message_id = run.get("rerun_assistant_msg_id")
            if subagent_id and assistant_message_id:
                hidden_message = (
                    await Chats.get_message_by_id_and_message_id(
                        str(subagent_id), str(assistant_message_id)
                    )
                    or {}
                )
                if (
                    hidden_message.get("role") == "assistant"
                    and hidden_message.get("done") is True
                    and not hidden_message.get("error")
                    and not hidden_message.get("userStopped")
                ):
                    recovered_final_text = (
                        await _extract_final_text(
                            str(subagent_id), str(assistant_message_id)
                        )
                        or ""
                    ).strip()
    except Exception:
        log.exception("detached rerun finalizer answer recovery failed")

    async def _write_terminal(
        target_status: str,
        *,
        final_text: str = "",
        terminal_error: Optional[str] = None,
    ) -> Optional[dict]:
        patch: dict = {
            "status": target_status,
            "ended_at": int(time.time()),
        }
        if final_text:
            patch["final_text"] = final_text
            patch["error"] = None
        elif target_status == "error":
            patch["error"] = {
                "message": (
                    terminal_error
                    or error_message
                    or "Subagent rerun ended before it could start."
                )
            }
        return await _upsert_subagent_run(
            parent_chat_id,
            parent_message_id,
            entry_key,
            patch,
            # A recovered answer is a real success commit: replace the old
            # parent tool result in the same transaction. Error/cancel fallback
            # only stops the spinner and intentionally keeps the last coherent
            # tool result.
            sync_placeholder=bool(final_text),
            allow_placeholder_append=False,
            expected_rerun_id=rerun_id,
            require_running=True,
            guard_parent_unconsumed=bool(final_text),
            require_parent_current=bool(final_text),
            require_parent_done=bool(final_text),
            # The terminal state and the root chat validator are one commit.
            touch_chat=True,
        )

    if recovered_final_text:
        try:
            merged = await _write_terminal(
                "done", final_text=recovered_final_text
            )
            return bool(merged and merged.get("status") == "done")
        except SubagentRerunBlockedError as blocked:
            # The hidden answer finished, but the parent consumed/moved before
            # it could be installed. It is no longer safe to replace the tool
            # result; it is still essential to close this exact rerun claim so
            # reload cannot leave a permanent spinner.
            log.info(
                "completed detached rerun %s could not be applied: %s",
                rerun_id,
                blocked,
            )
            merged = await _write_terminal(
                fallback_status,
                terminal_error=(
                    "The rerun finished, but its answer was not applied because "
                    "the parent chat changed before the result commit."
                ),
            )
            return bool(merged and merged.get("status") == fallback_status)

    merged = await _write_terminal(fallback_status)
    return bool(merged and merged.get("status") == fallback_status)


def _current_completed_subagent_result(
    subagent_chat, *, run_entry: Optional[dict] = None
) -> tuple[str, str, int]:
    """Return the hidden chat's selected, completed assistant answer.

    Opening a full subagent chat exposes the normal branch controls. A user can
    therefore rewind the failed assistant leaf and generate a successful sibling
    with a different model. The parent run still points at the original failed
    assistant id, so adoption must follow ``history.currentId`` rather than that
    stale id.

    Returns ``(assistant_message_id, final_text, timestamp)``. Raises ``ValueError``
    when the selected leaf is not a clean, completed assistant answer.
    """
    chat_data = (subagent_chat.chat if subagent_chat else {}) or {}
    history = chat_data.get("history") if isinstance(chat_data, dict) else None
    messages = history.get("messages") if isinstance(history, dict) else None
    current_id = history.get("currentId") if isinstance(history, dict) else None
    if not isinstance(messages, dict) or not isinstance(current_id, str) or not current_id:
        raise ValueError("The subagent chat has no selected result to use.")

    leaf = messages.get(current_id)
    if not isinstance(leaf, dict) or leaf.get("role") != "assistant":
        raise ValueError("The subagent chat's selected leaf is not an assistant answer.")
    if leaf.get("done") is not True:
        raise ValueError("The subagent chat's selected answer is still running.")
    if leaf.get("error"):
        raise ValueError("The subagent chat's selected answer failed.")
    if leaf.get("userStopped"):
        raise ValueError(
            "The subagent chat's selected answer was stopped before completion."
        )

    blocks = leaf.get("content_blocks")
    final_text = (
        _final_text_from_blocks_for_parent(blocks)
        if isinstance(blocks, list)
        else ""
    )
    if not final_text:
        content = leaf.get("content")
        final_text = content.strip() if isinstance(content, str) else ""
    if not final_text:
        raise ValueError("The subagent chat's selected answer is empty.")

    # A hidden chat can contain several launch/continue turns. "Use latest
    # answer" is allowed to follow a manually-created ASSISTANT SIBLING for the
    # clicked turn, but it must never copy the selected answer from a DIFFERENT
    # user→assistant turn (for example, a later subagent_continue) into this
    # parent's tool result. The launch-owned user_msg_id is the stable turn
    # identity; rewinding/regenerating the assistant preserves that parentId.
    if isinstance(run_entry, dict):
        expected_user_message_id = str(run_entry.get("user_msg_id") or "")
        if not expected_user_message_id:
            original_assistant_id = str(run_entry.get("assistant_msg_id") or "")
            original_assistant = (
                messages.get(original_assistant_id)
                if original_assistant_id and isinstance(messages, dict)
                else None
            )
            if isinstance(original_assistant, dict):
                expected_user_message_id = str(
                    original_assistant.get("parentId") or ""
                )
        if (
            expected_user_message_id
            and str(leaf.get("parentId") or "") != expected_user_message_id
        ):
            raise ValueError(
                "The subagent chat's selected answer belongs to a different "
                "subagent turn. Select a repaired sibling of the clicked turn."
            )

    timestamp = leaf.get("timestamp")
    try:
        timestamp = int(timestamp)
    except (TypeError, ValueError):
        timestamp = int(time.time())
    return current_id, final_text, timestamp


async def adopt_subagent_current_result(
    *,
    user,
    parent_chat_id: str,
    parent_message_id: str,
    entry_key: str,
) -> dict:
    """Adopt the selected answer from a manually repaired hidden subagent chat.

    This is the non-generating counterpart to ``rerun_subagent_turn``. It is used
    after the user opens the full subagent chat, rewinds a failed assistant leaf,
    and successfully regenerates it with another model.

    The same parent transcript guard as redo is enforced. If the parent already
    consumed the failed result, the caller must first create a rewind sibling and
    target that new message; rewriting the consumed branch in place would corrupt
    the provider transcript.
    """
    parent_chat_id, parent_chat = await load_effective_parent_chat_for_subagent_action(
        parent_chat_id, user
    )
    located_msg_id, canonical_key, run_entry = _find_subagent_entry(
        parent_chat, entry_key, preferred_message_id=parent_message_id
    )
    if run_entry is None:
        raise ValueError(f"subagent run entry '{entry_key}' not found")
    if located_msg_id:
        parent_message_id = located_msg_id
    if canonical_key:
        entry_key = canonical_key

    subagent_id = run_entry.get("subagent_id") or run_entry.get("chat_id")
    if not subagent_id:
        raise ValueError("subagent_id missing from entry")
    subagent_chat = await Chats.get_chat_by_id_and_user_id(subagent_id, user.id)
    if subagent_chat is None:
        raise ValueError("subagent chat not accessible")

    # Ownership is not enough: a copied/forged parent run must never be able to
    # import from a hidden chat belonging to another parent conversation.
    sub_parent = getattr(subagent_chat, "subagent_of", None) or None
    if not sub_parent:
        sub_meta = getattr(subagent_chat, "meta", None)
        if isinstance(sub_meta, dict):
            sub_parent = sub_meta.get("subagent_of")
    if str(sub_parent or "") != str(parent_chat_id):
        _rerun_blocked(
            "Cannot use this answer: the subagent does not belong to this chat.",
            code="subagent_parent_mismatch",
        )

    # Follow the child chat's currently selected branch, not the assistant id
    # captured when the original launch failed.
    assistant_msg_id, final_text, child_timestamp = (
        _current_completed_subagent_result(subagent_chat, run_entry=run_entry)
    )

    # Re-read immediately before the write so a concurrently continued parent
    # is caught. This mirrors the rerun success-path guard.
    fresh_parent = (
        await Chats.get_chat_by_id_and_user_id(parent_chat_id, user.id) or parent_chat
    )
    fresh_msg_id, fresh_key, fresh_entry = _find_subagent_entry(
        fresh_parent, entry_key, preferred_message_id=parent_message_id
    )
    if fresh_entry is None:
        raise ValueError(f"subagent run entry '{entry_key}' not found")
    if fresh_msg_id:
        parent_message_id = fresh_msg_id
    if fresh_key:
        entry_key = fresh_key
    _validate_parent_subagent_result_unconsumed(
        fresh_parent, parent_message_id, fresh_entry
    )

    now = int(time.time())
    merged = await _upsert_subagent_run(
        parent_chat_id,
        parent_message_id,
        entry_key,
        {
            "subagent_id": subagent_id,
            "chat_id": subagent_id,
            "entry_key": entry_key,
            "status": "done",
            "error": None,
            "final_text": final_text,
            # Keep the launch-owned assistant_msg_id/user_msg_id pair intact.
            # A manual rewind creates an assistant SIBLING under the original
            # user message; pretending that sibling was the launch-owned pair
            # would make a future "redo last turn" delete the shared user and
            # orphan the other branches. The separate provenance id lets the
            # leaf guard reject that unsafe narrow redo, while "restart from
            # beginning" remains safe because it wipes the whole hidden chat.
            "adopted_assistant_msg_id": assistant_msg_id,
            "ended_at": now,
            "stale": False,
            "rerun": False,
            "adopted_from_child": True,
            "adopted_at": now,
            "adopted_child_timestamp": child_timestamp,
        },
        allow_placeholder_append=False,
        guard_parent_unconsumed=True,
        require_parent_current=True,
        require_parent_done=True,
        touch_chat=True,
    )
    if not isinstance(merged, dict):
        raise RuntimeError("Could not save the repaired subagent answer.")
    return {
        "parent_chat_id": parent_chat_id,
        "parent_message_id": parent_message_id,
        "entry_key": entry_key,
        "run": merged,
    }


def _resolve_rewind_adopt_entries(
    source_message: dict, requested_entry_keys: list[str]
) -> list[tuple[str, dict]]:
    """Resolve requested aliases inside exactly one source parent message.

    Rewind siblings intentionally reuse run ids, so a whole-chat lookup is
    unsafe here. Resolution is exact-key first, then the existing
    ambiguity-safe placeholder alias map, and canonical keys are de-duplicated.
    """
    runs = source_message.get("subagent_runs")
    if not isinstance(runs, dict):
        raise ValueError("The source parent message has no subagent runs.")
    alias_lookup = _subagent_run_lookup_by_placeholder_id(source_message)

    resolved: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for requested in requested_entry_keys:
        requested = str(requested or "")
        if not requested:
            continue
        canonical = requested if requested in runs else alias_lookup.get(requested)
        run = runs.get(canonical) if canonical else None
        if not canonical or not isinstance(run, dict):
            raise ValueError(f"subagent run entry '{requested}' not found")
        if canonical in seen:
            continue
        seen.add(canonical)
        resolved.append((canonical, run))
    if not resolved:
        raise ValueError("No subagent answers were selected.")
    return resolved


def _rewind_current_id_for_source(parent_chat, source_message_id: str) -> str:
    """Return the guarded selected leaf when ``source_message_id`` is on it.

    A subagent card from an older turn remains visible in the selected
    transcript. Repairing it should branch at that turn and preserve all later
    work on the old branch, not fail merely because the source is an ancestor
    rather than the leaf. Off-branch cards remain invalid.
    """
    messages = _history_messages(parent_chat)
    current_id = _history_current_id(parent_chat)
    if not current_id or current_id not in messages:
        _rerun_blocked(
            "The parent chat has no valid selected branch to rewind.",
            code="rewind_parent_invalid",
        )
    if source_message_id not in _branch_message_ids(parent_chat, current_id):
        _rerun_blocked(
            "The subagent result is not on the parent chat's selected branch.",
            code="rewind_source_off_branch",
        )
    return current_id


def _prepare_rewind_subagent_branch_base(
    *,
    source_message: dict,
    source_message_id: str,
    branch_message_id: str,
    resolved_entries: list[tuple[str, dict]],
    operation_id: str,
    operation_kind: str,
) -> tuple[dict, list[tuple[str, dict, int]], dict[str, dict]]:
    """Build the common guarded rewind sibling used by adopt and rerun.

    The source is cut immediately after the latest selected subagent fan-out,
    with cross-round parent output rejected. Only runs represented in that
    prefix survive, and every surviving run is rebound to the new parent
    message id. No persistence occurs here.
    """
    blocks = source_message.get("content_blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("The source parent response has no structured tool history.")

    located: list[tuple[str, dict, int]] = []
    for entry_key, run in resolved_entries:
        found = _find_parent_tool_call_block(source_message, run)
        if found is None:
            raise ValueError(
                f"Cannot locate subagent run '{entry_key}' in the parent tool history."
            )
        located.append((entry_key, run, found[0]))

    earliest_idx = min(item[2] for item in located)
    latest_idx = max(item[2] for item in located)
    if earliest_idx != latest_idx:
        prior_blocks = blocks[: earliest_idx + 1]
        for block in blocks[earliest_idx + 1 : latest_idx + 1]:
            if _duplicate_subagent_placeholder_block(
                source_message, prior_blocks, block
            ):
                continue
            if _block_is_pure_subagent_fanout(block):
                continue
            if _block_has_meaningful_parent_output(block):
                raise ValueError(
                    "The selected subagents ran in different parent rounds; "
                    "repair them one round at a time."
                )

    kept_blocks = sanitize_content_blocks(
        copy.deepcopy(blocks[: latest_idx + 1])
    )
    kept_tool_call_ids: set[str] = set()
    kept_subagent_ids: set[str] = set()
    for block in kept_blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_calls":
            continue
        for call in block.get("content") or []:
            if isinstance(call, dict):
                call_id = _tool_call_id(call)
                if call_id:
                    kept_tool_call_ids.add(call_id)
        for result in block.get("results") or []:
            if isinstance(result, dict) and result.get("subagent_id"):
                kept_subagent_ids.add(str(result["subagent_id"]))

    source_runs = source_message.get("subagent_runs") or {}
    surviving_runs: dict[str, dict] = {}
    for entry_key, run in source_runs.items():
        if not isinstance(run, dict):
            continue
        tool_call_id = str(run.get("tool_call_id") or "")
        subagent_id = str(run.get("subagent_id") or run.get("chat_id") or "")
        if (
            tool_call_id and tool_call_id in kept_tool_call_ids
        ) or (
            subagent_id and subagent_id in kept_subagent_ids
        ):
            surviving_runs[str(entry_key)] = {
                **copy.deepcopy(run),
                "parent_message_id": branch_message_id,
            }

    target_model = str(
        source_message.get("selectedModelId") or source_message.get("model") or ""
    )
    if not target_model:
        raise ValueError("The source parent response has no model.")

    kept_tool_rounds = sum(
        1
        for block in kept_blocks
        if isinstance(block, dict) and block.get("type") == "tool_calls"
    )
    # The next parent stream needs a stable append target. Until that stream is
    # actually launched, this committed checkpoint is terminal/durable rather
    # than a phantom in-flight message.
    kept_blocks.append({"type": "text", "content": ""})
    now = int(time.time())
    branch: dict = {
        "id": branch_message_id,
        "parentId": source_message.get("parentId"),
        "childrenIds": [],
        "role": "assistant",
        "content": "",
        "model": target_model,
        "modelName": source_message.get("modelName") or target_model,
        "modelIdx": source_message.get("modelIdx", 0),
        "timestamp": now,
        "content_blocks": kept_blocks,
        "subagent_runs": surviving_runs,
        "done": True,
        "error": None,
        "userStopped": False,
        "rewind_operation": {
            "id": operation_id,
            "source_message_id": source_message_id,
            "kind": operation_kind,
        },
    }
    reasoning_rounds = source_message.get("reasoning_details_per_round")
    if isinstance(reasoning_rounds, list):
        branch["reasoning_details_per_round"] = copy.deepcopy(
            reasoning_rounds[:kept_tool_rounds]
        )

    return branch, located, surviving_runs


def _prepare_rewind_adopt_branch(
    *,
    source_message: dict,
    source_message_id: str,
    branch_message_id: str,
    resolved_entries: list[tuple[str, dict]],
    completed_results: dict[str, tuple[str, str, int]],
    operation_id: str,
) -> dict:
    """Build one internally coherent rewind sibling with repaired answers.

    Every selected run and its matching tool result is patched into the common
    rewind checkpoint before ``append_rewind_branch_atomic`` may commit it.
    """
    branch, located, surviving_runs = _prepare_rewind_subagent_branch_base(
        source_message=source_message,
        source_message_id=source_message_id,
        branch_message_id=branch_message_id,
        resolved_entries=resolved_entries,
        operation_id=operation_id,
        operation_kind="subagent_adopt",
    )

    now = int(time.time())
    for entry_key, prior_run, _block_idx in located:
        if entry_key not in surviving_runs:
            raise ValueError(
                f"Subagent run '{entry_key}' did not survive the requested rewind."
            )
        selected = completed_results.get(entry_key)
        if selected is None:
            raise ValueError(f"No completed repaired answer for '{entry_key}'.")
        assistant_message_id, final_text, child_timestamp = selected
        subagent_id = str(
            prior_run.get("subagent_id") or prior_run.get("chat_id") or ""
        )
        merged_run = {
            **surviving_runs[entry_key],
            "subagent_id": subagent_id,
            "chat_id": subagent_id,
            "entry_key": entry_key,
            "parent_message_id": branch_message_id,
            "status": "done",
            "error": None,
            "final_text": final_text,
            "adopted_assistant_msg_id": assistant_message_id,
            "ended_at": now,
            "stale": False,
            "rerun": False,
            "adopted_from_child": True,
            "adopted_at": now,
            "adopted_child_timestamp": child_timestamp,
        }
        surviving_runs[entry_key] = merged_run
        placeholder_patch: dict = {}
        _apply_subagent_placeholder_patch(
            branch,
            merged_run,
            placeholder_patch,
            allow_append=False,
        )
        branch.update(placeholder_patch)

    return branch


def _prepare_rewind_rerun_branch(
    *,
    source_message: dict,
    source_message_id: str,
    branch_message_id: str,
    resolved_entries: list[tuple[str, dict]],
    operation_id: str,
) -> dict:
    """Build a terminal rewind checkpoint before detached reruns start.

    The selected entries still carry their prior terminal answers in this
    checkpoint. Each rerun claims and replaces its own key afterward. If no
    rerun launches, the durable sibling remains a coherent branch instead of a
    half-created local phantom.
    """
    branch, located, surviving_runs = _prepare_rewind_subagent_branch_base(
        source_message=source_message,
        source_message_id=source_message_id,
        branch_message_id=branch_message_id,
        resolved_entries=resolved_entries,
        operation_id=operation_id,
        operation_kind="subagent_rerun",
    )
    for entry_key, _run, _block_idx in located:
        if entry_key not in surviving_runs:
            raise ValueError(
                f"Subagent run '{entry_key}' did not survive the requested rewind."
            )
    return branch


async def rewind_adopt_subagent_current_results(
    *,
    user,
    parent_chat_id: str,
    source_parent_message_id: str,
    branch_message_id: str,
    entry_keys: list[str],
    operation_id: Optional[str] = None,
) -> dict:
    """Atomically rewind one parent branch and adopt several repaired answers.

    All hidden leaves are validated before any write. Their chat/message
    revisions and the source parent message revision are then guarded again by
    ``append_rewind_branch_atomic`` under row locks. A failed preflight or
    conflict therefore creates no branch, and a successful commit can never
    contain a mixture of old errors and newly-adopted runs.
    """
    parent_chat_id, parent_chat = await load_effective_parent_chat_for_subagent_action(
        parent_chat_id, user
    )
    operation_id = str(operation_id or branch_message_id)
    messages = _history_messages(parent_chat)
    source_message = messages.get(source_parent_message_id)
    if not isinstance(source_message, dict):
        raise ValueError("The source parent message is no longer available.")
    expected_current_id = _rewind_current_id_for_source(
        parent_chat, source_parent_message_id
    )
    if source_message.get("done") is not True:
        raise ValueError("Stop the parent agent before using repaired answers.")

    normalized_keys = [str(key or "") for key in entry_keys if str(key or "")]
    resolved_entries = _resolve_rewind_adopt_entries(source_message, normalized_keys)

    child_ids = sorted(
        {
            str(run.get("subagent_id") or run.get("chat_id") or "")
            for _entry_key, run in resolved_entries
            if run.get("subagent_id") or run.get("chat_id")
        }
    )
    if len(child_ids) == 0:
        raise ValueError("The selected runs have no hidden subagent chats.")

    loaded_children = await asyncio.gather(
        *(Chats.get_chat_by_id_and_user_id(child_id, user.id) for child_id in child_ids)
    )
    child_by_id = {
        child_id: child
        for child_id, child in zip(child_ids, loaded_children)
        if child is not None
    }
    if set(child_by_id) != set(child_ids):
        raise ValueError("A selected subagent chat is not accessible.")

    completed_results: dict[str, tuple[str, str, int]] = {}
    expected_related_leaves: dict[str, dict] = {}
    for entry_key, run in resolved_entries:
        child_id = str(run.get("subagent_id") or run.get("chat_id") or "")
        child = child_by_id.get(child_id)
        if child is None:
            raise ValueError("A selected subagent chat is not accessible.")
        child_parent = getattr(child, "subagent_of", None) or (
            getattr(child, "meta", {}) or {}
        ).get("subagent_of")
        if str(child_parent or "") != parent_chat_id:
            _rerun_blocked(
                "Cannot use this answer: the subagent does not belong to this chat.",
                code="subagent_parent_mismatch",
            )
        selected = _current_completed_subagent_result(child, run_entry=run)
        completed_results[entry_key] = selected
        leaf_id = selected[0]
        leaf = _history_messages(child).get(leaf_id) or {}
        expected_related_leaves[child_id] = {
            "chat_id": child_id,
            "message_id": leaf_id,
            "message_rev": leaf.get("_rev"),
        }

    validators = await asyncio.gather(
        Chats.get_chat_open_validator(parent_chat_id, user.id),
        *(Chats.get_chat_open_validator(child_id, user.id) for child_id in child_ids),
    )
    parent_validator = validators[0]
    if not isinstance(parent_validator, dict):
        raise ValueError("The parent chat is no longer accessible.")
    for child_id, validator in zip(child_ids, validators[1:]):
        if not isinstance(validator, dict):
            raise ValueError("A selected subagent chat is no longer accessible.")
        expected_related_leaves[child_id]["chat_rev"] = validator.get("xmin")
        if validator.get("current_id") != expected_related_leaves[child_id]["message_id"]:
            raise ValueError(
                "A selected subagent answer changed during repair preflight."
            )

    branch = _prepare_rewind_adopt_branch(
        source_message=source_message,
        source_message_id=source_parent_message_id,
        branch_message_id=branch_message_id,
        resolved_entries=resolved_entries,
        completed_results=completed_results,
        operation_id=operation_id,
    )
    expected_source_rev = source_message.get("_rev")
    if expected_source_rev is None:
        # Legacy chat messages share the chat row's xmin.
        expected_source_rev = parent_validator.get("xmin")

    committed = await Chats.append_rewind_branch_atomic(
        parent_chat_id,
        source_parent_message_id,
        branch,
        user_id=user.id,
        expected_source_rev=expected_source_rev,
        expected_current_id=expected_current_id,
        expected_related_leaves=list(expected_related_leaves.values()),
        operation_id=operation_id,
    )
    committed_message = copy.deepcopy(committed.get("message") or branch)
    # Large lazy tool bodies were carried row-to-row by the atomic DB primitive.
    # They are not needed by the card/store response and must not be echoed.
    committed_message.pop("tool_result_bodies", None)
    committed_runs = committed_message.get("subagent_runs") or {}
    adoptions = [
        {
            "status": True,
            "parent_chat_id": parent_chat_id,
            "parent_message_id": branch_message_id,
            "entry_key": entry_key,
            "run": committed_runs.get(entry_key),
        }
        for entry_key, _run in resolved_entries
        if isinstance(committed_runs.get(entry_key), dict)
    ]
    return {
        "parent_chat_id": parent_chat_id,
        "source_parent_message_id": source_parent_message_id,
        "parent_message_id": branch_message_id,
        "branch_message": committed_message,
        "adoptions": adoptions,
        "entry_keys": [entry_key for entry_key, _run in resolved_entries],
        "idempotent": bool(committed.get("idempotent")),
        "updated_at": committed.get("updated_at"),
    }


async def rewind_subagent_runs_for_rerun(
    *,
    user,
    parent_chat_id: str,
    source_parent_message_id: str,
    branch_message_id: str,
    entry_keys: list[str],
    operation_id: Optional[str] = None,
) -> dict:
    """Atomically create the parent checkpoint used by rewind-and-redo.

    This replaces the old frontend append-message + pointer PATCH. The source
    row revision and selected current branch are guarded under the same
    transaction that appends the sibling, updates the graph, copies lazy tool
    bodies, and advances ``currentId``. A failed or ambiguous request therefore
    cannot leave a client-only branch or a partially persisted graph.
    """
    parent_chat_id, parent_chat = await load_effective_parent_chat_for_subagent_action(
        parent_chat_id, user
    )
    operation_id = str(operation_id or branch_message_id)
    messages = _history_messages(parent_chat)
    source_message = messages.get(source_parent_message_id)
    if not isinstance(source_message, dict):
        raise ValueError("The source parent message is no longer available.")
    expected_current_id = _rewind_current_id_for_source(
        parent_chat, source_parent_message_id
    )
    if source_message.get("done") is not True:
        raise ValueError("Stop the parent agent before redoing subagents.")

    normalized_keys = [str(key or "") for key in entry_keys if str(key or "")]
    resolved_entries = _resolve_rewind_adopt_entries(source_message, normalized_keys)
    parent_validator = await Chats.get_chat_open_validator(parent_chat_id, user.id)
    if not isinstance(parent_validator, dict):
        raise ValueError("The parent chat is no longer accessible.")

    branch = _prepare_rewind_rerun_branch(
        source_message=source_message,
        source_message_id=source_parent_message_id,
        branch_message_id=branch_message_id,
        resolved_entries=resolved_entries,
        operation_id=operation_id,
    )
    expected_source_rev = source_message.get("_rev")
    if expected_source_rev is None:
        expected_source_rev = parent_validator.get("xmin")

    committed = await Chats.append_rewind_branch_atomic(
        parent_chat_id,
        source_parent_message_id,
        branch,
        user_id=user.id,
        expected_source_rev=expected_source_rev,
        expected_current_id=expected_current_id,
        operation_id=operation_id,
    )
    committed_message = copy.deepcopy(committed.get("message") or branch)
    committed_message.pop("tool_result_bodies", None)
    return {
        "parent_chat_id": parent_chat_id,
        "source_parent_message_id": source_parent_message_id,
        "parent_message_id": branch_message_id,
        "branch_message": committed_message,
        "entry_keys": [entry_key for entry_key, _run in resolved_entries],
        "idempotent": bool(committed.get("idempotent")),
        "updated_at": committed.get("updated_at"),
    }


def _next_rerun_attempt(run: Optional[dict]) -> int:
    """Return the next monotonic UI ordering generation for one run entry."""
    try:
        return max(0, int((run or {}).get("rerun_attempt") or 0)) + 1
    except (TypeError, ValueError):
        return 1


async def rerun_subagent_turn(
    *,
    request: Request,
    user,
    parent_chat_id: str,
    parent_message_id: str,
    session_id: str,
    entry_key: str,
    scope: str,
    rerun_id: Optional[str] = None,
) -> None:
    """Re-run a subagent turn from the user-facing redo button.

    Two scopes:
    - ``"this_turn"``: revert just the user→assistant pair this entry
      represents and re-run with the entry's stored prompt. Other turns of
      the same subagent stay intact.
    - ``"from_launch"``: wipe the whole subagent chat history and re-run
      from the original launch entry's prompt + background. Any continue
      entries for the same subagent are now logically stale — for v1 we
      leave their stored ``final_text`` alone in the parent message so the
      user can still see what they previously got, but mark them with
      ``stale=True`` so the UI can grey them out if it wants.

    Emits ``chat:subagent:start`` and forwarded ``chat:subagent:update``
    events scoped to the parent chat / message so the existing live-update
    path in ``Chat.svelte`` refreshes the block in place. Runs the inner
    chat synchronously w.r.t. the calling task — the HTTP endpoint wraps
    this in an asyncio.create_task so the request returns immediately.
    """
    if scope not in ("this_turn", "from_launch"):
        raise ValueError(f"invalid rerun scope: {scope}")
    rerun_id = str(rerun_id or uuid4())

    parent_chat_id, parent_chat = await load_effective_parent_chat_for_subagent_action(
        parent_chat_id, user
    )

    # Resolve the clicked entry and the concrete entry this rerun would rewrite.
    # Validation happens BEFORE any hidden-chat reset/revert so blocked reruns
    # never mutate parent or subagent history.
    ctx = _resolve_subagent_rerun_context(
        parent_chat, parent_message_id, entry_key, scope
    )
    parent_message_id = ctx["parent_message_id"]
    target_entry = ctx["target_entry"]
    subagent_id = ctx["subagent_id"]
    write_msg_id = ctx["write_msg_id"]
    write_entry_key = ctx["write_entry_key"]
    write_entry = ctx["write_entry"]
    rerun_attempt = _next_rerun_attempt(write_entry)
    inner_prompt = ctx["inner_prompt"]
    prior_user_msg_id = (
        write_entry.get("user_msg_id") if isinstance(write_entry, dict) else None
    )
    prior_assistant_msg_id = (
        write_entry.get("assistant_msg_id")
        if isinstance(write_entry, dict)
        else None
    )

    subagent_chat = await Chats.get_chat_by_id_and_user_id(subagent_id, user.id)
    if subagent_chat is None:
        raise ValueError("subagent chat not accessible")

    _validate_subagent_rerun_context(parent_chat, subagent_chat, ctx)

    # --- C6/C7: ATOMICALLY CLAIM this entry BEFORE any destructive history
    # reset/revert. This is the single serialization point for concurrent reruns:
    # only one task can flip a terminal entry -> running. A second concurrent
    # rerun of the same entry (two tabs / a double-click; the router preflight is
    # a non-atomic snapshot read and can let both through) sees 'running' here and
    # is blocked, so it can never wipe the shared hidden chat out from under the
    # winner. The CAS raises SubagentRerunBlockedError on a tripped guard, which
    # propagates out (this is before the for-loop, so the per-attempt handlers
    # don't swallow it) and the router surfaces a 409.
    # The CAS below only guards the write_entry itself. ANY OTHER turn of the SAME
    # subagent (a continuation, or a detached rerun, under a different entry_key)
    # could be running; a redo mutates the shared hidden transcript (from_launch
    # wipes it, this_turn reverts+re-appends), so refuse while a sibling turn is
    # live. Re-read the parent fresh to shrink the TOCTOU window.
    _fresh_parent = (
        await Chats.get_chat_by_id_and_user_id(parent_chat_id, user.id)
        or parent_chat
    )
    # Recover a STRANDED entry: if it is stuck at 'running' (a prior rerun's task
    # died after the inner chat finished but before its terminal write — server
    # restart/crash, no finalize sweep) and the hidden chat is NOT generating, the
    # CAS below would block this very redo forever. Clear the stale 'running' to a
    # terminal status first (it is about to be re-claimed anyway) so the CAS can
    # succeed. Only do this when the inner chat is idle, so a genuinely in-flight
    # rerun is never stomped.
    if not _subagent_inner_chat_generating(subagent_chat):
        _block_if_recent_setup_turn(_fresh_parent, subagent_id)
        _fresh_msg_id, _fresh_key, _fresh_entry = _find_subagent_entry(
            _fresh_parent, write_entry_key, preferred_message_id=write_msg_id
        )
        if (
            isinstance(_fresh_entry, dict)
            and _fresh_entry.get("status") == "running"
            and not _fresh_entry.get("ended_at")
        ):
            log.info(
                f"reconciling stranded subagent run {write_entry_key} "
                f"(running with idle inner chat) before redo"
            )
            await _upsert_subagent_run(
                parent_chat_id,
                write_msg_id,
                write_entry_key,
                {"status": "cancelled", "ended_at": int(time.time())},
                sync_placeholder=False,
            )
    else:
        # Only enforce the sibling-running guard when the hidden chat is actually
        # live (idle ⇒ no turn of this subagent is running, stale entries aside).
        _block_if_other_running_turn(_fresh_parent, subagent_id, write_entry_key)
    claimed_run = await _upsert_subagent_run(
        parent_chat_id,
        write_msg_id,
        write_entry_key,
        {
            "subagent_id": subagent_id,
            "entry_key": write_entry_key,
            "rerun": True,
            "rerun_id": rerun_id,
            "rerun_attempt": rerun_attempt,
            "rerun_user_msg_id": None,
            "rerun_assistant_msg_id": None,
            "status": "running",
            "ended_at": None,
            # final_text is intentionally NOT nulled here (C5): a cancel/failure in
            # the setup window must not destroy the prior answer. It is replaced by
            # the done-write when the redo produces new text.
            "error": None,
            "stale": False,
            "started_at": int(time.time()),
        },
        sync_placeholder=False,
        cas_block_if_running=True,
        guard_parent_unconsumed=True,
        require_parent_current=True,
        require_parent_done=True,
        exclusive_running_subagent_id=subagent_id,
    )
    if not isinstance(claimed_run, dict):
        _rerun_blocked(
            "Cannot redo subagent safely: the rerun claim could not be "
            "persisted on the parent message.",
            code="subagent_rerun_claim_failed",
        )

    hidden_current_id = (
        ((subagent_chat.chat or {}).get("history") or {}).get("currentId")
    )
    history_transition: dict
    history_prepared_callback: Optional[Callable[[], Awaitable[None]]] = None
    if scope == "from_launch":
        # The wipe and replacement append are one guarded hidden-chat
        # transaction. A continue, another rerun, or a manual full-chat branch
        # change that wins first rotates currentId and makes this operation fail
        # cleanly without deleting any transcript rows.
        history_transition = {
            "expected_current_id": hidden_current_id,
            "reset_history": True,
        }

        async def _mark_restarted_continuations_stale() -> None:
            # Only run after the hidden transaction committed. Marking these
            # before the guarded reset used to leave false-stale parent cards
            # when setup failed.
            history_messages = _history_messages(parent_chat)
            for m_id, msg in history_messages.items():
                runs = msg.get("subagent_runs") if isinstance(msg, dict) else None
                if not isinstance(runs, dict):
                    continue
                for key, run in runs.items():
                    if (
                        isinstance(run, dict)
                        and run.get("subagent_id") == subagent_id
                        and key != write_entry_key
                    ):
                        await _upsert_subagent_run(
                            parent_chat_id,
                            m_id,
                            key,
                            {"stale": True},
                            sync_placeholder=False,
                        )

        history_prepared_callback = _mark_restarted_continuations_stale
    else:
        # Revert + replacement append are likewise one transaction. Missing
        # retry metadata is not safe to guess: appending without the revert
        # would silently turn "redo" into an extra continuation.
        prior_user_id = target_entry.get("user_msg_id")
        prior_assistant_id = target_entry.get("assistant_msg_id")
        if not prior_user_id or not prior_assistant_id:
            _rerun_blocked(
                "Cannot redo subagent safely: this run is missing its hidden "
                "turn identifiers."
            )
        history_transition = {
            "expected_current_id": prior_assistant_id,
            "revert_user_message_id": prior_user_id,
            "revert_assistant_message_id": prior_assistant_id,
        }

    # Resolve model — prefer the subagent's own configured model so reruns
    # stay consistent. Fall back to per-chat / global / parent.
    subagent_model_id = None
    sa_models = (subagent_chat.chat or {}).get("models") or []
    if sa_models and sa_models[0] in request.app.state.MODELS:
        subagent_model_id = sa_models[0]
    else:
        subagent_model_id = _resolve_subagent_model_id(request, parent_chat, None)
    if subagent_model_id is None:
        raise ValueError("no model available for rerun")
    subagent_model = request.app.state.MODELS[subagent_model_id]
    chat_params = ((parent_chat.chat or {}).get("params")) or {}

    # Synthesize the parent-side emitter that the forwarding emitter will
    # use. Same shape as during a normal subagent run — events go to
    # (user_id, session_id, parent_chat_id, parent_message_id) so the
    # frontend's `chat:subagent:update` handler routes them into the store.
    parent_emitter_info = {
        "user_id": user.id,
        "session_id": session_id,
        "chat_id": parent_chat_id,
        "message_id": write_msg_id,
    }
    parent_event_emitter = get_event_emitter(parent_emitter_info)
    parent_metadata = {
        "user_id": user.id,
        "session_id": session_id,
        "chat_id": parent_chat_id,
        "message_id": write_msg_id,
        "timezone": None,
    }

    rerun_started_at = int(time.time())
    # Build from the write entry but DROP None values so a missing launch entry
    # (e.g. clicked from a continuation whose launch was lost) can't overwrite
    # good prior num/name/prompt with None on the merge.
    rerun_base_run_patch = {
        "subagent_id": subagent_id,
        "entry_key": write_entry_key,
        "chat_id": subagent_id,
        "rerun": True,
        "rerun_id": rerun_id,
        "rerun_attempt": rerun_attempt,
        "continuation": bool((write_entry or {}).get("continuation")),
        "started_at": rerun_started_at,
    }
    for _k in ("num", "name", "tool_call_id", "prompt", "background"):
        _v = (write_entry or {}).get(_k)
        if _v is not None:
            rerun_base_run_patch[_k] = _v
    subagent_meta = {
        **rerun_base_run_patch,
        "parent_message_id": write_msg_id,
    }
    if not rerun_base_run_patch.get("continuation"):
        subagent_meta.pop("continuation", None)

    async def assert_parent_result_still_unconsumed() -> None:
        refreshed_parent_chat = await Chats.get_chat_by_id_and_user_id(parent_chat_id, user.id)
        if refreshed_parent_chat is None:
            _rerun_blocked("Cannot finish subagent redo: parent chat is no longer accessible.")
        _validate_parent_subagent_result_unconsumed(
            refreshed_parent_chat, write_msg_id, rerun_base_run_patch
        )

    # Flip the entry back to running + refresh started_at so the UI times the
    # redo, not the original launch. final_text is preserved (C5) so a cancel in
    # this window can't lose the prior answer; the done-write replaces it on success.
    refreshed_claim = await _upsert_subagent_run(
        parent_chat_id,
        write_msg_id,
        write_entry_key,
        {
            **rerun_base_run_patch,
            "status": "running",
            "ended_at": None,
            "error": None,
            "stale": False,
        },
        # Transactional replacement semantics: while the redo is running, keep
        # the previous parent tool result intact. If a concurrent parent request
        # wins the race, it consumes that coherent old snapshot and the guarded
        # success write is rejected; it can never consume a missing half-result.
        sync_placeholder=False,
        guard_parent_unconsumed=True,
        require_parent_current=True,
        require_parent_done=True,
    )
    if not isinstance(refreshed_claim, dict):
        _rerun_blocked(
            "Cannot redo subagent safely: the persisted rerun claim could not "
            "be refreshed before generation.",
            code="subagent_rerun_claim_failed",
        )

    # Tell the UI a rerun is starting. Same event the original launch used —
    # the frontend handler treats it as "(re)create the live state entry".
    try:
        await parent_event_emitter(
            {"type": "chat:subagent:start", "data": subagent_meta}
        )
    except Exception as e:  # noqa: BLE001
        log.debug(f"chat:subagent:start (rerun) emit failed: {e}")

    # Auto-retry once on unexpected errors, same as the launch / continue paths.
    last_error: Optional[str] = None
    for attempt in (1, 2):
        if attempt > 1:
            await _reemit_subagent_start_on_retry(parent_event_emitter, subagent_meta)
        user_msg_id = str(uuid4())
        assistant_msg_id = str(uuid4())
        attempt_identity = await _upsert_subagent_run(
            parent_chat_id,
            write_msg_id,
            write_entry_key,
            {
                **rerun_base_run_patch,
                "user_msg_id": user_msg_id,
                "assistant_msg_id": assistant_msg_id,
                "rerun_user_msg_id": user_msg_id,
                "rerun_assistant_msg_id": assistant_msg_id,
            },
            sync_placeholder=False,
            guard_parent_unconsumed=True,
            require_parent_current=True,
            require_parent_done=True,
        )
        if not isinstance(attempt_identity, dict):
            raise RuntimeError(
                "failed to persist detached rerun turn identity before generation"
            )
        final_text = None
        try:
            final_text = await _run_inner_chat_guarded(
                request=request,
                user=user,
                subagent_model=subagent_model,
                subagent_chat_id=subagent_id,
                prompt=inner_prompt,
                user_msg_id=user_msg_id,
                assistant_msg_id=assistant_msg_id,
                parent_metadata=parent_metadata,
                parent_event_emitter=parent_event_emitter,
                parent_event_call=None,
                subagent_meta=subagent_meta,
                chat_params=chat_params,
                history_transition=history_transition,
                history_prepared_callback=history_prepared_callback,
                force_fanout=True,
            )
            if not final_text:
                raise RuntimeError("subagent produced no final text")
            await assert_parent_result_still_unconsumed()
            await _upsert_subagent_run(
                parent_chat_id,
                write_msg_id,
                write_entry_key,
                {
                    **rerun_base_run_patch,
                    "status": "done",
                    "ended_at": int(time.time()),
                    "final_text": final_text,
                },
                allow_placeholder_append=False,
                guard_parent_unconsumed=True,
                require_parent_current=True,
                require_parent_done=True,
                touch_chat=True,
            )
            return
        except asyncio.CancelledError:
            # Discriminate a GENUINE user-stop (this rerun task was cancelled ->
            # current_task().cancelling()) from a SPURIOUS per-subagent cancel (a
            # dead inner stream), mirroring run_subagent_launch / run_subagent_continue.
            genuine_user_stop = _subagent_cancel_is_from_parent_task()
            if genuine_user_stop:
                # Durable terminal write FIRST and the re-raise UNCONDITIONAL.
                # Do NOT call assert_parent_result_still_unconsumed() here: it can
                # raise SubagentRerunBlockedError (e.g. the parent row was deleted
                # by delete-chat, which cancels this rerun then deletes), which
                # would both skip the cancelled write (card stuck 'running' — no
                # parent finalizer sweeps a standalone rerun) AND swallow the
                # cancel (a swallowed CancelledError pins the task in anyio's
                # cancel scope, rescheduling _deliver_cancellation forever). Wrap
                # the best-effort write/emit so nothing can preempt the re-raise.
                try:
                    _completed = isinstance(final_text, str) and bool(final_text.strip())
                    # Re-await the protected write through repeated Stops. A bare
                    # shield protects the child but still lets a second cancel
                    # detach it before we know whether the commit succeeded.
                    terminal_write_task = asyncio.create_task(
                        _upsert_subagent_run(
                            parent_chat_id,
                            write_msg_id,
                            write_entry_key,
                            {
                                **rerun_base_run_patch,
                                "status": "done" if _completed else "cancelled",
                                "ended_at": int(time.time()),
                                **({"final_text": final_text} if _completed else {}),
                            },
                            allow_placeholder_append=False,
                            guard_parent_unconsumed=_completed,
                            require_parent_current=_completed,
                            require_parent_done=_completed,
                            touch_chat=True,
                        )
                    )
                    await _wait_shielded_task_to_completion(terminal_write_task)
                    terminal_write = terminal_write_task.result()
                    if not isinstance(terminal_write, dict):
                        raise RuntimeError(
                            "subagent rerun cancel terminal write was not committed"
                        )
                    if _completed:
                        # C3: the rerun actually produced its answer before the Stop
                        # — preserve it (force_fanout so the terminal reaches the
                        # viewing tab; a rerun has no parent finalizer to recover it).
                        await _emit_subagent_terminal(
                            parent_event_emitter,
                            subagent_meta,
                            status="done",
                            user_id=user.id,
                            parent_chat_id=parent_chat_id,
                            parent_message_id=write_msg_id,
                            force_fanout=True,
                        )
                    else:
                        await _emit_subagent_cancel(
                            parent_event_emitter,
                            subagent_meta,
                            user_id=user.id,
                            parent_chat_id=parent_chat_id,
                            parent_message_id=write_msg_id,
                            force_fanout=True,
                        )
                except Exception:
                    log.exception("subagent rerun cancel terminal write/emit failed")
                raise
            # Spurious per-subagent cancel → retry like an error (don't mark the
            # rerun "stopped" when the user never stopped it).
            _clear_isolated_child_cancellation()
            last_error = "subagent inner stream was cancelled (not a user stop)"
            log.warning(
                f"subagent rerun attempt {attempt}/2 hit a spurious "
                "CancelledError (parent not stopping) — retrying"
            )
            if attempt == 1:
                if scope == "from_launch":
                    history_transition = {
                        "expected_current_id": assistant_msg_id,
                        "reset_history": True,
                    }
                else:
                    history_transition = {
                        "expected_current_id": assistant_msg_id,
                        "revert_user_message_id": user_msg_id,
                        "revert_assistant_message_id": assistant_msg_id,
                    }
                continue
            await _upsert_subagent_run(
                parent_chat_id,
                write_msg_id,
                write_entry_key,
                {
                    **rerun_base_run_patch,
                    "status": "error",
                    "ended_at": int(time.time()),
                    "error": {"message": last_error},
                },
                allow_placeholder_append=False,
                touch_chat=True,
            )
            await _emit_subagent_terminal(
                parent_event_emitter,
                subagent_meta,
                status="error",
                message=last_error,
                user_id=user.id,
                parent_chat_id=parent_chat_id,
                parent_message_id=write_msg_id,
                force_fanout=True,
            )
            return
        except SubagentRerunBlockedError as blocked:
            # Blocked AFTER the optimistic flip to running (a concurrent parent
            # edit landed between preflight and here). Resolve the card to error
            # so it doesn't spin forever; the router also toasts the reason.
            await _upsert_subagent_run(
                parent_chat_id,
                write_msg_id,
                write_entry_key,
                {
                    **rerun_base_run_patch,
                    "status": "error",
                    "ended_at": int(time.time()),
                    "error": {"message": str(blocked)},
                },
                allow_placeholder_append=False,
                touch_chat=True,
            )
            await _emit_subagent_terminal(
                parent_event_emitter,
                subagent_meta,
                status="error",
                message=str(blocked),
                user_id=user.id,
                parent_chat_id=parent_chat_id,
                parent_message_id=write_msg_id,
                force_fanout=True,
            )
            raise
        except Exception as e:  # noqa: BLE001
            # Do NOT call assert_parent_result_still_unconsumed() here: it can raise
            # SubagentRerunBlockedError, which would escape this handler (the sibling
            # `except SubagentRerunBlockedError` can't catch it) and leave the card
            # stuck 'running' after the optimistic flip. Terminalizing a rerun to
            # 'error' is always safe even if the parent moved on — it only stops the
            # spinner. (The success path keeps the guard, to avoid a stale answer.)
            last_error = str(e)
            log.exception(f"subagent rerun attempt {attempt}/2 failed: {e}")
            if attempt == 1 and not isinstance(
                e, (SubagentNonRetryableError, ChatHistoryConflictError)
            ):
                # from_launch: wipe the whole hidden history again to mimic the
                # launch retry path (the user asked to discard the subagent's
                # work, so a clean slate is correct).
                # this_turn: revert just this failed attempt's user→blank pair so
                # the retry re-runs against the same prior state WITHOUT doubling
                # turns — prior research is preserved and the transcript stays
                # clean for any subsequent redo.
                # SKIPPED for a non-retryable provider error (context window
                # exceeded): re-running just overflows again.
                if scope == "from_launch":
                    history_transition = {
                        "expected_current_id": assistant_msg_id,
                        "reset_history": True,
                    }
                else:
                    history_transition = {
                        "expected_current_id": assistant_msg_id,
                        "revert_user_message_id": user_msg_id,
                        "revert_assistant_message_id": assistant_msg_id,
                    }
                continue
            await _upsert_subagent_run(
                parent_chat_id,
                write_msg_id,
                write_entry_key,
                {
                    **rerun_base_run_patch,
                    "status": "error",
                    "ended_at": int(time.time()),
                    "error": {"message": last_error},
                    **(
                        {
                            "user_msg_id": prior_user_msg_id,
                            "assistant_msg_id": prior_assistant_msg_id,
                        }
                        if isinstance(e, ChatHistoryConflictError)
                        else {}
                    ),
                },
                allow_placeholder_append=False,
                touch_chat=True,
            )
            await _emit_subagent_terminal(
                parent_event_emitter,
                subagent_meta,
                status="error",
                message=last_error or "unknown error",
                user_id=user.id,
                parent_chat_id=parent_chat_id,
                parent_message_id=write_msg_id,
                force_fanout=True,
            )
            return
