import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Text, delete, select, update
from sqlalchemy.dialects.postgresql import JSONB

from open_webui.env import SRC_LOG_LEVELS
from open_webui.internal.db import Base, get_db

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


class Feedback(Base):
    __tablename__ = "feedback"
    id = Column(Text, primary_key=True)
    user_id = Column(Text)
    version = Column(BigInteger, default=0)
    type = Column(Text)
    data = Column(JSONB, nullable=True)
    meta = Column(JSONB, nullable=True)
    snapshot = Column(JSONB, nullable=True)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class FeedbackModel(BaseModel):
    id: str
    user_id: str
    version: int
    type: str
    data: Optional[dict] = None
    meta: Optional[dict] = None
    snapshot: Optional[dict] = None
    created_at: int
    updated_at: int
    model_config = ConfigDict(from_attributes=True)


class FeedbackResponse(BaseModel):
    id: str
    user_id: str
    version: int
    type: str
    data: Optional[dict] = None
    meta: Optional[dict] = None
    created_at: int
    updated_at: int


class RatingData(BaseModel):
    rating: Optional[str | int] = None
    model_id: Optional[str] = None
    sibling_model_ids: Optional[list[str]] = None
    reason: Optional[str] = None
    comment: Optional[str] = None
    model_config = ConfigDict(extra="allow", protected_namespaces=())


class MetaData(BaseModel):
    arena: Optional[bool] = None
    chat_id: Optional[str] = None
    message_id: Optional[str] = None
    tags: Optional[list[str]] = None
    model_config = ConfigDict(extra="allow")


class SnapshotData(BaseModel):
    chat: Optional[dict] = None
    model_config = ConfigDict(extra="allow")


class FeedbackForm(BaseModel):
    type: str
    data: Optional[RatingData] = None
    meta: Optional[dict] = None
    snapshot: Optional[SnapshotData] = None
    model_config = ConfigDict(extra="allow")


class FeedbackTable:
    async def insert_new_feedback(
        self, user_id: str, form_data: FeedbackForm
    ) -> Optional[FeedbackModel]:
        now = int(time.time())
        row = Feedback(
            id=str(uuid.uuid4()),
            user_id=user_id,
            version=0,
            **form_data.model_dump(),
            created_at=now,
            updated_at=now,
        )
        try:
            async with get_db() as db:
                db.add(row)
                await db.commit()
                await db.refresh(row)
                return FeedbackModel.model_validate(row)
        except Exception as e:
            log.exception(f"Error creating a new feedback: {e}")
            return None

    async def get_feedback_by_id(self, id: str) -> Optional[FeedbackModel]:
        async with get_db() as db:
            row = await db.get(Feedback, id)
            return FeedbackModel.model_validate(row) if row else None

    async def get_feedback_by_id_and_user_id(
        self, id: str, user_id: str
    ) -> Optional[FeedbackModel]:
        async with get_db() as db:
            row = (
                await db.execute(select(Feedback).where(Feedback.id == id, Feedback.user_id == user_id))
            ).scalars().first()
            return FeedbackModel.model_validate(row) if row else None

    async def get_all_feedbacks(self) -> list[FeedbackModel]:
        async with get_db() as db:
            rows = (
                await db.execute(select(Feedback).order_by(Feedback.updated_at.desc()))
            ).scalars().all()
            return [FeedbackModel.model_validate(row) for row in rows]

    async def get_feedbacks_by_type(self, type: str) -> list[FeedbackModel]:
        async with get_db() as db:
            rows = (
                await db.execute(
                    select(Feedback).where(Feedback.type == type).order_by(Feedback.updated_at.desc())
                )
            ).scalars().all()
            return [FeedbackModel.model_validate(row) for row in rows]

    async def get_feedbacks_by_user_id(self, user_id: str) -> list[FeedbackModel]:
        async with get_db() as db:
            rows = (
                await db.execute(
                    select(Feedback).where(Feedback.user_id == user_id).order_by(Feedback.updated_at.desc())
                )
            ).scalars().all()
            return [FeedbackModel.model_validate(row) for row in rows]

    async def update_feedback_by_id(
        self, id: str, form_data: FeedbackForm, user_id: Optional[str] = None
    ) -> Optional[FeedbackModel]:
        values = {"updated_at": int(time.time())}
        if form_data.data:
            values["data"] = form_data.data.model_dump()
        if form_data.meta:
            values["meta"] = form_data.meta
        if form_data.snapshot:
            values["snapshot"] = form_data.snapshot.model_dump()
        async with get_db() as db:
            criteria = [Feedback.id == id]
            if user_id is not None:
                criteria.append(Feedback.user_id == user_id)
            row = (
                await db.execute(
                    update(Feedback).where(*criteria).values(**values).returning(Feedback)
                )
            ).scalars().first()
            await db.commit()
            return FeedbackModel.model_validate(row) if row else None

    async def update_feedback_by_id_and_user_id(
        self, id: str, user_id: str, form_data: FeedbackForm
    ) -> Optional[FeedbackModel]:
        return await self.update_feedback_by_id(id, form_data, user_id)

    async def delete_feedback_by_id(self, id: str, user_id: Optional[str] = None) -> bool:
        async with get_db() as db:
            criteria = [Feedback.id == id]
            if user_id is not None:
                criteria.append(Feedback.user_id == user_id)
            result = await db.execute(delete(Feedback).where(*criteria))
            await db.commit()
            return result.rowcount > 0

    async def delete_feedback_by_id_and_user_id(self, id: str, user_id: str) -> bool:
        return await self.delete_feedback_by_id(id, user_id)

    async def delete_feedbacks_by_user_id(self, user_id: str) -> bool:
        async with get_db() as db:
            result = await db.execute(delete(Feedback).where(Feedback.user_id == user_id))
            await db.commit()
            return result.rowcount > 0

    async def delete_all_feedbacks(self) -> bool:
        async with get_db() as db:
            result = await db.execute(delete(Feedback))
            await db.commit()
            return result.rowcount > 0


Feedbacks = FeedbackTable()
