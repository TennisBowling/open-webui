import time
import logging
import sys
import os
import base64
import copy

import asyncio
from functools import partial
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

from open_webui.utils.chat_transport import should_attach_chat_event_transport
from open_webui.utils.response_durability import (
    is_selection_metadata_only_completion,
    text_content_blocks,
)


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

# Set alongside ``current_tool_call_id_var`` for each gathered tool branch. A
# parallel tool/subagent runs in its own child task, so inside the tool
# ``asyncio.current_task().cancelling()`` describes the child, not the parent chat
# generation. Subagent cancellation handling uses this to distinguish a genuine
# user-stop of the parent response task from an isolated child cancellation.
current_tool_parent_task_var: ContextVar[Optional[asyncio.Task]] = ContextVar(
    "current_tool_parent_task", default=None
)


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
    usage_has_data,
    normalize_provider_usage,
    is_primary_session,
    stream_version_init,
    stream_version_incr,
    stream_version_get,
    stream_version_flush,
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
    pop_pending_tool_selection,
    is_generation_cancelled,
    is_generation_turn_cancelled,
)
from open_webui.utils.live_tool_selection import (
    build_tool_selection_change_block,
    normalize_live_tool_selection,
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
from open_webui.utils.messages import (
    align_reasoning_rounds_to_blocks,
    blocks_to_api_messages,
    blocks_to_plain_text,
    is_aborted_attempt,
    resume_boundary_blocks,
    round_base_messages,
)
from open_webui.utils.lazy_blocks import (
    GENERIC_TOOL_INLINE_RESULT_MAX,
    LAZY_RESULT_EXEMPT_TOOL_NAMES,
    SUBAGENT_INLINE_RESULT_MAX,
    SUBAGENT_TOOL_NAMES,
    TOOL_INLINE_RESULT_MAX,
    WEB_TOOL_INLINE_RESULT_MAX,
    _merge_tool_result_body_maps,
    _slim_tool_result,
    _strip_tool_results,
    _summarize_tool_result,
    _tool_call_name_by_id,
    split_reasoning_bodies,
    split_tool_result_bodies,
    text_only_content_from_blocks,
)
from open_webui.models.mcp import MCPConnections
from open_webui.utils.mcp.client import (
    MCPClient,
    mcp_tool_alias,
    build_mcp_connect_kwargs,
    BearerRefreshAuth,
)
from open_webui.utils.tool_calling import (
    dedupe_repeated_tool_name,
    merge_streamed_field,
    mcp_model_facing_tool_name,
    parse_tool_call_arguments,
)
from open_webui.utils.mcp.connections import (
    build_personal_mcp_connect_kwargs,
    parse_personal_mcp_tool_id,
    resolve_personal_mcp_call_meta,
    tool_allowed_by_policy,
    tool_filter_allows,
)
from open_webui.utils.mcp.oauth import MCPOAuthReauthRequired
from open_webui.utils.container_workspace import (
    is_container_workspace_active,
    import_changed_container_outputs,
    prepare_container_workspace_for_turn,
    browser_progress_poller,
    build_bash_result_suffix,
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
    STREAM_DB_CHECKPOINT_POLICY,
    STREAM_DB_CHECKPOINT_INTERVAL_SECONDS,
    STREAM_DB_CHECKPOINT_CHAR_DELTA,
    AGENTIC_MAX_TOOL_ROUNDS,
    AGENTIC_EMPTY_ROUND_MAX_RETRIES,
    ENABLE_CONVERSATION_COMPACTION,
    COMPACTION_THRESHOLD,
    PROFILE_CHAT,
    PROFILE_CHAT_DIR,
)
from open_webui.utils.context_window import resolve_context_length
from open_webui.utils.compaction import (
    COMPACTION_BLOCK_TYPE,
    capture_compaction_envelope,
    compact_content_blocks,
    conversation_has_compacted_context,
    has_uncompacted_span,
    is_compact_command,
    is_compaction_block,
    should_compact,
    usage_total_tokens,
)
from open_webui.constants import TASKS


logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

# ---------------------------------------------------------------------------
# Extracted streaming subsystems (2026-08-02 de-spaghettification). These
# names are re-exported here so every existing importer of
# `open_webui.utils.middleware` keeps working; new code should import from the
# dedicated modules directly.
# ---------------------------------------------------------------------------
from open_webui.utils.provider_errors import (
    _is_context_fallback_provider_error,
    _is_context_limit_provider_error,
    _is_nonretryable_provider_error,
    _nonstreaming_round_length_error,
    _provider_error_code,
    _provider_error_payload,
    _provider_error_text,
    _safe_error_response_text,
)
from open_webui.utils.streaming.accumulate import (
    TailAccumulator,
    _StreamTextAccumulator,
    _append_reasoning_delta,
    _apply_reasoning_detail_delta,
)
from open_webui.utils.streaming.serialize import (
    serialize_content_blocks as _serialize_content_blocks,
)
from open_webui.utils.streaming.blocks import (
    _finalize_open_agentic_blocks,
    _total_text_block_len,
    _visible_nonstreaming_reasoning,
    _visible_reasoning_from_details,
)
from open_webui.utils.streaming.wire import (
    STREAM_DELTA_MAX_BYTES,
    STREAM_TEXT_COALESCE_MIN_CHARS,
    STREAM_TEXT_COALESCE_WINDOW_S,
    STREAM_TEXT_DELTA_MAX_BYTES,
    _emit_delta_for_blocks,
    _json_size_bytes,
    _split_stream_delta_op,
    _split_text_by_utf8_bytes,
    _utf8_len,
    _wrap_event_emitter_v21,
)


WEB_TOOL_NAMES = {"web_search", "web_fetch"}
# Tool result bodies are LAZY by default for ALL tools: the collapsed card
# renders entirely from the call arguments + the slim stub (summary / size /
# status), and the full body crosses the network only when the card is
# expanded (the tool-result-body endpoint). Inlining only pays below the
# stub's own overhead: see utils/lazy_blocks.py, the single owner of the lazy
# message-body contract (thresholds, exemptions, splitters). Imported above so
# existing references keep working.
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


def _should_enable_view_video_tool(request, model) -> bool:
    """Return true when view_video should be present in the model tool list.

    Gated purely on the model actually accepting video — unlike view_image there
    is no web-search/container precondition, because a video is something the
    model can usefully go and fetch on its own from any link in the conversation.
    """
    cfg = getattr(request.app.state, "config", None)
    if cfg is not None and not getattr(cfg, "ENABLE_VIDEO_INPUT", True):
        return False

    from open_webui.utils.models import model_supports_video_input

    return model_supports_video_input(model)


def _should_enable_ask_user_tool(request, metadata: dict) -> bool:
    """Return true when the built-in ask_user tool should be exposed to the
    model. Gated on the admin config flag and explicitly OFF for runs with no
    human on the other end — a subagent has no user to interrogate and an
    automation fires on a schedule with nobody watching, so either would block
    waiting on an answer that never arrives (and persisting question state on
    the parent chat from inside a subagent would be wrong besides). Temp/local
    chats are allowed: the tool degrades to a socket-ack prompt there (no
    durable blob)."""
    if metadata.get("subagent_inner") or metadata.get("automation_run"):
        return False
    try:
        return bool(getattr(request.app.state.config, "ENABLE_ASK_USER", False))
    except Exception:
        return False


# _tool_call_name_by_id / _summarize_tool_result / _slim_tool_result /
# split_tool_result_bodies / _merge_tool_result_body_maps / _strip_tool_results
# moved to utils/lazy_blocks.py (imported above): the read-path projection in
# models/chats.py needs the same slimming rules, and importing middleware from
# there would be circular.


async def process_tool_result(
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
    if isinstance(tool_result, dict) and isinstance(
        tool_result.get("_owui_meta"), dict
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
                            file_url = await get_file_url_from_base64(
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
                        if item.get("type") == "image" and _model_supports_vision(
                            model
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

    # Strip raw NUL (0x00) at the ingestion boundary. Tool results (web_fetch /
    # web_search scrapes, MCP, OpenAPI) can carry a 0x00 from binary/garbage page
    # content, and ``ensure_ascii=False`` above preserves it. Removing it here
    # keeps the byte out of content_blocks, the RAM stream snapshot, and any error
    # string built from this content — Postgres text/jsonb cannot store a NUL, so a
    # single stray byte would otherwise abort the assistant-message write.
    if isinstance(tool_result, str) and "\x00" in tool_result:
        tool_result = tool_result.replace("\x00", "")

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
                    ) = await process_tool_result(
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

    # The caller owns system-prompt composition. Returning the generated
    # turn-level context separately lets live tool refreshes rebuild selectable
    # feature prompts without rerunning image generation or losing this context.
    return form_data, system_message_content


async def apply_params_to_form_data(form_data, model):
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
    # Pipeline Inlet -> Filter Inlet -> Chat Web Search -> Chat Image Generation
    # -> (Default) Chat Tools Function Calling -> Chat Files

    # A live tool refresh deliberately re-enters only the canonical feature/tool
    # resolution portion of this function. Pipeline/filter/image handlers
    # are turn-level work and must not run again between agentic rounds.
    tool_selection_refresh = bool(metadata.pop("_tool_selection_refresh", False))
    incoming_params = form_data.get("params") if isinstance(form_data, dict) else None
    incoming_subagent_external_tools_enabled = None
    if (
        isinstance(incoming_params, dict)
        and "subagentExternalToolsEnabled" in incoming_params
    ):
        incoming_subagent_external_tools_enabled = bool(
            incoming_params.get("subagentExternalToolsEnabled")
        )

    if not tool_selection_refresh:
        form_data = await apply_params_to_form_data(form_data, model)

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
    if system_message and not tool_selection_refresh:  # Chat Controls/User Settings
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
    if chat_id and user and not tool_selection_refresh:
        chat = await Chats.get_chat_by_id_and_user_id(chat_id, user.id)
        if chat and chat.folder_id:
            folder = await Folders.get_folder_by_id_and_user_id(chat.folder_id, user.id)

            if folder and folder.data:
                if "system_prompt" in folder.data:
                    form_data = apply_system_prompt_to_body(
                        folder.data["system_prompt"], form_data, metadata, user
                    )
                # Folder-level file attachments / knowledge have been removed.
                pass

    variables = form_data.pop("variables", None)

    if not tool_selection_refresh:
        # Process the form_data through the pipeline
        try:
            form_data = await process_pipeline_inlet_filter(
                request, form_data, user, models
            )
        except Exception as e:
            raise e

        try:
            filter_functions = [
                await Functions.get_function_by_id(filter_id)
                for filter_id in await get_sorted_filter_ids(
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

    if not tool_selection_refresh:
        if not isinstance(metadata.get("live_tool_selection"), dict):
            requested_ids = _as_tool_id_list(tool_ids)
            requested_features = features if isinstance(features, dict) else {}
            feature_selection_ids = [
                f"feature:{feature}"
                for feature in (
                    "web_search",
                    "study_mode",
                    "data_viz",
                    "subagents",
                    "automations",
                )
                if requested_features.get(feature)
            ]
            metadata["live_tool_selection"] = normalize_live_tool_selection(
                {
                    "selection_ids": [*requested_ids, *feature_selection_ids],
                    "tool_ids": requested_ids,
                    "tool_servers": metadata.get("tool_servers") or [],
                    "features": requested_features,
                    "params": incoming_params or {},
                }
            )

    # C2: enforce the subagent feature gate SERVER-SIDE — the browser gate is only
    # advisory. Honor features.subagents (and a builtin:subagent tool a client may
    # smuggle straight into tool_ids) ONLY when the feature is globally enabled AND
    # this user is permitted. Otherwise an admin's global ENABLE_SUBAGENTS=off and a
    # per-user permission revocation are both cosmetic: any verified user (incl. an
    # API key) could still spawn subagents — hidden chats, inherited tools, token/USD
    # cost. Strip the flag + the tool when not allowed.
    _subagents_allowed = bool(
        getattr(request.app.state.config, "ENABLE_SUBAGENTS", False)
    )
    if _subagents_allowed and getattr(user, "role", None) != "admin":
        try:
            from open_webui.utils.access_control import has_permission_async

            _subagents_allowed = await has_permission_async(
                user.id,
                "features.subagents",
                request.app.state.config.USER_PERMISSIONS,
            )
        except Exception:
            log.exception("subagent permission check failed; denying")
            _subagents_allowed = False
    if not _subagents_allowed:
        if isinstance(features, dict):
            features.pop("subagents", None)
        if isinstance(tool_ids, list):
            tool_ids = [t for t in tool_ids if t != "builtin:subagent"]

    # Same server-side enforcement for data_viz: the browser feature flag is only
    # advisory, so honor features.data_viz (and a builtin:data_viz tool a client
    # may smuggle straight into tool_ids) ONLY when ENABLE_DATA_VIZ is globally
    # on. Otherwise an admin's global disable is cosmetic — any verified user
    # (incl. an API key) could still inject the show_widget tool.
    if not bool(getattr(request.app.state.config, "ENABLE_DATA_VIZ", False)):
        if isinstance(features, dict):
            features.pop("data_viz", None)
        if isinstance(tool_ids, list):
            tool_ids = [t for t in tool_ids if t != "builtin:data_viz"]

    # And for automations: a smuggled builtin:automations would let any verified
    # user schedule unattended, recurring, billable generations on an instance
    # whose admin has the feature off.
    if not bool(getattr(request.app.state.config, "ENABLE_AUTOMATIONS", False)):
        if isinstance(features, dict):
            features.pop("automations", None)
        if isinstance(tool_ids, list):
            tool_ids = [t for t in tool_ids if t != "builtin:automations"]

    if isinstance(metadata.get("live_tool_selection"), dict):
        effective_features = {
            feature: bool(features.get(feature))
            for feature in (
                "web_search",
                "study_mode",
                "data_viz",
                "subagents",
                "automations",
            )
            if isinstance(features, dict)
        }
        allowed_feature_ids = {
            f"feature:{feature}"
            for feature, enabled in effective_features.items()
            if enabled
        }
        current_live_selection = metadata["live_tool_selection"]
        metadata["live_tool_selection"] = normalize_live_tool_selection(
            {
                **current_live_selection,
                "features": effective_features,
                "selection_ids": [
                    selection_id
                    for selection_id in current_live_selection.get(
                        "selection_ids", []
                    )
                    if not str(selection_id).startswith("feature:")
                    or selection_id in allowed_feature_ids
                ],
            }
        )

    # One-time handlers such as image generation may enrich the conversation,
    # while selectable feature prompts below must be rebuilt on every live
    # change. Capture the seam between those two classes of work.
    tool_selection_base_messages = None
    post_feature_system_messages: list[str] = []
    feature_prompt_parts: list[str] = []

    if features:
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
                MINIMAL_FALLBACK_PROMPT,
            )

            if tool_ids is None:
                tool_ids = []
            if "builtin:data_viz" not in tool_ids:
                tool_ids.append("builtin:data_viz")
            metadata.setdefault("params", {})["function_calling"] = "native"

            data_viz_prompt = assemble_data_viz_system_prompt(request.app.state.config)
            if not data_viz_prompt:
                # The default prompt modules ship empty (admin pastes real text).
                # We still inject the show_widget tool above, so without ANY
                # guidance the model would be flying blind. Fall back to a minimal
                # built-in description of the tool's contract.
                data_viz_prompt = MINIMAL_FALLBACK_PROMPT
            if data_viz_prompt:
                feature_prompt_parts.append(data_viz_prompt)
            log.info(
                "Auto-enabled data visualization tool with native function calling"
            )

        if features.get("automations"):
            # Automations let the model schedule a prompt to run later in a
            # fresh chat (see utils/automations_tool.py). The system prompt is a
            # constant rather than admin-editable config: it encodes the
            # title/prompt contract the runner actually enforces, so drifting it
            # would produce automations that read as nonsense when they fire.
            from open_webui.utils.automations_tool import AUTOMATIONS_SYSTEM_PROMPT

            if tool_ids is None:
                tool_ids = []
            if "builtin:automations" not in tool_ids:
                tool_ids.append("builtin:automations")
            metadata.setdefault("params", {})["function_calling"] = "native"

            feature_prompt_parts.append(AUTOMATIONS_SYSTEM_PROMPT)
            log.info("Auto-enabled automations tools with native function calling")

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

        if not tool_selection_refresh:
            tool_selection_base_messages = copy.deepcopy(
                form_data.get("messages", [])
            )

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

        if (
            not tool_selection_refresh
            and "image_generation" in features
            and features["image_generation"]
        ):
            form_data, image_system_message = await chat_image_generation_handler(
                request, form_data, extra_params, user
            )
            if image_system_message:
                form_data["messages"] = add_or_update_system_message(
                    image_system_message, form_data["messages"]
                )
                post_feature_system_messages.append(image_system_message)

    if tool_selection_refresh:
        # These are one-time turn contexts that were originally composed after
        # selectable feature prompts. Reapply the stored values in the same
        # order without rerunning their side effects.
        for system_context in metadata.get(
            "_tool_selection_post_feature_system_messages", []
        ):
            if isinstance(system_context, str) and system_context:
                form_data["messages"] = add_or_update_system_message(
                    system_context, form_data["messages"]
                )
    else:
        metadata["_tool_selection_base_messages"] = (
            tool_selection_base_messages
            if tool_selection_base_messages is not None
            else copy.deepcopy(form_data.get("messages", []))
        )
        metadata["_tool_selection_post_feature_system_messages"] = (
            post_feature_system_messages
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

    if _should_enable_view_video_tool(request, model):
        if tool_ids is None:
            tool_ids = []
        if "builtin:view_video" not in tool_ids:
            tool_ids.append("builtin:view_video")
        metadata.setdefault("params", {})["function_calling"] = "native"
        log.info("Auto-enabled view_video tool with native function calling")

    # The compaction read-back escape hatch. Bound only when this conversation
    # actually carries a <compacted_context> envelope, so it never pollutes the
    # tool list (or the cached tools prefix) of a chat that has never compacted.
    #
    # Known gap: tools are resolved once, here, per turn. A conversation whose
    # FIRST compaction happens mid-turn therefore doesn't get the tool until the
    # next turn — the model's only recourse for that one turn is to re-run the
    # tool. The mechanical index is the load-bearing part (COMPACTION.md §7);
    # this is the escape hatch, so the gap is a degradation, not a break.
    if ENABLE_CONVERSATION_COMPACTION and conversation_has_compacted_context(
        form_data.get("messages")
    ):
        if tool_ids is None:
            tool_ids = []
        if "builtin:read_tool_result" not in tool_ids:
            tool_ids.append("builtin:read_tool_result")
        metadata.setdefault("params", {})["function_calling"] = "native"
        log.info("Auto-enabled read_tool_result tool (conversation is compacted)")

    if _should_enable_ask_user_tool(request, metadata):
        if tool_ids is None:
            tool_ids = []
        if "builtin:ask_user" not in tool_ids:
            tool_ids.append("builtin:ask_user")
        metadata.setdefault("params", {})["function_calling"] = "native"
        ask_user_prompt = getattr(
            request.app.state.config, "ASK_USER_PARENT_PROMPT", ""
        )
        if ask_user_prompt:
            form_data["messages"] = add_or_update_system_message(
                ask_user_prompt, form_data["messages"], append=True
            )
        log.info("Auto-enabled ask_user tool with native function calling")

    prompt = get_last_user_message(form_data["messages"])

    # Keep a handle to the CALLER's metadata object before we rebind `metadata` to
    # a fresh dict below. The connected MCP clients are only attached to that fresh
    # dict at the very end of this function — so if we raise/cancel mid-connect (a
    # CancelledError during a later connect / list_tool_specs / get_tools, or a
    # transient tool-load error), the caller, which holds the INPUT object and
    # reads it in its `finally` on error, could never reach the already-connected
    # clients and would leak a stdio subprocess / HTTP stream per launch. Attaching
    # the SAME mcp_clients dict to the caller's object up front (below) guarantees
    # cleanup always has a live target. (See main.process_chat and
    # subagent._run_inner_chat finally blocks, which both read the input metadata
    # on an early raise.)
    _caller_metadata = metadata

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
    # Expose the (initially empty) clients dict on the caller's metadata object
    # immediately, so a raise/cancel anywhere below still leaves every connected
    # client reachable for the caller's finally to disconnect (see _caller_metadata
    # rationale above). Same dict object that gets attached to the returned metadata
    # at the end, so there is exactly one set of clients and one disconnect.
    if isinstance(_caller_metadata, dict):
        _caller_metadata["mcp_clients"] = mcp_clients
    mcp_tools_dict = {}
    mcp_failures: list[dict] = []

    if tool_ids:
        for tool_id in tool_ids:
            personal_connection_id = parse_personal_mcp_tool_id(tool_id)
            if personal_connection_id:
                original_server_id = f"user:{personal_connection_id}"
                # Dedupe: a duplicate tool_id in the request would overwrite (and
                # leak) the already-connected client for this connection.
                if original_server_id in mcp_clients:
                    continue
                personal_connection = None
                try:
                    personal_connection = (
                        await MCPConnections.get_connection_by_id_and_user_id(
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

                    connect_kwargs = await build_personal_mcp_connect_kwargs(
                        personal_connection,
                        user=user,
                        metadata=metadata,
                    )
                    if personal_connection.transport == "stdio":
                        from open_webui.utils.mcp.persistent import personal_mcp_process_key

                        mcp_clients[original_server_id] = await request.app.state.persistent_mcp.ensure(
                            personal_mcp_process_key(user.id, personal_connection_id),
                            connect_kwargs,
                        )
                    else:
                        mcp_clients[original_server_id] = MCPClient()
                        await mcp_clients[original_server_id].connect(**connect_kwargs)

                    tool_specs = await mcp_clients[original_server_id].list_tool_specs()
                    call_meta = resolve_personal_mcp_call_meta(
                        personal_connection,
                        user=user,
                        metadata=metadata,
                    )
                    for tool_spec in tool_specs or []:
                        if not tool_allowed_by_policy(tool_spec, personal_connection):
                            continue

                        def make_tool_function(client, function_name, call_meta):
                            async def tool_function(**kwargs):
                                return await client.call_tool(
                                    function_name,
                                    function_args=kwargs,
                                    meta=call_meta,
                                    timeout_seconds=getattr(
                                        request.app.state.config,
                                        "MCP_TOOL_CALL_TIMEOUT",
                                        None,
                                    ),
                                )

                            return tool_function

                        tool_function = make_tool_function(
                            mcp_clients[original_server_id],
                            tool_spec["name"],
                            call_meta,
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
                    _leaked = mcp_clients.pop(original_server_id, None)
                    if _leaked is not None:
                        # C8: connect() may have SUCCEEDED and the failure came from
                        # list_tool_specs / spec iteration — popping alone leaks a live
                        # stdio subprocess / HTTP stream (amplified per parallel
                        # subagent). Disconnect best-effort before dropping it.
                        try:
                            await _leaked.disconnect()
                        except Exception:
                            log.exception(
                                "MCP client disconnect on setup-failure failed (%r)",
                                original_server_id,
                            )
                    mcp_failures.append(
                        {
                            "server_id": original_server_id,
                            "name": getattr(
                                personal_connection, "name", personal_connection_id
                            ),
                            "reason": f"{type(e).__name__}: {e}",
                            "needs_reauth": isinstance(e, MCPOAuthReauthRequired),
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
                # Dedupe: a duplicate tool_id would overwrite (and leak) the
                # already-connected client for this server.
                if original_server_id in mcp_clients:
                    continue
                try:
                    mcp_server_connection = None
                    # Bound before the connection-scan loop so the except handler
                    # (which reads them for needs_reauth) never hits an unbound or
                    # stale value if a malformed config entry throws in the scan.
                    auth_type = ""
                    bearer_token: Optional[str] = None
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
                            # Look up the session by the FULL server id — the same
                            # key the client/session were registered under
                            # (`mcp:{server_id}`). Truncating to a trailing colon
                            # segment here missed the session for any colon-bearing
                            # id, so the server connected unauthenticated.
                            oauth_token = await request.app.state.oauth_client_manager.get_oauth_token(
                                user.id, f"mcp:{server_id}"
                            )

                            if oauth_token:
                                bearer_token = (
                                    oauth_token.get("access_token", "") or None
                                )
                        except Exception as e:
                            log.error(f"Error getting OAuth token: {e}")
                            oauth_token = None

                    # For an admin oauth_2.1 remote_http server, ship a refreshing
                    # auth flow (mirroring the personal path) so a mid-session 401
                    # — the access token expiring during a long agentic turn —
                    # triggers a serialized refresh + retry instead of failing
                    # every remaining tool call (audit A6).
                    refresh_auth = None
                    server_transport = (
                        mcp_server_connection.get("transport") or "remote_http"
                    )
                    if auth_type == "oauth_2.1" and server_transport == "remote_http":
                        _ocm = request.app.state.oauth_client_manager
                        _sid = server_id
                        _uid = user.id

                        async def _admin_oauth_refresh_cb(
                            _stale, _ocm=_ocm, _sid=_sid, _uid=_uid
                        ):
                            tok = await _ocm.get_oauth_token(
                                _uid, f"mcp:{_sid}", force_refresh=True
                            )
                            return (tok or {}).get("access_token") or None

                        refresh_auth = BearerRefreshAuth(
                            bearer_token, _admin_oauth_refresh_cb
                        )

                    connect_kwargs = build_mcp_connect_kwargs(
                        mcp_server_connection,
                        bearer_token=None if refresh_auth is not None else bearer_token,
                        user=user,
                        metadata=metadata,
                    )
                    if refresh_auth is not None:
                        connect_kwargs["auth"] = refresh_auth

                    if connect_kwargs.get("transport") == "stdio":
                        from open_webui.utils.mcp.persistent import admin_mcp_process_key

                        mcp_clients[original_server_id] = await request.app.state.persistent_mcp.ensure(
                            admin_mcp_process_key(server_id), connect_kwargs
                        )
                    else:
                        mcp_clients[original_server_id] = MCPClient()
                        await mcp_clients[original_server_id].connect(**connect_kwargs)

                    tool_specs = await mcp_clients[original_server_id].list_tool_specs()
                    # A persistent stdio process has no per-request HTTP headers.
                    # Resolve configured context templates for this chat now and
                    # carry them in the protocol-native tools/call `_meta` field.
                    # Keep this value in the callable closure so concurrent chats
                    # never share mutable "current chat" process state.
                    call_meta = None
                    if connect_kwargs.get("transport") == "stdio":
                        call_meta = resolve_tool_server_headers(
                            mcp_server_connection,
                            user=user,
                            metadata=metadata,
                        ) or None
                    for tool_spec in tool_specs:
                        # Admin per-tool enable/disable: skip any tool the admin
                        # disabled for this server so the model never sees it.
                        if not tool_filter_allows(
                            tool_spec, mcp_server_connection.get("tool_filters")
                        ):
                            continue

                        def make_tool_function(client, function_name, call_meta):
                            async def tool_function(**kwargs):
                                return await client.call_tool(
                                    function_name,
                                    function_args=kwargs,
                                    meta=call_meta,
                                    timeout_seconds=getattr(
                                        request.app.state.config,
                                        "MCP_TOOL_CALL_TIMEOUT",
                                        None,
                                    ),
                                )

                            return tool_function

                        tool_function = make_tool_function(
                            mcp_clients[original_server_id],
                            tool_spec["name"],
                            call_meta,
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
                    _leaked = mcp_clients.pop(original_server_id, None)
                    if _leaked is not None:
                        # C8: if connect() succeeded but list_tool_specs / spec
                        # iteration raised, popping alone leaks a live connection.
                        try:
                            await _leaked.disconnect()
                        except Exception:
                            log.exception(
                                "MCP client disconnect on setup-failure failed (%r)",
                                original_server_id,
                            )
                    server_name = (mcp_server_connection or {}).get("info", {}).get(
                        "name"
                    ) or original_server_id
                    mcp_failures.append(
                        {
                            "server_id": original_server_id,
                            "name": server_name,
                            "reason": f"{type(e).__name__}: {e}",
                            # An oauth_2.1 server whose grant is dead resolves to no
                            # bearer token and then 401s; surface the same reconnect
                            # prompt the personal path shows instead of a generic
                            # "failed to load" (the admin OAuth path returns None
                            # rather than raising MCPOAuthReauthRequired).
                            "needs_reauth": auth_type == "oauth_2.1"
                            and not bearer_token,
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
                    needs_reauth = bool(fail.get("needs_reauth"))
                    description = (
                        f"MCP server '{fail['name']}' needs to be reconnected — "
                        f"its authorization expired. Reconnect it in Settings → Tools."
                        if needs_reauth
                        else f"MCP server '{fail['name']}' failed to load: {fail['reason']}"
                    )
                    try:
                        await event_emitter(
                            {
                                "type": "status",
                                "data": {
                                    "action": "mcp_server",
                                    "description": description,
                                    "server_id": fail.get("server_id"),
                                    "needs_reauth": needs_reauth,
                                    "done": True,
                                    "error": True,
                                },
                            }
                        )
                    except Exception:
                        log.exception("Failed to emit MCP failure status")
            if tool_selection_refresh:
                failed_names = ", ".join(
                    str(fail.get("name") or fail.get("server_id") or "tool server")
                    for fail in mcp_failures
                )
                raise RuntimeError(
                    f"Could not apply tool selection because {failed_names} failed to load"
                )

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


async def _chat_title_event_payload(chat_id: str, title: str) -> dict:
    chat = await Chats.get_chat_by_id(chat_id) if chat_id else None
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


# ---------------------------------------------------------------------------
# Per-round tool execution (hoisted out of the agentic round loop, where
# _execute_tool_call used to be re-defined EVERY round; 2026-08-02
# de-spaghettification). All former closure captures are explicit keyword
# parameters; the round loop binds them via functools.partial at the same
# point the def used to sit.
# ---------------------------------------------------------------------------
async def _execute_one_tool_call(
    tool_call,
    *,
    request,
    metadata,
    model,
    user,
    tools,
    event_emitter,
    event_caller,
    response_handler_task,
):
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
    current_tool_parent_task_var.set(response_handler_task)
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
    # Container `bash` bookkeeping: when this call turns out to
    # be the container's bash tool we stamp the wall clock just
    # before it runs, so afterwards we can report exactly the
    # outputs/ files IT touched (and point at the reference doc
    # for their type). Initialized here so the post-processing
    # block below is safe on every other tool path.
    _is_container_bash = False
    _bash_t0 = 0.0

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
                    # Same identity test as the browser tools,
                    # for `bash`: stamp the clock so the
                    # post-call hook can report only the
                    # outputs/ files this command wrote.
                    _is_container_bash = bool(
                        tool.get("type") == "mcp"
                        and _container_id
                        and _normalize_container_server_id(
                            str(_tmeta.get("server_id", ""))
                        )
                        == _container_id
                        and str(_tmeta.get("original_name", ""))
                        == "bash"
                    )
                    if _is_container_bash:
                        _bash_t0 = time.time()
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
        ) = await process_tool_result(
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

    # Container bash: tell the model what landed in outputs/ and
    # which reference doc covers those file types. This rides on
    # the tool RESULT, not the system prompt -- results are
    # appended to the conversation and never rewritten, so it is
    # prompt-cache safe. (The live outputs listing used to sit in
    # messages[0], where every produced file invalidated the whole
    # conversation's cached prefix.) Best-effort and guarded on a
    # string result: never let this affect the tool outcome.
    if _is_container_bash and isinstance(tool_result, str):
        try:
            _suffix = await asyncio.to_thread(
                build_bash_result_suffix,
                str(
                    getattr(
                        request.app.state.config,
                        "CONTAINER_DATA_ROOT",
                        "",
                    )
                    or ""
                ),
                metadata.get("container_workspace_chat_id")
                or metadata.get("chat_id"),
                _bash_t0,
            )
            if _suffix:
                tool_result = f"{tool_result}\n{_suffix}"
        except Exception:
            log.debug(
                "container bash result suffix failed", exc_info=True
            )

    # Fold a raised-exception error into the structured meta so
    # the UI shows the error row. An explicit _owui_meta reason
    # (e.g. from a web tool) takes precedence over the generic
    # exception string.
    if tool_exec_error is not None:
        tool_result_meta = tool_result_meta or {}
        tool_result_meta["error"] = True
        tool_result_meta.setdefault("error_reason", tool_exec_error)
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
            {"error": True} if tool_result_meta.get("error") else {}
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


def _tool_result_event_data(message_id: str, slim_result: dict) -> dict:
    """Wire payload for one finished tool call (`tool_call:result`).

    Only keys the client can act on are sent — a slim result carries the body
    by reference (`result_ref`/`size`/`sha256`) rather than inline, and the
    optional metadata keys are omitted when empty so the batching layer has
    less to coalesce.
    """
    optional = (
        "result_ref",
        "size",
        "sha256",
        "summary",
        "files",
        "embeds",
        "subagent_id",
        "error_reason",
        "notice",
    )
    return {
        "message_id": message_id,
        "tool_call_id": slim_result.get("tool_call_id"),
        "result": slim_result.get("content"),
        **({"result_lazy": True} if slim_result.get("result_lazy") else {}),
        **({"error": True} if slim_result.get("error") else {}),
        **{
            key: slim_result[key]
            for key in optional
            if slim_result.get(key) not in (None, "", [], {})
        },
    }


def _tool_call_is_parallelizable(tool_call, tools):
    name = tool_call.get("function", {}).get("name", "")
    tool = tools.get(name)
    return bool(
        tool
        and tool.get("metadata", {}).get("parallelizable", False)
    )


def _tool_result_for_failed_call(tool_call, exc, *, request):
    """Build a non-empty error tool-result so one failed/cancelled
    parallel tool call doesn't abandon the whole round. The model
    sees the error for THAT call and can proceed with its siblings'
    results (or retry)."""
    tcid = (
        tool_call.get("id", "")
        if isinstance(tool_call, dict)
        else ""
    )
    name = (
        tool_call.get("function", {}).get("name", "")
        if isinstance(tool_call, dict)
        else ""
    )
    if isinstance(exc, asyncio.CancelledError):
        # Do not claim "timed out" here. A child CancelledError
        # means this one parallel tool/subagent was interrupted;
        # the parent task itself was NOT cancelling (checked just
        # below), and SUBAGENT_RUN_TIMEOUT may be disabled. The old
        # "cancelled or timed out" wording made normal interrupted
        # child tasks look like a configured timeout or user stop.
        msg = "was interrupted before it returned"
        error_reason = "interrupted"
    elif isinstance(exc, asyncio.TimeoutError):
        msg = "timed out before it returned"
        error_reason = "timed out"
    else:
        msg = f"failed: {exc}"
        error_reason = "failed"
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


# Post-turn background tasks (title/tags/follow-ups...), hoisted out of
# _process_chat_response_impl (2026-08-02 de-spaghettification); former
# closure captures are explicit keyword parameters.
async def _run_background_tasks(
    *, request, form_data, metadata, user, event_emitter, tasks
):
    message = None
    messages = []

    if "chat_id" in metadata and not metadata["chat_id"].startswith("local:"):
        messages_map = await Chats.get_messages_map_by_chat_id(metadata["chat_id"])
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

                # Default to [] so that EVERY requested follow-up generation
                # emits exactly one event below — including when
                # generate_follow_ups returns a non-dict JSONResponse
                # (follow-ups disabled mid-flight, or the follow-up task model
                # call itself errored). Without the unconditional emit the
                # client's reserved follow-up space would be held open forever.
                follow_ups = []
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

                    # Tolerate a reply with no / garbled JSON object. find("{")
                    # returns -1 when absent; the old naive slice then produced
                    # "" and json.loads raised, silently swallowing the result.
                    brace_start = follow_ups_string.find("{")
                    brace_end = follow_ups_string.rfind("}")
                    if brace_start != -1 and brace_end > brace_start:
                        try:
                            follow_ups = (
                                json.loads(
                                    follow_ups_string[brace_start : brace_end + 1]
                                ).get("follow_ups", [])
                                or []
                            )
                        except Exception:
                            follow_ups = []

                # Always emit — even an empty list — so the client can resolve
                # its "follow-up pending" state and release the reserved space
                # instead of leaving an empty gap above the input.
                await event_emitter(
                    {
                        "type": "chat:message:follow_ups",
                        "data": {
                            "follow_ups": follow_ups,
                        },
                    }
                )

                if follow_ups and not metadata.get("chat_id", "").startswith(
                    "local:"
                ):
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            "followUps": follow_ups,
                        },
                        return_model=False,
                    )

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

                            await Chats.update_chat_title_by_id(
                                metadata["chat_id"], title
                            )

                            await event_emitter(
                                {
                                    "type": "chat:title",
                                    "data": await _chat_title_event_payload(
                                        metadata["chat_id"], title
                                    ),
                                }
                            )
                    elif len(messages) == 2:
                        title = messages[0].get("content", user_message)

                        await Chats.update_chat_title_by_id(
                            metadata["chat_id"], title
                        )

                        await event_emitter(
                            {
                                "type": "chat:title",
                                "data": await _chat_title_event_payload(
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
                            await Chats.update_chat_tags_by_id(
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
    generation_operation_released = False

    async def _release_completed_generation_operation() -> None:
        """End provider ownership before attempting the next queued turn.

        A task remains alive for a few final bookkeeping instructions after its
        response is durably complete. Releasing at this explicit terminal
        boundary lets the queue atomically claim the next turn without treating
        the finishing task as concurrent work. For multi-model turns, each
        sibling releases only itself; the shared turn lease remains until the
        last sibling completes.
        """
        nonlocal generation_operation_released
        if generation_operation_released:
            return
        operation = metadata.get("generation_operation")
        if not isinstance(operation, dict):
            return
        if str(operation.get("chat_id") or "") != str(
            metadata.get("chat_id") or ""
        ) or str(operation.get("message_id") or "") != str(
            metadata.get("message_id") or ""
        ):
            return
        from open_webui.tasks import unregister_generation_operation

        await unregister_generation_operation(
            getattr(request.app.state, "redis", None), operation
        )
        generation_operation_released = True


    event_emitter = None
    event_caller = None

    # Saved-chat event/persistence plumbing does not depend on a live origin
    # socket. With no session, the emitter fans out to the user's available tabs
    # and v2.1 stream state remains replayable; interactive callbacks use the
    # non-blocking headless caller below. Local/direct requests still require a
    # real session (or an explicitly headless run).
    if should_attach_chat_event_transport(metadata):
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

        if STREAM_PROTOCOL_VERSION == "v2.1" and not metadata.get(
            "event_emitter_override"
        ):
            event_emitter = _wrap_event_emitter_v21(event_emitter, metadata)

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
    _ns_terminal = {"committed": False, "errored": False}

    async def _commit_nonstreaming_error(error: Any) -> None:
        """Persist an explicit terminal failure before exposing it to clients."""

        if _ns_terminal["committed"]:
            return
        error_payload = _provider_error_payload(error)
        if not error_payload.get("content"):
            error_payload = {
                "content": "The model request ended before a response could be saved."
            }

        if (
            metadata.get("chat_id")
            and metadata.get("message_id")
            and not str(metadata.get("chat_id", "")).startswith("local:")
        ):
            await Chats.upsert_message_to_chat_by_id_and_message_id(
                metadata["chat_id"],
                metadata["message_id"],
                {
                    "role": "assistant",
                    "generation_id": metadata.get("generation_id"),
                    "turn_id": metadata.get("turn_id"),
                    "error": error_payload,
                    "done": True,
                },
                return_model=False,
            )
            if STREAM_PROTOCOL_VERSION == "v2.1":
                set_stream_state(
                    metadata["message_id"],
                    {"status": "error", "error": error_payload},
                )

        _ns_terminal["committed"] = True
        _ns_terminal["errored"] = True
        if event_emitter:
            await event_emitter(
                {
                    "type": "chat:message:error",
                    "data": {"error": error_payload},
                }
            )

    async def _finalize_nonstreaming_queue(errored: bool):
        if _ns_finalized["done"]:
            return
        _ns_finalized["done"] = True
        if not (metadata.get("chat_id") and metadata.get("message_id")):
            return
        if str(metadata.get("chat_id", "")).startswith("local:"):
            return
        if not _ns_terminal["committed"]:
            await _commit_nonstreaming_error(
                "The model returned no response content."
                if not errored
                else "The model request failed before its response was saved."
            )
        errored = _ns_terminal["errored"]
        # Settle the v2.1 stream store so a reloaded/zero-tab client sees a terminal
        # state (not a perpetual "in_progress" cursor on the headless placeholder
        # registered before the generation).
        if STREAM_PROTOCOL_VERSION == "v2.1":
            try:
                set_stream_state(
                    metadata["message_id"],
                    {"status": "error" if errored else "done"},
                )
                clear_stream_state(metadata["message_id"])
            except Exception:
                log.exception("non-streaming stream-state settle failed")
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

                await _release_completed_generation_operation()
                await maybe_drain_queue(
                    request.app,
                    user,
                    metadata["chat_id"],
                    finished_response_id=metadata.get("message_id"),
                )
            except Exception:
                log.exception("queue drain after non-streaming completion failed")

    agentic_nonstreaming_response = (
        _should_handle_nonstreaming_response_in_agentic_loop(
            response, form_data, metadata
        )
    )
    if agentic_nonstreaming_response and not (event_emitter and event_caller):
        agentic_nonstreaming_response = False

    # Non-streaming response
    if (
        not isinstance(response, StreamingResponse)
        and not agentic_nonstreaming_response
    ):
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
                    reasoning_content = await _visible_nonstreaming_reasoning(message)

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
                    await _commit_nonstreaming_error(response_data.get("error"))
                else:
                    # Get content from message (reasoning already processed earlier).
                    choices = response_data.get("choices", [])
                    message_data = (
                        choices[0].get("message")
                        if choices and isinstance(choices[0], dict)
                        else None
                    )
                    content = (
                        get_content_from_message(message_data)
                        if isinstance(message_data, dict)
                        else None
                    ) or response_data.get("content", "")

                    if isinstance(content, str) and content:
                        usage = response_data.get("usage", {})
                        content_blocks = text_content_blocks(content)
                        update_data = {
                            "role": "assistant",
                            "content": content,
                            "content_blocks": content_blocks,
                            "generation_id": metadata.get("generation_id"),
                            "turn_id": metadata.get("turn_id"),
                            "done": True,
                        }
                        if "selected_model_id" in response_data:
                            update_data["selectedModelId"] = response_data[
                                "selected_model_id"
                            ]
                        if usage_has_data(usage):
                            update_data["usage"] = usage

                        reasoning_details = (
                            message_data.get("reasoning_details")
                            if isinstance(message_data, dict)
                            else response_data.get("reasoning_details")
                        )
                        if reasoning_details:
                            update_data["reasoning_details"] = reasoning_details
                            # Write per_round too so the on-disk shape is
                            # symmetric with streaming. See
                            # utils/REASONING_DETAILS.md §6 Bug C.
                            update_data["reasoning_details_per_round"] = [
                                reasoning_details
                            ]

                        # ROOT ORDERING INVARIANT: content + done are one durable
                        # mutation, and it completes before any terminal event or
                        # fallible analytics/webhook/background side effect. A
                        # sleeping phone can therefore reconnect at any later
                        # point and reconstruct the answer from storage.
                        await Chats.upsert_message_to_chat_by_id_and_message_id(
                            metadata["chat_id"],
                            metadata["message_id"],
                            update_data,
                            return_model=False,
                        )
                        _ns_terminal["committed"] = True
                        _ns_terminal["errored"] = False
                        if STREAM_PROTOCOL_VERSION == "v2.1":
                            state_patch = {
                                "content_blocks": content_blocks,
                                "status": "done",
                            }
                            if usage_has_data(usage):
                                state_patch["usage"] = usage
                            if "selected_model_id" in response_data:
                                state_patch["selected_model_id"] = response_data[
                                    "selected_model_id"
                                ]
                            set_stream_state(metadata["message_id"], state_patch)

                        try:
                            title = await Chats.get_chat_title_by_id(
                                metadata["chat_id"]
                            )
                        except Exception:
                            title = None

                        try:
                            container_output_files = (
                                await import_changed_container_outputs(
                                    request, metadata, user, content=content
                                )
                            )
                        except Exception:
                            log.exception(
                                "non-streaming container output import failed"
                            )
                            container_output_files = []

                        if container_output_files:
                            try:
                                current_message = (
                                    await Chats.get_message_by_id_and_message_id(
                                        metadata["chat_id"], metadata["message_id"]
                                    )
                                    or {}
                                )
                                await Chats.upsert_message_to_chat_by_id_and_message_id(
                                    metadata["chat_id"],
                                    metadata["message_id"],
                                    {
                                        "files": current_message.get(
                                            "files", container_output_files
                                        )
                                    },
                                    return_model=False,
                                )
                                await event_emitter(
                                    {
                                        "type": "files",
                                        "data": {"files": container_output_files},
                                    }
                                )
                            except Exception:
                                log.exception(
                                    "non-streaming container output persistence failed"
                                )

                        # Live delivery follows the durable commit. The first event
                        # carries the provider response shape; the second is the
                        # terminal projection used by the ordinary chat client.
                        await event_emitter(
                            {
                                "type": "chat:completion",
                                "data": response_data,
                            }
                        )
                        completion_data = {
                            "done": True,
                            "content": content,
                            "title": title,
                        }
                        if container_output_files:
                            completion_data["files"] = container_output_files
                        if usage_has_data(usage):
                            completion_data["usage"] = usage
                            completion_data["selected_model_id"] = model_id
                        await event_emitter(
                            {
                                "type": "chat:completion",
                                "data": completion_data,
                            }
                        )

                        # These are post-commit side effects. Their failure must
                        # never turn a successfully stored answer into a blank
                        # terminal row.
                        if usage_has_data(usage):
                            try:
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
                            except Exception:
                                log.exception(
                                    "non-streaming token usage processing failed"
                                )

                        try:
                            if not get_active_status_by_user_id(user.id):
                                webhook_url = await Users.get_user_webhook_url_by_id(
                                    user.id
                                )
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
                        except Exception:
                            log.exception("non-streaming webhook failed")

                        try:
                            await _run_background_tasks(
                            request=request,
                            form_data=form_data,
                            metadata=metadata,
                            user=user,
                            event_emitter=event_emitter,
                            tasks=tasks,
                        )
                        except Exception:
                            log.exception(
                                "non-streaming background task processing failed"
                            )

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
                log.exception("Error occurred while processing non-streaming response")
                if not _ns_terminal["committed"]:
                    await _commit_nonstreaming_error(e)

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
        await Functions.get_function_by_id(filter_id)
        for filter_id in await get_sorted_filter_ids(
            request, model, metadata.get("filter_ids", [])
        )
    ]

    # Streaming response
    if event_emitter and event_caller:
        task_id = str(uuid4())  # Create a unique task ID.

        # Handle as a background task
        async def response_handler(response, events):
            nonlocal model_id
            response_handler_task = asyncio.current_task()

            # Display projection, hoisted to streaming/serialize.py; bind
            # this turn's metadata/request once so the 10 call sites below
            # keep their historical one-arg shape.
            serialize_content_blocks = partial(
                _serialize_content_blocks, metadata=metadata, request=request
            )

            message = await Chats.get_message_by_id_and_message_id(
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
            # Set by _run_round_with_retry only when every retry for its CURRENT
            # round has failed. The response-reading branches consume it
            # immediately to preserve that fact in the canonical error payload.
            round_retries_exhausted = False
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
            # Generation-local body ledgers — the correctness backbone for lazy
            # tool results.
            #
            # WHY: the socket store (TOOL_RESULT_BODIES) is shared, capped, and
            # actively pruned by EXTERNAL actors — finalize / cancel-teardown of a
            # racing attempt on the SAME message_id calls clear_tool_result_bodies,
            # the 300s STREAM_DONE_GRACE cleanup wipes a message's bodies, the
            # per-message/global byte caps evict LRU bodies (with a best-effort disk
            # spill that can itself fail), and any full store wipe drops everything.
            # None of those may be allowed to remove a body that the RUNNING
            # generation still needs to replay to the model on its next round. A
            # production incident did exactly that: the RAM store was wiped mid-
            # generation, the next checkpoint then persisted a near-empty
            # tool_result_bodies map, and the following round raised "Missing tool
            # result body for ref ...", killing the whole turn.
            #
            # So we keep our OWN copy of every body this generation has produced or
            # seen, immune to those external wipes, and merge it into the outbound
            # conversion (see _current_tool_result_bodies). The socket store is now
            # purely a UI-serving cache for correctness purposes — losing it can
            # only degrade lazy-expansion latency, never the model's context.
            # Bodies are treated read-only downstream, so we hold references (no
            # deepcopy — these can be large).
            generation_tool_result_bodies: dict = {}
            # Bodies not yet durably merged into the DB row (Layer 2 write-through
            # retry buffer): populated alongside every store write, drained by the
            # per-round Chats.merge_message_tool_result_bodies call, and left intact
            # (to retry next round) whenever that write fails.
            pending_db_body_merges: dict = {}
            if metadata.get("message_id") and isinstance(
                persisted_tool_result_bodies, dict
            ):
                for _tcid, _body in persisted_tool_result_bodies.items():
                    if isinstance(_body, dict):
                        set_tool_result_body(metadata.get("message_id"), _tcid, _body)
                        # These bodies came FROM the DB row, so they are already
                        # durable — seed the generation ledger (immunity to a later
                        # store wipe) but NOT the pending-merge buffer.
                        generation_tool_result_bodies[str(_tcid)] = _body

            # Retry-last-request can pre-seed the assistant row with completed
            # tool-call rounds. Continue streaming from those structured blocks
            # instead of flattening them to a single text block, otherwise v2.1
            # would resend the whole agentic turn instead of just the final
            # post-tool request.
            #
            # But resume at the RESUME BOUNDARY, not wherever the dead attempt's
            # bytes happened to stop. When the row is the wreckage of an attempt
            # that failed (`is_aborted_attempt`), its trailing prose is about to
            # be regenerated — and appending onto it is what produced the
            # five-answers-in-one-block garble, because the stream handler writes
            # into `content_blocks[-1]` whenever that is a text block. A cleanly
            # finished row is Continue Response, which deliberately re-opens that
            # trailing block; never trim it. Mirrors the identical trim applied to
            # the OUTBOUND payload in `assemble_conversation_from_leaf`, so what
            # we render and what the model sees agree.
            aborted_tail_trimmed = False
            pre_trim_client_blocks = None
            if (
                isinstance(existing_content_blocks, list)
                and existing_content_blocks
                and is_aborted_attempt(message)
            ):
                resumable = resume_boundary_blocks(existing_content_blocks)
                if resumable is not existing_content_blocks:
                    aborted_tail_trimmed = True
                    # What the OPEN TAB is still rendering. It kept the failed
                    # attempt's blocks on purpose (wiping them blanks the visible
                    # partial turn), so the mirror has to be seeded with THIS list
                    # for the first v2.1 diff to come out as a truncation and ship
                    # the `replace` that removes the stump. Seeding with the
                    # trimmed list instead would leave the client one block-index
                    # ahead of the server for the rest of the turn.
                    pre_trim_client_blocks = copy.deepcopy(existing_content_blocks)
                    log.info(
                        "retry resume: dropped %s regenerable trailing block(s) from "
                        "the previous attempt (chat=%s message=%s)",
                        len(existing_content_blocks) - len(resumable),
                        metadata.get("chat_id"),
                        metadata.get("message_id"),
                    )
                    existing_content_blocks = resumable

            if isinstance(existing_content_blocks, list) and existing_content_blocks:
                content_blocks = copy.deepcopy(existing_content_blocks)
            elif aborted_tail_trimmed:
                # The trim emptied the list: the dead attempt produced nothing but
                # prose. Start clean — do NOT fall through to the legacy `content`
                # seeding below, which would re-seed the very text we just dropped.
                content_blocks = []
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

            if aborted_tail_trimmed:
                # The legacy `content` string was read off the same row and is the
                # projection of the blocks we just dropped. It seeds `content_parts`
                # below (and through it the plain-text used for the inactive-user
                # webhook), so leaving it would re-introduce the dead attempt's
                # answer through the one path that doesn't read content_blocks.
                content = ""

            # O(1)-amortized tail accumulation for the streaming hot path — see
            # TailAccumulator in streaming/accumulate.py. Lives in
            # `response_handler` scope (not inside `stream_body_handler`) so the
            # checkpoint/finalize paths can materialize the tail before reading
            # content_blocks; `stream_body_handler` rebinds `content_blocks` via
            # `nonlocal` each round and the bound block changes with it.
            tail_acc = TailAccumulator()

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
            # Seed must be exactly as long as the blocks expand to, or every
            # later round's reasoning lands on the wrong emission (see the
            # function's docstring for the retry-seam incident). Same correction
            # `_run_round_with_retry` applies when it rolls back an unproductive
            # round.
            align_reasoning_rounds_to_blocks(round_reasoning_details, content_blocks)

            if (
                STREAM_PROTOCOL_VERSION == "v2.1"
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
                        # Freshly split from inline content: hold a wipe-immune copy
                        # AND queue it for durable write-through (it isn't in the
                        # slim DB row yet).
                        generation_tool_result_bodies[str(_tcid)] = _body
                        pending_db_body_merges[str(_tcid)] = _body

            if (
                STREAM_PROTOCOL_VERSION == "v2.1"
                and metadata.get("message_id")
                # `or aborted_tail_trimmed`: a dead attempt that produced nothing
                # but prose trims to an EMPTY list, and that emptiness is exactly
                # what the client has to be told about. Gating on truthy
                # `content_blocks` would skip the resync and leave the tab showing
                # a half-sentence for the rest of the turn.
                and (content_blocks or aborted_tail_trimmed)
            ):
                initial_v21_blocks = copy.deepcopy(_strip_tool_results(content_blocks))
                v21_mirror = getattr(event_emitter, "_v21_mirror", None)
                if v21_mirror is not None:
                    if aborted_tail_trimmed and pre_trim_client_blocks is not None:
                        v21_mirror["blocks"] = _strip_tool_results(
                            pre_trim_client_blocks
                        )
                    else:
                        v21_mirror["blocks"] = initial_v21_blocks
                set_stream_state(
                    metadata["message_id"],
                    {
                        "content_blocks": initial_v21_blocks,
                        "status": "in_progress",
                        # Baseline snapshot_version so the /snapshot endpoint never
                        # advertises the live wire counter (which races ahead of the
                        # cadence-written RAM content) before the first cadence
                        # snapshot lands. The content here matches the current
                        # version (no deltas emitted yet this round).
                        "snapshot_version": stream_version_get(metadata["message_id"]),
                    },
                )
                if aborted_tail_trimmed and v21_mirror is not None:
                    # Ship the truncation NOW, before the first token of the new
                    # attempt. `_emit_delta_for_blocks` sees fewer blocks than the
                    # mirror (seeded above with the client's copy) and falls back
                    # to a whole-list `replace`, which also resets the mirror to
                    # the trimmed list. Without this the open tab keeps the stump
                    # and every later `block_idx` is off by the number of blocks
                    # we dropped.
                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": {"content_blocks": content_blocks},
                        }
                    )

            # Avoid copying the whole growing plain-text response on every SSE
            # chunk. Native provider reasoning fields are rendered from
            # structured `content_blocks`; legacy inline reasoning-tag scanning
            # has been removed. Hidden v2.1 subagent runs never need the legacy
            # string.
            track_legacy_content = not (
                STREAM_PROTOCOL_VERSION == "v2.1" and metadata.get("subagent_inner")
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
            CHECKPOINT_INTERVAL_SECONDS = STREAM_DB_CHECKPOINT_INTERVAL_SECONDS
            CHECKPOINT_CHAR_DELTA = STREAM_DB_CHECKPOINT_CHAR_DELTA

            def get_plain_content() -> str:
                nonlocal content, content_dirty
                if not track_legacy_content:
                    return content
                if content_dirty:
                    content = "".join(content_parts)
                    content_dirty = False
                return content

            def _current_tool_result_bodies(extra_bodies=None):
                live_bodies = (
                    get_tool_result_bodies(metadata.get("message_id"), deep_copy=False)
                    if metadata.get("message_id")
                    else {}
                )
                # Layer the generation-local ledger AFTER the (wipe-prone) socket
                # store so a body this generation produced survives even if the
                # store lost it. Order matters only for value freshness, not key
                # coverage — all four layers union by tool_call_id.
                return _merge_tool_result_body_maps(
                    persisted_tool_result_bodies,
                    live_bodies,
                    generation_tool_result_bodies,
                    extra_bodies,
                )

            def _blocks_have_subagent_calls(blocks) -> bool:
                # Cheap in-memory scan: True iff any tool_calls block contains a
                # subagent_launch / subagent_continue call. Used to gate the D4
                # checkpoint pre-reconcile so non-subagent streams pay no extra
                # DB read on the checkpoint cadence.
                if not isinstance(blocks, list):
                    return False
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "tool_calls":
                        continue
                    calls = block.get("content")
                    if not isinstance(calls, list):
                        continue
                    for call in calls:
                        if not isinstance(call, dict):
                            continue
                        name = (call.get("function") or {}).get("name") or call.get(
                            "name"
                        )
                        if name in (
                            "subagent_launch",
                            "subagent_continue",
                            "subagent_agent_launch",
                        ):
                            return True
                return False

            def _build_checkpoint_update(include_legacy_content: bool = False):
                # Fold any buffered tail text into its block before reading
                # content_blocks, so the checkpoint/snapshot/persist carries the
                # full text (the streaming hot path leaves the tail in an
                # accumulator for O(1) appends).
                tail_acc.materialize()
                if STREAM_PROTOCOL_VERSION == "v2.1" and not str(
                    metadata.get("chat_id", "")
                ).startswith("local:"):
                    slim_blocks, split_bodies = split_tool_result_bodies(content_blocks)
                    # Reasoning text gets the same treatment as tool bodies:
                    # closed blocks over the inline threshold persist as a
                    # "Thought for N seconds" stub + a body-map entry, in the
                    # SAME upsert (stub and body land atomically). The RAM
                    # stream state / snapshot keeps the full text, so a client
                    # watching the live stream is unaffected. The bodies are
                    # re-derived from the in-memory blocks on every checkpoint,
                    # so no generation ledger entry is needed for them.
                    #
                    # Subagent INNER chats are exempt: their rows are only ever
                    # read wholesale by the parent's transcript card (already
                    # lazy at the card level via getChatById-on-expand), and
                    # that card renders reasoning straight from the block —
                    # stubbing it would blank transcripts, and the subagent
                    # machinery's observed behavior must not change.
                    if not metadata.get("subagent_inner"):
                        slim_blocks, reasoning_bodies = split_reasoning_bodies(
                            slim_blocks
                        )
                        if reasoning_bodies:
                            split_bodies = {**split_bodies, **reasoning_bodies}
                    tool_result_bodies = _current_tool_result_bodies(split_bodies)
                else:
                    slim_blocks, tool_result_bodies = content_blocks, {}
                update_data = {"content_blocks": slim_blocks}
                if metadata.get("generation_id") and metadata.get("turn_id"):
                    update_data["generation_id"] = metadata["generation_id"]
                    update_data["turn_id"] = metadata["turn_id"]
                if tool_result_bodies:
                    update_data["tool_result_bodies"] = tool_result_bodies
                if include_legacy_content:
                    update_data["content"] = text_only_content_from_blocks(slim_blocks)
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
                """Durable checkpoint for v2.1 streams. The RAM stream store is
                the live source of truth; DB checkpoints are intentionally
                coarse so high-TPS streams do not commit per token.

                The cheap threshold gate runs on the event loop; only the actual
                durable write is offloaded to the async DB bridge so a checkpoint
                on one stream never freezes the single loop for every other
                concurrent stream. Building the update dict happens on-loop
                (consistent snapshot of this stream's content_blocks, which only
                this coroutine mutates) and the finished dict is handed to the
                thread — `upsert_message_...` opens its own DB session, so there
                is no cross-thread session sharing."""
                nonlocal last_checkpoint_at, checkpoint_chars_since
                if STREAM_PROTOCOL_VERSION != "v2.1":
                    return
                if not metadata.get("chat_id") or not metadata.get("message_id"):
                    return
                if str(metadata.get("chat_id", "")).startswith("local:"):
                    return
                if STREAM_DB_CHECKPOINT_POLICY == "final_only":
                    return

                checkpoint_chars_since += max(0, int(char_delta or 0))
                now = time.monotonic()
                if not force:
                    if (
                        checkpoint_chars_since < CHECKPOINT_CHAR_DELTA
                        and now - last_checkpoint_at < CHECKPOINT_INTERVAL_SECONDS
                    ):
                        return

                # D4: a mid-stream checkpoint builds content_blocks PURELY from the
                # parent's in-memory list, then upserts it as the message's
                # content_blocks. If a detached rerun wrote a subagent answer into
                # the DB (via subagent_runs) that this in-memory list lacks, the
                # checkpoint would clobber it with an empty/absent result. Mirror the
                # freshly-read subagent_runs into the in-memory blocks first.
                # reconcile_block_results_from_runs only FILLS empties (it skips any
                # result whose content is already non-empty, and lazy refs), so it
                # never overwrites a real answer. Inlined (not via
                # _reconcile_subagent_results) so it can be gated by a cheap
                # in-memory scan — non-subagent streams (the high-TPS hot path)
                # pay zero extra DB cost per checkpoint.
                try:
                    if _blocks_have_subagent_calls(content_blocks):
                        from open_webui.utils.subagent import (
                            reconcile_block_results_from_runs,
                        )

                        _ckpt_msg = (
                            await Chats.get_message_by_id_and_message_id(
                                metadata["chat_id"], metadata["message_id"]
                            )
                            or {}
                        )
                        _ckpt_runs = _ckpt_msg.get("subagent_runs")
                        if isinstance(_ckpt_runs, dict) and _ckpt_runs:
                            reconcile_block_results_from_runs(
                                content_blocks, _ckpt_runs
                            )
                except Exception:
                    log.exception("checkpoint pre-reconcile failed")

                update_data = _build_checkpoint_update(include_legacy_content)
                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    metadata["chat_id"],
                    metadata["message_id"],
                    update_data,
                    return_model=False,
                )
                last_checkpoint_at = now
                checkpoint_chars_since = 0

            # Defined BEFORE the try: below (i.e. before the first await of the
            # response) — the CancelledError teardown references these, and a Stop
            # can land while the very first round is still streaming. When they
            # were defined between the first round and the tool loop, an early
            # cancel hit an unbound closure cell ("cannot access free variable
            # '_reconcile_subagent_results'"), which killed the teardown before
            # done=True was persisted and surfaced to the user as a retryable
            # error instead of a clean stop.
            async def _reconcile_subagent_results():
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
                        await Chats.get_message_by_id_and_message_id(
                            metadata["chat_id"], metadata["message_id"]
                        )
                        or {}
                    )
                    runs = msg.get("subagent_runs")
                    if isinstance(runs, dict) and runs:
                        reconcile_block_results_from_runs(content_blocks, runs)
                except Exception:
                    log.exception("subagent result reconciliation failed")

            async def _sweep_subagent_runs(fallback_status="cancelled"):
                """Finalizer backstop for the invariant 'parent terminal =>
                every subagent_runs entry terminal'. Flips any straggler
                'running' entry to a terminal status (prefer 'done' when it has
                a real result, else fallback). Call BEFORE _reconcile so a
                newly-'done' straggler's answer gets mirrored into
                content_blocks. No-op for non-subagent runs."""
                if not metadata.get("chat_id") or not metadata.get("message_id"):
                    return
                if str(metadata.get("chat_id", "")).startswith("local:"):
                    return
                try:
                    from open_webui.utils.subagent import (
                        sweep_subagent_runs_terminal,
                    )

                    await sweep_subagent_runs_terminal(
                        metadata["chat_id"],
                        metadata["message_id"],
                        fallback_status=fallback_status,
                    )
                    # ROOT GUARANTEE: now that the durable subagent_runs are
                    # authoritative, FAN every run's terminal out to all of the
                    # user's tabs (bypassing the stream-scoped/visibility-gated
                    # per-update path) so no card is left spinning "Researching…"
                    # once the parent finalizes — without needing a reload. Runs
                    # before the parent's own chat:done, so cards resolve first.
                    from open_webui.utils.subagent import (
                        broadcast_subagent_terminals,
                    )

                    await broadcast_subagent_terminals(
                        metadata["chat_id"],
                        metadata["message_id"],
                        metadata.get("user_id"),
                    )
                except Exception:
                    log.exception("subagent terminal sweep failed")

            try:
                for event in events:
                    await event_emitter(
                        {
                            "type": "chat:completion",
                            "data": event,
                        }
                    )

                    # Save message in the database
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            **event,
                        },
                        return_model=False,
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

                    # ── Native v2.1 fast-path bookkeeping ──────────────────
                    # Under STREAM_PROTOCOL_VERSION=="v2.1" we emit `chat:delta`
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
                    _v21_native = (
                        STREAM_PROTOCOL_VERSION == "v2.1"
                        and getattr(event_emitter, "_v21_mirror", None) is not None
                        and metadata.get("message_id")
                    )
                    _v21_mirror = (
                        getattr(event_emitter, "_v21_mirror", None)
                        if _v21_native
                        else None
                    )
                    _v21_emit_raw = (
                        getattr(event_emitter, "_emit_raw_primary", None)
                        if _v21_native
                        else None
                    )
                    _v21_message_id = (
                        metadata.get("message_id") if _v21_native else None
                    )

                    # Throttled event-loop yield. The per-token awaits on the v2.1
                    # hot path (`_v21_emit_raw` / `event_emitter`) only ENQUEUE into
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
                    # Coalescing gate clock: last instant a native text_append was
                    # actually emitted. Bounds trickle latency (a slow stream still
                    # flushes within STREAM_TEXT_COALESCE_WINDOW_S).
                    _last_native_emit_at = time.monotonic()

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

                    def _write_stream_snapshot(snapshot_version=None, dirty_from=None):
                        nonlocal _last_snapshot_at, _snapshot_chars_since
                        nonlocal _snapshot_established
                        if not _v21_message_id:
                            return
                        tail_acc.materialize()
                        patch = {
                            "content_blocks": _strip_tool_results(content_blocks),
                            "status": "in_progress",
                        }
                        if snapshot_version is not None:
                            patch["snapshot_version"] = snapshot_version
                        if dirty_from is not None:
                            patch["content_blocks_dirty_from"] = dirty_from
                        set_stream_state(_v21_message_id, patch)
                        if snapshot_version is not None:
                            stream_version_flush(_v21_message_id)
                        _last_snapshot_at = time.monotonic()
                        _snapshot_chars_since = 0
                        _snapshot_established = True

                    def _maybe_snapshot_stream_state(
                        snapshot_version=None, char_delta=0, dirty_from=None
                    ):
                        """Write the RAM snapshot if the bounded cadence
                        (time or chars) has elapsed. `snapshot_version` is the last
                        wire version whose content is fully contained in this
                        snapshot. Cheap no-op between cadence points.

                        The FIRST call always writes (force a baseline) so the
                        /snapshot endpoint has a (content, snapshot_version) pair
                        and never falls back to advertising the live wire counter,
                        which races ahead of the cadence-written RAM content."""
                        nonlocal _snapshot_chars_since
                        if not _v21_message_id:
                            return
                        _snapshot_chars_since += max(0, int(char_delta or 0))
                        now = time.monotonic()
                        if (
                            not _snapshot_established
                            or _snapshot_chars_since >= SNAPSHOT_CHAR_DELTA
                            or now - _last_snapshot_at >= SNAPSHOT_INTERVAL_SECONDS
                        ):
                            _write_stream_snapshot(
                                snapshot_version=snapshot_version, dirty_from=dirty_from
                            )

                    def _v21_try_native_append(peek: bool = False):
                        """Return (block_idx, appended_text, None) if the tail
                        block is a pure append since the last mirror sync AND no
                        earlier block changed; otherwise None to force a
                        translator-mediated full diff. Uses the accumulator's emit
                        cursor (O(appended)) instead of a full-string startswith
                        (which was O(N) per token → O(N^2) per stream).

                        With `peek=True` the accumulator is NOT consumed: returns
                        (block_idx, pending_chars) when a native append is possible
                        (else None). The coalescing gate uses this to decide whether
                        to hold small per-token appends before assigning a version."""
                        if not _v21_native or not content_blocks:
                            return None
                        mirror_blocks = _v21_mirror.get("blocks") or []
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
                        if (
                            tail_acc.acc is None
                            or tail_acc.block is not tail
                        ):
                            return None
                        if peek:
                            pending = tail_acc.acc.pending_len
                            if not pending:
                                return None
                            return tail_idx, pending
                        appended = tail_acc.acc.take_appended()
                        if not appended:
                            return None
                        return tail_idx, appended, None

                    async def flush_pending_delta_data(
                        threshold: int = 0, *, force: bool = False
                    ):
                        nonlocal delta_count
                        nonlocal last_delta_data
                        nonlocal _last_native_emit_at

                        if delta_count >= threshold and last_delta_data:
                            if event_emitter is None:
                                log.error(
                                    f"❌ FLUSH ERROR: event_emitter is None! Cannot emit events!"
                                )
                            else:
                                # Coalescing gate: for a pure tail-block text append
                                # (native-eligible), hold small per-token appends
                                # until MIN_CHARS accumulate OR the coalesce window
                                # elapses, so N tokens collapse into ONE versioned
                                # text_append (one version bump — strictly
                                # contiguous, so the client's version-gap guard never
                                # trips). Deferring leaves the text in the tail
                                # accumulator and keeps delta_count/last_delta_data,
                                # so the next token retries; structural changes
                                # (native peek is None) and force flushes never defer.
                                # The RAM snapshot is only ever stamped AFTER a native
                                # emit here, so held-but-unemitted text is never
                                # advertised at a stale version (coherence preserved).
                                if (
                                    not force
                                    and _v21_native
                                    and STREAM_TEXT_COALESCE_MIN_CHARS > 0
                                    and "content_blocks" in (last_delta_data or {})
                                ):
                                    peek = _v21_try_native_append(peek=True)
                                    if (
                                        peek is not None
                                        and peek[1] < STREAM_TEXT_COALESCE_MIN_CHARS
                                        and (time.monotonic() - _last_native_emit_at)
                                        < STREAM_TEXT_COALESCE_WINDOW_S
                                    ):
                                        return
                                native = (
                                    _v21_try_native_append()
                                    if _v21_native
                                    and "content_blocks" in (last_delta_data or {})
                                    else None
                                )
                                if native is not None:
                                    block_idx, appended, _ = native
                                    _last_native_emit_at = time.monotonic()
                                    last_native_version = None
                                    for text_chunk in _split_text_by_utf8_bytes(
                                        appended
                                    ):
                                        version = stream_version_incr(_v21_message_id)
                                        last_native_version = version
                                        payload = {
                                            "type": "chat:delta",
                                            "data": {
                                                "message_id": _v21_message_id,
                                                "version": version,
                                                "op": "text_append",
                                                "payload": {
                                                    "block_idx": block_idx,
                                                    "text": text_chunk,
                                                },
                                            },
                                        }
                                        await _v21_emit_raw(payload)
                                    # Advance the translator's mirror for this
                                    # block by LENGTH only — never by aliasing or
                                    # concatenating the growing string (that would
                                    # reintroduce the O(N^2) concat). The mirror's
                                    # stale `content` string is reconciled inside
                                    # _emit_delta_for_blocks itself, which honors
                                    # this `_emitted_len` cursor whenever the
                                    # translator path next diffs the block (at any
                                    # call site — round boundaries, fallbacks, etc).
                                    mirror_block = _v21_mirror["blocks"][block_idx]
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
                                        dirty_from=block_idx,
                                    )
                                else:
                                    # Translator fallback: it diffs the full
                                    # materialized content_blocks, so fold the tail
                                    # buffer back first. The translator itself
                                    # honors the mirror's `_emitted_len` cursor (set
                                    # by prior native flushes) when diffing, so the
                                    # mirror reconciles correctly without a separate
                                    # pass here.
                                    tail_acc.materialize()
                                    await event_emitter(
                                        {
                                            "type": "chat:completion",
                                            "data": last_delta_data,
                                        }
                                    )
                                    # The translator drained the tail; keep the
                                    # accumulator's emit cursor consistent so a
                                    # subsequent native flush won't re-ship text.
                                    if tail_acc.acc is not None:
                                        tail_acc.acc.take_appended()
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
                                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                                        metadata["chat_id"],
                                        metadata["message_id"],
                                        {
                                            "selectedModelId": model_id,
                                        },
                                        return_model=False,
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
                                    # Only treat a usage chunk as real if it
                                    # carries countable tokens/cost. This provider
                                    # emits zero-filled `usage` on intermediate
                                    # chunks; without this guard `response_usage`
                                    # (persisted as the message's final usage) gets
                                    # clobbered to 0 and a zero live delta is
                                    # emitted. See usage_has_data().
                                    if usage_has_data(usage):
                                        # Reshape the broken "C" gateway blob (top-
                                        # level reasoning excluded from completion)
                                        # into canonical shape BEFORE it is emitted,
                                        # counted, and persisted as meta.usage.
                                        usage = normalize_provider_usage(usage)
                                        response_usage = (
                                            usage  # Store for final completion event
                                        )
                                        # Emit the optimistic per-round op=usage
                                        # delta BEFORE process_token_usage. The
                                        # latter writes conversation_token_usage and
                                        # then pushes the authoritative cumulative
                                        # totals (chat:token-usage). The frontend's
                                        # optimistic op=usage path undercounts a
                                        # multi-round turn, so it must land FIRST and
                                        # let the authoritative push max-correct it —
                                        # the reverse order would let the optimistic
                                        # delta accumulate on top of the already-correct
                                        # total and overcount.
                                        await event_emitter(
                                            {
                                                "type": "chat:completion",
                                                "data": {
                                                    "usage": usage,
                                                },
                                            }
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
                                        # Normalize to the canonical error shape
                                        # ({"content": str}) HERE, where the raw
                                        # provider payload enters the pipeline.
                                        # The live chat:message:error emit, the
                                        # RAM snapshot, and the terminal DB
                                        # persist all forward this payload, and
                                        # the frontend reads error.content — a
                                        # raw {"message"/"code"} provider shape
                                        # here rendered a BLANK live error box
                                        # while a reload showed the real error.
                                        error_payload = _provider_error_payload(
                                            chunk_error
                                            or "Provider returned an error during streaming."
                                        )
                                        terminal_error = error_payload
                                        # Log the raw provider error too so
                                        # fields the extractor drops (code,
                                        # metadata.raw) are never silently lost.
                                        log.error(
                                            "mid-stream provider error "
                                            f"chat={metadata.get('chat_id')} "
                                            f"message={metadata.get('message_id')} "
                                            f"finish={chunk_finish}: "
                                            f"{error_payload['content']} "
                                            f"raw={chunk_error!r}"
                                        )
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
                                        for detail in delta_reasoning_details:
                                            _apply_reasoning_detail_delta(
                                                reasoning_details, detail
                                            )

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
                                                    ] = dedupe_repeated_tool_name(
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
                                                            dedupe_repeated_tool_name(
                                                                merge_streamed_field(
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
                                                            merge_streamed_field(
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
                                        tail_acc.materialize()
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
                                            # materialized; tail_acc.materialize() at
                                            # boundaries/readers folds it back.
                                            _reasoning_added = tail_acc.append_reasoning(
                                                reasoning_block, reasoning_content
                                            )
                                            # v1 reads the tail synchronously below;
                                            # v2.1 keeps it buffered (native emit).
                                            if STREAM_PROTOCOL_VERSION != "v2.1":
                                                tail_acc.materialize()
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
                                            tail_acc.materialize()
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
                                        if (
                                            not content_blocks
                                            or content_blocks[-1]["type"] != "text"
                                        ):
                                            # Open a fresh text block whenever the tail
                                            # is not a text block. Normally the loop
                                            # parks a trailing text("") as the stream
                                            # target, but a seeded continuation can end
                                            # on a non-text block — e.g. a user_steer
                                            # (steering / block-level rewind) whose
                                            # trailing text("") was stripped by the
                                            # empty-round cleanup above, or a tool_calls
                                            # block. Without this guard the answer tokens
                                            # would be appended INTO that block (the
                                            # user_steer's text), corrupting it into a
                                            # fake user turn and losing the answer. The
                                            # non-streaming path guards this identically
                                            # (see _consume_nonstreaming_round).
                                            content_blocks.append(
                                                {
                                                    "type": "text",
                                                    "content": "",
                                                }
                                            )

                                        # O(1) buffered text append (the hot path
                                        # for normal answer streaming). Replaces the
                                        # O(N)-per-token dict-subscript concat.
                                        tail_acc.append_text(content_blocks[-1], value)
                                        # Under v1 / realtime-save, the tail is read
                                        # synchronously below (DB write + serialize),
                                        # so fold it now. Under v2.1 it stays buffered
                                        # (native flush emits from the accumulator;
                                        # materializing per token would restore the
                                        # O(N^2)).
                                        if STREAM_PROTOCOL_VERSION != "v2.1":
                                            tail_acc.materialize()
                                        await checkpoint_stream_state(
                                            char_delta=len(value)
                                        )

                                        if (
                                            ENABLE_REALTIME_CHAT_SAVE
                                            and STREAM_PROTOCOL_VERSION != "v2.1"
                                        ):
                                            # Legacy/non-v2.1 realtime save path.
                                            # v2.1 uses the in-memory stream
                                            # snapshot for reload/resume and
                                            # periodic/final checkpoints instead
                                            # of committing on every token.
                                            update_data = {
                                                "content_blocks": content_blocks,
                                            }
                                            if STREAM_PROTOCOL_VERSION != "v2.1":
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

                                            await Chats.upsert_message_to_chat_by_id_and_message_id(
                                                metadata["chat_id"],
                                                metadata["message_id"],
                                                update_data,
                                                return_model=False,
                                            )

                                        # Regardless of realtime DB writes, the
                                        # stream event must carry content_blocks
                                        # so the v2.1 wrapper can translate this
                                        # chunk into chat:delta ops. The v2.1
                                        # serializer intentionally returns an
                                        # empty content string on the hot path;
                                        # frontends render from content_blocks.
                                        # (Tail already folded above for v1; under
                                        # v2.1 it stays buffered by design.)
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
                    # Terminal flush: force-emit any text the coalescing gate is
                    # still holding so no tail tokens are stranded at stream end.
                    await flush_pending_delta_data(force=True)

                    # Fold the tail accumulator into its block before the
                    # end-of-stream cleanup reads/strips content_blocks.
                    tail_acc.materialize()

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

                    reasoning_content = await _visible_nonstreaming_reasoning(message)
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
                        tail_acc.materialize()
                        content_blocks[-1]["content"] += msg_content
                        append_plain_content(msg_content)

                    res_tool_calls = message.get("tool_calls")
                    length_error = _nonstreaming_round_length_error(res)
                    if length_error:
                        terminal_error = _provider_error_payload(length_error)

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
                    if usage_has_data(usage):
                        # Reshape the broken "C" gateway blob (top-level reasoning
                        # excluded from completion) into canonical shape before it is
                        # emitted, counted, and persisted as meta.usage.
                        usage = normalize_provider_usage(usage)
                        response_usage = usage
                        # op=usage (optimistic) BEFORE process_token_usage (which
                        # pushes the authoritative cumulative totals) so the push
                        # max-corrects the optimistic delta rather than the delta
                        # accumulating on top of the already-correct total. See the
                        # matching note in the streaming chunk loop above.
                        await event_emitter(
                            {
                                "type": "chat:completion",
                                "data": {"usage": usage},
                            }
                        )
                        await process_token_usage(
                            model_id,
                            usage,
                            chat_id=_get_token_usage_chat_id(metadata),
                            user_id=user.id if user else None,
                            source_chat_id=metadata.get("chat_id"),
                            message_id=metadata.get("message_id"),
                            parent_message_id=metadata.get("parent_message_id"),
                            source_type=(
                                "subagent" if metadata.get("subagent_inner") else "chat"
                            ),
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
                    """Run one model round (streaming or non-streaming). If the
                    request FAILS to make progress — no tool calls AND no assistant
                    text, whether it came back empty, returned only reasoning,
                    errored mid-stream, or returned an error/unknown shape —
                    re-issue the SAME request up to AGENTIC_EMPTY_ROUND_MAX_RETRIES
                    times. Such a round is almost always a transient upstream
                    failure; retrying recovers it instead of ending the turn with no
                    answer (the "it researched, then said nothing" bug).

                    A PRODUCTIVE round (any tool call OR any answer text) returns
                    immediately and is NEVER retried — a late hiccup after real
                    output is kept, not thrown away.

                    On exhaustion the turn finalizes as a visible ERROR (the last
                    provider error, or a generic "no response" when the request just
                    kept coming back empty) — a persistent failure is surfaced, never
                    a silent blank. Returns the LAST response object for the caller's
                    dispatch ladder; productive rounds are already folded in here and
                    re-folding is suppressed via the `_round_already_consumed` flag.

                    NOT retried: a genuine user cancel (CancelledError propagates
                    out). Subagents inherit this via the shared loop."""
                    nonlocal _round_already_consumed, terminal_error
                    nonlocal round_retries_exhausted
                    round_retries_exhausted = False
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
                        # else: error / unknown shape — NOT consumed. A failed
                        # request; falls through to the retry logic below.

                        tool_calls_grew = len(tool_calls) > tc_before
                        produced = (
                            tool_calls_grew
                            or _total_text_block_len(content_blocks) > text_before
                        )
                        if produced:
                            # Real progress (a tool call or answer text) — keep it.
                            # A trailing provider error on a round that produced real
                            # output is NON-terminal: for a NEW tool call the loop goes
                            # on to execute it and a later round answers; for ANSWER
                            # TEXT the answer is already complete and the error is
                            # post-output noise (e.g. OpenRouter post-stream
                            # credit/upstream errors that arrive after finish_reason=
                            # stop). Either way a transient mid/post-stream error must
                            # not poison the turn and discard the output. Clear an error
                            # THIS round set whenever the round produced output.
                            if (
                                produced
                                and terminal_error is not None
                                and terminal_error is not terminal_before
                            ):
                                terminal_error = terminal_before
                            return current

                        # DETERMINISTIC non-retryable failure (context window
                        # exceeded / over-long input / empty max-output truncation):
                        # re-issuing the SAME request can't recover it, so surface it
                        # NOW instead of burning AGENTIC_EMPTY_ROUND_MAX_RETRIES
                        # identical doomed calls (each retry only re-sends the same
                        # too-large payload). This is the fix for "a research subagent
                        # ran 20-30 min, then errored 'input exceeds the context
                        # window'": without it, an overflow that's already terminal got
                        # retried 5x at the round level AND re-run wholesale by the
                        # launch/continue/rerun outer loop. The error itself is surfaced
                        # by the consumed-round terminal_error (length truncation) or by
                        # the caller's unconsumed-body read (context-window 4xx); we only
                        # decline to retry. Transient 5xx/429/timeout/connection failures
                        # match no needle and still retry below.
                        if _round_already_consumed:
                            _round_err = (
                                terminal_error
                                if terminal_error is not terminal_before
                                else None
                            )
                        else:
                            _round_err = _safe_error_response_text(current)
                        if _round_err is not None and _is_nonretryable_provider_error(
                            _round_err
                        ):
                            log.warning(
                                "non-retryable provider error (context window / "
                                "over-long input / max-output truncation) — surfacing "
                                "without retry "
                                f"chat={metadata.get('chat_id')} "
                                f"subagent_inner={metadata.get('subagent_inner', False)}"
                            )
                            return current

                        if attempt >= AGENTIC_EMPTY_ROUND_MAX_RETRIES:
                            # Out of retries, still no progress. Never finalize a
                            # silent blank: if the round was consumed but empty and
                            # set no error, synthesize one so the turn surfaces a
                            # failure. A consumed provider error is kept as-is; an
                            # unconsumed error/unknown shape is left for the caller's
                            # existing error-reading branch.
                            round_retries_exhausted = True
                            if _round_already_consumed:
                                if terminal_error is None:
                                    terminal_error = _provider_error_payload(
                                        (
                                            "The model returned no response after "
                                            f"retrying {AGENTIC_EMPTY_ROUND_MAX_RETRIES} "
                                            "times. Please try again."
                                        ),
                                        retries_exhausted=True,
                                        empty_response=True,
                                    )
                                else:
                                    terminal_error = _provider_error_payload(
                                        terminal_error,
                                        retries_exhausted=True,
                                    )
                            return current

                        # Unproductive/failed round with retries left: roll back this
                        # round's residue (empty/partial blocks, per-round reasoning
                        # bookkeeping — REASONING_DETAILS.md §6 Bug B) AND clear any
                        # error it set, so the retry starts clean. The v2.1 emitter
                        # emits a `replace` to resync the client mirror on shrink.
                        tail_acc.materialize()
                        tail_acc.acc = None
                        tail_acc.block = None
                        if blocks_before < len(content_blocks):
                            del content_blocks[blocks_before:]
                        if rrd_before < len(round_reasoning_details):
                            del round_reasoning_details[rrd_before:]
                        if terminal_error is not terminal_before:
                            terminal_error = terminal_before
                        attempt += 1
                        log.warning(
                            "unproductive/failed model round (no tool calls, no "
                            f"answer text) — retry {attempt}/"
                            f"{AGENTIC_EMPTY_ROUND_MAX_RETRIES} "
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
                            terminal_error = _provider_error_payload(
                                error_msg,
                                retries_exhausted=round_retries_exhausted,
                            )
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
                # Steer ids already injected as user_steer blocks this turn — so a
                # silently-failed durable delete can't make the next round re-peek
                # and re-inject the same steer a second time.
                delivered_steer_ids: set = set()

                async def _disconnect_tool_clients(clients: Any) -> None:
                    if not isinstance(clients, dict):
                        return
                    for server_id, client in clients.items():
                        try:
                            await client.disconnect()
                        except Exception:
                            log.exception(
                                "Live tool refresh cleanup failed for MCP server %r",
                                server_id,
                            )

                async def _apply_pending_tool_selection() -> dict | None:
                    """Resolve and commit the latest selection before a provider round.

                    Resolution happens off to the side. The active metadata/schema
                    is swapped only after every selected tool loaded successfully,
                    so a failed addition cannot destroy the still-working current
                    tool set. If another toggle lands while MCP discovery is
                    awaiting I/O, discard that intermediate build and resolve the
                    newer full snapshot before returning.
                    """

                    redis = getattr(request.app.state, "redis", None)
                    pending = await pop_pending_tool_selection(redis, task_id)
                    if not pending:
                        return None

                    while pending:
                        desired = normalize_live_tool_selection(pending)
                        refresh_input_metadata = {
                            key: value
                            for key, value in metadata.items()
                            if key not in {"tools", "mcp_clients"}
                        }
                        refresh_input_metadata.update(
                            {
                                "_tool_selection_refresh": True,
                                "tool_ids": desired["tool_ids"],
                                "tool_servers": copy.deepcopy(
                                    desired["tool_servers"]
                                ),
                                "features": desired["features"],
                                "live_tool_selection": desired,
                                "params": {
                                    **(
                                        metadata.get("params")
                                        if isinstance(metadata.get("params"), dict)
                                        else {}
                                    ),
                                    **desired["params"],
                                    "function_calling": "native",
                                },
                            }
                        )
                        refresh_form_data = {
                            "model": model_id,
                            "stream": True,
                            "messages": copy.deepcopy(
                                metadata.get("_tool_selection_base_messages")
                                or form_data.get("messages", [])
                            ),
                            "params": desired["params"],
                            "tool_ids": desired["tool_ids"],
                            "features": desired["features"],
                        }
                        refresh_model = (
                            request.app.state.MODELS.get(model_id)
                            if hasattr(request.app.state, "MODELS")
                            else None
                        ) or model

                        try:
                            (
                                refreshed_form_data,
                                refreshed_metadata,
                                _,
                            ) = await process_chat_payload(
                                request,
                                refresh_form_data,
                                user,
                                refresh_input_metadata,
                                refresh_model,
                            )
                        except Exception as exc:
                            # A newer click may have replaced the failed selection
                            # while this one was loading. Prefer it before surfacing
                            # an error for an already-obsolete snapshot.
                            newer = await pop_pending_tool_selection(redis, task_id)
                            if newer:
                                await _disconnect_tool_clients(
                                    refresh_input_metadata.get("mcp_clients")
                                )
                                pending = newer
                                continue
                            log.exception("Live tool selection refresh failed")
                            await event_emitter(
                                {
                                    "type": "tool-selection:error",
                                    "data": {
                                        "operation_id": desired.get("operation_id"),
                                        "message": str(exc),
                                        "task_id": task_id,
                                    },
                                }
                            )
                            return {"applied": False}

                        newer = await pop_pending_tool_selection(redis, task_id)
                        if newer:
                            await _disconnect_tool_clients(
                                refreshed_metadata.get("mcp_clients")
                            )
                            pending = newer
                            continue

                        desired = normalize_live_tool_selection(
                            refreshed_metadata.get("live_tool_selection") or desired
                        )
                        old_clients = metadata.get("mcp_clients") or {}
                        for key in ("tools", "mcp_clients", "tool_ids", "tool_servers"):
                            metadata.pop(key, None)
                        for key in (
                            "tools",
                            "mcp_clients",
                            "tool_ids",
                            "tool_servers",
                            "params",
                        ):
                            if key in refreshed_metadata:
                                metadata[key] = refreshed_metadata[key]
                        metadata["live_tool_selection"] = desired

                        form_data["messages"] = refreshed_form_data.get(
                            "messages", form_data.get("messages", [])
                        )
                        if refreshed_form_data.get("tools"):
                            form_data["tools"] = refreshed_form_data["tools"]
                        else:
                            form_data.pop("tools", None)
                        if "tool_choice" in refreshed_form_data:
                            form_data["tool_choice"] = refreshed_form_data["tool_choice"]
                        else:
                            form_data.pop("tool_choice", None)

                        # Current-round calls have all completed before this
                        # boundary, so their old clients are now safe to close.
                        await _disconnect_tool_clients(old_clients)

                        return {"applied": True, "selection": desired}

                def _round_base_messages() -> list:
                    """Current `form_data["messages"]` through the pure
                    `round_base_messages` (see its docstring: continuation bases
                    end with this message's own partial turn, which every
                    between-rounds assembly re-carries itself). Shared by the
                    round loop and the mid-turn compaction planner so both see
                    the same base."""
                    return round_base_messages(
                        form_data["messages"], metadata.get("message_id")
                    )

                async def _maybe_compact_between_rounds(force: bool = False) -> None:
                    """Compaction gate for the agentic loop (COMPACTION.md §5).

                    Trigger: the LAST round's ``usage.total_tokens`` reaches
                    ``COMPACTION_THRESHOLD`` × the model's declared context
                    window. ``response_usage`` is the right source — it is
                    plain-ASSIGNED from each usage chunk (never summed across the
                    tool loop, which is upstream's #27031 bug), so after round N
                    it holds round N's payload and nothing else.

                    Unknown window ⇒ never auto-compact: ``resolve_context_length``
                    returns None rather than 0 precisely so this stays decidable.

                    On a hit, the summarizer (the chat's own model, no
                    ``max_tokens``) writes the narrative, and the anchor is
                    spliced into ``content_blocks`` immediately after the last
                    completed ``tool_calls`` block — the one boundary that can
                    never dangle a ``tool_use``. The next request's conversion in
                    ``blocks_to_api_messages`` sees the anchor and drops
                    everything before it.

                    Best-effort throughout: a failure leaves the turn running
                    uncompacted, where the provider's own context-length error is
                    what surfaces. It must never be the thing that kills a turn
                    that is otherwise working.
                    """
                    context_length = resolve_context_length(model)
                    total_tokens = usage_total_tokens(response_usage)
                    if not force:
                        # `force` is a `/compact` the user steered into this turn.
                        # It overrides POLICY only — the feature flag, the
                        # threshold, and an unresolvable window (that number is
                        # display-only on the block, so not knowing it is no
                        # reason to refuse an explicit request).
                        if not ENABLE_CONVERSATION_COMPACTION:
                            return
                        if context_length is None:
                            return
                        if not should_compact(
                            total_tokens, context_length, COMPACTION_THRESHOLD
                        ):
                            return
                    # Anti-thrash: the trigger reads a number measured BEFORE the
                    # previous cut took effect, so without this the round right
                    # after a compaction would compact again. Kept under `force`
                    # too — it is arithmetic, not policy: with nothing after the
                    # last anchor there is nothing to summarize.
                    if not has_uncompacted_span(
                        [{"role": "assistant", "content_blocks": content_blocks}]
                    ):
                        if force:
                            await event_emitter(
                                {
                                    "type": "status",
                                    "data": {
                                        "action": "compaction",
                                        "description": (
                                            "Nothing to compact — the context was "
                                            "already compacted"
                                        ),
                                        "done": True,
                                    },
                                }
                            )
                        return

                    # NO progress/completion status for a successful compaction —
                    # see the matching note in `maybe_compact_at_turn_start`.
                    # `status` events land in the message's persisted
                    # `statusHistory`, so the pair "Compacting conversation
                    # context" / "Compacted N messages" became a permanent second
                    # copy of what the divider block already says, parked next to
                    # it forever. The anchor block is the record. A FAILURE still
                    # emits (below): that is the one outcome no block records.
                    try:
                        api_messages = await asyncio.to_thread(
                            blocks_to_api_messages,
                            [
                                *_round_base_messages(),
                                {
                                    "role": "assistant",
                                    "content_blocks": content_blocks,
                                    "tool_result_bodies": _current_tool_result_bodies(),
                                },
                            ],
                            model_id,
                        )
                        new_blocks, block = await compact_content_blocks(
                            request,
                            user,
                            model_id=model_id,
                            api_messages=api_messages,
                            content_blocks=content_blocks,
                            total_tokens=total_tokens,
                            context_length=context_length,
                        )
                    except Exception:
                        log.exception(
                            "mid-turn compaction failed for chat %s",
                            metadata.get("chat_id"),
                        )
                        await event_emitter(
                            {
                                "type": "status",
                                "data": {
                                    "action": "compaction",
                                    "description": (
                                        "Context compaction failed — continuing "
                                        "with full history"
                                    ),
                                    "done": True,
                                },
                            }
                        )
                        return

                    # Mutate IN PLACE: `content_blocks` is aliased by the stream
                    # handler, the checkpointer, and `in_flight_assistant`, all of
                    # which must see the anchor. Rebinding the name would leave
                    # every one of them on the pre-cut list.
                    content_blocks[:] = new_blocks

                    # Record the envelope THIS cut will send. The router-side
                    # capture deliberately skips an anchor on the in-flight
                    # message (a whole-list write from there would race this
                    # stream's own checkpoint), so the mid-turn path stores its
                    # own — same assembly the next round runs, written into the
                    # live block so it lands through `checkpoint_stream_state`
                    # below rather than through a second, competing writer.
                    try:
                        post_cut = await asyncio.to_thread(
                            blocks_to_api_messages,
                            [
                                *_round_base_messages(),
                                {
                                    "role": "assistant",
                                    "content_blocks": content_blocks,
                                    "tool_result_bodies": _current_tool_result_bodies(),
                                },
                            ],
                            model_id,
                        )
                        capture = capture_compaction_envelope(post_cut)
                        if capture is not None:
                            anchor_index = capture["block_index"]
                            if 0 <= anchor_index < len(content_blocks):
                                anchor = content_blocks[anchor_index]
                                if is_compaction_block(anchor):
                                    anchor["envelope"] = capture["envelope"]
                    except Exception:
                        # Display-only. Never worth failing a compaction that
                        # otherwise succeeded.
                        log.exception(
                            "capturing sent compaction envelope failed for chat %s",
                            metadata.get("chat_id"),
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
                    # Durable before the next provider call: a crash between here
                    # and the response would otherwise lose the narrative (which
                    # is generate-once by contract) while keeping the cost.
                    await checkpoint_stream_state(force=True)
                    log.info(
                        "compaction (mid-turn): chat=%s message=%s covers=%s "
                        "tokens=%s/%s",
                        metadata.get("chat_id"),
                        metadata.get("message_id"),
                        block.get("covers"),
                        total_tokens,
                        context_length,
                    )

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

                    # Synthesize a stable id for any tool call the provider streamed
                    # WITHOUT one (some OpenAI-compatible proxies omit 'id' on tool-call
                    # deltas). If left empty, current_tool_call_id_var becomes '' and the
                    # tool result is stored with tool_call_id='' — after which the
                    # subagent's real answer is permanently unreachable to the parent
                    # model: every later round and every reload re-emit
                    # '[No output was produced for this tool call.]' because the result
                    # row, the by_tool_call recovery map, AND reconcile_block_results all
                    # key on a non-empty id. Assigning the id HERE (before the call goes
                    # into content_blocks and before dispatch) keeps it consistent across
                    # the persisted assistant message, the result row, the pinned
                    # ContextVar, and reconcile. Mirrors response.py's call_{uuid4}.
                    if isinstance(response_tool_calls, list):
                        _seen_tc_ids: set = set()
                        for _tc in response_tool_calls:
                            if not isinstance(_tc, dict):
                                continue
                            _id = _tc.get("id")
                            # C24: synthesize for a missing id AND reassign a fresh id
                            # to any DUPLICATE non-empty id (a misbehaving provider can
                            # send two distinct calls with the same id). A collision in
                            # the subagent side-channel (subagent_id_by_tool_call[id])
                            # otherwise overwrites the first, stranding one card.
                            if not _id or _id in _seen_tc_ids:
                                _id = f"call_{str(uuid4())}"
                                _tc["id"] = _id
                            _seen_tc_ids.add(_id)

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

                    execute_tool_call = partial(
                        _execute_one_tool_call,
                        request=request,
                        metadata=metadata,
                        model=model,
                        user=user,
                        tools=tools,
                        event_emitter=event_emitter,
                        event_caller=event_caller,
                        response_handler_task=response_handler_task,
                    )


                    name_by_id = {
                        tc.get("id"): tc.get("function", {}).get("name", "")
                        for tc in response_tool_calls
                        if isinstance(tc, dict)
                    }
                    msg_id = metadata.get("message_id")
                    allow_lazy_tool_results = (
                        STREAM_PROTOCOL_VERSION == "v2.1"
                        and not str(metadata.get("chat_id", "")).startswith("local:")
                    )
                    v21_mirror = getattr(event_emitter, "_v21_mirror", None)
                    emit_raw = getattr(event_emitter, "_emit_raw_primary", None)
                    can_stream_tool_results = bool(
                        STREAM_PROTOCOL_VERSION == "v2.1"
                        and msg_id
                        and v21_mirror is not None
                        and emit_raw is not None
                    )

                    tc_block = content_blocks[-1]
                    results = [None] * len(response_tool_calls)
                    tc_block["results"] = []

                    async def land_tool_result(index, result):
                        """Land ONE finished tool call.

                        Tool calls in a round finish at wildly different times (a
                        cached web_fetch in 200ms next to a 40s subagent), so a
                        result is slimmed, stored and broadcast the moment ITS call
                        returns rather than when the round does. The block's
                        `results` stay in tool-call order and only ever contain
                        calls that have actually finished, which is also what makes
                        a mid-round checkpoint (or an interrupted turn) persist the
                        work that did complete.
                        """
                        if not result:
                            results[index] = result
                            return result

                        tc_id = result.get("tool_call_id")
                        slim_result = result
                        if allow_lazy_tool_results:
                            # Keep canonical content_blocks slim: full web bodies
                            # live in tool_result_bodies and are hydrated only for
                            # model replay / explicit UI expansion.
                            slim_result, body_result = _slim_tool_result(
                                result, name_by_id.get(tc_id, ""), store_body=True
                            )
                            if body_result is not None and msg_id and tc_id:
                                set_tool_result_body(msg_id, tc_id, body_result)
                                # Newly produced this round: keep a wipe-immune copy
                                # and queue it for the per-round durable write-
                                # through below (it isn't in the DB row yet).
                                generation_tool_result_bodies[str(tc_id)] = body_result
                                pending_db_body_merges[str(tc_id)] = body_result

                        results[index] = slim_result
                        tc_block["results"] = [r for r in results if r]

                        if can_stream_tool_results and tc_id:
                            sent = v21_mirror.setdefault("tool_results_sent", set())
                            if tc_id not in sent:
                                set_tool_result(msg_id, tc_id, slim_result)
                                sent.add(tc_id)
                                await emit_raw(
                                    {
                                        "type": "tool_call:result",
                                        "data": _tool_result_event_data(
                                            msg_id, slim_result
                                        ),
                                    }
                                )
                        elif len(response_tool_calls) > 1:
                            # No raw v2.1 channel here — a legacy-protocol stream, or
                            # a subagent's inner turn, whose emitters both learn about
                            # results by diffing the blocks of a chat:completion. Ship
                            # one so those surfaces land calls individually too. Not
                            # worth it for a single call: its landing IS the round end,
                            # which emits this anyway.
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
                        return slim_result

                    async def run_tool_call(index):
                        return await land_tool_result(
                            index, await execute_tool_call(response_tool_calls[index])
                        )

                    # Group consecutive parallelizable tool calls so they run concurrently
                    # via asyncio.gather. A non-parallelizable call acts as a barrier:
                    # everything before it must finish before it runs, and everything after
                    # it waits until it completes. This keeps state-mutating tools strictly
                    # ordered while letting read-only tools (web_search, web_fetch, ...) run
                    # in parallel. Result order matches the tool_calls input order.

                    i = 0
                    while i < len(response_tool_calls):
                        if _tool_call_is_parallelizable(response_tool_calls[i], tools):
                            j = i
                            while j < len(
                                response_tool_calls
                            ) and _tool_call_is_parallelizable(
                                response_tool_calls[j], tools
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
                                *[run_tool_call(k) for k in range(i, j)],
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
                                            (
                                                failed_call.get("function", {}).get(
                                                    "name", ""
                                                )
                                                if isinstance(failed_call, dict)
                                                else failed_call
                                            ),
                                        )
                                    else:
                                        log.error(
                                            "parallel tool call raised: %r",
                                            result,
                                            exc_info=result,
                                        )
                                    # The failure only becomes a result here, so
                                    # this is the moment it lands — same path as a
                                    # call that returned normally.
                                    await land_tool_result(
                                        i + offset,
                                        _tool_result_for_failed_call(
                                            failed_call, result, request=request
                                        ),
                                    )
                            i = j
                        else:
                            await run_tool_call(i)
                            i += 1

                    # Every result was slimmed, stored and broadcast as its call
                    # landed; the round only has to close the block over them.
                    tc_block["results"] = [r for r in results if r]
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

                    # Layer 2 — per-round DB write-through (the durability layer).
                    # Before this existed, a large tool body was only persisted at
                    # the coarse checkpoint / final save, so a server restart or a
                    # socket-store wipe mid-turn lost every body accumulated so far
                    # and the NEXT round raised "Missing tool result body for ref
                    # ...". Merge this round's newly produced bodies straight into
                    # the durable row now (targeted jsonb union — never reserializes
                    # the rest of meta). On failure the batch stays in
                    # pending_db_body_merges and is retried next round; the terminal
                    # persist (union semantics) is the last-resort carrier.
                    if (
                        allow_lazy_tool_results
                        and metadata.get("chat_id")
                        and msg_id
                        and not str(metadata.get("chat_id", "")).startswith("local:")
                        and pending_db_body_merges
                    ):
                        try:
                            # merge_message_tool_result_bodies swallows its own
                            # exceptions and returns False on failure — only a
                            # True return may drain the retry buffer, otherwise a
                            # transient DB failure would silently drop the batch.
                            if await Chats.merge_message_tool_result_bodies(
                                metadata["chat_id"], msg_id, pending_db_body_merges
                            ):
                                pending_db_body_merges.clear()
                            else:
                                log.error(
                                    "per-round tool_result_bodies write-through "
                                    "failed (will retry next round)"
                                )
                        except Exception:
                            log.exception(
                                "per-round tool_result_bodies write-through failed "
                                "(will retry next round)"
                            )

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
                        consumed_steer_ids: list[str] = []
                        # Armed by a `/compact` steer just below; read by the
                        # compaction gate immediately after. Per-round: the
                        # command applies to the boundary the user asked at.
                        compact_requested = False
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

                                # PEEK (non-destructive), then delete only the
                                # steers we actually inject — and only AFTER they're
                                # checkpointed into content_blocks (below). Skip ids
                                # already delivered this turn (a prior round whose
                                # durable delete silently failed) so we never inject
                                # the same steer twice. Steers with no id can't be
                                # tracked/deleted safely, so they're left for the
                                # post-completion drain (delivered as a follow-up).
                                steer_items = [
                                    s
                                    for s in await Chats.peek_steer_items_by_id(
                                        metadata["chat_id"]
                                    )
                                    if isinstance(s, dict)
                                    and s.get("id")
                                    and s.get("id") not in delivered_steer_ids
                                ]
                                consumed_steer_ids = [s.get("id") for s in steer_items]
                                steer_blocks = []
                                for steer_item in steer_items:
                                    steer_text = (
                                        _item_spec(steer_item).get("content")
                                        or steer_item.get("prompt")
                                        or ""
                                    ).strip()
                                    if is_compact_command(steer_text):
                                        # `/compact` is a command, not something
                                        # to say to the model: it must NOT become
                                        # a user_steer block (which would put the
                                        # literal text in the transcript and in
                                        # the next request). It is still consumed
                                        # like any other steer — same dedupe, same
                                        # deferred delete — and arms the gate a
                                        # few lines below, which is already the
                                        # safe cut point for this round.
                                        compact_requested = True
                                        continue
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

                                # The steer is now durably part of THIS turn's
                                # content_blocks (it will be persisted whether
                                # the round succeeds, errors, or is cancelled).
                                # So remove it from the queue NOW — deferring to
                                # after the model call would leave it queued if a
                                # Stop/error landed mid-call, and clear_draining
                                # would then downgrade it to after_final → the
                                # same steer both in the message AND re-queued as
                                # a duplicate follow-up. Mark delivered first so a
                                # silent delete failure can't re-inject it.
                                #
                                # Keyed on CONSUMED, not on `steer_blocks`: a
                                # `/compact` steer produces no block, and leaving
                                # it queued would re-arm the gate every round
                                # (compacting forever) and finally drain as a
                                # follow-up turn whose prompt is the literal
                                # "/compact".
                                if consumed_steer_ids:
                                    delivered_steer_ids.update(consumed_steer_ids)
                                    try:
                                        await Chats.remove_steer_items_by_ids(
                                            metadata["chat_id"], consumed_steer_ids
                                        )
                                        await broadcast_queue_state(
                                            metadata.get("user_id"),
                                            metadata["chat_id"],
                                        )
                                    except Exception:
                                        log.exception(
                                            "steer consume failed for chat %s",
                                            metadata.get("chat_id"),
                                        )
                                    consumed_steer_ids = []
                            except Exception:
                                log.exception(
                                    "steer injection failed for chat %s",
                                    metadata.get("chat_id"),
                                )

                        # Conversation compaction, mid-turn half of the gate.
                        # COMPACTION.md §5: one gate evaluated before EVERY model
                        # request, not just at turn boundaries — this is
                        # OpenHands' shape, and it is also the only design that
                        # helps a single long research turn, which message-
                        # boundary compaction cannot touch. Runs here, at the
                        # last quiet point before the next provider call, after
                        # the steer/tool-selection drains so the anchor lands
                        # over the FINAL block layout of this round.
                        await _maybe_compact_between_rounds(force=compact_requested)

                        in_flight_assistant: dict = {
                            "role": "assistant",
                            "content_blocks": content_blocks,
                        }
                        # Backfill any subagent result that finished but whose answer
                        # didn't land in content_blocks (interrupted/partial save), so
                        # the next model round sees every subagent's real output rather
                        # than the "[No output...]" placeholder.
                        await _reconcile_subagent_results()
                        # Mirror the blessed PERSISTED path (utils/chat.py): carry the
                        # durable subagent_runs answer mirror onto the between-rounds
                        # assistant so blocks_to_api_messages/_expand_assistant can
                        # recover a slimmed/empty subagent result from
                        # subagent_runs.final_text. Without this the live between-rounds
                        # round can feed the model "[No output...]" for a subagent whose
                        # tool_result_bodies write was lost.
                        if (
                            metadata.get("chat_id")
                            and metadata.get("message_id")
                            and not str(metadata.get("chat_id", "")).startswith(
                                "local:"
                            )
                        ):
                            try:
                                _msg = (
                                    await Chats.get_message_by_id_and_message_id(
                                        metadata["chat_id"], metadata["message_id"]
                                    )
                                    or {}
                                )
                                _runs = _msg.get("subagent_runs")
                                if isinstance(_runs, dict) and _runs:
                                    in_flight_assistant["subagent_runs"] = _runs
                            except Exception:
                                log.exception(
                                    "subagent_runs forward to in_flight_assistant failed"
                                )
                        tool_result_bodies = _current_tool_result_bodies()
                        if tool_result_bodies:
                            in_flight_assistant["tool_result_bodies"] = (
                                tool_result_bodies
                            )
                        if round_reasoning_details:
                            in_flight_assistant["reasoning_details_per_round"] = list(
                                round_reasoning_details
                            )

                        # Live tool selection is the final asynchronous boundary
                        # before constructing and issuing the next provider
                        # request. Drain until quiet: an update can be acknowledged
                        # while MCP discovery, steering, reconciliation, or the UI
                        # event for an earlier update is awaiting I/O. Once the
                        # final empty check returns, no await remains before the
                        # provider call below.
                        selection_before_boundary = normalize_live_tool_selection(
                            metadata.get("live_tool_selection") or {}
                        )
                        applied_selection = None
                        applied_since_publish = False
                        tool_selection_change_index = None
                        while True:
                            tool_selection_outcome = (
                                await _apply_pending_tool_selection()
                            )
                            if tool_selection_outcome is not None:
                                if not tool_selection_outcome.get("applied"):
                                    continue
                                applied_selection = tool_selection_outcome.get(
                                    "selection"
                                )
                                applied_since_publish = True
                                continue
                            if not applied_since_publish:
                                break

                            tool_selection_change = (
                                build_tool_selection_change_block(
                                    selection_before_boundary, applied_selection
                                )
                                if applied_selection is not None
                                else None
                            )
                            await event_emitter(
                                {
                                    "type": "tool-selection:applied",
                                    "data": {
                                        "operation_id": applied_selection.get(
                                            "operation_id"
                                        ),
                                        "task_id": task_id,
                                        "added": (tool_selection_change or {}).get(
                                            "added", []
                                        ),
                                        "removed": (tool_selection_change or {}).get(
                                            "removed", []
                                        ),
                                    },
                                }
                            )

                            content_blocks_changed = False
                            if tool_selection_change_index is not None:
                                if tool_selection_change:
                                    content_blocks[tool_selection_change_index] = (
                                        tool_selection_change
                                    )
                                else:
                                    content_blocks.pop(tool_selection_change_index)
                                    tool_selection_change_index = None
                                content_blocks_changed = True
                            elif tool_selection_change:
                                trailing = None
                                if (
                                    content_blocks
                                    and content_blocks[-1].get("type") == "text"
                                    and not (
                                        content_blocks[-1].get("content") or ""
                                    ).strip()
                                ):
                                    trailing = content_blocks.pop()
                                tool_selection_change_index = len(content_blocks)
                                content_blocks.append(tool_selection_change)
                                content_blocks.append(
                                    trailing
                                    if trailing is not None
                                    else {"type": "text", "content": ""}
                                )
                                content_blocks_changed = True

                            if content_blocks_changed:
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

                            applied_since_publish = False

                        new_form_data = {
                            **form_data,
                            "model": model_id,
                            "stream": True,
                            "messages": [
                                *_round_base_messages(),
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

                        _round_response = await generate_chat_completion(
                            request,
                            new_form_data,
                            user,
                        )

                        res = await _run_round_with_retry(
                            _round_response,
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

                                    # Surface this as a terminal error so the turn
                                    # finalizes VISIBLY (the terminal-error path
                                    # persists error + sweeps subagents) rather than
                                    # falling through to the clean finalizer with
                                    # done:true despite the error we just emitted.
                                    terminal_error = _provider_error_payload(
                                        error_msg,
                                        retries_exhausted=round_retries_exhausted,
                                    )
                                    await event_emitter(
                                        {
                                            "type": "chat:message:error",
                                            "data": {"error": terminal_error},
                                        }
                                    )
                            except Exception as read_err:
                                log.error(f"Could not read error response: {read_err}")
                            if terminal_error is None:
                                # No readable body — still finalize visibly.
                                terminal_error = _provider_error_payload(
                                    None,
                                    retries_exhausted=round_retries_exhausted,
                                )
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
                    # Every terminal_error assignment site normalizes to the
                    # canonical {"content": str} shape at entry; the extractor
                    # here tolerates a raw string or an empty content so a
                    # malformed payload still surfaces the fallback below
                    # instead of a blank "bricked" errored message.
                    error_content = _provider_error_text(terminal_error)
                    if not error_content:
                        error_content = (
                            "The model request failed and could not be recovered."
                        )
                    # Defensive: a stringified DB/driver exception can embed the
                    # offending content; strip any raw NUL so re-persisting the
                    # error (and the RAM snapshot / socket event below, which
                    # bypass the DB sanitizer) can never re-trigger the failure.
                    if isinstance(error_content, str) and "\x00" in error_content:
                        error_content = error_content.replace("\x00", "")
                    # B2: a terminal tool-loop error also finalizes the parent —
                    # flip stragglers terminal ('error', or 'done' if finished) and
                    # mirror finished answers so no subagent is left "Researching…".
                    await _sweep_subagent_runs("error")
                    await _reconcile_subagent_results()
                    # Same dangling-clock fix as the cancel teardown: an error
                    # that lands while the model is still thinking leaves the
                    # reasoning block open, and `duration == null` is the ONLY
                    # thing the UI reads to choose between "Thought for N seconds"
                    # and a spinning "Thinking…". Close it AND tell the tab — the
                    # persist below is durable but silent, so without the emit the
                    # spinner runs under the error box until a reload.
                    if _finalize_open_agentic_blocks(content_blocks):
                        try:
                            await event_emitter(
                                {
                                    "type": "chat:completion",
                                    "data": {"content_blocks": content_blocks},
                                }
                            )
                        except Exception:
                            log.exception("terminal-error open-block emit failed")
                    update_data = _build_checkpoint_update(include_legacy_content=True)
                    # Keep the structured retry-exhaustion/context code. The old
                    # content-only write erased the distinction the subagent
                    # lifecycle needs for a safe model handoff.
                    error_payload = _provider_error_payload(terminal_error)
                    if "\x00" in error_payload["content"]:
                        error_payload["content"] = error_payload["content"].replace(
                            "\x00", ""
                        )
                    update_data["error"] = error_payload
                    # An errored turn is TERMINAL, so say so durably. Without this
                    # the row lands in the one state that is not a state — carrying
                    # an error AND `done: false` — which reads to every reconcile
                    # path (chat open, reconnect, the queue sweeper, exports, the
                    # subagent handoff) as "still generating". The client papered
                    # over it per-load with `inactiveAssistantTerminalPatch`, so the
                    # same message healed itself on every open and any path that
                    # read the raw row disagreed with the one that healed it. This
                    # is the same terminal shape the success finaliser writes and
                    # the same one the client's local patch synthesizes:
                    # `{done: true, error: {...}}`.
                    update_data["done"] = True
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        update_data,
                        return_model=False,
                    )
                    if STREAM_PROTOCOL_VERSION == "v2.1" and metadata.get("message_id"):
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
                                "error": error_payload,
                                "snapshot_version": stream_version_get(
                                    metadata["message_id"]
                                ),
                            },
                        )
                        stream_version_flush(metadata["message_id"])
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

                title = await Chats.get_chat_title_by_id(metadata["chat_id"])
                # Canonical end-of-stream persist: ensure the tail accumulator is
                # folded into content_blocks before serialize/split.
                tail_acc.materialize()
                # Make the durable record authoritative: backfill any subagent
                # result that finished but never made it into content_blocks, so a
                # reload / fresh client / the next turn all see complete results.
                # B2: sweep first so a straggler 'running' run (terminal write lost
                # under teardown) is flipped terminal, THEN reconcile mirrors any
                # newly-'done' run's answer into content_blocks. Invariant: a
                # finalized parent never leaves a subagent stuck "Researching…".
                await _sweep_subagent_runs("cancelled")
                await _reconcile_subagent_results()
                if STREAM_PROTOCOL_VERSION == "v2.1" and not str(
                    metadata.get("chat_id", "")
                ).startswith("local:"):
                    final_slim_blocks, final_split_bodies = split_tool_result_bodies(
                        content_blocks
                    )
                    for _tcid, _body in final_split_bodies.items():
                        set_tool_result_body(metadata.get("message_id"), _tcid, _body)
                        # Keep a wipe-immune copy so the terminal persist below
                        # (which reads _current_tool_result_bodies) can never miss a
                        # body just because the socket store dropped it. No pending-
                        # merge queue entry: the final upsert right below carries
                        # these durably (with union + prune).
                        generation_tool_result_bodies[str(_tcid)] = _body
                else:
                    final_slim_blocks = content_blocks
                # Belt to the totality contract's braces. This projection is
                # DISPLAY-ONLY (it feeds the terminal socket emit and the
                # container-output importer; the durable `content` column comes
                # from `text_only_content_from_blocks` in the checkpoint builder).
                # It sits upstream of the persist that writes `done: True`, so a
                # raise here used to discard an answer that had already fully
                # streamed. Never again: degrade to the plain-text projection and
                # finish the turn.
                try:
                    final_content = serialize_content_blocks(
                        final_slim_blocks, force=True
                    )
                except Exception:
                    log.exception(
                        "legacy content projection failed for chat %s message %s — "
                        "falling back to the plain-text projection",
                        metadata.get("chat_id"),
                        metadata.get("message_id"),
                    )
                    final_content = text_only_content_from_blocks(final_slim_blocks)

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

                if STREAM_PROTOCOL_VERSION == "v2.1" or not ENABLE_REALTIME_CHAT_SAVE:
                    # Save the final canonical message in the database. v2.1 no
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
                            await Chats.get_message_by_id_and_message_id(
                                metadata["chat_id"], metadata["message_id"]
                            )
                            or {}
                        )
                        update_data["files"] = current_message.get(
                            "files", container_output_files
                        )

                    # Terminal persist: opt into GC of the accumulate-only
                    # tool_result_bodies map. Union persistence never shrinks it
                    # mid-turn (that monotonicity is the whole anti-corruption
                    # guarantee), so the ONLY safe place to drop bodies no longer
                    # referenced by the final content_blocks is right here, once the
                    # turn is definitively done.
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        update_data,
                        return_model=False,
                        prune_tool_result_bodies=True,
                    )
                    if STREAM_PROTOCOL_VERSION == "v2.1" and metadata.get("message_id"):
                        clear_tool_result_bodies(metadata["message_id"])
                elif response_usage:
                    # Non-v2.1 realtime-save mode writes content on the hot path;
                    # still persist final usage so opened full subagent chats
                    # and future rebuilds can recover provider/cache details.
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {"usage": response_usage},
                        return_model=False,
                    )

                # Send a webhook notification if the user is not active.
                # Hidden subagent chats are implementation detail rows; sending
                # one webhook per inner worker would be noisy and would force us
                # to keep a legacy full-text buffer just for those hidden runs.
                if not metadata.get(
                    "subagent_inner"
                ) and not get_active_status_by_user_id(user.id):
                    webhook_url = await Users.get_user_webhook_url_by_id(user.id)
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

                if STREAM_PROTOCOL_VERSION == "v2.1" and metadata.get("message_id"):
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
                    stream_version_flush(msg_id)
                    chat_obj = None
                    try:
                        chat_obj = await Chats.get_chat_by_id(metadata["chat_id"])
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

                # Outlet filters run once at the authoritative server-side tail
                # of the stream. Mutations are persisted and emitted so the
                # frontend mirror converges on the stored result.
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

                # The turn is OVER here: the response is persisted, `chat:done`
                # has been emitted, and the outlet filters have committed their
                # last mutations. Release the chat-turn lease now, before the
                # background handler — title, tag and follow-up generation are
                # separate LLM round-trips that take seconds, and holding the
                # lease across them rejected the user's very next message with
                # 409 `chat_generation_in_progress`. From the client's point of
                # view the answer had finished, so the send simply failed for no
                # visible reason. Idempotent: the drain below re-calls this.
                await _release_completed_generation_operation()

                await _run_background_tasks(
                            request=request,
                            form_data=form_data,
                            metadata=metadata,
                            user=user,
                            event_emitter=event_emitter,
                            tasks=tasks,
                        )

                # Autonomous queue drain: this generation finished CLEANLY, so
                # start the next queued follow-up (if any) server-side. Runs only
                # here — the terminal-error `return` above and the CancelledError
                # handler below bypass it, so Stop and genuine errors PAUSE the
                # queue. Best-effort: a drain failure must never break the
                # generation that just succeeded.
                if metadata.get("chat_id") and metadata.get("message_id"):
                    # Belt-and-suspenders: ensure every steer we delivered this
                    # turn is gone from the queue BEFORE draining. The per-round
                    # delete is best-effort (remove_steer_items_by_ids swallows a DB
                    # error and returns None), so a failed delete could leave a
                    # delivered steer queued; since the drain PREFERS steers, it
                    # would re-pop and regenerate it as a duplicate follow-up turn.
                    # This final purge closes that. Kept in its own try so it can
                    # never break the drain that follows.
                    try:
                        if delivered_steer_ids:
                            await Chats.remove_steer_items_by_ids(
                                metadata["chat_id"], list(delivered_steer_ids)
                            )
                    except Exception:
                        log.exception(
                            "final delivered-steer purge failed for chat %s",
                            metadata.get("chat_id"),
                        )
                    try:
                        from open_webui.utils.chat_queue import maybe_drain_queue

                        await _release_completed_generation_operation()
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

                async def _cancel_teardown():
                    # ALL authoritative terminal cleanup for a Stop, in one coroutine
                    # so it can be shielded as a unit (see the shield-and-re-await
                    # loop below). A 2nd CancelledError must not truncate any of this.
                    #
                    # Every step is failure-isolated: no matter what breaks earlier
                    # in the teardown, the terminal done=True upsert at the bottom
                    # must run — a Stop that leaves the message not-done reads to
                    # every reconcile path as a failed request and gets auto-retried,
                    # which is exactly what a Stop must never do.
                    # Close any block the Stop caught mid-flight and PUSH it,
                    # before the cancel event. The teardown below already stamps
                    # ended_at/duration for the DB and the RAM snapshot, but a
                    # durable value nobody is told about is invisible: the tab
                    # that pressed Stop keeps `duration == null` on the open
                    # reasoning block and spins "Thinking…" until a reload. It is
                    # also the one tab the cancel handler deliberately does NOT
                    # re-snapshot (its own view is treated as authoritative), so
                    # nothing else was ever going to correct it.
                    #
                    # Ordering matters: `chat:tasks:cancel` flips the message
                    # terminal client-side, so the content has to land first.
                    try:
                        tail_acc.materialize()
                        if _finalize_open_agentic_blocks(content_blocks):
                            await event_emitter(
                                {
                                    "type": "chat:completion",
                                    "data": {"content_blocks": content_blocks},
                                }
                            )
                    except Exception:
                        log.exception("cancel open-block finalize/emit failed")

                    try:
                        await event_emitter({"type": "chat:tasks:cancel"})
                    except Exception:
                        log.exception("chat:tasks:cancel emit failed")

                    # Stop pressed mid-stream: PAUSE the queue. Clear only THIS
                    # generation's draining marker so a queued follow-up that was
                    # already started isn't disturbed; the user resumes manually.
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

                    if STREAM_PROTOCOL_VERSION == "v2.1" and metadata.get("message_id"):
                        # Fold the tail buffer + push the FULL final content into the
                        # RAM snapshot before flipping to "cancelled", so a reload
                        # racing this terminal transition sees the complete partial
                        # response (not the cadence snapshot, which can lag).
                        try:
                            tail_acc.materialize()
                            _finalize_open_agentic_blocks(content_blocks)
                            set_stream_state(
                                metadata["message_id"],
                                {
                                    "content_blocks": _strip_tool_results(
                                        content_blocks
                                    ),
                                    "status": "cancelled",
                                    "snapshot_version": stream_version_get(
                                        metadata["message_id"]
                                    ),
                                },
                            )
                            stream_version_flush(metadata["message_id"])
                            clear_stream_state(metadata["message_id"])
                        except Exception:
                            log.exception("cancel RAM-snapshot finalize failed")

                    # C22: ALWAYS run the subagent sweep/reconcile/broadcast and the
                    # terminal done write on cancel, regardless of stream protocol or
                    # realtime-save — matching the unconditional sweep in the clean and
                    # error finalizers. Previously this was gated behind
                    # `v2.1 or not ENABLE_REALTIME_CHAT_SAVE`, so a Stop under the
                    # supported v1 + ENABLE_REALTIME_CHAT_SAVE=True combo left any
                    # non-terminal subagent stuck 'running' and the parent message
                    # never marked done.
                    update_data = {}
                    try:
                        tail_acc.materialize()
                        # Match the clean/error finalizers: terminalize any run
                        # whose per-child cancellation write was interrupted,
                        # then broadcast that durable truth before reconciling
                        # completed answers into the parent's tool results.
                        await _sweep_subagent_runs("cancelled")
                        await _reconcile_subagent_results()
                        _finalize_open_agentic_blocks(content_blocks)
                        update_data = _build_checkpoint_update(
                            include_legacy_content=True
                        )
                    except Exception:
                        log.exception("cancel content finalize failed")
                    # Mark the message TERMINAL so its chat doesn't render as
                    # perpetually generating after a cancel. Written even when the
                    # content finalize above failed (partial content is already
                    # checkpointed; a stranded not-done message is worse).
                    update_data["done"] = True

                    # Record USER intent here, at the one point that knows this run
                    # really was cancelled. The Stop endpoint's own marker refuses
                    # already-`done` rows (so a Stop racing a clean finish can't
                    # mislabel a complete answer), which leaves the very fast Stop —
                    # latched before the assistant row existed — with nobody to
                    # write it. Gated on the durable cancellation latch so a
                    # shutdown / chat-delete cancellation is not reported as a user
                    # Stop.
                    try:
                        _redis = getattr(request.app.state, "redis", None)
                        if metadata.get("chat_id") and (
                            await is_generation_cancelled(
                                _redis,
                                metadata.get("chat_id"),
                                metadata.get("generation_id"),
                            )
                            or await is_generation_turn_cancelled(
                                _redis,
                                metadata.get("chat_id"),
                                metadata.get("turn_id"),
                            )
                        ):
                            update_data["userStopped"] = True
                    except Exception:
                        log.exception("cancel userStopped classification failed")

                    # Terminal (cancel) persist: same GC opt-in as the clean
                    # finalizer — this is a definitive end-of-turn write, so it is
                    # safe to prune tool_result_bodies down to the refs the final
                    # content_blocks still point at.
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        update_data,
                        return_model=False,
                        prune_tool_result_bodies=True,
                    )
                    if STREAM_PROTOCOL_VERSION == "v2.1" and metadata.get("message_id"):
                        try:
                            clear_tool_result_bodies(metadata["message_id"])
                        except Exception:
                            log.exception("cancel tool-result-body clear failed")

                # SHIELD-AND-RE-AWAIT: a Stop can be delivered MORE THAN ONCE (two
                # clicks; delete-chat firing several stop commands). A bare
                # asyncio.shield is NOT enough — when the outer await is re-cancelled
                # it re-raises immediately, leaving the detached teardown unfinished
                # (verified). Loop, swallowing re-cancels, until the shielded teardown
                # actually COMPLETES, so a double-cancel can never truncate the
                # terminal writes (sweep / done=True / cancel emit) and strand
                # subagent cards 'running' forever.
                _td = asyncio.ensure_future(_cancel_teardown())
                while not _td.done():
                    try:
                        await asyncio.shield(_td)
                    except asyncio.CancelledError:
                        if _td.done():
                            break
                        continue  # re-cancel during teardown — keep awaiting it
                if not _td.cancelled() and _td.exception() is not None:
                    log.error(
                        "cancel teardown failed: %r",
                        _td.exception(),
                        exc_info=_td.exception(),
                    )

                # Re-raise so the cancellation propagates and the task unwinds/exits.
                # Swallowing it leaves the task alive in anyio's cancel scope,
                # rescheduling _deliver_cancellation every tick forever (py-spy:
                # ~78% CPU at idle). The teardown above is already complete.
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
