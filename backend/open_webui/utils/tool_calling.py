import ast
import hashlib
import json
import re
from typing import Any


MODEL_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_MCP_TOOL_NAME_MAX = 64
_MCP_TOOL_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_-]")


def merge_streamed_field(existing: str | None, chunk: str | None) -> str:
    """Merge a streamed string field fragment without overlap loss.

    THE canonical merge for every streamed string the model produces — tool-call
    ``function.name``/``function.arguments``, ``reasoning_details``
    text/data/summary, and reasoning block content. OpenAI-compatible streams
    normally send true deltas, which must append byte-for-byte; the persisted
    string is replayed to the provider on later turns, so any deviation from
    what the model generated silently corrupts content AND breaks prompt-cache
    reuse for the rest of the conversation.

    The only compatibility case handled is a provider resending a cumulative
    full prefix (``chunk`` starts with the ENTIRE ``existing`` string). Partial
    suffix/prefix overlap dedupe is deliberately NOT done: it cannot be
    distinguished from legitimately repeated text and eats real characters
    (``bana``+``na``, ``https://``, ``<<``, doubled letters split across
    tokens, base64 runs). Do not add heuristics here.
    """
    if not chunk:
        return existing or ""
    existing = existing or ""
    if not existing:
        return chunk
    if chunk == existing:
        return existing
    if chunk.startswith(existing):
        return chunk
    return existing + chunk


def dedupe_repeated_tool_name(name: str | None) -> str:
    """Collapse a tool name that is a whole-unit repetition of itself
    (``web_searchweb_search`` → ``web_search``) — some providers resend the
    full function name on every stream chunk. Tool names are constrained
    identifiers, so unit-repetition is a reliable resend signal there; do NOT
    apply this to free-form text."""
    if not name:
        return ""
    for unit_len in range(1, (len(name) // 2) + 1):
        if len(name) % unit_len == 0:
            unit = name[:unit_len]
            if unit and unit * (len(name) // unit_len) == name:
                return unit
    return name


def mcp_tool_alias(server_id: str, tool_name: str) -> str:
    """Build a model-facing MCP tool name within provider constraints."""
    safe_name = _MCP_TOOL_NAME_INVALID.sub("_", tool_name or "")
    digest = hashlib.sha1((server_id or "").encode("utf-8")).hexdigest()[:8]
    prefix = f"mcp_{digest}_"
    return (prefix + safe_name)[:_MCP_TOOL_NAME_MAX] or prefix[:_MCP_TOOL_NAME_MAX]


def parse_tool_call_arguments(tool_args: Any) -> dict:
    """Parse model-emitted tool arguments as JSON first.

    OpenAI-compatible providers define ``function.arguments`` as a JSON string.
    ``ast.literal_eval`` accepts a Python-ish subset, but putting it first can
    reinterpret code-heavy strings before the JSON parser gets a chance. Keep it
    only as a compatibility fallback for older non-JSON tool-call payloads.
    """
    if isinstance(tool_args, dict):
        return dict(tool_args)
    if tool_args is None:
        return {}
    if not isinstance(tool_args, str):
        return {}

    raw = tool_args.strip()
    if not raw:
        return {}

    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except Exception:
        try:
            parsed = ast.literal_eval(raw)
        except Exception:
            return {}

    return parsed if isinstance(parsed, dict) else {}


def mcp_model_facing_tool_name(
    *,
    container_server_id: str,
    server_id: str,
    tool_name: str,
    existing_names: set[str],
) -> str:
    """Return the provider-facing MCP tool name.

    Generic remote MCP servers keep the hashed alias to avoid provider name
    limits/collisions. The configured container MCP server exposes a tiny,
    stable Claude-Code-like surface (bash/read/write/edit), so when those names
    already satisfy provider constraints we preserve them.
    """
    configured_container = str(container_server_id or "").strip()
    if (
        configured_container
        and str(server_id or "") == configured_container
        and isinstance(tool_name, str)
        and MODEL_TOOL_NAME_RE.match(tool_name)
        and tool_name not in existing_names
    ):
        return tool_name

    alias = mcp_tool_alias(server_id, tool_name)
    if alias in existing_names:
        alias = mcp_tool_alias(f"{server_id}:{tool_name}", tool_name)
    return alias
