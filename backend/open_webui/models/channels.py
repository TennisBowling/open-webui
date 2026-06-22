import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB

from open_webui.internal.db import Base, get_db
from open_webui.models.groups import Groups
from open_webui.utils.access_control import has_access


class Channel(Base):
    __tablename__ = "channel"

    id = Column(Text, primary_key=True)
    user_id = Column(Text)
    type = Column(Text, nullable=True)
    name = Column(Text)
    description = Column(Text, nullable=True)
    data = Column(JSONB, nullable=True)
    meta = Column(JSONB, nullable=True)
    access_control = Column(JSONB, nullable=True)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class ChannelModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_id: str
    type: Optional[str] = None
    name: str
    description: Optional[str] = None
    data: Optional[dict] = None
    meta: Optional[dict] = None
    access_control: Optional[dict] = None
    created_at: int
    updated_at: int


class ChannelResponse(ChannelModel):
    write_access: bool = False


class ChannelForm(BaseModel):
    name: str
    description: Optional[str] = None
    data: Optional[dict] = None
    meta: Optional[dict] = None
    access_control: Optional[dict] = None


class ChannelTable:
    async def insert_new_channel(
        self, type: Optional[str], form_data: ChannelForm, user_id: str
    ) -> Optional[ChannelModel]:
        async with get_db() as db:
            now = int(time.time_ns())
            row = Channel(
                **form_data.model_dump(),
                type=type,
                name=form_data.name.lower(),
                id=str(uuid.uuid4()),
                user_id=user_id,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return ChannelModel.model_validate(row)

    async def get_channels(self) -> list[ChannelModel]:
        async with get_db() as db:
            rows = (await db.execute(select(Channel))).scalars().all()
            return [ChannelModel.model_validate(row) for row in rows]

    async def get_channels_by_user_id(
        self, user_id: str, permission: str = "read"
    ) -> list[ChannelModel]:
        channels = await self.get_channels()
        user_group_ids = {group.id for group in await Groups.get_groups_by_member_id(user_id)}
        return [
            channel
            for channel in channels
            if channel.user_id == user_id
            or has_access(user_id, permission, channel.access_control, user_group_ids)
        ]

    async def get_channel_by_id(self, id: str) -> Optional[ChannelModel]:
        async with get_db() as db:
            row = await db.get(Channel, id)
            return ChannelModel.model_validate(row) if row else None

    async def update_channel_by_id(
        self, id: str, form_data: ChannelForm
    ) -> Optional[ChannelModel]:
        async with get_db() as db:
            row = await db.get(Channel, id)
            if not row:
                return None
            row.name = form_data.name
            row.description = form_data.description
            row.data = form_data.data
            row.meta = form_data.meta
            row.access_control = form_data.access_control
            row.updated_at = int(time.time_ns())
            await db.commit()
            await db.refresh(row)
            return ChannelModel.model_validate(row)

    async def delete_channel_by_id(self, id: str):
        async with get_db() as db:
            await db.execute(delete(Channel).where(Channel.id == id))
            await db.commit()
            return True


Channels = ChannelTable()
