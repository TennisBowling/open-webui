"""Built-in view_image tool for Chat Completions vision handoff.

Chat Completions tool messages cannot carry image_url parts, so this tool
stores the requested image as an Open WebUI file and returns a structured
internal attachment. The model-visible tool output remains text-only; the
API-message conversion layer turns the attachment into a synthetic user image
message for the next request.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import logging
import mimetypes
import os
import re
import socket
import uuid
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver
from fastapi import Request
from pydantic import BaseModel

from open_webui.env import AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL, SRC_LOG_LEVELS
from open_webui.models.files import FileForm, Files
from open_webui.storage.provider import Storage
from open_webui.utils.container_workspace import _reclaim_outputs
from open_webui.utils.image_conversion import (
    prepare_image_data_for_provider,
    sniff_image_mime_type,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

VIEW_IMAGE_MAX_BYTES = 20 * 1024 * 1024
VIEW_IMAGE_FETCH_TIMEOUT_SECONDS = 30
VIEW_IMAGE_MAX_REDIRECTS = 5

_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_NAME_RE = re.compile(r"[\r\n\t/\\]+")
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain"}


class ViewImageError(Exception):
    """User/model-facing view_image failure."""


def _safe_chat_id(raw: Any) -> str:
    chat_id = str(raw or "")
    if (
        not chat_id
        or chat_id.startswith("local:")
        or len(chat_id) > 128
        or chat_id in {".", ".."}
        or not _CHAT_ID_RE.match(chat_id)
    ):
        raise ViewImageError("container workspace is not available for this chat")
    return chat_id


def _safe_filename(name: str | None, fallback: str = "image") -> str:
    name = os.path.basename((name or "").replace("\x00", "")).strip()
    if not name or name in {".", ".."}:
        name = fallback
    name = _SAFE_NAME_RE.sub("_", name).strip()
    return name or fallback


def _filename_for_mime(name: str, mime_type: str) -> str:
    stem = Path(_safe_filename(name)).stem or "image"
    ext = mimetypes.guess_extension(mime_type) or ""
    if ext == ".jpe":
        ext = ".jpg"
    if not ext:
        ext = Path(name).suffix or ".img"
    return f"{stem}{ext}"


def _workspace_root(data_root: str, chat_id: str) -> Path:
    return (Path(data_root).expanduser().resolve() / chat_id / "workspace").resolve()


def _sandbox_rel_path(source: str) -> str:
    if source.startswith("sandbox://workspace/"):
        rel = source[len("sandbox://workspace/") :]
    elif source == "sandbox://workspace":
        rel = ""
    elif source.startswith("sandbox:/workspace/"):
        rel = source[len("sandbox:/workspace/") :]
    elif source == "sandbox:/workspace":
        rel = ""
    else:
        raise ViewImageError("sandbox URL must start with sandbox:/workspace/")

    rel = unquote(rel).lstrip("/")
    if not rel:
        raise ViewImageError("sandbox URL must point to an image file")
    return rel


def _resolve_sandbox_image_path(source: str, metadata: dict) -> tuple[Path, str, str]:
    workspace = metadata.get("container_workspace") or {}
    chat_id = _safe_chat_id(workspace.get("chat_id") or metadata.get("chat_id"))
    data_root = str(workspace.get("data_root") or "").strip()
    if not data_root:
        raise ViewImageError("container workspace data root is not available")

    root = _workspace_root(data_root, chat_id)
    rel = _sandbox_rel_path(source)
    raw_path = root / rel

    try:
        if raw_path.is_symlink():
            raise ViewImageError("sandbox image path cannot be a symlink")
        resolved = raw_path.resolve()
        resolved.relative_to(root)
    except ViewImageError:
        raise
    except Exception as exc:
        raise ViewImageError("sandbox image path escapes the workspace") from exc

    if not resolved.is_file():
        raise ViewImageError(f"sandbox image not found: /workspace/{rel}")

    return resolved, rel, chat_id


def _guess_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = os.path.basename(unquote(parsed.path or ""))
    return _safe_filename(name, "image")


def _validate_web_image_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ViewImageError("image URL must use http or https")
    if not parsed.hostname:
        raise ViewImageError("image URL is missing a host")
    if parsed.username or parsed.password:
        raise ViewImageError("image URL must not include credentials")
    _validate_public_host_literal(parsed.hostname)


def _ip_is_public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.strip("[]")).is_global
    except ValueError:
        return False


def _validate_public_host_literal(host: str) -> None:
    normalized = (host or "").strip().strip("[]").lower().rstrip(".")
    if not normalized:
        raise ViewImageError("image URL is missing a host")
    if normalized in _LOCAL_HOSTNAMES or normalized.endswith(".local"):
        raise ViewImageError("image URL host must be public")
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return
    if not ip.is_global:
        raise ViewImageError("image URL host must be public")


def _resolve_public_addresses(host: str, port: int, family: socket.AddressFamily):
    _validate_public_host_literal(host)
    try:
        infos = socket.getaddrinfo(
            host,
            port,
            family=family or socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ViewImageError(f"could not resolve image URL host: {host}") from exc

    results = []
    seen = set()
    for info_family, _type, proto, _canonname, sockaddr in infos:
        address = sockaddr[0]
        if not _ip_is_public(address):
            continue
        key = (info_family, proto, address)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": info_family,
                "proto": proto,
                "flags": socket.AI_NUMERICHOST,
            }
        )

    if not results:
        raise ViewImageError("image URL host did not resolve to a public address")
    return results


class _PublicResolver(AbstractResolver):
    """Resolver that pins aiohttp to public addresses it validated itself."""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ):
        return await asyncio.to_thread(_resolve_public_addresses, host, port, family)

    async def close(self) -> None:
        return None


async def _read_response_limited(response: aiohttp.ClientResponse) -> bytes:
    length_header = response.headers.get("Content-Length")
    if length_header:
        try:
            if int(length_header) > VIEW_IMAGE_MAX_BYTES:
                raise ViewImageError(
                    f"image is too large (limit {VIEW_IMAGE_MAX_BYTES // (1024 * 1024)} MB)"
                )
        except ValueError:
            pass

    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > VIEW_IMAGE_MAX_BYTES:
            raise ViewImageError(
                f"image is too large (limit {VIEW_IMAGE_MAX_BYTES // (1024 * 1024)} MB)"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_web_image_bytes(
    request: Request, source: str
) -> tuple[bytes, str | None, str]:
    current_url = source.strip()
    _validate_web_image_url(current_url)

    timeout = aiohttp.ClientTimeout(total=VIEW_IMAGE_FETCH_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(
        resolver=_PublicResolver(),
        family=socket.AF_UNSPEC,
        use_dns_cache=False,
    )
    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        trust_env=False,
    ) as session:
        for _ in range(VIEW_IMAGE_MAX_REDIRECTS + 1):
            async with session.get(
                current_url,
                headers={
                    "Accept": "image/*,*/*;q=0.8",
                    "User-Agent": "OpenWebUI view_image",
                },
                allow_redirects=False,
                ssl=AIOHTTP_CLIENT_SESSION_TOOL_SERVER_SSL,
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ViewImageError("image URL redirected without a Location header")
                    current_url = urljoin(current_url, location)
                    _validate_web_image_url(current_url)
                    continue

                if response.status >= 400:
                    raise ViewImageError(f"image URL returned HTTP {response.status}")

                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
                data = await _read_response_limited(response)
                return data, content_type or None, _guess_name_from_url(current_url)

        raise ViewImageError("image URL redirected too many times")


def _store_image_file(
    request: Request,
    user_dict: dict,
    image_data: bytes,
    mime_type: str,
    display_name: str,
    source: str,
) -> dict:
    file_id = str(uuid.uuid4())
    filename = _filename_for_mime(display_name, mime_type)
    storage_name = f"{file_id}_{filename}"
    size = len(image_data)
    sha256 = hashlib.sha256(image_data).hexdigest()

    _, stored_path = Storage.upload_file(
        io.BytesIO(image_data),
        storage_name,
        {
            "OpenWebUI-User-Email": str(user_dict.get("email", "")),
            "OpenWebUI-User-Id": str(user_dict.get("id", "")),
            "OpenWebUI-User-Name": str(user_dict.get("name", "")),
            "OpenWebUI-File-Id": file_id,
        },
    )

    file_item = Files.insert_new_file(
        str(user_dict.get("id") or ""),
        FileForm(
            id=file_id,
            filename=filename,
            path=stored_path,
            data={
                "status": "completed",
                "view_image": {"source": source, "sha256": sha256},
            },
            meta={
                "name": filename,
                "content_type": mime_type,
                "size": size,
                "data": {"view_image": {"source": source, "sha256": sha256}},
            },
        ),
    )
    if not file_item:
        raise ViewImageError("failed to store image")

    return {
        "url": str(request.app.url_path_for("get_file_content_by_id", id=file_id)),
        "file_id": file_id,
        "display_name": filename,
        "mime_type": mime_type,
        "size": size,
        "sha256": sha256,
    }


def _prepare_image_for_attachment(
    raw: bytes, mime_type: str | None, display_name: str
) -> tuple[bytes, str]:
    image_data, provider_mime = prepare_image_data_for_provider(
        raw, mime_type, display_name
    )
    sniffed = sniff_image_mime_type(image_data)
    if sniffed != provider_mime:
        raise ViewImageError("source did not contain a supported image")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_data)) as image:
            image.verify()
    except ImportError:
        pass
    except Exception as exc:
        raise ViewImageError("source did not contain a readable image") from exc
    return image_data, provider_mime


class ViewImageTools:
    """Builtin tool collection for attaching images to the next model request."""

    class Valves(BaseModel):
        """Placeholder; no per-instance configuration."""

        pass

    def __init__(self):
        self.valves = self.Valves()

    async def view_image(
        self,
        source: str,
        detail: Literal["auto", "low", "high"] = "auto",
        __request__: Optional[Request] = None,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ) -> str | dict:
        """Attach an image so you can inspect it visually on the next model call.

        Use for image URLs found by web_search/web_fetch or sandbox:/workspace/...
        image files created or discovered in the container workspace.

        :param source: Image location. Must be an http(s) URL or sandbox:/workspace/... URL.
        :param detail: Vision detail level: auto, low, or high.
        :return: A short status message. On success the image is attached internally for visual inspection.
        """
        if not __request__:
            return "Error: request context is not available"
        user_dict = __user__ or {}
        metadata = __metadata__ or {}
        detail = detail if detail in {"auto", "low", "high"} else "auto"
        source = (source or "").strip()

        try:
            if source.startswith(("http://", "https://")):
                raw, mime_type, display_name = await _fetch_web_image_bytes(__request__, source)
            elif source.startswith("sandbox:"):
                target, rel, chat_id = _resolve_sandbox_image_path(source, metadata)
                workspace = metadata.get("container_workspace") or {}
                server_id = str(workspace.get("server_id") or "")
                if server_id:
                    await _reclaim_outputs(__request__, chat_id, server_id)
                raw = target.read_bytes()
                if len(raw) > VIEW_IMAGE_MAX_BYTES:
                    raise ViewImageError(
                        f"image is too large (limit {VIEW_IMAGE_MAX_BYTES // (1024 * 1024)} MB)"
                    )
                mime_type = mimetypes.guess_type(target.name)[0]
                display_name = target.name
                source = f"sandbox:/workspace/{rel}"
            else:
                raise ViewImageError("source must be an http(s) URL or sandbox:/workspace/... URL")

            image_data, provider_mime = _prepare_image_for_attachment(
                raw, mime_type, display_name
            )
            stored = _store_image_file(
                __request__, user_dict, image_data, provider_mime, display_name, source
            )
            attachment = {
                "url": stored["url"],
                "detail": detail,
                "source": source,
                "file_id": stored["file_id"],
                "mime_type": stored["mime_type"],
            }
            return {
                "content": f"Image attached for visual inspection: {stored['display_name']}",
                "vision_attachments": [attachment],
            }
        except ViewImageError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            log.exception("view_image failed for %r", source)
            return f"Error: failed to attach image: {exc}"


_view_image_tools_instance: Optional[ViewImageTools] = None


def get_view_image_tools_instance() -> ViewImageTools:
    global _view_image_tools_instance
    if _view_image_tools_instance is None:
        _view_image_tools_instance = ViewImageTools()
    return _view_image_tools_instance
