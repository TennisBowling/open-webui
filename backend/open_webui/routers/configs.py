import asyncio
import logging
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, ConfigDict
import aiohttp

from typing import Optional

from open_webui.models.chats import Chats
from open_webui.utils import chat_embedder as ce
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.config import get_config_async, save_config_async
from open_webui.config import BannerModel

from open_webui.utils.tools import (
    get_tool_server_data,
    get_tool_server_url,
    set_tool_servers,
)
from open_webui.utils.mcp.client import MCPClient, build_mcp_connect_kwargs

from open_webui.env import SRC_LOG_LEVELS

from open_webui.utils.oauth import (
    get_discovery_urls,
    get_oauth_client_info_with_dynamic_client_registration,
    encrypt_data,
    decrypt_data,
    OAuthClientInformationFull,
)
from mcp.shared.auth import OAuthMetadata

router = APIRouter()

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


############################
# ImportConfig
############################


class ImportConfigForm(BaseModel):
    config: dict


@router.post("/import", response_model=dict)
async def import_config(form_data: ImportConfigForm, user=Depends(get_admin_user)):
    await save_config_async(form_data.config)
    return await get_config_async()


############################
# ExportConfig
############################


@router.get("/export", response_model=dict)
async def export_config(user=Depends(get_admin_user)):
    return await get_config_async()


############################
# Connections Config
############################


class ConnectionsConfigForm(BaseModel):
    ENABLE_DIRECT_CONNECTIONS: bool
    ENABLE_BASE_MODELS_CACHE: bool


@router.get("/connections", response_model=ConnectionsConfigForm)
async def get_connections_config(request: Request, user=Depends(get_admin_user)):
    return {
        "ENABLE_DIRECT_CONNECTIONS": request.app.state.config.ENABLE_DIRECT_CONNECTIONS,
        "ENABLE_BASE_MODELS_CACHE": request.app.state.config.ENABLE_BASE_MODELS_CACHE,
    }


@router.post("/connections", response_model=ConnectionsConfigForm)
async def set_connections_config(
    request: Request,
    form_data: ConnectionsConfigForm,
    user=Depends(get_admin_user),
):
    request.app.state.config.ENABLE_DIRECT_CONNECTIONS = (
        form_data.ENABLE_DIRECT_CONNECTIONS
    )
    request.app.state.config.ENABLE_BASE_MODELS_CACHE = (
        form_data.ENABLE_BASE_MODELS_CACHE
    )

    return {
        "ENABLE_DIRECT_CONNECTIONS": request.app.state.config.ENABLE_DIRECT_CONNECTIONS,
        "ENABLE_BASE_MODELS_CACHE": request.app.state.config.ENABLE_BASE_MODELS_CACHE,
    }


class OAuthClientRegistrationForm(BaseModel):
    url: str
    client_id: str
    client_name: Optional[str] = None


@router.post("/oauth/clients/register")
async def register_oauth_client(
    request: Request,
    form_data: OAuthClientRegistrationForm,
    type: Optional[str] = None,
    user=Depends(get_admin_user),
):
    try:
        oauth_client_id = form_data.client_id
        if type:
            oauth_client_id = f"{type}:{form_data.client_id}"

        oauth_client_info = (
            await get_oauth_client_info_with_dynamic_client_registration(
                request, oauth_client_id, form_data.url
            )
        )
        return {
            "status": True,
            "oauth_client_info": encrypt_data(
                oauth_client_info.model_dump(mode="json")
            ),
        }
    except Exception as e:
        log.debug(f"Failed to register OAuth client: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Failed to register OAuth client",
        )


############################
# ToolServers Config
############################


class ToolServerConnection(BaseModel):
    url: str
    path: str
    type: Optional[str] = "openapi"  # openapi, mcp
    auth_type: Optional[str]
    key: Optional[str]
    config: Optional[dict]
    # Per-tool enable/disable for MCP servers: {"include": [...], "exclude": [...]}.
    # include is an allowlist (active when non-empty); exclude is a denylist.
    tool_filters: Optional[dict] = None

    model_config = ConfigDict(extra="allow")


class ToolServersConfigForm(BaseModel):
    TOOL_SERVER_CONNECTIONS: list[ToolServerConnection]
    # Default max seconds for MCP tool calls. 0 disables the generic cap. The
    # runtime always exempts bash/web_search/web_fetch from this generic timeout.
    MCP_TOOL_CALL_TIMEOUT: Optional[int] = None


@router.get("/tool_servers", response_model=ToolServersConfigForm)
async def get_tool_servers_config(request: Request, user=Depends(get_admin_user)):
    return {
        "TOOL_SERVER_CONNECTIONS": request.app.state.config.TOOL_SERVER_CONNECTIONS,
        "MCP_TOOL_CALL_TIMEOUT": request.app.state.config.MCP_TOOL_CALL_TIMEOUT,
    }


@router.post("/tool_servers", response_model=ToolServersConfigForm)
async def set_tool_servers_config(
    request: Request,
    form_data: ToolServersConfigForm,
    user=Depends(get_admin_user),
):
    from open_webui.utils.mcp.persistent import admin_mcp_process_key

    previous = request.app.state.config.TOOL_SERVER_CONNECTIONS or []
    for connection in previous:
        if connection.get("type") == "mcp" and (connection.get("config") or {}).get("command"):
            server_id = (connection.get("info") or {}).get("id")
            if server_id:
                await request.app.state.persistent_mcp.stop(admin_mcp_process_key(server_id))
    request.app.state.config.TOOL_SERVER_CONNECTIONS = [
        connection.model_dump() for connection in form_data.TOOL_SERVER_CONNECTIONS
    ]
    if form_data.MCP_TOOL_CALL_TIMEOUT is not None:
        request.app.state.config.MCP_TOOL_CALL_TIMEOUT = max(
            0, int(form_data.MCP_TOOL_CALL_TIMEOUT)
        )

    await set_tool_servers(request)

    for connection in request.app.state.config.TOOL_SERVER_CONNECTIONS:
        server_type = connection.get("type", "openapi")
        if server_type == "mcp":
            server_id = connection.get("info", {}).get("id")
            auth_type = connection.get("auth_type", "none")
            if server_id and (connection.get("config") or {}).get("command"):
                try:
                    connect_kwargs = build_mcp_connect_kwargs(
                        connection, bearer_token=None, user=user, metadata=None
                    )
                    await request.app.state.persistent_mcp.ensure(
                        admin_mcp_process_key(server_id), connect_kwargs
                    )
                except Exception:
                    log.exception("Failed to start persistent admin MCP server %s", server_id)
            if auth_type == "oauth_2.1" and server_id:
                try:
                    oauth_client_info = connection.get("info", {}).get(
                        "oauth_client_info", ""
                    )
                    oauth_client_info = decrypt_data(oauth_client_info)

                    request.app.state.oauth_client_manager.add_client(
                        f"{server_type}:{server_id}",
                        OAuthClientInformationFull(**oauth_client_info),
                    )
                except Exception as e:
                    log.debug(f"Failed to add OAuth client for MCP tool server: {e}")
                    continue

    return {
        "TOOL_SERVER_CONNECTIONS": request.app.state.config.TOOL_SERVER_CONNECTIONS,
        "MCP_TOOL_CALL_TIMEOUT": request.app.state.config.MCP_TOOL_CALL_TIMEOUT,
    }


@router.post("/tool_servers/{server_id}/restart")
async def restart_tool_server(server_id: str, request: Request, user=Depends(get_admin_user)):
    from open_webui.utils.mcp.persistent import admin_mcp_process_key

    connection = next(
        (
            item for item in request.app.state.config.TOOL_SERVER_CONNECTIONS
            if item.get("type") == "mcp" and (item.get("info") or {}).get("id") == server_id
        ),
        None,
    )
    if not connection:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if not (connection.get("config") or {}).get("command"):
        raise HTTPException(status_code=400, detail="Only local stdio MCP servers can be restarted")
    try:
        kwargs = build_mcp_connect_kwargs(
            connection, bearer_token=None, user=user, metadata=None
        )
        client = await request.app.state.persistent_mcp.restart(
            admin_mcp_process_key(server_id), kwargs
        )
        return {"status": True, "specs": await client.list_tool_specs() or []}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")


@router.post("/tool_servers/verify")
async def verify_tool_servers_config(
    request: Request, form_data: ToolServerConnection, user=Depends(get_admin_user)
):
    """
    Verify the connection to the tool server.

    For MCP servers this uses the SAME connection path as the chat runtime
    (``build_mcp_connect_kwargs`` + ``MCPClient.connect``) so a successful
    verify is a reliable predictor of runtime, and a failed verify reports
    the actual upstream error instead of a generic "Failed to create MCP
    client" string.
    """
    client = None
    try:
        if form_data.type == "mcp":
            # Resolve a bearer token first; falls back to OAuth 2.1 discovery
            # probe only when no token is available so newly-configured but
            # not-yet-authorized servers can still validate their metadata.
            bearer_token = None
            connection_dict = form_data.model_dump()
            server_id = (connection_dict.get("info") or {}).get("id") or ""

            if form_data.auth_type == "bearer":
                bearer_token = form_data.key
            elif form_data.auth_type == "session":
                bearer_token = request.state.token.credentials
            elif form_data.auth_type == "system_oauth":
                try:
                    if request.cookies.get("oauth_session_id", None):
                        bearer_token = (
                            await request.app.state.oauth_manager.get_oauth_token(
                                user.id,
                                request.cookies.get("oauth_session_id", None),
                            )
                        )
                        if isinstance(bearer_token, dict):
                            bearer_token = bearer_token.get("access_token")
                except Exception:
                    pass
            elif form_data.auth_type == "oauth_2.1":
                # Try to reuse an existing token for this user. If none, fall
                # back to the discovery-document probe so the admin can
                # confirm the server's OAuth metadata is reachable BEFORE
                # they've authorized.
                try:
                    if server_id:
                        oauth_token = (
                            await request.app.state.oauth_client_manager.get_oauth_token(
                                user.id, f"mcp:{server_id}"
                            )
                        )
                        if oauth_token:
                            bearer_token = oauth_token.get("access_token")
                except Exception:
                    bearer_token = None

                if not bearer_token:
                    discovery_urls = get_discovery_urls(form_data.url)
                    last_err = None
                    for discovery_url in discovery_urls:
                        log.debug(
                            f"Trying to fetch OAuth 2.1 discovery document from {discovery_url}"
                        )
                        try:
                            async with aiohttp.ClientSession() as session:
                                async with session.get(
                                    discovery_url
                                ) as oauth_server_metadata_response:
                                    if oauth_server_metadata_response.status == 200:
                                        oauth_server_metadata = (
                                            OAuthMetadata.model_validate(
                                                await oauth_server_metadata_response.json()
                                            )
                                        )
                                        return {
                                            "status": True,
                                            "auth_required": True,
                                            "oauth_server_metadata": oauth_server_metadata.model_dump(
                                                mode="json"
                                            ),
                                        }
                        except Exception as e:
                            last_err = e
                            continue

                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Failed to fetch OAuth 2.1 discovery document from "
                            f"{discovery_urls}"
                            + (f": {type(last_err).__name__}: {last_err}" if last_err else "")
                        ),
                    )

            connect_kwargs = build_mcp_connect_kwargs(
                connection_dict,
                bearer_token=bearer_token,
                user=user,
                metadata=None,
            )

            client = MCPClient()
            await client.connect(**connect_kwargs)
            specs = await client.list_tool_specs()
            return {
                "status": True,
                "specs": specs or [],
            }
        else:  # openapi
            token = None
            if form_data.auth_type == "bearer":
                token = form_data.key
            elif form_data.auth_type == "session":
                token = request.state.token.credentials
            elif form_data.auth_type == "system_oauth":
                try:
                    if request.cookies.get("oauth_session_id", None):
                        token = await request.app.state.oauth_manager.get_oauth_token(
                            user.id,
                            request.cookies.get("oauth_session_id", None),
                        )
                except Exception as e:
                    pass

            url = get_tool_server_url(form_data.url, form_data.path)
            return await get_tool_server_data(token, url)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to verify tool server connection")
        raise HTTPException(
            status_code=400,
            detail=f"{type(e).__name__}: {e}",
        )
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log.exception("MCP verify disconnect failed")


############################
# ContainerConfig
############################


class ContainerConfigForm(BaseModel):
    ENABLE_CONTAINER_WORKSPACE_SYNC: bool
    CONTAINER_DATA_ROOT: str
    CONTAINER_MCP_SERVER_ID: str
    CONTAINER_SYSTEM_PROMPT: str


@router.get("/container", response_model=ContainerConfigForm)
async def get_container_config(request: Request, user=Depends(get_admin_user)):
    return {
        "ENABLE_CONTAINER_WORKSPACE_SYNC": request.app.state.config.ENABLE_CONTAINER_WORKSPACE_SYNC,
        "CONTAINER_DATA_ROOT": request.app.state.config.CONTAINER_DATA_ROOT,
        "CONTAINER_MCP_SERVER_ID": request.app.state.config.CONTAINER_MCP_SERVER_ID,
        "CONTAINER_SYSTEM_PROMPT": request.app.state.config.CONTAINER_SYSTEM_PROMPT,
    }


@router.post("/container", response_model=ContainerConfigForm)
async def set_container_config(
    request: Request, form_data: ContainerConfigForm, user=Depends(get_admin_user)
):
    request.app.state.config.ENABLE_CONTAINER_WORKSPACE_SYNC = (
        form_data.ENABLE_CONTAINER_WORKSPACE_SYNC
    )
    request.app.state.config.CONTAINER_DATA_ROOT = form_data.CONTAINER_DATA_ROOT
    request.app.state.config.CONTAINER_MCP_SERVER_ID = form_data.CONTAINER_MCP_SERVER_ID
    request.app.state.config.CONTAINER_SYSTEM_PROMPT = form_data.CONTAINER_SYSTEM_PROMPT

    return {
        "ENABLE_CONTAINER_WORKSPACE_SYNC": request.app.state.config.ENABLE_CONTAINER_WORKSPACE_SYNC,
        "CONTAINER_DATA_ROOT": request.app.state.config.CONTAINER_DATA_ROOT,
        "CONTAINER_MCP_SERVER_ID": request.app.state.config.CONTAINER_MCP_SERVER_ID,
        "CONTAINER_SYSTEM_PROMPT": request.app.state.config.CONTAINER_SYSTEM_PROMPT,
    }


############################
# SetDefaultModels
############################
class ModelsConfigForm(BaseModel):
    DEFAULT_MODELS: Optional[str]
    MODEL_ORDER_LIST: Optional[list[str]]


@router.get("/models", response_model=ModelsConfigForm)
async def get_models_config(request: Request, user=Depends(get_admin_user)):
    return {
        "DEFAULT_MODELS": request.app.state.config.DEFAULT_MODELS,
        "MODEL_ORDER_LIST": request.app.state.config.MODEL_ORDER_LIST,
    }


@router.post("/models", response_model=ModelsConfigForm)
async def set_models_config(
    request: Request, form_data: ModelsConfigForm, user=Depends(get_admin_user)
):
    request.app.state.config.DEFAULT_MODELS = form_data.DEFAULT_MODELS
    request.app.state.config.MODEL_ORDER_LIST = form_data.MODEL_ORDER_LIST
    return {
        "DEFAULT_MODELS": request.app.state.config.DEFAULT_MODELS,
        "MODEL_ORDER_LIST": request.app.state.config.MODEL_ORDER_LIST,
    }


class PromptSuggestion(BaseModel):
    title: list[str]
    content: str


class SetDefaultSuggestionsForm(BaseModel):
    suggestions: list[PromptSuggestion]


@router.post("/suggestions", response_model=list[PromptSuggestion])
async def set_default_suggestions(
    request: Request,
    form_data: SetDefaultSuggestionsForm,
    user=Depends(get_admin_user),
):
    data = form_data.model_dump()
    request.app.state.config.DEFAULT_PROMPT_SUGGESTIONS = data["suggestions"]
    return request.app.state.config.DEFAULT_PROMPT_SUGGESTIONS


############################
# SetBanners
############################


class SetBannersForm(BaseModel):
    banners: list[BannerModel]


@router.post("/banners", response_model=list[BannerModel])
async def set_banners(
    request: Request,
    form_data: SetBannersForm,
    user=Depends(get_admin_user),
):
    data = form_data.model_dump()
    request.app.state.config.BANNERS = data["banners"]
    return request.app.state.config.BANNERS


@router.get("/banners", response_model=list[BannerModel])
async def get_banners(
    request: Request,
    user=Depends(get_verified_user),
):
    return request.app.state.config.BANNERS


############################
# StudyMode Config
############################


class StudyModeConfigForm(BaseModel):
    ENABLE_STUDY_MODE: bool
    STUDY_MODE_SYSTEM_PROMPT: str


@router.get("/study_mode", response_model=StudyModeConfigForm)
async def get_study_mode_config(request: Request, user=Depends(get_admin_user)):
    return {
        "ENABLE_STUDY_MODE": request.app.state.config.ENABLE_STUDY_MODE,
        "STUDY_MODE_SYSTEM_PROMPT": request.app.state.config.STUDY_MODE_SYSTEM_PROMPT,
    }


@router.post("/study_mode", response_model=StudyModeConfigForm)
async def set_study_mode_config(
    request: Request,
    form_data: StudyModeConfigForm,
    user=Depends(get_admin_user),
):
    request.app.state.config.ENABLE_STUDY_MODE = form_data.ENABLE_STUDY_MODE
    request.app.state.config.STUDY_MODE_SYSTEM_PROMPT = (
        form_data.STUDY_MODE_SYSTEM_PROMPT
    )

    return {
        "ENABLE_STUDY_MODE": request.app.state.config.ENABLE_STUDY_MODE,
        "STUDY_MODE_SYSTEM_PROMPT": request.app.state.config.STUDY_MODE_SYSTEM_PROMPT,
    }


############################
# Data Visualization Config
############################


class DataVizConfigForm(BaseModel):
    ENABLE_DATA_VIZ: bool
    DATA_VIZ_SHARED_CORE_PROMPT: str
    DATA_VIZ_MODULE_DIAGRAM_ENABLED: bool
    DATA_VIZ_MODULE_DIAGRAM_PROMPT: str
    DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_ENABLED: bool
    DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_PROMPT: str
    DATA_VIZ_MODULE_CHART_DATAVIZ_ENABLED: bool
    DATA_VIZ_MODULE_CHART_DATAVIZ_PROMPT: str
    DATA_VIZ_MODULE_ART_ENABLED: bool
    DATA_VIZ_MODULE_ART_PROMPT: str
    DATA_VIZ_AUTO_REPAIR_ENABLED: bool
    DATA_VIZ_AUTO_REPAIR_MAX_ATTEMPTS: int
    DATA_VIZ_AUTO_REPAIR_MODEL: str
    DATA_VIZ_AUTO_REPAIR_REASONING_EFFORT: str


def _data_viz_config_response(request: Request) -> dict:
    cfg = request.app.state.config
    return {
        "ENABLE_DATA_VIZ": cfg.ENABLE_DATA_VIZ,
        "DATA_VIZ_SHARED_CORE_PROMPT": cfg.DATA_VIZ_SHARED_CORE_PROMPT,
        "DATA_VIZ_MODULE_DIAGRAM_ENABLED": cfg.DATA_VIZ_MODULE_DIAGRAM_ENABLED,
        "DATA_VIZ_MODULE_DIAGRAM_PROMPT": cfg.DATA_VIZ_MODULE_DIAGRAM_PROMPT,
        "DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_ENABLED": cfg.DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_ENABLED,
        "DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_PROMPT": cfg.DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_PROMPT,
        "DATA_VIZ_MODULE_CHART_DATAVIZ_ENABLED": cfg.DATA_VIZ_MODULE_CHART_DATAVIZ_ENABLED,
        "DATA_VIZ_MODULE_CHART_DATAVIZ_PROMPT": cfg.DATA_VIZ_MODULE_CHART_DATAVIZ_PROMPT,
        "DATA_VIZ_MODULE_ART_ENABLED": cfg.DATA_VIZ_MODULE_ART_ENABLED,
        "DATA_VIZ_MODULE_ART_PROMPT": cfg.DATA_VIZ_MODULE_ART_PROMPT,
        "DATA_VIZ_AUTO_REPAIR_ENABLED": cfg.DATA_VIZ_AUTO_REPAIR_ENABLED,
        "DATA_VIZ_AUTO_REPAIR_MAX_ATTEMPTS": cfg.DATA_VIZ_AUTO_REPAIR_MAX_ATTEMPTS,
        "DATA_VIZ_AUTO_REPAIR_MODEL": cfg.DATA_VIZ_AUTO_REPAIR_MODEL,
        "DATA_VIZ_AUTO_REPAIR_REASONING_EFFORT": cfg.DATA_VIZ_AUTO_REPAIR_REASONING_EFFORT,
    }


@router.get("/data_viz", response_model=DataVizConfigForm)
async def get_data_viz_config(request: Request, user=Depends(get_admin_user)):
    return _data_viz_config_response(request)


@router.post("/data_viz", response_model=DataVizConfigForm)
async def set_data_viz_config(
    request: Request,
    form_data: DataVizConfigForm,
    user=Depends(get_admin_user),
):
    cfg = request.app.state.config
    cfg.ENABLE_DATA_VIZ = form_data.ENABLE_DATA_VIZ
    cfg.DATA_VIZ_SHARED_CORE_PROMPT = form_data.DATA_VIZ_SHARED_CORE_PROMPT
    cfg.DATA_VIZ_MODULE_DIAGRAM_ENABLED = form_data.DATA_VIZ_MODULE_DIAGRAM_ENABLED
    cfg.DATA_VIZ_MODULE_DIAGRAM_PROMPT = form_data.DATA_VIZ_MODULE_DIAGRAM_PROMPT
    cfg.DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_ENABLED = (
        form_data.DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_ENABLED
    )
    cfg.DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_PROMPT = (
        form_data.DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_PROMPT
    )
    cfg.DATA_VIZ_MODULE_CHART_DATAVIZ_ENABLED = (
        form_data.DATA_VIZ_MODULE_CHART_DATAVIZ_ENABLED
    )
    cfg.DATA_VIZ_MODULE_CHART_DATAVIZ_PROMPT = (
        form_data.DATA_VIZ_MODULE_CHART_DATAVIZ_PROMPT
    )
    cfg.DATA_VIZ_MODULE_ART_ENABLED = form_data.DATA_VIZ_MODULE_ART_ENABLED
    cfg.DATA_VIZ_MODULE_ART_PROMPT = form_data.DATA_VIZ_MODULE_ART_PROMPT
    cfg.DATA_VIZ_AUTO_REPAIR_ENABLED = form_data.DATA_VIZ_AUTO_REPAIR_ENABLED
    # Clamp to the same 1..5 range the tool enforces at runtime, so an API client
    # can't persist 0 / negative / absurd values that bypass the UI's clamp.
    try:
        _attempts = int(form_data.DATA_VIZ_AUTO_REPAIR_MAX_ATTEMPTS)
    except (TypeError, ValueError):
        _attempts = 3
    cfg.DATA_VIZ_AUTO_REPAIR_MAX_ATTEMPTS = max(1, min(_attempts, 5))
    cfg.DATA_VIZ_AUTO_REPAIR_MODEL = form_data.DATA_VIZ_AUTO_REPAIR_MODEL
    # Whitelist reasoning_effort; anything else (incl. junk) becomes "" = unset.
    _effort = (form_data.DATA_VIZ_AUTO_REPAIR_REASONING_EFFORT or "").strip().lower()
    cfg.DATA_VIZ_AUTO_REPAIR_REASONING_EFFORT = (
        _effort
        if _effort in ("minimal", "low", "medium", "high", "xhigh", "max")
        else ""
    )

    return _data_viz_config_response(request)


############################
# Chat Semantic Search (message embeddings) Config
############################


class ChatEmbeddingConfigForm(BaseModel):
    ENABLE_CHAT_SEMANTIC_SEARCH: bool
    CHAT_EMBED_URL: str
    CHAT_EMBED_MODEL: str
    # Deferred-batching knobs (optional so older clients keep working).
    CHAT_EMBED_SWEEP_INTERVAL: Optional[int] = None
    CHAT_EMBED_TEXT_BATCH: Optional[int] = None


def _chat_embedding_config_response(request: Request) -> dict:
    cfg = request.app.state.config
    return {
        "ENABLE_CHAT_SEMANTIC_SEARCH": cfg.ENABLE_CHAT_SEMANTIC_SEARCH,
        "CHAT_EMBED_URL": cfg.CHAT_EMBED_URL,
        "CHAT_EMBED_MODEL": cfg.CHAT_EMBED_MODEL,
        "CHAT_EMBED_SWEEP_INTERVAL": cfg.CHAT_EMBED_SWEEP_INTERVAL,
        "CHAT_EMBED_TEXT_BATCH": cfg.CHAT_EMBED_TEXT_BATCH,
        # Read-only: the vector dimension is pinned to the pgvector column and can't
        # be changed here (surfaced so the UI can warn if a verified embedder's dim
        # differs from what the stored index expects).
        "CHAT_EMBED_DIM": ce.CHAT_EMBED_DIM,
    }


@router.get("/chat_embedding")
async def get_chat_embedding_config(request: Request, user=Depends(get_admin_user)):
    return _chat_embedding_config_response(request)


@router.post("/chat_embedding")
async def set_chat_embedding_config(
    request: Request,
    form_data: ChatEmbeddingConfigForm,
    user=Depends(get_admin_user),
):
    cfg = request.app.state.config
    cfg.ENABLE_CHAT_SEMANTIC_SEARCH = form_data.ENABLE_CHAT_SEMANTIC_SEARCH
    # Normalize the URL (strip trailing slash) so the "{url}/embeddings" join is clean.
    cfg.CHAT_EMBED_URL = (form_data.CHAT_EMBED_URL or "").strip().rstrip("/")
    cfg.CHAT_EMBED_MODEL = (form_data.CHAT_EMBED_MODEL or "").strip()
    # Clamp the batching knobs to the same ranges apply_runtime_config enforces, so an
    # API client can't persist values that would then silently diverge from runtime.
    if form_data.CHAT_EMBED_SWEEP_INTERVAL is not None:
        try:
            cfg.CHAT_EMBED_SWEEP_INTERVAL = max(
                10, int(form_data.CHAT_EMBED_SWEEP_INTERVAL)
            )
        except (TypeError, ValueError):
            pass
    if form_data.CHAT_EMBED_TEXT_BATCH is not None:
        try:
            cfg.CHAT_EMBED_TEXT_BATCH = max(
                1, min(128, int(form_data.CHAT_EMBED_TEXT_BATCH))
            )
        except (TypeError, ValueError):
            pass

    # Bridge the freshly-persisted values into the chat_embedder module's live globals
    # so the background sweep + query path pick them up immediately (no restart).
    ce.apply_runtime_config(
        url=cfg.CHAT_EMBED_URL,
        model=cfg.CHAT_EMBED_MODEL,
        enabled=cfg.ENABLE_CHAT_SEMANTIC_SEARCH,
        sweep_interval=cfg.CHAT_EMBED_SWEEP_INTERVAL,
        text_batch=cfg.CHAT_EMBED_TEXT_BATCH,
    )

    return _chat_embedding_config_response(request)


class ChatEmbeddingVerifyForm(BaseModel):
    CHAT_EMBED_URL: str
    # Probed with the URL: llama-swap routes (and cold-starts) the upstream by the
    # model field, so verifying with the wrong name fails even on a good URL.
    CHAT_EMBED_MODEL: Optional[str] = None


@router.post("/chat_embedding/verify")
async def verify_chat_embedding_connection(
    request: Request,
    form_data: ChatEmbeddingVerifyForm,
    user=Depends(get_admin_user),
):
    """Probe the embedder URL and confirm it returns a well-formed vector. Reports the
    detected dimension so the UI can flag a mismatch against the stored CHAT_EMBED_DIM."""
    url = (form_data.CHAT_EMBED_URL or "").strip().rstrip("/")
    try:
        dim = await ce.verify_embedder(url, model=form_data.CHAT_EMBED_MODEL)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")
    return {
        "status": True,
        "dim": dim,
        "expected_dim": ce.CHAT_EMBED_DIM,
        "dim_matches": dim == ce.CHAT_EMBED_DIM,
    }


@router.get("/chat_embedding/stats")
async def get_chat_embedding_stats(request: Request, user=Depends(get_admin_user)):
    """Counts of created / failed / pending message embeddings for the admin dashboard."""
    counts = await Chats.count_message_embeddings()
    return {
        **counts,
        "enabled": request.app.state.config.ENABLE_CHAT_SEMANTIC_SEARCH,
        "rebuilding": _chat_embedding_rebuild_in_progress,
    }


# Guard so a double-click (or overlapping admin requests) can't spawn several
# concurrent full-rebuild sweeps. The periodic keep-fresh sweeper is separately
# idempotent (ON CONFLICT upserts), so at worst they overlap harmlessly.
_chat_embedding_rebuild_in_progress = False


async def _run_rebuild_sweep():
    global _chat_embedding_rebuild_in_progress
    try:
        # run_sweep is a blocking psycopg2 job — run it off the event loop. It reads
        # the (already-bridged) chat_embedder globals for URL/model, and no-ops if the
        # embedder is unhealthy (rows stay pending until it recovers).
        from open_webui.scripts.backfill_chat_embeddings import run_sweep

        await asyncio.to_thread(
            run_sweep, None, 200, lambda *a: log.info("[chat-embed rebuild] %s", a[0] if a else "")
        )
    except Exception:
        log.exception("chat embedding rebuild sweep failed")
    finally:
        _chat_embedding_rebuild_in_progress = False


@router.post("/chat_embedding/rebuild")
async def rebuild_chat_embeddings(request: Request, user=Depends(get_admin_user)):
    """Throw out ALL stored embeddings and kick an immediate re-embed sweep. Use after
    changing the embedding model. Returns right away; the sweep runs in the background
    (poll /chat_embedding/stats to watch 'pending' drain)."""
    global _chat_embedding_rebuild_in_progress

    deleted = await Chats.delete_all_message_embeddings()

    started = False
    if not _chat_embedding_rebuild_in_progress:
        _chat_embedding_rebuild_in_progress = True
        started = True
        asyncio.create_task(_run_rebuild_sweep())

    return {"status": True, "deleted": deleted, "sweep_started": started}


@router.post("/chat_embedding/retry_failed")
async def retry_failed_chat_embeddings(request: Request, user=Depends(get_admin_user)):
    """Clear the permanent-failure markers (embedding IS NULL) and kick a sweep so those
    messages get another embedding attempt — use after fixing/upgrading a flaky embedder.
    Non-destructive: it only drops failure markers, never valid vectors."""
    global _chat_embedding_rebuild_in_progress

    cleared = await Chats.delete_failed_message_embeddings()

    started = False
    if not _chat_embedding_rebuild_in_progress:
        _chat_embedding_rebuild_in_progress = True
        started = True
        asyncio.create_task(_run_rebuild_sweep())

    return {"status": True, "cleared": cleared, "sweep_started": started}
