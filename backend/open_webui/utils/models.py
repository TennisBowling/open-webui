import logging
import asyncio
import sys

from fastapi import Request

from open_webui.routers import openai, ollama
from open_webui.functions import get_function_models


from open_webui.models.functions import Functions
from open_webui.models.models import Models


from open_webui.utils.plugin import (
    get_function_module_from_cache,
)
from open_webui.utils.access_control import has_access
from open_webui.utils.context_window import resolve_context_length


from open_webui.config import (
    BYPASS_ADMIN_ACCESS_CONTROL,
    DEFAULT_ARENA_MODEL,
)

from open_webui.env import BYPASS_MODEL_ACCESS_CONTROL, SRC_LOG_LEVELS, GLOBAL_LOG_LEVEL
from open_webui.models.users import UserModel


logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

_pending_base_model_requests: dict[
    tuple[str | None, str | None, bool], asyncio.Task
] = {}

# Stable `created` timestamp for provider/arena models. These lists have no real
# per-model creation time, and using int(time.time()) made the /api/models
# payload (and its ETag) change on every single request, so 304 revalidation
# never fired. No client surface sorts or renders base/arena `created`.
STABLE_MODEL_CREATED = 0


def model_supports_video_input(model: dict) -> bool:
    """Whether ``model`` accepts ``video_url`` content parts.

    Precedence is explicit-over-discovered: an admin who ticks (or unticks) the
    Video capability on a workspace model is stating something the provider's
    metadata cannot override — that is the escape hatch for models OpenRouter
    hasn't tagged yet, and for ones whose tag is wrong. Only when no explicit
    value exists do we fall back to the provider's declared input modalities.

    Absence means "no": unlike vision, video is rare enough that defaulting to
    True would attach multi-megabyte payloads to models that will reject them.
    """
    if not isinstance(model, dict):
        return False

    caps = (((model.get("info") or {}).get("meta") or {}).get("capabilities")) or {}
    if isinstance(caps, dict) and caps.get("video") is not None:
        return bool(caps.get("video"))

    # Flattened form filled by _enrich_openrouter_modalities. This is the only
    # source available when the connection uses a model_ids allowlist, because
    # that path synthesizes bare stubs with no `architecture` at all.
    flat = model.get("input_modalities")
    if isinstance(flat, list) and "video" in flat:
        return True

    # OpenRouter (and any provider echoing its schema) reports this directly.
    for source in (model, model.get("openai") or {}):
        architecture = source.get("architecture") if isinstance(source, dict) else None
        if isinstance(architecture, dict):
            modalities = architecture.get("input_modalities") or []
            if isinstance(modalities, list) and "video" in modalities:
                return True

    return False


async def fetch_ollama_models(
    request: Request, user: UserModel = None, refresh: bool = False
):
    cache_options = {"cache_read": False} if refresh else {}
    raw_ollama_models = await ollama.get_all_models(
        request,
        user=user,
        # aiocache supports bypassing reads while still writing the fresh result.
        # This is both safer and more precise than guessing/deleting backend keys.
        **cache_options,
    )
    return [
        {
            "id": model["model"],
            "name": model["name"],
            "object": "model",
            "created": STABLE_MODEL_CREATED,
            "owned_by": "ollama",
            "ollama": model,
            "connection_type": model.get("connection_type", "local"),
            "tags": model.get("tags", []),
        }
        for model in raw_ollama_models["models"]
    ]


async def fetch_openai_models(
    request: Request, user: UserModel = None, refresh: bool = False
):
    cache_options = {"cache_read": False} if refresh else {}
    openai_response = await openai.get_all_models(
        request,
        user=user,
        **cache_options,
    )
    return openai_response["data"]


async def get_all_base_models(
    request: Request, user: UserModel = None, refresh: bool = False
):
    openai_task = (
        fetch_openai_models(request, user, refresh=refresh)
        if request.app.state.config.ENABLE_OPENAI_API
        else asyncio.sleep(0, result=[])
    )
    ollama_task = (
        fetch_ollama_models(request, user, refresh=refresh)
        if request.app.state.config.ENABLE_OLLAMA_API
        else asyncio.sleep(0, result=[])
    )
    function_task = get_function_models(request)

    openai_models, ollama_models, function_models = await asyncio.gather(
        openai_task, ollama_task, function_task
    )

    return function_models + openai_models + ollama_models


async def get_all_base_models_deduped(
    request: Request, refresh: bool = False, user: UserModel = None
):
    key = (
        getattr(user, "id", None),
        getattr(user, "role", None),
        refresh,
    )

    task = _pending_base_model_requests.get(key)
    if task is None or task.done():
        task = asyncio.create_task(
            get_all_base_models(request, user=user, refresh=refresh)
        )
        _pending_base_model_requests[key] = task

    try:
        return await asyncio.shield(task)
    finally:
        if _pending_base_model_requests.get(key) is task and task.done():
            _pending_base_model_requests.pop(key, None)


async def get_all_models(request, refresh: bool = False, user: UserModel = None):
    if (
        request.app.state.MODELS
        and request.app.state.BASE_MODELS
        and (request.app.state.config.ENABLE_BASE_MODELS_CACHE and not refresh)
    ):
        base_models = request.app.state.BASE_MODELS
    else:
        base_models = await get_all_base_models_deduped(
            request, refresh=refresh, user=user
        )
        request.app.state.BASE_MODELS = base_models
        request.app.state.BASE_MODELS_LOADED = True

    # deep copy the base models to avoid modifying the original list
    models = [model.copy() for model in base_models]

    # If there are no models, return an empty list
    if len(models) == 0:
        request.app.state.MODELS = {}
        return []

    # Add arena models
    if request.app.state.config.ENABLE_EVALUATION_ARENA_MODELS:
        arena_models = []
        if len(request.app.state.config.EVALUATION_ARENA_MODELS) > 0:
            arena_models = [
                {
                    "id": model["id"],
                    "name": model["name"],
                    "info": {
                        "meta": model["meta"],
                    },
                    "object": "model",
                    "created": STABLE_MODEL_CREATED,
                    "owned_by": "arena",
                    "arena": True,
                }
                for model in request.app.state.config.EVALUATION_ARENA_MODELS
            ]
        else:
            # Add default arena model
            arena_models = [
                {
                    "id": DEFAULT_ARENA_MODEL["id"],
                    "name": DEFAULT_ARENA_MODEL["name"],
                    "info": {
                        "meta": DEFAULT_ARENA_MODEL["meta"],
                    },
                    "object": "model",
                    "created": STABLE_MODEL_CREATED,
                    "owned_by": "arena",
                    "arena": True,
                }
            ]
        models = models + arena_models

    enabled_functions, custom_models = await asyncio.gather(
        Functions.get_functions(active_only=True),
        Models.get_all_models(),
    )

    enabled_actions = {
        function.id: function
        for function in enabled_functions
        if function.type == "action"
    }
    enabled_filters = {
        function.id: function
        for function in enabled_functions
        if function.type == "filter"
    }
    global_action_ids = [
        function.id for function in enabled_actions.values() if function.is_global
    ]
    global_filter_ids = [
        function.id for function in enabled_filters.values() if function.is_global
    ]
    for custom_model in custom_models:
        if custom_model.base_model_id is None:
            # Applied directly to a base model
            for model in models:
                if custom_model.id == model["id"] or (
                    model.get("owned_by") == "ollama"
                    and custom_model.id
                    == model["id"].split(":")[
                        0
                    ]  # Ollama may return model ids in different formats (e.g., 'llama3' vs. 'llama3:7b')
                ):
                    if custom_model.is_active:
                        model["name"] = custom_model.name
                        model["info"] = custom_model.model_dump()

                        # Set action_ids and filter_ids
                        action_ids = []
                        filter_ids = []

                        if "info" in model and "meta" in model["info"]:
                            action_ids.extend(
                                model["info"]["meta"].get("actionIds", [])
                            )
                            filter_ids.extend(
                                model["info"]["meta"].get("filterIds", [])
                            )

                        model["action_ids"] = action_ids
                        model["filter_ids"] = filter_ids
                    else:
                        models.remove(model)

        elif custom_model.is_active and (
            custom_model.id not in [model["id"] for model in models]
        ):
            owned_by = "openai"
            pipe = None

            action_ids = []
            filter_ids = []
            base_reasoning = None
            base_input_modalities = None
            base_context_length = None

            for model in models:
                if (
                    custom_model.base_model_id == model["id"]
                    or custom_model.base_model_id == model["id"].split(":")[0]
                ):
                    owned_by = model.get("owned_by", "unknown owner")
                    if "pipe" in model:
                        pipe = model["pipe"]
                    # Carry the base model's discovered reasoning descriptor so a
                    # workspace model derived from an OpenRouter base still gets
                    # the auto-discovered effort options as a fallback.
                    base_reasoning = model.get("reasoning")
                    # Same rationale for input modalities: without this a preset
                    # derived from a video-capable base silently loses video
                    # support, since this branch builds a fresh dict rather than
                    # mutating the provider one.
                    base_input_modalities = model.get("input_modalities") or (
                        (model.get("architecture") or {}).get("input_modalities")
                        if isinstance(model.get("architecture"), dict)
                        else None
                    )
                    # Same rationale again for the context window: a preset over a
                    # 272k base must not read as "window unknown" just because this
                    # branch builds a fresh dict. Anything deciding whether to
                    # compact needs a real number here, and "absent" means "don't
                    # act", so silently losing it is the dangerous direction.
                    base_context_length = resolve_context_length(model)
                    break

            if custom_model.meta:
                meta = custom_model.meta.model_dump()

                if "actionIds" in meta:
                    action_ids.extend(meta["actionIds"])

                if "filterIds" in meta:
                    filter_ids.extend(meta["filterIds"])

            models.append(
                {
                    "id": f"{custom_model.id}",
                    "name": custom_model.name,
                    "object": "model",
                    "created": custom_model.created_at,
                    "owned_by": owned_by,
                    "info": custom_model.model_dump(),
                    "preset": True,
                    **({"pipe": pipe} if pipe is not None else {}),
                    **({"reasoning": base_reasoning} if base_reasoning else {}),
                    **(
                        {"input_modalities": base_input_modalities}
                        if base_input_modalities
                        else {}
                    ),
                    **(
                        {"context_length": base_context_length}
                        if base_context_length
                        else {}
                    ),
                    "action_ids": action_ids,
                    "filter_ids": filter_ids,
                }
            )

    # Process action_ids to get the actions
    def get_action_items_from_module(function, module):
        actions = []
        if hasattr(module, "actions"):
            actions = module.actions
            return [
                {
                    "id": f"{function.id}.{action['id']}",
                    "name": action.get("name", f"{function.name} ({action['id']})"),
                    "description": function.meta.description,
                    "icon": action.get(
                        "icon_url",
                        function.meta.manifest.get("icon_url", None)
                        or getattr(module, "icon_url", None)
                        or getattr(module, "icon", None),
                    ),
                }
                for action in actions
            ]
        else:
            return [
                {
                    "id": function.id,
                    "name": function.name,
                    "description": function.meta.description,
                    "icon": function.meta.manifest.get("icon_url", None)
                    or getattr(module, "icon_url", None)
                    or getattr(module, "icon", None),
                }
            ]

    # Process filter_ids to get the filters
    def get_filter_items_from_module(function, module):
        return [
            {
                "id": function.id,
                "name": function.name,
                "description": function.meta.description,
                "icon": function.meta.manifest.get("icon_url", None)
                or getattr(module, "icon_url", None)
                or getattr(module, "icon", None),
                "has_user_valves": hasattr(module, "UserValves"),
            }
        ]

    async def get_function_module_by_id(function_id):
        function_module, _, _ = await get_function_module_from_cache(
            request, function_id
        )
        return function_module

    model_function_ids = []
    for model in models:
        action_ids = [
            action_id
            for action_id in dict.fromkeys(
                model.pop("action_ids", []) + global_action_ids
            )
            if action_id in enabled_actions
        ]
        filter_ids = [
            filter_id
            for filter_id in dict.fromkeys(
                model.pop("filter_ids", []) + global_filter_ids
            )
            if filter_id in enabled_filters
        ]
        model_function_ids.append((model, action_ids, filter_ids))

    # Global actions/filters used to be fetched from the database and resolved
    # from the plugin cache once *per model*. With hundreds of provider models,
    # one global function therefore caused hundreds of serial DB round-trips.
    # Resolve each distinct function once, concurrently, then reuse its immutable
    # list descriptor for every model.
    function_ids = list(
        dict.fromkeys(
            function_id
            for _, action_ids, filter_ids in model_function_ids
            for function_id in action_ids + filter_ids
        )
    )
    function_modules = {}
    if function_ids:
        modules = await asyncio.gather(
            *(get_function_module_by_id(function_id) for function_id in function_ids)
        )
        function_modules = dict(zip(function_ids, modules))

    action_items = {
        function_id: get_action_items_from_module(
            enabled_actions[function_id], function_modules[function_id]
        )
        for function_id in function_ids
        if function_id in enabled_actions
    }
    filter_items = {
        function_id: (
            get_filter_items_from_module(
                enabled_filters[function_id], function_modules[function_id]
            )
            if getattr(function_modules[function_id], "toggle", None)
            else []
        )
        for function_id in function_ids
        if function_id in enabled_filters
    }

    for model, action_ids, filter_ids in model_function_ids:
        model["actions"] = [
            item for function_id in action_ids for item in action_items[function_id]
        ]
        model["filters"] = [
            item for function_id in filter_ids for item in filter_items[function_id]
        ]

    log.debug(f"get_all_models() returned {len(models)} models")

    request.app.state.MODELS = {model["id"]: model for model in models}
    return models


async def check_model_access(user, model):
    if model.get("arena"):
        if not has_access(
            user.id,
            type="read",
            access_control=model.get("info", {})
            .get("meta", {})
            .get("access_control", {}),
        ):
            raise Exception("Model not found")
    else:
        model_info = await Models.get_model_by_id(model.get("id"))
        if not model_info:
            raise Exception("Model not found")
        elif not (
            user.id == model_info.user_id
            or has_access(
                user.id, type="read", access_control=model_info.access_control
            )
        ):
            raise Exception("Model not found")


async def get_filtered_models(models, user):
    # Filter out models that the user does not have access to
    if (
        user.role == "user"
        or (user.role == "admin" and not BYPASS_ADMIN_ACCESS_CONTROL)
    ) and not BYPASS_MODEL_ACCESS_CONTROL:
        filtered_models = []
        model_infos = await Models.get_models_by_ids(
            [
                model["id"]
                for model in models
                if not model.get("arena") and model.get("id")
            ]
        )

        for model in models:
            if model.get("arena"):
                if has_access(
                    user.id,
                    type="read",
                    access_control=model.get("info", {})
                    .get("meta", {})
                    .get("access_control", {}),
                ):
                    filtered_models.append(model)
                continue

            model_info = model_infos.get(model["id"])
            if model_info:
                if (
                    (user.role == "admin" and BYPASS_ADMIN_ACCESS_CONTROL)
                    or user.id == model_info.user_id
                    or has_access(
                        user.id,
                        type="read",
                        access_control=model_info.access_control,
                    )
                ):
                    filtered_models.append(model)

        return filtered_models
    else:
        return models
