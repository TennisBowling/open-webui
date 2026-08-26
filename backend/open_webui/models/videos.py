"""Durable video-ingest job state.

A video attachment is produced by a background pipeline (resolve → download →
probe → process → store) that can easily outlive the tab that started it. The
row is the single source of truth for that pipeline: socket events are a live
*optimization*, and every client re-derives the same state by reading these rows
on mount. That is what makes "close the tab / switch devices / come back later"
work without a special resume path — there is nothing client-side to lose.

``status`` is the coarse lifecycle (is this job still going?) and ``stage`` is
the fine-grained, user-visible step. They are kept separate so the UI can show
"Downloading…" while the scheduler only cares that the job is ``running``.
"""

import logging
import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Index, String, Text, select, update
from sqlalchemy.dialects.postgresql import JSONB

from open_webui.env import SRC_LOG_LEVELS
from open_webui.internal.db import Base, get_db

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


# Coarse lifecycle.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"

TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED}
ACTIVE_STATUSES = {STATUS_QUEUED, STATUS_RUNNING}

# Fine-grained, user-visible stages (ordered).
STAGE_QUEUED = "queued"
STAGE_RESOLVING = "resolving"
STAGE_DOWNLOADING = "downloading"
STAGE_FALLBACK = "fallback"
STAGE_PROBING = "probing"
STAGE_PROCESSING = "processing"
STAGE_STORING = "storing"
STAGE_DONE = "done"


class VideoJob(Base):
    __tablename__ = "video_job"

    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    # Null while the message is still being composed in a brand-new chat, so the
    # job is owned by the user rather than by a chat that may never be created.
    chat_id = Column(String, nullable=True)

    status = Column(String, nullable=False, default=STATUS_QUEUED)
    stage = Column(String, nullable=False, default=STAGE_QUEUED)

    source_type = Column(String, nullable=False)  # "url" | "upload"
    source_url = Column(Text, nullable=True)
    source_file_id = Column(String, nullable=True)

    title = Column(Text, nullable=True)
    params = Column(JSONB, nullable=True)
    progress = Column(JSONB, nullable=True)
    result = Column(JSONB, nullable=True)
    error = Column(Text, nullable=True)

    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("video_job_user_id_idx", "user_id"),
        # The composer's rehydrate query: "my jobs that are still going".
        Index("video_job_user_status_idx", "user_id", "status"),
    )


class VideoJobModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    chat_id: Optional[str] = None
    status: str
    stage: str
    source_type: str
    source_url: Optional[str] = None
    source_file_id: Optional[str] = None
    title: Optional[str] = None
    params: Optional[dict] = None
    progress: Optional[dict] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: int
    updated_at: int


class VideoJobsTable:
    async def insert_new_job(
        self,
        user_id: str,
        source_type: str,
        *,
        chat_id: Optional[str] = None,
        source_url: Optional[str] = None,
        source_file_id: Optional[str] = None,
        title: Optional[str] = None,
        params: Optional[dict] = None,
    ) -> Optional[VideoJobModel]:
        now = int(time.time())
        async with get_db() as db:
            try:
                row = VideoJob(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    chat_id=chat_id,
                    status=STATUS_QUEUED,
                    stage=STAGE_QUEUED,
                    source_type=source_type,
                    source_url=source_url,
                    source_file_id=source_file_id,
                    title=title,
                    params=params or {},
                    progress={"percent": 0},
                    result=None,
                    error=None,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                await db.commit()
                await db.refresh(row)
                return VideoJobModel.model_validate(row)
            except Exception:
                log.exception("Failed to insert video job for user %s", user_id)
                await db.rollback()
                return None

    async def get_job_by_id(self, job_id: str) -> Optional[VideoJobModel]:
        async with get_db() as db:
            try:
                row = await db.get(VideoJob, job_id)
                return VideoJobModel.model_validate(row) if row else None
            except Exception:
                return None

    async def get_job_by_id_and_user_id(
        self, job_id: str, user_id: str
    ) -> Optional[VideoJobModel]:
        async with get_db() as db:
            try:
                result = await db.execute(
                    select(VideoJob)
                    .where(VideoJob.id == job_id, VideoJob.user_id == user_id)
                    .limit(1)
                )
                row = result.scalars().first()
                return VideoJobModel.model_validate(row) if row else None
            except Exception:
                return None

    async def get_active_jobs_by_user_id(self, user_id: str) -> list[VideoJobModel]:
        """Jobs the composer must re-attach to after a reload / device switch."""
        async with get_db() as db:
            try:
                result = await db.execute(
                    select(VideoJob)
                    .where(
                        VideoJob.user_id == user_id,
                        VideoJob.status.in_(tuple(ACTIVE_STATUSES)),
                    )
                    .order_by(VideoJob.created_at.asc())
                )
                return [
                    VideoJobModel.model_validate(row) for row in result.scalars().all()
                ]
            except Exception:
                log.exception("Failed to list active video jobs for %s", user_id)
                return []

    async def get_jobs_by_ids_and_user_id(
        self, job_ids: list[str], user_id: str
    ) -> list[VideoJobModel]:
        if not job_ids:
            return []
        async with get_db() as db:
            try:
                result = await db.execute(
                    select(VideoJob).where(
                        VideoJob.id.in_(tuple(job_ids)),
                        VideoJob.user_id == user_id,
                    )
                )
                return [
                    VideoJobModel.model_validate(row) for row in result.scalars().all()
                ]
            except Exception:
                return []

    async def update_job(self, job_id: str, **fields: Any) -> Optional[VideoJobModel]:
        """Patch a job row. Unknown keys are ignored so callers can splat freely."""
        allowed = {
            "chat_id",
            "status",
            "stage",
            "title",
            "params",
            "progress",
            "result",
            "error",
            "source_file_id",
            "source_url",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        if not payload:
            return await self.get_job_by_id(job_id)
        payload["updated_at"] = int(time.time())

        async with get_db() as db:
            try:
                await db.execute(
                    update(VideoJob).where(VideoJob.id == job_id).values(**payload)
                )
                await db.commit()
                row = await db.get(VideoJob, job_id)
                return VideoJobModel.model_validate(row) if row else None
            except Exception:
                log.exception("Failed to update video job %s", job_id)
                await db.rollback()
                return None

    async def mark_terminal(
        self,
        job_id: str,
        status: str,
        *,
        error: Optional[str] = None,
        result: Optional[dict] = None,
    ) -> Optional[VideoJobModel]:
        fields: dict[str, Any] = {"status": status, "stage": STAGE_DONE}
        if error is not None:
            fields["error"] = error
        if result is not None:
            fields["result"] = result
        return await self.update_job(job_id, **fields)

    async def reclaim_stranded_jobs(self, max_age_seconds: int = 3600) -> int:
        """Fail jobs whose worker died (process restart) so the UI never shows a
        spinner that can never advance. Called once at startup."""
        cutoff = int(time.time()) - max_age_seconds
        async with get_db() as db:
            try:
                result = await db.execute(
                    update(VideoJob)
                    .where(
                        VideoJob.status.in_((STATUS_QUEUED, STATUS_RUNNING)),
                        VideoJob.updated_at < cutoff,
                    )
                    .values(
                        status=STATUS_FAILED,
                        stage=STAGE_DONE,
                        error="Processing was interrupted by a server restart.",
                        updated_at=int(time.time()),
                    )
                )
                await db.commit()
                return int(result.rowcount or 0)
            except Exception:
                log.exception("Failed to reclaim stranded video jobs")
                await db.rollback()
                return 0

    async def delete_job(self, job_id: str, user_id: str) -> bool:
        async with get_db() as db:
            try:
                row = await db.get(VideoJob, job_id)
                if not row or row.user_id != user_id:
                    return False
                await db.delete(row)
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                return False


VideoJobs = VideoJobsTable()
