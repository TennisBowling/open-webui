import asyncio
import hashlib
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from open_webui.env import SRC_LOG_LEVELS
from open_webui.utils.auth import get_verified_user
from open_webui.utils.access_control import get_permissions

from open_webui.models.users import Users
from open_webui.models.chats import Chats
from open_webui.models.channels import Channels
from open_webui.models.tags import Tags

from open_webui.routers import configs as configs_router
from open_webui.routers import folders as folders_router
from open_webui.routers import tools as tools_router

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


router = APIRouter()


# Wire Contract #4 (Phase 0): the set of bootstrap components a client may request.
# Order matters only for deterministic iteration when assembling responses.
_ALLOWED_INCLUDES = (
    "config",
    "user",
    "settings",
    "models",
    "banners",
    "tools",
    "folders",
    "tags",
    "pinned",
    "chats",
    "channels",
)


def _etag(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _bundle_etag(component_etags: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(component_etags.items())).encode()
    ).hexdigest()[:16]


def _to_jsonable(value):
    # Match the projection FastAPI route handlers would have applied. Pydantic
    # models surface via model_dump; lists of them recurse.
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


def _unwrap_response(value):
    # Route handlers that are wrapped by `etag_response()` (B4) return a
    # `Response`/`JSONResponse` instead of a plain dict/list. Bootstrap needs
    # the raw payload so it can re-serialize and hash it as part of the
    # bundle. Detect & unwrap; otherwise pass the value through to
    # `_to_jsonable` which handles Pydantic models, lists, and primitives.
    if isinstance(value, Response):
        body = getattr(value, "body", None)
        if body is None:
            return None
        try:
            return json.loads(body)
        except (TypeError, ValueError):
            try:
                return json.loads(body.decode())
            except Exception:
                return None
    return _to_jsonable(value)


async def _resolve_config(request: Request, user):
    # Reuse the existing route handler so feature flag projection stays in sync.
    from open_webui.main import get_app_config

    return _unwrap_response(await get_app_config(request))


async def _resolve_user(request: Request, user):
    fresh = await Users.get_user_by_id(user.id) or user
    return {
        "id": fresh.id,
        "email": fresh.email,
        "name": fresh.name,
        "role": fresh.role,
        "profile_image_url": fresh.profile_image_url,
        "permissions": get_permissions(
            fresh.id, request.app.state.config.USER_PERMISSIONS
        ),
    }


async def _resolve_settings(request: Request, user):
    fresh = await Users.get_user_by_id(user.id)
    if not fresh:
        return None
    return _to_jsonable(fresh.settings)


async def _resolve_models(request: Request, user):
    from open_webui.main import get_models

    return _unwrap_response(await get_models(request, user=user))


async def _resolve_banners(request: Request, user):
    return _unwrap_response(
        await configs_router.get_banners(request=request, user=user)
    )


async def _resolve_tools(request: Request, user):
    return _unwrap_response(
        await tools_router.get_tools(request=request, user=user)
    )


async def _resolve_folders(request: Request, user):
    # After B4, folders.get_folders accepts a leading `request` arg (for
    # etag_response wiring). Pass via kwarg so this works whether or not
    # B4 has landed yet.
    try:
        result = await folders_router.get_folders(request=request, user=user)
    except TypeError:
        # Pre-B4 signature: (user=...)
        result = await folders_router.get_folders(user=user)
    return _unwrap_response(result)


async def _resolve_tags(request: Request, user):
    return _to_jsonable(await Tags.get_tags_by_user_id(user.id))


async def _resolve_pinned(request: Request, user):
    return _to_jsonable(await Chats.get_pinned_chats_by_user_id(user.id))


async def _resolve_chats(request: Request, user):
    # Page 1 of the sidebar chat list — matches the default surfaced by
    # /api/v1/chats/?page=1 (limit=60). Pinned/folders are separate components.
    limit = 60
    return _to_jsonable(
        await Chats.get_chat_title_id_list_by_user_id(
            user.id,
            include_folders=False,
            include_pinned=False,
            skip=0,
            limit=limit,
        )
    )


async def _resolve_channels(request: Request, user):
    return _to_jsonable(await Channels.get_channels_by_user_id(user.id))


_RESOLVERS = {
    "config": _resolve_config,
    "user": _resolve_user,
    "settings": _resolve_settings,
    "models": _resolve_models,
    "banners": _resolve_banners,
    "tools": _resolve_tools,
    "folders": _resolve_folders,
    "tags": _resolve_tags,
    "pinned": _resolve_pinned,
    "chats": _resolve_chats,
    "channels": _resolve_channels,
}


async def _safe_resolve(name, request, user):
    try:
        return name, await _RESOLVERS[name](request, user)
    except Exception as e:
        # Conservative: keep the rest of the bundle usable. Surface the failure
        # inline so the client can decide whether to retry the component alone.
        log.exception(f"bootstrap component '{name}' failed: {e}")
        return name, {"error": str(e)}


@router.get("/")
@router.get("")
async def get_bootstrap(
    request: Request,
    include: Optional[str] = None,
    user=Depends(get_verified_user),
):
    if include:
        requested = [name.strip() for name in include.split(",") if name.strip()]
        requested = [name for name in requested if name in _RESOLVERS]
    else:
        requested = list(_ALLOWED_INCLUDES)

    seen = set()
    ordered = []
    for name in requested:
        if name not in seen:
            seen.add(name)
            ordered.append(name)

    results = await asyncio.gather(
        *(_safe_resolve(name, request, user) for name in ordered)
    )

    components = {name: data for name, data in results}
    components_etags = {name: _etag(data) for name, data in components.items()}
    bundle_etag = _bundle_etag(components_etags)

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match.strip('"') == bundle_etag:
        return Response(
            status_code=304,
            headers={
                "ETag": f'"{bundle_etag}"',
                "Cache-Control": "private, max-age=30",
            },
        )

    return JSONResponse(
        content={
            "components": components,
            "components_etags": components_etags,
            "bundle_etag": bundle_etag,
        },
        headers={
            "ETag": f'"{bundle_etag}"',
            "Cache-Control": "private, max-age=30",
        },
    )
