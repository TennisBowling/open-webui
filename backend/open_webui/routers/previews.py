"""Sandbox web previews: a thin proxy to container-agent-middleware.

CAM serves a server the agent started in a chat's sandbox at its own origin,
``<port>-<id>.<CAM_PREVIEW_DOMAIN>``, and lets a browser in only through a
single-use ticket. This router is the authorization layer: the chat's
participants (owner or editor of a shared chat, or an admin) may open that
chat's previews. The returned ``open_url`` carries
the ticket; the browser navigates to it and lands on the page.
"""

import logging
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.chats import Chats
from open_webui.routers.network import _WRITE_TIMEOUT_SECONDS, _cam
from open_webui.utils.auth import get_verified_user

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

router = APIRouter()


class OpenForm(BaseModel):
    path: Optional[str] = Field(default=None, max_length=2048)


class CreateForm(OpenForm):
    port: int = Field(ge=1, le=65535)


async def _require_chat_access(chat_id: str, user) -> None:
    """Owner or editor (or an admin); 404 otherwise so foreign ids are not
    confirmed. Not network.py's owner-only gate: an editor drives the same
    sandbox, so it may open the servers its own turns start. Viewers have no
    preview UI (read-only) and stay out."""
    if user.role == "admin":
        return
    if not await Chats.user_can_write_chat(chat_id, user.id):
        raise HTTPException(status_code=404, detail="Chat not found")


async def _preview_chat(request: Request, preview_id: str, user) -> str:
    """The preview's chat id, after checking the user may use that chat."""
    preview = await _cam(
        request, "GET", f"/links/{quote(preview_id, safe='')}", prefix="/preview"
    )
    chat_id = preview.get("chat_id") if isinstance(preview, dict) else None
    if not chat_id:
        raise HTTPException(status_code=404, detail="Preview not found")
    try:
        await _require_chat_access(chat_id, user)
    except HTTPException:
        raise HTTPException(status_code=404, detail="Preview not found") from None
    return chat_id


@router.get("/status")
async def get_preview_status(request: Request, user=Depends(get_verified_user)):
    return await _cam(request, "GET", "/status", prefix="/preview")


@router.get("/chats/{chat_id}")
async def list_chat_previews(
    request: Request, chat_id: str, user=Depends(get_verified_user)
):
    await _require_chat_access(chat_id, user)
    return await _cam(
        request, "GET", "/links", params={"chat_id": chat_id}, prefix="/preview"
    )


@router.post("/chats/{chat_id}")
async def open_chat_port(
    request: Request, chat_id: str, form: CreateForm, user=Depends(get_verified_user)
):
    """Create (or reuse) the chat's preview for a port and return a fresh open URL."""
    await _require_chat_access(chat_id, user)
    body = await _cam(
        request,
        "POST",
        "/links",
        json={
            "chat_id": chat_id,
            "port": form.port,
            "path": form.path,
            "created_by": user.id,
        },
        timeout_s=_WRITE_TIMEOUT_SECONDS,
        prefix="/preview",
    )
    log.info("preview: user %s opens chat %s port %s", user.id, chat_id, form.port)
    return body


@router.post("/{preview_id}/open")
async def open_preview(
    request: Request, preview_id: str, form: OpenForm, user=Depends(get_verified_user)
):
    await _preview_chat(request, preview_id, user)
    return await _cam(
        request,
        "POST",
        f"/links/{quote(preview_id, safe='')}/open",
        json={"path": form.path},
        timeout_s=_WRITE_TIMEOUT_SECONDS,
        prefix="/preview",
    )


@router.delete("/{preview_id}")
async def delete_preview(
    request: Request, preview_id: str, user=Depends(get_verified_user)
):
    await _preview_chat(request, preview_id, user)
    return await _cam(
        request,
        "DELETE",
        f"/links/{quote(preview_id, safe='')}",
        timeout_s=_WRITE_TIMEOUT_SECONDS,
        prefix="/preview",
    )
