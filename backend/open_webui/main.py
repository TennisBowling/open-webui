import asyncio
import hashlib
import html as _stdlib_html
import inspect
import io
import json
import logging
import mimetypes
import os
import shutil
import sys
import time
import random
import re
from uuid import uuid4


from contextlib import asynccontextmanager
from urllib.parse import urlencode, parse_qs, urlparse
from pydantic import BaseModel, Field
from sqlalchemy import text

from typing import Dict, Optional
from aiocache import cached
import aiohttp
import anyio.to_thread
import requests
from redis import Redis


from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
    applications,
    BackgroundTasks,
)
from fastapi.openapi.docs import get_swagger_ui_html

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool

from starlette_compress import CompressMiddleware

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response, StreamingResponse
from starlette.datastructures import Headers

from starsessions import (
    SessionMiddleware as StarSessionsMiddleware,
    SessionAutoloadMiddleware,
)
from starsessions.stores.redis import RedisStore

from open_webui.utils import logger
from open_webui.utils.audit import AuditLevel, AuditLoggingMiddleware
from open_webui.utils.logger import start_logger
from open_webui.socket.main import (
    app as socket_app,
    get_event_emitter,
    emit_chat_user_message,
    get_models_in_use,
    get_active_user_ids,
    get_token_groups,
    set_stream_state,
    set_token_group,
    update_token_group,
    delete_token_group,
    get_token_usage,
    clear_stream_state,
    stream_version_init,
)
from open_webui.routers import (
    analytics,
    audio,
    automations,
    push,
    images,
    ollama,
    openai,
    retrieval,
    pipelines,
    tasks,
    auths,
    channels,
    chats,
    notes,
    folders,
    configs,
    groups,
    files,
    functions,
    models,
    mcp,
    prompts,
    evaluations,
    tools,
    users,
    utils,
    scim,
    subagents,
    flex_auto_flip,
    streams,
    bootstrap,
    videos,
)

from open_webui.routers.retrieval import (
    get_embedding_function,
    get_reranking_function,
    get_ef,
    get_rf,
)

from open_webui.internal.db import dispose_engine, engine

from open_webui.models.functions import Functions
from open_webui.models.models import Models
from open_webui.models.users import UserModel, Users
from open_webui.models.chats import Chats, sanitize_shared_chat_model
from open_webui.utils.lazy_blocks import text_only_content_from_blocks
from open_webui.models.token_usage import TokenGroup, TokenUsage

from open_webui.config import (
    # Ollama
    ENABLE_OLLAMA_API,
    OLLAMA_BASE_URLS,
    OLLAMA_API_CONFIGS,
    # OpenAI
    ENABLE_OPENAI_API,
    ENABLE_API_DEBUG_LOGGING,
    OPENAI_API_BASE_URLS,
    OPENAI_API_KEYS,
    OPENAI_API_CONFIGS,
    # Direct Connections
    ENABLE_DIRECT_CONNECTIONS,
    # Model list
    ENABLE_BASE_MODELS_CACHE,
    ENABLE_PRICING_SYNC,
    PRICING_SYNC_INTERVAL_HOURS,
    ENABLE_OPENROUTER_REASONING_DISCOVERY,
    # Thread pool size for FastAPI/AnyIO
    THREAD_POOL_SIZE,
    # Tool Server Configs
    TOOL_SERVER_CONNECTIONS,
    MCP_TOOL_CALL_TIMEOUT,
    # Container Workspace
    ENABLE_CONTAINER_WORKSPACE_SYNC,
    CONTAINER_DATA_ROOT,
    CONTAINER_MCP_SERVER_ID,
    CONTAINER_SYSTEM_PROMPT,
    # Image
    AUTOMATIC1111_API_AUTH,
    AUTOMATIC1111_BASE_URL,
    AUTOMATIC1111_CFG_SCALE,
    AUTOMATIC1111_SAMPLER,
    AUTOMATIC1111_SCHEDULER,
    COMFYUI_BASE_URL,
    COMFYUI_API_KEY,
    COMFYUI_WORKFLOW,
    COMFYUI_WORKFLOW_NODES,
    ENABLE_IMAGE_GENERATION,
    ENABLE_IMAGE_PROMPT_GENERATION,
    IMAGE_GENERATION_ENGINE,
    IMAGE_GENERATION_MODEL,
    IMAGE_SIZE,
    IMAGE_STEPS,
    IMAGES_OPENAI_API_BASE_URL,
    IMAGES_OPENAI_API_VERSION,
    IMAGES_OPENAI_API_KEY,
    IMAGES_GEMINI_API_BASE_URL,
    IMAGES_GEMINI_API_KEY,
    # Audio
    AUDIO_STT_ENGINE,
    AUDIO_STT_MODEL,
    AUDIO_STT_SUPPORTED_CONTENT_TYPES,
    AUDIO_STT_OPENAI_API_BASE_URL,
    AUDIO_STT_OPENAI_API_KEY,
    AUDIO_STT_OPENROUTER_API_KEY,
    AUDIO_STT_OPENROUTER_TEMPERATURE,
    AUDIO_STT_AZURE_API_KEY,
    AUDIO_STT_AZURE_REGION,
    AUDIO_STT_AZURE_LOCALES,
    AUDIO_STT_AZURE_BASE_URL,
    AUDIO_STT_AZURE_MAX_SPEAKERS,
    AUDIO_TTS_ENGINE,
    AUDIO_TTS_MODEL,
    AUDIO_TTS_VOICE,
    AUDIO_TTS_OPENAI_API_BASE_URL,
    AUDIO_TTS_OPENAI_API_KEY,
    AUDIO_TTS_OPENAI_PARAMS,
    AUDIO_TTS_OPENROUTER_API_KEY,
    AUDIO_TTS_API_KEY,
    AUDIO_TTS_SPLIT_ON,
    AUDIO_TTS_AZURE_SPEECH_REGION,
    AUDIO_TTS_AZURE_SPEECH_BASE_URL,
    AUDIO_TTS_AZURE_SPEECH_OUTPUT_FORMAT,
    WHISPER_MODEL,
    WHISPER_VAD_FILTER,
    WHISPER_LANGUAGE,
    DEEPGRAM_API_KEY,
    WHISPER_MODEL_AUTO_UPDATE,
    WHISPER_MODEL_DIR,
    # Retrieval
    RAG_TEMPLATE,
    DEFAULT_RAG_TEMPLATE,
    RAG_FULL_CONTEXT,
    BYPASS_EMBEDDING_AND_RETRIEVAL,
    RAG_EMBEDDING_MODEL,
    RAG_EMBEDDING_MODEL_AUTO_UPDATE,
    RAG_EMBEDDING_MODEL_TRUST_REMOTE_CODE,
    RAG_RERANKING_ENGINE,
    RAG_RERANKING_MODEL,
    RAG_EXTERNAL_RERANKER_URL,
    RAG_EXTERNAL_RERANKER_API_KEY,
    RAG_RERANKING_MODEL_AUTO_UPDATE,
    RAG_RERANKING_MODEL_TRUST_REMOTE_CODE,
    RAG_EMBEDDING_ENGINE,
    RAG_EMBEDDING_BATCH_SIZE,
    RAG_TOP_K,
    RAG_TOP_K_RERANKER,
    RAG_RELEVANCE_THRESHOLD,
    RAG_HYBRID_BM25_WEIGHT,
    RAG_ALLOWED_FILE_EXTENSIONS,
    RAG_FILE_MAX_COUNT,
    RAG_FILE_MAX_SIZE,
    FILE_IMAGE_COMPRESSION_WIDTH,
    FILE_IMAGE_COMPRESSION_HEIGHT,
    IMAGE_PROVIDER_COMPRESSION_ENABLED,
    IMAGE_PROVIDER_COMPRESSION_QUALITY,
    IMAGE_PROVIDER_COMPRESSION_MIN_BYTES,
    IMAGE_PROVIDER_MAX_DIMENSION,
    RAG_OPENAI_API_BASE_URL,
    RAG_OPENAI_API_KEY,
    RAG_AZURE_OPENAI_BASE_URL,
    RAG_AZURE_OPENAI_API_KEY,
    RAG_AZURE_OPENAI_API_VERSION,
    RAG_OLLAMA_BASE_URL,
    RAG_OLLAMA_API_KEY,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    CONTENT_EXTRACTION_ENGINE,
    DATALAB_MARKER_API_KEY,
    DATALAB_MARKER_API_BASE_URL,
    DATALAB_MARKER_ADDITIONAL_CONFIG,
    DATALAB_MARKER_SKIP_CACHE,
    DATALAB_MARKER_FORCE_OCR,
    DATALAB_MARKER_PAGINATE,
    DATALAB_MARKER_STRIP_EXISTING_OCR,
    DATALAB_MARKER_DISABLE_IMAGE_EXTRACTION,
    DATALAB_MARKER_FORMAT_LINES,
    DATALAB_MARKER_OUTPUT_FORMAT,
    MINERU_API_MODE,
    MINERU_API_URL,
    MINERU_API_KEY,
    MINERU_PARAMS,
    DATALAB_MARKER_USE_LLM,
    EXTERNAL_DOCUMENT_LOADER_URL,
    EXTERNAL_DOCUMENT_LOADER_API_KEY,
    TIKA_SERVER_URL,
    DOCLING_SERVER_URL,
    DOCLING_PARAMS,
    DOCLING_DO_OCR,
    DOCLING_FORCE_OCR,
    DOCLING_OCR_ENGINE,
    DOCLING_OCR_LANG,
    DOCLING_TABLE_MODE,
    DOCLING_PIPELINE,
    DOCLING_DO_PICTURE_DESCRIPTION,
    DOCLING_PICTURE_DESCRIPTION_MODE,
    DOCLING_PICTURE_DESCRIPTION_LOCAL,
    DOCLING_PICTURE_DESCRIPTION_API,
    DOCUMENT_INTELLIGENCE_ENDPOINT,
    DOCUMENT_INTELLIGENCE_KEY,
    RAG_TEXT_SPLITTER,
    TIKTOKEN_ENCODING_NAME,
    YOUTUBE_LOADER_LANGUAGE,
    YOUTUBE_LOADER_PROXY_URL,
    # Retrieval (Web Search: Exa search + Jina Reader fetch)
    ENABLE_WEB_SEARCH,
    EXA_API_KEY,
    EXA_API_KEY_2,
    EXA_KEY_STATUS,
    EXA_SEARCH_NUM_RESULTS,
    EXA_SEARCH_TYPE,
    EXA_INCLUDE_DOMAINS,
    EXA_EXCLUDE_DOMAINS,
    JINA_API_KEY,
    JINA_READER_API_BASE_URL,
    JINA_READER_TOKEN_USAGE,
    JINA_READER_VIEWPORT_WIDTH,
    JINA_READER_VIEWPORT_HEIGHT,
    JINA_READER_TIMEOUT,
    EXA_CONTENTS_MAX_CHARACTERS,
    EXA_CONTENTS_LIVECRAWL,
    WEB_SEARCH_SYSTEM_PROMPT,
    ENABLE_STUDY_MODE,
    STUDY_MODE_SYSTEM_PROMPT,
    # Chat semantic search (message embeddings)
    ENABLE_CHAT_SEMANTIC_SEARCH,
    CHAT_EMBED_URL,
    CHAT_EMBED_MODEL,
    CHAT_EMBED_SWEEP_INTERVAL,
    CHAT_EMBED_TEXT_BATCH,
    # Data Visualization
    ENABLE_DATA_VIZ,
    # Automations
    ENABLE_AUTOMATIONS,
    AUTOMATIONS_MAX_ACTIVE_PER_USER,
    WEBPUSH_VAPID_PUBLIC_KEY,
    WEBPUSH_VAPID_PRIVATE_KEY,
    ENABLE_VIDEO_INPUT,
    ENABLE_VIDEO_URL_INGEST,
    VIDEO_DEFAULT_AUDIO,
    VIDEO_DEFAULT_FPS,
    VIDEO_DEFAULT_QUALITY,
    VIDEO_MAX_SOURCE_SIZE_MB,
    VIDEO_WARN_DURATION_SECONDS,
    DATA_VIZ_SHARED_CORE_PROMPT,
    DATA_VIZ_MODULE_DIAGRAM_ENABLED,
    DATA_VIZ_MODULE_DIAGRAM_PROMPT,
    DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_ENABLED,
    DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_PROMPT,
    DATA_VIZ_MODULE_CHART_DATAVIZ_ENABLED,
    DATA_VIZ_MODULE_CHART_DATAVIZ_PROMPT,
    DATA_VIZ_MODULE_ART_ENABLED,
    DATA_VIZ_MODULE_ART_PROMPT,
    DATA_VIZ_AUTO_REPAIR_ENABLED,
    DATA_VIZ_AUTO_REPAIR_MAX_ATTEMPTS,
    DATA_VIZ_AUTO_REPAIR_MODEL,
    DATA_VIZ_AUTO_REPAIR_REASONING_EFFORT,
    # Subagents
    ENABLE_SUBAGENTS,
    SUBAGENT_DEFAULT_MODEL,
    SUBAGENT_CONTEXT_FALLBACK_MODEL,
    SUBAGENT_SYSTEM_PROMPT,
    SUBAGENT_SYSTEM_PROMPT_APPEND,
    SUBAGENT_PARENT_PROMPT,
    SUBAGENT_DEFAULT_REASONING_EFFORT,
    SUBAGENT_DEFAULT_SERVICE_TIER,
    SUBAGENT_ALLOW_EXTERNAL_TOOLS,
    SUBAGENT_EXTERNAL_TOOLS_PROMPT,
    # Ask user
    ENABLE_ASK_USER,
    ASK_USER_PARENT_PROMPT,
    # Flex auto-flip
    FLEX_AUTO_FLIP_ENABLED,
    FLEX_AUTO_FLIP_OFF_PEAK_START_HOUR,
    FLEX_AUTO_FLIP_OFF_PEAK_END_HOUR,
    FLEX_AUTO_FLIP_OFF_PEAK_TIMEZONE,
    FLEX_AUTO_FLIP_THRESHOLD_RATIO,
    GOOGLE_DRIVE_CLIENT_ID,
    GOOGLE_DRIVE_API_KEY,
    ENABLE_ONEDRIVE_INTEGRATION,
    ONEDRIVE_CLIENT_ID_PERSONAL,
    ONEDRIVE_CLIENT_ID_BUSINESS,
    ONEDRIVE_SHAREPOINT_URL,
    ONEDRIVE_SHAREPOINT_TENANT_ID,
    ENABLE_ONEDRIVE_PERSONAL,
    ENABLE_ONEDRIVE_BUSINESS,
    ENABLE_RAG_HYBRID_SEARCH,
    ENABLE_RAG_LOCAL_WEB_FETCH,
    ENABLE_GOOGLE_DRIVE_INTEGRATION,
    UPLOAD_DIR,
    # WebUI
    WEBUI_AUTH,
    WEBUI_NAME,
    WEBUI_BANNERS,
    WEBHOOK_URL,
    ADMIN_EMAIL,
    SHOW_ADMIN_DETAILS,
    JWT_EXPIRES_IN,
    ENABLE_SIGNUP,
    ENABLE_LOGIN_FORM,
    ENABLE_API_KEY,
    ENABLE_API_KEY_ENDPOINT_RESTRICTIONS,
    API_KEY_ALLOWED_ENDPOINTS,
    ENABLE_CHANNELS,
    ENABLE_NOTES,
    ENABLE_COMMUNITY_SHARING,
    ENABLE_MESSAGE_RATING,
    ENABLE_USER_WEBHOOKS,
    ENABLE_EVALUATION_ARENA_MODELS,
    BYPASS_ADMIN_ACCESS_CONTROL,
    USER_PERMISSIONS,
    DEFAULT_USER_ROLE,
    PENDING_USER_OVERLAY_CONTENT,
    PENDING_USER_OVERLAY_TITLE,
    DEFAULT_PROMPT_SUGGESTIONS,
    DEFAULT_MODELS,
    DEFAULT_ARENA_MODEL,
    MODEL_ORDER_LIST,
    EVALUATION_ARENA_MODELS,
    # WebUI (OAuth)
    ENABLE_OAUTH_ROLE_MANAGEMENT,
    OAUTH_ROLES_CLAIM,
    OAUTH_EMAIL_CLAIM,
    OAUTH_PICTURE_CLAIM,
    OAUTH_USERNAME_CLAIM,
    OAUTH_ALLOWED_ROLES,
    OAUTH_ADMIN_ROLES,
    # WebUI (LDAP)
    ENABLE_LDAP,
    LDAP_SERVER_LABEL,
    LDAP_SERVER_HOST,
    LDAP_SERVER_PORT,
    LDAP_ATTRIBUTE_FOR_MAIL,
    LDAP_ATTRIBUTE_FOR_USERNAME,
    LDAP_SEARCH_FILTERS,
    LDAP_SEARCH_BASE,
    LDAP_APP_DN,
    LDAP_APP_PASSWORD,
    LDAP_USE_TLS,
    LDAP_CA_CERT_FILE,
    LDAP_VALIDATE_CERT,
    LDAP_CIPHERS,
    # LDAP Group Management
    ENABLE_LDAP_GROUP_MANAGEMENT,
    ENABLE_LDAP_GROUP_CREATION,
    LDAP_ATTRIBUTE_FOR_GROUPS,
    # Misc
    ENV,
    CACHE_DIR,
    STATIC_DIR,
    FRONTEND_BUILD_DIR,
    CORS_ALLOW_ORIGIN,
    DEFAULT_LOCALE,
    OAUTH_PROVIDERS,
    WEBUI_URL,
    RESPONSE_WATERMARK,
    # Admin
    ENABLE_ADMIN_CHAT_ACCESS,
    BYPASS_ADMIN_ACCESS_CONTROL,
    ENABLE_ADMIN_EXPORT,
    # Tasks
    TASK_MODEL,
    TASK_MODEL_EXTERNAL,
    ENABLE_TAGS_GENERATION,
    ENABLE_TITLE_GENERATION,
    TITLE_GENERATION_OVERRIDE,
    TITLE_GENERATION_MODEL,
    ENABLE_FOLLOW_UP_GENERATION,
    FOLLOW_UP_GENERATION_OVERRIDE,
    ENABLE_SEARCH_QUERY_GENERATION,
    ENABLE_RETRIEVAL_QUERY_GENERATION,
    ENABLE_AUTOCOMPLETE_GENERATION,
    TITLE_GENERATION_PROMPT_TEMPLATE,
    FOLLOW_UP_GENERATION_PROMPT_TEMPLATE,
    TAGS_GENERATION_PROMPT_TEMPLATE,
    IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE,
    TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE,
    QUERY_GENERATION_PROMPT_TEMPLATE,
    AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE,
    AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH,
    AppConfig,
    reset_config_async,
)
from open_webui.env import (
    LICENSE_KEY,
    AUDIT_EXCLUDED_PATHS,
    AUDIT_LOG_LEVEL,
    CHANGELOG,
    REDIS_URL,
    REDIS_CLUSTER,
    REDIS_KEY_PREFIX,
    REDIS_SENTINEL_HOSTS,
    REDIS_SENTINEL_PORT,
    GLOBAL_LOG_LEVEL,
    MAX_BODY_LOG_SIZE,
    SAFE_MODE,
    SRC_LOG_LEVELS,
    VERSION,
    INSTANCE_ID,
    PROFILE_LOOP_LAG,
    PROFILE_LOOP_LAG_INTERVAL,
    PROFILE_LOOP_LAG_WINDOW_SECONDS,
    WEBUI_BUILD_HASH,
    WEBUI_SECRET_KEY,
    WEBUI_SESSION_COOKIE_SAME_SITE,
    WEBUI_SESSION_COOKIE_SECURE,
    ENABLE_SIGNUP_PASSWORD_CONFIRMATION,
    WEBUI_AUTH_TRUSTED_EMAIL_HEADER,
    WEBUI_AUTH_TRUSTED_NAME_HEADER,
    WEBUI_AUTH_SIGNOUT_REDIRECT_URL,
    # SCIM
    SCIM_ENABLED,
    SCIM_TOKEN,
    ENABLE_COMPRESSION_MIDDLEWARE,
    ENABLE_WEBSOCKET_SUPPORT,
    STREAM_PROTOCOL_VERSION,
    BYPASS_MODEL_ACCESS_CONTROL,
    RESET_CONFIG_ON_START,
    ENABLE_VERSION_UPDATE_CHECK,
    ENABLE_OTEL,
    EXTERNAL_PWA_MANIFEST_URL,
    AIOHTTP_CLIENT_SESSION_SSL,
    AIOHTTP_CLIENT_TIMEOUT,
    ENABLE_STAR_SESSIONS_MIDDLEWARE,
    AUTOMATION_SWEEP_INTERVAL_SECONDS,
)


from open_webui.utils.models import (
    get_all_models,
    get_all_base_models_deduped,
    check_model_access,
    get_filtered_models,
)
from open_webui.utils.chat import (
    ActiveSubagentRerunError,
    ChatMessageAncestryError,
    assemble_conversation_from_leaf,
    generate_chat_completion as chat_completion_handler,
    chat_action as chat_action_handler,
)
from open_webui.utils.embeddings import generate_embeddings
from open_webui.utils.middleware import process_chat_payload, process_chat_response
from open_webui.utils.live_tool_selection import normalize_live_tool_selection
from open_webui.utils.chat_transport import is_persisted_chat_generation
from open_webui.utils.access_control import has_access

from open_webui.utils.auth import (
    get_license_data,
    get_http_authorization_cred,
    decode_token,
    get_admin_user,
    get_optional_user,
    get_verified_user,
)
from open_webui.utils.plugin import install_tool_and_function_dependencies
from open_webui.utils.cache import etag_response
from open_webui.utils.oauth import (
    OAuthManager,
    OAuthClientManager,
    decrypt_data,
    OAuthClientInformationFull,
)
from open_webui.utils.security_headers import SecurityHeadersMiddleware
from open_webui.utils.redis import get_redis_connection

from open_webui.tasks import (
    GenerationCancelledError,
    redis_task_command_listener,
    get_generation_operation,
    is_chat_work_blocked,
    is_generation_cancelled,
    is_generation_turn_cancelled,
    mark_generation_cancelled,
    heartbeat_generation_operation_until_bound,
    latch_generation_cancellation,
    list_generation_operations_by_item,
    list_item_task_ids_by_prefix,
    register_generation_operation,
    supersede_generation_operation,
    finish_generation_supersede,
    unregister_generation_operation,
    collect_chat_work_state,
    create_task,
    stop_tasks_and_wait,
    cancel_all_local_tasks,
)  # Import from tasks.py

from open_webui.utils.redis import get_sentinels_from_env
from open_webui.utils.headless_request import HeadlessRequest


from open_webui.constants import ERROR_MESSAGES


if SAFE_MODE:
    print("SAFE MODE ENABLED")

logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


SPA_REVALIDATE_CACHE_CONTROL = "no-cache"
SPA_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
SPA_LONG_CACHE_CONTROL = "public, max-age=31536000"
SPA_WASM_CACHE_CONTROL = "public, max-age=86400"
# Brand/static images + fonts: content-addressed by filename in practice and
# safe to cache for a week with background revalidation (unlike the html shell,
# which must stay no-cache so redeploys reach the client immediately).
SPA_BRAND_ASSET_CACHE_CONTROL = "public, max-age=604800, stale-while-revalidate=86400"
_CACHEABLE_ASSET_EXTS = (
    ".png",
    ".svg",
    ".ico",
    ".jpg",
    ".jpeg",
    ".webp",
    ".woff2",
)


class CacheControlStaticFiles(StaticFiles):
    def get_cache_control(self, path: str) -> Optional[str]:
        return None

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)

        path = os.path.relpath(full_path, self.directory).replace(os.sep, "/")
        cache_control = self.get_cache_control(path)
        if cache_control:
            response.headers["Cache-Control"] = cache_control

        return response


class FrontendStaticFiles(CacheControlStaticFiles):
    def get_cache_control(self, path: str) -> Optional[str]:
        if path.startswith("_app/immutable/"):
            return SPA_IMMUTABLE_CACHE_CONTROL

        if path.startswith(("assets/", "audio/", "themes/")):
            return SPA_LONG_CACHE_CONTROL

        if path.startswith("wasm/"):
            return SPA_WASM_CACHE_CONTROL

        # Build-root brand images/fonts (favicon, user, image-placeholder,
        # marker icons, web-app-manifest pngs, …). html stays no-cache below.
        if path.lower().endswith(_CACHEABLE_ASSET_EXTS):
            return SPA_BRAND_ASSET_CACHE_CONTROL

        return SPA_REVALIDATE_CACHE_CONTROL

    async def get_response(self, path: str, scope):
        precompressed = self._precompressed_response(path, scope)
        if precompressed is not None:
            return precompressed
        return await super().get_response(path, scope)

    def _precompressed_response(self, path: str, scope) -> Optional[Response]:
        """Serve a build-time brotli-11 sibling (``.br``) for immutable JS/CSS.

        Avoids recompressing every cold-load chunk on the fly at brotli q4 (and
        spending that CPU on the single worker per request); guarantees the smaller
        q11 payload instead. Produced by ``scripts/precompress.mjs`` (postbuild).
        Falls back to the normal (live-compressed) path when no ``.br`` exists or
        the client doesn't accept br. ``Content-Encoding: br`` makes the compress
        middleware skip the response, so there's no double-encoding.
        """
        if scope.get("method", "GET") not in ("GET", "HEAD"):
            return None
        if not path.startswith("_app/immutable/"):
            return None
        if not (path.endswith(".js") or path.endswith(".css")):
            return None
        headers = Headers(scope=scope)
        accept_encoding = headers.get("accept-encoding", "")
        if "br" not in {
            tok.split(";")[0].strip() for tok in accept_encoding.split(",")
        }:
            return None
        if "range" in headers:  # don't range-serve an encoded body
            return None

        br_full_path = os.path.join(self.directory, path + ".br")
        try:
            stat_result = os.stat(br_full_path)
        except OSError:
            return None

        media_type = "text/javascript" if path.endswith(".js") else "text/css"
        response = FileResponse(
            br_full_path,
            stat_result=stat_result,
            media_type=media_type,
            method=scope.get("method"),
        )
        response.headers["Content-Encoding"] = "br"
        response.headers["Vary"] = "Accept-Encoding"
        cache_control = self.get_cache_control(path)
        if cache_control:
            response.headers["Cache-Control"] = cache_control
        return response


class RevalidatingStaticFiles(CacheControlStaticFiles):
    def get_cache_control(self, path: str) -> Optional[str]:
        # Images/fonts under /static are effectively immutable brand assets;
        # cache them for a week. Everything else (html, js, css, json) stays
        # no-cache so admin edits / redeploys reach clients immediately.
        if path.lower().endswith(_CACHEABLE_ASSET_EXTS):
            return SPA_BRAND_ASSET_CACHE_CONTROL
        return SPA_REVALIDATE_CACHE_CONTROL


def _render_index_html(
    raw: str,
    name: str,
    base_url: str,
    strip_loader: bool = False,
    strip_custom_css: bool = False,
    title: Optional[str] = None,
    description: Optional[str] = None,
    url: Optional[str] = None,
) -> str:
    """Inject the instance name + link-preview meta tags into the built SPA shell.

    Pure string transform (no I/O) so it can be memoized. Rewrites the single
    ``<title>Open WebUI</title>`` line to the configured name and, unless already
    present (idempotency guard on ``og:title``), appends description /
    application-name / Open Graph / Twitter ``<meta>`` tags before ``</head>`` so
    JS-less link-preview crawlers (Discord/WhatsApp/iMessage) and the first paint
    both show the real name.

    ``title``/``description``/``url`` override the instance defaults for
    per-page previews (shared chats): the browser tab becomes
    ``"{title} • {name}"`` (mirroring the SPA's own title format), the
    ``og:title``/``twitter:title`` carry the bare page title, and
    ``og:site_name`` keeps the instance name.
    """
    safe_name = _stdlib_html.escape(name, quote=True)
    if title:
        safe_title = _stdlib_html.escape(title, quote=True)
        safe_page_title = _stdlib_html.escape(f"{title} • {name}", quote=True)
    else:
        safe_title = safe_name
        safe_page_title = safe_name
    if description:
        # Collapse newlines/runs of whitespace: meta content attributes should
        # be a single line regardless of what the source message contained.
        page_description = _stdlib_html.escape(
            re.sub(r"\s+", " ", description).strip(), quote=True
        )
    else:
        page_description = _stdlib_html.escape(
            f"{name} is an open, extensible, user-friendly interface for AI that adapts to your workflow.",
            quote=True,
        )
    base = (base_url or "").rstrip("/")
    image_url = (
        f"{base}/static/web-app-manifest-512x512.png"
        if base
        else "/static/web-app-manifest-512x512.png"
    )
    out = raw.replace("<title>Open WebUI</title>", f"<title>{safe_page_title}</title>", 1)
    # Drop the optional admin-hook tags when their files are empty/absent so the
    # client doesn't spend a round-trip fetching 0 bytes on every cold load.
    if strip_loader:
        out = re.sub(
            r"<script[^>]*src=\"/static/loader\.js\"[^>]*>\s*</script>",
            "",
            out,
            count=1,
        )
    if strip_custom_css:
        out = re.sub(
            r"<link[^>]*href=\"/static/custom\.css\"[^>]*/?>",
            "",
            out,
            count=1,
        )
    if "og:title" not in out:
        if url:
            og_url_tag = f'<meta property="og:url" content="{_stdlib_html.escape(url, quote=True)}" />'
        elif base:
            og_url_tag = f'<meta property="og:url" content="{base}/" />'
        else:
            og_url_tag = ""
        tags = (
            f'<meta name="description" content="{page_description}" />'
            f'<meta name="application-name" content="{safe_name}" />'
            f'<meta name="apple-mobile-web-app-title" content="{safe_name}" />'
            f'<meta property="og:type" content="website" />'
            f'<meta property="og:site_name" content="{safe_name}" />'
            f'<meta property="og:title" content="{safe_title}" />'
            f'<meta property="og:description" content="{page_description}" />'
            f'<meta property="og:image" content="{image_url}" />'
            + og_url_tag
            + f'<meta name="twitter:card" content="summary" />'
            f'<meta name="twitter:title" content="{safe_title}" />'
            f'<meta name="twitter:description" content="{page_description}" />'
            f'<meta name="twitter:image" content="{image_url}" />'
        )
        out = out.replace("</head>", tags + "</head>", 1)
    return out


_LINK_PREVIEW_DESCRIPTION_LIMIT = 280


def _link_preview_truncate(text: str, limit: int = _LINK_PREVIEW_DESCRIPTION_LIMIT) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Prefer a word boundary so the snippet doesn't end mid-word.
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" \t,;:—-") + "…"


def _link_preview_message_text(message: dict) -> str:
    """Visible text of one message: string content, multimodal text parts, or
    (lazy-block messages) the canonical text-only projection of content_blocks."""
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            part.get("text") or ""
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        text = ""
    if not text.strip():
        blocks = message.get("content_blocks")
        if isinstance(blocks, list) and blocks:
            try:
                text = text_only_content_from_blocks(blocks)
            except Exception:
                text = ""
    return text


def _shared_chat_link_preview(chat) -> tuple:
    """(title, description) for a shared chat's link-preview meta tags.

    Title is the chat's own title; the description is the visible text of the
    first user message (falling back to the first non-empty message) so the
    embed reflects the actual conversation instead of a static blurb. Messages
    are ordered leaf→root along the parent chain — the server-side twin of the
    viewer's ``createMessagesList``. ``chat`` must already be shaped by
    ``sanitize_shared_chat_model`` so only publicly-shared data is surfaced.
    """
    title = (getattr(chat, "title", "") or "").strip()
    data = getattr(chat, "chat", None)
    if not title and isinstance(data, dict):
        title = str(data.get("title") or "").strip()

    description = ""
    if isinstance(data, dict):
        history = data.get("history")
        messages_map = history.get("messages") if isinstance(history, dict) else None

        ordered = []
        if isinstance(messages_map, dict) and messages_map:
            # Cycle guard mirrors the frontend walk: a corrupted tree must not
            # spin the request handler forever.
            seen = set()
            current_id = history.get("currentId") if isinstance(history, dict) else None
            current = messages_map.get(current_id)
            while isinstance(current, dict):
                mid = current.get("id")
                if mid is not None:
                    if mid in seen:
                        break
                    seen.add(mid)
                ordered.insert(0, current)
                parent_id = current.get("parentId")
                current = (
                    messages_map.get(parent_id)
                    if parent_id and parent_id != mid
                    else None
                )
        if not ordered and isinstance(data.get("messages"), list):
            ordered = [m for m in data["messages"] if isinstance(m, dict)]

        def _clean(text: str) -> str:
            # Message content is markdown/plain text: strip the common markup
            # so the snippet reads as prose (links → text, headings/emphasis
            # markers dropped), then drop any residual HTML tags and collapse
            # whitespace into a single preview line.
            text = re.sub(r"```[a-zA-Z0-9_-]*", " ", text)
            text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
            text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
            text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
            text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text, flags=re.DOTALL)
            text = re.sub(r"`([^`]*)`", r"\1", text)
            text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
            text = re.sub(r"<[^>]+>", " ", text)
            return re.sub(r"\s+", " ", text).strip()

        candidate = next(
            (m for m in ordered if m.get("role") == "user" and _link_preview_message_text(m).strip()),
            None,
        )
        if candidate is None:
            candidate = next(
                (m for m in ordered if _link_preview_message_text(m).strip()), None
            )
        if candidate is not None:
            description = _link_preview_truncate(_clean(_link_preview_message_text(candidate)))

    return title or "Shared Chat", description


def _build_injected_index_response(
    scope,
    directory: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    url: Optional[str] = None,
) -> Optional[Response]:
    """Serve the SPA shell with the instance name injected, computed once.

    Zero per-request cost for the generic shell: the transformed HTML is
    memoized on ``app.state`` keyed by ``(name, base_url)`` and only recomputed
    when the instance name (which can change at runtime via the license path)
    or the resolved base URL changes. It's the same single HTML fetch the
    client already makes — just different bytes. Returns ``None`` on any
    failure so the caller falls back to the untransformed shell.

    Per-page ``title``/``description``/``url`` overrides (shared-chat link
    previews) skip the transformed-HTML memo and render per request from a
    stat-keyed raw-shell cache instead — chat titles/messages change, so only
    the file read is shared with the generic path.
    """
    # Only the document fetch (GET) needs the injected body; leave HEAD and
    # other methods to the default file handling (correct empty-body HEAD).
    if scope.get("method", "GET") != "GET":
        return None
    try:
        name = getattr(app.state, "WEBUI_NAME", "Open WebUI")

        base_url = getattr(app.state.config, "WEBUI_URL", "") or ""
        if not base_url:
            headers = Headers(scope=scope)
            proto = (
                headers.get("x-forwarded-proto") or scope.get("scheme") or "http"
            )
            host = headers.get("host")
            if host:
                base_url = f"{proto}://{host}"

        # Key the memo on the shell file's identity too: a frontend rebuild
        # under a running backend replaces the hashed bundles on disk, so a
        # memo keyed only on (name, base_url) kept serving the OLD html —
        # whose entry-chunk references 404 (the new build deleted them) and
        # the app dies with "Failed to fetch dynamically imported module"
        # until a backend restart. One os.stat per document GET is noise.
        index_path = os.path.join(directory, "index.html")
        stat = os.stat(index_path)

        # Stat the optional admin-hook files so (a) an empty one gets its tag
        # stripped and (b) an admin edit (0 -> non-empty or content change)
        # busts the memo + ETag and the tag reappears.
        def _asset_stat(fname):
            try:
                st = os.stat(os.path.join(STATIC_DIR, fname))
                return (st.st_mtime_ns, st.st_size)
            except OSError:
                return None

        loader_stat = _asset_stat("loader.js")
        custom_css_stat = _asset_stat("custom.css")
        strip_loader = loader_stat is None or loader_stat[1] == 0
        strip_custom_css = custom_css_stat is None or custom_css_stat[1] == 0

        if title is not None or description is not None or url is not None:
            raw_cache = getattr(app.state, "_spa_raw_index_cache", None)
            raw_key = (stat.st_mtime_ns, stat.st_size)
            if raw_cache is None or raw_cache.get("key") != raw_key:
                with open(index_path, "r", encoding="utf-8") as f:
                    raw_cache = {"key": raw_key, "raw": f.read()}
                app.state._spa_raw_index_cache = raw_cache
            html = _render_index_html(
                raw_cache["raw"],
                name,
                base_url,
                strip_loader,
                strip_custom_css,
                title=title,
                description=description,
                url=url,
            )
            etag = (
                '"'
                + hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]
                + '"'
            )
        else:
            key = (
                name,
                base_url,
                stat.st_mtime_ns,
                stat.st_size,
                loader_stat,
                custom_css_stat,
            )
            cache = getattr(app.state, "_spa_index_cache", None)
            if cache is None or cache.get("key") != key:
                with open(index_path, "r", encoding="utf-8") as f:
                    raw = f.read()
                html = _render_index_html(
                    raw, name, base_url, strip_loader, strip_custom_css
                )
                etag = (
                    '"'
                    + hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:16]
                    + '"'
                )
                cache = {"key": key, "html": html, "etag": etag}
                app.state._spa_index_cache = cache

            html = cache["html"]
            etag = cache["etag"]

        inm = Headers(scope=scope).get("if-none-match")
        if inm and etag.strip('"') in {
            part.strip().strip('"') for part in inm.split(",")
        }:
            return Response(
                status_code=304,
                headers={
                    "ETag": etag,
                    "Cache-Control": SPA_REVALIDATE_CACHE_CONTROL,
                },
            )

        return HTMLResponse(
            content=html,
            headers={
                "Cache-Control": SPA_REVALIDATE_CACHE_CONTROL,
                "ETag": etag,
            },
        )
    except Exception as e:
        log.debug(f"SPA index injection skipped: {e}")
        return None


class SPAStaticFiles(FrontendStaticFiles):
    def _injected_index_response(self, scope) -> Optional[Response]:
        return _build_injected_index_response(scope, self.directory)

    async def get_response(self, path: str, scope):
        if path in ("", ".", "index.html"):
            injected = self._injected_index_response(scope)
            if injected is not None:
                return injected
        try:
            return await super().get_response(path, scope)
        except (HTTPException, StarletteHTTPException) as ex:
            if ex.status_code == 404:
                if path.endswith(".js"):
                    # Return 404 for javascript files
                    raise ex
                else:
                    injected = self._injected_index_response(scope)
                    if injected is not None:
                        return injected
                    return await super().get_response("index.html", scope)
            else:
                raise ex


print(
    rf"""
 ██████╗ ██████╗ ███████╗███╗   ██╗    ██╗    ██╗███████╗██████╗ ██╗   ██╗██╗
██╔═══██╗██╔══██╗██╔════╝████╗  ██║    ██║    ██║██╔════╝██╔══██╗██║   ██║██║
██║   ██║██████╔╝█████╗  ██╔██╗ ██║    ██║ █╗ ██║█████╗  ██████╔╝██║   ██║██║
██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║    ██║███╗██║██╔══╝  ██╔══██╗██║   ██║██║
╚██████╔╝██║     ███████╗██║ ╚████║    ╚███╔███╔╝███████╗██████╔╝╚██████╔╝██║
 ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝     ╚══╝╚══╝ ╚══════╝╚═════╝  ╚═════╝ ╚═╝


v{VERSION} - building the best AI user interface.
{f"Commit: {WEBUI_BUILD_HASH}" if WEBUI_BUILD_HASH != "dev-build" else ""}
https://github.com/open-webui/open-webui
"""
)


@asynccontextmanager
def _install_benign_shutdown_error_filter() -> None:
    """Downgrade known-benign async-generator teardown errors the event loop
    reports while finalizing async generators at process exit.

    On Ctrl+C an MCP streamable-HTTP client (an anyio task group living inside an
    async generator) whose owning chat/subagent task was cancelled mid-stream may
    not finish unwinding before ``loop.shutdown_asyncgens()`` finalizes it. asyncio
    then routes ``RuntimeError: athrow(): asynchronous generator is already
    running`` (or anyio's "Attempted to exit cancel scope in a different task")
    through the loop exception handler, which loguru renders as a scary multi-frame
    traceback. These are teardown races during exit — nothing is generating, no
    data is touched. Everything else is delegated to asyncio's normal handler
    unchanged, so real errors are still surfaced.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    prior_handler = loop.get_exception_handler()
    benign_markers = (
        "asynchronous generator is already running",
        "Attempted to exit cancel scope in a different task",
    )

    def _handler(loop, context):
        exc = context.get("exception")
        blob = f"{context.get('message', '')} {exc!r}"
        if any(marker in blob for marker in benign_markers):
            log.debug(
                "Suppressed benign async-gen teardown at shutdown: %s", blob.strip()
            )
            return
        if prior_handler is not None:
            prior_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


async def lifespan(app: FastAPI):
    app.state.instance_id = INSTANCE_ID
    start_logger()
    _install_benign_shutdown_error_filter()

    # Local stdio MCP servers are application services, not chat-turn
    # subprocesses. Start every enabled saved connection now and retain its
    # actor-owned session until restart/disable/delete or application shutdown.
    from open_webui.utils.mcp.persistent import (
        PersistentMCPManager,
        personal_mcp_process_key,
    )

    app.state.persistent_mcp = PersistentMCPManager()

    if RESET_CONFIG_ON_START:
        await reset_config_async()

    if SAFE_MODE:
        await Functions.deactivate_all_functions()

    try:
        from open_webui.models.mcp import MCPConnections
        from open_webui.utils.mcp.connections import build_personal_mcp_connect_kwargs

        for public_connection in await MCPConnections.get_all_connections():
            if not public_connection.enabled or public_connection.transport != "stdio":
                continue
            try:
                connection = await MCPConnections.get_connection_by_id(
                    public_connection.id, include_secrets=True
                )
                kwargs = await build_personal_mcp_connect_kwargs(connection)
                await app.state.persistent_mcp.ensure(
                    personal_mcp_process_key(connection.user_id, connection.id), kwargs
                )
            except Exception:
                log.exception(
                    "Failed to start persistent MCP connection %s",
                    public_connection.id,
                )
    except Exception:
        log.exception("Failed to load persistent MCP connections")

    try:
        from open_webui.utils.mcp.client import build_mcp_connect_kwargs
        from open_webui.utils.mcp.persistent import admin_mcp_process_key

        for connection in app.state.config.TOOL_SERVER_CONNECTIONS or []:
            server_id = (connection.get("info") or {}).get("id")
            if (
                connection.get("type") == "mcp"
                and server_id
                and (connection.get("config") or {}).get("command")
            ):
                try:
                    kwargs = build_mcp_connect_kwargs(
                        connection, bearer_token=None, metadata=None
                    )
                    await app.state.persistent_mcp.ensure(
                        admin_mcp_process_key(server_id), kwargs
                    )
                except Exception:
                    log.exception("Failed to start persistent admin MCP server %s", server_id)
    except Exception:
        log.exception("Failed to load persistent admin MCP servers")

    if LICENSE_KEY:
        get_license_data(app, LICENSE_KEY)

    # This should be blocking (sync) so functions are not deactivated on first /get_models calls
    # when the first user lands on the / route.
    log.info("Installing external dependencies of functions and tools...")
    await install_tool_and_function_dependencies()

    # A video job's worker is an in-process task, so a restart orphans anything
    # mid-flight. Fail those rows now: the UI reads job state from the DB, and a
    # row left "running" would spin forever with nothing driving it.
    try:
        from open_webui.models.videos import VideoJobs

        reclaimed = await VideoJobs.reclaim_stranded_jobs(max_age_seconds=0)
        if reclaimed:
            log.info("Reclaimed %s stranded video job(s) after restart", reclaimed)
    except Exception:
        log.debug("Video job reclaim failed", exc_info=True)

    app.state.redis = get_redis_connection(
        redis_url=REDIS_URL,
        redis_sentinels=get_sentinels_from_env(
            REDIS_SENTINEL_HOSTS, REDIS_SENTINEL_PORT
        ),
        redis_cluster=REDIS_CLUSTER,
        async_mode=True,
    )

    if app.state.redis is not None:
        app.state.redis_task_command_listener = asyncio.create_task(
            redis_task_command_listener(app)
        )

    # Periodic sweeper: recover chats whose message-queue drain marker was set
    # but whose generation died before clearing it (worker crash between marking
    # and the terminal handler). Runs for BOTH multi-worker (enumerates the Redis
    # draining_chats set) AND single-worker (enumerates draining chats from the
    # DB) — without it a single-worker crash mid-drain wedged the queue forever
    # (the stale marker's response id matches no future completion, so every
    # later drain's ownership guard bails). The per-chat orphan test inside
    # (started_at grace + no live task + no active stream) prevents reclaiming a
    # genuinely live drain during normal operation.
    async def _queue_drain_sweeper():
        from open_webui.utils.chat_queue import (
            sweep_orphaned_drains,
            sweep_pending_queues,
        )

        while True:
            try:
                await asyncio.sleep(15)
                # Order matters: unwedge dead drain markers FIRST, so a chat
                # freed by that is already eligible when the reconciler looks
                # at it a line later instead of waiting another tick.
                await sweep_orphaned_drains(app)
                # The reconciler proper: any chat that still has queued items
                # with nothing running gets the drain its completion event never
                # delivered. This is what makes "queue it and close the app"
                # true — a missed trigger (restart mid-turn, contended lock,
                # completion down an unexpected path) costs one sweep interval
                # instead of costing the message.
                await sweep_pending_queues(app)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("queue drain sweeper iteration failed")

    app.state.queue_drain_sweeper = asyncio.create_task(_queue_drain_sweeper())

    # Automations scheduler: the only thing that ever fires a scheduled task.
    # Same shape as the queue sweeper (in-process loop, no external scheduler)
    # and multi-worker safe without one — each due automation is claimed by a
    # compare-and-set on its own next_run_at, so exactly one worker fires it.
    # The flag is read live so an admin toggling it takes effect on the next
    # tick rather than at the next restart.
    async def _automation_scheduler():
        from open_webui.utils.automation_runner import sweep_due_automations

        while True:
            try:
                await asyncio.sleep(AUTOMATION_SWEEP_INTERVAL_SECONDS)
                if not app.state.config.ENABLE_AUTOMATIONS:
                    continue
                await sweep_due_automations(app)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("automation scheduler iteration failed")

    app.state.automation_scheduler = asyncio.create_task(_automation_scheduler())

    # Web push needs a stable VAPID keypair: rotating it silently invalidates
    # every existing browser subscription, so mint one ONCE and persist it.
    if app.state.config.ENABLE_AUTOMATIONS and not app.state.config.WEBPUSH_VAPID_PRIVATE_KEY:
        from open_webui.utils.webpush import generate_vapid_keys

        public_key, private_key = generate_vapid_keys()
        await app.state.config.set_async("WEBPUSH_VAPID_PUBLIC_KEY", public_key)
        await app.state.config.set_async("WEBPUSH_VAPID_PRIVATE_KEY", private_key)
        log.info("Generated a VAPID keypair for web push notifications")

    if THREAD_POOL_SIZE and THREAD_POOL_SIZE > 0:
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = THREAD_POOL_SIZE

    if PROFILE_LOOP_LAG:
        from open_webui.utils.loop_lag import start_loop_lag_monitor

        app.state.loop_lag_monitor = start_loop_lag_monitor(
            interval=PROFILE_LOOP_LAG_INTERVAL,
            window_seconds=PROFILE_LOOP_LAG_WINDOW_SECONDS,
        )

    # Keep-fresh sweep for chat-search message embeddings (semantic search). Decoupled
    # from the sync write path; embeds new/changed messages on an interval. No-op when
    # ENABLE_CHAT_SEMANTIC_SEARCH is off or there's nothing stale.
    from open_webui.utils.chat_embedder import embedding_sweeper_loop

    app.state.chat_embedding_sweeper = asyncio.create_task(embedding_sweeper_loop())

    # Periodic OpenRouter price-catalog sync. Populates model_pricing_catalog so
    # admin cost views can price models. First run is ~immediate to fill an empty
    # catalog on boot; thereafter every PRICING_SYNC_INTERVAL_HOURS. In-process
    # asyncio loop (no external scheduler), mirroring the queue-drain sweeper.
    async def _pricing_sync_loop():
        from open_webui.utils.pricing import run_pricing_sync
        from open_webui.utils import openrouter_reasoning

        # small initial delay so boot isn't blocked on an external HTTP call
        first = True
        while True:
            try:
                if not first:
                    interval = app.state.config.PRICING_SYNC_INTERVAL_HOURS or 12
                    await asyncio.sleep(max(1, int(interval)) * 3600)
                else:
                    await asyncio.sleep(5)
                    first = False
                if app.state.config.ENABLE_PRICING_SYNC:
                    await run_pricing_sync()
                # Warm the reasoning-discovery cache on the same cadence. The
                # pricing sync already fed it from its catalog fetch; this force
                # refresh is a cheap backstop when pricing sync is disabled.
                if app.state.config.ENABLE_OPENROUTER_REASONING_DISCOVERY:
                    if openrouter_reasoning.cache_is_cold():
                        await openrouter_reasoning.get_reasoning_map(force=True)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("pricing sync iteration failed")
                await asyncio.sleep(3600)

    app.state.pricing_sync_loop = asyncio.create_task(_pricing_sync_loop())

    # Subscription-provider usage poller (percentage windows for connections
    # flagged `subscription_usage`). Keeps an in-memory snapshot fresh and
    # pushes `subscription-usage:update` only when the numbers actually move;
    # skips fetches entirely while no sessions are connected.
    from open_webui.utils.subscription_usage import subscription_usage_poller

    app.state.subscription_usage_poller = asyncio.create_task(
        subscription_usage_poller(app)
    )

    # Persistent HTTP session with connection pooling — avoids the cost of
    # creating a new aiohttp.ClientSession (TLS handshake, DNS, connector
    # init) for every outgoing LLM request. Shared across all routes.
    app.state.http_session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            limit=500,
            limit_per_host=200,
            ttl_dns_cache=600,
            use_dns_cache=True,
            keepalive_timeout=60,
            enable_cleanup_closed=True,
        ),
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT),
        trust_env=True,
    )

    if app.state.config.ENABLE_BASE_MODELS_CACHE:
        await get_all_models(
            Request(
                # Creating a mock request object to pass to get_all_models
                {
                    "type": "http",
                    "asgi.version": "3.0",
                    "asgi.spec_version": "2.0",
                    "method": "GET",
                    "path": "/internal",
                    "query_string": b"",
                    "headers": Headers({}).raw,
                    "client": ("127.0.0.1", 12345),
                    "server": ("127.0.0.1", 80),
                    "scheme": "http",
                    "app": app,
                }
            ),
            None,
        )

    yield

    try:
        shutdown_results = await cancel_all_local_tasks(timeout=60.0)
        timed_out = [r for r in shutdown_results if r.get("status") == "timeout"]
        if timed_out:
            log.warning(f"Timed out waiting for tasks during shutdown: {timed_out}")
    except Exception as e:
        log.exception(f"Error cancelling active tasks during shutdown: {e}")

    await app.state.persistent_mcp.close()

    if hasattr(app.state, "http_session"):
        await app.state.http_session.close()

    if hasattr(app.state, "redis_task_command_listener"):
        app.state.redis_task_command_listener.cancel()

    if hasattr(app.state, "queue_drain_sweeper"):
        app.state.queue_drain_sweeper.cancel()

    if hasattr(app.state, "automation_scheduler"):
        app.state.automation_scheduler.cancel()

    if hasattr(app.state, "pricing_sync_loop"):
        app.state.pricing_sync_loop.cancel()

    if hasattr(app.state, "chat_embedding_sweeper"):
        app.state.chat_embedding_sweeper.cancel()

    if getattr(app.state, "loop_lag_monitor", None) is not None:
        app.state.loop_lag_monitor.cancel()

    try:
        await dispose_engine()
    except Exception as e:
        log.exception(f"Error during database teardown: {e}")


app = FastAPI(
    title=WEBUI_NAME,
    docs_url="/docs" if ENV == "dev" else None,
    openapi_url="/openapi.json" if ENV == "dev" else None,
    redoc_url=None,
    lifespan=lifespan,
)

# For Open WebUI OIDC/OAuth2
oauth_manager = OAuthManager(app)
app.state.oauth_manager = oauth_manager

# For Integrations
oauth_client_manager = OAuthClientManager(app)
app.state.oauth_client_manager = oauth_client_manager

app.state.instance_id = None
app.state.config = AppConfig(
    redis_url=REDIS_URL,
    redis_sentinels=get_sentinels_from_env(REDIS_SENTINEL_HOSTS, REDIS_SENTINEL_PORT),
    redis_cluster=REDIS_CLUSTER,
    redis_key_prefix=REDIS_KEY_PREFIX,
)
app.state.redis = None

app.state.WEBUI_NAME = WEBUI_NAME
app.state.LICENSE_METADATA = None


########################################
#
# OPENTELEMETRY
#
########################################

if ENABLE_OTEL:
    from open_webui.utils.telemetry.setup import setup as setup_opentelemetry

    setup_opentelemetry(app=app, db_engine=engine)


########################################
#
# OLLAMA
#
########################################


app.state.config.ENABLE_OLLAMA_API = ENABLE_OLLAMA_API
app.state.config.OLLAMA_BASE_URLS = OLLAMA_BASE_URLS
app.state.config.OLLAMA_API_CONFIGS = OLLAMA_API_CONFIGS

app.state.OLLAMA_MODELS = {}

########################################
#
# OPENAI
#
########################################

app.state.config.ENABLE_OPENAI_API = ENABLE_OPENAI_API
app.state.config.ENABLE_API_DEBUG_LOGGING = ENABLE_API_DEBUG_LOGGING
app.state.config.OPENAI_API_BASE_URLS = OPENAI_API_BASE_URLS
app.state.config.OPENAI_API_KEYS = OPENAI_API_KEYS
app.state.config.OPENAI_API_CONFIGS = OPENAI_API_CONFIGS

app.state.OPENAI_MODELS = {}

########################################
#
# TOOL SERVERS
#
########################################

app.state.config.TOOL_SERVER_CONNECTIONS = TOOL_SERVER_CONNECTIONS
# Was defined in config.py but never registered here, so every read of
# app.state.config.MCP_TOOL_CALL_TIMEOUT raised AttributeError -- which 500'd
# GET/POST /api/v1/configs/tool_servers and made the whole Admin -> External
# Tools page unloadable.
app.state.config.MCP_TOOL_CALL_TIMEOUT = MCP_TOOL_CALL_TIMEOUT
app.state.TOOL_SERVERS = []

########################################
#
# CONTAINER WORKSPACE
#
########################################

app.state.config.ENABLE_CONTAINER_WORKSPACE_SYNC = ENABLE_CONTAINER_WORKSPACE_SYNC
app.state.config.CONTAINER_DATA_ROOT = CONTAINER_DATA_ROOT
app.state.config.CONTAINER_MCP_SERVER_ID = CONTAINER_MCP_SERVER_ID
app.state.config.CONTAINER_SYSTEM_PROMPT = CONTAINER_SYSTEM_PROMPT

########################################
#
# DIRECT CONNECTIONS
#
########################################

app.state.config.ENABLE_DIRECT_CONNECTIONS = ENABLE_DIRECT_CONNECTIONS

########################################
#
# SCIM
#
########################################

app.state.SCIM_ENABLED = SCIM_ENABLED
app.state.SCIM_TOKEN = SCIM_TOKEN

########################################
#
# MODELS
#
########################################

app.state.config.ENABLE_BASE_MODELS_CACHE = ENABLE_BASE_MODELS_CACHE
app.state.config.ENABLE_PRICING_SYNC = ENABLE_PRICING_SYNC
app.state.config.PRICING_SYNC_INTERVAL_HOURS = PRICING_SYNC_INTERVAL_HOURS
app.state.config.ENABLE_OPENROUTER_REASONING_DISCOVERY = (
    ENABLE_OPENROUTER_REASONING_DISCOVERY
)
app.state.BASE_MODELS = []
app.state.BASE_MODELS_LOADED = False

########################################
#
# WEBUI
#
########################################

app.state.config.WEBUI_URL = WEBUI_URL
app.state.config.ENABLE_SIGNUP = ENABLE_SIGNUP
app.state.config.ENABLE_LOGIN_FORM = ENABLE_LOGIN_FORM

app.state.config.ENABLE_API_KEY = ENABLE_API_KEY
app.state.config.ENABLE_API_KEY_ENDPOINT_RESTRICTIONS = (
    ENABLE_API_KEY_ENDPOINT_RESTRICTIONS
)
app.state.config.API_KEY_ALLOWED_ENDPOINTS = API_KEY_ALLOWED_ENDPOINTS

app.state.config.JWT_EXPIRES_IN = JWT_EXPIRES_IN

app.state.config.SHOW_ADMIN_DETAILS = SHOW_ADMIN_DETAILS
app.state.config.ADMIN_EMAIL = ADMIN_EMAIL


app.state.config.DEFAULT_MODELS = DEFAULT_MODELS
app.state.config.DEFAULT_PROMPT_SUGGESTIONS = DEFAULT_PROMPT_SUGGESTIONS
app.state.config.DEFAULT_USER_ROLE = DEFAULT_USER_ROLE

app.state.config.PENDING_USER_OVERLAY_CONTENT = PENDING_USER_OVERLAY_CONTENT
app.state.config.PENDING_USER_OVERLAY_TITLE = PENDING_USER_OVERLAY_TITLE

app.state.config.RESPONSE_WATERMARK = RESPONSE_WATERMARK

app.state.config.USER_PERMISSIONS = USER_PERMISSIONS
app.state.config.WEBHOOK_URL = WEBHOOK_URL
app.state.config.BANNERS = WEBUI_BANNERS
app.state.config.MODEL_ORDER_LIST = MODEL_ORDER_LIST


app.state.config.ENABLE_CHANNELS = ENABLE_CHANNELS
app.state.config.ENABLE_NOTES = ENABLE_NOTES
app.state.config.ENABLE_COMMUNITY_SHARING = ENABLE_COMMUNITY_SHARING
app.state.config.ENABLE_MESSAGE_RATING = ENABLE_MESSAGE_RATING
app.state.config.ENABLE_USER_WEBHOOKS = ENABLE_USER_WEBHOOKS

app.state.config.ENABLE_EVALUATION_ARENA_MODELS = ENABLE_EVALUATION_ARENA_MODELS
app.state.config.EVALUATION_ARENA_MODELS = EVALUATION_ARENA_MODELS

app.state.config.OAUTH_USERNAME_CLAIM = OAUTH_USERNAME_CLAIM
app.state.config.OAUTH_PICTURE_CLAIM = OAUTH_PICTURE_CLAIM
app.state.config.OAUTH_EMAIL_CLAIM = OAUTH_EMAIL_CLAIM

app.state.config.ENABLE_OAUTH_ROLE_MANAGEMENT = ENABLE_OAUTH_ROLE_MANAGEMENT
app.state.config.OAUTH_ROLES_CLAIM = OAUTH_ROLES_CLAIM
app.state.config.OAUTH_ALLOWED_ROLES = OAUTH_ALLOWED_ROLES
app.state.config.OAUTH_ADMIN_ROLES = OAUTH_ADMIN_ROLES

app.state.config.ENABLE_LDAP = ENABLE_LDAP
app.state.config.LDAP_SERVER_LABEL = LDAP_SERVER_LABEL
app.state.config.LDAP_SERVER_HOST = LDAP_SERVER_HOST
app.state.config.LDAP_SERVER_PORT = LDAP_SERVER_PORT
app.state.config.LDAP_ATTRIBUTE_FOR_MAIL = LDAP_ATTRIBUTE_FOR_MAIL
app.state.config.LDAP_ATTRIBUTE_FOR_USERNAME = LDAP_ATTRIBUTE_FOR_USERNAME
app.state.config.LDAP_APP_DN = LDAP_APP_DN
app.state.config.LDAP_APP_PASSWORD = LDAP_APP_PASSWORD
app.state.config.LDAP_SEARCH_BASE = LDAP_SEARCH_BASE
app.state.config.LDAP_SEARCH_FILTERS = LDAP_SEARCH_FILTERS
app.state.config.LDAP_USE_TLS = LDAP_USE_TLS
app.state.config.LDAP_CA_CERT_FILE = LDAP_CA_CERT_FILE
app.state.config.LDAP_VALIDATE_CERT = LDAP_VALIDATE_CERT
app.state.config.LDAP_CIPHERS = LDAP_CIPHERS

# For LDAP Group Management
app.state.config.ENABLE_LDAP_GROUP_MANAGEMENT = ENABLE_LDAP_GROUP_MANAGEMENT
app.state.config.ENABLE_LDAP_GROUP_CREATION = ENABLE_LDAP_GROUP_CREATION
app.state.config.LDAP_ATTRIBUTE_FOR_GROUPS = LDAP_ATTRIBUTE_FOR_GROUPS


app.state.AUTH_TRUSTED_EMAIL_HEADER = WEBUI_AUTH_TRUSTED_EMAIL_HEADER
app.state.AUTH_TRUSTED_NAME_HEADER = WEBUI_AUTH_TRUSTED_NAME_HEADER
app.state.WEBUI_AUTH_SIGNOUT_REDIRECT_URL = WEBUI_AUTH_SIGNOUT_REDIRECT_URL
app.state.EXTERNAL_PWA_MANIFEST_URL = EXTERNAL_PWA_MANIFEST_URL

app.state.USER_COUNT = None

app.state.TOOLS = {}
app.state.TOOL_CONTENTS = {}

app.state.FUNCTIONS = {}
app.state.FUNCTION_CONTENTS = {}

########################################
#
# RETRIEVAL
#
########################################


app.state.config.TOP_K = RAG_TOP_K
app.state.config.TOP_K_RERANKER = RAG_TOP_K_RERANKER
app.state.config.RELEVANCE_THRESHOLD = RAG_RELEVANCE_THRESHOLD
app.state.config.HYBRID_BM25_WEIGHT = RAG_HYBRID_BM25_WEIGHT


app.state.config.ALLOWED_FILE_EXTENSIONS = RAG_ALLOWED_FILE_EXTENSIONS
app.state.config.FILE_MAX_SIZE = RAG_FILE_MAX_SIZE
app.state.config.FILE_MAX_COUNT = RAG_FILE_MAX_COUNT
app.state.config.FILE_IMAGE_COMPRESSION_WIDTH = FILE_IMAGE_COMPRESSION_WIDTH
app.state.config.FILE_IMAGE_COMPRESSION_HEIGHT = FILE_IMAGE_COMPRESSION_HEIGHT
app.state.config.IMAGE_PROVIDER_COMPRESSION_ENABLED = IMAGE_PROVIDER_COMPRESSION_ENABLED
app.state.config.IMAGE_PROVIDER_COMPRESSION_QUALITY = IMAGE_PROVIDER_COMPRESSION_QUALITY
app.state.config.IMAGE_PROVIDER_COMPRESSION_MIN_BYTES = (
    IMAGE_PROVIDER_COMPRESSION_MIN_BYTES
)
app.state.config.IMAGE_PROVIDER_MAX_DIMENSION = IMAGE_PROVIDER_MAX_DIMENSION


app.state.config.RAG_FULL_CONTEXT = RAG_FULL_CONTEXT
app.state.config.BYPASS_EMBEDDING_AND_RETRIEVAL = BYPASS_EMBEDDING_AND_RETRIEVAL
app.state.config.ENABLE_RAG_HYBRID_SEARCH = ENABLE_RAG_HYBRID_SEARCH

app.state.config.CONTENT_EXTRACTION_ENGINE = CONTENT_EXTRACTION_ENGINE
app.state.config.DATALAB_MARKER_API_KEY = DATALAB_MARKER_API_KEY
app.state.config.DATALAB_MARKER_API_BASE_URL = DATALAB_MARKER_API_BASE_URL
app.state.config.DATALAB_MARKER_ADDITIONAL_CONFIG = DATALAB_MARKER_ADDITIONAL_CONFIG
app.state.config.DATALAB_MARKER_SKIP_CACHE = DATALAB_MARKER_SKIP_CACHE
app.state.config.DATALAB_MARKER_FORCE_OCR = DATALAB_MARKER_FORCE_OCR
app.state.config.DATALAB_MARKER_PAGINATE = DATALAB_MARKER_PAGINATE
app.state.config.DATALAB_MARKER_STRIP_EXISTING_OCR = DATALAB_MARKER_STRIP_EXISTING_OCR
app.state.config.DATALAB_MARKER_DISABLE_IMAGE_EXTRACTION = (
    DATALAB_MARKER_DISABLE_IMAGE_EXTRACTION
)
app.state.config.DATALAB_MARKER_FORMAT_LINES = DATALAB_MARKER_FORMAT_LINES
app.state.config.DATALAB_MARKER_USE_LLM = DATALAB_MARKER_USE_LLM
app.state.config.DATALAB_MARKER_OUTPUT_FORMAT = DATALAB_MARKER_OUTPUT_FORMAT
app.state.config.EXTERNAL_DOCUMENT_LOADER_URL = EXTERNAL_DOCUMENT_LOADER_URL
app.state.config.EXTERNAL_DOCUMENT_LOADER_API_KEY = EXTERNAL_DOCUMENT_LOADER_API_KEY
app.state.config.TIKA_SERVER_URL = TIKA_SERVER_URL
app.state.config.DOCLING_SERVER_URL = DOCLING_SERVER_URL
app.state.config.DOCLING_PARAMS = DOCLING_PARAMS
app.state.config.DOCLING_DO_OCR = DOCLING_DO_OCR
app.state.config.DOCLING_FORCE_OCR = DOCLING_FORCE_OCR
app.state.config.DOCLING_OCR_ENGINE = DOCLING_OCR_ENGINE
app.state.config.DOCLING_OCR_LANG = DOCLING_OCR_LANG
app.state.config.DOCLING_TABLE_MODE = DOCLING_TABLE_MODE
app.state.config.DOCLING_PIPELINE = DOCLING_PIPELINE
app.state.config.DOCLING_DO_PICTURE_DESCRIPTION = DOCLING_DO_PICTURE_DESCRIPTION
app.state.config.DOCLING_PICTURE_DESCRIPTION_MODE = DOCLING_PICTURE_DESCRIPTION_MODE
app.state.config.DOCLING_PICTURE_DESCRIPTION_LOCAL = DOCLING_PICTURE_DESCRIPTION_LOCAL
app.state.config.DOCLING_PICTURE_DESCRIPTION_API = DOCLING_PICTURE_DESCRIPTION_API
app.state.config.DOCUMENT_INTELLIGENCE_ENDPOINT = DOCUMENT_INTELLIGENCE_ENDPOINT
app.state.config.DOCUMENT_INTELLIGENCE_KEY = DOCUMENT_INTELLIGENCE_KEY
app.state.config.MINERU_API_MODE = MINERU_API_MODE
app.state.config.MINERU_API_URL = MINERU_API_URL
app.state.config.MINERU_API_KEY = MINERU_API_KEY
app.state.config.MINERU_PARAMS = MINERU_PARAMS

app.state.config.TEXT_SPLITTER = RAG_TEXT_SPLITTER
app.state.config.TIKTOKEN_ENCODING_NAME = TIKTOKEN_ENCODING_NAME

app.state.config.CHUNK_SIZE = CHUNK_SIZE
app.state.config.CHUNK_OVERLAP = CHUNK_OVERLAP

app.state.config.RAG_EMBEDDING_ENGINE = RAG_EMBEDDING_ENGINE
app.state.config.RAG_EMBEDDING_MODEL = RAG_EMBEDDING_MODEL
app.state.config.RAG_EMBEDDING_BATCH_SIZE = RAG_EMBEDDING_BATCH_SIZE

app.state.config.RAG_RERANKING_ENGINE = RAG_RERANKING_ENGINE
app.state.config.RAG_RERANKING_MODEL = RAG_RERANKING_MODEL
app.state.config.RAG_EXTERNAL_RERANKER_URL = RAG_EXTERNAL_RERANKER_URL
app.state.config.RAG_EXTERNAL_RERANKER_API_KEY = RAG_EXTERNAL_RERANKER_API_KEY

app.state.config.RAG_TEMPLATE = RAG_TEMPLATE

app.state.config.RAG_OPENAI_API_BASE_URL = RAG_OPENAI_API_BASE_URL
app.state.config.RAG_OPENAI_API_KEY = RAG_OPENAI_API_KEY

app.state.config.RAG_AZURE_OPENAI_BASE_URL = RAG_AZURE_OPENAI_BASE_URL
app.state.config.RAG_AZURE_OPENAI_API_KEY = RAG_AZURE_OPENAI_API_KEY
app.state.config.RAG_AZURE_OPENAI_API_VERSION = RAG_AZURE_OPENAI_API_VERSION

app.state.config.RAG_OLLAMA_BASE_URL = RAG_OLLAMA_BASE_URL
app.state.config.RAG_OLLAMA_API_KEY = RAG_OLLAMA_API_KEY

# LibreOffice availability for the "Convert to PDF" attachment mode. Probed once
# at startup; the frontend hides the PDF mode option when this is False so users
# don't get failure toasts after the fact.
import shutil as _shutil

_libreoffice_bin = _shutil.which("libreoffice") or _shutil.which("soffice")
app.state.LIBREOFFICE_BIN = _libreoffice_bin
app.state.PDF_CONVERSION_AVAILABLE = _libreoffice_bin is not None
if _libreoffice_bin:
    log.info(
        "LibreOffice detected at %s; 'Convert to PDF' attachment mode enabled.",
        _libreoffice_bin,
    )
else:
    log.warning(
        "LibreOffice not found on PATH (looked for 'libreoffice' and 'soffice'). "
        "'Convert to PDF' attachment mode is disabled. Install LibreOffice to enable."
    )

app.state.config.YOUTUBE_LOADER_LANGUAGE = YOUTUBE_LOADER_LANGUAGE
app.state.config.YOUTUBE_LOADER_PROXY_URL = YOUTUBE_LOADER_PROXY_URL


# Web Search (Exa search + Jina Reader fetch)
app.state.config.ENABLE_WEB_SEARCH = ENABLE_WEB_SEARCH
app.state.config.EXA_API_KEY = EXA_API_KEY
app.state.config.EXA_API_KEY_2 = EXA_API_KEY_2
app.state.config.EXA_KEY_STATUS = EXA_KEY_STATUS
app.state.config.EXA_SEARCH_NUM_RESULTS = EXA_SEARCH_NUM_RESULTS
app.state.config.EXA_SEARCH_TYPE = EXA_SEARCH_TYPE
app.state.config.EXA_INCLUDE_DOMAINS = EXA_INCLUDE_DOMAINS
app.state.config.EXA_EXCLUDE_DOMAINS = EXA_EXCLUDE_DOMAINS
app.state.config.JINA_API_KEY = JINA_API_KEY
app.state.config.JINA_READER_API_BASE_URL = JINA_READER_API_BASE_URL
app.state.config.JINA_READER_TOKEN_USAGE = JINA_READER_TOKEN_USAGE
app.state.config.JINA_READER_VIEWPORT_WIDTH = JINA_READER_VIEWPORT_WIDTH
app.state.config.JINA_READER_VIEWPORT_HEIGHT = JINA_READER_VIEWPORT_HEIGHT
app.state.config.JINA_READER_TIMEOUT = JINA_READER_TIMEOUT
app.state.config.EXA_CONTENTS_MAX_CHARACTERS = EXA_CONTENTS_MAX_CHARACTERS
app.state.config.EXA_CONTENTS_LIVECRAWL = EXA_CONTENTS_LIVECRAWL
app.state.config.WEB_SEARCH_SYSTEM_PROMPT = WEB_SEARCH_SYSTEM_PROMPT

app.state.config.ENABLE_STUDY_MODE = ENABLE_STUDY_MODE
app.state.config.STUDY_MODE_SYSTEM_PROMPT = STUDY_MODE_SYSTEM_PROMPT

# Video inputs
app.state.config.ENABLE_VIDEO_INPUT = ENABLE_VIDEO_INPUT
app.state.config.ENABLE_VIDEO_URL_INGEST = ENABLE_VIDEO_URL_INGEST
app.state.config.VIDEO_DEFAULT_FPS = VIDEO_DEFAULT_FPS
app.state.config.VIDEO_DEFAULT_QUALITY = VIDEO_DEFAULT_QUALITY
app.state.config.VIDEO_DEFAULT_AUDIO = VIDEO_DEFAULT_AUDIO
app.state.config.VIDEO_MAX_SOURCE_SIZE_MB = VIDEO_MAX_SOURCE_SIZE_MB
app.state.config.VIDEO_WARN_DURATION_SECONDS = VIDEO_WARN_DURATION_SECONDS

# Automations
app.state.config.ENABLE_AUTOMATIONS = ENABLE_AUTOMATIONS
app.state.config.AUTOMATIONS_MAX_ACTIVE_PER_USER = AUTOMATIONS_MAX_ACTIVE_PER_USER
app.state.config.WEBPUSH_VAPID_PUBLIC_KEY = WEBPUSH_VAPID_PUBLIC_KEY
app.state.config.WEBPUSH_VAPID_PRIVATE_KEY = WEBPUSH_VAPID_PRIVATE_KEY

# Data Visualization
app.state.config.ENABLE_DATA_VIZ = ENABLE_DATA_VIZ
app.state.config.DATA_VIZ_SHARED_CORE_PROMPT = DATA_VIZ_SHARED_CORE_PROMPT
app.state.config.DATA_VIZ_MODULE_DIAGRAM_ENABLED = DATA_VIZ_MODULE_DIAGRAM_ENABLED
app.state.config.DATA_VIZ_MODULE_DIAGRAM_PROMPT = DATA_VIZ_MODULE_DIAGRAM_PROMPT
app.state.config.DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_ENABLED = (
    DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_ENABLED
)
app.state.config.DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_PROMPT = (
    DATA_VIZ_MODULE_MOCKUP_INTERACTIVE_PROMPT
)
app.state.config.DATA_VIZ_MODULE_CHART_DATAVIZ_ENABLED = (
    DATA_VIZ_MODULE_CHART_DATAVIZ_ENABLED
)
app.state.config.DATA_VIZ_MODULE_CHART_DATAVIZ_PROMPT = (
    DATA_VIZ_MODULE_CHART_DATAVIZ_PROMPT
)
app.state.config.DATA_VIZ_MODULE_ART_ENABLED = DATA_VIZ_MODULE_ART_ENABLED
app.state.config.DATA_VIZ_MODULE_ART_PROMPT = DATA_VIZ_MODULE_ART_PROMPT
app.state.config.DATA_VIZ_AUTO_REPAIR_ENABLED = DATA_VIZ_AUTO_REPAIR_ENABLED
app.state.config.DATA_VIZ_AUTO_REPAIR_MAX_ATTEMPTS = DATA_VIZ_AUTO_REPAIR_MAX_ATTEMPTS
app.state.config.DATA_VIZ_AUTO_REPAIR_MODEL = DATA_VIZ_AUTO_REPAIR_MODEL
app.state.config.DATA_VIZ_AUTO_REPAIR_REASONING_EFFORT = (
    DATA_VIZ_AUTO_REPAIR_REASONING_EFFORT
)

# Chat semantic search (message embeddings). Bridge the persisted values into the
# chat_embedder module's live globals so the background sweep + query path use the
# admin-configured URL/model/enable flag (POST /configs/chat_embedding re-applies).
app.state.config.ENABLE_CHAT_SEMANTIC_SEARCH = ENABLE_CHAT_SEMANTIC_SEARCH
app.state.config.CHAT_EMBED_URL = CHAT_EMBED_URL
app.state.config.CHAT_EMBED_MODEL = CHAT_EMBED_MODEL
app.state.config.CHAT_EMBED_SWEEP_INTERVAL = CHAT_EMBED_SWEEP_INTERVAL
app.state.config.CHAT_EMBED_TEXT_BATCH = CHAT_EMBED_TEXT_BATCH

from open_webui.utils.chat_embedder import (
    apply_runtime_config as _apply_chat_embed_config,
)

_apply_chat_embed_config(
    url=app.state.config.CHAT_EMBED_URL,
    model=app.state.config.CHAT_EMBED_MODEL,
    enabled=app.state.config.ENABLE_CHAT_SEMANTIC_SEARCH,
    sweep_interval=app.state.config.CHAT_EMBED_SWEEP_INTERVAL,
    text_batch=app.state.config.CHAT_EMBED_TEXT_BATCH,
)

# Subagents
app.state.config.ENABLE_SUBAGENTS = ENABLE_SUBAGENTS
app.state.config.SUBAGENT_DEFAULT_MODEL = SUBAGENT_DEFAULT_MODEL
app.state.config.SUBAGENT_CONTEXT_FALLBACK_MODEL = SUBAGENT_CONTEXT_FALLBACK_MODEL
app.state.config.SUBAGENT_SYSTEM_PROMPT = SUBAGENT_SYSTEM_PROMPT
app.state.config.SUBAGENT_SYSTEM_PROMPT_APPEND = SUBAGENT_SYSTEM_PROMPT_APPEND
app.state.config.SUBAGENT_PARENT_PROMPT = SUBAGENT_PARENT_PROMPT
app.state.config.SUBAGENT_DEFAULT_REASONING_EFFORT = SUBAGENT_DEFAULT_REASONING_EFFORT
app.state.config.SUBAGENT_DEFAULT_SERVICE_TIER = SUBAGENT_DEFAULT_SERVICE_TIER
app.state.config.SUBAGENT_ALLOW_EXTERNAL_TOOLS = SUBAGENT_ALLOW_EXTERNAL_TOOLS
app.state.config.SUBAGENT_EXTERNAL_TOOLS_PROMPT = SUBAGENT_EXTERNAL_TOOLS_PROMPT

app.state.config.ENABLE_ASK_USER = ENABLE_ASK_USER
app.state.config.ASK_USER_PARENT_PROMPT = ASK_USER_PARENT_PROMPT

# Flex auto-flip
app.state.config.FLEX_AUTO_FLIP_ENABLED = FLEX_AUTO_FLIP_ENABLED
app.state.config.FLEX_AUTO_FLIP_OFF_PEAK_START_HOUR = FLEX_AUTO_FLIP_OFF_PEAK_START_HOUR
app.state.config.FLEX_AUTO_FLIP_OFF_PEAK_END_HOUR = FLEX_AUTO_FLIP_OFF_PEAK_END_HOUR
app.state.config.FLEX_AUTO_FLIP_OFF_PEAK_TIMEZONE = FLEX_AUTO_FLIP_OFF_PEAK_TIMEZONE
app.state.config.FLEX_AUTO_FLIP_THRESHOLD_RATIO = FLEX_AUTO_FLIP_THRESHOLD_RATIO

app.state.config.ENABLE_GOOGLE_DRIVE_INTEGRATION = ENABLE_GOOGLE_DRIVE_INTEGRATION
app.state.config.ENABLE_ONEDRIVE_INTEGRATION = ENABLE_ONEDRIVE_INTEGRATION

app.state.EMBEDDING_FUNCTION = None
app.state.RERANKING_FUNCTION = None
app.state.ef = None
app.state.rf = None

app.state.YOUTUBE_LOADER_TRANSLATION = None


try:
    app.state.ef = get_ef(
        app.state.config.RAG_EMBEDDING_ENGINE,
        app.state.config.RAG_EMBEDDING_MODEL,
        RAG_EMBEDDING_MODEL_AUTO_UPDATE,
    )
    if (
        app.state.config.ENABLE_RAG_HYBRID_SEARCH
        and not app.state.config.BYPASS_EMBEDDING_AND_RETRIEVAL
    ):
        app.state.rf = get_rf(
            app.state.config.RAG_RERANKING_ENGINE,
            app.state.config.RAG_RERANKING_MODEL,
            app.state.config.RAG_EXTERNAL_RERANKER_URL,
            app.state.config.RAG_EXTERNAL_RERANKER_API_KEY,
            RAG_RERANKING_MODEL_AUTO_UPDATE,
        )
    else:
        app.state.rf = None
except Exception as e:
    log.error(f"Error updating models: {e}")
    pass


app.state.EMBEDDING_FUNCTION = get_embedding_function(
    app.state.config.RAG_EMBEDDING_ENGINE,
    app.state.config.RAG_EMBEDDING_MODEL,
    embedding_function=app.state.ef,
    url=(
        app.state.config.RAG_OPENAI_API_BASE_URL
        if app.state.config.RAG_EMBEDDING_ENGINE == "openai"
        else (
            app.state.config.RAG_OLLAMA_BASE_URL
            if app.state.config.RAG_EMBEDDING_ENGINE == "ollama"
            else app.state.config.RAG_AZURE_OPENAI_BASE_URL
        )
    ),
    key=(
        app.state.config.RAG_OPENAI_API_KEY
        if app.state.config.RAG_EMBEDDING_ENGINE == "openai"
        else (
            app.state.config.RAG_OLLAMA_API_KEY
            if app.state.config.RAG_EMBEDDING_ENGINE == "ollama"
            else app.state.config.RAG_AZURE_OPENAI_API_KEY
        )
    ),
    embedding_batch_size=app.state.config.RAG_EMBEDDING_BATCH_SIZE,
    azure_api_version=(
        app.state.config.RAG_AZURE_OPENAI_API_VERSION
        if app.state.config.RAG_EMBEDDING_ENGINE == "azure_openai"
        else None
    ),
)

app.state.RERANKING_FUNCTION = get_reranking_function(
    app.state.config.RAG_RERANKING_ENGINE,
    app.state.config.RAG_RERANKING_MODEL,
    reranking_function=app.state.rf,
)

########################################
#
# IMAGES
#
########################################

app.state.config.IMAGE_GENERATION_ENGINE = IMAGE_GENERATION_ENGINE
app.state.config.ENABLE_IMAGE_GENERATION = ENABLE_IMAGE_GENERATION
app.state.config.ENABLE_IMAGE_PROMPT_GENERATION = ENABLE_IMAGE_PROMPT_GENERATION

app.state.config.IMAGES_OPENAI_API_BASE_URL = IMAGES_OPENAI_API_BASE_URL
app.state.config.IMAGES_OPENAI_API_VERSION = IMAGES_OPENAI_API_VERSION
app.state.config.IMAGES_OPENAI_API_KEY = IMAGES_OPENAI_API_KEY

app.state.config.IMAGES_GEMINI_API_BASE_URL = IMAGES_GEMINI_API_BASE_URL
app.state.config.IMAGES_GEMINI_API_KEY = IMAGES_GEMINI_API_KEY

app.state.config.IMAGE_GENERATION_MODEL = IMAGE_GENERATION_MODEL

app.state.config.AUTOMATIC1111_BASE_URL = AUTOMATIC1111_BASE_URL
app.state.config.AUTOMATIC1111_API_AUTH = AUTOMATIC1111_API_AUTH
app.state.config.AUTOMATIC1111_CFG_SCALE = AUTOMATIC1111_CFG_SCALE
app.state.config.AUTOMATIC1111_SAMPLER = AUTOMATIC1111_SAMPLER
app.state.config.AUTOMATIC1111_SCHEDULER = AUTOMATIC1111_SCHEDULER
app.state.config.COMFYUI_BASE_URL = COMFYUI_BASE_URL
app.state.config.COMFYUI_API_KEY = COMFYUI_API_KEY
app.state.config.COMFYUI_WORKFLOW = COMFYUI_WORKFLOW
app.state.config.COMFYUI_WORKFLOW_NODES = COMFYUI_WORKFLOW_NODES

app.state.config.IMAGE_SIZE = IMAGE_SIZE
app.state.config.IMAGE_STEPS = IMAGE_STEPS


########################################
#
# AUDIO
#
########################################

app.state.config.STT_ENGINE = AUDIO_STT_ENGINE
app.state.config.STT_MODEL = AUDIO_STT_MODEL
app.state.config.STT_SUPPORTED_CONTENT_TYPES = AUDIO_STT_SUPPORTED_CONTENT_TYPES

app.state.config.STT_OPENAI_API_BASE_URL = AUDIO_STT_OPENAI_API_BASE_URL
app.state.config.STT_OPENAI_API_KEY = AUDIO_STT_OPENAI_API_KEY
app.state.config.STT_OPENROUTER_API_KEY = AUDIO_STT_OPENROUTER_API_KEY
app.state.config.STT_OPENROUTER_TEMPERATURE = AUDIO_STT_OPENROUTER_TEMPERATURE

app.state.config.WHISPER_MODEL = WHISPER_MODEL
app.state.config.WHISPER_VAD_FILTER = WHISPER_VAD_FILTER
app.state.config.DEEPGRAM_API_KEY = DEEPGRAM_API_KEY

app.state.config.AUDIO_STT_AZURE_API_KEY = AUDIO_STT_AZURE_API_KEY
app.state.config.AUDIO_STT_AZURE_REGION = AUDIO_STT_AZURE_REGION
app.state.config.AUDIO_STT_AZURE_LOCALES = AUDIO_STT_AZURE_LOCALES
app.state.config.AUDIO_STT_AZURE_BASE_URL = AUDIO_STT_AZURE_BASE_URL
app.state.config.AUDIO_STT_AZURE_MAX_SPEAKERS = AUDIO_STT_AZURE_MAX_SPEAKERS

app.state.config.TTS_ENGINE = AUDIO_TTS_ENGINE

app.state.config.TTS_MODEL = AUDIO_TTS_MODEL
app.state.config.TTS_VOICE = AUDIO_TTS_VOICE

app.state.config.TTS_OPENAI_API_BASE_URL = AUDIO_TTS_OPENAI_API_BASE_URL
app.state.config.TTS_OPENAI_API_KEY = AUDIO_TTS_OPENAI_API_KEY
app.state.config.TTS_OPENAI_PARAMS = AUDIO_TTS_OPENAI_PARAMS
app.state.config.TTS_OPENROUTER_API_KEY = AUDIO_TTS_OPENROUTER_API_KEY

app.state.config.TTS_API_KEY = AUDIO_TTS_API_KEY
app.state.config.TTS_SPLIT_ON = AUDIO_TTS_SPLIT_ON


app.state.config.TTS_AZURE_SPEECH_REGION = AUDIO_TTS_AZURE_SPEECH_REGION
app.state.config.TTS_AZURE_SPEECH_BASE_URL = AUDIO_TTS_AZURE_SPEECH_BASE_URL
app.state.config.TTS_AZURE_SPEECH_OUTPUT_FORMAT = AUDIO_TTS_AZURE_SPEECH_OUTPUT_FORMAT


app.state.faster_whisper_model = None
app.state.speech_synthesiser = None
app.state.speech_speaker_embeddings_dataset = None


########################################
#
# TASKS
#
########################################


app.state.config.TASK_MODEL = TASK_MODEL
app.state.config.TASK_MODEL_EXTERNAL = TASK_MODEL_EXTERNAL


app.state.config.ENABLE_SEARCH_QUERY_GENERATION = ENABLE_SEARCH_QUERY_GENERATION
app.state.config.ENABLE_RETRIEVAL_QUERY_GENERATION = ENABLE_RETRIEVAL_QUERY_GENERATION
app.state.config.ENABLE_AUTOCOMPLETE_GENERATION = ENABLE_AUTOCOMPLETE_GENERATION
app.state.config.ENABLE_TAGS_GENERATION = ENABLE_TAGS_GENERATION
app.state.config.ENABLE_TITLE_GENERATION = ENABLE_TITLE_GENERATION
app.state.config.TITLE_GENERATION_OVERRIDE = TITLE_GENERATION_OVERRIDE
app.state.config.TITLE_GENERATION_MODEL = TITLE_GENERATION_MODEL
app.state.config.ENABLE_FOLLOW_UP_GENERATION = ENABLE_FOLLOW_UP_GENERATION
app.state.config.FOLLOW_UP_GENERATION_OVERRIDE = FOLLOW_UP_GENERATION_OVERRIDE


app.state.config.TITLE_GENERATION_PROMPT_TEMPLATE = TITLE_GENERATION_PROMPT_TEMPLATE
app.state.config.TAGS_GENERATION_PROMPT_TEMPLATE = TAGS_GENERATION_PROMPT_TEMPLATE
app.state.config.IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE = (
    IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE
)
app.state.config.FOLLOW_UP_GENERATION_PROMPT_TEMPLATE = (
    FOLLOW_UP_GENERATION_PROMPT_TEMPLATE
)

app.state.config.TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE = (
    TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE
)
app.state.config.QUERY_GENERATION_PROMPT_TEMPLATE = QUERY_GENERATION_PROMPT_TEMPLATE
app.state.config.AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE = (
    AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE
)
app.state.config.AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH = (
    AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH
)


########################################
#
# WEBUI
#
########################################

app.state.MODELS = {}


class RedirectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Check if the request is a GET request
        if request.method == "GET":
            path = request.url.path
            query_params = dict(parse_qs(urlparse(str(request.url)).query))

            redirect_params = {}

            # Check for the specific watch path and the presence of 'v' parameter
            if path.endswith("/watch") and "v" in query_params:
                # Extract the first 'v' parameter
                youtube_video_id = query_params["v"][0]
                redirect_params["youtube"] = youtube_video_id

            if "shared" in query_params and len(query_params["shared"]) > 0:
                # PWA share_target support

                text = query_params["shared"][0]
                if text:
                    urls = re.match(r"https://\S+", text)
                    if urls:
                        from open_webui.retrieval.loaders.youtube import _parse_video_id

                        if youtube_video_id := _parse_video_id(urls[0]):
                            redirect_params["youtube"] = youtube_video_id
                        else:
                            redirect_params["load-url"] = urls[0]
                    else:
                        redirect_params["q"] = text

            if redirect_params:
                redirect_url = f"/?{urlencode(redirect_params)}"
                return RedirectResponse(url=redirect_url)

        # Proceed with the normal flow of other requests
        response = await call_next(request)
        return response


# Add the middleware to the app
if ENABLE_COMPRESSION_MIDDLEWARE:
    # Tuned for metered/slow clients: brotli q9 (much smaller than the q4 default
    # for the sizable JSON/HTML responses here) with a matching zstd level, gzip 6
    # as the fallback. text/event-stream is excluded by the library's streaming
    # content-type set, so SSE stays unbuffered. minimum_size stays at the 500B
    # default (tiny bodies don't benefit).
    app.add_middleware(
        CompressMiddleware,
        minimum_size=500,
        brotli_quality=9,
        zstd_level=9,
        gzip_level=6,
    )

app.add_middleware(RedirectMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


@app.middleware("http")
async def commit_session_after_request(request: Request, call_next):
    response = await call_next(request)
    return response


@app.middleware("http")
async def check_url(request: Request, call_next):
    start_time = int(time.time())
    request.state.token = get_http_authorization_cred(
        request.headers.get("Authorization")
    )

    request.state.enable_api_key = app.state.config.ENABLE_API_KEY
    response = await call_next(request)
    process_time = int(time.time()) - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response


@app.middleware("http")
async def inspect_websocket(request: Request, call_next):
    if (
        "/ws/socket.io" in request.url.path
        and request.query_params.get("transport") == "websocket"
    ):
        upgrade = (request.headers.get("Upgrade") or "").lower()
        connection = (request.headers.get("Connection") or "").lower().split(",")
        # Check that there's the correct headers for an upgrade, else reject the connection
        # This is to work around this upstream issue: https://github.com/miguelgrinberg/python-engineio/issues/367
        if upgrade != "websocket" or "upgrade" not in connection:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "Invalid WebSocket upgrade request"},
            )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGIN,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Without this, cross-origin JS (dev: vite on :5173 against :8080) can't
    # read the ETag response header and the conditional chat-open silently
    # never engages. Same-origin prod is unaffected either way.
    expose_headers=["ETag"],
)


app.mount("/ws", socket_app)


app.include_router(ollama.router, prefix="/ollama", tags=["ollama"])
app.include_router(openai.router, prefix="/openai", tags=["openai"])


app.include_router(pipelines.router, prefix="/api/v1/pipelines", tags=["pipelines"])
app.include_router(tasks.router, prefix="/api/v1/tasks", tags=["tasks"])
app.include_router(images.router, prefix="/api/v1/images", tags=["images"])
app.include_router(videos.router, prefix="/api/v1/videos", tags=["videos"])

app.include_router(audio.router, prefix="/api/v1/audio", tags=["audio"])
app.include_router(retrieval.router, prefix="/api/v1/retrieval", tags=["retrieval"])
app.include_router(subagents.router, prefix="/api/v1/subagents", tags=["subagents"])
app.include_router(
    automations.router, prefix="/api/v1/automations", tags=["automations"]
)
app.include_router(push.router, prefix="/api/v1/push", tags=["push"])
app.include_router(
    flex_auto_flip.router,
    prefix="/api/v1/flex-auto-flip",
    tags=["flex-auto-flip"],
)

app.include_router(configs.router, prefix="/api/v1/configs", tags=["configs"])

app.include_router(auths.router, prefix="/api/v1/auths", tags=["auths"])
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])


app.include_router(channels.router, prefix="/api/v1/channels", tags=["channels"])
app.include_router(chats.router, prefix="/api/v1/chats", tags=["chats"])
app.include_router(streams.router, prefix="/api/v1/streams", tags=["streams"])
app.include_router(bootstrap.router, prefix="/api/bootstrap", tags=["bootstrap"])
app.include_router(notes.router, prefix="/api/v1/notes", tags=["notes"])


app.include_router(models.router, prefix="/api/v1/models", tags=["models"])
app.include_router(mcp.router, prefix="/api/v1/mcp", tags=["mcp"])
app.include_router(mcp.oauth_router, prefix="/oauth/mcp", tags=["mcp-oauth"])
app.include_router(prompts.router, prefix="/api/v1/prompts", tags=["prompts"])
app.include_router(tools.router, prefix="/api/v1/tools", tags=["tools"])

app.include_router(folders.router, prefix="/api/v1/folders", tags=["folders"])
app.include_router(groups.router, prefix="/api/v1/groups", tags=["groups"])
app.include_router(files.router, prefix="/api/v1/files", tags=["files"])
app.include_router(functions.router, prefix="/api/v1/functions", tags=["functions"])
app.include_router(
    evaluations.router, prefix="/api/v1/evaluations", tags=["evaluations"]
)
app.include_router(utils.router, prefix="/api/v1/utils", tags=["utils"])

# Analytics API for token usage "Wrapped" feature
app.include_router(analytics.router, prefix="/api/v1/analytics", tags=["analytics"])

# SCIM 2.0 API for identity management
if SCIM_ENABLED:
    app.include_router(scim.router, prefix="/api/v1/scim/v2", tags=["scim"])


try:
    audit_level = AuditLevel(AUDIT_LOG_LEVEL)
except ValueError as e:
    logger.error(f"Invalid audit level: {AUDIT_LOG_LEVEL}. Error: {e}")
    audit_level = AuditLevel.NONE

if audit_level != AuditLevel.NONE:
    app.add_middleware(
        AuditLoggingMiddleware,
        audit_level=audit_level,
        excluded_paths=AUDIT_EXCLUDED_PATHS,
        max_body_size=MAX_BODY_LOG_SIZE,
    )
##################################
#
# Chat Endpoints
#
##################################


def _model_avatar_url(model_id, data_uri: str) -> str:
    """Client-facing URL for a data: model avatar, versioned by content hash.

    The base64 bytes live server-side; the list ships a tiny cacheable URL
    instead (see get_model_profile_image, which serves them with an ETag +
    immutable Cache-Control). ``v`` = sha256(data_uri)[:16] so a changed avatar
    busts the cache without a query on every list render.
    """
    v = hashlib.sha256(data_uri.encode("utf-8")).hexdigest()[:16]
    return "/api/v1/models/model/profile/image?" + urlencode(
        {"id": str(model_id), "v": v}
    )


def _project_model_for_client(model: dict) -> dict:
    """Trim a single model dict for the client-facing /api/models list.

    Behavior-safe payload reduction only: strips the raw upstream echo
    (``openai``), prunes the ``ollama`` echo to the few fields the client reads,
    drops server-enforced ``info.access_control``, and replaces inline base64
    ``profile_image_url`` data URIs with a cacheable endpoint URL. Never mutates
    the shared app.state objects — every nested dict it changes is copied first.
    ``info.params`` is intentionally kept (Chat.svelte reads custom_params /
    stream_response from it).
    """
    m = dict(model)

    # Raw upstream provider echo — never read by any client surface.
    m.pop("openai", None)

    # Prune the ollama echo to only the fields the client actually reads.
    ollama = m.get("ollama")
    if isinstance(ollama, dict):
        pruned: dict = {}
        details = ollama.get("details")
        if isinstance(details, dict):
            pruned_details = {
                k: details[k]
                for k in ("parameter_size", "quantization_level")
                if k in details
            }
            if pruned_details:
                pruned["details"] = pruned_details
        for k in ("size", "expires_at"):
            if k in ollama:
                pruned[k] = ollama[k]
        m["ollama"] = pruned

    model_id = m.get("id")

    info = m.get("info")
    if isinstance(info, dict):
        new_info = dict(info)
        # Access control is enforced server-side (get_filtered_models); the
        # workspace/admin editors read it from /api/v1/models/ instead.
        new_info.pop("access_control", None)
        meta = new_info.get("meta")
        if isinstance(meta, dict):
            img = meta.get("profile_image_url")
            if isinstance(img, str) and img.startswith("data:"):
                new_meta = dict(meta)
                new_meta["profile_image_url"] = _model_avatar_url(model_id, img)
                new_info["meta"] = new_meta
        m["info"] = new_info

    top_meta = m.get("meta")
    if isinstance(top_meta, dict):
        img = top_meta.get("profile_image_url")
        if isinstance(img, str) and img.startswith("data:"):
            new_meta = dict(top_meta)
            new_meta["profile_image_url"] = _model_avatar_url(model_id, img)
            m["meta"] = new_meta

    return m


@app.get("/api/models")
@app.get("/api/v1/models")  # Experimental: Compatibility with OpenAI API
async def get_models(
    request: Request, refresh: bool = False, user=Depends(get_verified_user)
):
    all_models = await get_all_models(request, refresh=refresh, user=user)

    models = []
    for model in all_models:
        # Filter out filter pipelines
        if "pipeline" in model and model["pipeline"].get("type", None) == "filter":
            continue

        try:
            model_tags = [
                tag.get("name")
                for tag in model.get("info", {}).get("meta", {}).get("tags", [])
            ]
            tags = [tag.get("name") for tag in model.get("tags", [])]

            tags = list(set(model_tags + tags))
            model["tags"] = [{"name": tag} for tag in tags]
        except Exception as e:
            log.debug(f"Error processing model tags: {e}")
            model["tags"] = []
            pass

        models.append(model)

    model_order_list = request.app.state.config.MODEL_ORDER_LIST
    if model_order_list:
        model_order_dict = {model_id: i for i, model_id in enumerate(model_order_list)}
        # Sort models by order list priority, with fallback for those not in the list
        models.sort(
            key=lambda model: (
                model_order_dict.get(model.get("id", ""), float("inf")),
                (model.get("name", "") or ""),
            )
        )

    models = await get_filtered_models(models, user)

    log.debug(
        f"/api/models returned filtered models accessible to the user: {json.dumps([model.get('id') for model in models])}"
    )

    # Trim the client-facing payload (base64 avatars, raw upstream echoes,
    # server-only fields) without touching app.state.MODELS / the full objects
    # server paths depend on.
    models = [_project_model_for_client(model) for model in models]

    return etag_response({"data": models}, request)


@app.get("/api/models/base")
async def get_base_models(
    request: Request, refresh: bool = False, user=Depends(get_admin_user)
):
    # The Admin Models page should display the same provider catalog the model
    # registry is already using. Re-querying every provider here made merely
    # opening the page block on the slowest/down connection. Only an explicit
    # refresh performs discovery; normal loads reuse the populated registry.
    catalog_loaded = getattr(
        request.app.state,
        "BASE_MODELS_LOADED",
        bool(request.app.state.BASE_MODELS),
    )
    models = request.app.state.BASE_MODELS if not refresh and catalog_loaded else None
    if models is None:
        models = await get_all_base_models_deduped(request, refresh=refresh, user=user)
        request.app.state.BASE_MODELS = models
        request.app.state.BASE_MODELS_LOADED = True
    return {"data": models}


##################################
# Embeddings
##################################


@app.post("/api/embeddings")
@app.post("/api/v1/embeddings")  # Experimental: Compatibility with OpenAI API
async def embeddings(
    request: Request, form_data: dict, user=Depends(get_verified_user)
):
    """
    OpenAI-compatible embeddings endpoint.

    This handler:
      - Performs user/model checks and dispatches to the correct backend.
      - Supports OpenAI, Ollama, arena models, pipelines, and any compatible provider.

    Args:
        request (Request): Request context.
        form_data (dict): OpenAI-like payload (e.g., {"model": "...", "input": [...]})
        user (UserModel): Authenticated user.

    Returns:
        dict: OpenAI-compatible embeddings response.
    """
    # Make sure models are loaded in app state
    if not request.app.state.MODELS:
        await get_all_models(request, user=user)
    # Use generic dispatcher in utils.embeddings
    return await generate_embeddings(request, form_data, user)


async def _chat_completion_impl(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
    *,
    generation_operation: Optional[dict] = None,
):
    if not request.app.state.MODELS:
        await get_all_models(request, user=user)

    # B10: detect v2.1 body shape (carries `leaf_message_id`, no `messages`).
    # Server walks the chat tree to assemble the canonical conversation. v1 body
    # (with `messages`) falls through to the existing pipeline unchanged.
    #
    # Cross-device prompt sync: assemble fills this with the freshly-persisted
    # user-message row (only on a genuinely new turn) so we can broadcast it to
    # other devices viewing this chat once `metadata` is known.
    persisted_user_out: dict = {}
    if (
        "leaf_message_id" in form_data
        and "messages" not in form_data
        and form_data.get("chat_id")
        and not str(form_data.get("chat_id", "")).startswith("local:")
    ):
        try:
            # Auth: caller must own the chat we're about to walk.
            if user and user.role != "admin":
                owned = await Chats.get_chat_by_id_and_user_id(
                    form_data["chat_id"], user.id
                )
                if owned is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=ERROR_MESSAGES.DEFAULT(),
                    )

            try:
                _subagent_work_state = await collect_chat_work_state(
                    request.app.state.redis, form_data["chat_id"]
                )
            except Exception:
                log.exception(
                    "subagent rerun task preflight failed for chat %s",
                    form_data["chat_id"],
                )
                _subagent_work_state = None
            _active_rerun_keys = (
                _subagent_work_state.get("subagent_rerun_entry_keys") or []
                if isinstance(_subagent_work_state, dict)
                else []
            )
            if _active_rerun_keys:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "message": (
                            "Wait for the active subagent redo to finish before "
                            "continuing the main chat."
                        ),
                        "code": "subagent_rerun_in_progress",
                        "entry_keys": list(dict.fromkeys(_active_rerun_keys)),
                    },
                )
            if isinstance(_subagent_work_state, dict):
                # Concurrent-turn ownership is enforced atomically by the
                # generation-operation registry before assembly. Unlike this
                # old task-list snapshot, that lease distinguishes a legitimate
                # multi-model sibling (same turn_id) from a second user turn and
                # also covers the pre-task registration window.
                try:
                    from open_webui.utils.subagent import (
                        reconcile_stranded_subagent_runs_by_chat_id,
                    )

                    await reconcile_stranded_subagent_runs_by_chat_id(
                        form_data["chat_id"],
                        parent_live=bool(_subagent_work_state.get("generations")),
                        live_rerun_entry_keys=[],
                        user_id=user.id,
                    )
                except Exception:
                    # The persisted-chain guard below remains fail-closed. This
                    # repair merely lets a direct API send recover a rerun whose
                    # worker died before any chat-open/task-poll could heal it.
                    log.exception(
                        "pre-send stranded subagent reconcile failed for chat %s",
                        form_data["chat_id"],
                    )

            leaf_id = form_data.pop("leaf_message_id", None)
            new_user_message = form_data.pop("new_user_message", None)
            assemble_model = None
            assemble_model_id = form_data.get("model")
            if assemble_model_id and assemble_model_id in request.app.state.MODELS:
                assemble_model = request.app.state.MODELS[assemble_model_id]
            elif (
                isinstance(form_data.get("model_item"), dict)
                and form_data["model_item"]
            ):
                assemble_model = form_data["model_item"]

            container_server_id = str(
                request.app.state.config.CONTAINER_MCP_SERVER_ID or ""
            )
            tool_ids = form_data.get("tool_ids") or []
            if isinstance(tool_ids, str):
                tool_ids = [tool_ids]
            container_workspace_active = bool(
                request.app.state.config.ENABLE_CONTAINER_WORKSPACE_SYNC
                and container_server_id
                and f"server:mcp:{container_server_id}" in tool_ids
            )

            assembled = await assemble_conversation_from_leaf(
                form_data["chat_id"],
                leaf_id,
                new_user_message=new_user_message,
                model=assemble_model,
                system_prompt=(form_data.get("params") or {}).get("system"),
                container_workspace_active=container_workspace_active,
                request=request,
                user=user,
                persisted_out=persisted_user_out,
                # The assistant row this turn writes into. On a retry it is also
                # the pinned leaf, so it is in the walked chain carrying the
                # previous attempt's half-written answer.
                resume_message_id=form_data.get("id"),
            )
            form_data["messages"] = assembled

            # Conversation compaction, inter-turn half of the gate (see
            # utils/COMPACTION.md §5 — the check runs before EVERY model request,
            # and this is the one that fires between turns). It reads the previous
            # assistant message's last-round usage, and on a hit summarizes and
            # writes a `compaction` anchor into that message. The conversation is
            # then re-assembled so `blocks_to_api_messages` can apply the cut.
            #
            # Best-effort by design: a summarizer failure must never block a send.
            # The turn proceeds uncompacted and the provider's own context-length
            # error is what surfaces — visible, not silent.
            try:
                from open_webui.utils.compaction import maybe_compact_at_turn_start

                if await maybe_compact_at_turn_start(
                    request,
                    user,
                    chat_id=form_data["chat_id"],
                    model=assemble_model,
                    api_messages=assembled,
                    chain=persisted_user_out.get("chain") or [],
                    # The in-flight assistant, so the "Compacting…" status lands
                    # on the message the user is watching. Turn-start compaction
                    # runs before the first upstream call and can take ~90s on a
                    # 350k-token conversation; unannounced, that reads as a hang.
                    response_message_id=form_data.get("id"),
                ):
                    form_data["messages"] = await assemble_conversation_from_leaf(
                        form_data["chat_id"],
                        leaf_id,
                        new_user_message=None,
                        model=assemble_model,
                        system_prompt=(form_data.get("params") or {}).get("system"),
                        container_workspace_active=container_workspace_active,
                        request=request,
                        user=user,
                    )
            except ChatMessageAncestryError:
                raise
            except Exception:
                log.exception(
                    "turn-start compaction failed for chat %s",
                    form_data.get("chat_id"),
                )
        except ActiveSubagentRerunError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": str(e),
                    "code": "subagent_rerun_in_progress",
                    "entry_keys": e.entry_keys,
                },
            )
        except ChatMessageAncestryError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": str(e),
                    "code": e.code,
                    "leaf_id": e.leaf_id,
                    "message_id": e.message_id,
                },
            )
        except HTTPException:
            raise
        except Exception as e:
            log.exception(f"v2.1 conversation assembly failed: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to assemble conversation: {e}",
            )

    model_id = form_data.get("model", None)
    model_item = form_data.pop("model_item", {})
    tasks = form_data.pop("background_tasks", None)

    metadata = {}
    try:
        # Check if this is really a direct model or if it's in MODELS (backend-managed)
        is_in_models = model_id in request.app.state.MODELS
        is_direct_requested = model_item.get("direct", False)

        # If model is in MODELS, always use backend handling even if direct was requested
        if is_in_models:
            model = request.app.state.MODELS[model_id]
            model_info = await Models.get_model_by_id(model_id)

            # Check if user has access to the model
            if not BYPASS_MODEL_ACCESS_CONTROL and (
                user.role != "admin" or not BYPASS_ADMIN_ACCESS_CONTROL
            ):
                try:
                    await check_model_access(user, model)
                except Exception as e:
                    raise e
        elif is_direct_requested:
            model = model_item
            model_info = None

            request.state.direct = True
            request.state.model = model
        else:
            raise Exception("Model not found")

        model_info_params = (
            model_info.params.model_dump() if model_info and model_info.params else {}
        )

        # Chat Params
        stream_delta_chunk_size = form_data.get("params", {}).get(
            "stream_delta_chunk_size"
        )

        # Model Params
        if model_info_params.get("stream_delta_chunk_size"):
            stream_delta_chunk_size = model_info_params.get("stream_delta_chunk_size")

        metadata = {
            "user_id": user.id,
            "chat_id": form_data.pop("chat_id", None),
            "message_id": form_data.pop("id", None),
            "generation_id": form_data.pop("generation_id", None),
            "turn_id": form_data.pop("turn_id", None),
            # Internal ownership object. It is passed explicitly through the
            # generation pipeline so terminal queue handoff never depends on a
            # hidden request.state side channel.
            "generation_operation": generation_operation,
            "session_id": form_data.pop("session_id", None),
            "headless": form_data.pop("headless", False),
            # Set (to the automation's id) when this turn is a scheduled run.
            # Headless already says "no originating socket"; this says "no human
            # at all", which is what gates the interactive built-ins.
            "automation_run": form_data.pop("automation_run", None),
            "queue_drained_broadcast": form_data.pop("queue_drained_broadcast", None),
            "filter_ids": form_data.pop("filter_ids", []),
            "tool_ids": form_data.get("tool_ids", None),
            "tool_servers": form_data.pop("tool_servers", None),
            "live_tool_selection": (
                normalize_live_tool_selection(form_data.pop("tool_selection"))
                if isinstance(form_data.get("tool_selection"), dict)
                else None
            ),
            "files": form_data.get("files", None),
            "features": form_data.get("features", {}),
            "variables": form_data.get("variables", {}),
            "timezone": form_data.pop("timezone", None),
            "model": model,
            "direct": model_item.get("direct", False),
            "params": {
                "stream_delta_chunk_size": stream_delta_chunk_size,
                "function_calling": (
                    "native"
                    if (
                        form_data.get("params", {}).get("function_calling") == "native"
                        or model_info_params.get("function_calling") == "native"
                    )
                    else "default"
                ),
            },
        }

        if metadata.get("chat_id") and (user and user.role != "admin"):
            if not metadata["chat_id"].startswith("local:"):
                chat = await Chats.get_chat_by_id_and_user_id(
                    metadata["chat_id"], user.id
                )
                if chat is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=ERROR_MESSAGES.DEFAULT(),
                    )

        request.state.metadata = metadata
        form_data["metadata"] = metadata

    except Exception as e:
        log.debug(f"Error processing chat metadata: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    # Cross-device prompt sync: co-deliver the just-persisted user message to
    # other devices viewing this chat so the prompt bubble appears alongside the
    # assistant stream (which already reaches them via the chat's stream room).
    #
    # This now ALSO covers queue-drained turns. The drained user message is
    # persisted by assembly above, so emitting it here makes the bubble appear the
    # instant the queue chip is cleared (see the paired chip-clear broadcast
    # below) instead of only when the much-later `chat:queue:drained` ->
    # loadChat() lands — which, for a non-streaming upstream, is withheld until
    # the whole response is ready, so the queued message used to disappear for the
    # entire turn. The emit is idempotent on the client (keyed by user_message.id)
    # and the drained turn carries no session_id, so every viewing tab (no
    # origin-skip) surgically inserts the bubble; the subsequent loadChat()
    # reconciles to that same row rather than duplicating it.
    _persisted_user_message = persisted_user_out.get("user_message")
    if (
        _persisted_user_message
        and metadata.get("chat_id")
        and not str(metadata.get("chat_id", "")).startswith("local:")
    ):
        bubble_delivered = False
        try:
            bubble_delivered = await emit_chat_user_message(
                metadata.get("user_id"),
                metadata.get("chat_id"),
                metadata.get("session_id"),
                metadata.get("message_id"),
                _persisted_user_message,
                persisted_user_out.get("leaf_message_id"),
            )
        except Exception:
            log.exception("cross-device chat:user-message emit failed")
        # Queue-drained turn: the chip strip is still showing this item (the early
        # chip-shrink broadcast was removed from maybe_drain_queue so the message
        # never disappears into a gap). Clear it NOW, atomically with the bubble
        # appearing above, so the queued chip visibly becomes the chat bubble with
        # no flicker / no empty window.
        #
        # ONLY clear the chip when the bubble was actually delivered. If the
        # user-message emit was skipped (e.g. an oversized data: image trips the
        # payload-size guard), keep the chip so the message stays visible until the
        # later chat:queue:drained -> loadChat backstop renders the bubble AND
        # clears the chip together — otherwise the oversized case would reintroduce
        # the very "chip gone, bubble missing" gap this fix removes.
        if metadata.get("queue_drained_broadcast") and bubble_delivered:
            try:
                from open_webui.utils.chat_queue import broadcast_queue_state

                await broadcast_queue_state(
                    metadata.get("user_id"),
                    metadata.get("chat_id"),
                    event_type="chat:queue:updated",
                )
            except Exception:
                log.exception("queue-drained chip-clear broadcast failed")

    async def register_stream_start_if_needed():
        if STREAM_PROTOCOL_VERSION != "v2.1":
            return
        if not (
            metadata.get("chat_id")
            and metadata.get("message_id")
            and not str(metadata.get("chat_id", "")).startswith("local:")
        ):
            return
        try:
            # Parent linkage hint: when the frontend's pre-send append PATCH never
            # landed (offline send retried after a blip), this upsert CREATES the
            # assistant row — without a parent it would be an orphan the tree walk
            # can't reach (reload shows the prompt with no response). The freshly-
            # persisted user message from assembly is the authoritative parent in
            # exactly that case; when the row already exists this merge is a no-op
            # on identical values.
            _parent_hint = (persisted_user_out.get("user_message") or {}).get("id")
            await Chats.upsert_message_to_chat_by_id_and_message_id(
                metadata["chat_id"],
                metadata["message_id"],
                {
                    "role": "assistant",
                    "model": model_id,
                    "generation_id": metadata.get("generation_id"),
                    "turn_id": metadata.get("turn_id"),
                    "done": False,
                    **({"parentId": _parent_hint} if _parent_hint else {}),
                    # Clear any stale Stop flag from a PRIOR run of this message id
                    # (continue/retry reuses the same id). Otherwise the queue
                    # drain's stop-intent check (_was_user_stopped) could read the
                    # old userStopped:true after this fresh run finishes cleanly and
                    # wrongly pause the queue. Merge-upsert, so this only clears it.
                    "userStopped": False,
                    # Likewise clear a stale error from a PRIOR run: a retryable
                    # error persists error onto the row AND emits chat:message:error
                    # to every tab. This fresh run (the retry) must clear it so the
                    # completed answer doesn't reload with a stale red error banner on
                    # any client. Merge-upsert, so this only clears it.
                    "error": None,
                },
                return_model=False,
            )
        except Exception:
            log.exception("stream startup assistant placeholder upsert failed")
        try:
            stream_version_init(
                metadata["message_id"],
                chat_id=metadata.get("chat_id"),
                user_id=metadata.get("user_id"),
                session_id=metadata.get("session_id"),
                content_blocks=[],
            )
            set_stream_state(
                metadata["message_id"],
                {
                    "chat_id": metadata.get("chat_id"),
                    "user_id": metadata.get("user_id"),
                    "session_id": metadata.get("session_id"),
                    "status": "in_progress",
                    "content_blocks": [],
                    "snapshot_version": 0,
                    "model": model_id,
                },
            )
        except Exception:
            log.exception("stream startup state registration failed")

    async def process_chat(request, form_data, user, metadata, model):
        # Track which stage we reached so the cancel log can say where it
        # happened — otherwise "Chat processing was cancelled" gives us no
        # signal about whether the LLM was ever called or which tool block
        # was active.
        stage = "payload"
        try:
            form_data, metadata, events = await process_chat_payload(
                request, form_data, user, metadata, model
            )

            # Headless queue drain: register the assistant placeholder + in-progress
            # stream state and broadcast chat:queue:drained BEFORE the (possibly
            # blocking) upstream call below — NOT after it.
            #
            # A headless drain carries session_id=None, so chat_completion's
            # register_stream_start_if_needed (the normal-send path that stamps the
            # done:false placeholder + registers the active stream early) was SKIPPED.
            # If we deferred this until after chat_completion_handler returned, then
            # for a NON-STREAMING upstream (which blocks until the entire body is
            # ready) — and during the time-to-first-token of a streaming one — every
            # viewing / origin / reopened tab would render the drained user bubble with
            # NO assistant container and NO live cursor (history.currentId stuck on the
            # user message; get_active_streams_for_chat empty so loadChat can't
            # materialize it). That is the "I just see my message" symptom. Doing it
            # up front makes the assistant row + active stream exist immediately, so
            # loadChat / requestStreamSnapshot attach and the canonical
            # "user bubble + assistant + live cursor" renders right away, exactly like
            # a normal send. The assembly above already persisted the user message;
            # the error/cancel handlers below tear this stream state down if the
            # upstream call then fails (symmetric with the normal-send placeholder).
            if (
                metadata.get("headless")
                and metadata.get("chat_id")
                and metadata.get("message_id")
                and not str(metadata["chat_id"]).startswith("local:")
            ):
                broadcast_spec = metadata.get("queue_drained_broadcast") or {}
                try:
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata["chat_id"],
                        metadata["message_id"],
                        {
                            "role": "assistant",
                            "model": model_id,
                            "parentId": broadcast_spec.get("user_message_id"),
                            "generation_id": metadata.get("generation_id"),
                            "turn_id": metadata.get("turn_id"),
                            "done": False,
                        },
                        return_model=False,
                    )
                except Exception:
                    log.exception("headless assistant placeholder upsert failed")
                try:
                    set_stream_state(
                        metadata["message_id"],
                        {
                            "chat_id": metadata["chat_id"],
                            "status": "in_progress",
                            "content_blocks": [],
                        },
                    )
                except Exception:
                    log.exception("headless set_stream_state failed")
                try:
                    from open_webui.utils.chat_queue import broadcast_queue_state

                    await broadcast_queue_state(
                        metadata.get("user_id"),
                        metadata["chat_id"],
                        event_type="chat:queue:drained",
                        **{
                            k: broadcast_spec[k]
                            for k in (
                                "item_id",
                                "user_message_id",
                                "response_message_id",
                                "generation_id",
                                "turn_id",
                            )
                            if broadcast_spec.get(k) is not None
                        },
                    )
                except Exception:
                    log.exception("headless chat:queue:drained broadcast failed")

            stage = "completion"
            response = await chat_completion_handler(request, form_data, user)
            if metadata.get("chat_id") and metadata.get("message_id"):
                try:
                    if not metadata["chat_id"].startswith("local:"):
                        # Same shape-safety as the stream-start placeholder: on the
                        # session-less (synchronous) path this stamp can be the row's
                        # CREATION — a bare {model} used to leave a role-less,
                        # parent-less skeleton the tree walk can't reach.
                        _sync_parent_hint = (
                            persisted_user_out.get("user_message") or {}
                        ).get("id")
                        await Chats.upsert_message_to_chat_by_id_and_message_id(
                            metadata["chat_id"],
                            metadata["message_id"],
                            {
                                "role": "assistant",
                                "model": model_id,
                                "generation_id": metadata.get("generation_id"),
                                "turn_id": metadata.get("turn_id"),
                                **(
                                    {"parentId": _sync_parent_hint}
                                    if _sync_parent_hint
                                    else {}
                                ),
                            },
                            return_model=False,
                        )
                except:
                    pass

            stage = "response"
            return await process_chat_response(
                request, response, form_data, user, metadata, model, events, tasks
            )
        except asyncio.CancelledError:
            mcp_server_ids = list((metadata.get("mcp_clients") or {}).keys())
            log.info(
                "Chat processing was cancelled (stage=%s, mcp_servers=%s)",
                stage,
                mcp_server_ids,
            )
            user_stopped = False
            if metadata.get("chat_id"):
                try:
                    user_stopped = bool(
                        await is_generation_cancelled(
                            getattr(request.app.state, "redis", None),
                            metadata["chat_id"],
                            metadata.get("generation_id"),
                        )
                        or await is_generation_turn_cancelled(
                            getattr(request.app.state, "redis", None),
                            metadata["chat_id"],
                            metadata.get("turn_id"),
                        )
                    )
                except Exception:
                    log.exception("generation cancellation classification failed")
            interruption_error = {
                "content": (
                    "The model request was interrupted before a response could be completed."
                )
            }
            if metadata.get("message_id"):
                set_stream_state(
                    metadata["message_id"],
                    {
                        "chat_id": metadata.get("chat_id"),
                        "status": "cancelled" if user_stopped else "error",
                        **({} if user_stopped else {"error": interruption_error}),
                    },
                )
                clear_stream_state(metadata["message_id"])
            # Cancelled before/around streaming: PAUSE the queue (clear this
            # generation's marker, best-effort).
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
                    pass
            # Any saved-chat cancellation must leave an explicit durable terminal
            # row, not just a socket event. This covers setup/TTFT cancellations
            # before process_chat_response installs its richer teardown. A mobile
            # tab may be gone when the event fires; on return it reconstructs the
            # exact outcome from this row. User Stop remains a clean stopped turn;
            # infrastructure/shutdown cancellation is a visible retryable error.
            if is_persisted_chat_generation(metadata):
                broadcast_spec = metadata.get("queue_drained_broadcast") or {}
                parent_hint = broadcast_spec.get("user_message_id") or (
                    (persisted_user_out.get("user_message") or {}).get("id")
                )
                terminal_update = {
                    "role": "assistant",
                    "model": model_id,
                    "parentId": parent_hint,
                    "done": True,
                    **(
                        {"userStopped": True}
                        if user_stopped
                        else {"error": interruption_error}
                    ),
                }
                try:
                    await Chats.update_generation_message_if_current(
                        metadata["chat_id"],
                        metadata["message_id"],
                        metadata.get("generation_id"),
                        metadata.get("turn_id"),
                        terminal_update,
                        create_if_missing=True,
                    )
                except Exception:
                    log.exception("cancel terminal assistant upsert failed")

                if metadata.get("headless"):
                    try:
                        from open_webui.utils.chat_queue import broadcast_queue_state

                        await broadcast_queue_state(
                            metadata.get("user_id"),
                            metadata["chat_id"],
                            event_type="chat:queue:drained",
                            **{
                                k: broadcast_spec[k]
                                for k in (
                                    "item_id",
                                    "user_message_id",
                                    "response_message_id",
                                    "generation_id",
                                    "turn_id",
                                )
                                if broadcast_spec.get(k) is not None
                            },
                        )
                    except Exception:
                        log.exception(
                            "headless cancel chat:queue:drained broadcast failed"
                        )
            try:
                event_emitter = get_event_emitter(metadata)
                if not user_stopped:
                    await event_emitter(
                        {
                            "type": "chat:message:error",
                            "data": {"error": interruption_error},
                        }
                    )
                await event_emitter({"type": "chat:tasks:cancel"})
            except Exception:
                pass
            # Re-raise after cleanup so the cancellation propagates and the task
            # actually unwinds/exits. Swallowing it leaves the task alive inside
            # anyio's cancel scope, which then reschedules _deliver_cancellation
            # every loop tick forever, pinning a core at idle until restart (see
            # the matching fix + py-spy evidence in process_chat_response).
            raise
        except Exception as e:
            # `log.exception`, not `log.debug`: this is the LAST handler for an
            # unhandled server-side fault in a generation. Under the old debug-level
            # log an internal crash left no traceback anywhere — the only trace of a
            # `KeyError('content')` in the terminal projection was the reader-facing
            # string "'content'" on the message row, which is not a diagnosis.
            log.exception(f"Error processing chat payload: {e}")
            # A bare `str(e)` is the exception's *repr payload*, which for the common
            # KeyError/AttributeError/IndexError family is just the missing key —
            # unreadable to a user and unsearchable in a log. Name the fault instead
            # and keep the cause attached.
            _err_detail = f"{type(e).__name__}: {e}".strip().rstrip(":").strip()
            _error_payload = {
                "content": (
                    "The server hit an internal error while completing this "
                    f"response ({_err_detail})."
                ),
                "code": "internal_error",
            }
            if metadata.get("chat_id") and metadata.get("message_id"):
                # Update the chat message with the error
                set_stream_state(
                    metadata["message_id"],
                    {
                        "chat_id": metadata.get("chat_id"),
                        "status": "error",
                        "error": _error_payload,
                    },
                )
                clear_stream_state(metadata["message_id"])
                try:
                    if not metadata["chat_id"].startswith("local:"):
                        await Chats.upsert_message_to_chat_by_id_and_message_id(
                            metadata["chat_id"],
                            metadata["message_id"],
                            {
                                "generation_id": metadata.get("generation_id"),
                                "turn_id": metadata.get("turn_id"),
                                "error": _error_payload,
                                # Terminal means terminal: never persist an error
                                # alongside `done: false`. See the matching write in
                                # the middleware's terminal-error path — a row that
                                # is both errored and unfinished reads as "still
                                # generating" to every reconcile path, which is what
                                # made a crashed turn look like it resumed streaming
                                # after a reload.
                                "done": True,
                            },
                        )

                    event_emitter = get_event_emitter(metadata)
                    await event_emitter(
                        {
                            "type": "chat:message:error",
                            "data": {"error": _error_payload},
                        }
                    )
                    await event_emitter(
                        {"type": "chat:tasks:cancel"},
                    )

                except:
                    pass

            # Generation failed before/around streaming: PAUSE the queue by
            # clearing this generation's draining marker (best-effort). The
            # middleware error path already clears for mid-stream errors; this
            # catches payload/setup-stage failures that never reached it.
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
                    pass
        finally:
            # MCP transports create AnyIO cancel scopes that must be closed by
            # the same asyncio task that opened them, and in REVERSE connection
            # order — see disconnect_mcp_clients for why the order is a hard
            # requirement rather than a preference.
            from open_webui.utils.mcp.client import disconnect_mcp_clients

            await disconnect_mcp_clients(
                metadata.get("mcp_clients"), context="chat cleanup"
            )

    # A saved chat is durable work whether or not its originating browser socket
    # happens to be connected. Socket presence controls event delivery only; it
    # must never select the synchronous path, whose response exists only in the
    # requesting tab and leaves an empty assistant placeholder after reload.
    if is_persisted_chat_generation(metadata):
        try:
            await register_stream_start_if_needed()
            task_id, _ = await create_task(
                request.app.state.redis,
                process_chat(request, form_data, user, metadata, model),
                id=metadata["chat_id"],
                generation_operation=generation_operation,
            )
        except GenerationCancelledError:
            if (
                metadata.get("chat_id")
                and metadata.get("message_id")
                and metadata.get("generation_id")
                and metadata.get("turn_id")
                and not str(metadata["chat_id"]).startswith("local:")
            ):
                await Chats.mark_generation_stopped_if_current(
                    metadata["chat_id"],
                    metadata["message_id"],
                    metadata["generation_id"],
                    metadata["turn_id"],
                )
            return {
                "status": True,
                "cancelled": True,
                "generation_id": metadata.get("generation_id"),
            }
        return {"status": True, "task_id": task_id}
    else:
        return await process_chat(request, form_data, user, metadata, model)


async def _persist_stopped_generation_operations(
    chat_id: str,
    operations: list[dict],
) -> None:
    """Persist cancellation only while each displaced identity still owns its row."""
    for operation in operations:
        message_id = str(operation.get("message_id") or "")
        generation_id = str(operation.get("generation_id") or "")
        turn_id = str(operation.get("turn_id") or "")
        if not message_id or not generation_id or not turn_id:
            continue
        try:
            await Chats.mark_generation_stopped_if_current(
                chat_id,
                message_id,
                generation_id,
                turn_id,
                require_unfinished=True,
            )
        except Exception:
            log.exception(
                "persisting stopped generation marker failed for %s/%s",
                chat_id,
                message_id,
            )


async def _chat_completion_with_operation(
    request: Request,
    form_data: dict,
    user,
    *,
    pre_registered_operation: Optional[dict] = None,
):
    """Own one generation operation from request arrival through task teardown.

    Registering here, before conversation assembly, makes pending work visible
    to Stop and queue-drain guards. ``turn_id`` permits parallel sibling model
    responses while atomically rejecting a different concurrent user turn.
    """
    # This is a lifecycle command for our registry, never an upstream model
    # parameter. Remove it before payload processing can forward unknown fields.
    supersede_active_turn = form_data.pop("supersede_active_turn", False) is True
    chat_id = str(form_data.get("chat_id") or "")
    message_id = str(form_data.get("id") or "")
    saved_chat = bool(chat_id and not chat_id.startswith("local:"))
    operation = None
    displaced_operations: list[dict] = []
    if saved_chat and message_id:
        if user and user.role != "admin":
            owned = await Chats.get_chat_by_id_and_user_id(chat_id, user.id)
            if owned is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=ERROR_MESSAGES.DEFAULT(),
                )

        turn_id = str(form_data.get("turn_id") or "")
        generation_id = str(form_data.get("generation_id") or "")
        if not generation_id or not turn_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": (
                        "Saved-chat completions require generation_id and turn_id."
                    ),
                    "code": "generation_identity_required",
                },
            )
        form_data["generation_id"] = generation_id
        form_data["turn_id"] = turn_id
        operation = {
            "generation_id": generation_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "turn_id": turn_id,
            "task_id": "",
        }

        if pre_registered_operation is not None:
            pre_registered_identity = {
                key: str(pre_registered_operation.get(key) or "")
                for key in ("generation_id", "chat_id", "message_id", "turn_id")
            }
            operation_identity = {
                key: operation[key]
                for key in ("generation_id", "chat_id", "message_id", "turn_id")
            }
            existing = await get_generation_operation(
                request.app.state.redis, generation_id
            )
            existing_identity = (
                {
                    key: str(existing.get(key) or "")
                    for key in (
                        "generation_id",
                        "chat_id",
                        "message_id",
                        "turn_id",
                    )
                }
                if existing is not None
                else None
            )
            if (
                pre_registered_identity != operation_identity
                or existing_identity != operation_identity
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "message": "The reserved queue generation is no longer owned by this task.",
                        "code": "generation_reservation_lost",
                    },
                )
            operation["task_id"] = str(existing.get("task_id") or "")
            registration = (
                "cancelled"
                if (
                    await is_generation_cancelled(
                        request.app.state.redis, chat_id, generation_id
                    )
                    or await is_generation_turn_cancelled(
                        request.app.state.redis, chat_id, turn_id
                    )
                    or await is_chat_work_blocked(request.app.state.redis, chat_id)
                )
                else "acquired"
            )
        elif supersede_active_turn:
            supersede_result = await supersede_generation_operation(
                request.app.state.redis, operation
            )
            registration = supersede_result["registration"]
            displaced_operations = supersede_result["displaced"]
        else:
            registration = await register_generation_operation(
                request.app.state.redis, operation
            )
        if registration == "cancelled":
            return {
                "status": True,
                "cancelled": True,
                "generation_id": generation_id,
            }
        if registration == "turn_conflict":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "The chat is already processing a different turn.",
                    "code": "chat_generation_in_progress",
                },
            )
        if registration == "id_conflict":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "The generation id is already owned by another request.",
                    "code": "generation_id_conflict",
                },
            )
        if registration == "duplicate":
            # A lost POST response may retry while the original is still in
            # assembly. Return its task once bound; otherwise explicitly report
            # pending instead of manufacturing a second generation.
            existing = await get_generation_operation(
                request.app.state.redis, generation_id
            )
            for _ in range(20):
                if existing and existing.get("task_id"):
                    return {
                        "status": True,
                        "task_id": existing["task_id"],
                        "generation_id": generation_id,
                        "deduped": True,
                    }
                if await is_generation_cancelled(
                    request.app.state.redis, chat_id, generation_id
                ):
                    return {
                        "status": True,
                        "cancelled": True,
                        "generation_id": generation_id,
                    }
                await asyncio.sleep(0.1)
                existing = await get_generation_operation(
                    request.app.state.redis, generation_id
                )
            return {
                "status": True,
                "pending": True,
                "generation_id": generation_id,
                "deduped": True,
            }

        if supersede_active_turn and displaced_operations:
            # The registry transaction above already transferred admission to
            # this replacement and latched the displaced turn. Provider work for
            # the replacement waits here until every concrete old task has
            # unwound, preserving the intuitive redo contract: one click means
            # stop the old run, then start this one.
            await _persist_stopped_generation_operations(chat_id, displaced_operations)
            displaced_task_ids = list(
                dict.fromkeys(
                    str(displaced.get("task_id") or "")
                    for displaced in displaced_operations
                    if str(displaced.get("task_id") or "")
                )
            )
            pending_task_ids = await stop_tasks_and_wait(
                request.app.state.redis,
                displaced_task_ids,
                timeout=10.0,
            )
            # A cancellation can race the last provider token. Repeat the
            # identity-guarded terminal marker after teardown so a genuinely
            # cancelled row is durable without relabelling a clean completion.
            await _persist_stopped_generation_operations(chat_id, displaced_operations)
            if pending_task_ids:
                await mark_generation_cancelled(
                    request.app.state.redis, chat_id, generation_id
                )
                await unregister_generation_operation(
                    request.app.state.redis, operation
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "message": (
                            "The previous turn did not stop in time; the replacement "
                            "was not started."
                        ),
                        "code": "turn_supersede_timeout",
                    },
                )
            await finish_generation_supersede(
                request.app.state.redis, chat_id, turn_id
            )

    preflight_heartbeat = None
    if (
        operation
        and request.app.state.redis
        and not str(operation.get("task_id") or "")
    ):
        preflight_heartbeat = asyncio.create_task(
            heartbeat_generation_operation_until_bound(
                request.app.state.redis, operation
            )
        )

    try:
        return await _chat_completion_impl(
            request,
            form_data,
            user,
            generation_operation=operation,
        )
    finally:
        if preflight_heartbeat is not None:
            preflight_heartbeat.cancel()
            try:
                await preflight_heartbeat
            except asyncio.CancelledError:
                pass
        if operation and not operation.get("task_id"):
            await unregister_generation_operation(request.app.state.redis, operation)


@app.post("/api/chat/completions")
@app.post("/api/v1/chat/completions")  # Experimental: Compatibility with OpenAI API
async def chat_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
):
    return await _chat_completion_with_operation(request, form_data, user)


async def start_generation(
    chat_id: str,
    send_spec: dict,
    user,
    *,
    oauth_session_id: Optional[str] = None,
    generation_operation: Optional[dict] = None,
):
    """Request-free entrypoint to start a chat generation.

    Mirrors what the ``/api/chat/completions`` route does, but without an
    inbound HTTP ``Request`` — used by the autonomous message-queue drain to
    start the next queued turn with zero browser tabs open. Builds a
    ``HeadlessRequest`` carrier and a v2.1 ``form_data`` from ``send_spec`` (the
    self-contained queue item), then calls the same ``chat_completion`` so the
    full pipeline (assembly, preprocessing, tools, persistence, socket
    delivery) is byte-identical to a tab-driven send.

    ``send_spec`` keys (all optional except ``model`` + ``leaf_message_id`` +
    ``new_user_message``): ``model``, ``leaf_message_id``, ``new_user_message``,
    ``params``, ``tool_ids``, ``tool_servers``, ``tool_selection``,
    ``filter_ids``, ``features``,
    ``variables``, ``files``, ``reasoning``, ``service_tier``, ``timezone``,
    ``background_tasks``, ``stream`` (defaults True), ``model_item``.

    ``session_id`` is intentionally NOT set: with no originating socket, the
    event emitter fans out to all of the user's open tabs (and persists to the
    DB regardless), so any open tab receives the stream and closed-tab runs are
    recoverable via the snapshot/active-stream machinery. Returns whatever
    ``chat_completion`` returns (the generation runs inline within this call).
    """
    request = HeadlessRequest(
        app,
        cookies={"oauth_session_id": oauth_session_id} if oauth_session_id else None,
    )
    form_data = {
        "stream": send_spec.get("stream", True),
        "model": send_spec.get("model"),
        "chat_id": chat_id,
        "id": send_spec.get("response_message_id") or send_spec.get("id"),
        "leaf_message_id": send_spec.get("leaf_message_id"),
        # Marks this as a request-free run so process_chat_response builds a
        # fan-out emitter despite the absent session_id (see its gate).
        "headless": True,
    }
    # Optional fields — include only when present so we match the route's body
    # shape (which omits undefined keys) and don't override pipeline defaults.
    for key in (
        "new_user_message",
        "params",
        "tool_ids",
        "tool_servers",
        "tool_selection",
        "filter_ids",
        "features",
        "variables",
        "files",
        "reasoning",
        "service_tier",
        "background_tasks",
        "model_item",
        "stream_options",
        "timezone",
        "queue_drained_broadcast",
        "generation_id",
        "turn_id",
        "automation_run",
    ):
        if send_spec.get(key) is not None:
            form_data[key] = send_spec[key]

    return await _chat_completion_with_operation(
        request,
        form_data,
        user,
        pre_registered_operation=generation_operation,
    )


@app.post("/api/chat/actions/{action_id}")
async def chat_action(
    request: Request, action_id: str, form_data: dict, user=Depends(get_verified_user)
):
    try:
        model_item = form_data.pop("model_item", {})

        if model_item.get("direct", False):
            request.state.direct = True
            request.state.model = model_item

        return await chat_action_handler(request, action_id, form_data, user)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


class StopGenerationTarget(BaseModel):
    generation_id: str
    message_id: str
    turn_id: str


class StopChatGenerationsForm(BaseModel):
    generations: list[StopGenerationTarget] = Field(default_factory=list)
    # Detached subagent reruns are registered outside the generation registry so
    # a parent-generation cancel cannot tear them down. The composer's Stop means
    # "halt this chat", so it opts in; narrowly-scoped callers (the drain-raced-a-
    # Stop guard) leave this off and only cancel the generation they named.
    include_subagent_reruns: bool = False


@app.post("/api/tasks/stop/chat/{chat_id}")
async def stop_chat_generations_endpoint(
    request: Request,
    chat_id: str,
    form_data: StopChatGenerationsForm,
    user=Depends(get_verified_user),
):
    """Latch exact generation intent, then cancel that complete user turn.

    Intent is written first. A task that registers after our registry snapshot
    observes it before opening its start gate; a task that registered first is
    already discoverable below. Message ids are deliberately insufficient:
    continue/retry may reuse an assistant row, so every Stop carries the
    generation and turn identities that owned it.

    ``include_subagent_reruns`` additionally cancels the chat's detached subagent
    redos, which own their own task registry entries and their own shielded
    terminal writes.
    """
    chat = await Chats.get_chat_by_id(chat_id)
    if chat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found",
        )
    if chat.user_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to stop this chat",
        )

    redis = request.app.state.redis

    # Detached subagent redos live in the task registry under their own item key,
    # not as generation operations, so they must be collected separately. Failing
    # to find them must not block cancelling the generations themselves.
    subagent_rerun_task_ids: list[str] = []
    if form_data.include_subagent_reruns:
        try:
            subagent_rerun_task_ids = list(
                dict.fromkeys(
                    await list_item_task_ids_by_prefix(
                        redis, f"subagent-rerun:{chat_id}:"
                    )
                )
            )
        except Exception:
            log.exception(
                "collecting subagent rerun tasks while stopping chat %s failed",
                chat_id,
            )

    active_operations = await list_generation_operations_by_item(redis, chat_id)
    operations_by_id = {
        str(operation.get("generation_id") or ""): operation
        for operation in active_operations
        if operation.get("generation_id")
    }
    target_operations: dict[str, dict[str, str]] = {}
    generation_ids_to_cancel: set[str] = set()
    turn_ids_to_cancel: set[str] = set()

    for target in form_data.generations:
        generation_id = str(target.generation_id or "")
        message_id = str(target.message_id or "")
        turn_id = str(target.turn_id or "")
        if not generation_id or not message_id or not turn_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": (
                        "generation_id, message_id, and turn_id are required "
                        "for every Stop target."
                    ),
                    "code": "generation_identity_required",
                },
            )

        active_operation = operations_by_id.get(generation_id)
        if active_operation is None:
            # Generation ids are globally unique. Resolve a same-id operation
            # outside this chat so the request cannot turn a foreign live id
            # into an unrelated local cancellation latch.
            active_operation = await get_generation_operation(redis, generation_id)
        if active_operation is not None:
            if str(active_operation.get("chat_id") or "") != chat_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Generation belongs to another chat",
                )
            active_message_id = str(active_operation.get("message_id") or "")
            if active_message_id != message_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Generation belongs to another message",
                )
            active_turn_id = str(active_operation.get("turn_id") or "")
            if active_turn_id != turn_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Generation belongs to another turn",
                )

        identity = {
            "generation_id": generation_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "turn_id": turn_id,
            "task_id": str((active_operation or {}).get("task_id") or ""),
        }
        previous = target_operations.get(generation_id)
        if previous is not None and previous != identity:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conflicting identities were supplied for one generation",
            )
        target_operations[generation_id] = identity
        generation_ids_to_cancel.add(generation_id)
        turn_ids_to_cancel.add(turn_id)

    if not target_operations:
        # No generation to latch, but a Stop with reruns in flight still has work
        # to do — the user asked for the whole chat to halt.
        remaining_rerun_task_ids = (
            await stop_tasks_and_wait(redis, subagent_rerun_task_ids, timeout=10.0)
            if subagent_rerun_task_ids
            else []
        )
        return {
            "status": True,
            "generation_ids": [],
            "turn_ids": [],
            "task_ids": list(subagent_rerun_task_ids),
            "pending_task_ids": remaining_rerun_task_ids,
            "subagent_rerun_task_ids": list(subagent_rerun_task_ids),
        }

    try:
        cancelled_operations = await latch_generation_cancellation(
            redis,
            chat_id,
            generation_ids=generation_ids_to_cancel,
            turn_ids=turn_ids_to_cancel,
        )
    except Exception as e:
        log.exception("collecting tasks while stopping chat %s failed", chat_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Cancellation was recorded, but live task state is unavailable.",
                "code": "task_state_unavailable",
            },
        ) from e

    cancelled_operation_task_ids: set[str] = set()
    for operation in cancelled_operations:
        operation_generation_id = str(operation.get("generation_id") or "")
        operation_turn_id = str(operation.get("turn_id") or "")
        operation_task_id = str(operation.get("task_id") or "")
        if operation_generation_id:
            generation_ids_to_cancel.add(operation_generation_id)
            target_operations[operation_generation_id] = {
                "generation_id": operation_generation_id,
                "chat_id": chat_id,
                "message_id": str(operation.get("message_id") or ""),
                "turn_id": operation_turn_id,
                "task_id": operation_task_id,
            }
        if operation_turn_id:
            turn_ids_to_cancel.add(operation_turn_id)
        if operation_task_id:
            cancelled_operation_task_ids.add(operation_task_id)

    task_ids_to_stop = sorted(cancelled_operation_task_ids)
    # Reruns are stopped in the SAME wait as the generations: a detached redo that
    # outlived the Stop it was cancelled by would keep writing into the chat the
    # user just halted.
    task_ids_to_stop.extend(
        task_id
        for task_id in subagent_rerun_task_ids
        if task_id not in cancelled_operation_task_ids
    )

    async def _persist_stopped_identities() -> None:
        for operation in target_operations.values():
            message_id = operation["message_id"]
            if not message_id:
                continue
            try:
                await Chats.mark_generation_stopped_if_current(
                    chat_id,
                    message_id,
                    operation["generation_id"],
                    operation["turn_id"],
                    # Never relabel a turn that already reached `done`. Stop
                    # races completion constantly (the button is live until the
                    # last token), and stamping userStopped on a finished answer
                    # both mislabels it in the UI and pauses the message queue.
                    # A genuinely cancelled run records its own intent in the
                    # task's cancel teardown.
                    require_unfinished=True,
                )
            except Exception:
                log.exception(
                    "persisting stopped generation marker failed for %s/%s",
                    chat_id,
                    message_id,
                )

    # Mark before cancellation so queue ownership observes Stop. The compare and
    # update are one DB transaction, so an old request cannot mutate a row that a
    # newer generation has already claimed.
    await _persist_stopped_identities()

    remaining_task_ids = await stop_tasks_and_wait(
        redis,
        task_ids_to_stop,
        timeout=10.0,
    )
    # A very fast Stop can precede placeholder persistence. Task teardown writes
    # the exact generation identity as part of its terminal checkpoint; retry
    # the conditional marker after waiting so that case also records user intent.
    await _persist_stopped_identities()

    return {
        "status": True,
        "generation_ids": sorted(generation_ids_to_cancel),
        "turn_ids": sorted(turn_ids_to_cancel),
        "task_ids": task_ids_to_stop,
        "pending_task_ids": remaining_task_ids,
        "subagent_rerun_task_ids": list(subagent_rerun_task_ids),
    }


@app.get("/api/tasks/chat/{chat_id}")
async def get_chat_work_state_endpoint(
    request: Request, chat_id: str, user=Depends(get_verified_user)
):
    # Ownership check without hydrating the complete conversation blob.
    if await Chats.get_chat_open_validator(chat_id, user.id) is None:
        return {
            "generations": [],
            "rerun_task_ids": [],
            "subagent_rerun_entry_keys": [],
            "draining": None,
        }

    try:
        work_state = await collect_chat_work_state(request.app.state.redis, chat_id)
    except Exception as e:
        log.exception("collecting work state failed for chat %s", chat_id)
        # Unknown is not empty. Returning an empty registry here caused the
        # caller and the stranded-run reconciler below to declare genuinely
        # live work dead during a Redis outage. A 503 makes clients preserve
        # their current state and retry.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Task state is temporarily unavailable.",
                "code": "task_state_unavailable",
            },
        ) from e

    generations = work_state["generations"]
    generation_task_ids = [
        operation["task_id"] for operation in generations if operation.get("task_id")
    ]
    rerun_task_ids = work_state["rerun_task_ids"]
    subagent_rerun_entry_keys = work_state["subagent_rerun_entry_keys"]
    log.debug(
        "Generation/rerun task IDs for chat %s: %s / %s",
        chat_id,
        generation_task_ids,
        rerun_task_ids,
    )

    # Self-heal stranded subagent runs: an entry stuck at status='running' whose
    # owning task DIED (server restart/crash, or — pre-shield — a cancel that
    # truncated the terminal write) never resolves on the backend, because a
    # detached rerun has no parent finalizer sweep. The frontend only DISPLAYS it
    # as a permanent "Stopped" without recovering the real answer. The poller is the
    # natural server-side liveness hook (the client calls it on chat load), and it
    # already knows which tasks/reruns are live — so terminalize any genuinely-idle
    # stranded entry durably here (recovering final_text when its hidden chat
    # produced one). Gated so a live run is never stomped.
    try:
        from open_webui.utils.subagent import (
            reconcile_stranded_subagent_runs_by_chat_id,
        )

        parent_live = bool(generations)
        await reconcile_stranded_subagent_runs_by_chat_id(
            chat_id,
            parent_live=parent_live,
            live_rerun_entry_keys=subagent_rerun_entry_keys,
            user_id=user.id,
        )
    except Exception:
        log.exception("stranded subagent reconcile failed for chat %s", chat_id)

    return work_state


##################################
#
# Config Endpoints
#
##################################


@app.get("/api/config")
async def get_app_config(request: Request):
    user = None
    token = None

    auth_header = request.headers.get("Authorization")
    if auth_header:
        cred = get_http_authorization_cred(auth_header)
        if cred:
            token = cred.credentials

    if not token and "token" in request.cookies:
        token = request.cookies.get("token")

    if token:
        try:
            data = decode_token(token)
        except Exception as e:
            log.debug(e)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            )
        if data is not None and "id" in data:
            user = await Users.get_user_by_id(data["id"])

    user_count = await Users.get_num_users()
    onboarding = False

    if user is None:
        onboarding = user_count == 0

    config_payload = {
        **({"onboarding": True} if onboarding else {}),
        "status": True,
        "name": app.state.WEBUI_NAME,
        "version": VERSION,
        "default_locale": str(DEFAULT_LOCALE),
        "oauth": {
            "providers": {
                name: config.get("name", name)
                for name, config in OAUTH_PROVIDERS.items()
            }
        },
        "features": {
            "auth": WEBUI_AUTH,
            "auth_trusted_header": bool(app.state.AUTH_TRUSTED_EMAIL_HEADER),
            "enable_signup_password_confirmation": ENABLE_SIGNUP_PASSWORD_CONFIRMATION,
            "enable_ldap": app.state.config.ENABLE_LDAP,
            "enable_api_key": app.state.config.ENABLE_API_KEY,
            "enable_signup": app.state.config.ENABLE_SIGNUP,
            "enable_login_form": app.state.config.ENABLE_LOGIN_FORM,
            "enable_websocket": ENABLE_WEBSOCKET_SUPPORT,
            "enable_version_update_check": ENABLE_VERSION_UPDATE_CHECK,
            "stream_protocol_version": STREAM_PROTOCOL_VERSION,
            **(
                {
                    "enable_direct_connections": app.state.config.ENABLE_DIRECT_CONNECTIONS,
                    "enable_channels": app.state.config.ENABLE_CHANNELS,
                    "enable_notes": app.state.config.ENABLE_NOTES,
                    "enable_web_search": app.state.config.ENABLE_WEB_SEARCH,
                    "enable_study_mode": app.state.config.ENABLE_STUDY_MODE,
                    "enable_data_viz": app.state.config.ENABLE_DATA_VIZ,
                    "enable_automations": app.state.config.ENABLE_AUTOMATIONS,
                    # The VAPID public key is public by definition — the browser
                    # needs it as `applicationServerKey` to subscribe at all.
                    "webpush_public_key": app.state.config.WEBPUSH_VAPID_PUBLIC_KEY,
                    "enable_video_input": app.state.config.ENABLE_VIDEO_INPUT,
                    "enable_video_url_ingest": app.state.config.ENABLE_VIDEO_URL_INGEST,
                    "enable_subagents": app.state.config.ENABLE_SUBAGENTS,
                    "enable_ask_user": app.state.config.ENABLE_ASK_USER,
                    "enable_container_workspace_sync": app.state.config.ENABLE_CONTAINER_WORKSPACE_SYNC,
                    "container_mcp_server_id": app.state.config.CONTAINER_MCP_SERVER_ID,
                    # Surfaced (non-secret) so the per-chat Subagent settings
                    # popover in MessageInput can resolve the effective subagent
                    # model and pick the right `service_tiers` list for its
                    # dropdown without needing an admin-only round trip.
                    "subagent_default_model": app.state.config.SUBAGENT_DEFAULT_MODEL,
                    "subagent_allow_external_tools": (
                        app.state.config.SUBAGENT_ALLOW_EXTERNAL_TOOLS
                    ),
                    # Flex auto-flip policy — read by Chat.svelte's auto-flip
                    # reactive. Surfaced (non-secret) so admins can tune the
                    # off-peak window and threshold without touching code.
                    "flex_auto_flip_enabled": app.state.config.FLEX_AUTO_FLIP_ENABLED,
                    "flex_auto_flip_off_peak_start_hour": app.state.config.FLEX_AUTO_FLIP_OFF_PEAK_START_HOUR,
                    "flex_auto_flip_off_peak_end_hour": app.state.config.FLEX_AUTO_FLIP_OFF_PEAK_END_HOUR,
                    "flex_auto_flip_off_peak_timezone": app.state.config.FLEX_AUTO_FLIP_OFF_PEAK_TIMEZONE,
                    "flex_auto_flip_threshold_ratio": app.state.config.FLEX_AUTO_FLIP_THRESHOLD_RATIO,
                    "enable_image_generation": app.state.config.ENABLE_IMAGE_GENERATION,
                    "enable_autocomplete_generation": app.state.config.ENABLE_AUTOCOMPLETE_GENERATION,
                    # Effective follow-up enablement (override beats the base flag),
                    # mirroring generate_follow_ups() in routers/tasks.py. Surfaced so
                    # the chat UI can reserve the follow-up row's space during streaming
                    # ONLY when a row will actually arrive — otherwise the reserve would
                    # be held open and never filled (empty trailing gap).
                    "enable_follow_up_generation": (
                        app.state.config.FOLLOW_UP_GENERATION_OVERRIDE == "force_enable"
                        or (
                            app.state.config.FOLLOW_UP_GENERATION_OVERRIDE
                            != "force_disable"
                            and app.state.config.ENABLE_FOLLOW_UP_GENERATION
                        )
                    ),
                    "enable_community_sharing": app.state.config.ENABLE_COMMUNITY_SHARING,
                    "enable_message_rating": app.state.config.ENABLE_MESSAGE_RATING,
                    "enable_user_webhooks": app.state.config.ENABLE_USER_WEBHOOKS,
                    "enable_admin_export": ENABLE_ADMIN_EXPORT,
                    "enable_admin_chat_access": ENABLE_ADMIN_CHAT_ACCESS,
                    "pdf_conversion_available": app.state.PDF_CONVERSION_AVAILABLE,
                    "enable_google_drive_integration": app.state.config.ENABLE_GOOGLE_DRIVE_INTEGRATION,
                    "enable_onedrive_integration": app.state.config.ENABLE_ONEDRIVE_INTEGRATION,
                    **(
                        {
                            "enable_onedrive_personal": ENABLE_ONEDRIVE_PERSONAL,
                            "enable_onedrive_business": ENABLE_ONEDRIVE_BUSINESS,
                        }
                        if app.state.config.ENABLE_ONEDRIVE_INTEGRATION
                        else {}
                    ),
                }
                if user is not None
                else {}
            ),
        },
        **(
            {
                "default_models": app.state.config.DEFAULT_MODELS,
                "default_prompt_suggestions": app.state.config.DEFAULT_PROMPT_SUGGESTIONS,
                "user_count": user_count,
                "audio": {
                    "tts": {
                        "engine": app.state.config.TTS_ENGINE,
                        "voice": app.state.config.TTS_VOICE,
                        "split_on": app.state.config.TTS_SPLIT_ON,
                    },
                    "stt": {
                        "engine": app.state.config.STT_ENGINE,
                    },
                },
                "file": {
                    "max_size": app.state.config.FILE_MAX_SIZE,
                    "max_count": app.state.config.FILE_MAX_COUNT,
                    "image_compression": {
                        "width": app.state.config.FILE_IMAGE_COMPRESSION_WIDTH,
                        "height": app.state.config.FILE_IMAGE_COMPRESSION_HEIGHT,
                    },
                    "image_provider_compression": {
                        "enabled": app.state.config.IMAGE_PROVIDER_COMPRESSION_ENABLED,
                        "max_dimension": app.state.config.IMAGE_PROVIDER_MAX_DIMENSION,
                    },
                },
                # Global permission DEFAULTS are admin-config data; every client
                # surface reads the per-user resolved `user.permissions` instead
                # (grep confirms zero `config.permissions` reads), so only ship
                # the global defaults to admins.
                **(
                    {"permissions": {**app.state.config.USER_PERMISSIONS}}
                    if user is not None and user.role == "admin"
                    else {}
                ),
                "google_drive": {
                    "client_id": GOOGLE_DRIVE_CLIENT_ID.value,
                    "api_key": GOOGLE_DRIVE_API_KEY.value,
                },
                "onedrive": {
                    "client_id_personal": ONEDRIVE_CLIENT_ID_PERSONAL,
                    "client_id_business": ONEDRIVE_CLIENT_ID_BUSINESS,
                    "sharepoint_url": ONEDRIVE_SHAREPOINT_URL.value,
                    "sharepoint_tenant_id": ONEDRIVE_SHAREPOINT_TENANT_ID.value,
                },
                "ui": {
                    "pending_user_overlay_title": app.state.config.PENDING_USER_OVERLAY_TITLE,
                    "pending_user_overlay_content": app.state.config.PENDING_USER_OVERLAY_CONTENT,
                    "response_watermark": app.state.config.RESPONSE_WATERMARK,
                },
                "license_metadata": app.state.LICENSE_METADATA,
                **(
                    {
                        "active_entries": app.state.USER_COUNT,
                    }
                    if user.role == "admin"
                    else {}
                ),
            }
            if user is not None and (user.role in ["admin", "user"])
            else {
                **(
                    {
                        "ui": {
                            "pending_user_overlay_title": app.state.config.PENDING_USER_OVERLAY_TITLE,
                            "pending_user_overlay_content": app.state.config.PENDING_USER_OVERLAY_CONTENT,
                        }
                    }
                    if user and user.role == "pending"
                    else {}
                ),
                **(
                    {
                        "metadata": {
                            "login_footer": app.state.LICENSE_METADATA.get(
                                "login_footer", ""
                            ),
                            "auth_logo_position": app.state.LICENSE_METADATA.get(
                                "auth_logo_position", ""
                            ),
                        }
                    }
                    if app.state.LICENSE_METADATA
                    else {}
                ),
            }
        ),
    }

    # Per-user payload (auth-derived); cache as private with short TTL + ETag for 304s.
    return etag_response(config_payload, request)


class UrlForm(BaseModel):
    url: str


@app.get("/api/webhook")
async def get_webhook_url(user=Depends(get_admin_user)):
    return {
        "url": app.state.config.WEBHOOK_URL,
    }


@app.post("/api/webhook")
async def update_webhook_url(form_data: UrlForm, user=Depends(get_admin_user)):
    app.state.config.WEBHOOK_URL = form_data.url
    app.state.WEBHOOK_URL = app.state.config.WEBHOOK_URL
    return {"url": app.state.config.WEBHOOK_URL}


@app.get("/api/version")
async def get_app_version():
    return {
        "version": VERSION,
    }


@app.get("/api/version/updates")
async def get_app_latest_release_version(user=Depends(get_verified_user)):
    if not ENABLE_VERSION_UPDATE_CHECK:
        log.debug(
            f"Version update check is disabled, returning current version as latest version"
        )
        return {"current": VERSION, "latest": VERSION}
    try:
        timeout = aiohttp.ClientTimeout(total=1)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                "https://api.github.com/repos/open-webui/open-webui/releases/latest",
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as response:
                response.raise_for_status()
                data = await response.json()
                latest_version = data["tag_name"]

                return {"current": VERSION, "latest": latest_version[1:]}
    except Exception as e:
        log.debug(e)
        return {"current": VERSION, "latest": VERSION}


@app.get("/api/changelog")
async def get_app_changelog():
    return {key: CHANGELOG[key] for idx, key in enumerate(CHANGELOG) if idx < 5}


@app.get("/api/usage")
async def get_current_usage(user=Depends(get_verified_user)):
    """
    Get current usage statistics for Open WebUI.
    This is an experimental endpoint and subject to change.
    """
    try:
        return {"model_ids": get_models_in_use(), "user_ids": get_active_user_ids()}
    except Exception as e:
        log.error(f"Error getting usage statistics: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


############################
# OAuth Login & Callback
############################


# Initialize OAuth client manager with any MCP tool servers using OAuth 2.1
if len(app.state.config.TOOL_SERVER_CONNECTIONS) > 0:
    for tool_server_connection in app.state.config.TOOL_SERVER_CONNECTIONS:
        if tool_server_connection.get("type", "openapi") == "mcp":
            server_id = tool_server_connection.get("info", {}).get("id")
            auth_type = tool_server_connection.get("auth_type", "none")
            if server_id and auth_type == "oauth_2.1":
                oauth_client_info = tool_server_connection.get("info", {}).get(
                    "oauth_client_info", ""
                )

                try:
                    oauth_client_info = decrypt_data(oauth_client_info)
                    app.state.oauth_client_manager.add_client(
                        f"mcp:{server_id}",
                        OAuthClientInformationFull(**oauth_client_info),
                    )
                except Exception as e:
                    log.error(
                        f"Error adding OAuth client for MCP tool server {server_id}: {e}"
                    )
                    pass

try:
    if ENABLE_STAR_SESSIONS_MIDDLEWARE:
        redis_session_store = RedisStore(
            url=REDIS_URL,
            prefix=(f"{REDIS_KEY_PREFIX}:session:" if REDIS_KEY_PREFIX else "session:"),
        )

        app.add_middleware(SessionAutoloadMiddleware)
        app.add_middleware(
            StarSessionsMiddleware,
            store=redis_session_store,
            cookie_name="owui-session",
            cookie_same_site=WEBUI_SESSION_COOKIE_SAME_SITE,
            cookie_https_only=WEBUI_SESSION_COOKIE_SECURE,
        )
        log.info("Using Redis for session")
    else:
        raise ValueError("No Redis URL provided")
except Exception as e:
    app.add_middleware(
        SessionMiddleware,
        secret_key=WEBUI_SECRET_KEY,
        session_cookie="owui-session",
        same_site=WEBUI_SESSION_COOKIE_SAME_SITE,
        https_only=WEBUI_SESSION_COOKIE_SECURE,
    )


@app.get("/oauth/clients/{client_id}/authorize")
async def oauth_client_authorize(
    client_id: str,
    request: Request,
    response: Response,
    user=Depends(get_verified_user),
):
    return await oauth_client_manager.handle_authorize(request, client_id=client_id)


@app.get("/oauth/clients/{client_id}/callback")
async def oauth_client_callback(
    client_id: str,
    request: Request,
    response: Response,
    user=Depends(get_verified_user),
):
    return await oauth_client_manager.handle_callback(
        request,
        client_id=client_id,
        user_id=user.id if user else None,
        response=response,
    )


@app.get("/oauth/{provider}/login")
async def oauth_login(provider: str, request: Request):
    return await oauth_manager.handle_login(request, provider)


# OAuth login logic is as follows:
# 1. Attempt to find a user with matching subject ID, tied to the provider
# 2. If OAUTH_MERGE_ACCOUNTS_BY_EMAIL is true, find a user with the email address provided via OAuth
#    - This is considered insecure in general, as OAuth providers do not always verify email addresses
# 3. If there is no user, and ENABLE_OAUTH_SIGNUP is true, create a user
#    - Email addresses are considered unique, so we fail registration if the email address is already taken
@app.get("/oauth/{provider}/login/callback")
@app.get("/oauth/{provider}/callback")  # Legacy endpoint
async def oauth_login_callback(provider: str, request: Request, response: Response):
    return await oauth_manager.handle_callback(request, provider, response)


@app.get("/manifest.json")
async def get_manifest_json(response: Response):
    response.headers["Cache-Control"] = SPA_REVALIDATE_CACHE_CONTROL

    if app.state.EXTERNAL_PWA_MANIFEST_URL:
        # Async fetch — a blocking requests.get() here would stall the whole event
        # loop for every concurrent request while the external manifest loads.
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(app.state.EXTERNAL_PWA_MANIFEST_URL) as resp:
                return await resp.json()
    else:
        return {
            "id": "/",
            "name": app.state.WEBUI_NAME,
            "short_name": app.state.WEBUI_NAME,
            "description": f"{app.state.WEBUI_NAME} is an open, extensible, user-friendly interface for AI that adapts to your workflow.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#FAFAF7",
            "theme_color": "#FAFAF7",
            "icons": [
                {
                    "src": "/static/web-app-manifest-192x192.png",
                    "type": "image/png",
                    "sizes": "192x192",
                    "purpose": "any",
                },
                {
                    "src": "/static/web-app-manifest-512x512.png",
                    "type": "image/png",
                    "sizes": "512x512",
                    "purpose": "any",
                },
                {
                    "src": "/static/web-app-manifest-512x512.png",
                    "type": "image/png",
                    "sizes": "512x512",
                    "purpose": "maskable",
                },
            ],
            "share_target": {
                "action": "/",
                "method": "GET",
                "params": {"text": "shared"},
            },
            "shortcuts": [
                {
                    "name": "New Chat",
                    "short_name": "New",
                    "url": "/",
                    "description": "Start a new chat",
                }
            ],
            "categories": ["productivity", "utilities"],
            "display_override": ["standalone"],
        }


# In-process cache for generated iOS launch-screen PNGs. Keyed by the raw
# `spec` string (e.g. "1170x2532" or "1170x2532-dark"). The endpoint only
# accepts specs whose (width, height) pair is in `_SPLASH_ALLOWED_SIZES`
# below (the exact sizes referenced by the apple-touch-startup-image <link>
# tags in src/app.html), so the reachable key space is small and fixed —
# a plain dict (no LRU eviction) is safe.
_splash_cache: dict[str, bytes] = {}
_SPLASH_SPEC_RE = re.compile(r"^(\d+)x(\d+)(-dark)?$")

# Exact (width, height) pairs referenced by apple-touch-startup-image <link>
# tags in src/app.html. Keep in sync with that file.
_SPLASH_ALLOWED_SIZES = frozenset(
    {
        (750, 1334),
        (1125, 2436),
        (828, 1792),
        (1242, 2688),
        (1080, 2340),
        (1170, 2532),
        (1284, 2778),
        (1179, 2556),
        (1290, 2796),
        (1206, 2622),
        (1320, 2868),
        (1620, 2160),
        (1640, 2360),
        (1668, 2388),
        (2048, 2732),
    }
)


def _render_splash_image(width: int, height: int, is_dark: bool) -> bytes:
    """Blocking PIL work — must be run off the event loop (threadpool)."""
    from PIL import Image

    bg_color = "#262625" if is_dark else "#FAFAF7"
    logo_path = os.path.join(STATIC_DIR, "splash-dark.png" if is_dark else "splash.png")

    canvas = Image.new("RGBA", (width, height), bg_color)

    try:
        with Image.open(logo_path) as logo:
            logo = logo.convert("RGBA")
            # Scale the logo so it roughly fills 20% of the shorter side,
            # preserving aspect ratio.
            target = int(min(width, height) * 0.2)
            logo_w, logo_h = logo.size
            if logo_w and logo_h and target > 0:
                scale = target / min(logo_w, logo_h)
                new_size = (max(1, int(logo_w * scale)), max(1, int(logo_h * scale)))
                resample = getattr(Image, "Resampling", Image).LANCZOS
                logo = logo.resize(new_size, resample)

            paste_x = (width - logo.size[0]) // 2
            paste_y = (height - logo.size[1]) // 2
            canvas.paste(logo, (paste_x, paste_y), logo)
    except Exception as e:
        log.debug("splash logo composite skipped for %sx%s: %s", width, height, e)

    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


@app.get("/api/splash/{spec}.png")
async def get_splash_image(spec: str):
    """Generate an iOS `apple-touch-startup-image` launch screen on the fly.

    Public/unauthenticated — iOS fetches launch screens without app auth
    headers — so only the exact (width, height) pairs actually referenced
    by src/app.html are accepted (see `_SPLASH_ALLOWED_SIZES`); anything
    else 404s rather than hinting that other sizes might be valid. This
    keeps the cacheable key space small and fixed regardless of who calls
    it. Rendering itself runs in a threadpool so it never blocks the event
    loop.
    """
    match = _SPLASH_SPEC_RE.match(spec)
    if not match:
        raise HTTPException(status_code=404)

    width, height = int(match.group(1)), int(match.group(2))
    is_dark = bool(match.group(3))

    if (width, height) not in _SPLASH_ALLOWED_SIZES:
        raise HTTPException(status_code=404)

    cache_key = spec
    cached = _splash_cache.get(cache_key)
    if cached is None:
        cached = await run_in_threadpool(_render_splash_image, width, height, is_dark)
        _splash_cache[cache_key] = cached

    return Response(
        content=cached,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.get("/opensearch.xml")
async def get_opensearch_xml():
    xml_content = rf"""
    <OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/" xmlns:moz="http://www.mozilla.org/2006/browser/search/">
    <ShortName>{app.state.WEBUI_NAME}</ShortName>
    <Description>Search {app.state.WEBUI_NAME}</Description>
    <InputEncoding>UTF-8</InputEncoding>
    <Image width="16" height="16" type="image/x-icon">{app.state.config.WEBUI_URL}/static/favicon.png</Image>
    <Url type="text/html" method="get" template="{app.state.config.WEBUI_URL}/?q={"{searchTerms}"}"/>
    <moz:SearchForm>{app.state.config.WEBUI_URL}</moz:SearchForm>
    </OpenSearchDescription>
    """
    return Response(content=xml_content, media_type="application/xml")


# Token Usage API Models
class TokenGroupCreate(BaseModel):
    name: str
    models: list[str]
    limit: Optional[int] = None
    resetTime: Optional[str] = "00:00"
    resetTimezone: Optional[str] = "UTC"


class TokenGroupUpdate(BaseModel):
    models: Optional[list[str]] = None
    limit: Optional[int] = None
    resetTime: Optional[str] = None
    resetTimezone: Optional[str] = None


# Token Usage API Endpoints
@app.get("/api/usage/groups")
async def get_usage_groups(user=Depends(get_verified_user)):
    """
    Get all token groups with their usage.

    This endpoint now proactively checks for and applies resets,
    ensuring clients always see the correct token counts even if
    no messages have been sent since the reset time.

    Response includes:
    - models: list of model IDs in the group
    - limit: token limit
    - usage: {in, out, total}
    - next_reset_at: Unix timestamp of next reset (for client-side scheduling)
    - reset_type: 'daily' or 'rolling_window'
    """
    # get_token_groups now handles reset checks and returns full data
    groups = await get_token_groups()

    # Subscription-provider usage rides along so the mount-time fetch seeds
    # both bars in one request. Kick a background refresh when the snapshot is
    # stale (poller idles while no sessions are connected); any change lands
    # via the subscription-usage:update push moments later.
    from open_webui.utils.subscription_usage import (
        get_subscription_usage_state,
        kick_refresh_if_stale,
    )

    kick_refresh_if_stale()
    return {"groups": groups, "subscriptions": get_subscription_usage_state()}


@app.post("/api/usage/groups")
async def create_usage_group(
    form_data: TokenGroupCreate, user=Depends(get_verified_user)
):
    """Create a new token group"""
    try:
        await set_token_group(
            form_data.name,
            form_data.models,
            form_data.limit,
            form_data.resetTime,
            form_data.resetTimezone,
        )

        # Get the created group data (includes usage and reset info)
        groups = await get_token_groups()
        group_data = groups.get(form_data.name, {})

        return {"status": True, "group": {"name": form_data.name, **group_data}}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/usage/groups/{name}")
async def update_usage_group(
    name: str, form_data: TokenGroupUpdate, user=Depends(get_verified_user)
):
    """Update an existing token group"""
    try:
        success = await update_token_group(name, form_data.models, form_data.limit)
        if not success:
            raise HTTPException(status_code=404, detail="Group not found")

        # Get the updated group data (includes usage and reset info)
        groups = await get_token_groups()
        group_data = groups.get(name, {})

        return {"status": True, "group": {"name": name, **group_data}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/usage/groups/{name}")
async def delete_usage_group(name: str, user=Depends(get_verified_user)):
    """Delete a token group"""
    try:
        success = await delete_token_group(name)
        if not success:
            raise HTTPException(status_code=404, detail="Group not found")

        return {"status": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/usage/reset")
async def manual_reset_usage(user=Depends(get_verified_user)):
    """Manually reset all token usage (for testing daily reset functionality)"""
    try:
        from open_webui.models.token_usage import token_groups

        success = await token_groups.force_reset_all_usage()

        if success:
            return {
                "status": True,
                "message": "All token usage counters have been reset to 0",
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to reset token usage")

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/health")
async def healthcheck():
    return {"status": True}


@app.get("/health/db")
async def healthcheck_with_db():
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": True}


############################
# SharedChatPage
#
# Serves the SPA shell for /s/{share_id} with per-chat link-preview meta tags
# (og:title = chat title, og:description = first-message snippet) so JS-less
# crawlers (Discord/WhatsApp/iMessage) unfurl the actual conversation instead
# of the generic instance blurb. Registered before the catch-all SPA mount,
# which would otherwise serve the unmodified shell for this path.
############################


@app.get("/s/{share_id}")
async def get_shared_chat_page(
    share_id: str, request: Request, user=Depends(get_optional_user)
):
    try:
        chat = await Chats.resolve_shared_chat(share_id)
    except Exception:
        chat = None

    # Not found / not previewable keeps title/description None, which serves
    # the plain injected shell so the SPA loads and applies its own not-found
    # redirect behavior.
    title = description = None
    if chat and not (user and user.role == "pending"):
        # Same public shaping as GET /api/v1/chats/share/{share_id}: only data
        # an anonymous viewer could already fetch is embedded in the HTML.
        sanitized = sanitize_shared_chat_model(chat, share_id=share_id)
        title, description = _shared_chat_link_preview(sanitized)

    response = _build_injected_index_response(
        request.scope,
        FRONTEND_BUILD_DIR,
        title=title,
        description=description,
        url=str(request.url),
    )
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return response


app.mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static")


@app.get("/cache/{path:path}")
async def serve_cache_file(
    path: str,
    user=Depends(get_verified_user),
):
    file_path = os.path.abspath(os.path.join(CACHE_DIR, path))
    # prevent path traversal
    if not file_path.startswith(os.path.abspath(CACHE_DIR)):
        raise HTTPException(status_code=404, detail="File not found")
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


def swagger_ui_html(*args, **kwargs):
    return get_swagger_ui_html(
        *args,
        **kwargs,
        swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui/swagger-ui.css",
        swagger_favicon_url="/static/swagger-ui/favicon.png",
    )


applications.get_swagger_ui_html = swagger_ui_html

if os.path.exists(FRONTEND_BUILD_DIR):
    mimetypes.add_type("text/javascript", ".js")
    app.mount(
        "/",
        SPAStaticFiles(directory=FRONTEND_BUILD_DIR, html=True),
        name="spa-static-files",
    )
else:
    log.warning(
        f"Frontend build directory not found at '{FRONTEND_BUILD_DIR}'. Serving API only."
    )
