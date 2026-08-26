"""Built-in view_video tool — the video counterpart to view_image.

Chat Completions will not carry a ``video_url`` part on a tool message, so this
tool stores the processed clip as an Open WebUI file and returns a structured
internal attachment; the API-message conversion layer replays it as a synthetic
user message on the next request.

Encoding settings are deliberately **not** exposed to the model. Frame rate,
resolution, and audio are fixed at the same defaults the composer uses (1 fps,
720p, audio on), because those are cost/quality decisions that belong to the
operator, not to a model that cannot see the bill. The model gets exactly the
two parameters that are genuinely about *intent* — which part of the video to
look at — and both default to the whole thing.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from fastapi import Request
from pydantic import BaseModel

from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.files import FileForm, Files
from open_webui.storage.provider import Storage
from open_webui.utils.container_workspace import _reclaim_outputs
from open_webui.utils.video_ingest import VideoIngestError, download_video
from open_webui.utils.video_processing import (
    VideoProcessingError,
    probe_video,
    process_video,
    quality_to_height,
)
from open_webui.utils.view_image_tool import _SANDBOX_URI_PREFIX_RE, _safe_chat_id

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

# Fixed pipeline settings — see module docstring.
VIEW_VIDEO_FPS = 1.0
VIEW_VIDEO_QUALITY = "720p"
VIEW_VIDEO_AUDIO = True
VIEW_VIDEO_MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024


class ViewVideoError(Exception):
    """User/model-facing view_video failure."""


def _workspace_root(data_root: str, chat_id: str) -> Path:
    return (Path(data_root).expanduser().resolve() / chat_id / "workspace").resolve()


def _resolve_sandbox_video_path(source: str, metadata: dict) -> tuple[Path, str, str]:
    workspace = metadata.get("container_workspace") or {}
    chat_id = _safe_chat_id(workspace.get("chat_id") or metadata.get("chat_id"))
    data_root = str(workspace.get("data_root") or "").strip()
    if not data_root:
        raise ViewVideoError("container workspace data root is not available")

    root = _workspace_root(data_root, chat_id)
    rel = _SANDBOX_URI_PREFIX_RE.sub("", (source or "").strip(), count=1)
    rel = unquote(rel).lstrip("/")
    if not rel:
        raise ViewVideoError("sandbox URL must point to a video file")

    target = (root / rel).resolve()
    # Confinement check after resolve() so a symlink cannot escape /workspace.
    try:
        target.relative_to(root)
    except ValueError:
        raise ViewVideoError("sandbox path escapes the workspace root")
    if not target.is_file():
        raise ViewVideoError(f"no such file in the workspace: {rel}")
    return target, rel, chat_id


async def _store_video_file(
    user_id: str, path: Path, display_name: str, source: str
) -> dict:
    file_id = str(uuid.uuid4())
    stem = Path(display_name).stem or "video"
    stem = (
        "".join(c for c in stem if c.isalnum() or c in " ._-").strip()[:60] or "video"
    )
    filename = f"{stem}.mp4"

    def _upload():
        with open(path, "rb") as fh:
            return Storage.upload_file(
                fh,
                f"{file_id}_{filename}",
                {"OpenWebUI-User-Id": user_id, "OpenWebUI-File-Id": file_id},
            )

    size, stored_path = await asyncio.to_thread(_upload)

    item = await Files.insert_new_file(
        user_id,
        FileForm(
            id=file_id,
            filename=filename,
            path=stored_path,
            data={"status": "completed", "view_video": {"source": source}},
            meta={
                "name": filename,
                "content_type": "video/mp4",
                "size": size,
                "data": {"view_video": {"source": source}},
            },
        ),
    )
    if not item:
        raise ViewVideoError("failed to store video")
    return {"file_id": file_id, "filename": filename, "size": size}


class ViewVideoTools:
    """Builtin tool collection for attaching videos to the next model request."""

    class Valves(BaseModel):
        """Placeholder; no per-instance configuration."""

        pass

    def __init__(self):
        self.valves = self.Valves()

    async def view_video(
        self,
        source: str,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        __request__: Optional[Request] = None,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ) -> str | dict:
        """Attach a video so you can watch it on the next model call.

        Handles direct video links and normal video pages (YouTube, X/Twitter,
        TikTok, and most other sites), as well as video files in the container
        workspace. The video is sampled at 1 frame per second at 720p with audio.

        :param source: Video location. An http(s) URL (page or direct file) or a workspace path (/workspace/... or sandbox:/workspace/...).
        :param start_time: Optional start offset in seconds. Defaults to the beginning of the video.
        :param end_time: Optional end offset in seconds. Defaults to the end of the video.
        :return: A short status message. On success the video is attached internally for viewing.
        """
        if not __request__:
            return "Error: request context is not available"

        user_dict = __user__ or {}
        user_id = str(user_dict.get("id") or "")
        metadata = __metadata__ or {}
        source = (source or "").strip()
        if not source:
            return "Error: a video source is required"

        cfg = getattr(__request__.app.state, "config", None)
        if cfg is not None and not getattr(cfg, "ENABLE_VIDEO_INPUT", True):
            return "Error: video input is disabled on this instance"

        workdir = Path(tempfile.mkdtemp(prefix="owui_viewvideo_"))
        try:
            if source.startswith(("http://", "https://")):
                if cfg is not None and not getattr(
                    cfg, "ENABLE_VIDEO_URL_INGEST", True
                ):
                    return "Error: downloading videos from links is disabled"
                result = await download_video(
                    source,
                    workdir / "dl",
                    max_height=quality_to_height(VIEW_VIDEO_QUALITY),
                    max_filesize=VIEW_VIDEO_MAX_SOURCE_BYTES,
                )
                src_path = result.path
                display_name = result.title or Path(src_path).name
            elif _SANDBOX_URI_PREFIX_RE.match(source):
                target, rel, chat_id = _resolve_sandbox_video_path(source, metadata)
                workspace = metadata.get("container_workspace") or {}
                server_id = str(workspace.get("server_id") or "")
                if server_id:
                    # Flush pending container writes so a video the model just
                    # produced is actually on disk before we read it.
                    await _reclaim_outputs(__request__, chat_id, server_id)
                src_path = target
                display_name = target.name
                source = f"sandbox:/workspace/{rel}"
            else:
                return (
                    "Error: source must be an http(s) URL, a sandbox:/workspace/... URL, "
                    "or a /workspace/... path"
                )

            info = await probe_video(src_path)
            out_path = workdir / "processed.mp4"
            processed = await process_video(
                src_path,
                out_path,
                fps=VIEW_VIDEO_FPS,
                quality=VIEW_VIDEO_QUALITY,
                start=start_time,
                end=end_time,
                keep_audio=VIEW_VIDEO_AUDIO,
                source_info=info,
            )

            stored = await _store_video_file(
                user_id, processed.path, display_name, source
            )
            url = str(
                __request__.app.url_path_for(
                    "get_file_content_by_id", id=stored["file_id"]
                )
            )

            span = processed.info.duration
            summary = (
                f"Video attached for viewing: {stored['filename']} "
                f"({span:.0f}s at {VIEW_VIDEO_FPS:g} fps, "
                f"{processed.info.width}x{processed.info.height}"
                f"{', with audio' if processed.info.has_audio else ''})."
            )
            if info.duration and span < info.duration - 1:
                summary += f" Source video is {info.duration:.0f}s long."

            return {
                "content": summary,
                "video_attachments": [
                    {
                        "url": url,
                        "source": source,
                        "file_id": stored["file_id"],
                        "mime_type": "video/mp4",
                    }
                ],
            }
        except (ViewVideoError, VideoIngestError, VideoProcessingError) as exc:
            return f"Error: {exc}"
        except Exception as exc:
            log.exception("view_video failed for %r", source)
            return f"Error: failed to attach video: {exc}"
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


_view_video_tools_instance: Optional[ViewVideoTools] = None


def get_view_video_tools_instance() -> ViewVideoTools:
    global _view_video_tools_instance
    if _view_video_tools_instance is None:
        _view_video_tools_instance = ViewVideoTools()
    return _view_video_tools_instance
