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


async def run_outlet_filters_on_completed_stream(
    request: Request,
    user: Any,
    metadata: dict,
    model: dict,
    model_id: str,
    filter_ids: list,
    content_blocks: list,
    event_emitter,
    event_caller,
    serialize_content_blocks,
):
    # B12: outlet filters used to run from POST /api/chat/completed; now run
    # server-side at end of process_chat_response. Mirrors the synthetic
    # message list assembly the frontend used to send: a final assistant turn
    # carrying the serialized content. If a filter mutates that content, we
    # persist the mutation and emit a catch-up event so the frontend mirror
    # matches what's in the DB.
    if not request.app.state.MODELS:
        await get_all_models(request, user=user)

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {request.state.model["id"]: request.state.model}
    else:
        models = request.app.state.MODELS

    if model_id not in models:
        return

    content = serialize_content_blocks(content_blocks, force=True)
    assistant_message = {
        "id": metadata.get("message_id"),
        "role": "assistant",
        "content": content,
    }

    data = {
        "model": model_id,
        "messages": [assistant_message],
        "chat_id": metadata.get("chat_id"),
        "session_id": metadata.get("session_id"),
        "id": metadata.get("message_id"),
        "filter_ids": filter_ids or [],
    }

    try:
        data = await process_pipeline_outlet_filter(request, data, user, models)
    except Exception as e:
        log.exception(f"Pipeline outlet filter failed: {e}")
        return

    extra_params = {
        "__event_emitter__": event_emitter,
        "__event_call__": event_caller,
        "__user__": user.model_dump() if isinstance(user, UserModel) else {},
        "__metadata__": metadata,
        "__request__": request,
        "__model__": model,
    }

    try:
        filter_functions = [
            Functions.get_function_by_id(filter_id)
            for filter_id in get_sorted_filter_ids(
                request, model, filter_ids or []
            )
        ]
        result, _ = await process_filter_functions(
            request=request,
            filter_functions=filter_functions,
            filter_type="outlet",
            form_data=data,
            extra_params=extra_params,
        )
        if isinstance(result, dict):
            data = result
    except Exception as e:
        log.exception(f"Outlet filter functions failed: {e}")
        return

    final_messages = data.get("messages") or []
    final_content = None
    for m in reversed(final_messages):
        if m.get("role") == "assistant":
            final_content = m.get("content")
            break

    if final_content is None or final_content == content:
        return

    # Outlet filter mutated the assistant content. Merge the mutation back
    # into content_blocks WHILE PRESERVING structural blocks (reasoning,
    # tool_calls, subagent_launch, code_interpreter) byte-identical. The
    # serialized projection interleaves text-block contents with top-level
    # <details type="..."> markers; we split both the original and the
    # filter output on those markers and require an exact 1:1 structural
    # match. If anything is ambiguous, fail safe by leaving content_blocks
    # untouched — losing the filter text edit is strictly better than
    # silently destroying reasoning/tool_call data the user cannot recover.
    merged_blocks = _merge_outlet_filter_into_content_blocks(
        content_blocks, content, final_content
    )
    if merged_blocks is None:
        log.warning(
            "Outlet filter mutated structural markers or produced an "
            "ambiguous diff; skipping content_blocks update for message %s",
            metadata.get("message_id"),
        )
        return

    if merged_blocks == content_blocks:
        return

    try:
        Chats.upsert_message_to_chat_by_id_and_message_id(
            metadata["chat_id"],
            metadata["message_id"],
            {
                "content": final_content,
                "content_blocks": merged_blocks,
            },
        )
    except Exception as e:
        log.exception(f"Outlet filter persist failed: {e}")

    try:
        from open_webui.env import STREAM_PROTOCOL_VERSION

        if STREAM_PROTOCOL_VERSION == "v2":
            changed_indices = [
                i
                for i, (old_b, new_b) in enumerate(zip(content_blocks, merged_blocks))
                if old_b != new_b
            ]
            for idx in changed_indices:
                await event_emitter(
                    {
                        "type": "chat:delta",
                        "data": {
                            "message_id": metadata.get("message_id"),
                            "op": "replace",
                            "payload": {
                                "block_idx": idx,
                                "content_blocks": [merged_blocks[idx]],
                            },
                        },
                    }
                )
        else:
            await event_emitter(
                {
                    "type": "chat:message",
                    "data": {
                        "content": final_content,
                        "content_blocks": merged_blocks,
                    },
                }
            )
    except Exception as e:
        log.exception(f"Outlet filter catch-up emit failed: {e}")


_DETAILS_RE = re.compile(
    r'<details\s+type="[^"]+"[^>]*>.*?</details>',
    re.DOTALL,
)


def _split_serialized(s):
    segments = []
    last = 0
    for m in _DETAILS_RE.finditer(s):
        if m.start() > last:
            segments.append(("text", s[last : m.start()]))
        segments.append(("details", m.group(0)))
        last = m.end()
    if last < len(s):
        segments.append(("text", s[last:]))
    return segments


def _text_runs_between_details(segments):
    runs = []
    buf = []
    for kind, seg in segments:
        if kind == "text":
            buf.append(seg)
        else:
            runs.append("".join(buf))
            buf = []
    runs.append("".join(buf))
    return runs


def _merge_outlet_filter_into_content_blocks(
    content_blocks, original_serialized, filter_serialized
):
    # Returns updated content_blocks list on success, or None to signal
    # "ambiguous — caller should fail safe and leave blocks alone".
    original_segments = _split_serialized(original_serialized)
    filter_segments = _split_serialized(filter_serialized)

    orig_details = [seg for kind, seg in original_segments if kind == "details"]
    filt_details = [seg for kind, seg in filter_segments if kind == "details"]

    if orig_details != filt_details:
        return None

    orig_runs = _text_runs_between_details(original_segments)
    filt_runs = _text_runs_between_details(filter_segments)

    if len(orig_runs) != len(filt_runs):
        return None

    # Each non-empty text block in content_blocks produces exactly one text
    # contribution into the current run; structural blocks advance the run.
    # If multiple text blocks share one run we cannot safely re-attribute.
    text_blocks_in_run = [[]]
    for i, block in enumerate(content_blocks):
        btype = block.get("type")
        if btype == "text":
            text_blocks_in_run[-1].append(i)
        elif btype in ("tool_calls", "reasoning", "code_interpreter"):
            text_blocks_in_run.append([])
        else:
            return None

    if len(text_blocks_in_run) != len(filt_runs):
        return None

    new_blocks = [dict(b) for b in content_blocks]

    for run_i, idx_list in enumerate(text_blocks_in_run):
        orig_run = orig_runs[run_i]
        new_run = filt_runs[run_i]
        if orig_run == new_run:
            continue
        if len(idx_list) > 1:
            return None
        if len(idx_list) == 0:
            return None
        block_i = idx_list[0]
        new_blocks[block_i] = {
            **new_blocks[block_i],
            "content": new_run.strip("\n"),
        }

    return new_blocks


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
