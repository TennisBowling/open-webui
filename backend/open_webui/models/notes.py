import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB

from open_webui.internal.db import Base, get_db
from open_webui.models.groups import Groups
from open_webui.models.users import UserResponse
from open_webui.utils.access_control import has_access


class Note(Base):
    __tablename__ = "note"
    id = Column(Text, primary_key=True)
    user_id = Column(Text)
    title = Column(Text)
    data = Column(JSONB, nullable=True)
    meta = Column(JSONB, nullable=True)
    access_control = Column(JSONB, nullable=True)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class NoteModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_id: str
    title: str
    data: Optional[dict] = None
    meta: Optional[dict] = None
    access_control: Optional[dict] = None
    created_at: int
    updated_at: int


class NoteForm(BaseModel):
    title: str
    data: Optional[dict] = None
    meta: Optional[dict] = None
    access_control: Optional[dict] = None


class NoteUpdateForm(BaseModel):
    title: Optional[str] = None
    data: Optional[dict] = None
    meta: Optional[dict] = None
    access_control: Optional[dict] = None


class NoteUserResponse(NoteModel):
    user: Optional[UserResponse] = None


class NoteTable:
    async def insert_new_note(self, form_data: NoteForm, user_id: str) -> Optional[NoteModel]:
        async with get_db() as db:
            now = int(time.time_ns())
            row = Note(id=str(uuid.uuid4()), user_id=user_id, **form_data.model_dump(), created_at=now, updated_at=now)
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return NoteModel.model_validate(row)

    async def get_notes(self, skip: Optional[int] = None, limit: Optional[int] = None) -> list[NoteModel]:
        async with get_db() as db:
            stmt = select(Note).order_by(Note.updated_at.desc())
            if skip is not None:
                stmt = stmt.offset(skip)
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = (await db.execute(stmt)).scalars().all()
            return [NoteModel.model_validate(row) for row in rows]

    async def get_notes_by_user_id(
        self, user_id: str, skip: Optional[int] = None, limit: Optional[int] = None
    ) -> list[NoteModel]:
        async with get_db() as db:
            stmt = select(Note).where(Note.user_id == user_id).order_by(Note.updated_at.desc())
            if skip is not None:
                stmt = stmt.offset(skip)
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = (await db.execute(stmt)).scalars().all()
            return [NoteModel.model_validate(row) for row in rows]

    async def get_notes_by_permission(
        self,
        user_id: str,
        permission: str = "write",
        skip: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[NoteModel]:
        user_group_ids = {group.id for group in await Groups.get_groups_by_member_id(user_id)}
        async with get_db() as db:
            rows = (await db.execute(select(Note).order_by(Note.updated_at.desc()))).scalars().all()

        results: list[NoteModel] = []
        n_skipped = 0
        for row in rows:
            permitted = (
                row.user_id == user_id
                or (row.access_control is None and permission == "read")
                or has_access(user_id, permission, row.access_control, user_group_ids)
            )
            if not permitted:
                continue
            if skip and n_skipped < skip:
                n_skipped += 1
                continue
            results.append(NoteModel.model_validate(row))
            if limit is not None and len(results) >= limit:
                break
        return results

    async def get_note_by_id(self, id: str) -> Optional[NoteModel]:
        async with get_db() as db:
            row = await db.get(Note, id)
            return NoteModel.model_validate(row) if row else None

    async def update_note_by_id(self, id: str, form_data: NoteUpdateForm) -> Optional[NoteModel]:
        async with get_db() as db:
            row = await db.get(Note, id)
            if not row:
                return None
            data = form_data.model_dump(exclude_unset=True)
            if "title" in data:
                row.title = data["title"]
            if "data" in data:
                row.data = {**(row.data or {}), **(data["data"] or {})}
            if "meta" in data:
                row.meta = {**(row.meta or {}), **(data["meta"] or {})}
            if "access_control" in data:
                row.access_control = data["access_control"]
            row.updated_at = int(time.time_ns())
            await db.commit()
            await db.refresh(row)
            return NoteModel.model_validate(row)

    async def delete_note_by_id(self, id: str):
        async with get_db() as db:
            await db.execute(delete(Note).where(Note.id == id))
            await db.commit()
            return True


Notes = NoteTable()
