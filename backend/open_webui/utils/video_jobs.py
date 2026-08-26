"""Runs the video pipeline as durable, resumable background work.

Every stage transition and progress tick is written to the ``video_job`` row
*before* it is broadcast over the socket. The socket is therefore a pure
optimization: a client that missed events (tab closed, phone asleep, different
device) recovers the identical state by re-reading the row via
``GET /api/v1/videos/jobs/active``. Nothing about progress lives only in memory.

Progress writes are throttled — ffmpeg and yt-dlp both emit hundreds of ticks a
second, and neither the database nor the socket should see that rate.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.files import FileForm, Files
from open_webui.models.videos import (
    STAGE_DONE,
    STAGE_DOWNLOADING,
    STAGE_PROBING,
    STAGE_PROCESSING,
    STAGE_RESOLVING,
    STAGE_STORING,
    STATUS_CANCELED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    VideoJobModel,
    VideoJobs,
)
from open_webui.storage.provider import Storage
from open_webui.utils.video_ingest import VideoIngestError, download_video
from open_webui.utils.video_processing import (
    DEFAULT_FPS,
    DEFAULT_QUALITY,
    VideoProcessingError,
    estimate_tokens,
    probe_video,
    process_video,
    quality_to_height,
    resolve_trim,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

# Emit at most this often, and only when the number actually moved.
_PROGRESS_MIN_INTERVAL = 0.7
_PROGRESS_MIN_DELTA = 2.0

# job_id -> running task, so a cancel request can reach the worker.
_RUNNING: dict[str, asyncio.Task] = {}

_STAGE_LABELS = {
    "queued": "Queued",
    "resolving": "Resolving link",
    "downloading": "Downloading",
    "fallback": "Trying fallback source",
    "probing": "Inspecting video",
    "processing": "Processing",
    "storing": "Saving",
    "done": "Done",
}


def stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, stage.replace("_", " ").title())


def job_to_dict(job: VideoJobModel) -> dict:
    """Wire shape shared by the REST endpoints and the socket events."""
    return {
        "id": job.id,
        "chat_id": job.chat_id,
        "status": job.status,
        "stage": job.stage,
        "stage_label": stage_label(job.stage),
        "source_type": job.source_type,
        "source_url": job.source_url,
        "title": job.title,
        "params": job.params or {},
        "progress": job.progress or {},
        "result": job.result or {},
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


async def broadcast_job(user_id: str, job: VideoJobModel) -> None:
    """Push job state to every session the user has open."""
    try:
        from open_webui.socket.main import broadcast_video_job_event

        await broadcast_video_job_event(user_id, job_to_dict(job))
    except Exception:
        log.debug("broadcast_job failed for %s", job.id, exc_info=True)


class _JobReporter:
    """Persists + broadcasts stage/progress, throttled."""

    def __init__(self, job_id: str, user_id: str):
        self.job_id = job_id
        self.user_id = user_id
        self._last_emit = 0.0
        self._last_pct: Optional[float] = None
        self._stage: Optional[str] = None

    async def set_stage(self, stage: str, detail: Optional[str] = None) -> None:
        self._stage = stage
        self._last_pct = None
        self._last_emit = 0.0
        job = await VideoJobs.update_job(
            self.job_id,
            status=STATUS_RUNNING,
            stage=stage,
            progress={
                "percent": None,
                "detail": detail,
                "label": stage_label(stage),
            },
        )
        if job:
            await broadcast_job(self.user_id, job)

    async def __call__(
        self, stage: str, percent: Optional[float], detail: Optional[str]
    ) -> None:
        now = time.monotonic()
        stage_changed = stage != self._stage
        moved = (
            percent is not None
            and self._last_pct is not None
            and abs(percent - self._last_pct) >= _PROGRESS_MIN_DELTA
        )
        first = percent is not None and self._last_pct is None

        if not stage_changed and not first and not moved:
            return
        if not stage_changed and now - self._last_emit < _PROGRESS_MIN_INTERVAL:
            return

        self._stage = stage
        self._last_emit = now
        if percent is not None:
            self._last_pct = percent

        job = await VideoJobs.update_job(
            self.job_id,
            status=STATUS_RUNNING,
            stage=stage,
            progress={
                "percent": round(percent, 1) if percent is not None else None,
                "detail": detail,
                "label": stage_label(stage),
            },
        )
        if job:
            await broadcast_job(self.user_id, job)


def _safe_stem(name: Optional[str], fallback: str = "video") -> str:
    stem = Path((name or "").strip()).stem
    stem = "".join(c for c in stem if c.isalnum() or c in " ._-").strip()
    return (stem[:60] or fallback).strip() or fallback


async def _store_processed_file(
    user_id: str,
    path: Path,
    *,
    display_name: str,
    meta_extra: dict,
) -> Optional[str]:
    """Persist the processed clip as a normal Open WebUI file row."""
    file_id = str(uuid.uuid4())
    filename = f"{_safe_stem(display_name)}.mp4"
    stored_name = f"{file_id}_{filename}"

    def _upload() -> tuple[int, str]:
        with open(path, "rb") as fh:
            return Storage.upload_file(
                fh,
                stored_name,
                {
                    "OpenWebUI-User-Id": user_id,
                    "OpenWebUI-File-Id": file_id,
                },
            )

    size, file_path = await asyncio.to_thread(_upload)

    item = await Files.insert_new_file(
        user_id,
        FileForm(
            id=file_id,
            filename=filename,
            path=file_path,
            # "completed" keeps this file out of the text-extraction pipeline.
            data={"status": "completed"},
            meta={
                "name": filename,
                "content_type": "video/mp4",
                "size": size,
                **meta_extra,
            },
        ),
    )
    return item.id if item else None


async def _resolve_source(
    job: VideoJobModel, workdir: Path, reporter: _JobReporter, max_height: Optional[int]
) -> tuple[Path, Optional[str], dict]:
    """Produce a local source file plus provenance metadata."""
    if job.source_type == "upload":
        if not job.source_file_id:
            raise VideoIngestError("The uploaded video is missing.")
        record = await Files.get_file_by_id(job.source_file_id)
        if not record or not record.path:
            raise VideoIngestError("The uploaded video could not be found.")

        await reporter.set_stage(STAGE_PROBING, "Reading uploaded file…")
        local = await asyncio.to_thread(Storage.get_file, record.path)
        return (
            Path(local),
            record.filename or (record.meta or {}).get("name"),
            {"source": "upload", "fallback_used": False},
        )

    await reporter.set_stage(STAGE_RESOLVING, "Resolving link…")
    result = await download_video(
        job.source_url or "",
        workdir / "download",
        max_height=max_height,
        on_progress=reporter,
    )
    return (
        result.path,
        result.title,
        {
            "source": result.source,
            "fallback_used": result.fallback_used,
            "extractor": result.extractor,
        },
    )


async def run_job(job_id: str, user_id: str) -> None:
    """Execute a job end to end. Terminal state is always written to the row."""
    job = await VideoJobs.get_job_by_id(job_id)
    if not job:
        return

    params = job.params or {}
    fps = float(params.get("fps") or DEFAULT_FPS)
    quality = str(params.get("quality") or DEFAULT_QUALITY)
    keep_audio = bool(params.get("audio", True))
    start = params.get("start")
    end = params.get("end")
    max_filesize = params.get("max_source_bytes") or None

    reporter = _JobReporter(job_id, user_id)
    workdir = Path(tempfile.mkdtemp(prefix=f"owui_video_{job_id[:8]}_"))

    try:
        source_path, title, provenance = await _resolve_source(
            job, workdir, reporter, quality_to_height(quality)
        )

        await reporter.set_stage(STAGE_PROBING, "Inspecting video…")
        info = await probe_video(source_path)

        start_s, end_s = resolve_trim(info.duration, start, end)
        await reporter.set_stage(
            STAGE_PROCESSING,
            f"{fps:g} fps · {quality} · {max(end_s - start_s, 0):.0f}s",
        )

        out_path = workdir / "processed.mp4"
        processed = await process_video(
            source_path,
            out_path,
            fps=fps,
            quality=quality,
            start=start_s,
            end=end_s,
            keep_audio=keep_audio,
            on_progress=reporter,
            source_info=info,
        )

        await reporter.set_stage(STAGE_STORING, "Saving…")
        display = title or job.title or "video"
        file_id = await _store_processed_file(
            user_id,
            processed.path,
            display_name=display,
            meta_extra={
                "video": {
                    "duration": round(processed.info.duration, 2),
                    "width": processed.info.width,
                    "height": processed.info.height,
                    "fps": fps,
                    "quality": quality,
                    "has_audio": processed.info.has_audio,
                    "frames": processed.frames,
                    "estimated_tokens": processed.estimated_tokens,
                    "trim": {"start": start_s, "end": end_s},
                    "source_duration": round(info.duration, 2),
                    **provenance,
                }
            },
        )
        if not file_id:
            raise VideoProcessingError("Could not save the processed video.")

        result = {
            "file_id": file_id,
            "filename": f"{_safe_stem(display)}.mp4",
            "duration": round(processed.info.duration, 2),
            "width": processed.info.width,
            "height": processed.info.height,
            "size": processed.path.stat().st_size,
            "frames": processed.frames,
            "estimated_tokens": processed.estimated_tokens,
            "has_audio": processed.info.has_audio,
            "source_duration": round(info.duration, 2),
            **provenance,
        }
        job = await VideoJobs.update_job(
            job_id,
            status=STATUS_COMPLETED,
            stage=STAGE_DONE,
            title=job.title or title,
            result=result,
            progress={"percent": 100, "label": "Done", "detail": None},
            error=None,
        )
        if job:
            await broadcast_job(user_id, job)

    except asyncio.CancelledError:
        job = await VideoJobs.mark_terminal(job_id, STATUS_CANCELED, error="Canceled.")
        if job:
            await broadcast_job(user_id, job)
        raise
    except (VideoIngestError, VideoProcessingError) as e:
        job = await VideoJobs.mark_terminal(job_id, STATUS_FAILED, error=str(e))
        if job:
            await broadcast_job(user_id, job)
    except Exception as e:
        log.exception("Video job %s failed", job_id)
        job = await VideoJobs.mark_terminal(
            job_id, STATUS_FAILED, error=f"Unexpected error: {e}"
        )
        if job:
            await broadcast_job(user_id, job)
    finally:
        _RUNNING.pop(job_id, None)
        # Only downloads and the processed copy live here; the stored file was
        # already handed to the storage provider, and an uploaded source lives
        # outside workdir, so this never deletes anything still referenced.
        shutil.rmtree(workdir, ignore_errors=True)


def schedule_job(job_id: str, user_id: str) -> None:
    """Fire-and-forget the pipeline; the row carries state, not this task."""
    if job_id in _RUNNING:
        return
    task = asyncio.create_task(run_job(job_id, user_id))
    _RUNNING[job_id] = task
    task.add_done_callback(lambda _t: _RUNNING.pop(job_id, None))


async def cancel_job(job_id: str) -> bool:
    task = _RUNNING.get(job_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


def estimate_for_params(
    *,
    duration: float,
    fps: float,
    keep_audio: bool,
    has_audio: bool = True,
) -> dict:
    frames, tokens = estimate_tokens(
        duration=duration, fps=fps, keep_audio=keep_audio, has_audio=has_audio
    )
    return {"frames": frames, "estimated_tokens": tokens}
