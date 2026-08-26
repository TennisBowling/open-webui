import time
import logging
import re
import sys

from aiocache import cached
from typing import Any, Optional
import random
import json
import inspect

from fastapi import Request, status
from starlette.responses import Response, StreamingResponse, JSONResponse


from open_webui.models.users import UserModel

from open_webui.socket.main import (
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
from open_webui.models.chats import Chats
from open_webui.models.files import Files
from open_webui.utils.messages import (
    blocks_to_api_messages,
    is_aborted_attempt,
    resume_boundary_blocks,
)


from open_webui.utils.plugin import (
    load_function_module_by_id,
    get_function_module_from_cache,
)
from open_webui.utils.models import (
    check_model_access,
    get_all_models,
    model_supports_video_input,
)
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


# ---------------------------------------------------------------------------
# B10: server-side conversation assembly (v2.1 chat/completions body shape)
#
# Ports `expandMessagesForToolResumption` and `buildTextFileBlocks` from
# `src/lib/components/chat/Chat.svelte` so the frontend can stop shipping the
# full `messages: [...]` array on every send. The frontend posts a small body
# carrying `leaf_message_id`; the backend walks the chat tree to assemble the
# canonical conversation context.
# ---------------------------------------------------------------------------


_TEXT_FILE_EXTS = {
    "txt",
    "md",
    "markdown",
    "rst",
    "csv",
    "tsv",
    "json",
    "jsonl",
    "ndjson",
    "yaml",
    "yml",
    "toml",
    "ini",
    "cfg",
    "conf",
    "env",
    "log",
    "xml",
    "svg",
    "py",
    "pyi",
    "ipynb",
    "js",
    "mjs",
    "cjs",
    "ts",
    "tsx",
    "jsx",
    "vue",
    "svelte",
    "java",
    "kt",
    "kts",
    "scala",
    "groovy",
    "c",
    "cc",
    "cpp",
    "cxx",
    "h",
    "hpp",
    "hxx",
    "rs",
    "go",
    "rb",
    "php",
    "pl",
    "pm",
    "lua",
    "r",
    "jl",
    "dart",
    "swift",
    "m",
    "mm",
    "cs",
    "fs",
    "fsx",
    "ex",
    "exs",
    "erl",
    "hs",
    "ml",
    "mli",
    "clj",
    "cljs",
    "sh",
    "bash",
    "zsh",
    "fish",
    "ps1",
    "bat",
    "cmd",
    "sql",
    "graphql",
    "gql",
    "proto",
    "css",
    "scss",
    "sass",
    "less",
    "tex",
    "bib",
    "srt",
    "vtt",
    "patch",
    "diff",
    "gitignore",
    "dockerignore",
    "editorconfig",
}

_EXTRACTABLE_EXTS = {
    "docx",
    "doc",
    "odt",
    "rtf",
    "pptx",
    "ppt",
    "xlsx",
    "xls",
    "html",
    "htm",
    "epub",
}


class ActiveSubagentRerunError(RuntimeError):
    """A parent completion tried to consume a tool result being replaced."""

    def __init__(self, entry_keys: list[str]):
        self.entry_keys = list(dict.fromkeys(entry_keys))
        super().__init__(
            "Wait for the active subagent redo to finish before continuing "
            "the main chat."
        )


class ChatMessageAncestryError(RuntimeError):
    """A persisted conversation leaf does not have a complete, acyclic ancestry."""

    def __init__(self, leaf_id: str, message_id: str, *, cycle: bool = False):
        self.leaf_id = str(leaf_id)
        self.message_id = str(message_id)
        self.code = (
            "chat_message_ancestry_cycle" if cycle else "chat_message_ancestor_missing"
        )
        super().__init__(
            f"Conversation ancestry for {self.leaf_id} contains a cycle at {self.message_id}."
            if cycle
            else f"Conversation ancestry for {self.leaf_id} is missing message {self.message_id}."
        )


def active_detached_subagent_rerun_entries(messages: list[dict]) -> list[str]:
    """Return active detached-rerun keys in one assembled parent chain.

    The persisted run is an authoritative backstop when Redis/task events are
    delayed or unavailable. Inline subagents are deliberately excluded: their
    parent generation already owns the request in which they are running.
    """
    active: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        runs = message.get("subagent_runs")
        if not isinstance(runs, dict):
            continue
        for entry_key, run in runs.items():
            if (
                isinstance(run, dict)
                and run.get("status") == "running"
                and run.get("ended_at") is None
                and (
                    run.get("rerun") is True
                    or run.get("detached_rerun") is True
                    or bool(run.get("rerun_id"))
                    or bool(run.get("rerun_task_id"))
                )
            ):
                active.append(str(run.get("entry_key") or entry_key))
    return list(dict.fromkeys(active))


def _reject_active_detached_subagent_reruns(messages: list[dict]) -> None:
    active = active_detached_subagent_rerun_entries(messages)
    if active:
        raise ActiveSubagentRerunError(active)


def _file_ext(file: dict) -> str:
    name = (file.get("name") or (file.get("file") or {}).get("filename") or "").lower()
    if name.endswith(".pdf"):
        return "pdf"
    dot = name.rfind(".")
    return name[dot + 1 :] if dot >= 0 else ""


def _file_content_url(file: dict) -> str:
    url = file.get("url")
    if isinstance(url, str) and url:
        return url

    file_id = file.get("id") or (file.get("file") or {}).get("id")
    if file_id:
        return f"/api/v1/files/{file_id}/content"

    return ""


def _is_text_file(file: dict) -> bool:
    if not file or file.get("type") != "file":
        return False
    ext = _file_ext(file)
    if ext == "pdf":
        return False
    if ext and ext in _TEXT_FILE_EXTS:
        return True
    ct = (
        file.get("content_type")
        or ((file.get("file") or {}).get("meta") or {}).get("content_type")
        or ""
    ).lower()
    if ct.startswith("text/") and "html" not in ct:
        return True
    return False


def _escape_xml_attr(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


async def _read_file_text(file: dict) -> str:
    """Best-effort plaintext read for a file dict that's already passed
    ``_is_text_file``. Prefers the cached extracted content on ``file.data``;
    falls back to reading the storage path directly.
    """
    if isinstance(file.get("_inlinedText"), str):
        return file["_inlinedText"]

    file_id = file.get("id") or (file.get("file") or {}).get("id")
    if not file_id:
        return ""

    try:
        from open_webui.storage.provider import Storage

        record = await Files.get_file_by_id(file_id)
        if record is None:
            return ""

        data = record.data or {}
        cached_content = data.get("content")
        if isinstance(cached_content, str) and cached_content:
            return cached_content

        if record.path:
            try:
                local_path = Storage.get_file(record.path)
                with open(local_path, "r", encoding="utf-8", errors="replace") as fh:
                    return fh.read()
            except Exception as e:
                log.debug(f"build_text_file_blocks: failed to read {file_id}: {e}")
                return ""
    except Exception as e:
        log.debug(f"build_text_file_blocks: lookup failed for {file_id}: {e}")
        return ""

    return ""


async def build_text_file_blocks(files: Optional[list]) -> str:
    """Ported from Chat.svelte:buildTextFileBlocks. Returns the
    ``<document filename="...">...</document>`` prefix to prepend to a user
    message's text content, or '' if there are no inlineable text files.
    """
    if not files:
        return ""
    text_files = [f for f in files if _is_text_file(f)]
    if not text_files:
        return ""

    blocks = []
    for f in text_files:
        name = f.get("name") or (f.get("file") or {}).get("filename") or "file"
        text = await _read_file_text(f)
        blocks.append(
            f'<document filename="{_escape_xml_attr(name)}">\n{text}\n</document>'
        )
    return "\n\n".join(blocks) + "\n\n"


_TOOL_CALLS_DETAILS_RE = re.compile(
    r'<details\s+type="tool_calls"([^>]*)>[\s\S]*?</details>', re.IGNORECASE
)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_LEGACY_TOOL_DETAILS_RE = re.compile(
    r'<details\s+type="tool_calls"[^>]+done="true"', re.IGNORECASE
)


def _get_string_message_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "".join(parts)
    return ""


def _normalize_preserved(text: str) -> str:
    return (text or "").strip()


def _expand_preserved_tool_context_message(message: dict) -> list:
    if not message.get("preservedToolContext"):
        return [message]

    content = _get_string_message_content(message.get("content"))
    matches = list(_TOOL_CALLS_DETAILS_RE.finditer(content))
    if not matches:
        return [message]

    parsed = []
    for m in matches:
        attrs = {}
        for am in _ATTR_RE.finditer(m.group(1) or ""):
            attrs[am.group(1)] = am.group(2)
        if attrs.get("done") == "true" and attrs.get("id") and attrs.get("name"):
            parsed.append(
                {
                    "matchStart": m.start(),
                    "matchEnd": m.end(),
                    "id": attrs["id"],
                    "name": attrs["name"],
                    "arguments": attrs.get("arguments", ""),
                    "result": attrs.get("result", ""),
                }
            )

    if not parsed:
        return [message]

    groups = [{"toolCalls": [parsed[0]], "textBeforeStart": 0}]
    for i in range(1, len(parsed)):
        prev_end = parsed[i - 1]["matchEnd"]
        curr_start = parsed[i]["matchStart"]
        between = _normalize_preserved(content[prev_end:curr_start])
        if between:
            groups.append({"toolCalls": [parsed[i]], "textBeforeStart": prev_end})
        else:
            groups[-1]["toolCalls"].append(parsed[i])

    last_tc = parsed[-1]
    trailing_text = _normalize_preserved(content[last_tc["matchEnd"] :])
    has_trailing = bool(trailing_text)

    reasoning_per_round = message.get("reasoning_details_per_round")
    if not isinstance(reasoning_per_round, list):
        reasoning_per_round = None

    expanded = []
    for group_idx, group in enumerate(groups):
        is_last_group = group_idx == len(groups) - 1
        first_tc = group["toolCalls"][0]
        text_before = _normalize_preserved(
            content[group["textBeforeStart"] : first_tc["matchStart"]]
        )

        if reasoning_per_round is not None and group_idx < len(reasoning_per_round):
            group_reasoning = reasoning_per_round[group_idx]
        elif is_last_group and not has_trailing:
            group_reasoning = message.get("reasoning_details")
        else:
            group_reasoning = None

        emitted = {
            **message,
            "role": "assistant",
            "content": text_before,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"] or "",
                    },
                }
                for tc in group["toolCalls"]
            ],
        }
        emitted.pop("preservedToolContext", None)
        if group_reasoning is None:
            emitted.pop("reasoning_details", None)
        else:
            emitted["reasoning_details"] = group_reasoning
        expanded.append(emitted)

        for tc in group["toolCalls"]:
            expanded.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tc["result"] or "",
                }
            )

    if has_trailing:
        if reasoning_per_round is not None and len(reasoning_per_round) > len(groups):
            trailing_reasoning = reasoning_per_round[-1]
        elif reasoning_per_round is None:
            trailing_reasoning = message.get("reasoning_details")
        else:
            trailing_reasoning = None

        trailing_msg = {
            **message,
            "content": trailing_text,
        }
        trailing_msg.pop("preservedToolContext", None)
        trailing_msg.pop("tool_calls", None)
        if trailing_reasoning is None:
            trailing_msg.pop("reasoning_details", None)
        else:
            trailing_msg["reasoning_details"] = trailing_reasoning
        expanded.append(trailing_msg)

    return expanded


def expand_messages_for_tool_resumption(messages: list) -> list:
    """Port of Chat.svelte:expandMessagesForToolResumption. Assistant messages
    that already carry structured ``content_blocks`` pass through unchanged
    (``blocks_to_api_messages`` handles them downstream). Messages with the
    ``preservedToolContext`` flag are re-expanded from their HTML tool-call
    markers. Legacy assistant messages with tool-call HTML but no flag get the
    same recovery applied opportunistically.
    """
    out: list = []
    for message in messages or []:
        if not isinstance(message, dict):
            out.append(message)
            continue

        if (
            message.get("role") == "assistant"
            and isinstance(message.get("content_blocks"), list)
            and len(message["content_blocks"]) > 0
        ):
            out.append(message)
            continue

        if message.get("preservedToolContext"):
            out.extend(_expand_preserved_tool_context_message(message))
            continue

        if message.get("role") == "assistant" and not message.get("tool_calls"):
            content = _get_string_message_content(message.get("content") or "")
            if _LEGACY_TOOL_DETAILS_RE.search(content):
                out.extend(
                    _expand_preserved_tool_context_message(
                        {**message, "preservedToolContext": True}
                    )
                )
                continue

        out.append(message)
    return out


def _walk_messages_from_leaf(
    messages_map: dict, leaf_id: str, *, allow_missing_leaf: bool = False
) -> list:
    chain = []
    seen = set()
    current_id = leaf_id
    while current_id:
        if current_id in seen:
            raise ChatMessageAncestryError(leaf_id, current_id, cycle=True)
        seen.add(current_id)
        msg = messages_map.get(current_id)
        if not isinstance(msg, dict):
            if not chain and allow_missing_leaf:
                return []
            raise ChatMessageAncestryError(leaf_id, current_id)
        chain.append(msg)
        current_id = msg.get("parentId")
    chain.reverse()
    return chain


_DEFAULT_VISION_PREPROCESSOR_PROMPT = (
    "Perform OCR on this image and describe its contents in the context of the "
    "user query: {query}"
)


def _message_image_files(message: dict) -> list:
    return [f for f in (message.get("files") or []) if f.get("type") == "image"]


async def _message_pdf_files(message: dict) -> list:
    out = []
    for f in message.get("files") or []:
        if f.get("type") != "file":
            continue
        name = (f.get("name") or "").lower()
        inner = ((f.get("file") or {}).get("filename") or "").lower()
        if name.endswith(".pdf") or inner.endswith(".pdf"):
            out.append(f)
    return out


async def preprocess_nonvision_files(
    request,
    user,
    chat_id: str,
    user_message: dict,
    model: Optional[dict],
) -> None:
    """Server-side port of the client's vision/PDF preprocessing.

    When a user message carries images or PDFs but the target ``model`` lacks
    native vision AND has a configured ``vision_preprocessor_model_id``, run the
    preprocessor model to OCR/describe the attachments, then REWRITE the user
    message content to ``[Vision Analysis:\\n…]`` / ``[PDF Analysis (N pages):
    \\n…]`` and persist it. This makes queued multimodal messages work when the
    backend drains with zero tabs open (the browser that used to do this isn't
    around).

    Idempotent: guarded by the persisted ``vision_processed`` / ``pdf_processed``
    flags (the same flags the client sets), so a re-drain / crash never
    double-prepends the analysis. Mutates ``user_message`` IN PLACE so the
    caller's assembled chain sees the rewritten content.

    Best-effort for images (degrade to text-only on failure). For PDFs a failure
    persists an error on the message and raises, so the drain PAUSES rather than
    silently sending a non-vision model a PDF it can't read.
    """
    if not isinstance(user_message, dict) or user_message.get("role") != "user":
        return
    if not isinstance(model, dict):
        return

    meta = ((model.get("info") or {}).get("meta")) or {}
    caps = meta.get("capabilities") or {}
    has_native_vision = caps.get("vision", True)
    preprocessor_id = meta.get("vision_preprocessor_model_id")
    if has_native_vision or not preprocessor_id:
        return

    vision_prompt_tmpl = (
        meta.get("vision_preprocessor_prompt") or _DEFAULT_VISION_PREPROCESSOR_PROMPT
    )
    base_content = user_message.get("content") or ""
    message_id = user_message.get("id")

    async def _run_ocr(messages: list) -> str:
        ocr_form = {
            "model": preprocessor_id,
            "messages": messages,
            "stream": False,
            "max_tokens": 4096,
        }
        res = await generate_chat_completion(
            request, ocr_form, user, bypass_filter=True
        )
        # generate_chat_completion returns a dict for non-streaming responses.
        if isinstance(res, dict):
            choices = res.get("choices") or []
            if choices:
                return (choices[0].get("message") or {}).get("content") or ""
        return ""

    async def _persist(content: str, **flags) -> None:
        update = {"content": content, **flags}
        if chat_id and not str(chat_id).startswith("local:") and message_id:
            try:
                await Chats.upsert_message_to_chat_by_id_and_message_id(
                    chat_id, message_id, update, return_model=False
                )
            except Exception:
                log.exception(
                    "preprocess_nonvision_files: persist failed for %s/%s",
                    chat_id,
                    message_id,
                )

    # --- Images ---------------------------------------------------------------
    images = _message_image_files(user_message)
    if images and not user_message.get("vision_processed"):
        try:
            ocr_messages = [
                {
                    "role": "system",
                    "content": vision_prompt_tmpl.replace("{query}", base_content),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": base_content},
                        *[
                            {
                                "type": "image_url",
                                "image_url": {"url": _file_content_url(f)},
                            }
                            for f in images
                            if _file_content_url(f)
                        ],
                    ],
                },
            ]
            vision_response = await _run_ocr(ocr_messages)
            new_content = f"[Vision Analysis:\n{vision_response}\n]\n\n{base_content}"
            user_message["content"] = new_content
            user_message["vision_processed"] = True
            base_content = new_content
            await _persist(new_content, vision_processed=True)
        except Exception:
            log.exception(
                "preprocess_nonvision_files: image OCR failed for %s; sending text-only",
                chat_id,
            )
            # Degrade gracefully — mark processed=False, leave content as-is.
            user_message["vision_processed"] = False
            await _persist(base_content, vision_processed=False)

    # --- PDFs -----------------------------------------------------------------
    pdfs = await _message_pdf_files(user_message)
    if pdfs and not user_message.get("pdf_processed"):
        try:
            ocr_messages = [
                {
                    "role": "system",
                    "content": vision_prompt_tmpl.replace("{query}", base_content),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"I have uploaded {len(pdfs)} PDF document(s). "
                                f"Please analyze them:\n\n{base_content}"
                            ),
                        },
                        *[
                            {
                                "type": "file",
                                "file": {
                                    "filename": f.get("name")
                                    or (f.get("file") or {}).get("filename")
                                    or "document.pdf",
                                    "file_data": _file_content_url(f),
                                },
                            }
                            for f in pdfs
                        ],
                    ],
                },
            ]
            vision_response = await _run_ocr(ocr_messages)
            pages = len(pdfs)
            new_content = f"[PDF Analysis ({pages} pages):\n{vision_response}\n]\n\n{base_content}"
            user_message["content"] = new_content
            user_message["pdf_processed"] = True
            await _persist(new_content, pdf_processed=True)
        except Exception as e:
            log.exception("preprocess_nonvision_files: PDF OCR failed for %s", chat_id)
            # PDF failure is fatal for the turn (the model can't read the PDF):
            # persist an error and raise so the drain PAUSES.
            err = {
                "content": (
                    f"PDF preprocessing failed: {e}\n\nThe selected model does not "
                    "support vision natively, and PDF preprocessing could not be "
                    "completed."
                )
            }
            if chat_id and not str(chat_id).startswith("local:") and message_id:
                try:
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        chat_id, message_id, {"error": err}, return_model=False
                    )
                except Exception:
                    pass
            raise


def _normalize_files_for_portability(files):
    """Rewrite a user message's ``files`` so every attachment renders on ANOTHER
    device (cross-device prompt sync) as well as on a plain reload.

    Only ``id``-based (``/api/v1/files/{id}/content``) and ``data:`` urls resolve
    on a device other than the one that created the message; a transient
    ``blob:`` url (clipboard paste / local preview) is origin-local and 404s
    elsewhere. ``UserMessage.svelte`` renders ``<Image src={file.url}>`` directly,
    so the url on the persisted row must already be portable. We run this BEFORE
    the DB upsert so the persisted row, the emitted ``chat:user-message`` payload,
    and a later ``loadChat`` all agree byte-for-byte.

    Best-effort and non-destructive: when a portable url cannot be derived (e.g. a
    pasted image with neither an ``id`` nor a ``data:`` url) the entry is left
    untouched rather than dropped, so behaviour never diverges from a plain
    reload.
    """
    if not isinstance(files, list):
        return files
    out = []
    for f in files:
        if not isinstance(f, dict):
            out.append(f)
            continue
        g = dict(f)
        url = g.get("url")
        fid = g.get("id")
        portable = isinstance(url, str) and (
            url.startswith("/api/v1/files/") or url.startswith("data:")
        )
        if not portable and fid:
            g["url"] = f"/api/v1/files/{fid}/content"
        out.append(g)
    return out


async def assemble_conversation_from_leaf(
    chat_id: str,
    leaf_message_id: Optional[str],
    new_user_message: Optional[dict] = None,
    model: Optional[dict] = None,
    system_prompt: Optional[str] = None,
    container_workspace_active: bool = False,
    request=None,
    user=None,
    persisted_out: Optional[dict] = None,
    resume_message_id: Optional[str] = None,
) -> list[dict]:
    """Backend equivalent of the frontend's ``createMessagesList`` +
    ``expandMessagesForToolResumption`` + ``buildTextFileBlocks`` +
    ``blocks_to_api_messages`` pipeline. Returns the OpenAI-shape message list
    ready to feed into ``generate_chat_completion``.

    ``new_user_message`` (when provided) is persisted via
    ``await Chats.upsert_message_to_chat_by_id_and_message_id`` AND appended to the
    walk so the first send of a new turn works without a separate save round-trip
    from the frontend.

    When ``request`` + ``user`` are provided and the model lacks native vision
    but has a configured preprocessor, images/PDFs on the last user message are
    OCR'd and folded into its text (see ``preprocess_nonvision_files``). This is
    what makes queued multimodal messages work under the zero-tab server drain;
    it also runs for normal tab-driven sends so behavior is identical.

    ``resume_message_id`` is the assistant row this generation will WRITE INTO
    (retry / regenerate / continue reuse an existing id). When that row is still
    unfinished it carries the tail of a failed attempt, which is trimmed to the
    resume boundary before the payload is built — see ``resume_boundary_blocks``.
    """
    messages_map = await Chats.get_messages_map_by_chat_id(chat_id) or {}

    chain = (
        _walk_messages_from_leaf(
            messages_map,
            leaf_message_id,
            allow_missing_leaf=bool(
                new_user_message and new_user_message.get("id") == leaf_message_id
            ),
        )
        if leaf_message_id
        else []
    )
    # Do this before any new user row is persisted. A detached rerun replaces
    # one of the chain's tool results transactionally; starting the parent from
    # the temporary running state would consume neither the old nor the new
    # result coherently.
    _reject_active_detached_subagent_reruns(chain)

    if new_user_message and new_user_message.get("id"):
        new_id = new_user_message["id"]
        if new_id not in messages_map:
            parent_id = new_user_message.get("parentId") or leaf_message_id
            # NEVER persist a self-parented message. The frontend convention
            # passes leaf_message_id == the NEW user message's own id, so when
            # its parentId is null (first turn) the fallback above resolved to
            # the message itself — a cycle in the tree that wedges every
            # unguarded parent-chain walker (server-wide 100%-CPU freeze in
            # get_message_list, and the same loop client-side). Reachable in
            # production whenever the completion POST arrives before/without
            # the user-row save (flaky-network race, API callers).
            if parent_id == new_id:
                parent_id = new_user_message.get("parentId") or None
                if parent_id == new_id:
                    parent_id = None
            if leaf_message_id == new_id and parent_id and not chain:
                chain = _walk_messages_from_leaf(messages_map, parent_id)
                _reject_active_detached_subagent_reruns(chain)
            persisted = {
                "id": new_id,
                "parentId": parent_id,
                "childrenIds": [],
                "role": new_user_message.get("role") or "user",
                "content": new_user_message.get("content") or "",
                "files": _normalize_files_for_portability(
                    new_user_message.get("files") or []
                ),
                "timestamp": int(time.time()),
                "models": new_user_message.get("models") or [],
            }

            # The model write owns graph integrity: migrated chats derive
            # children from parent_id, while the legacy path updates the
            # parent's childrenIds in the same locked transaction.
            await Chats.upsert_message_to_chat_by_id_and_message_id(
                chat_id, new_id, persisted, return_model=False
            )

            chain.append(persisted)
            _user_row_for_broadcast = persisted
        else:
            # Normal interactive send: the frontend already persisted this user
            # message (an append_message op + an awaited save) BEFORE calling
            # /api/chat/completions, so new_id is already in messages_map and the
            # message is already in the walked chain. Surface that persisted row
            # anyway so cross-device sync still emits — without this, the prompt
            # bubble would only appear on other devices after a full reload (the
            # `new_id not in messages_map` branch above is hit only by the
            # headless queue drain, whose chat:user-message emit is now ALSO sent —
            # paired with an atomic chip-clear — so the drained bubble appears
            # immediately rather than waiting for the post-response loadChat).
            existing_user_row = messages_map.get(new_id)
            _user_row_for_broadcast = (
                existing_user_row if isinstance(existing_user_row, dict) else None
            )

        # Cross-device prompt sync: hand the user-message row to the caller so
        # chat_completion can broadcast a `chat:user-message` event, regardless of
        # which side wrote it (assemble here for a server drain, or the frontend
        # pre-save for a normal interactive send). Files are normalized so the
        # emitted payload is portable and agrees with a later loadChat. Regenerate
        # reuses the existing assistant turn and sends no new_user_message, so this
        # whole block is never entered for it.
        if persisted_out is not None and isinstance(_user_row_for_broadcast, dict):
            persisted_out["user_message"] = {
                "id": _user_row_for_broadcast.get("id") or new_id,
                "parentId": _user_row_for_broadcast.get("parentId"),
                "childrenIds": list(_user_row_for_broadcast.get("childrenIds") or []),
                "role": _user_row_for_broadcast.get("role") or "user",
                "content": _user_row_for_broadcast.get("content") or "",
                "files": _normalize_files_for_portability(
                    _user_row_for_broadcast.get("files") or []
                ),
                "timestamp": _user_row_for_broadcast.get("timestamp"),
                "models": list(_user_row_for_broadcast.get("models") or []),
            }
            persisted_out["leaf_message_id"] = leaf_message_id

    # Vision/PDF preprocessing for non-vision models (server-side port of the
    # client path). Operates on the LAST user message in the chain — the one
    # being sent this turn — before we build the API messages, so the OCR
    # rewrite is what the model actually receives. Idempotent via persisted
    # vision_processed/pdf_processed flags.
    if request is not None and user is not None:
        last_user_message = next(
            (
                m
                for m in reversed(chain)
                if isinstance(m, dict) and m.get("role") == "user"
            ),
            None,
        )
        if last_user_message is not None:
            await preprocess_nonvision_files(
                request, user, chat_id, last_user_message, model
            )

    # Retry/continue writes back into an EXISTING assistant row, which is in this
    # chain. When that row is the wreckage of an attempt that died mid-flight, its
    # trailing prose is about to be regenerated — sending it upstream asks the
    # model to continue a half-written sentence, which it answers by restarting.
    # Trim to the resume boundary here so the payload matches what
    # `process_chat_response` will actually stream into (it applies the identical
    # trim when it seeds `content_blocks`). Scoped to the row this generation
    # owns: an unfinished assistant anywhere ELSE in the chain is real history the
    # user saw, and must not be silently shortened.
    if resume_message_id:
        chain = [
            (
                {**m, "content_blocks": resume_boundary_blocks(m["content_blocks"])}
                if isinstance(m, dict)
                and m.get("id") == resume_message_id
                and m.get("role") == "assistant"
                and isinstance(m.get("content_blocks"), list)
                and is_aborted_attempt(m)
                else m
            )
            for m in chain
        ]

    expanded = expand_messages_for_tool_resumption(chain)

    model_supports_vision = True
    model_supports_video = False
    if isinstance(model, dict):
        caps = (((model.get("info") or {}).get("meta") or {}).get("capabilities")) or {}
        model_supports_vision = caps.get("vision", True)
        model_supports_video = model_supports_video_input(model)

    prepared: list[dict] = []
    if system_prompt:
        prepared.append({"role": "system", "content": system_prompt})

    for message in expanded:
        if not isinstance(message, dict):
            continue

        if (
            message.get("role") == "assistant"
            and isinstance(message.get("content_blocks"), list)
            and len(message["content_blocks"]) > 0
        ):
            forwarded = {
                "role": "assistant",
                "content_blocks": message["content_blocks"],
            }
            if message.get("id"):
                # Internal only. A compaction anchor lives in `content_blocks`,
                # and `apply_compaction_to_messages` needs the id of the message
                # holding it so the envelope can be written back to that row
                # (utils/compaction.py `capture_compaction_envelope`). Safe on
                # this branch specifically: it is expanded by `_expand_assistant`,
                # which takes the BLOCK LIST and builds fresh dicts, so no key
                # from here can reach the wire.
                forwarded["id"] = message["id"]
            if message.get("tool_result_bodies"):
                forwarded["tool_result_bodies"] = message["tool_result_bodies"]
            if message.get("reasoning_details_per_round"):
                forwarded["reasoning_details_per_round"] = message[
                    "reasoning_details_per_round"
                ]
            if message.get("reasoning_details"):
                forwarded["reasoning_details"] = message["reasoning_details"]
            if message.get("subagent_runs"):
                # Carry the durable subagent answer mirror so blocks_to_api_messages
                # can recover a subagent tool result whose persisted content is empty
                # (results array vs subagent_runs can diverge on an interrupted turn).
                forwarded["subagent_runs"] = message["subagent_runs"]
            prepared.append(forwarded)
            continue

        if message.get("role") == "tool":
            forwarded = {"role": "tool", "content": message.get("content") or ""}
            if message.get("tool_call_id"):
                forwarded["tool_call_id"] = message["tool_call_id"]
            prepared.append(forwarded)
            continue

        if message.get("tool_calls"):
            forwarded = {
                "role": "assistant",
                "content": message.get("content") or None,
                "tool_calls": message["tool_calls"],
            }
            if message.get("reasoning_details"):
                forwarded["reasoning_details"] = message["reasoning_details"]
            prepared.append(forwarded)
            continue

        files = message.get("files") or []
        is_user = message.get("role") == "user"

        has_images = any(f.get("type") == "image" for f in files)
        has_pdf = any(
            f.get("type") == "file"
            and (
                (f.get("name") or "").lower().endswith(".pdf")
                or ((f.get("file") or {}).get("filename") or "")
                .lower()
                .endswith(".pdf")
            )
            for f in files
        )
        has_extractable = any(
            f.get("type") == "file" and _file_ext(f) in _EXTRACTABLE_EXTS for f in files
        )
        has_video = any(f.get("type") == "video" for f in files)

        should_attach_videos = is_user and has_video and model_supports_video
        should_attach_images = is_user and has_images and model_supports_vision
        should_send_files_to_model = is_user and not container_workspace_active
        # PDFs use OpenRouter's native file-parser path, so container mode still
        # sends them to the model while also copying them into /workspace/inputs.
        should_attach_pdf_files = is_user and has_pdf and model_supports_vision
        should_attach_extractable_files = should_send_files_to_model and has_extractable

        text_prefix = (
            await build_text_file_blocks(files) if should_send_files_to_model else ""
        )
        # A few corrupted rows persisted the literal string "null" (JSON-encoded
        # None) into `content` instead of a real SQL NULL — guard it the same as
        # a missing/empty content so it never gets treated as real message text.
        _merged_content = (message.get("merged") or {}).get("content")
        if _merged_content == "null":
            _merged_content = None
        _raw_content = message.get("content")
        if _raw_content == "null":
            _raw_content = None
        base_text = _merged_content or _raw_content or ""

        if is_user and (
            should_attach_images
            or should_attach_pdf_files
            or should_attach_extractable_files
            or should_attach_videos
        ):
            parts: list = [{"type": "text", "text": text_prefix + base_text}]

            if should_attach_videos:
                for f in files:
                    if f.get("type") != "video":
                        continue
                    # Providers that accept video want the bytes inline; only
                    # AI Studio's YouTube-link path takes a plain URL, and that
                    # is deliberately out of scope. `_file_content_url` yields a
                    # data URL for stored files, which is what we need here.
                    video_url = _file_content_url(f)
                    if video_url:
                        parts.append(
                            {"type": "video_url", "video_url": {"url": video_url}}
                        )

            if should_attach_images:
                for f in files:
                    image_url = _file_content_url(f)
                    if f.get("type") == "image" and image_url:
                        parts.append(
                            {"type": "image_url", "image_url": {"url": image_url}}
                        )

            if should_attach_pdf_files:
                for f in files:
                    if f.get("type") == "file" and (
                        (f.get("name") or "").lower().endswith(".pdf")
                        or ((f.get("file") or {}).get("filename") or "")
                        .lower()
                        .endswith(".pdf")
                    ):
                        parts.append(
                            {
                                "type": "file",
                                "file": {
                                    "filename": f.get("name")
                                    or (f.get("file") or {}).get("filename")
                                    or "document.pdf",
                                    "file_data": _file_content_url(f),
                                },
                            }
                        )

            if should_attach_extractable_files:
                for f in files:
                    if f.get("type") == "file" and _file_ext(f) in _EXTRACTABLE_EXTS:
                        parts.append(
                            {
                                "type": "file",
                                "file": {
                                    "filename": f.get("name")
                                    or (f.get("file") or {}).get("filename")
                                    or "document",
                                    "file_data": _file_content_url(f),
                                    "processing_mode": (
                                        "pdf"
                                        if f.get("processing_mode") == "pdf"
                                        else "text"
                                    ),
                                },
                            }
                        )

            prepared.append({"role": message.get("role"), "content": parts})
            continue

        forwarded = {
            "role": message.get("role"),
            "content": (
                text_prefix + base_text if is_user and text_prefix else base_text
            ),
        }
        if message.get("reasoning_details"):
            forwarded["reasoning_details"] = message["reasoning_details"]
        prepared.append(forwarded)

    # Surface the RAW walked chain (persisted rows, `content_blocks` intact) for
    # the turn-start compaction gate in main.py. It needs three things the
    # converted list cannot give it: the id of the message to anchor into, that
    # message's `meta.usage` (the last round's token count), and whether any
    # compaction anchor already covers this span. Set unconditionally so the
    # regenerate path — which passes no `new_user_message` and therefore skips
    # the block above — gets it too.
    if persisted_out is not None:
        persisted_out["chain"] = chain

    # model_id gates the Gemini-only `$ref` sanitization inside blocks_to_api_messages
    # (see sanitize_gemini_tool_result) — a stray OpenAPI $ref in a tool result body
    # (e.g. a web-fetched Swagger/OpenAPI spec) makes Gemini 400 INVALID_ARGUMENT on
    # every subsequent request that resends it, permanently bricking the chat.
    model_id = model.get("id") if isinstance(model, dict) else None
    return blocks_to_api_messages(prepared, model_id=model_id)


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
        fd_meta = form_data.get("metadata") if isinstance(form_data, dict) else None
        if isinstance(fd_meta, dict) and fd_meta.get("subagent_inner"):
            # A subagent's inner rounds carry their OWN authoritative metadata in
            # form_data. `request.state` is a per-Request singleton SHARED across a
            # parallel subagent fan-out (all gather branches use the same Request),
            # so a sibling subagent may have just swapped `request.state.metadata`
            # to ITS chat/message/emitter. Merging that here would route this
            # subagent's continuation round to the wrong chat — emitting its events
            # and writing its transcript into a sibling. Trust form_data only.
            pass
        elif "metadata" not in form_data:
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

    completion_metadata = (
        form_data.get("metadata") if isinstance(form_data, dict) else None
    )
    provider_stream_override = (
        completion_metadata.get("provider_stream")
        if isinstance(completion_metadata, dict)
        and "provider_stream" in completion_metadata
        else None
    )
    upstream_stream = (
        bool(provider_stream_override)
        if provider_stream_override is not None
        else bool(form_data.get("stream"))
    )

    # Check if model is in MODELS (backend-managed) - if so, DON'T use direct flow
    is_in_backend_models = model_id in request.app.state.MODELS
    is_direct_flag_set = getattr(request.state, "direct", False)

    log.info(
        f"🔄 ROUTING: model_id={model_id}, is_in_backend_models={is_in_backend_models}, is_direct_flag_set={is_direct_flag_set}"
    )

    if is_direct_flag_set and not is_in_backend_models:
        log.warning(
            f"🔄 ROUTING: Direct-connection flow requested for {model_id} but is disabled in this deployment"
        )
        raise Exception("Direct connections are not supported in this deployment")
    else:
        log.info(
            f"🔄 ROUTING: Using BACKEND completion flow for {model_id} (owned_by: {model.get('owned_by')})"
        )
        # Check if user has access to the model
        if not bypass_filter and user.role == "user":
            try:
                await check_model_access(user, model)
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

            if form_data.get("stream") == True and upstream_stream:

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
            if form_data.get("stream") and upstream_stream:
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
    # Run outlet filters against the authoritative completed assistant turn.
    # Persist any mutation and emit a catch-up event so the live mirror matches
    # the stored result.
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
            await Functions.get_function_by_id(filter_id)
            for filter_id in await get_sorted_filter_ids(
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
    # into content_blocks under TWO unconditional invariants (no fail-safe):
    #   1. Structural blocks (reasoning, tool_calls, subagent_launch, ...)
    #      are preserved BYTE-IDENTICAL regardless
    #      of what the filter did to their serialized <details ...> markers
    #      — including filters that elide or reformat those markers.
    #   2. The filter's text changes are ALWAYS applied — never silently
    #      dropped. Losing a filter edit was previously considered "safe";
    #      it is not — it produces a silent divergence between what the
    #      filter intended and what the user sees.
    # See _apply_outlet_text_to_blocks for the algorithm.
    merged_blocks = _apply_outlet_text_to_blocks(content_blocks, content, final_content)

    if merged_blocks == content_blocks:
        return

    # Recompute the serialized projection from the merged blocks so the
    # legacy `content` column matches the canonical `content_blocks`. The
    # filter's `final_content` may not round-trip through
    # serialize_content_blocks verbatim (text normalisation, struct marker
    # reformatting) — using the post-merge projection keeps both columns
    # consistent.
    merged_serialized = serialize_content_blocks(merged_blocks, force=True)

    try:
        await Chats.upsert_message_to_chat_by_id_and_message_id(
            metadata["chat_id"],
            metadata["message_id"],
            {
                "content": merged_serialized,
                "content_blocks": merged_blocks,
            },
            return_model=False,
        )
    except Exception as e:
        log.exception(f"Outlet filter persist failed: {e}")

    try:
        from open_webui.env import STREAM_PROTOCOL_VERSION

        if STREAM_PROTOCOL_VERSION == "v2.1":
            # The merge is non-incremental and may have added/removed text
            # blocks; emit a single `replace` covering the full block list
            # (B9 wire contract #1 — `replace` with block_idx=0 and a
            # complete content_blocks payload). F4's applyDeltaOp replaces
            # mirror.content_blocks entirely when given the full list.
            await event_emitter(
                {
                    "type": "chat:delta",
                    "data": {
                        "message_id": metadata.get("message_id"),
                        "op": "replace",
                        "payload": {
                            "block_idx": 0,
                            "content_blocks": merged_blocks,
                        },
                    },
                }
            )
        else:
            await event_emitter(
                {
                    "type": "chat:message",
                    "data": {
                        "content": merged_serialized,
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


# Block types treated as text-bearing: their `content` participates in the
# serialized text projection. Everything else is structural (immutable across
# an outlet-filter merge).
_TEXT_BLOCK_TYPES = frozenset({"text"})


def _split_serialized(s):
    """Tokenize a serialized projection into a sequence of
    ('text'|'details', str) pairs. Kept for any external callers.
    """
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


def _structural_block_indices(content_blocks):
    """Indices of blocks that are NOT plain text. Anything not in
    _TEXT_BLOCK_TYPES is treated as structural and preserved verbatim —
    including unknown block types we don't recognize (better to keep an
    opaque block than to lose data).
    """
    return [
        i
        for i, b in enumerate(content_blocks)
        if b.get("type") not in _TEXT_BLOCK_TYPES
    ]


def _fill_region_gaps(
    gaps,
    first_gap,
    last_gap,
    region_text,
    original_text_segments,
    first_orig_seg,
):
    """Distribute ``region_text`` (a stretch of filter output between two
    surviving markers — or filter boundaries) across the gap indices
    ``[first_gap .. last_gap]`` inclusive, using the corresponding
    original text segments as positional anchors when markers in the
    middle were elided by the filter.

    Strategy: walk the original segments belonging to this region in
    order. For every segment EXCEPT the last, look for the NEXT
    segment's text as a substring in ``region_text`` starting at the
    current cursor; everything up to that match becomes the current
    gap, then the cursor advances to the match start (the matched text
    is NOT consumed — it belongs to the next slot). If a needed anchor
    isn't findable (filter rewrote the surrounding text), fall back to
    emitting "" for the current gap so the run accumulates into the
    next slot — this preserves the historical "accumulate into next
    surviving gap" behavior and keeps the unconditional invariants
    intact (no text is dropped: the trailing slot always gets
    region_text[cursor:]).
    """
    n_slots = last_gap - first_gap + 1
    if n_slots <= 0:
        return
    if n_slots == 1:
        gaps[first_gap] = region_text
        return
    cursor = 0
    for k in range(n_slots - 1):
        # Look for the NEXT segment's text as an anchor.
        next_seg_idx = first_orig_seg + k + 1
        anchor = (
            original_text_segments[next_seg_idx]
            if next_seg_idx < len(original_text_segments)
            else ""
        )
        # Use a stripped anchor for the search to be resilient to
        # whitespace fluctuations introduced by the filter. The
        # leading/trailing newlines around <details> markers in the
        # original serialization frequently don't survive a round-trip
        # through arbitrary filter code.
        anchor_search = anchor.strip("\n")
        if anchor_search:
            pos = region_text.find(anchor_search, cursor)
        else:
            pos = -1
        if pos < 0:
            # Can't anchor — leave this slot empty; text reflows into
            # the next slot (or the trailing slot). Filter text is
            # never lost because the final slot always receives
            # region_text[cursor:].
            gaps[first_gap + k] = ""
        else:
            gaps[first_gap + k] = region_text[cursor:pos]
            cursor = pos
    # Trailing slot: rest of the region.
    gaps[last_gap] = region_text[cursor:]


def _apply_outlet_text_to_blocks(
    content_blocks, original_serialized, filter_serialized
):
    """Always-applies, always-preserves merge of an outlet filter's
    textual edit back into ``content_blocks``.

    Invariants (unconditional, no fail-safe):
      1. Every structural (non-text) block is preserved BYTE-IDENTICAL in
         the returned list, in the same relative order.
      2. The filter's textual output is FULLY reflected in the returned
         list — never silently dropped, even if the filter elided or
         reformatted some/all structural ``<details>`` markers.

    Algorithm:
      * Tokenize original_serialized to recover the ordered list of
        top-level ``<details ...>`` markers.
      * For each marker (paired positionally with a structural block),
        locate the *exact* marker substring inside filter_serialized using
        a rolling cursor (so matches are in order). Missing markers are
        "elided by the filter" — their structural block still survives.
      * Slice filter_serialized at the found marker positions into text
        "gaps" between structural slots; gaps for elided markers fold
        naturally into the adjacent surviving gap.
      * Reconstruct content_blocks: structural blocks copied as-is; for
        each gap, write its text into the first original text block in
        that slot (blanking any sibling text blocks that would otherwise
        ambiguously share the run); materialize NEW text blocks for gaps
        that have no original text block to host them.

    Never returns None.
    """
    # Identity short-circuit.
    if original_serialized == filter_serialized:
        return [dict(b) for b in content_blocks]

    struct_indices = _structural_block_indices(content_blocks)
    # Tokenize original_serialized into the alternating sequence of
    # original text-segments (between markers) and marker spans. We need
    # both the marker strings AND the text between them — the inter-marker
    # text acts as a positional anchor when reconstructing slot text for
    # elided markers (see below).
    original_markers = []  # list[str]
    original_text_segments = []  # length = len(original_markers) + 1
    _last = 0
    for _m in _DETAILS_RE.finditer(original_serialized):
        original_text_segments.append(original_serialized[_last : _m.start()])
        original_markers.append(_m.group(0))
        _last = _m.end()
    original_text_segments.append(original_serialized[_last:])

    # Pair original markers with structural blocks positionally. If counts
    # mismatch (shouldn't normally — serialize_content_blocks emits one
    # marker per structural block — but be defensive), we still preserve
    # every structural block; markers without a structural slot are
    # ignored, structural slots without a marker get None (elided).
    paired = min(len(original_markers), len(struct_indices))

    # Locate each paired marker in filter_serialized via rolling cursor.
    found_spans = []  # list[Optional[tuple[int, int]]] aligned with struct_indices
    cursor = 0
    for i in range(paired):
        marker = original_markers[i]
        pos = filter_serialized.find(marker, cursor)
        if pos < 0:
            found_spans.append(None)
        else:
            found_spans.append((pos, pos + len(marker)))
            cursor = pos + len(marker)
    # Extra structural blocks beyond `paired` get no marker.
    found_spans.extend([None] * (len(struct_indices) - paired))

    # We want exactly len(struct_indices) + 1 gaps (one before each
    # structural slot, one trailing). Build them by walking the filter
    # output between surviving markers and splitting each "region" by
    # using ORIGINAL text-segments around elided markers as substring
    # anchors. This preserves the filter's intended text positioning
    # around elided markers (e.g. text that originally preceded an
    # elided <details> stays in the slot BEFORE the surviving
    # structural, not after it).
    gaps = [""] * (len(struct_indices) + 1)

    # Group consecutive slots into "regions" delimited by surviving
    # markers (or the start/end of the filter output). Each region knows
    # the slice of filter_serialized it spans, and the contiguous run of
    # gap indices it must populate.
    region_start = 0  # position in filter_serialized
    region_gap_start = 0  # index into `gaps`
    for slot_i, span in enumerate(found_spans):
        if span is None:
            continue
        # Region covers gaps [region_gap_start .. slot_i] inclusive
        # (slot_i is the gap immediately before this surviving struct).
        _fill_region_gaps(
            gaps,
            region_gap_start,
            slot_i,
            filter_serialized[region_start : span[0]],
            original_text_segments,
            region_gap_start,  # original-text-segment index of first gap in region
        )
        region_start = span[1]
        region_gap_start = slot_i + 1
    # Trailing region: from last surviving marker (or start) to end of
    # filter_serialized, covering gaps [region_gap_start .. last].
    _fill_region_gaps(
        gaps,
        region_gap_start,
        len(gaps) - 1,
        filter_serialized[region_start:],
        original_text_segments,
        region_gap_start,
    )

    # UX nicety: when NO markers survived AND the anchor-based split
    # found no anchors at all (filter completely rewrote everything),
    # the trailing slot is the only one with text. Hoisting that text
    # to the FIRST slot makes it visible BEFORE the surviving
    # structural blocks the model produced — matches the pre-fix
    # behavior for the "filter replaces everything" case.
    if (
        found_spans
        and all(s is None for s in found_spans)
        and all(not g for g in gaps[:-1])
        and gaps[-1]
    ):
        gaps[0] = gaps[-1]
        gaps[-1] = ""

    # Reconstruct content_blocks slot-by-slot.
    new_blocks = []
    slot = 0  # current gap index
    text_idxs_in_slot = []  # original indices of text blocks before next struct

    def _flush_slot():
        nonlocal text_idxs_in_slot, slot
        gap_text = gaps[slot].strip("\n")
        if text_idxs_in_slot:
            # Re-attribute: ALL gap text into the first text block; blank
            # any siblings. Consecutive text blocks share a single run in
            # the serialized form, so a faithful split back across them
            # isn't possible — collapsing into the first preserves the
            # filter's text verbatim while keeping block identity.
            first = text_idxs_in_slot[0]
            new_blocks.append({**content_blocks[first], "content": gap_text})
            for extra in text_idxs_in_slot[1:]:
                new_blocks.append({**content_blocks[extra], "content": ""})
        elif gap_text:
            # No original text block in this slot; materialize a new one.
            new_blocks.append({"type": "text", "content": gap_text})
        text_idxs_in_slot = []
        slot += 1

    for i, block in enumerate(content_blocks):
        if block.get("type") in _TEXT_BLOCK_TYPES:
            text_idxs_in_slot.append(i)
        else:
            _flush_slot()
            # Structural block preserved byte-identical (same dict object).
            new_blocks.append(block)

    # Trailing slot.
    _flush_slot()

    return new_blocks


async def _merge_outlet_filter_into_content_blocks(
    content_blocks, original_serialized, filter_serialized
):
    """Back-compat wrapper. The previous version could return None to
    signal "ambiguous — leave blocks alone"; that fail-safe is GONE.
    Always returns a list, always preserves structural blocks, always
    applies the filter's text edit.
    """
    return _apply_outlet_text_to_blocks(
        content_blocks, original_serialized, filter_serialized
    )


async def chat_action(request: Request, action_id: str, form_data: dict, user: Any):
    if "." in action_id:
        action_id, sub_action_id = action_id.split(".")
    else:
        sub_action_id = None

    action = await Functions.get_function_by_id(action_id)
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

    function_module, _, _ = await get_function_module_from_cache(request, action_id)

    if hasattr(function_module, "valves") and hasattr(function_module, "Valves"):
        valves = await Functions.get_function_valves_by_id(action_id)
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
                            **await Functions.get_user_valves_by_id_and_user_id(
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
