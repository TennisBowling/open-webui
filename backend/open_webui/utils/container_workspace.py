from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote

import aiohttp
from fastapi import Request

from open_webui.env import (
    SRC_LOG_LEVELS,
    STREAM_BROWSER_FRAME_MAX_BYTES,
    STREAM_BROWSER_FRAME_MAX_FPS,
)
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
_OFFICE_PREVIEW_EXTS = {
    "doc", "docx", "odt", "rtf", "ppt", "pptx", "odp", "xls", "xlsx", "ods"
}
_OFFICE_PREVIEW_MIME_TYPES = {
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.presentation",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/rtf",
}
_PREVIEW_CONVERSION_TIMEOUT_SECONDS = 120


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


def _workspace_chat_id(metadata: dict) -> Optional[str]:
    return _safe_chat_id(
        metadata.get("container_workspace_chat_id") or metadata.get("chat_id")
    )


def _workspace_message_id(metadata: dict) -> Optional[str]:
    message_id = metadata.get("container_workspace_message_id") or metadata.get(
        "message_id"
    )
    return str(message_id) if message_id else None


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

    if _workspace_chat_id(metadata) is None:
        return False

    selected = _as_tool_id_list(
        tool_ids if tool_ids is not None else metadata.get("tool_ids")
    )
    target = f"server:mcp:{server_id}"
    return any(
        tool_id == target or tool_id.startswith(f"{target}|") for tool_id in selected
    )


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


def _stat_file(path: Path) -> tuple[int, int]:
    """Return (size, mtime_ns) cheaply for the incremental-scan fast path.
    A file whose size AND mtime match the recorded state is treated as
    unchanged, so we can skip re-hashing it (the expensive per-turn work over a
    workspace that accumulates many outputs)."""
    st = path.stat()
    return int(st.st_size), int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))


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


async def _current_user_message(chat_id: str, assistant_message_id: str | None) -> tuple[str | None, dict | None]:
    if not assistant_message_id:
        return None, None

    assistant_msg = await Chats.get_message_by_id_and_message_id(chat_id, assistant_message_id)
    parent_id = assistant_msg.get("parentId") if isinstance(assistant_msg, dict) else None
    if parent_id:
        parent_msg = await Chats.get_message_by_id_and_message_id(chat_id, parent_id)
        if isinstance(parent_msg, dict):
            return parent_id, parent_msg

    messages_map = await Chats.get_messages_map_by_chat_id(chat_id) or {}
    for mid, msg in messages_map.items():
        if not isinstance(msg, dict):
            continue
        if assistant_message_id in (msg.get("childrenIds") or []):
            return str(mid), msg
    return None, None


def _build_workspace_prompt(system_prompt: str, output_paths: list[str]) -> str:
    lines = [system_prompt.strip()] if system_prompt.strip() else []
    if output_paths:
        lines.append("Existing output files available for modification:")
        for path in output_paths[:50]:
            lines.append(f"- /workspace/{path}")
        if len(output_paths) > 50:
            lines.append(f"- ... {len(output_paths) - 50} more output file(s)")
    return "\n".join(line for line in lines if line)


def _xml_attr(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_input_location_context(input_records: list[dict]) -> str:
    blocks = []
    for record in input_records:
        original = _xml_attr(record.get("original_name") or "file")
        workspace_path = record.get("workspace_path") or ""
        if not workspace_path:
            continue
        blocks.append(
            f'<document filename="{original}">\n'
            f"Uploaded to location /workspace/{workspace_path}\n"
            "</document>"
        )
    return "\n\n".join(blocks)


async def _append_to_last_user_message(messages: list[dict], content: str) -> list[dict]:
    if not content:
        return messages
    updated = list(messages or [])
    for idx in range(len(updated) - 1, -1, -1):
        message = updated[idx]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        msg_content = message.get("content")
        if isinstance(msg_content, list):
            parts = list(msg_content)
            for part_idx, part in enumerate(parts):
                if isinstance(part, dict) and part.get("type") == "text":
                    text = str(part.get("text") or "")
                    parts[part_idx] = {**part, "text": f"{text}\n\n{content}"}
                    updated[idx] = {**message, "content": parts}
                    return updated
            updated[idx] = {**message, "content": [{"type": "text", "text": content}, *parts]}
            return updated
        text = str(msg_content or "")
        updated[idx] = {**message, "content": f"{text}\n\n{content}" if text else content}
        return updated
    return [*updated, {"role": "user", "content": content}]


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
    chat_id = _workspace_chat_id(metadata)
    if not chat_id:
        return None

    workspace = _workspace_root(data_root, chat_id)
    inputs_dir = workspace / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "outputs").mkdir(parents=True, exist_ok=True)

    user_message_id, user_message = await _current_user_message(
        chat_id, _workspace_message_id(metadata)
    )
    attached_files = []
    if isinstance(user_message, dict) and isinstance(user_message.get("files"), list):
        attached_files = user_message.get("files") or []
    elif isinstance(metadata.get("files"), list):
        attached_files = metadata.get("files") or []

    existing_input_records = []
    if isinstance(user_message, dict) and isinstance(
        user_message.get("container_workspace_inputs"), list
    ):
        existing_input_records = user_message.get("container_workspace_inputs") or []

    reuse_existing_inputs = bool(
        metadata.get("container_workspace_reuse_existing_inputs")
    )
    input_records: list[dict] = (
        list(existing_input_records) if reuse_existing_inputs else []
    )

    if not reuse_existing_inputs:
        used_names = {p.name for p in inputs_dir.iterdir() if p.exists()}
        seen_file_ids: set[str] = set()

        for item in attached_files:
            if not isinstance(item, dict):
                continue
            file_id = _file_id_from_item(item)
            if not file_id or file_id in seen_file_ids:
                continue
            seen_file_ids.add(file_id)

            file_record = await Files.get_file_by_id(file_id)
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
                log.warning(
                    "failed to copy file %s into container inputs: %s", file_id, exc
                )

    metadata["container_workspace"] = {
        "active": True,
        "chat_id": chat_id,
        "server_id": server_id,
        "data_root": data_root,
        "inputs": input_records,
    }

    if input_records and user_message_id and not reuse_existing_inputs:
        await Chats.upsert_message_to_chat_by_id_and_message_id(
            chat_id,
            user_message_id,
            {"container_workspace_inputs": [*existing_input_records, *input_records]}, return_model=False
        )

    input_context = _build_input_location_context(input_records)
    if input_context:
        form_data["messages"] = await _append_to_last_user_message(
            form_data.get("messages", []), input_context
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
    return _build_workspace_prompt(system_prompt, output_paths)


async def _container_connection_url(request: Request, server_id: str) -> Optional[str]:
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
    url = await _container_connection_url(request, server_id)
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


def _is_office_preview_file(filename: str, content_type: str) -> bool:
    ext = Path(filename or "").suffix.lower().lstrip(".")
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    return ext in _OFFICE_PREVIEW_EXTS or normalized in _OFFICE_PREVIEW_MIME_TYPES


async def _create_pdf_preview_file(
    request: Request,
    user: UserModel,
    source_path: Path,
    display_name: str,
    source_file_id: str,
) -> tuple[str | None, str | None]:
    libreoffice_bin = getattr(request.app.state, "LIBREOFFICE_BIN", None)
    if not libreoffice_bin:
        return None, "LibreOffice is not installed on the server."

    try:
        with tempfile.TemporaryDirectory(prefix="ow_container_preview_") as workdir:
            profile_dir = Path(workdir) / "lo_profile"
            outdir = Path(workdir) / "out"
            profile_dir.mkdir(parents=True, exist_ok=True)
            outdir.mkdir(parents=True, exist_ok=True)

            cmd = [
                libreoffice_bin,
                f"-env:UserInstallation=file://{profile_dir}",
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--convert-to",
                "pdf",
                "--outdir",
                str(outdir),
                str(source_path),
            ]
            result = subprocess.run(
                cmd,
                timeout=_PREVIEW_CONVERSION_TIMEOUT_SECONDS,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", "replace")[:500]
                return None, f"LibreOffice exited with code {result.returncode}: {stderr}"

            pdf_path = outdir / f"{source_path.stem}.pdf"
            if not pdf_path.is_file():
                candidates = list(outdir.glob("*.pdf"))
                pdf_path = candidates[0] if candidates else pdf_path
            if not pdf_path.is_file():
                return None, "LibreOffice did not produce a PDF preview."

            preview_id = str(uuid.uuid4())
            preview_name = f"{Path(display_name).stem}.pdf"
            storage_name = f"{preview_id}_{preview_name}"
            with pdf_path.open("rb") as f:
                preview_size, stored_path = Storage.upload_file(
                    f,
                    storage_name,
                    {
                        "OpenWebUI-User-Id": getattr(user, "id", ""),
                        "OpenWebUI-File-Id": preview_id,
                        "OpenWebUI-Source-File-Id": source_file_id,
                    },
                )

            preview_item = await Files.insert_new_file(
                user.id,
                FileForm(
                    id=preview_id,
                    filename=preview_name,
                    path=stored_path,
                    data={"status": "completed", "source_file_id": source_file_id},
                    meta={
                        "name": preview_name,
                        "content_type": "application/pdf",
                        "size": preview_size,
                    },
                ),
            )
            if not preview_item:
                return None, "Failed to persist PDF preview."
            return preview_id, None
    except subprocess.TimeoutExpired:
        return None, f"PDF preview conversion timed out after {_PREVIEW_CONVERSION_TIMEOUT_SECONDS}s."
    except Exception as exc:
        log.exception("failed to create output PDF preview for %s: %s", source_path, exc)
        return None, str(exc)


async def _store_output_file(
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

    if _is_office_preview_file(display_name, content_type):
        data["preview_status"] = "pending"

    file_item = await Files.insert_new_file(
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

    if _is_office_preview_file(display_name, content_type):
        preview_id, preview_error = await _create_pdf_preview_file(
            request, user, source_path, display_name, file_id
        )
        if preview_id:
            data["preview_status"] = "completed"
            data["preview_file_id"] = preview_id
            metadata["preview_status"] = "completed"
            metadata["preview_file_id"] = preview_id
        else:
            data["preview_status"] = "failed"
            data["preview_error"] = preview_error
            metadata["preview_status"] = "failed"
            metadata["preview_error"] = preview_error
        data["container_workspace"] = metadata
        await Files.update_file_data_by_id(file_id, data)

    return {
        "type": "file",
        "id": file_id,
        "name": display_name,
        "url": str(request.app.url_path_for("get_file_by_id", id=file_id)),
        "size": size,
        "status": "uploaded",
        "container_workspace": metadata,
        **(
            {"preview_file_id": metadata["preview_file_id"]}
            if metadata.get("preview_file_id")
            else {}
        ),
    }


async def import_changed_container_outputs(
    request: Request,
    metadata: dict,
    user: UserModel,
    content: str | None = None,
    content_blocks: list | None = None,
) -> list[dict]:
    if metadata.get("container_workspace_import_outputs") is False:
        return []

    if not is_container_workspace_active(request, metadata):
        return []

    _, data_root, server_id = _settings(request)
    chat_id = _workspace_chat_id(metadata)
    message_id = str(
        metadata.get("container_workspace_output_message_id")
        or metadata.get("message_id")
        or ""
    )
    if not chat_id or not message_id:
        return []

    workspace = _workspace_root(data_root, chat_id)
    outputs_dir = workspace / "outputs"

    await _reclaim_outputs(request, chat_id, server_id)

    chat = await Chats.get_chat_by_id(chat_id)
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
            state = dict(outputs_state.get(state_key) or {})

            # Incremental fast path: if size + mtime match the recorded state,
            # the file is unchanged — reuse the stored hash and skip re-reading
            # it. This avoids re-hashing the whole (possibly large, possibly
            # many-file) outputs tree every single turn. Falls back to a full
            # hash whenever the recorded stat is absent or doesn't match.
            try:
                cur_size, cur_mtime = await asyncio.to_thread(_stat_file, path)
            except Exception:
                cur_size, cur_mtime = None, None

            prior_hash = state.get("last_hash")
            if (
                prior_hash
                and cur_size is not None
                and state.get("stat_size") == cur_size
                and state.get("stat_mtime_ns") == cur_mtime
            ):
                # Unchanged since last turn — nothing to import.
                continue

            size, sha256 = await asyncio.to_thread(_hash_file, path)
            if state.get("last_hash") == sha256:
                # Content identical (mtime touched but bytes same): refresh the
                # cheap stat cache so future turns hit the fast path, then skip.
                if cur_size is not None:
                    outputs_state[state_key] = {
                        **state,
                        "stat_size": cur_size,
                        "stat_mtime_ns": cur_mtime,
                    }
                    changed = True
                continue

            version = int(state.get("version") or 0) + 1
            display_name = _unique_display_name(workspace_path, used_display_names)
            # Offload the whole store step to a thread: it does blocking Storage
            # I/O, a DB insert, AND (for office docs) a LibreOffice subprocess
            # that can run up to 120s. On the event loop that stalls EVERY other
            # chat on the worker until it finishes.
            descriptor = await _store_output_file(
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
                    **(
                        {"preview_file_id": descriptor["preview_file_id"]}
                        if descriptor.get("preview_file_id")
                        else {}
                    ),
                }
            )
            outputs_state[state_key] = {
                **state,
                "workspace_path": workspace_path,
                "last_hash": sha256,
                "stat_size": cur_size,
                "stat_mtime_ns": cur_mtime,
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
        await Chats.update_chat_meta_by_id(chat_id, chat_meta)

    if imported:
        message = await Chats.get_message_by_id_and_message_id(chat_id, message_id) or {}
        files = _merge_files(message.get("files"), imported)
        await Chats.upsert_message_to_chat_by_id_and_message_id(
            chat_id, message_id, {"files": files}, return_model=False
        )

    return imported


# ---------------------------------------------------------------------------
# Live browser progress (the in-container browser daemon writes per-session
# live-<session>.jpg + state-<session>.json — plus legacy live.jpg/state.json for
# the default "main" session — under .cam/browser/ while an action runs; we read
# them host-side and push them to the UI). With PARALLEL browsing there can be N
# concurrent tabs (one per agent/session), so the poller enumerates every active
# session and emits one browser:frame per session, tagged with its `session` id.
# See routers/streams for the reattach GET endpoint.
# ---------------------------------------------------------------------------

_BROWSER_LIVE_REL = ".cam/browser"
_BROWSER_LIVE_JPG = "live.jpg"
_BROWSER_LIVE_STATE = "state.json"
# Per-session file patterns: state-<session>.json / live-<session>.jpg.
_BROWSER_SESSION_STATE_RE = re.compile(r"^state-(?P<session>.+)\.json$")
_DEFAULT_BROWSER_SESSION = "main"


def _browser_live_dir(data_root: str, chat_id: str) -> Path:
    return _workspace_root(data_root, chat_id) / _BROWSER_LIVE_REL


def _read_live_pair(jpg: Path, state_path: Path) -> Optional[dict]:
    """Read one (jpg, state.json) pair into the live dict shape, or None."""
    state: Optional[dict] = None
    try:
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None

    frame: Optional[str] = None
    stat: Optional[tuple[int, int]] = None
    try:
        if jpg.is_file():
            stat = _stat_file(jpg)
            data = jpg.read_bytes()
            if data:
                frame = "data:image/jpeg;base64," + base64.b64encode(data).decode(
                    "ascii"
                )
    except OSError:
        frame = None
        stat = None

    if frame is None and state is None:
        return None
    return {"frame": frame, "state": state or {}, "stat": stat}


def read_browser_live(
    data_root: str, chat_id: str, session: Optional[str] = None
) -> Optional[dict]:
    """Read the daemon's current live frame + state host-side for ONE session.

    ``session`` selects which tab's files to read; ``None`` (or "main") reads the
    legacy un-suffixed live.jpg/state.json the default session also writes — so the
    existing single-view reattach path is unchanged. A specific session reads
    live-<session>.jpg/state-<session>.json.

    Returns ``{"frame": <data-url|None>, "state": <dict>, "stat": (size,mtime_ns)|None}``
    or ``None`` if nothing is available. Never deletes the files (the daemon
    overwrites them continuously). Safe to call from a thread.
    """
    safe_chat = _safe_chat_id(chat_id)
    if not data_root or not safe_chat:
        return None
    live_dir = _browser_live_dir(data_root, safe_chat)
    if not session or session == _DEFAULT_BROWSER_SESSION:
        jpg = live_dir / _BROWSER_LIVE_JPG
        state_path = live_dir / _BROWSER_LIVE_STATE
    else:
        token = _safe_session_token(session)
        jpg = live_dir / f"live-{token}.jpg"
        state_path = live_dir / f"state-{token}.json"
    return _read_live_pair(jpg, state_path)


def _safe_session_token(session: str) -> str:
    """Clamp a session id to the daemon's filesystem-safe filename charset.

    Mirrors the daemon's sessionFileToken so the host reads the same file the
    daemon writes. Defends against a surprising id escaping the live dir.
    """
    token = re.sub(r"[^A-Za-z0-9._-]", "_", str(session))[:128]
    return token or _DEFAULT_BROWSER_SESSION


async def read_browser_live_sessions(data_root: str, chat_id: str) -> dict[str, dict]:
    """Read EVERY active session's live frame + state for a chat.

    Enumerates the per-session state-<session>.json files in the chat's
    .cam/browser dir (the canonical "which tabs exist" signal) and pairs each with
    its live-<session>.jpg. Returns ``{session_id: live_dict}`` (the live_dict
    shape from read_browser_live). The legacy default ("main") session is read from
    the un-suffixed files so a daemon that only ever wrote those (no session ids
    sent) still surfaces exactly one tab. Empty dict when nothing is available.
    Safe to call from a thread.
    """
    safe_chat = _safe_chat_id(chat_id)
    if not data_root or not safe_chat:
        return {}
    live_dir = _browser_live_dir(data_root, safe_chat)
    out: dict[str, dict] = {}

    # Default/main session from the legacy un-suffixed files (the default session
    # writes both these AND state-main.json; prefer the legacy pair so a daemon
    # that predates per-session files still works).
    main_live = _read_live_pair(
        live_dir / _BROWSER_LIVE_JPG, live_dir / _BROWSER_LIVE_STATE
    )
    if main_live is not None:
        out[_DEFAULT_BROWSER_SESSION] = main_live

    # Per-session files for every other tab.
    try:
        names = list(live_dir.iterdir())
    except OSError:
        names = []
    for entry in names:
        match = _BROWSER_SESSION_STATE_RE.match(entry.name)
        if not match:
            continue
        session = match.group("session")
        if session == _DEFAULT_BROWSER_SESSION and _DEFAULT_BROWSER_SESSION in out:
            continue  # already covered by the legacy pair
        live = _read_live_pair(
            live_dir / f"live-{session}.jpg", live_dir / f"state-{session}.json"
        )
        if live is not None:
            out[session] = live
    return out


async def browser_progress_poller(
    *,
    data_root: str,
    chat_id: str,
    message_id: Optional[str],
    session_id: Optional[str],
    event_emitter,
    interval: float = 0.5,
    session: Optional[str] = None,
) -> None:
    """While a browser tool call runs, push live frames to the UI side-panel.

    Started concurrently with the (blocking) MCP browser tool call and cancelled
    when it returns. Emits ``browser:frame`` — a fire-and-forget custom event
    carrying the JPEG data URL — for EACH active browser session (tab). With
    parallel browsing several agents browse at once; this poller enumerates all of
    them and tags every frame with its ``session`` id so the UI can group them into
    tabs. Only re-emitted when a session's frame bytes actually change (per-session
    mtime/size guard), so a static page doesn't spam identical frames. On cancel it
    emits a TERMINAL frame (``done:true``) for every known session so the panel
    freezes each tab on its final view.

    Note: this poller intentionally does NOT emit persisted ``status``
    breadcrumbs. Each browser action already renders as its own inline
    collapsible tool-call card (running vs done), and the live side-panel shows
    the real-time screenshot, so a separate status line would be redundant.

    Designed to be resilient: any read/emit error is swallowed so the poller can
    never break the tool call it is shadowing.

    The default poll cadence (0.5s) is intentionally faster than the daemon's
    live-capture cadence: a single navigation is often ~1.4s, so a 1s poll would
    routinely miss the only in-flight frame and never open the live panel.
    """
    if not event_emitter or not data_root or not _safe_chat_id(chat_id):
        return
    poll_session = session or _DEFAULT_BROWSER_SESSION
    effective_interval = max(0.2, interval)
    if STREAM_BROWSER_FRAME_MAX_FPS > 0:
        effective_interval = max(effective_interval, 1.0 / STREAM_BROWSER_FRAME_MAX_FPS)
    last_stat: Optional[tuple[int, int]] = None

    async def _emit_frame(state: dict, frame: Optional[str], *, done: bool) -> None:
        payload = {
            # The session id this frame belongs to. The UI keys its browser panel
            # tabs by this. The daemon stamps `session` into state, so prefer that
            # (authoritative for which tab wrote it); else the poll session.
            "session": state.get("session") or poll_session,
            "url": state.get("url", ""),
            "title": state.get("title", ""),
            "phase": "done" if done else state.get("phase", ""),
            "action": state.get("action", ""),
            "elapsedMs": state.get("elapsedMs", 0),
            "startedAt": state.get("startedAt", 0),
            "done": done,
        }
        if frame:
            payload["frame"] = frame
        try:
            await event_emitter({"type": "browser:frame", "data": payload})
        except Exception:
            pass

    try:
        while True:
            try:
                live = await asyncio.to_thread(
                    read_browser_live, data_root, chat_id, poll_session
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                live = None

            if live:
                state = live.get("state") or {}
                stat = live.get("stat")
                frame = live.get("frame")
                # A daemon that just (re)started stamps the leftover state from a
                # previous turn done+stale so it can't masquerade as live. Treat
                # it as terminal: never emit it as an in-progress frame.
                is_stale = bool(state.get("stale"))
                done_flag = bool(state.get("done")) or is_stale

                # Frame: emit only when the JPEG changed. Optional byte cap keeps
                # pathological screenshots from dominating socket bandwidth; the
                # metadata event still reaches the panel, and reattach can fetch
                # the latest frame on demand.
                if frame and stat is not None and stat != last_stat:
                    last_stat = stat
                    if STREAM_BROWSER_FRAME_MAX_BYTES > 0 and stat[0] > STREAM_BROWSER_FRAME_MAX_BYTES:
                        frame = None
                    await _emit_frame(state, frame, done=done_flag)

            await asyncio.sleep(effective_interval)
    except asyncio.CancelledError:
        # The tool call returned. Do ONE final read and emit a terminal frame
        # (done:true) for this session. Shielded so cancellation still completes
        # promptly. The terminal frame freezes the live panel on the final view.
        try:
            live = await asyncio.shield(
                asyncio.to_thread(
                    read_browser_live, data_root, chat_id, poll_session
                )
            )
        except Exception:
            live = None
        if live and event_emitter:
            state = live.get("state") or {}
            frame = live.get("frame")
            await _emit_frame(state, frame, done=True)
        return
    except Exception:
        log.exception("browser_progress_poller crashed")
        return
