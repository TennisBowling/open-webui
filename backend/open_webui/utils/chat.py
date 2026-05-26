import time
import logging
import re
import sys

from aiocache import cached
from typing import Any, Optional
import random
import json
import inspect
import uuid
import asyncio

from fastapi import Request, status
from starlette.responses import Response, StreamingResponse, JSONResponse


from open_webui.models.users import UserModel

from open_webui.socket.main import (
    sio,
    get_event_call,
    get_event_emitter,
)
from open_webui.functions import generate_function_chat_completion

from open_webui.routers.openai import (
    generate_chat_completion as generate_openai_chat_completion,
)

from open_webui.routers.ollama import (
    generate_chat_completion as generate_ollama_chat_completion,
)

from open_webui.routers.pipelines import (
    process_pipeline_inlet_filter,
    process_pipeline_outlet_filter,
)

from open_webui.models.functions import Functions
from open_webui.models.models import Models
from open_webui.models.chats import Chats
from open_webui.models.files import Files
from open_webui.utils.messages import blocks_to_api_messages


from open_webui.utils.plugin import (
    load_function_module_by_id,
    get_function_module_from_cache,
)
from open_webui.utils.models import get_all_models, check_model_access
from open_webui.utils.payload import convert_payload_openai_to_ollama
from open_webui.utils.response import (
    convert_response_ollama_to_openai,
    convert_streaming_response_ollama_to_openai,
)
from open_webui.utils.filter import (
    get_sorted_filter_ids,
    process_filter_functions,
)

from open_webui.env import SRC_LOG_LEVELS, GLOBAL_LOG_LEVEL, BYPASS_MODEL_ACCESS_CONTROL


logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


# ---------------------------------------------------------------------------
# B10: server-side conversation assembly (v2 chat/completions body shape)
#
# Ports `expandMessagesForToolResumption` and `buildTextFileBlocks` from
# `src/lib/components/chat/Chat.svelte` so the frontend can stop shipping the
# full `messages: [...]` array on every send. The frontend posts a small body
# carrying `leaf_message_id`; the backend walks the chat tree to assemble the
# canonical conversation context.
# ---------------------------------------------------------------------------


_TEXT_FILE_EXTS = {
    "txt", "md", "markdown", "rst", "csv", "tsv", "json", "jsonl", "ndjson",
    "yaml", "yml", "toml", "ini", "cfg", "conf", "env", "log", "xml", "svg",
    "py", "pyi", "ipynb", "js", "mjs", "cjs", "ts", "tsx", "jsx", "vue",
    "svelte", "java", "kt", "kts", "scala", "groovy", "c", "cc", "cpp",
    "cxx", "h", "hpp", "hxx", "rs", "go", "rb", "php", "pl", "pm", "lua",
    "r", "jl", "dart", "swift", "m", "mm", "cs", "fs", "fsx", "ex", "exs",
    "erl", "hs", "ml", "mli", "clj", "cljs", "sh", "bash", "zsh", "fish",
    "ps1", "bat", "cmd", "sql", "graphql", "gql", "proto", "css", "scss",
    "sass", "less", "tex", "bib", "srt", "vtt", "patch", "diff", "gitignore",
    "dockerignore", "editorconfig",
}

_EXTRACTABLE_EXTS = {
    "docx", "doc", "odt", "rtf", "pptx", "ppt", "xlsx", "xls", "html",
    "htm", "epub",
}


def _file_ext(file: dict) -> str:
    name = (file.get("name") or (file.get("file") or {}).get("filename") or "").lower()
    if name.endswith(".pdf"):
        return "pdf"
    dot = name.rfind(".")
    return name[dot + 1 :] if dot >= 0 else ""


def _is_text_file(file: dict) -> bool:
    if not file or file.get("type") != "file":
        return False
    ext = _file_ext(file)
    if ext == "pdf":
        return False
    if ext and ext in _TEXT_FILE_EXTS:
        return True
    ct = (
        file.get("content_type")
        or ((file.get("file") or {}).get("meta") or {}).get("content_type")
        or ""
    ).lower()
    if ct.startswith("text/") and "html" not in ct:
        return True
    return False


def _escape_xml_attr(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _read_file_text(file: dict) -> str:
    """Best-effort plaintext read for a file dict that's already passed
    ``_is_text_file``. Prefers the cached extracted content on ``file.data``;
    falls back to reading the storage path directly.
    """
    if isinstance(file.get("_inlinedText"), str):
        return file["_inlinedText"]

    file_id = file.get("id") or (file.get("file") or {}).get("id")
    if not file_id:
        return ""

    try:
        from open_webui.storage.provider import Storage

        record = Files.get_file_by_id(file_id)
        if record is None:
            return ""

        data = record.data or {}
        cached_content = data.get("content")
        if isinstance(cached_content, str) and cached_content:
            return cached_content

        if record.path:
            try:
                local_path = Storage.get_file(record.path)
                with open(local_path, "r", encoding="utf-8", errors="replace") as fh:
                    return fh.read()
            except Exception as e:
                log.debug(f"build_text_file_blocks: failed to read {file_id}: {e}")
                return ""
    except Exception as e:
        log.debug(f"build_text_file_blocks: lookup failed for {file_id}: {e}")
        return ""

    return ""


def build_text_file_blocks(files: Optional[list]) -> str:
    """Ported from Chat.svelte:buildTextFileBlocks. Returns the
    ``<document filename="...">...</document>`` prefix to prepend to a user
    message's text content, or '' if there are no inlineable text files.
    """
    if not files:
        return ""
    text_files = [f for f in files if _is_text_file(f)]
    if not text_files:
        return ""

    blocks = []
    for f in text_files:
        name = f.get("name") or (f.get("file") or {}).get("filename") or "file"
        text = _read_file_text(f)
        blocks.append(
            f'<document filename="{_escape_xml_attr(name)}">\n{text}\n</document>'
        )
    return "\n\n".join(blocks) + "\n\n"


_TOOL_CALLS_DETAILS_RE = re.compile(
    r'<details\s+type="tool_calls"([^>]*)>[\s\S]*?</details>', re.IGNORECASE
)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_LEGACY_TOOL_DETAILS_RE = re.compile(
    r'<details\s+type="tool_calls"[^>]+done="true"', re.IGNORECASE
)


def _get_string_message_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "".join(parts)
    return ""


def _normalize_preserved(text: str) -> str:
    return (text or "").strip()


def _expand_preserved_tool_context_message(message: dict) -> list:
    if not message.get("preservedToolContext"):
        return [message]

    content = _get_string_message_content(message.get("content"))
    matches = list(_TOOL_CALLS_DETAILS_RE.finditer(content))
    if not matches:
        return [message]

    parsed = []
    for m in matches:
        attrs = {}
        for am in _ATTR_RE.finditer(m.group(1) or ""):
            attrs[am.group(1)] = am.group(2)
        if attrs.get("done") == "true" and attrs.get("id") and attrs.get("name"):
            parsed.append(
                {
                    "matchStart": m.start(),
                    "matchEnd": m.end(),
                    "id": attrs["id"],
                    "name": attrs["name"],
                    "arguments": attrs.get("arguments", ""),
                    "result": attrs.get("result", ""),
                }
            )

    if not parsed:
        return [message]

    groups = [{"toolCalls": [parsed[0]], "textBeforeStart": 0}]
    for i in range(1, len(parsed)):
        prev_end = parsed[i - 1]["matchEnd"]
        curr_start = parsed[i]["matchStart"]
        between = _normalize_preserved(content[prev_end:curr_start])
        if between:
            groups.append({"toolCalls": [parsed[i]], "textBeforeStart": prev_end})
        else:
            groups[-1]["toolCalls"].append(parsed[i])

    last_tc = parsed[-1]
    trailing_text = _normalize_preserved(content[last_tc["matchEnd"] :])
    has_trailing = bool(trailing_text)

    reasoning_per_round = message.get("reasoning_details_per_round")
    if not isinstance(reasoning_per_round, list):
        reasoning_per_round = None

    expanded = []
    for group_idx, group in enumerate(groups):
        is_last_group = group_idx == len(groups) - 1
        first_tc = group["toolCalls"][0]
        text_before = _normalize_preserved(
            content[group["textBeforeStart"] : first_tc["matchStart"]]
        )

        if reasoning_per_round is not None and group_idx < len(reasoning_per_round):
            group_reasoning = reasoning_per_round[group_idx]
        elif is_last_group and not has_trailing:
            group_reasoning = message.get("reasoning_details")
        else:
            group_reasoning = None

        emitted = {
            **message,
            "role": "assistant",
            "content": text_before,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"] or "",
                    },
                }
                for tc in group["toolCalls"]
            ],
        }
        emitted.pop("preservedToolContext", None)
        if group_reasoning is None:
            emitted.pop("reasoning_details", None)
        else:
            emitted["reasoning_details"] = group_reasoning
        expanded.append(emitted)

        for tc in group["toolCalls"]:
            expanded.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tc["result"] or "",
                }
            )

    if has_trailing:
        if reasoning_per_round is not None and len(reasoning_per_round) > len(groups):
            trailing_reasoning = reasoning_per_round[-1]
        elif reasoning_per_round is None:
            trailing_reasoning = message.get("reasoning_details")
        else:
            trailing_reasoning = None

        trailing_msg = {
            **message,
            "content": trailing_text,
        }
        trailing_msg.pop("preservedToolContext", None)
        trailing_msg.pop("tool_calls", None)
        if trailing_reasoning is None:
            trailing_msg.pop("reasoning_details", None)
        else:
            trailing_msg["reasoning_details"] = trailing_reasoning
        expanded.append(trailing_msg)

    return expanded


def expand_messages_for_tool_resumption(messages: list) -> list:
    """Port of Chat.svelte:expandMessagesForToolResumption. Assistant messages
    that already carry structured ``content_blocks`` pass through unchanged
    (``blocks_to_api_messages`` handles them downstream). Messages with the
    ``preservedToolContext`` flag are re-expanded from their HTML tool-call
    markers. Legacy assistant messages with tool-call HTML but no flag get the
    same recovery applied opportunistically.
    """
    out: list = []
    for message in messages or []:
        if not isinstance(message, dict):
            out.append(message)
            continue

        if (
            message.get("role") == "assistant"
            and isinstance(message.get("content_blocks"), list)
            and len(message["content_blocks"]) > 0
        ):
            out.append(message)
            continue

        if message.get("preservedToolContext"):
            out.extend(_expand_preserved_tool_context_message(message))
            continue

        if message.get("role") == "assistant" and not message.get("tool_calls"):
            content = _get_string_message_content(message.get("content") or "")
            if _LEGACY_TOOL_DETAILS_RE.search(content):
                out.extend(
                    _expand_preserved_tool_context_message(
                        {**message, "preservedToolContext": True}
                    )
                )
                continue

        out.append(message)
    return out


def _walk_messages_from_leaf(messages_map: dict, leaf_id: str) -> list:
    chain = []
    seen = set()
    current_id = leaf_id
    while current_id and current_id not in seen:
        seen.add(current_id)
        msg = messages_map.get(current_id)
        if not msg:
            break
        chain.append(msg)
        current_id = msg.get("parentId")
    chain.reverse()
    return chain


def assemble_conversation_from_leaf(
    chat_id: str,
    leaf_message_id: Optional[str],
    new_user_message: Optional[dict] = None,
    model: Optional[dict] = None,
    system_prompt: Optional[str] = None,
) -> list[dict]:
    """Backend equivalent of the frontend's ``createMessagesList`` +
    ``expandMessagesForToolResumption`` + ``buildTextFileBlocks`` +
    ``blocks_to_api_messages`` pipeline. Returns the OpenAI-shape message list
    ready to feed into ``generate_chat_completion``.

    ``new_user_message`` (when provided) is persisted via
    ``Chats.upsert_message_to_chat_by_id_and_message_id`` AND appended to the
    walk so the first send of a new turn works without a separate save round-trip
    from the frontend.
    """
    messages_map = Chats.get_messages_map_by_chat_id(chat_id) or {}

    chain = (
        _walk_messages_from_leaf(messages_map, leaf_message_id)
        if leaf_message_id
        else []
    )

    if new_user_message and new_user_message.get("id"):
        new_id = new_user_message["id"]
        if new_id not in messages_map:
            persisted = {
                "id": new_id,
                "parentId": new_user_message.get("parentId") or leaf_message_id,
                "childrenIds": [],
                "role": new_user_message.get("role") or "user",
                "content": new_user_message.get("content") or "",
                "files": new_user_message.get("files") or [],
                "timestamp": int(time.time()),
                "models": new_user_message.get("models") or [],
            }
            try:
                Chats.upsert_message_to_chat_by_id_and_message_id(
                    chat_id, new_id, persisted
                )
            except Exception as e:
                log.debug(
                    f"assemble_conversation_from_leaf: failed to persist new_user_message: {e}"
                )
            chain.append(persisted)

    expanded = expand_messages_for_tool_resumption(chain)

    model_supports_vision = True
    if isinstance(model, dict):
        caps = (((model.get("info") or {}).get("meta") or {}).get("capabilities")) or {}
        model_supports_vision = caps.get("vision", True)

    prepared: list[dict] = []
    if system_prompt:
        prepared.append({"role": "system", "content": system_prompt})

    for message in expanded:
        if not isinstance(message, dict):
            continue

        if (
            message.get("role") == "assistant"
            and isinstance(message.get("content_blocks"), list)
            and len(message["content_blocks"]) > 0
        ):
            forwarded = {
                "role": "assistant",
                "content_blocks": message["content_blocks"],
            }
            if message.get("reasoning_details_per_round"):
                forwarded["reasoning_details_per_round"] = message["reasoning_details_per_round"]
            if message.get("reasoning_details"):
                forwarded["reasoning_details"] = message["reasoning_details"]
            prepared.append(forwarded)
            continue

        if message.get("role") == "tool":
            forwarded = {"role": "tool", "content": message.get("content") or ""}
            if message.get("tool_call_id"):
                forwarded["tool_call_id"] = message["tool_call_id"]
            prepared.append(forwarded)
            continue

        if message.get("tool_calls"):
            forwarded = {
                "role": "assistant",
                "content": message.get("content") or None,
                "tool_calls": message["tool_calls"],
            }
            if message.get("reasoning_details"):
                forwarded["reasoning_details"] = message["reasoning_details"]
            prepared.append(forwarded)
            continue

        files = message.get("files") or []
        is_user = message.get("role") == "user"

        has_images = any(f.get("type") == "image" for f in files)
        has_pdf = any(
            f.get("type") == "file"
            and (
                (f.get("name") or "").lower().endswith(".pdf")
                or ((f.get("file") or {}).get("filename") or "").lower().endswith(".pdf")
            )
            for f in files
        )
        has_extractable = any(
            f.get("type") == "file" and _file_ext(f) in _EXTRACTABLE_EXTS
            for f in files
        )

        text_prefix = build_text_file_blocks(files) if is_user else ""
        base_text = (message.get("merged") or {}).get("content") or message.get("content") or ""

        if is_user and (
            ((has_images or has_pdf) and model_supports_vision) or has_extractable
        ):
            parts: list = [{"type": "text", "text": text_prefix + base_text}]

            if model_supports_vision:
                for f in files:
                    if f.get("type") == "image":
                        parts.append(
                            {"type": "image_url", "image_url": {"url": f.get("url")}}
                        )
                for f in files:
                    if f.get("type") == "file" and (
                        (f.get("name") or "").lower().endswith(".pdf")
                        or ((f.get("file") or {}).get("filename") or "")
                        .lower()
                        .endswith(".pdf")
                    ):
                        parts.append(
                            {
                                "type": "file",
                                "file": {
                                    "filename": f.get("name")
                                    or (f.get("file") or {}).get("filename")
                                    or "document.pdf",
                                    "file_data": f.get("url")
                                    or f"/api/v1/files/{f.get('id')}/content",
                                },
                            }
                        )

            for f in files:
                if f.get("type") == "file" and _file_ext(f) in _EXTRACTABLE_EXTS:
                    parts.append(
                        {
                            "type": "file",
                            "file": {
                                "filename": f.get("name")
                                or (f.get("file") or {}).get("filename")
                                or "document",
                                "file_data": f.get("url")
                                or f"/api/v1/files/{f.get('id')}/content",
                                "processing_mode": "pdf"
                                if f.get("processing_mode") == "pdf"
                                else "text",
                            },
                        }
                    )

            prepared.append({"role": message.get("role"), "content": parts})
            continue

        forwarded = {
            "role": message.get("role"),
            "content": text_prefix + base_text if is_user and text_prefix else base_text,
        }
        if message.get("reasoning_details"):
            forwarded["reasoning_details"] = message["reasoning_details"]
        prepared.append(forwarded)

    return blocks_to_api_messages(prepared)


async def generate_direct_chat_completion(
    request: Request,
    form_data: dict,
    user: Any,
    models: dict,
):
    metadata = form_data.pop("metadata", {})

    user_id = metadata.get("user_id")
    session_id = metadata.get("session_id")
    request_id = str(uuid.uuid4())  # Generate a unique request ID

    event_caller = get_event_call(metadata)

    channel = f"{user_id}:{session_id}:{request_id}"

    if form_data.get("stream"):
        q = asyncio.Queue()

        async def message_listener(sid, data):
            """
            Handle received socket messages and push them into the queue.
            """
            await q.put(data)

        # Register the listener
        sio.on(channel, message_listener)

        # Start processing chat completion in background
        res = await event_caller(
            {
                "type": "request:chat:completion",
                "data": {
                    "form_data": form_data,
                    "model": models[form_data["model"]],
                    "channel": channel,
                    "session_id": session_id,
                },
            }
        )

        if res.get("status", False):
            # Define a generator to stream responses
            async def event_generator():
                nonlocal q
                try:
                    while True:
                        data = await q.get()  # Wait for new messages

                        if isinstance(data, dict):
                            if "done" in data and data["done"]:
                                break  # Stop streaming when 'done' is received

                            yield f"data: {json.dumps(data)}\n\n"
                        elif isinstance(data, str):
                            if "data:" in data:
                                yield f"{data}\n\n"
                            else:
                                yield f"data: {data}\n\n"
                except Exception as e:
                    log.error(f"Error in event generator: {e}", exc_info=True)
                    pass

            # Define a background task to run the event generator
            async def background():
                try:
                    del sio.handlers["/"][channel]
                except Exception as e:
                    log.warning(f"Error cleaning up channel: {e}")
                    pass

            # Return the streaming response
            return StreamingResponse(
                event_generator(), media_type="text/event-stream", background=background
            )
        else:
            log.error(f"Direct completion status is False! Response: {res}")
            raise Exception(str(res))
    else:
        res = await event_caller(
            {
                "type": "request:chat:completion",
                "data": {
                    "form_data": form_data,
                    "model": models[form_data["model"]],
                    "channel": channel,
                    "session_id": session_id,
                },
            }
        )

        if "error" in res and res["error"]:
            raise Exception(res["error"])

        return res


async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user: Any,
    bypass_filter: bool = False,
):
    log.debug(f"generate_chat_completion: {form_data}")
    if BYPASS_MODEL_ACCESS_CONTROL:
        bypass_filter = True

    if hasattr(request.state, "metadata"):
        if "metadata" not in form_data:
            form_data["metadata"] = request.state.metadata
        else:
            form_data["metadata"] = {
                **form_data["metadata"],
                **request.state.metadata,
            }

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
        log.info(f"🔄 ROUTING: Direct flag set, model: {request.state.model['id']}")
    else:
        models = request.app.state.MODELS

    model_id = form_data["model"]
    if model_id not in models:
        raise Exception("Model not found")

    model = models[model_id]

    # Check if model is in MODELS (backend-managed) - if so, DON'T use direct flow
    is_in_backend_models = model_id in request.app.state.MODELS
    is_direct_flag_set = getattr(request.state, "direct", False)

    log.info(f"🔄 ROUTING: model_id={model_id}, is_in_backend_models={is_in_backend_models}, is_direct_flag_set={is_direct_flag_set}")

    if is_direct_flag_set and not is_in_backend_models:
        log.info(f"🔄 ROUTING: Using DIRECT completion flow for {model_id}")
        return await generate_direct_chat_completion(
            request, form_data, user=user, models=models
        )
    else:
        log.info(f"🔄 ROUTING: Using BACKEND completion flow for {model_id} (owned_by: {model.get('owned_by')})")
        # Check if user has access to the model
        if not bypass_filter and user.role == "user":
            try:
                check_model_access(user, model)
            except Exception as e:
                raise e

        if model.get("owned_by") == "arena":
            model_ids = model.get("info", {}).get("meta", {}).get("model_ids")
            filter_mode = model.get("info", {}).get("meta", {}).get("filter_mode")
            if model_ids and filter_mode == "exclude":
                model_ids = [
                    model["id"]
                    for model in list(request.app.state.MODELS.values())
                    if model.get("owned_by") != "arena" and model["id"] not in model_ids
                ]

            selected_model_id = None
            if isinstance(model_ids, list) and model_ids:
                selected_model_id = random.choice(model_ids)
            else:
                model_ids = [
                    model["id"]
                    for model in list(request.app.state.MODELS.values())
                    if model.get("owned_by") != "arena"
                ]
                selected_model_id = random.choice(model_ids)

            form_data["model"] = selected_model_id

            if form_data.get("stream") == True:

                async def stream_wrapper(stream):
                    yield f"data: {json.dumps({'selected_model_id': selected_model_id})}\n\n"
                    async for chunk in stream:
                        yield chunk

                response = await generate_chat_completion(
                    request, form_data, user, bypass_filter=True
                )
                return StreamingResponse(
                    stream_wrapper(response.body_iterator),
                    media_type="text/event-stream",
                    background=response.background,
                )
            else:
                return {
                    **(
                        await generate_chat_completion(
                            request, form_data, user, bypass_filter=True
                        )
                    ),
                    "selected_model_id": selected_model_id,
                }

        if model.get("pipe"):
            # Below does not require bypass_filter because this is the only route the uses this function and it is already bypassing the filter
            return await generate_function_chat_completion(
                request, form_data, user=user, models=models
            )
        if model.get("owned_by") == "ollama":
            # Using /ollama/api/chat endpoint
            form_data = convert_payload_openai_to_ollama(form_data)
            response = await generate_ollama_chat_completion(
                request=request,
                form_data=form_data,
                user=user,
                bypass_filter=bypass_filter,
            )
            if form_data.get("stream"):
                response.headers["content-type"] = "text/event-stream"
                return StreamingResponse(
                    convert_streaming_response_ollama_to_openai(response),
                    headers=dict(response.headers),
                    background=response.background,
                )
            else:
                return convert_response_ollama_to_openai(response)
        else:
            return await generate_openai_chat_completion(
                request=request,
                form_data=form_data,
                user=user,
                bypass_filter=bypass_filter,
            )


chat_completion = generate_chat_completion


async def chat_completed(request: Request, form_data: dict, user: Any):
    if not request.app.state.MODELS:
        await get_all_models(request, user=user)

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    data = form_data
    model_id = data["model"]
    if model_id not in models:
        raise Exception("Model not found")

    model = models[model_id]

    try:
        data = await process_pipeline_outlet_filter(request, data, user, models)
    except Exception as e:
        return Exception(f"Error: {e}")

    metadata = {
        "chat_id": data["chat_id"],
        "message_id": data["id"],
        "filter_ids": data.get("filter_ids", []),
        "session_id": data["session_id"],
        "user_id": user.id,
    }

    extra_params = {
        "__event_emitter__": get_event_emitter(metadata),
        "__event_call__": get_event_call(metadata),
        "__user__": user.model_dump() if isinstance(user, UserModel) else {},
        "__metadata__": metadata,
        "__request__": request,
        "__model__": model,
    }

    try:
        filter_functions = [
            Functions.get_function_by_id(filter_id)
            for filter_id in get_sorted_filter_ids(
                request, model, metadata.get("filter_ids", [])
            )
        ]

        result, _ = await process_filter_functions(
            request=request,
            filter_functions=filter_functions,
            filter_type="outlet",
            form_data=data,
            extra_params=extra_params,
        )
        return result
    except Exception as e:
        return Exception(f"Error: {e}")


async def chat_action(request: Request, action_id: str, form_data: dict, user: Any):
    if "." in action_id:
        action_id, sub_action_id = action_id.split(".")
    else:
        sub_action_id = None

    action = Functions.get_function_by_id(action_id)
    if not action:
        raise Exception(f"Action not found: {action_id}")

    if not request.app.state.MODELS:
        await get_all_models(request, user=user)

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    data = form_data
    model_id = data["model"]

    if model_id not in models:
        raise Exception("Model not found")
    model = models[model_id]

    __event_emitter__ = get_event_emitter(
        {
            "chat_id": data["chat_id"],
            "message_id": data["id"],
            "session_id": data["session_id"],
            "user_id": user.id,
        }
    )
    __event_call__ = get_event_call(
        {
            "chat_id": data["chat_id"],
            "message_id": data["id"],
            "session_id": data["session_id"],
            "user_id": user.id,
        }
    )

    function_module, _, _ = get_function_module_from_cache(request, action_id)

    if hasattr(function_module, "valves") and hasattr(function_module, "Valves"):
        valves = Functions.get_function_valves_by_id(action_id)
        function_module.valves = function_module.Valves(**(valves if valves else {}))

    if hasattr(function_module, "action"):
        try:
            action = function_module.action

            # Get the signature of the function
            sig = inspect.signature(action)
            params = {"body": data}

            # Extra parameters to be passed to the function
            extra_params = {
                "__model__": model,
                "__id__": sub_action_id if sub_action_id is not None else action_id,
                "__event_emitter__": __event_emitter__,
                "__event_call__": __event_call__,
                "__request__": request,
            }

            # Add extra params in contained in function signature
            for key, value in extra_params.items():
                if key in sig.parameters:
                    params[key] = value

            if "__user__" in sig.parameters:
                __user__ = user.model_dump() if isinstance(user, UserModel) else {}

                try:
                    if hasattr(function_module, "UserValves"):
                        __user__["valves"] = function_module.UserValves(
                            **Functions.get_user_valves_by_id_and_user_id(
                                action_id, user.id
                            )
                        )
                except Exception as e:
                    log.exception(f"Failed to get user values: {e}")

                params = {**params, "__user__": __user__}

            if inspect.iscoroutinefunction(action):
                data = await action(**params)
            else:
                data = action(**params)

        except Exception as e:
            return Exception(f"Error: {e}")

    return data
