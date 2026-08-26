import asyncio
import datetime
import hashlib
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from open_webui.env import (
    SRC_LOG_LEVELS,
    WEBUI_AUTH_COOKIE_SAME_SITE,
    WEBUI_AUTH_COOKIE_SECURE,
)
from open_webui.utils.auth import (
    get_verified_user,
    get_http_authorization_cred,
    decode_token,
)
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
    "subscription_usage",
)


def _etag(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _bundle_etag(component_etags: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(component_etags.items())).encode()
    ).hexdigest()[:16]


def _parse_bootstrap_etags(raw: Optional[str]) -> dict[str, str]:
    # Contract 1: the client's "x-bootstrap-etags" header is a compact JSON
    # object mapping component name -> the etag it already has cached. Any
    # absence/parse failure/wrong shape means "no per-component revalidation",
    # so callers fall back to today's full-body behaviour.
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        k: v for k, v in parsed.items() if isinstance(k, str) and isinstance(v, str)
    }


def _build_bootstrap_body(
    components: dict,
    components_etags: dict[str, str],
    bundle_etag: str,
    client_etags: dict[str, str],
) -> dict:
    # Contract 1: per-component revalidation. When the client sent a parseable
    # x-bootstrap-etags map, omit the components whose etag matches what it
    # already has cached and list their names under "unchanged" (the client
    # fills those from localStorage). The full "components_etags" map and
    # "bundle_etag" are always returned. If the client map is empty OR nothing
    # matched, the body is exactly today's (full "components", no "unchanged").
    body = {
        "components_etags": components_etags,
        "bundle_etag": bundle_etag,
    }
    if client_etags:
        unchanged = [
            name
            for name, etag in components_etags.items()
            if client_etags.get(name) == etag
        ]
        if unchanged:
            unchanged_set = set(unchanged)
            body["components"] = {
                name: data
                for name, data in components.items()
                if name not in unchanged_set
            }
            body["unchanged"] = unchanged
            return body
    body["components"] = components
    return body


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

    body = _unwrap_response(await get_models(request, user=user))
    # The handler wraps the list as {"data": [...]}. The client stores the bare
    # Model[] array (the getModels() shape consumed by consumeBootstrap('models')
    # in (app)/+layout.svelte), so unwrap the envelope here.
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


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
    # Same trimmed projection as GET /api/v1/chats/all/tags: list consumers only
    # read id+name, and on tag-heavy accounts the full rows dominate the bundle.
    tags = await Tags.get_tags_by_user_id(user.id)
    return [{"id": tag.id, "name": tag.name} for tag in tags]


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


async def _resolve_subscription_usage(request: Request, user):
    # In-memory snapshot maintained by the poller — synchronous, and stable
    # per underlying provider state (no fetch timestamps), so it only perturbs
    # the bundle etag when the provider's usage actually moved. If the
    # snapshot is stale (poller idles with no sessions connected) this kicks a
    # background refresh; a resulting change reaches the client via the
    # subscription-usage:update push and the mount-time /api/usage/groups
    # fetch, not this response.
    from open_webui.utils.subscription_usage import (
        get_subscription_usage_state,
        kick_refresh_if_stale,
    )

    kick_refresh_if_stale()
    return get_subscription_usage_state()


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
    "subscription_usage": _resolve_subscription_usage,
}


def _renew_token_cookie(request: Request, response: Response) -> None:
    # <img> tags (e.g. model profile images) authenticate via the HttpOnly
    # `token` cookie only — they can't send an Authorization header. That
    # cookie used to be refreshed by GET /api/v1/auths/ (get_session_user),
    # but the app now boots via this endpoint instead, and bootstrap is the
    # only guaranteed periodic authenticated round-trip. Without renewing the
    # cookie here it silently expires (esp. on iOS PWAs) and every avatar
    # <img> starts 401ing while the rest of the app keeps working off the
    # localStorage bearer token. Mirrors get_session_user's set_cookie exactly.
    try:
        auth_header = request.headers.get("Authorization")
        auth_token = get_http_authorization_cred(auth_header)
        token = auth_token.credentials if auth_token is not None else None
        if token is None:
            token = request.cookies.get("token")
        if not token:
            return

        data = decode_token(token)
        if not data:
            return

        expires_at = data.get("exp")
        if expires_at is not None and int(time.time()) > expires_at:
            return

        response.set_cookie(
            key="token",
            value=token,
            expires=(
                datetime.datetime.fromtimestamp(expires_at, datetime.timezone.utc)
                if expires_at
                else None
            ),
            httponly=True,
            samesite=WEBUI_AUTH_COOKIE_SAME_SITE,
            secure=WEBUI_AUTH_COOKIE_SECURE,
        )
    except Exception:
        # Never let cookie renewal break bootstrap.
        pass


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
        not_modified = Response(
            status_code=304,
            headers={
                "ETag": f'"{bundle_etag}"',
                "Cache-Control": "private, max-age=30",
            },
        )
        # 304 is the common case for a returning client — exactly when a
        # dying cookie most needs renewing — so it must be covered too.
        _renew_token_cookie(request, not_modified)
        return not_modified

    headers = {
        "ETag": f'"{bundle_etag}"',
        "Cache-Control": "private, max-age=30",
    }

    # Contract 1: per-component revalidation. An absent/unparseable header yields
    # an empty client map -> full body (exactly today's behaviour).
    client_etags = _parse_bootstrap_etags(request.headers.get("x-bootstrap-etags"))
    body = _build_bootstrap_body(
        components, components_etags, bundle_etag, client_etags
    )
    response = JSONResponse(content=body, headers=headers)
    _renew_token_cookie(request, response)
    return response
