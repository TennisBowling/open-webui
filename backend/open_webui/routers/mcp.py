import logging
import secrets
from typing import Optional
from urllib.parse import urlparse, urljoin

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from open_webui.constants import ERROR_MESSAGES
from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.mcp import (
    MCPConnectionForm,
    MCPConnectionModel,
    MCPConnectionWithHeaders,
    MCPConnectionWithSecrets,
    MCPConnections,
)
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.access_control import has_permission
from open_webui.utils.mcp.client import MCPClient
from open_webui.utils.mcp.connections import (
    STDIO_TEMPLATES,
    build_personal_mcp_connect_kwargs,
    discard_refresh_lock,
    tool_allowed_by_policy,
)
from open_webui.utils.mcp.oauth import (
    MCPOAuthError,
    MCPOAuthHTTPError,
    MCPOAuthReauthRequired,
    build_authorization_url,
    create_pkce,
    create_state,
    discover_from_challenge,
    exchange_code,
    fetch_json,
    register_client,
    token_expires_at,
    validate_public_url,
)
from open_webui.utils.mcp.persistent import personal_mcp_process_key


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


def _safe_return_path(return_to) -> Optional[str]:
    """Accept only a same-origin relative path for the post-OAuth redirect.

    Prevents the callback from being turned into an open redirect: must start
    with a single '/', not '//' (protocol-relative) and contain no scheme.
    """
    if not return_to or not isinstance(return_to, str):
        return None
    if not return_to.startswith("/") or return_to.startswith("//"):
        return None
    if "://" in return_to or "\\" in return_to:
        return None
    return return_to


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
    # allow_localhost_oauth relaxes the SSRF guard (permits loopback OAuth
    # targets), so it is admin-only — otherwise any user could self-grant it.
    if (form.policy or {}).get("allow_localhost_oauth") and user.role != "admin":
        raise HTTPException(
            status_code=403, detail="allow_localhost_oauth is restricted to admins"
        )
    if form.transport in {"remote_http", "remote_sse"} and not form.url:
        raise HTTPException(status_code=400, detail="Remote MCP connections require a URL")
    if form.transport in {"remote_http", "remote_sse"} and not _allowed(
        user, request, "features.mcp_remote_custom"
    ):
        raise HTTPException(status_code=403, detail="Remote MCP connections are disabled")
    if (form.auth_type in {"bearer", "headers"} or form.headers) and not _allowed(
        user, request, "features.mcp_static_secrets"
    ):
        raise HTTPException(status_code=403, detail="Static MCP secrets are disabled")
    if form.transport == "stdio":
        template = (form.meta or {}).get("template")
        if template and template not in STDIO_TEMPLATES:
            raise HTTPException(status_code=400, detail="Unknown stdio MCP template")
        if template and (form.command or form.args):
            # A template's command/args are admin-curated and authoritative;
            # carrying a user command/args alongside a template is the stdio
            # privilege-escalation vector (arbitrary host command execution), so
            # reject it outright rather than silently ignore.
            raise HTTPException(
                status_code=400,
                detail="A template stdio connection cannot define its own command or args",
            )
        if template and not _allowed(user, request, "features.mcp_stdio_templates"):
            raise HTTPException(status_code=403, detail="Stdio MCP templates are disabled")
        if not template and not _allowed(user, request, "features.mcp_stdio_custom"):
            raise HTTPException(status_code=403, detail="Custom stdio MCP commands are admin-only")
        if not template and not form.command:
            raise HTTPException(status_code=400, detail="Custom stdio MCP requires command")


async def _start_oauth(request: Request, connection: MCPConnectionWithSecrets, return_to: Optional[str] = None) -> dict:
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
                client_name=request.app.state.WEBUI_NAME,
                allow_localhost=allow_localhost,
            )
        code_verifier, code_challenge = create_pkce()
        state = create_state()
        # Persist ONLY the freshly-minted auth-flow fields, under the row lock,
        # merged into a re-read of the blob. Never carry the stale snapshot's
        # `tokens` forward: a concurrent agentic-turn refresh may have rotated the
        # (Notion) refresh token during the network round-trips above, and writing
        # the whole snapshot back unlocked would replace the rotated token with the
        # dead one and revoke the grant (audit A2).
        set_fields = {
            "resource_metadata": resource_metadata,
            "auth_metadata": auth_metadata,
            "client_info": client_info,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_verifier": code_verifier,
        }
        drop_fields = []
        safe_return = _safe_return_path(return_to)
        if safe_return:
            set_fields["return_to"] = safe_return
        else:
            drop_fields.append("return_to")
        await MCPConnections.merge_oauth_fields_by_id_and_user_id(
            connection.id, connection.user_id, set_fields, drop_fields
        )
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
        await validate_public_url(discover_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        # Route through fetch_json: it follows redirects MANUALLY and re-validates
        # every hop (including literal-IP Location targets, which aiohttp's guarded
        # resolver is bypassed for), closing the redirect-to-internal SSRF hole.
        data = await fetch_json(discover_url)
    except MCPOAuthHTTPError:
        raise HTTPException(status_code=404, detail="No MCP discovery document found")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"MCP discovery failed: {exc}")
    if not isinstance(data, dict):
        raise HTTPException(status_code=404, detail="No MCP discovery document found")
    return {
        "name": data.get("name"),
        "description": data.get("description"),
        "icon": data.get("icon"),
        "endpoint": data.get("endpoint"),
    }


@router.get("/connections", response_model=list[MCPConnectionWithHeaders])
async def get_mcp_connections(user=Depends(get_verified_user)):
    connections = await MCPConnections.get_connections_by_user_id(user.id, include_secrets=True)
    result = []
    for connection in connections:
        # Owners need their custom header/request-metadata and env entries back
        # so the edit form can show and modify them. Other secrets stay write-only.
        item = connection.model_dump(exclude={"key", "oauth"})
        if connection.auth_type == "oauth_2.1":
            item["authenticated"] = bool((connection.oauth or {}).get("tokens", {}).get("access_token"))
        result.append(item)
    return result


@router.post("/connections", response_model=MCPConnectionModel)
async def create_mcp_connection(request: Request, form_data: MCPConnectionForm, user=Depends(get_verified_user)):
    _validate_form(form_data, user, request)
    connection = await MCPConnections.insert_new_connection(user.id, form_data)
    if not connection:
        raise HTTPException(status_code=400, detail="Failed to create MCP connection")
    if connection.transport == "stdio" and connection.enabled:
        stored = await MCPConnections.get_connection_by_id_and_user_id(
            connection.id, user.id, include_secrets=True
        )
        try:
            kwargs = await build_personal_mcp_connect_kwargs(stored, user=user)
            await request.app.state.persistent_mcp.ensure(
                personal_mcp_process_key(user.id, connection.id), kwargs
            )
        except Exception:
            log.exception("Failed to eagerly start MCP connection %s", connection.id)
    return connection


@router.patch("/connections/{connection_id}", response_model=MCPConnectionModel)
async def update_mcp_connection(connection_id: str, request: Request, form_data: MCPConnectionUpdateForm, user=Depends(get_verified_user)):
    existing = await MCPConnections.get_connection_by_id_and_user_id(connection_id, user.id)
    if not existing:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    updated = form_data.model_dump(exclude_unset=True)
    # `enabled` is governed ONLY by the admin enable/disable endpoints (the
    # governance kill switch). A connection's owner must not be able to re-enable
    # a connection an admin disabled, so drop any user-supplied `enabled` here —
    # there is a single shared column, so the user PATCH cannot be allowed to
    # touch it.
    updated.pop("enabled", None)
    merged = MCPConnectionForm(**{**existing.model_dump(), **updated, "id": connection_id, "name": updated.get("name", existing.name)})
    _validate_form(merged, user, request)
    # Clear the stored OAuth grant when the connection is retargeted (URL or
    # transport change) OR when auth_type changes at all. The access token is
    # audience-bound (RFC 8707) to the old resource and must never be sent to a
    # different server. Gating only on the CURRENT auth_type let an owner launder
    # a stored token to an attacker URL: flip auth_type to "none" (token survives,
    # url unchanged), then back to "oauth_2.1" with a new URL (the clear was
    # skipped because existing.auth_type was now "none"). Clearing an absent grant
    # is a harmless no-op.
    if (
        ("url" in updated and updated.get("url") != existing.url)
        or ("transport" in updated and updated.get("transport") != existing.transport)
        or ("auth_type" in updated and updated.get("auth_type") != existing.auth_type)
    ):
        updated["oauth"] = {}
        discard_refresh_lock(connection_id)
    connection = await MCPConnections.update_connection_by_id_and_user_id(connection_id, user.id, updated)
    if not connection:
        raise HTTPException(status_code=400, detail="Failed to update MCP connection")
    key = personal_mcp_process_key(user.id, connection_id)
    if connection.transport == "stdio" and connection.enabled:
        stored = await MCPConnections.get_connection_by_id_and_user_id(
            connection_id, user.id, include_secrets=True
        )
        kwargs = await build_personal_mcp_connect_kwargs(stored, user=user)
        await request.app.state.persistent_mcp.restart(key, kwargs)
    else:
        await request.app.state.persistent_mcp.stop(key)
    return connection


@router.delete("/connections/{connection_id}")
async def delete_mcp_connection(connection_id: str, request: Request, user=Depends(get_verified_user)):
    await request.app.state.persistent_mcp.stop(
        personal_mcp_process_key(user.id, connection_id)
    )
    status = await MCPConnections.delete_connection_by_id_and_user_id(connection_id, user.id)
    if status:
        discard_refresh_lock(connection_id)
    return {"status": status}


# --- Admin governance over ALL users' personal MCP connections ----------------


@router.get("/admin/connections", response_model=list[MCPConnectionModel])
async def admin_list_mcp_connections(user=Depends(get_admin_user)):
    # Metadata + authenticated flag only; never returns secrets/tokens.
    return await MCPConnections.get_all_connections()


@router.post("/admin/connections/{connection_id}/disable")
async def admin_disable_mcp_connection(connection_id: str, request: Request, user=Depends(get_admin_user)):
    conn = await MCPConnections.set_enabled_by_id(connection_id, False)
    if not conn:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    discard_refresh_lock(connection_id)
    await request.app.state.persistent_mcp.stop(
        personal_mcp_process_key(conn.user_id, connection_id)
    )
    return {"status": True}


@router.post("/admin/connections/{connection_id}/enable")
async def admin_enable_mcp_connection(connection_id: str, request: Request, user=Depends(get_admin_user)):
    conn = await MCPConnections.set_enabled_by_id(connection_id, True)
    if not conn:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    if conn.transport == "stdio":
        stored = await MCPConnections.get_connection_by_id(connection_id, include_secrets=True)
        kwargs = await build_personal_mcp_connect_kwargs(stored)
        await request.app.state.persistent_mcp.ensure(
            personal_mcp_process_key(conn.user_id, connection_id), kwargs
        )
    return {"status": True}


@router.post("/admin/connections/{connection_id}/revoke")
async def admin_revoke_mcp_oauth(connection_id: str, user=Depends(get_admin_user)):
    # Force-clear a user's OAuth grant (kill switch) without deleting the config.
    ok = await MCPConnections.admin_clear_oauth_tokens_by_id(connection_id)
    if not ok:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    discard_refresh_lock(connection_id)
    return {"status": True}


@router.delete("/admin/connections/{connection_id}")
async def admin_delete_mcp_connection(connection_id: str, request: Request, user=Depends(get_admin_user)):
    existing = await MCPConnections.get_connection_by_id(connection_id)
    if existing:
        await request.app.state.persistent_mcp.stop(
            personal_mcp_process_key(existing.user_id, connection_id)
        )
    ok = await MCPConnections.delete_by_id(connection_id)
    if ok:
        discard_refresh_lock(connection_id)
    return {"status": ok}


@router.post("/connections/{connection_id}/restart")
async def restart_mcp_connection(connection_id: str, request: Request, user=Depends(get_verified_user)):
    connection = await MCPConnections.get_connection_by_id_and_user_id(
        connection_id, user.id, include_secrets=True
    )
    if not connection:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    if connection.transport != "stdio":
        raise HTTPException(status_code=400, detail="Only local stdio MCP servers can be restarted")
    if not connection.enabled:
        raise HTTPException(status_code=403, detail="MCP connection is disabled")
    try:
        kwargs = await build_personal_mcp_connect_kwargs(connection, user=user)
        client = await request.app.state.persistent_mcp.restart(
            personal_mcp_process_key(user.id, connection_id), kwargs
        )
        specs = await client.list_tool_specs()
        return {"status": True, "specs": specs or []}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")


@router.post("/admin/connections/{connection_id}/restart")
async def admin_restart_mcp_connection(connection_id: str, request: Request, user=Depends(get_admin_user)):
    connection = await MCPConnections.get_connection_by_id(connection_id, include_secrets=True)
    if not connection:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    if connection.transport != "stdio":
        raise HTTPException(status_code=400, detail="Only local stdio MCP servers can be restarted")
    if not connection.enabled:
        raise HTTPException(status_code=403, detail="MCP connection is disabled")
    try:
        kwargs = await build_personal_mcp_connect_kwargs(connection)
        client = await request.app.state.persistent_mcp.restart(
            personal_mcp_process_key(connection.user_id, connection_id), kwargs
        )
        specs = await client.list_tool_specs()
        return {"status": True, "specs": specs or []}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")


@router.post("/connections/{connection_id}/oauth/start")
async def start_mcp_oauth(connection_id: str, request: Request, return_to: Optional[str] = None, user=Depends(get_verified_user)):
    connection = await MCPConnections.get_connection_by_id_and_user_id(
        connection_id, user.id, include_secrets=True
    )
    if not connection:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    if not connection.enabled:
        raise HTTPException(status_code=403, detail="MCP connection is disabled")
    return await _start_oauth(request, connection, return_to=return_to)  # type: ignore[arg-type]


@router.post("/connections/{connection_id}/oauth/disconnect")
async def disconnect_mcp_oauth(connection_id: str, user=Depends(get_verified_user)):
    existing = await MCPConnections.get_connection_by_id_and_user_id(connection_id, user.id)
    if not existing:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    # Clear tokens/state but PRESERVE client_info so a later reconnect can reuse
    # the dynamic client registration instead of churning a new one each time.
    await MCPConnections.clear_oauth_tokens_by_id_and_user_id(connection_id, user.id)
    discard_refresh_lock(connection_id)
    return {"status": True}


async def _list_specs(
    connection: MCPConnectionWithSecrets,
    user=None,
    metadata: Optional[dict] = None,
    apply_filter: bool = True,
    persistent_manager=None,
):
    client = None
    borrowed = False
    try:
        connect_kwargs = await build_personal_mcp_connect_kwargs(
            connection, user=user, metadata=metadata
        )
        if connection.transport == "stdio" and persistent_manager is not None:
            client = await persistent_manager.ensure(
                personal_mcp_process_key(connection.user_id, connection.id),
                connect_kwargs,
            )
            borrowed = True
        else:
            client = MCPClient()
            await client.connect(**connect_kwargs)
        specs = await client.list_tool_specs()
        if not apply_filter:
            # The tool-management UI needs the FULL upstream catalog (incl. tools
            # the user has disabled) so it can render their toggle state.
            return specs or []
        return [spec for spec in specs or [] if tool_allowed_by_policy(spec, connection)]
    finally:
        if client is not None and not borrowed:
            await client.disconnect()


@router.get("/connections/{connection_id}/tools")
async def get_mcp_connection_tools(connection_id: str, request: Request, all: bool = False, user=Depends(get_verified_user)):
    connection = await MCPConnections.get_connection_by_id_and_user_id(
        connection_id, user.id, include_secrets=True
    )
    if not connection:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    if not connection.enabled:
        raise HTTPException(status_code=403, detail="MCP connection is disabled")
    try:
        return {"status": True, "specs": await _list_specs(connection, user=user, apply_filter=not all, persistent_manager=request.app.state.persistent_mcp)}  # type: ignore[arg-type]
    except MCPOAuthReauthRequired:
        # Tokens were cleared by a terminal refresh failure — prompt re-auth.
        connection = await MCPConnections.get_connection_by_id_and_user_id(
            connection_id, user.id, include_secrets=True
        )
        if not connection:
            raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
        return {"status": True, "auth_required": True, **(await _start_oauth(request, connection))}  # type: ignore[arg-type]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")


@router.post("/connections/{connection_id}/verify")
async def verify_mcp_connection(connection_id: str, request: Request, user=Depends(get_verified_user)):
    connection = await MCPConnections.get_connection_by_id_and_user_id(
        connection_id, user.id, include_secrets=True
    )
    if not connection:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    if not connection.enabled:
        raise HTTPException(status_code=403, detail="MCP connection is disabled")
    if connection.auth_type == "oauth_2.1" and not (connection.oauth or {}).get("tokens"):
        return {"status": True, "auth_required": True, **(await _start_oauth(request, connection))}  # type: ignore[arg-type]
    try:
        # Report the policy-hidden count alongside the allowed specs: a server
        # whose tools all lack readOnlyHint annotations passes 0 tools through
        # the write gate, which otherwise reads as "broken server" in the UI.
        specs = await _list_specs(connection, user=user, apply_filter=False, persistent_manager=request.app.state.persistent_mcp)  # type: ignore[arg-type]
        allowed = [spec for spec in specs if tool_allowed_by_policy(spec, connection)]
        return {
            "status": True,
            "specs": allowed,
            "hidden_by_policy": len(specs) - len(allowed),
        }
    except MCPOAuthReauthRequired:
        connection = await MCPConnections.get_connection_by_id_and_user_id(
            connection_id, user.id, include_secrets=True
        )
        if not connection:
            raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
        return {"status": True, "auth_required": True, **(await _start_oauth(request, connection))}  # type: ignore[arg-type]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")


@oauth_router.get("/{connection_id}/callback")
async def mcp_oauth_callback(connection_id: str, request: Request):
    connection = await MCPConnections.get_connection_by_id(connection_id, include_secrets=True)
    if not connection:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    oauth = connection.oauth or {}
    return_path = _safe_return_path(oauth.get("return_to")) or "/"
    # The return path may already carry a query string, so pick the right
    # separator to avoid a malformed double-'?' URL.
    sep = "&" if "?" in return_path else "?"
    if request.query_params.get("error"):
        return RedirectResponse(
            url=f"{_base_url(request)}{return_path}{sep}mcp_oauth_error={request.query_params.get('error')}"
        )
    expected_state = oauth.get("state")
    provided_state = request.query_params.get("state")
    # Require a non-empty state on both sides and compare in constant time, so a
    # callback that arrives before any /oauth/start (state absent) can't pass via
    # a None == None comparison.
    if (
        not expected_state
        or not provided_state
        or not secrets.compare_digest(str(provided_state), str(expected_state))
    ):
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
        # Persist through the row-locked read-modify-write path (like every other
        # oauth writer), NOT a blind full-blob UPDATE of the pre-exchange snapshot:
        # exchange_code yields the loop, so a concurrent disconnect / refresh /
        # second start could commit during it and be silently clobbered. Merging
        # only the token set (and dropping the transient auth-flow fields) into a
        # SELECT..FOR UPDATE re-read preserves whatever a concurrent writer committed.
        saved = await MCPConnections.merge_oauth_fields_by_id_and_user_id(
            connection.id,
            connection.user_id,
            {"tokens": {**tokens, "expires_at": token_expires_at(tokens)}},
            ["state", "code_verifier", "return_to"],
        )
        if not saved:
            # Persisting the freshly-exchanged tokens IS the point of the
            # callback, and the single-use code is already spent at the provider.
            # update_connection_by_id_and_user_id swallows DB errors and returns
            # None, so an unchecked write would redirect "connected" while the
            # tokens were silently lost. Report the failure instead of faking
            # success so the UI doesn't show a connected-but-tokenless state.
            log.error(
                "MCP OAuth callback: token persist failed for connection %s",
                connection.id,
            )
            return RedirectResponse(
                url=f"{_base_url(request)}{return_path}{sep}mcp_oauth_error=persist_failed"
            )
    except Exception as exc:
        log.exception("MCP OAuth callback failed")
        return RedirectResponse(
            url=f"{_base_url(request)}{return_path}{sep}mcp_oauth_error={type(exc).__name__}"
        )
    return RedirectResponse(url=f"{_base_url(request)}{return_path}{sep}mcp_oauth=connected")
