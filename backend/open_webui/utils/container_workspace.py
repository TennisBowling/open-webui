from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote

import aiohttp
from fastapi import Request

from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.chats import Chats
from open_webui.models.files import FileForm, Files
from open_webui.storage.provider import Storage
from open_webui.utils.access_control import has_access
from open_webui.models.users import UserModel

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SANDBOX_WORKSPACE_RE = re.compile(r"sandbox:/workspace/([^\s)\]]+)")
_MAX_CHAT_ID_LEN = 128
_CHUNK_SIZE = 1024 * 1024
_PREVIEW_MAX_BYTES = 2 * 1024 * 1024

_TEXT_PREVIEW_EXTS = {
    "txt", "md", "markdown", "rst", "csv", "tsv", "json", "jsonl", "ndjson",
    "yaml", "yml", "toml", "ini", "cfg", "conf", "env", "log", "xml", "svg",
    "py", "pyi", "ipynb", "js", "mjs", "cjs", "ts", "tsx", "jsx", "vue",
    "svelte", "java", "kt", "kts", "scala", "groovy", "c", "cc", "cpp",
    "cxx", "h", "hpp", "hxx", "rs", "go", "rb", "php", "pl", "pm", "lua",
    "r", "jl", "dart", "swift", "m", "mm", "cs", "fs", "fsx", "ex", "exs",
    "erl", "hs", "ml", "mli", "clj", "cljs", "sh", "bash", "zsh", "fish",
    "ps1", "bat", "cmd", "sql", "graphql", "gql", "proto", "css", "scss",
    "sass", "less", "tex", "bib", "srt", "vtt", "patch", "diff", "gitignore",
    "dockerignore", "editorconfig",
}
_TEXT_PREVIEW_MIME_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "text/csv",
    "text/markdown",
}


def _normalize_container_server_id(server_id: str | None) -> str:
    server_id = (server_id or "").strip()
    if server_id.startswith("server:mcp:"):
        server_id = server_id[len("server:mcp:") :]
    return server_id


def _settings(request: Request) -> tuple[bool, str, str]:
    config = request.app.state.config
    enabled = bool(getattr(config, "ENABLE_CONTAINER_WORKSPACE_SYNC", False))
    data_root = str(getattr(config, "CONTAINER_DATA_ROOT", "") or "").strip()
    server_id = _normalize_container_server_id(
        str(getattr(config, "CONTAINER_MCP_SERVER_ID", "") or "")
    )
    return enabled, data_root, server_id


def _safe_chat_id(raw: Any) -> Optional[str]:
    chat_id = str(raw or "")
    if not chat_id or chat_id.startswith("local:"):
        return None
    if len(chat_id) > _MAX_CHAT_ID_LEN:
        return None
    if chat_id in {".", ".."} or not _CHAT_ID_RE.match(chat_id):
        return None
    return chat_id


def _workspace_root(data_root: str, chat_id: str) -> Path:
    return (Path(data_root).expanduser().resolve() / chat_id / "workspace").resolve()


def _as_tool_id_list(tool_ids: Any) -> list[str]:
    if isinstance(tool_ids, str):
        return [tool_ids]
    if isinstance(tool_ids, list):
        return [str(t) for t in tool_ids if t is not None]
    return []


def is_container_workspace_active(
    request: Request, metadata: dict, tool_ids: Any = None
) -> bool:
    enabled, data_root, server_id = _settings(request)
    if not enabled or not data_root or not server_id:
        return False

    if _safe_chat_id(metadata.get("chat_id")) is None:
        return False

    selected = _as_tool_id_list(tool_ids if tool_ids is not None else metadata.get("tool_ids"))
    target = f"server:mcp:{server_id}"
    return any(tool_id == target or tool_id.startswith(f"{target}|") for tool_id in selected)


def _safe_filename(name: str | None, fallback: str = "file") -> str:
    name = os.path.basename((name or "").replace("\x00", "")).strip()
    if not name or name in {".", ".."}:
        name = fallback
    # Keep names readable, just remove path-ish/control characters.
    name = re.sub(r"[\r\n\t/\\]+", "_", name).strip()
    return name or fallback


def _unique_name(directory: Path, desired_name: str, used_names: set[str]) -> str:
    desired_name = _safe_filename(desired_name)
    stem, suffix = os.path.splitext(desired_name)
    stem = stem or "file"
    candidate = f"{stem}{suffix}"
    idx = 1
    used_lower = {n.lower() for n in used_names}
    while candidate.lower() in used_lower or (directory / candidate).exists():
        candidate = f"{stem}_{idx}{suffix}"
        idx += 1
    used_names.add(candidate)
    return candidate


def _hash_copy(src: Path, dest: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with src.open("rb") as in_f, dest.open("wb") as out_f:
        while True:
            chunk = in_f.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            out_f.write(chunk)
    return size, digest.hexdigest()


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _user_can_read_file(file: Any, user: Any) -> bool:
    if file is None or user is None:
        return False
    if getattr(file, "user_id", None) == getattr(user, "id", None):
        return True
    if getattr(user, "role", None) == "admin":
        return True
    access_control = getattr(file, "access_control", None)
    if access_control:
        return has_access(getattr(user, "id", ""), type="read", access_control=access_control)
    return False


def _file_id_from_item(item: dict) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    file_id = item.get("id") or ((item.get("file") or {}).get("id"))
    return str(file_id) if file_id else None


def _current_user_message(chat_id: str, assistant_message_id: str | None) -> tuple[str | None, dict | None]:
    if not assistant_message_id:
        return None, None

    assistant_msg = Chats.get_message_by_id_and_message_id(chat_id, assistant_message_id)
    parent_id = assistant_msg.get("parentId") if isinstance(assistant_msg, dict) else None
    if parent_id:
        parent_msg = Chats.get_message_by_id_and_message_id(chat_id, parent_id)
        if isinstance(parent_msg, dict):
            return parent_id, parent_msg

    messages_map = Chats.get_messages_map_by_chat_id(chat_id) or {}
    for mid, msg in messages_map.items():
        if not isinstance(msg, dict):
            continue
        if assistant_message_id in (msg.get("childrenIds") or []):
            return str(mid), msg
    return None, None


def _build_workspace_prompt(
    system_prompt: str, input_records: list[dict], output_paths: list[str]
) -> str:
    lines = [system_prompt.strip()] if system_prompt.strip() else []
    if input_records:
        lines.append("Uploaded files copied into /workspace/inputs:")
        for record in input_records[:50]:
            original = record.get("original_name") or "file"
            path = record.get("workspace_path") or ""
            lines.append(f"- {original} -> /workspace/{path}")
        if len(input_records) > 50:
            lines.append(f"- ... {len(input_records) - 50} more input file(s)")
    if output_paths:
        lines.append("Existing output files available for modification:")
        for path in output_paths[:50]:
            lines.append(f"- /workspace/{path}")
        if len(output_paths) > 50:
            lines.append(f"- ... {len(output_paths) - 50} more output file(s)")
    return "\n".join(line for line in lines if line)


async def prepare_container_workspace_for_turn(
    request: Request,
    metadata: dict,
    form_data: dict,
    user: UserModel,
    tool_ids: Any,
) -> Optional[str]:
    """Copy this turn's attached files into the container inputs folder.

    Returns a system prompt fragment when the container workspace is active.
    """
    if not is_container_workspace_active(request, metadata, tool_ids):
        return None

    _, data_root, server_id = _settings(request)
    chat_id = _safe_chat_id(metadata.get("chat_id"))
    if not chat_id:
        return None

    workspace = _workspace_root(data_root, chat_id)
    inputs_dir = workspace / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "outputs").mkdir(parents=True, exist_ok=True)

    user_message_id, user_message = _current_user_message(
        chat_id, metadata.get("message_id")
    )
    attached_files = []
    if isinstance(user_message, dict) and isinstance(user_message.get("files"), list):
        attached_files = user_message.get("files") or []
    elif isinstance(metadata.get("files"), list):
        attached_files = metadata.get("files") or []

    used_names = {p.name for p in inputs_dir.iterdir() if p.exists()}
    seen_file_ids: set[str] = set()
    input_records: list[dict] = []

    for item in attached_files:
        if not isinstance(item, dict):
            continue
        file_id = _file_id_from_item(item)
        if not file_id or file_id in seen_file_ids:
            continue
        seen_file_ids.add(file_id)

        file_record = Files.get_file_by_id(file_id)
        if not file_record or not _user_can_read_file(file_record, user):
            continue
        if not file_record.path:
            continue

        try:
            source_path = Path(Storage.get_file(file_record.path))
            if not source_path.is_file():
                continue
            original_name = _safe_filename(
                item.get("name")
                or ((item.get("file") or {}).get("filename"))
                or (file_record.meta or {}).get("name")
                or file_record.filename,
                fallback=file_record.filename or "file",
            )
            workspace_name = _unique_name(inputs_dir, original_name, used_names)
            target = inputs_dir / workspace_name
            size, sha256 = _hash_copy(source_path, target)
            content_type = (file_record.meta or {}).get("content_type")
            input_records.append(
                {
                    "file_id": file_id,
                    "original_name": original_name,
                    "workspace_path": f"inputs/{workspace_name}",
                    "size": size,
                    "sha256": sha256,
                    "content_type": content_type,
                    "message_id": user_message_id,
                }
            )
        except Exception as exc:
            log.warning("failed to copy file %s into container inputs: %s", file_id, exc)

    metadata["container_workspace"] = {
        "active": True,
        "server_id": server_id,
        "data_root": data_root,
        "inputs": input_records,
    }

    if input_records and user_message_id:
        existing = []
        if isinstance(user_message, dict) and isinstance(
            user_message.get("container_workspace_inputs"), list
        ):
            existing = user_message.get("container_workspace_inputs") or []
        Chats.upsert_message_to_chat_by_id_and_message_id(
            chat_id,
            user_message_id,
            {"container_workspace_inputs": [*existing, *input_records]},
        )

    output_paths = []
    for output in _output_files(workspace / "outputs"):
        try:
            output_paths.append(output.relative_to(workspace).as_posix())
        except Exception:
            continue

    system_prompt = str(
        getattr(request.app.state.config, "CONTAINER_SYSTEM_PROMPT", "") or ""
    )
    return _build_workspace_prompt(system_prompt, input_records, output_paths)


def _container_connection_url(request: Request, server_id: str) -> Optional[str]:
    for connection in getattr(request.app.state.config, "TOOL_SERVER_CONNECTIONS", []) or []:
        if not isinstance(connection, dict):
            continue
        if connection.get("type", "openapi") != "mcp":
            continue
        if str((connection.get("info") or {}).get("id") or "") == server_id:
            url = str(connection.get("url") or "").strip()
            return url or None
    return None


async def _reclaim_outputs(request: Request, chat_id: str, server_id: str) -> None:
    url = _container_connection_url(request, server_id)
    if not url:
        return
    base = url.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    endpoint = f"{base}/files/outputs/{quote(chat_id, safe='')}/reclaim"
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(endpoint) as response:
                await response.read()
    except Exception as exc:
        log.debug("container output ownership reclaim failed: %s", exc)


def _output_files(outputs_dir: Path) -> list[Path]:
    if not outputs_dir.is_dir():
        return []
    root = outputs_dir.resolve()
    files: list[Path] = []
    try:
        paths = list(outputs_dir.rglob("*"))
    except OSError as exc:
        log.warning("failed to list container outputs %s: %s", outputs_dir, exc)
        return []

    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            resolved.relative_to(root)
            files.append(path)
        except Exception:
            continue
    return sorted(files)


def _iter_text_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _iter_text_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_text_values(child)


def _sandbox_linked_files(
    workspace: Path, content: str | None, content_blocks: list | None
) -> list[Path]:
    root = workspace.resolve()
    texts = []
    if content:
        texts.append(content)
    if content_blocks:
        texts.extend(_iter_text_values(content_blocks))

    files: list[Path] = []
    seen: set[str] = set()
    for text in texts:
        for match in _SANDBOX_WORKSPACE_RE.finditer(text):
            rel = unquote(match.group(1)).lstrip("/")
            if not rel or rel.startswith("inputs/"):
                continue
            try:
                path = root / rel
                if path.is_symlink() or not path.is_file():
                    continue
                path.resolve().relative_to(root)
                key = str(path.resolve())
                if key in seen:
                    continue
                seen.add(key)
                files.append(path)
            except Exception:
                continue
    return files


def _used_output_display_names(outputs_state: dict) -> set[str]:
    used: set[str] = set()
    for state in outputs_state.values():
        if not isinstance(state, dict):
            continue
        for version in state.get("versions") or []:
            if isinstance(version, dict) and version.get("display_name"):
                used.add(str(version["display_name"]))
    return used


def _unique_display_name(rel_path: str, used_names: set[str]) -> str:
    desired = _safe_filename(Path(rel_path).name, fallback="output")
    stem, suffix = os.path.splitext(desired)
    stem = stem or "output"
    candidate = f"{stem}{suffix}"
    idx = 1
    used_lower = {n.lower() for n in used_names}
    while candidate.lower() in used_lower:
        candidate = f"{stem}_{idx}{suffix}"
        idx += 1
    used_names.add(candidate)
    return candidate


def _merge_files(existing: Any, new_files: list[dict]) -> list[dict]:
    merged = list(existing) if isinstance(existing, list) else []
    seen = {str(item.get("id")) for item in merged if isinstance(item, dict) and item.get("id")}
    for item in new_files:
        file_id = str(item.get("id") or "")
        if file_id and file_id in seen:
            continue
        if file_id:
            seen.add(file_id)
        merged.append(item)
    return merged


def _is_text_preview_file(filename: str, content_type: str) -> bool:
    ext = Path(filename or "").suffix.lower().lstrip(".")
    if ext in _TEXT_PREVIEW_EXTS:
        return True
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    return normalized.startswith("text/") or normalized in _TEXT_PREVIEW_MIME_TYPES


def _read_text_preview(path: Path, size: int) -> str:
    with path.open("rb") as f:
        data = f.read(_PREVIEW_MAX_BYTES + 1)
    truncated = size > _PREVIEW_MAX_BYTES or len(data) > _PREVIEW_MAX_BYTES
    if len(data) > _PREVIEW_MAX_BYTES:
        data = data[:_PREVIEW_MAX_BYTES]
    text = data.decode("utf-8", "replace")
    if truncated:
        text += f"\n\n[... preview truncated at {_PREVIEW_MAX_BYTES} bytes ...]"
    return text


def _store_output_file(
    request: Request,
    user: UserModel,
    source_path: Path,
    display_name: str,
    size: int,
    sha256: str,
    workspace_path: str,
    chat_id: str,
    message_id: str,
    version: int,
) -> Optional[dict]:
    file_id = str(uuid.uuid4())
    storage_name = f"{file_id}_{display_name}"
    content_type = mimetypes.guess_type(display_name)[0] or "application/octet-stream"
    metadata = {
        "chat_id": chat_id,
        "message_id": message_id,
        "workspace_path": workspace_path,
        "sha256": sha256,
        "version": version,
    }

    with source_path.open("rb") as f:
        _, stored_path = Storage.upload_file(
            f,
            storage_name,
            {
                "OpenWebUI-User-Email": getattr(user, "email", ""),
                "OpenWebUI-User-Id": getattr(user, "id", ""),
                "OpenWebUI-User-Name": getattr(user, "name", ""),
                "OpenWebUI-File-Id": file_id,
            },
        )

    data = {
        "status": "completed",
        "container_workspace": metadata,
    }
    if _is_text_preview_file(display_name, content_type):
        try:
            data["content"] = _read_text_preview(source_path, size)
        except OSError as exc:
            log.debug("failed to read text preview for %s: %s", source_path, exc)

    file_item = Files.insert_new_file(
        user.id,
        FileForm(
            id=file_id,
            filename=display_name,
            path=stored_path,
            data=data,
            meta={
                "name": display_name,
                "content_type": content_type,
                "size": size,
                "data": {"container_workspace": metadata},
            },
        ),
    )
    if not file_item:
        return None

    return {
        "type": "file",
        "id": file_id,
        "name": display_name,
        "url": str(request.app.url_path_for("get_file_by_id", id=file_id)),
        "size": size,
        "status": "uploaded",
        "container_workspace": metadata,
    }


async def import_changed_container_outputs(
    request: Request,
    metadata: dict,
    user: UserModel,
    content: str | None = None,
    content_blocks: list | None = None,
) -> list[dict]:
    if not is_container_workspace_active(request, metadata):
        return []

    _, data_root, server_id = _settings(request)
    chat_id = _safe_chat_id(metadata.get("chat_id"))
    message_id = str(metadata.get("message_id") or "")
    if not chat_id or not message_id:
        return []

    workspace = _workspace_root(data_root, chat_id)
    outputs_dir = workspace / "outputs"

    await _reclaim_outputs(request, chat_id, server_id)

    chat = Chats.get_chat_by_id(chat_id)
    if chat is None:
        return []
    chat_meta = dict(chat.meta or {})
    container_meta = dict(chat_meta.get("container_workspace") or {})
    outputs_state = dict(container_meta.get("outputs") or {})
    used_display_names = _used_output_display_names(outputs_state)

    imported: list[dict] = []
    changed = False

    candidates: list[tuple[str, str, Path]] = []
    seen_candidate_paths: set[str] = set()
    for path in _output_files(outputs_dir):
        try:
            rel_path = path.relative_to(outputs_dir).as_posix()
            key = str(path.resolve())
            seen_candidate_paths.add(key)
            candidates.append((rel_path, f"outputs/{rel_path}", path))
        except Exception:
            continue

    for path in _sandbox_linked_files(workspace, content, content_blocks):
        try:
            key = str(path.resolve())
            if key in seen_candidate_paths:
                continue
            workspace_path = path.relative_to(workspace).as_posix()
            seen_candidate_paths.add(key)
            candidates.append((workspace_path, workspace_path, path))
        except Exception:
            continue

    for state_key, workspace_path, path in candidates:
        try:
            size, sha256 = _hash_file(path)
            state = dict(outputs_state.get(state_key) or {})
            if state.get("last_hash") == sha256:
                continue

            version = int(state.get("version") or 0) + 1
            display_name = _unique_display_name(workspace_path, used_display_names)
            descriptor = _store_output_file(
                request,
                user,
                path,
                display_name,
                size,
                sha256,
                workspace_path,
                chat_id,
                message_id,
                version,
            )
            if not descriptor:
                continue

            versions = list(state.get("versions") or [])
            versions.append(
                {
                    "version": version,
                    "sha256": sha256,
                    "size": size,
                    "display_name": display_name,
                    "file_id": descriptor["id"],
                    "message_id": message_id,
                }
            )
            outputs_state[state_key] = {
                **state,
                "workspace_path": workspace_path,
                "last_hash": sha256,
                "version": version,
                "versions": versions,
            }
            imported.append(descriptor)
            changed = True
        except Exception as exc:
            log.warning("failed to import container output %s: %s", path, exc)

    if changed:
        container_meta["outputs"] = outputs_state
        container_meta["data_root"] = data_root
        container_meta["server_id"] = server_id
        chat_meta["container_workspace"] = container_meta
        Chats.update_chat_meta_by_id(chat_id, chat_meta)

    if imported:
        message = Chats.get_message_by_id_and_message_id(chat_id, message_id) or {}
        files = _merge_files(message.get("files"), imported)
        Chats.upsert_message_to_chat_by_id_and_message_id(
            chat_id, message_id, {"files": files}
        )

    return imported
