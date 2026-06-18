import importlib.metadata
import json
import logging
import os
import pkgutil
import sys
import shutil
from uuid import uuid4
from pathlib import Path
from cryptography.hazmat.primitives import serialization

import markdown
from bs4 import BeautifulSoup
from open_webui.constants import ERROR_MESSAGES

####################################
# Load .env file
####################################

# Use .resolve() to get the canonical path, removing any '..' or '.' components
ENV_FILE_PATH = Path(__file__).resolve()

# OPEN_WEBUI_DIR should be the directory where env.py resides (open_webui/)
OPEN_WEBUI_DIR = ENV_FILE_PATH.parent

# BACKEND_DIR is the parent of OPEN_WEBUI_DIR (backend/)
BACKEND_DIR = OPEN_WEBUI_DIR.parent

# BASE_DIR is the parent of BACKEND_DIR (open-webui-dev/)
BASE_DIR = BACKEND_DIR.parent

try:
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(str(BASE_DIR / ".env")))
except ImportError:
    print("dotenv not installed, skipping...")

DOCKER = os.environ.get("DOCKER", "False").lower() == "true"

# device type embedding models - "cpu" (default), "cuda" (nvidia gpu required) or "mps" (apple silicon) - choosing this right can lead to better performance
USE_CUDA = os.environ.get("USE_CUDA_DOCKER", "false")

if USE_CUDA.lower() == "true":
    try:
        import torch

        assert torch.cuda.is_available(), "CUDA not available"
        DEVICE_TYPE = "cuda"
    except Exception as e:
        cuda_error = (
            "Error when testing CUDA but USE_CUDA_DOCKER is true. "
            f"Resetting USE_CUDA_DOCKER to false: {e}"
        )
        os.environ["USE_CUDA_DOCKER"] = "false"
        USE_CUDA = "false"
        DEVICE_TYPE = "cpu"
else:
    DEVICE_TYPE = "cpu"

try:
    import torch

    if torch.backends.mps.is_available() and torch.backends.mps.is_built():
        DEVICE_TYPE = "mps"
except Exception:
    pass

####################################
# LOGGING
####################################

GLOBAL_LOG_LEVEL = os.environ.get("GLOBAL_LOG_LEVEL", "").upper()
if GLOBAL_LOG_LEVEL in logging.getLevelNamesMapping():
    logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL, force=True)
else:
    GLOBAL_LOG_LEVEL = "INFO"

log = logging.getLogger(__name__)
log.info(f"GLOBAL_LOG_LEVEL: {GLOBAL_LOG_LEVEL}")

if "cuda_error" in locals():
    log.exception(cuda_error)
    del cuda_error

log_sources = [
    "AUDIO",
    "COMFYUI",
    "CONFIG",
    "DB",
    "IMAGES",
    "MAIN",
    "MODELS",
    "OLLAMA",
    "OPENAI",
    "RAG",
    "WEBHOOK",
    "SOCKET",
    "OAUTH",
]

SRC_LOG_LEVELS = {}

for source in log_sources:
    log_env_var = source + "_LOG_LEVEL"
    SRC_LOG_LEVELS[source] = os.environ.get(log_env_var, "").upper()
    if SRC_LOG_LEVELS[source] not in logging.getLevelNamesMapping():
        SRC_LOG_LEVELS[source] = GLOBAL_LOG_LEVEL
    log.info(f"{log_env_var}: {SRC_LOG_LEVELS[source]}")

log.setLevel(SRC_LOG_LEVELS["CONFIG"])

WEBUI_NAME = os.environ.get("WEBUI_NAME", "Open WebUI")
if WEBUI_NAME != "Open WebUI":
    WEBUI_NAME += " (Open WebUI)"

WEBUI_FAVICON_URL = "https://openwebui.com/favicon.png"

TRUSTED_SIGNATURE_KEY = os.environ.get("TRUSTED_SIGNATURE_KEY", "")

####################################
# ENV (dev,test,prod)
####################################

ENV = os.environ.get("ENV", "dev")

FROM_INIT_PY = os.environ.get("FROM_INIT_PY", "False").lower() == "true"

if FROM_INIT_PY:
    PACKAGE_DATA = {"version": importlib.metadata.version("open-webui")}
else:
    try:
        PACKAGE_DATA = json.loads((BASE_DIR / "package.json").read_text())
    except Exception:
        PACKAGE_DATA = {"version": "0.0.0"}

VERSION = PACKAGE_DATA["version"]
INSTANCE_ID = os.environ.get("INSTANCE_ID", str(uuid4()))


# Function to parse each section
def parse_section(section):
    items = []
    for li in section.find_all("li"):
        # Extract raw HTML string
        raw_html = str(li)

        # Extract text without HTML tags
        text = li.get_text(separator=" ", strip=True)

        # Split into title and content
        parts = text.split(": ", 1)
        title = parts[0].strip() if len(parts) > 1 else ""
        content = parts[1].strip() if len(parts) > 1 else text

        items.append({"title": title, "content": content, "raw": raw_html})
    return items


def load_changelog_content() -> str:
    changelog_path = BASE_DIR / "CHANGELOG.md"

    try:
        return changelog_path.read_text(encoding="utf8")
    except FileNotFoundError:
        pass
    except UnicodeDecodeError:
        log.warning("Local CHANGELOG.md is not valid UTF-8; falling back to bundled data")
    except Exception as e:
        log.debug(f"Unable to read local CHANGELOG.md: {e}")

    try:
        changelog_bytes = pkgutil.get_data("open_webui", "CHANGELOG.md")
        if not changelog_bytes:
            return ""
        return changelog_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"Unable to read bundled CHANGELOG.md: {e}")
        return ""

# Convert markdown content to HTML
changelog_content = load_changelog_content()
html_content = markdown.markdown(changelog_content)

# Parse the HTML content
soup = BeautifulSoup(html_content, "html.parser")

# Initialize JSON structure
changelog_json = {}

# Iterate over each version
for version in soup.find_all("h2"):
    version_number = version.get_text().strip().split(" - ")[0][1:-1]  # Remove brackets
    date = version.get_text().strip().split(" - ")[1]

    version_data = {"date": date}

    # Find the next sibling that is a h3 tag (section title)
    current = version.find_next_sibling()

    while current and current.name != "h2":
        if current.name == "h3":
            section_title = current.get_text().lower()  # e.g., "added", "fixed"
            section_items = parse_section(current.find_next_sibling("ul"))
            version_data[section_title] = section_items

        # Move to the next element
        current = current.find_next_sibling()

    changelog_json[version_number] = version_data

CHANGELOG = changelog_json

####################################
# SAFE_MODE
####################################

SAFE_MODE = os.environ.get("SAFE_MODE", "false").lower() == "true"


####################################
# ENABLE_FORWARD_USER_INFO_HEADERS
####################################

ENABLE_FORWARD_USER_INFO_HEADERS = (
    os.environ.get("ENABLE_FORWARD_USER_INFO_HEADERS", "False").lower() == "true"
)

# Experimental feature, may be removed in future
ENABLE_STAR_SESSIONS_MIDDLEWARE = (
    os.environ.get("ENABLE_STAR_SESSIONS_MIDDLEWARE", "False").lower() == "true"
)

####################################
# WEBUI_BUILD_HASH
####################################

WEBUI_BUILD_HASH = os.environ.get("WEBUI_BUILD_HASH", "dev-build")

####################################
# DATA/FRONTEND BUILD DIR
####################################
#
# IMPORTANT — production deployment override:
#   On the intelserver production host, DATA_DIR is overridden via the
#   ``start_modified.sh`` wrapper to:
#     /home/tennisbowling/.local/lib/python3.12/site-packages/open_webui/data
#   That is where ``webui.db`` actually lives in production (~1.9 GB).
#   The in-repo ``backend/data/webui.db`` is a stale leftover.
#   Any DB-touching maintenance task (backups, migrations, sqlite shells)
#   must export ``DATA_DIR`` to the path above or it will hit the wrong file.

DATA_DIR = Path(os.getenv("DATA_DIR", BACKEND_DIR / "data")).resolve()

if FROM_INIT_PY:
    NEW_DATA_DIR = Path(os.getenv("DATA_DIR", OPEN_WEBUI_DIR / "data")).resolve()
    NEW_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Check if the data directory exists in the package directory
    if DATA_DIR.exists() and DATA_DIR != NEW_DATA_DIR:
        log.info(f"Moving {DATA_DIR} to {NEW_DATA_DIR}")
        for item in DATA_DIR.iterdir():
            dest = NEW_DATA_DIR / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        # Zip the data directory
        shutil.make_archive(DATA_DIR.parent / "open_webui_data", "zip", DATA_DIR)

        # Remove the old data directory
        shutil.rmtree(DATA_DIR)

    DATA_DIR = Path(os.getenv("DATA_DIR", OPEN_WEBUI_DIR / "data"))

STATIC_DIR = Path(os.getenv("STATIC_DIR", OPEN_WEBUI_DIR / "static"))

FONTS_DIR = Path(os.getenv("FONTS_DIR", OPEN_WEBUI_DIR / "static" / "fonts"))

FRONTEND_BUILD_DIR = Path(os.getenv("FRONTEND_BUILD_DIR", BASE_DIR / "build")).resolve()

if FROM_INIT_PY:
    FRONTEND_BUILD_DIR = Path(
        os.getenv("FRONTEND_BUILD_DIR", OPEN_WEBUI_DIR / "frontend")
    ).resolve()

####################################
# Database
####################################

# Check if the file exists
if os.path.exists(f"{DATA_DIR}/ollama.db"):
    # Rename the file
    os.rename(f"{DATA_DIR}/ollama.db", f"{DATA_DIR}/webui.db")
    log.info("Database migrated from Ollama-WebUI successfully.")
else:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR}/webui.db")

DATABASE_TYPE = os.environ.get("DATABASE_TYPE")
DATABASE_USER = os.environ.get("DATABASE_USER")
DATABASE_PASSWORD = os.environ.get("DATABASE_PASSWORD")

DATABASE_CRED = ""
if DATABASE_USER:
    DATABASE_CRED += f"{DATABASE_USER}"
if DATABASE_PASSWORD:
    DATABASE_CRED += f":{DATABASE_PASSWORD}"

DB_VARS = {
    "db_type": DATABASE_TYPE,
    "db_cred": DATABASE_CRED,
    "db_host": os.environ.get("DATABASE_HOST"),
    "db_port": os.environ.get("DATABASE_PORT"),
    "db_name": os.environ.get("DATABASE_NAME"),
}

if all(DB_VARS.values()):
    DATABASE_URL = f"{DB_VARS['db_type']}://{DB_VARS['db_cred']}@{DB_VARS['db_host']}:{DB_VARS['db_port']}/{DB_VARS['db_name']}"
elif DATABASE_TYPE == "sqlite+sqlcipher" and not os.environ.get("DATABASE_URL"):
    # Handle SQLCipher with local file when DATABASE_URL wasn't explicitly set
    DATABASE_URL = f"sqlite+sqlcipher:///{DATA_DIR}/webui.db"

# Replace the postgres:// with postgresql://
if "postgres://" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

DATABASE_SCHEMA = os.environ.get("DATABASE_SCHEMA", None)

DATABASE_POOL_SIZE = os.environ.get("DATABASE_POOL_SIZE", None)

if DATABASE_POOL_SIZE != None:
    try:
        DATABASE_POOL_SIZE = int(DATABASE_POOL_SIZE)
    except Exception:
        DATABASE_POOL_SIZE = None

DATABASE_POOL_MAX_OVERFLOW = os.environ.get("DATABASE_POOL_MAX_OVERFLOW", 0)

if DATABASE_POOL_MAX_OVERFLOW == "":
    DATABASE_POOL_MAX_OVERFLOW = 0
else:
    try:
        DATABASE_POOL_MAX_OVERFLOW = int(DATABASE_POOL_MAX_OVERFLOW)
    except Exception:
        DATABASE_POOL_MAX_OVERFLOW = 0

DATABASE_POOL_TIMEOUT = os.environ.get("DATABASE_POOL_TIMEOUT", 30)

if DATABASE_POOL_TIMEOUT == "":
    DATABASE_POOL_TIMEOUT = 30
else:
    try:
        DATABASE_POOL_TIMEOUT = int(DATABASE_POOL_TIMEOUT)
    except Exception:
        DATABASE_POOL_TIMEOUT = 30

DATABASE_POOL_RECYCLE = os.environ.get("DATABASE_POOL_RECYCLE", 3600)

if DATABASE_POOL_RECYCLE == "":
    DATABASE_POOL_RECYCLE = 3600
else:
    try:
        DATABASE_POOL_RECYCLE = int(DATABASE_POOL_RECYCLE)
    except Exception:
        DATABASE_POOL_RECYCLE = 3600

DATABASE_ENABLE_SQLITE_WAL = (
    os.environ.get("DATABASE_ENABLE_SQLITE_WAL", "False").lower() == "true"
)

DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL = os.environ.get(
    "DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL", None
)
if DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL is not None:
    try:
        DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL = float(
            DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL
        )
    except Exception:
        DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL = 0.0

RESET_CONFIG_ON_START = (
    os.environ.get("RESET_CONFIG_ON_START", "False").lower() == "true"
)

ENABLE_REALTIME_CHAT_SAVE = (
    os.environ.get("ENABLE_REALTIME_CHAT_SAVE", "True").lower() == "true"
)

DISABLE_STREAM_SNAPSHOT_DB_WRITES = (
    os.environ.get("DISABLE_STREAM_SNAPSHOT_DB_WRITES", "False").lower() == "true"
)

ENABLE_QUERIES_CACHE = os.environ.get("ENABLE_QUERIES_CACHE", "False").lower() == "true"

####################################
# REDIS
####################################

REDIS_URL = os.environ.get("REDIS_URL", "")
REDIS_CLUSTER = os.environ.get("REDIS_CLUSTER", "False").lower() == "true"

REDIS_KEY_PREFIX = os.environ.get("REDIS_KEY_PREFIX", "open-webui")

REDIS_SENTINEL_HOSTS = os.environ.get("REDIS_SENTINEL_HOSTS", "")
REDIS_SENTINEL_PORT = os.environ.get("REDIS_SENTINEL_PORT", "26379")

# Maximum number of retries for Redis operations when using Sentinel fail-over
REDIS_SENTINEL_MAX_RETRY_COUNT = os.environ.get("REDIS_SENTINEL_MAX_RETRY_COUNT", "2")
try:
    REDIS_SENTINEL_MAX_RETRY_COUNT = int(REDIS_SENTINEL_MAX_RETRY_COUNT)
    if REDIS_SENTINEL_MAX_RETRY_COUNT < 1:
        REDIS_SENTINEL_MAX_RETRY_COUNT = 2
except ValueError:
    REDIS_SENTINEL_MAX_RETRY_COUNT = 2

####################################
# UVICORN WORKERS
####################################

# Number of uvicorn worker processes for handling requests
UVICORN_WORKERS = os.environ.get("UVICORN_WORKERS", "1")
try:
    UVICORN_WORKERS = int(UVICORN_WORKERS)
    if UVICORN_WORKERS < 1:
        UVICORN_WORKERS = 1
except ValueError:
    UVICORN_WORKERS = 1
    log.info(f"Invalid UVICORN_WORKERS value, defaulting to {UVICORN_WORKERS}")

####################################
# WEBUI_AUTH (Required for security)
####################################

WEBUI_AUTH = os.environ.get("WEBUI_AUTH", "True").lower() == "true"

ENABLE_INITIAL_ADMIN_SIGNUP = (
    os.environ.get("ENABLE_INITIAL_ADMIN_SIGNUP", "False").lower() == "true"
)
ENABLE_SIGNUP_PASSWORD_CONFIRMATION = (
    os.environ.get("ENABLE_SIGNUP_PASSWORD_CONFIRMATION", "False").lower() == "true"
)

WEBUI_AUTH_TRUSTED_EMAIL_HEADER = os.environ.get(
    "WEBUI_AUTH_TRUSTED_EMAIL_HEADER", None
)
WEBUI_AUTH_TRUSTED_NAME_HEADER = os.environ.get("WEBUI_AUTH_TRUSTED_NAME_HEADER", None)
WEBUI_AUTH_TRUSTED_GROUPS_HEADER = os.environ.get(
    "WEBUI_AUTH_TRUSTED_GROUPS_HEADER", None
)


BYPASS_MODEL_ACCESS_CONTROL = (
    os.environ.get("BYPASS_MODEL_ACCESS_CONTROL", "False").lower() == "true"
)

WEBUI_AUTH_SIGNOUT_REDIRECT_URL = os.environ.get(
    "WEBUI_AUTH_SIGNOUT_REDIRECT_URL", None
)

####################################
# WEBUI_SECRET_KEY
####################################

WEBUI_SECRET_KEY = os.environ.get(
    "WEBUI_SECRET_KEY",
    os.environ.get(
        "WEBUI_JWT_SECRET_KEY", "t0p-s3cr3t"
    ),  # DEPRECATED: remove at next major version
)

WEBUI_SESSION_COOKIE_SAME_SITE = os.environ.get("WEBUI_SESSION_COOKIE_SAME_SITE", "lax")

WEBUI_SESSION_COOKIE_SECURE = (
    os.environ.get("WEBUI_SESSION_COOKIE_SECURE", "false").lower() == "true"
)

WEBUI_AUTH_COOKIE_SAME_SITE = os.environ.get(
    "WEBUI_AUTH_COOKIE_SAME_SITE", WEBUI_SESSION_COOKIE_SAME_SITE
)

WEBUI_AUTH_COOKIE_SECURE = (
    os.environ.get(
        "WEBUI_AUTH_COOKIE_SECURE",
        os.environ.get("WEBUI_SESSION_COOKIE_SECURE", "false"),
    ).lower()
    == "true"
)

if WEBUI_AUTH and WEBUI_SECRET_KEY == "":
    raise ValueError(ERROR_MESSAGES.ENV_VAR_NOT_FOUND)

ENABLE_COMPRESSION_MIDDLEWARE = (
    os.environ.get("ENABLE_COMPRESSION_MIDDLEWARE", "True").lower() == "true"
)

####################################
# OAUTH Configuration
####################################
ENABLE_OAUTH_EMAIL_FALLBACK = (
    os.environ.get("ENABLE_OAUTH_EMAIL_FALLBACK", "False").lower() == "true"
)

ENABLE_OAUTH_ID_TOKEN_COOKIE = (
    os.environ.get("ENABLE_OAUTH_ID_TOKEN_COOKIE", "True").lower() == "true"
)

OAUTH_CLIENT_INFO_ENCRYPTION_KEY = os.environ.get(
    "OAUTH_CLIENT_INFO_ENCRYPTION_KEY", WEBUI_SECRET_KEY
)

OAUTH_SESSION_TOKEN_ENCRYPTION_KEY = os.environ.get(
    "OAUTH_SESSION_TOKEN_ENCRYPTION_KEY", WEBUI_SECRET_KEY
)

####################################
# SCIM Configuration
####################################

SCIM_ENABLED = os.environ.get("SCIM_ENABLED", "False").lower() == "true"
SCIM_TOKEN = os.environ.get("SCIM_TOKEN", "")

####################################
# LICENSE_KEY
####################################

LICENSE_KEY = os.environ.get("LICENSE_KEY", "")

LICENSE_BLOB = None
LICENSE_BLOB_PATH = os.environ.get("LICENSE_BLOB_PATH", DATA_DIR / "l.data")
if LICENSE_BLOB_PATH and os.path.exists(LICENSE_BLOB_PATH):
    with open(LICENSE_BLOB_PATH, "rb") as f:
        LICENSE_BLOB = f.read()

LICENSE_PUBLIC_KEY = os.environ.get("LICENSE_PUBLIC_KEY", "")

pk = None
if LICENSE_PUBLIC_KEY:
    pk = serialization.load_pem_public_key(
        f"""
-----BEGIN PUBLIC KEY-----
{LICENSE_PUBLIC_KEY}
-----END PUBLIC KEY-----
""".encode(
            "utf-8"
        )
    )


####################################
# MODELS
####################################

MODELS_CACHE_TTL = os.environ.get("MODELS_CACHE_TTL", "1")
if MODELS_CACHE_TTL == "":
    MODELS_CACHE_TTL = None
else:
    try:
        MODELS_CACHE_TTL = int(MODELS_CACHE_TTL)
    except Exception:
        MODELS_CACHE_TTL = 1


####################################
# CHAT
####################################

CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE = os.environ.get(
    "CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE", "1"
)

if CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE == "":
    CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE = 1
else:
    try:
        CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE = int(
            CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE
        )
    except Exception:
        CHAT_RESPONSE_STREAM_DELTA_CHUNK_SIZE = 1


# Stream protocol selector. v1 emits full content_blocks every flush; v2 emits
# only deltas (text_append, block_open/close, tool_call:result, chat:done).
STREAM_PROTOCOL_VERSION = os.environ.get("STREAM_PROTOCOL_VERSION", "v2").strip() or "v2"
if STREAM_PROTOCOL_VERSION not in ("v1", "v2"):
    STREAM_PROTOCOL_VERSION = "v2"

# Under v2, coalesce per-tick `chat:delta` and `tool_call:result` socket
# emissions into a single `chat:delta:batch` envelope per user. Reduces socket
# I/O at high concurrency (many subagents streaming simultaneously). Opt-out
# via STREAM_DELTA_BATCH_ENABLED=false for ops if anything regresses.
STREAM_DELTA_BATCH_ENABLED = (
    os.environ.get("STREAM_DELTA_BATCH_ENABLED", "true").strip().lower()
    not in ("0", "false", "no", "off")
)

# TTL (seconds) applied to the per-stream Redis hashes (stream_version,
# tool_results, stream_state). Set once at stream_version_init so any reasonable
# stream completes within the window; orphan entries from crashed/killed workers
# expire after the TTL. Default 48h.
STREAM_STATE_TTL_SECONDS = int(os.environ.get("STREAM_STATE_TTL_SECONDS", "172800"))

# Keep the default parent-chat delta flush size at 1 so v2 still feels like
# token streaming. High-throughput inner/subagent streams can opt into larger
# batches via metadata.params.stream_delta_chunk_size.


####################################
# AGENTIC TOOL-CALL LOOP BOUNDS
####################################

# Hard ceiling on the number of tool-call rounds in a single agentic response.
# The loop is otherwise `while len(tool_calls) > 0` with no backstop, so a model
# that calls tools forever runs unbounded DB writes / token spend / wall time.
# When hit, the loop stops requesting more tools and lets the model produce a
# final answer (a synthetic notice is fed in). 0/negative disables the cap.
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


# Hard cap on agentic tool-call rounds for a single chat turn. DISABLED by
# default (0): neither the parent chat nor subagents are capped, so the model
# can keep calling tools as long as it needs. Set a positive value only if you
# want an ops backstop; on reaching it the next model call is forced tool-free
# so it must produce a final answer. Subagents are exempt regardless (see
# utils/middleware.py) so re-enabling a parent cap never limits subagent depth.
AGENTIC_MAX_TOOL_ROUNDS = _int_env("AGENTIC_MAX_TOOL_ROUNDS", 0)

# Re-issue a model round that completed successfully but produced NOTHING usable
# (no tool calls AND no/empty assistant text) up to this many times before giving
# up. Models occasionally end a turn on a bare reasoning block or an empty
# completion; without this the agentic loop just stops with no answer. Applies to
# regular chats AND subagents (the subagent inner pipeline re-enters the same
# loop). Does NOT retry provider errors or user cancels — only the "successful but
# empty" case. Each retry is a full LLM round, so keep this small. 0 disables.
AGENTIC_EMPTY_ROUND_MAX_RETRIES = _int_env("AGENTIC_EMPTY_ROUND_MAX_RETRIES", 3)

# Per-subagent wall-clock timeout (seconds). DISABLED by default (0): subagents
# must be free to research as deeply as they need, for as long as they need —
# no time ceiling on how far they can dig. Set a positive value only if you want
# an ops backstop against a subagent whose own tool genuinely hangs; on timeout
# the subagent is cancelled and a non-empty error result is fed back to the
# parent model (preserving the "one non-empty tool message per tool_call"
# invariant). 0/negative disables the timeout.
SUBAGENT_RUN_TIMEOUT_SECONDS = _int_env("SUBAGENT_RUN_TIMEOUT_SECONDS", 0)

# Max number of subagents allowed to run concurrently per worker. `subagent_launch`
# is parallelizable, so a single parent turn can otherwise gather an unbounded
# number of full nested pipelines (each = a hidden chat + MCP connects + web
# traffic). DISABLED by default (0): subagents fan out without a concurrency
# ceiling. Set a positive value only if you want an ops backstop. 0/negative
# disables the bound (the run path then skips the semaphore entirely).
SUBAGENT_MAX_CONCURRENCY = _int_env("SUBAGENT_MAX_CONCURRENCY", 0)

# Provider-facing subagent streaming. Disabled by default: subagents still run
# through Open WebUI's local stream/agentic pipeline for UI updates and tool
# loops, but their upstream LLM requests are sent as non-streaming JSON bodies.
# Set true to restore the older provider streaming behavior.
SUBAGENT_PROVIDER_STREAM = (
    os.environ.get("SUBAGENT_PROVIDER_STREAM", "False").lower() == "true"
)


####################################
# PROFILING (opt-in, off by default — zero overhead when disabled)
####################################

# Background event-loop lag monitor. When enabled, a task wakes every
# PROFILE_LOOP_LAG_INTERVAL seconds and measures how late it actually fired
# (actual minus expected). On a saturated single loop that drift is the stall
# every other coroutine — including socket delta delivery — is waiting on. The
# monitor logs max + p95 stall (ms) over a rolling window. Use it to quantify
# "the loop is maxed" and to prove whether uvloop/orjson/offload helped.
PROFILE_LOOP_LAG = os.environ.get("PROFILE_LOOP_LAG", "False").lower() == "true"
PROFILE_LOOP_LAG_INTERVAL = float(
    os.environ.get("PROFILE_LOOP_LAG_INTERVAL", "0.05")
)
PROFILE_LOOP_LAG_WINDOW_SECONDS = float(
    os.environ.get("PROFILE_LOOP_LAG_WINDOW_SECONDS", "10")
)

# Per-response cProfile around process_chat_response. When enabled, each
# streaming response is profiled and its stats dumped to PROFILE_CHAT_DIR as a
# .pstats file keyed by chat/message id. Off by default (the wrapper is skipped
# entirely, so there is no overhead unless you opt in). Inspect with
# `python -m pstats <file>` or snakeviz.
PROFILE_CHAT = os.environ.get("PROFILE_CHAT", "False").lower() == "true"
PROFILE_CHAT_DIR = os.environ.get(
    "PROFILE_CHAT_DIR", str(DATA_DIR / "profiles")
)


####################################
# WEBSOCKET SUPPORT
####################################

ENABLE_WEBSOCKET_SUPPORT = (
    os.environ.get("ENABLE_WEBSOCKET_SUPPORT", "True").lower() == "true"
)


WEBSOCKET_MANAGER = os.environ.get("WEBSOCKET_MANAGER", "")

WEBSOCKET_REDIS_URL = os.environ.get("WEBSOCKET_REDIS_URL", REDIS_URL)
WEBSOCKET_REDIS_CLUSTER = (
    os.environ.get("WEBSOCKET_REDIS_CLUSTER", str(REDIS_CLUSTER)).lower() == "true"
)

websocket_redis_lock_timeout = os.environ.get("WEBSOCKET_REDIS_LOCK_TIMEOUT", "60")

try:
    WEBSOCKET_REDIS_LOCK_TIMEOUT = int(websocket_redis_lock_timeout)
except ValueError:
    WEBSOCKET_REDIS_LOCK_TIMEOUT = 60

WEBSOCKET_SENTINEL_HOSTS = os.environ.get("WEBSOCKET_SENTINEL_HOSTS", "")
WEBSOCKET_SENTINEL_PORT = os.environ.get("WEBSOCKET_SENTINEL_PORT", "26379")

# Maximum message size for WebSocket/SocketIO in bytes
# Default 10MB to handle large reasoning tokens from models like o1/o3
websocket_max_message_size = os.environ.get("WEBSOCKET_MAX_MESSAGE_SIZE", "10000000")

try:
    WEBSOCKET_MAX_MESSAGE_SIZE = int(websocket_max_message_size)
except ValueError:
    WEBSOCKET_MAX_MESSAGE_SIZE = 10000000  # 10MB default


# AIOHTTP_CLIENT_TIMEOUT: Set to None for infinite timeout (recommended for image/file processing)
# Set to a number of seconds to limit the timeout
AIOHTTP_CLIENT_TIMEOUT = os.environ.get("AIOHTTP_CLIENT_TIMEOUT", "")

if AIOHTTP_CLIENT_TIMEOUT == "":
    # Default to None (infinite timeout) for better handling of large files and images
    AIOHTTP_CLIENT_TIMEOUT = None
else:
    try:
        AIOHTTP_CLIENT_TIMEOUT = int(AIOHTTP_CLIENT_TIMEOUT)
    except Exception:
        # If parsing fails, default to infinite timeout instead of 300
        AIOHTTP_CLIENT_TIMEOUT = None


AIOHTTP_CLIENT_SESSION_SSL = (
    os.environ.get("AIOHTTP_CLIENT_SESSION_SSL", "True").lower() == "true"
)


# Streaming chat-completion timeout. The streaming request to the upstream model
# provider must NOT use aiohttp's built-in 5-minute `total` default — a long
# reasoning answer or a deep research subagent legitimately streams for many
# minutes, and a `total` cap cancels it mid-generation (surfaces as a spurious
# CancelledError that strands subagents). We therefore set `total=None` and guard
# only against a genuinely DEAD connection with `sock_read` — the max gap allowed
# BETWEEN received bytes. An actively-generating model emits tokens well within
# this window, so it never trips during real work; a stalled/half-open socket
# trips it and fails cleanly instead of hanging forever. Set to 0/empty to
# disable the sock_read guard too (fully unbounded).
AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ = os.environ.get(
    "AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ", "600"
)
if AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ in ("", "0"):
    AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ = None
else:
    try:
        AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ = int(
            AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ
        )
    except Exception:
        AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ = 600

AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST = os.environ.get(
    "AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST",
    os.environ.get("AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST", "10"),
)

if AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST == "":
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST = None
else:
    try:
        AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST = int(AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    except Exception:
        AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST = 10


AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA = os.environ.get(
    "AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA", "10"
)

if AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA == "":
    AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA = None
else:
    try:
        AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA = int(
            AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA
        )
    except Exception:
        AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER_DATA = 10


AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL = (
    os.environ.get("AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL", "True").lower() == "true"
)


####################################
# SENTENCE TRANSFORMERS
####################################


SENTENCE_TRANSFORMERS_BACKEND = os.environ.get("SENTENCE_TRANSFORMERS_BACKEND", "")
if SENTENCE_TRANSFORMERS_BACKEND == "":
    SENTENCE_TRANSFORMERS_BACKEND = "torch"


SENTENCE_TRANSFORMERS_MODEL_KWARGS = os.environ.get(
    "SENTENCE_TRANSFORMERS_MODEL_KWARGS", ""
)
if SENTENCE_TRANSFORMERS_MODEL_KWARGS == "":
    SENTENCE_TRANSFORMERS_MODEL_KWARGS = None
else:
    try:
        SENTENCE_TRANSFORMERS_MODEL_KWARGS = json.loads(
            SENTENCE_TRANSFORMERS_MODEL_KWARGS
        )
    except Exception:
        SENTENCE_TRANSFORMERS_MODEL_KWARGS = None


SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND = os.environ.get(
    "SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND", ""
)
if SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND == "":
    SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND = "torch"


SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS = os.environ.get(
    "SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS", ""
)
if SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS == "":
    SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS = None
else:
    try:
        SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS = json.loads(
            SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS
        )
    except Exception:
        SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS = None

####################################
# OFFLINE_MODE
####################################

ENABLE_VERSION_UPDATE_CHECK = (
    os.environ.get("ENABLE_VERSION_UPDATE_CHECK", "true").lower() == "true"
)
OFFLINE_MODE = os.environ.get("OFFLINE_MODE", "false").lower() == "true"

if OFFLINE_MODE:
    os.environ["HF_HUB_OFFLINE"] = "1"
    ENABLE_VERSION_UPDATE_CHECK = False

####################################
# AUDIT LOGGING
####################################
# Where to store log file
AUDIT_LOGS_FILE_PATH = f"{DATA_DIR}/audit.log"
# Maximum size of a file before rotating into a new log file
AUDIT_LOG_FILE_ROTATION_SIZE = os.getenv("AUDIT_LOG_FILE_ROTATION_SIZE", "10MB")

# Comma separated list of logger names to use for audit logging
# Default is "uvicorn.access" which is the access log for Uvicorn
# You can add more logger names to this list if you want to capture more logs
AUDIT_UVICORN_LOGGER_NAMES = os.getenv(
    "AUDIT_UVICORN_LOGGER_NAMES", "uvicorn.access"
).split(",")

# METADATA | REQUEST | REQUEST_RESPONSE
AUDIT_LOG_LEVEL = os.getenv("AUDIT_LOG_LEVEL", "NONE").upper()
try:
    MAX_BODY_LOG_SIZE = int(os.environ.get("MAX_BODY_LOG_SIZE") or 2048)
except ValueError:
    MAX_BODY_LOG_SIZE = 2048

# Comma separated list for urls to exclude from audit
AUDIT_EXCLUDED_PATHS = os.getenv("AUDIT_EXCLUDED_PATHS", "/chats,/chat,/folders").split(
    ","
)
AUDIT_EXCLUDED_PATHS = [path.strip() for path in AUDIT_EXCLUDED_PATHS]
AUDIT_EXCLUDED_PATHS = [path.lstrip("/") for path in AUDIT_EXCLUDED_PATHS]


####################################
# OPENTELEMETRY
####################################

ENABLE_OTEL = os.environ.get("ENABLE_OTEL", "False").lower() == "true"
ENABLE_OTEL_TRACES = os.environ.get("ENABLE_OTEL_TRACES", "False").lower() == "true"
ENABLE_OTEL_METRICS = os.environ.get("ENABLE_OTEL_METRICS", "False").lower() == "true"
ENABLE_OTEL_LOGS = os.environ.get("ENABLE_OTEL_LOGS", "False").lower() == "true"

OTEL_EXPORTER_OTLP_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
)
OTEL_METRICS_EXPORTER_OTLP_ENDPOINT = os.environ.get(
    "OTEL_METRICS_EXPORTER_OTLP_ENDPOINT", OTEL_EXPORTER_OTLP_ENDPOINT
)
OTEL_LOGS_EXPORTER_OTLP_ENDPOINT = os.environ.get(
    "OTEL_LOGS_EXPORTER_OTLP_ENDPOINT", OTEL_EXPORTER_OTLP_ENDPOINT
)
OTEL_EXPORTER_OTLP_INSECURE = (
    os.environ.get("OTEL_EXPORTER_OTLP_INSECURE", "False").lower() == "true"
)
OTEL_METRICS_EXPORTER_OTLP_INSECURE = (
    os.environ.get(
        "OTEL_METRICS_EXPORTER_OTLP_INSECURE", str(OTEL_EXPORTER_OTLP_INSECURE)
    ).lower()
    == "true"
)
OTEL_LOGS_EXPORTER_OTLP_INSECURE = (
    os.environ.get(
        "OTEL_LOGS_EXPORTER_OTLP_INSECURE", str(OTEL_EXPORTER_OTLP_INSECURE)
    ).lower()
    == "true"
)
OTEL_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "open-webui")
OTEL_RESOURCE_ATTRIBUTES = os.environ.get(
    "OTEL_RESOURCE_ATTRIBUTES", ""
)  # e.g. key1=val1,key2=val2
OTEL_TRACES_SAMPLER = os.environ.get(
    "OTEL_TRACES_SAMPLER", "parentbased_always_on"
).lower()
OTEL_BASIC_AUTH_USERNAME = os.environ.get("OTEL_BASIC_AUTH_USERNAME", "")
OTEL_BASIC_AUTH_PASSWORD = os.environ.get("OTEL_BASIC_AUTH_PASSWORD", "")

OTEL_METRICS_BASIC_AUTH_USERNAME = os.environ.get(
    "OTEL_METRICS_BASIC_AUTH_USERNAME", OTEL_BASIC_AUTH_USERNAME
)
OTEL_METRICS_BASIC_AUTH_PASSWORD = os.environ.get(
    "OTEL_METRICS_BASIC_AUTH_PASSWORD", OTEL_BASIC_AUTH_PASSWORD
)
OTEL_LOGS_BASIC_AUTH_USERNAME = os.environ.get(
    "OTEL_LOGS_BASIC_AUTH_USERNAME", OTEL_BASIC_AUTH_USERNAME
)
OTEL_LOGS_BASIC_AUTH_PASSWORD = os.environ.get(
    "OTEL_LOGS_BASIC_AUTH_PASSWORD", OTEL_BASIC_AUTH_PASSWORD
)

OTEL_OTLP_SPAN_EXPORTER = os.environ.get(
    "OTEL_OTLP_SPAN_EXPORTER", "grpc"
).lower()  # grpc or http

OTEL_METRICS_OTLP_SPAN_EXPORTER = os.environ.get(
    "OTEL_METRICS_OTLP_SPAN_EXPORTER", OTEL_OTLP_SPAN_EXPORTER
).lower()  # grpc or http

OTEL_LOGS_OTLP_SPAN_EXPORTER = os.environ.get(
    "OTEL_LOGS_OTLP_SPAN_EXPORTER", OTEL_OTLP_SPAN_EXPORTER
).lower()  # grpc or http

####################################
# TOOLS/FUNCTIONS PIP OPTIONS
####################################

PIP_OPTIONS = os.getenv("PIP_OPTIONS", "").split()
PIP_PACKAGE_INDEX_OPTIONS = os.getenv("PIP_PACKAGE_INDEX_OPTIONS", "").split()


####################################
# PROGRESSIVE WEB APP OPTIONS
####################################

EXTERNAL_PWA_MANIFEST_URL = os.environ.get("EXTERNAL_PWA_MANIFEST_URL")

####################################
# API DEBUG LOGGING
####################################

ENABLE_API_DEBUG_LOGGING = (
    os.environ.get("ENABLE_API_DEBUG_LOGGING", "False").lower() == "true"
)
