import logging
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, Index, PrimaryKeyConstraint, String, delete, select
from sqlalchemy.dialects.postgresql import JSONB

from open_webui.env import SRC_LOG_LEVELS
from open_webui.internal.db import Base, get_db

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


class Tag(Base):
    __tablename__ = "tag"
    id = Column(String)
    name = Column(String)
    user_id = Column(String)
    meta = Column(JSONB, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("id", "user_id", name="pk_id_user_id"),
        Index("user_id_idx", "user_id"),
    )


class TagModel(BaseModel):
    id: str
    name: str
    user_id: str
    meta: Optional[dict] = None
    model_config = ConfigDict(from_attributes=True)


class TagChatIdForm(BaseModel):
    name: str
    chat_id: str


def _tag_id(name: str) -> str:
    return name.replace(" ", "_").lower()


class TagTable:
    async def insert_new_tag(self, name: str, user_id: str) -> Optional[TagModel]:
        async with get_db() as db:
            tag = Tag(id=_tag_id(name), user_id=user_id, name=name)
            try:
                db.add(tag)
                await db.commit()
                await db.refresh(tag)
                return TagModel.model_validate(tag)
            except Exception as e:
                log.exception(f"Error inserting a new tag: {e}")
                return None

    async def get_tag_by_name_and_user_id(
        self, name: str, user_id: str
    ) -> Optional[TagModel]:
        try:
            async with get_db() as db:
                tag = await db.get(Tag, {"id": _tag_id(name), "user_id": user_id})
                return TagModel.model_validate(tag) if tag else None
        except Exception:
            return None

    async def get_tags_by_user_id(self, user_id: str) -> list[TagModel]:
        async with get_db() as db:
            tags = (
                await db.execute(select(Tag).where(Tag.user_id == user_id))
            ).scalars().all()
            return [TagModel.model_validate(tag) for tag in tags]

    async def get_tags_by_ids_and_user_id(
        self, ids: list[str], user_id: str
    ) -> list[TagModel]:
        async with get_db() as db:
            tags = (
                await db.execute(select(Tag).where(Tag.id.in_(ids), Tag.user_id == user_id))
            ).scalars().all()
            return [TagModel.model_validate(tag) for tag in tags]

    async def delete_tag_by_name_and_user_id(self, name: str, user_id: str) -> bool:
        try:
            async with get_db() as db:
                await db.execute(delete(Tag).where(Tag.id == _tag_id(name), Tag.user_id == user_id))
                await db.commit()
                return True
        except Exception as e:
            log.error(f"delete_tag: {e}")
            return False


Tags = TagTable()
