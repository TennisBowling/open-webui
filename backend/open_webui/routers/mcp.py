import logging
from typing import Optional
from urllib.parse import urlparse, urljoin

import aiohttp

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from open_webui.constants import ERROR_MESSAGES
from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.mcp import (
    MCPConnectionForm,
    MCPConnectionModel,
    MCPConnectionWithSecrets,
    MCPConnections,
)
from open_webui.utils.auth import get_verified_user
from open_webui.utils.access_control import has_permission
from open_webui.utils.mcp.client import MCPClient
from open_webui.utils.mcp.connections import (
    STDIO_TEMPLATES,
    build_personal_mcp_connect_kwargs,
    tool_allowed_by_policy,
)
from open_webui.utils.mcp.oauth import (
    MCPOAuthError,
    build_authorization_url,
    create_pkce,
    create_state,
    discover_from_challenge,
    exchange_code,
    register_client,
    token_expires_at,
    validate_public_url,
)


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

router = APIRouter()
oauth_router = APIRouter()


class MCPConnectionUpdateForm(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    transport: Optional[str] = None
    url: Optional[str] = None
    command: Optional[str] = None
    args: Optional[list[str]] = None
    cwd: Optional[str] = None
    auth_type: Optional[str] = None
    key: Optional[str] = None
    headers: Optional[list[dict[str, str]]] = None
    env: Optional[dict[str, str]] = None
    policy: Optional[dict] = None
    tool_filters: Optional[dict] = None
    meta: Optional[dict] = None
    enabled: Optional[bool] = None


class MCPDiscoverForm(BaseModel):
    url: str


def _base_url(request: Request) -> str:
    return str(request.app.state.config.WEBUI_URL or request.base_url).rstrip("/")


def _allowed(user, request: Request, key: str) -> bool:
    return user.role == "admin" or has_permission(
        user.id, key, request.app.state.config.USER_PERMISSIONS
    )


def _validate_form(form: MCPConnectionForm, user, request: Request) -> None:
    if form.id and (":" in form.id or "|" in form.id):
        raise HTTPException(status_code=400, detail='MCP connection id cannot contain ":" or "|"')
    if form.transport not in {"remote_http", "remote_sse", "stdio"}:
        raise HTTPException(status_code=400, detail="Unsupported MCP transport")
    if form.auth_type not in {"none", "oauth_2.1", "bearer", "headers"}:
        raise HTTPException(status_code=400, detail="Unsupported MCP auth_type")
    if form.transport in {"remote_http", "remote_sse"} and not form.url:
        raise HTTPException(status_code=400, detail="Remote MCP connections require a URL")
    if form.transport in {"remote_http", "remote_sse"} and not _allowed(
        user, request, "features.mcp_remote_custom"
    ):
        raise HTTPException(status_code=403, detail="Remote MCP connections are disabled")
    if form.auth_type in {"bearer", "headers"} and not _allowed(
        user, request, "features.mcp_static_secrets"
    ):
        raise HTTPException(status_code=403, detail="Static MCP secrets are disabled")
    if form.transport == "stdio":
        template = (form.meta or {}).get("template")
        if template and template not in STDIO_TEMPLATES:
            raise HTTPException(status_code=400, detail="Unknown stdio MCP template")
        if template and not _allowed(user, request, "features.mcp_stdio_templates"):
            raise HTTPException(status_code=403, detail="Stdio MCP templates are disabled")
        if not template and not _allowed(user, request, "features.mcp_stdio_custom"):
            raise HTTPException(status_code=403, detail="Custom stdio MCP commands are admin-only")
        if not template and not form.command:
            raise HTTPException(status_code=400, detail="Custom stdio MCP requires command")


async def _start_oauth(request: Request, connection: MCPConnectionWithSecrets) -> dict:
    if connection.transport not in {"remote_http", "remote_sse"} or not connection.url:
        raise HTTPException(status_code=400, detail="OAuth is only supported for remote MCP URLs")

    allow_localhost = bool((connection.policy or {}).get("allow_localhost_oauth"))
    try:
        resource_metadata, auth_metadata = await discover_from_challenge(
            connection.url, allow_localhost=allow_localhost
        )
        redirect_uri = f"{_base_url(request)}/oauth/mcp/{connection.id}/callback"
        oauth = connection.oauth or {}
        client_info = oauth.get("client_info")
        if not client_info:
            client_info = await register_client(
                auth_metadata,
                redirect_uri,
                client_name="Open WebUI",
                allow_localhost=allow_localhost,
            )
        code_verifier, code_challenge = create_pkce()
        state = create_state()
        oauth = {
            **oauth,
            "resource_metadata": resource_metadata,
            "auth_metadata": auth_metadata,
            "client_info": client_info,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_verifier": code_verifier,
        }
        MCPConnections.update_oauth_by_id_and_user_id(connection.id, connection.user_id, oauth)
        authorization_url = build_authorization_url(
            auth_metadata,
            client_info,
            resource_metadata,
            redirect_uri,
            state,
            code_challenge,
        )
        return {"status": True, "authorization_url": authorization_url}
    except MCPOAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/templates")
async def get_mcp_templates(user=Depends(get_verified_user)):
    return {
        key: {k: v for k, v in template.items() if k != "node_stdio_sanitize"}
        for key, template in STDIO_TEMPLATES.items()
    }


@router.post("/discover")
async def discover_mcp_url(form_data: MCPDiscoverForm, user=Depends(get_verified_user)):
    parsed = urlparse(form_data.url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid URL")
    discover_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/.well-known/mcp.json")
    try:
        validate_public_url(discover_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(discover_url) as response:
                if response.status != 200:
                    raise HTTPException(status_code=404, detail="No MCP discovery document found")
                data = await response.json()
                return {
                    "name": data.get("name"),
                    "description": data.get("description"),
                    "icon": data.get("icon"),
                    "endpoint": data.get("endpoint"),
                }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"MCP discovery failed: {exc}")


@router.get("/connections", response_model=list[MCPConnectionModel])
async def get_mcp_connections(user=Depends(get_verified_user)):
    connections = MCPConnections.get_connections_by_user_id(user.id, include_secrets=True)
    result = []
    for connection in connections:
        item = connection.model_dump(exclude={"key", "headers", "env", "oauth"})
        if connection.auth_type == "oauth_2.1":
            item["authenticated"] = bool((connection.oauth or {}).get("tokens", {}).get("access_token"))
        result.append(item)
    return result


@router.post("/connections", response_model=MCPConnectionModel)
async def create_mcp_connection(request: Request, form_data: MCPConnectionForm, user=Depends(get_verified_user)):
    _validate_form(form_data, user, request)
    connection = MCPConnections.insert_new_connection(user.id, form_data)
    if not connection:
        raise HTTPException(status_code=400, detail="Failed to create MCP connection")
    return connection


@router.patch("/connections/{connection_id}", response_model=MCPConnectionModel)
async def update_mcp_connection(connection_id: str, request: Request, form_data: MCPConnectionUpdateForm, user=Depends(get_verified_user)):
    existing = MCPConnections.get_connection_by_id_and_user_id(connection_id, user.id)
    if not existing:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    updated = form_data.model_dump(exclude_unset=True)
    merged = MCPConnectionForm(**{**existing.model_dump(), **updated, "id": connection_id, "name": updated.get("name", existing.name)})
    _validate_form(merged, user, request)
    connection = MCPConnections.update_connection_by_id_and_user_id(connection_id, user.id, updated)
    if not connection:
        raise HTTPException(status_code=400, detail="Failed to update MCP connection")
    return connection


@router.delete("/connections/{connection_id}")
async def delete_mcp_connection(connection_id: str, user=Depends(get_verified_user)):
    return {"status": MCPConnections.delete_connection_by_id_and_user_id(connection_id, user.id)}


@router.post("/connections/{connection_id}/oauth/start")
async def start_mcp_oauth(connection_id: str, request: Request, user=Depends(get_verified_user)):
    connection = MCPConnections.get_connection_by_id_and_user_id(
        connection_id, user.id, include_secrets=True
    )
    if not connection:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    return await _start_oauth(request, connection)  # type: ignore[arg-type]


@router.post("/connections/{connection_id}/oauth/disconnect")
async def disconnect_mcp_oauth(connection_id: str, user=Depends(get_verified_user)):
    connection = MCPConnections.update_oauth_by_id_and_user_id(connection_id, user.id, {})
    if not connection:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    return {"status": True}


async def _list_specs(connection: MCPConnectionWithSecrets, user=None, metadata: Optional[dict] = None):
    client = MCPClient()
    try:
        connect_kwargs = await build_personal_mcp_connect_kwargs(
            connection, user=user, metadata=metadata
        )
        await client.connect(**connect_kwargs)
        specs = await client.list_tool_specs()
        return [spec for spec in specs or [] if tool_allowed_by_policy(spec, connection)]
    finally:
        await client.disconnect()


@router.get("/connections/{connection_id}/tools")
async def get_mcp_connection_tools(connection_id: str, user=Depends(get_verified_user)):
    connection = MCPConnections.get_connection_by_id_and_user_id(
        connection_id, user.id, include_secrets=True
    )
    if not connection:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    try:
        return {"status": True, "specs": await _list_specs(connection, user=user)}  # type: ignore[arg-type]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")


@router.post("/connections/{connection_id}/verify")
async def verify_mcp_connection(connection_id: str, request: Request, user=Depends(get_verified_user)):
    connection = MCPConnections.get_connection_by_id_and_user_id(
        connection_id, user.id, include_secrets=True
    )
    if not connection:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    if connection.auth_type == "oauth_2.1" and not (connection.oauth or {}).get("tokens"):
        return {"status": True, "auth_required": True, **(await _start_oauth(request, connection))}  # type: ignore[arg-type]
    try:
        return {"status": True, "specs": await _list_specs(connection, user=user)}  # type: ignore[arg-type]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")


@oauth_router.get("/{connection_id}/callback")
async def mcp_oauth_callback(connection_id: str, request: Request):
    connection = MCPConnections.get_connection_by_id(connection_id, include_secrets=True)
    if not connection:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    oauth = connection.oauth or {}
    if request.query_params.get("error"):
        return RedirectResponse(
            url=f"{_base_url(request)}/?error={request.query_params.get('error')}"
        )
    if request.query_params.get("state") != oauth.get("state"):
        raise HTTPException(status_code=403, detail="Invalid OAuth state")
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")
    try:
        tokens = await exchange_code(
            oauth,
            code,
            oauth.get("redirect_uri") or f"{_base_url(request)}/oauth/mcp/{connection_id}/callback",
            allow_localhost=bool((connection.policy or {}).get("allow_localhost_oauth")),
        )
        oauth["tokens"] = {**tokens, "expires_at": token_expires_at(tokens)}
        oauth.pop("state", None)
        oauth.pop("code_verifier", None)
        MCPConnections.update_oauth_by_id_and_user_id(connection.id, connection.user_id, oauth)
    except Exception as exc:
        log.exception("MCP OAuth callback failed")
        return RedirectResponse(url=f"{_base_url(request)}/?error={type(exc).__name__}")
    return RedirectResponse(url=_base_url(request))
