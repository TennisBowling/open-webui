"""Video-attachment endpoints.

The pipeline runs server-side and its state lives in ``video_job`` rows, so the
client's job here is only to (a) start work, (b) subscribe to socket updates,
and (c) re-read state on mount. ``GET /jobs/active`` is what makes closing the
tab, reloading, or switching devices a non-event: whatever the composer lost, it
refetches.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from open_webui.config import (
    ENABLE_VIDEO_INPUT,
    ENABLE_VIDEO_URL_INGEST,
    VIDEO_DEFAULT_AUDIO,
    VIDEO_DEFAULT_FPS,
    VIDEO_DEFAULT_QUALITY,
    VIDEO_MAX_SOURCE_SIZE_MB,
    VIDEO_WARN_DURATION_SECONDS,
)
from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.files import Files
from open_webui.models.videos import (
    STATUS_CANCELED,
    TERMINAL_STATUSES,
    VideoJobs,
)
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.video_jobs import (
    broadcast_job,
    cancel_job,
    estimate_for_params,
    job_to_dict,
    schedule_job,
)
from open_webui.utils.video_processing import QUALITY_PRESETS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

router = APIRouter()

MAX_FPS = 30.0
MIN_FPS = 0.1


def _video_config(request: Request) -> dict:
    cfg = request.app.state.config
    return {
        "enabled": bool(cfg.ENABLE_VIDEO_INPUT),
        "url_ingest_enabled": bool(cfg.ENABLE_VIDEO_URL_INGEST),
        "default_fps": float(cfg.VIDEO_DEFAULT_FPS),
        "default_quality": str(cfg.VIDEO_DEFAULT_QUALITY),
        "default_audio": bool(cfg.VIDEO_DEFAULT_AUDIO),
        "max_source_size_mb": int(cfg.VIDEO_MAX_SOURCE_SIZE_MB),
        "warn_duration_seconds": int(cfg.VIDEO_WARN_DURATION_SECONDS),
        "qualities": [q for q in QUALITY_PRESETS],
    }


class VideoJobForm(BaseModel):
    source_type: str = Field(default="url")  # "url" | "upload"
    url: Optional[str] = None
    file_id: Optional[str] = None
    chat_id: Optional[str] = None
    fps: Optional[float] = None
    quality: Optional[str] = None
    start: Optional[float] = None
    end: Optional[float] = None
    audio: Optional[bool] = None


class VideoConfigForm(BaseModel):
    enabled: Optional[bool] = None
    url_ingest_enabled: Optional[bool] = None
    default_fps: Optional[float] = None
    default_quality: Optional[str] = None
    default_audio: Optional[bool] = None
    max_source_size_mb: Optional[int] = None
    warn_duration_seconds: Optional[int] = None


class VideoEstimateForm(BaseModel):
    duration: float = 0.0
    fps: Optional[float] = None
    audio: Optional[bool] = None
    has_audio: bool = True


@router.get("/config")
async def get_video_config(request: Request, user=Depends(get_verified_user)):
    return _video_config(request)


@router.post("/config")
async def update_video_config(
    request: Request, form_data: VideoConfigForm, user=Depends(get_admin_user)
):
    cfg = request.app.state.config
    if form_data.enabled is not None:
        cfg.ENABLE_VIDEO_INPUT = form_data.enabled
    if form_data.url_ingest_enabled is not None:
        cfg.ENABLE_VIDEO_URL_INGEST = form_data.url_ingest_enabled
    if form_data.default_fps is not None:
        cfg.VIDEO_DEFAULT_FPS = max(MIN_FPS, min(float(form_data.default_fps), MAX_FPS))
    if form_data.default_quality is not None:
        if form_data.default_quality not in QUALITY_PRESETS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown quality preset: {form_data.default_quality}",
            )
        cfg.VIDEO_DEFAULT_QUALITY = form_data.default_quality
    if form_data.default_audio is not None:
        cfg.VIDEO_DEFAULT_AUDIO = form_data.default_audio
    if form_data.max_source_size_mb is not None:
        cfg.VIDEO_MAX_SOURCE_SIZE_MB = max(1, int(form_data.max_source_size_mb))
    if form_data.warn_duration_seconds is not None:
        cfg.VIDEO_WARN_DURATION_SECONDS = max(0, int(form_data.warn_duration_seconds))
    return _video_config(request)


@router.post("/estimate")
async def estimate(
    request: Request, form_data: VideoEstimateForm, user=Depends(get_verified_user)
):
    """Token/frame estimate for the composer. Advisory only — never blocks."""
    cfg = request.app.state.config
    fps = form_data.fps if form_data.fps is not None else float(cfg.VIDEO_DEFAULT_FPS)
    audio = (
        form_data.audio
        if form_data.audio is not None
        else bool(cfg.VIDEO_DEFAULT_AUDIO)
    )
    return estimate_for_params(
        duration=max(form_data.duration, 0.0),
        fps=max(MIN_FPS, min(float(fps), MAX_FPS)),
        keep_audio=bool(audio),
        has_audio=bool(form_data.has_audio),
    )


@router.post("/jobs")
async def create_job(
    request: Request, form_data: VideoJobForm, user=Depends(get_verified_user)
):
    cfg = request.app.state.config
    if not cfg.ENABLE_VIDEO_INPUT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Video input is disabled on this instance.",
        )

    source_type = (form_data.source_type or "url").lower()
    if source_type not in {"url", "upload"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid source type."
        )

    source_url = None
    source_file_id = None
    title = None

    if source_type == "url":
        if not cfg.ENABLE_VIDEO_URL_INGEST:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Downloading videos from links is disabled on this instance.",
            )
        source_url = (form_data.url or "").strip()
        if not source_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A video URL is required.",
            )
    else:
        source_file_id = (form_data.file_id or "").strip()
        if not source_file_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An uploaded file is required.",
            )
        record = await Files.get_file_by_id_and_user_id(source_file_id, user.id)
        if not record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Uploaded file not found."
            )
        title = record.filename or (record.meta or {}).get("name")

    quality = form_data.quality or str(cfg.VIDEO_DEFAULT_QUALITY)
    if quality not in QUALITY_PRESETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown quality preset: {quality}",
        )

    fps = form_data.fps if form_data.fps is not None else float(cfg.VIDEO_DEFAULT_FPS)
    fps = max(MIN_FPS, min(float(fps), MAX_FPS))
    audio = (
        form_data.audio
        if form_data.audio is not None
        else bool(cfg.VIDEO_DEFAULT_AUDIO)
    )

    start = max(float(form_data.start), 0.0) if form_data.start is not None else None
    end = max(float(form_data.end), 0.0) if form_data.end is not None else None

    job = await VideoJobs.insert_new_job(
        user.id,
        source_type,
        chat_id=form_data.chat_id,
        source_url=source_url,
        source_file_id=source_file_id,
        title=title,
        params={
            "fps": fps,
            "quality": quality,
            "audio": bool(audio),
            "start": start,
            "end": end,
            "max_source_bytes": int(cfg.VIDEO_MAX_SOURCE_SIZE_MB) * 1024 * 1024,
        },
    )
    if not job:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not start video processing.",
        )

    schedule_job(job.id, user.id)
    return job_to_dict(job)


@router.get("/jobs/active")
async def list_active_jobs(user=Depends(get_verified_user)):
    """Everything still running for this user — the rehydrate call on mount."""
    jobs = await VideoJobs.get_active_jobs_by_user_id(user.id)
    return {"jobs": [job_to_dict(j) for j in jobs]}


@router.post("/jobs/by-ids")
async def get_jobs_by_ids(form_data: dict, user=Depends(get_verified_user)):
    """Re-read specific jobs, including finished ones.

    A composer that was closed across a job's completion needs terminal state
    too, which ``/jobs/active`` deliberately omits.
    """
    ids = [str(i) for i in (form_data.get("ids") or []) if i][:100]
    jobs = await VideoJobs.get_jobs_by_ids_and_user_id(ids, user.id)
    return {"jobs": [job_to_dict(j) for j in jobs]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, user=Depends(get_verified_user)):
    job = await VideoJobs.get_job_by_id_and_user_id(job_id, user.id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Video job not found."
        )
    return job_to_dict(job)


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str, user=Depends(get_verified_user)):
    job = await VideoJobs.get_job_by_id_and_user_id(job_id, user.id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Video job not found."
        )
    if job.status in TERMINAL_STATUSES:
        return job_to_dict(job)

    await cancel_job(job_id)
    # The worker writes CANCELED on its way out, but a job that was queued and
    # never picked up has no worker to do that — mark it here so the row can
    # never be left stuck in a non-terminal state.
    updated = await VideoJobs.get_job_by_id(job_id)
    if updated and updated.status not in TERMINAL_STATUSES:
        updated = await VideoJobs.mark_terminal(
            job_id, STATUS_CANCELED, error="Canceled."
        )
        if updated:
            await broadcast_job(user.id, updated)
    return job_to_dict(updated or job)


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, user=Depends(get_verified_user)):
    await cancel_job(job_id)
    ok = await VideoJobs.delete_job(job_id, user.id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Video job not found."
        )
    return {"ok": True}
