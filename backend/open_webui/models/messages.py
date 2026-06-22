import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB

from open_webui.internal.db import Base, get_db
from open_webui.models.users import UserNameResponse, Users


class MessageReaction(Base):
    __tablename__ = "message_reaction"
    id = Column(Text, primary_key=True)
    user_id = Column(Text)
    message_id = Column(Text)
    name = Column(Text)
    created_at = Column(BigInteger)


class MessageReactionModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_id: str
    message_id: str
    name: str
    created_at: int


class Message(Base):
    __tablename__ = "message"
    id = Column(Text, primary_key=True)
    user_id = Column(Text)
    channel_id = Column(Text, nullable=True)
    reply_to_id = Column(Text, nullable=True)
    parent_id = Column(Text, nullable=True)
    content = Column(Text)
    data = Column(JSONB, nullable=True)
    meta = Column(JSONB, nullable=True)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class MessageModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_id: str
    channel_id: Optional[str] = None
    reply_to_id: Optional[str] = None
    parent_id: Optional[str] = None
    content: str
    data: Optional[dict] = None
    meta: Optional[dict] = None
    created_at: int
    updated_at: int


class MessageForm(BaseModel):
    content: str
    reply_to_id: Optional[str] = None
    parent_id: Optional[str] = None
    data: Optional[dict] = None
    meta: Optional[dict] = None


class Reactions(BaseModel):
    name: str
    user_ids: list[str]
    count: int


class MessageUserResponse(MessageModel):
    user: Optional[UserNameResponse] = None


class MessageReplyToResponse(MessageUserResponse):
    reply_to_message: Optional[MessageUserResponse] = None


class MessageResponse(MessageReplyToResponse):
    latest_reply_at: Optional[int]
    reply_count: int
    reactions: list[Reactions]


class MessageTable:
    async def insert_new_message(
        self, form_data: MessageForm, channel_id: str, user_id: str
    ) -> Optional[MessageModel]:
        async with get_db() as db:
            ts = int(time.time_ns())
            row = Message(
                id=str(uuid.uuid4()),
                user_id=user_id,
                channel_id=channel_id,
                reply_to_id=form_data.reply_to_id,
                parent_id=form_data.parent_id,
                content=form_data.content,
                data=form_data.data,
                meta=form_data.meta,
                created_at=ts,
                updated_at=ts,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return MessageModel.model_validate(row)

    async def _reply_to_response(self, row: Message) -> MessageReplyToResponse:
        reply_to_message = (
            await self.get_message_by_id(row.reply_to_id) if row.reply_to_id else None
        )
        return MessageReplyToResponse.model_validate(
            {
                **MessageModel.model_validate(row).model_dump(),
                "reply_to_message": reply_to_message.model_dump()
                if reply_to_message
                else None,
            }
        )

    async def get_message_by_id(self, id: str) -> Optional[MessageResponse]:
        async with get_db() as db:
            row = await db.get(Message, id)
        if not row:
            return None
        reply_to_message = await self.get_message_by_id(row.reply_to_id) if row.reply_to_id else None
        reactions = await self.get_reactions_by_message_id(id)
        thread_replies = await self.get_thread_replies_by_message_id(id)
        user = await Users.get_user_by_id(row.user_id)
        return MessageResponse.model_validate(
            {
                **MessageModel.model_validate(row).model_dump(),
                "user": user.model_dump() if user else None,
                "reply_to_message": reply_to_message.model_dump() if reply_to_message else None,
                "latest_reply_at": thread_replies[0].created_at if thread_replies else None,
                "reply_count": len(thread_replies),
                "reactions": reactions,
            }
        )

    async def get_thread_replies_by_message_id(self, id: str) -> list[MessageReplyToResponse]:
        async with get_db() as db:
            rows = (
                await db.execute(
                    select(Message).where(Message.parent_id == id).order_by(Message.created_at.desc())
                )
            ).scalars().all()
        return [await self._reply_to_response(row) for row in rows]

    async def get_reply_user_ids_by_message_id(self, id: str) -> list[str]:
        async with get_db() as db:
            return list(
                (
                    await db.execute(select(Message.user_id).where(Message.parent_id == id))
                ).scalars().all()
            )

    async def get_messages_by_channel_id(
        self, channel_id: str, skip: int = 0, limit: int = 50
    ) -> list[MessageReplyToResponse]:
        async with get_db() as db:
            rows = (
                await db.execute(
                    select(Message)
                    .where(Message.channel_id == channel_id, Message.parent_id.is_(None))
                    .order_by(Message.created_at.desc())
                    .offset(skip)
                    .limit(limit)
                )
            ).scalars().all()
        return [await self._reply_to_response(row) for row in rows]

    async def get_messages_by_parent_id(
        self, channel_id: str, parent_id: str, skip: int = 0, limit: int = 50
    ) -> list[MessageReplyToResponse]:
        async with get_db() as db:
            parent = await db.get(Message, parent_id)
            if not parent:
                return []
            rows = (
                await db.execute(
                    select(Message)
                    .where(Message.channel_id == channel_id, Message.parent_id == parent_id)
                    .order_by(Message.created_at.desc())
                    .offset(skip)
                    .limit(limit)
                )
            ).scalars().all()
        if len(rows) < limit:
            rows.append(parent)
        return [await self._reply_to_response(row) for row in rows]

    async def update_message_by_id(
        self, id: str, form_data: MessageForm
    ) -> Optional[MessageModel]:
        async with get_db() as db:
            row = await db.get(Message, id)
            if not row:
                return None
            row.content = form_data.content
            row.data = {**(row.data or {}), **(form_data.data or {})}
            row.meta = {**(row.meta or {}), **(form_data.meta or {})}
            row.updated_at = int(time.time_ns())
            await db.commit()
            await db.refresh(row)
            return MessageModel.model_validate(row)

    async def add_reaction_to_message(
        self, id: str, user_id: str, name: str
    ) -> Optional[MessageReactionModel]:
        async with get_db() as db:
            row = MessageReaction(
                id=str(uuid.uuid4()),
                user_id=user_id,
                message_id=id,
                name=name,
                created_at=int(time.time_ns()),
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return MessageReactionModel.model_validate(row)

    async def get_reactions_by_message_id(self, id: str) -> list[Reactions]:
        async with get_db() as db:
            rows = (
                await db.execute(select(MessageReaction).where(MessageReaction.message_id == id))
            ).scalars().all()
        reactions = {}
        for reaction in rows:
            reactions.setdefault(reaction.name, {"name": reaction.name, "user_ids": [], "count": 0})
            reactions[reaction.name]["user_ids"].append(reaction.user_id)
            reactions[reaction.name]["count"] += 1
        return [Reactions(**reaction) for reaction in reactions.values()]

    async def remove_reaction_by_id_and_user_id_and_name(
        self, id: str, user_id: str, name: str
    ) -> bool:
        async with get_db() as db:
            await db.execute(
                delete(MessageReaction).where(
                    MessageReaction.message_id == id,
                    MessageReaction.user_id == user_id,
                    MessageReaction.name == name,
                )
            )
            await db.commit()
            return True

    async def delete_reactions_by_id(self, id: str) -> bool:
        async with get_db() as db:
            await db.execute(delete(MessageReaction).where(MessageReaction.message_id == id))
            await db.commit()
            return True

    async def delete_replies_by_id(self, id: str) -> bool:
        async with get_db() as db:
            await db.execute(delete(Message).where(Message.parent_id == id))
            await db.commit()
            return True

    async def delete_message_by_id(self, id: str) -> bool:
        async with get_db() as db:
            await db.execute(delete(Message).where(Message.id == id))
            await db.execute(delete(MessageReaction).where(MessageReaction.message_id == id))
            await db.commit()
            return True


Messages = MessageTable()
