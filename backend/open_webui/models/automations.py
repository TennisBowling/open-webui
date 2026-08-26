import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Index,
    String,
    Text,
    delete,
    func,
    select,
    text,
    update,
)

from open_webui.env import SRC_LOG_LEVELS
from open_webui.internal.db import Base, JSONField, get_db

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


class Automation(Base):
    """One user-owned scheduled task.

    ``rrule`` holds a bare iCal RRULE body (no ``RRULE:`` prefix, no DTSTART
    line — see utils/automation_schedule.py) and is NULL for a one-off, whose
    single firing time is ``dtstart``. All timestamps are epoch seconds UTC;
    ``timezone`` is the IANA zone the schedule's wall-clock times are expressed
    in, so a daily 8am automation stays 8am across DST.

    ``next_run_at`` is the scheduler's claim token as well as the due time: the
    sweeper advances it with a compare-and-set on the value it observed, which
    is what makes firing exactly-once across workers without a lock service.
    NULL means "nothing scheduled" (paused, or a completed one-off).
    """

    __tablename__ = "automation"

    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    title = Column(Text, nullable=False)
    prompt = Column(Text, nullable=False)
    rrule = Column(Text, nullable=True)
    dtstart = Column(BigInteger, nullable=False)
    timezone = Column(String, nullable=False)
    model_id = Column(String, nullable=False)
    tool_ids = Column(JSONField, nullable=True)
    features = Column(JSONField, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    next_run_at = Column(BigInteger, nullable=True)
    last_run_at = Column(BigInteger, nullable=True)
    last_run_status = Column(String, nullable=True)
    updated_at = Column(BigInteger, nullable=False)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("automation_user_id_idx", "user_id"),
        # The sweeper's every-30s "what is due?" lookup. Partial so the index
        # holds only armed automations — paused rows and completed one-offs
        # (both next_run_at IS NULL) never enter it.
        Index(
            "automation_due_idx",
            "next_run_at",
            postgresql_where=text("active AND next_run_at IS NOT NULL"),
        ),
    )


class AutomationRun(Base):
    """One firing of an automation.

    A dedicated table (not derived from the hidden chat) because status/error/
    preview/seen must survive the user deleting that chat, and because the
    stuck-run sweep needs an in-flight record to find after a worker dies
    mid-run.
    """

    __tablename__ = "automation_run"

    id = Column(String, primary_key=True)
    automation_id = Column(String, nullable=False)
    user_id = Column(String, nullable=False)
    chat_id = Column(String, nullable=True)
    status = Column(String, nullable=False)
    error = Column(Text, nullable=True)
    preview = Column(Text, nullable=True)
    seen = Column(Boolean, nullable=False, default=False)
    started_at = Column(BigInteger, nullable=False)
    ended_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("automation_run_automation_id_idx", "automation_id"),
        # Drives the sidebar's unread badge — partial so it only holds the
        # handful of runs the user hasn't looked at yet.
        Index(
            "automation_run_unseen_idx",
            "user_id",
            postgresql_where=text("seen = false"),
        ),
    )


class AutomationModel(BaseModel):
    id: str
    user_id: str
    title: str
    prompt: str
    rrule: Optional[str] = None
    dtstart: int
    timezone: str
    model_id: str
    tool_ids: list[str] = Field(default_factory=list)
    features: dict = Field(default_factory=dict)
    active: bool = True
    next_run_at: Optional[int] = None
    last_run_at: Optional[int] = None
    last_run_status: Optional[str] = None
    updated_at: int
    created_at: int

    model_config = ConfigDict(from_attributes=True)


class AutomationRunModel(BaseModel):
    id: str
    automation_id: str
    user_id: str
    chat_id: Optional[str] = None
    status: str
    error: Optional[str] = None
    preview: Optional[str] = None
    seen: bool = False
    started_at: int
    ended_at: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


# Preview text stored on a run so the automations page and the notification can
# show what the model answered without opening (or hydrating) the hidden chat.
PREVIEW_MAX_LENGTH = 280


def _automation_from_row(row: Automation) -> AutomationModel:
    return AutomationModel.model_validate(
        {
            "id": row.id,
            "user_id": row.user_id,
            "title": row.title,
            "prompt": row.prompt,
            "rrule": row.rrule,
            "dtstart": row.dtstart,
            "timezone": row.timezone,
            "model_id": row.model_id,
            "tool_ids": row.tool_ids or [],
            "features": row.features or {},
            "active": bool(row.active),
            "next_run_at": row.next_run_at,
            "last_run_at": row.last_run_at,
            "last_run_status": row.last_run_status,
            "updated_at": row.updated_at,
            "created_at": row.created_at,
        }
    )


class AutomationsTable:
    async def insert_new_automation(
        self,
        user_id: str,
        *,
        title: str,
        prompt: str,
        rrule: Optional[str],
        dtstart: int,
        timezone: str,
        model_id: str,
        tool_ids: list[str],
        features: dict,
        next_run_at: Optional[int],
    ) -> Optional[AutomationModel]:
        now = int(time.time())
        try:
            async with get_db() as db:
                row = Automation(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    title=title,
                    prompt=prompt,
                    rrule=rrule,
                    dtstart=dtstart,
                    timezone=timezone,
                    model_id=model_id,
                    tool_ids=tool_ids,
                    features=features,
                    active=True,
                    next_run_at=next_run_at,
                    updated_at=now,
                    created_at=now,
                )
                db.add(row)
                await db.commit()
                await db.refresh(row)
                return _automation_from_row(row)
        except Exception:
            log.exception("Error creating automation")
            return None

    async def get_automation_by_id_and_user_id(
        self, id: str, user_id: str
    ) -> Optional[AutomationModel]:
        try:
            async with get_db() as db:
                row = await db.get(Automation, id)
                if not row or row.user_id != user_id:
                    return None
                return _automation_from_row(row)
        except Exception:
            log.exception("Error getting automation")
            return None

    async def get_automations_by_user_id(self, user_id: str) -> list[AutomationModel]:
        async with get_db() as db:
            rows = (
                await db.execute(
                    select(Automation)
                    .where(Automation.user_id == user_id)
                    .order_by(Automation.created_at.desc())
                )
            ).scalars().all()
            return [_automation_from_row(row) for row in rows]

    async def count_active_by_user_id(self, user_id: str) -> int:
        """Automations that occupy a slot against the per-user cap.

        Counts only ARMED rows (active with a scheduled next run). A completed
        one-off is deactivated when it fires, so a user who schedules ten
        reminders over a week doesn't slowly lock themselves out of creating
        new ones.
        """
        async with get_db() as db:
            return int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(Automation)
                        .where(
                            Automation.user_id == user_id,
                            Automation.active == True,
                            Automation.next_run_at.is_not(None),
                        )
                    )
                ).scalar()
                or 0
            )

    async def update_automation_by_id_and_user_id(
        self, id: str, user_id: str, updated: dict
    ) -> Optional[AutomationModel]:
        try:
            async with get_db() as db:
                row = (
                    await db.execute(
                        update(Automation)
                        .where(Automation.id == id, Automation.user_id == user_id)
                        .values({**updated, "updated_at": int(time.time())})
                        .returning(Automation)
                    )
                ).scalars().first()
                await db.commit()
                return _automation_from_row(row) if row else None
        except Exception:
            log.exception("Error updating automation")
            return None

    async def delete_automation_by_id_and_user_id(self, id: str, user_id: str) -> bool:
        try:
            async with get_db() as db:
                result = await db.execute(
                    delete(Automation).where(
                        Automation.id == id, Automation.user_id == user_id
                    )
                )
                # Run history goes with the automation; the hidden chats it
                # produced deliberately do NOT — those are the user's chats and
                # stay openable from history/search after the schedule is gone.
                await db.execute(
                    delete(AutomationRun).where(AutomationRun.automation_id == id)
                )
                await db.commit()
                return result.rowcount > 0
        except Exception:
            log.exception("Error deleting automation")
            return False

    async def get_due_automations(self, now: int, limit: int = 20) -> list[AutomationModel]:
        async with get_db() as db:
            rows = (
                await db.execute(
                    select(Automation)
                    .where(
                        Automation.active == True,
                        Automation.next_run_at.is_not(None),
                        Automation.next_run_at <= now,
                    )
                    .order_by(Automation.next_run_at.asc())
                    .limit(limit)
                )
            ).scalars().all()
            return [_automation_from_row(row) for row in rows]

    async def claim_due(
        self, id: str, observed: int, new_next: Optional[int]
    ) -> bool:
        """Claim a due automation and advance it in ONE statement.

        The WHERE clause pins the ``next_run_at`` the sweeper observed, so two
        workers looking at the same row race on the UPDATE and exactly one sees
        ``rowcount == 1``; the loser simply skips it. That makes firing
        exactly-once without Redis, SKIP LOCKED, or a leader election.
        """
        try:
            now = int(time.time())
            async with get_db() as db:
                result = await db.execute(
                    update(Automation)
                    .where(
                        Automation.id == id,
                        Automation.active == True,
                        Automation.next_run_at == observed,
                    )
                    .values(next_run_at=new_next, last_run_at=now, updated_at=now)
                )
                await db.commit()
                return result.rowcount == 1
        except Exception:
            log.exception("Error claiming due automation %s", id)
            return False

    async def set_last_run_status(
        self, id: str, status: str, *, deactivate: bool = False
    ) -> None:
        values: dict = {"last_run_status": status, "updated_at": int(time.time())}
        if deactivate:
            values["active"] = False
            values["next_run_at"] = None
        try:
            async with get_db() as db:
                await db.execute(
                    update(Automation).where(Automation.id == id).values(values)
                )
                await db.commit()
        except Exception:
            log.exception("Error setting automation last-run status for %s", id)


class AutomationRunsTable:
    async def create_run(
        self,
        automation_id: str,
        user_id: str,
        status: str = "running",
    ) -> Optional[AutomationRunModel]:
        now = int(time.time())
        try:
            async with get_db() as db:
                row = AutomationRun(
                    id=str(uuid.uuid4()),
                    automation_id=automation_id,
                    user_id=user_id,
                    status=status,
                    seen=False,
                    started_at=now,
                    ended_at=None if status == "running" else now,
                )
                db.add(row)
                await db.commit()
                await db.refresh(row)
                return AutomationRunModel.model_validate(row)
        except Exception:
            log.exception("Error creating automation run for %s", automation_id)
            return None

    async def set_chat_id(self, id: str, chat_id: str) -> None:
        try:
            async with get_db() as db:
                await db.execute(
                    update(AutomationRun)
                    .where(AutomationRun.id == id)
                    .values(chat_id=chat_id)
                )
                await db.commit()
        except Exception:
            log.exception("Error setting automation run chat id for %s", id)

    async def finalize_run(
        self,
        id: str,
        status: str,
        *,
        error: Optional[str] = None,
        preview: Optional[str] = None,
    ) -> Optional[AutomationRunModel]:
        try:
            async with get_db() as db:
                row = (
                    await db.execute(
                        update(AutomationRun)
                        .where(AutomationRun.id == id)
                        .values(
                            status=status,
                            error=error,
                            preview=preview[:PREVIEW_MAX_LENGTH] if preview else None,
                            ended_at=int(time.time()),
                        )
                        .returning(AutomationRun)
                    )
                ).scalars().first()
                await db.commit()
                return AutomationRunModel.model_validate(row) if row else None
        except Exception:
            log.exception("Error finalizing automation run %s", id)
            return None

    async def get_runs_by_automation_id(
        self, automation_id: str, user_id: str, limit: int = 30
    ) -> list[AutomationRunModel]:
        async with get_db() as db:
            rows = (
                await db.execute(
                    select(AutomationRun)
                    .where(
                        AutomationRun.automation_id == automation_id,
                        AutomationRun.user_id == user_id,
                    )
                    .order_by(AutomationRun.started_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [AutomationRunModel.model_validate(row) for row in rows]

    async def count_unseen(self, user_id: str) -> int:
        async with get_db() as db:
            return int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(AutomationRun)
                        .where(
                            AutomationRun.user_id == user_id,
                            AutomationRun.seen == False,
                        )
                    )
                ).scalar()
                or 0
            )

    async def mark_all_seen(self, user_id: str) -> None:
        try:
            async with get_db() as db:
                await db.execute(
                    update(AutomationRun)
                    .where(AutomationRun.user_id == user_id, AutomationRun.seen == False)
                    .values(seen=True)
                )
                await db.commit()
        except Exception:
            log.exception("Error marking automation runs seen for %s", user_id)

    async def sweep_stuck_runs(self, cutoff: int) -> int:
        """Fail runs left ``running`` by a worker that died mid-flight.

        The in-process firing task is the only thing that would ever finalize
        them, so after a restart nothing else will — without this they'd spin
        forever in the UI. ``cutoff`` is the oldest ``started_at`` a genuinely
        live run could still have (run timeout + grace).
        """
        try:
            async with get_db() as db:
                result = await db.execute(
                    update(AutomationRun)
                    .where(
                        AutomationRun.status == "running",
                        AutomationRun.started_at < cutoff,
                    )
                    .values(
                        status="error",
                        error="The worker running this automation died mid-run.",
                        ended_at=int(time.time()),
                    )
                )
                await db.commit()
                return result.rowcount
        except Exception:
            log.exception("Error sweeping stuck automation runs")
            return 0


Automations = AutomationsTable()
AutomationRuns = AutomationRunsTable()
