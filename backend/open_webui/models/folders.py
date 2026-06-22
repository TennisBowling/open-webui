import logging
import re
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Boolean, Column, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB

from open_webui.env import SRC_LOG_LEVELS
from open_webui.internal.db import Base, get_db

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


class Folder(Base):
    __tablename__ = "folder"
    id = Column(Text, primary_key=True)
    parent_id = Column(Text, nullable=True)
    user_id = Column(Text)
    name = Column(Text)
    items = Column(JSONB, nullable=True)
    meta = Column(JSONB, nullable=True)
    data = Column(JSONB, nullable=True)
    is_expanded = Column(Boolean, default=False)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class FolderModel(BaseModel):
    id: str
    parent_id: Optional[str] = None
    user_id: str
    name: str
    items: Optional[dict] = None
    meta: Optional[dict] = None
    data: Optional[dict] = None
    is_expanded: bool = False
    created_at: int
    updated_at: int
    model_config = ConfigDict(from_attributes=True)


class FolderMetadataResponse(BaseModel):
    icon: Optional[str] = None


class FolderNameIdResponse(BaseModel):
    id: str
    name: str
    meta: Optional[FolderMetadataResponse] = None
    parent_id: Optional[str] = None
    is_expanded: bool = False
    created_at: int
    updated_at: int


class FolderForm(BaseModel):
    name: str
    data: Optional[dict] = None
    meta: Optional[dict] = None
    model_config = ConfigDict(extra="allow")


class FolderUpdateForm(BaseModel):
    name: Optional[str] = None
    data: Optional[dict] = None
    meta: Optional[dict] = None
    model_config = ConfigDict(extra="allow")


class FolderTable:
    async def insert_new_folder(
        self, user_id: str, form_data: FolderForm, parent_id: Optional[str] = None
    ) -> Optional[FolderModel]:
        async with get_db() as db:
            now = int(time.time())
            row = Folder(
                id=str(uuid.uuid4()),
                user_id=user_id,
                **(form_data.model_dump(exclude_unset=True) or {}),
                parent_id=parent_id,
                created_at=now,
                updated_at=now,
            )
            try:
                db.add(row)
                await db.commit()
                await db.refresh(row)
                return FolderModel.model_validate(row)
            except Exception as e:
                log.exception(f"Error inserting a new folder: {e}")
                return None

    async def get_folder_by_id_and_user_id(self, id: str, user_id: str) -> Optional[FolderModel]:
        async with get_db() as db:
            row = (
                await db.execute(select(Folder).where(Folder.id == id, Folder.user_id == user_id))
            ).scalars().first()
            return FolderModel.model_validate(row) if row else None

    async def _rows_by_user(self, user_id: str) -> list[Folder]:
        async with get_db() as db:
            return list((await db.execute(select(Folder).where(Folder.user_id == user_id))).scalars().all())

    async def get_children_folders_by_id_and_user_id(self, id: str, user_id: str) -> Optional[list[FolderModel]]:
        rows = await self._rows_by_user(user_id)
        if not any(row.id == id for row in rows):
            return None
        by_parent: dict[str | None, list[Folder]] = {}
        for row in rows:
            by_parent.setdefault(row.parent_id, []).append(row)
        out: list[FolderModel] = []

        def visit(parent_id: str):
            for child in by_parent.get(parent_id, []):
                visit(child.id)
                out.append(FolderModel.model_validate(child))

        visit(id)
        return out

    async def get_folders_by_user_id(self, user_id: str) -> list[FolderModel]:
        return [FolderModel.model_validate(row) for row in await self._rows_by_user(user_id)]

    async def get_folder_by_parent_id_and_user_id_and_name(
        self, parent_id: Optional[str], user_id: str, name: str
    ) -> Optional[FolderModel]:
        async with get_db() as db:
            row = (
                await db.execute(
                    select(Folder).where(
                        Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id,
                        Folder.user_id == user_id,
                        Folder.name.ilike(name),
                    )
                )
            ).scalars().first()
            return FolderModel.model_validate(row) if row else None

    async def get_folders_by_parent_id_and_user_id(self, parent_id: Optional[str], user_id: str) -> list[FolderModel]:
        async with get_db() as db:
            stmt = select(Folder).where(Folder.user_id == user_id)
            stmt = stmt.where(Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id)
            rows = (await db.execute(stmt)).scalars().all()
            return [FolderModel.model_validate(row) for row in rows]

    async def update_folder_parent_id_by_id_and_user_id(self, id: str, user_id: str, parent_id: str) -> Optional[FolderModel]:
        async with get_db() as db:
            row = (
                await db.execute(select(Folder).where(Folder.id == id, Folder.user_id == user_id))
            ).scalars().first()
            if not row:
                return None
            row.parent_id = parent_id
            row.updated_at = int(time.time())
            await db.commit()
            await db.refresh(row)
            return FolderModel.model_validate(row)

    async def update_folder_by_id_and_user_id(self, id: str, user_id: str, form_data: FolderUpdateForm) -> Optional[FolderModel]:
        async with get_db() as db:
            row = (
                await db.execute(select(Folder).where(Folder.id == id, Folder.user_id == user_id))
            ).scalars().first()
            if not row:
                return None
            data = form_data.model_dump(exclude_unset=True)
            if "name" in data:
                existing = (
                    await db.execute(
                        select(Folder).where(
                            Folder.name == data["name"],
                            Folder.parent_id == row.parent_id,
                            Folder.user_id == user_id,
                            Folder.id != id,
                        )
                    )
                ).scalars().first()
                if existing:
                    return None
                row.name = data["name"]
            if "data" in data:
                row.data = {**(row.data or {}), **(data["data"] or {})}
            if "meta" in data:
                row.meta = {**(row.meta or {}), **(data["meta"] or {})}
            row.updated_at = int(time.time())
            await db.commit()
            await db.refresh(row)
            return FolderModel.model_validate(row)

    async def update_folder_is_expanded_by_id_and_user_id(self, id: str, user_id: str, is_expanded: bool) -> Optional[FolderModel]:
        async with get_db() as db:
            row = (
                await db.execute(select(Folder).where(Folder.id == id, Folder.user_id == user_id))
            ).scalars().first()
            if not row:
                return None
            row.is_expanded = is_expanded
            row.updated_at = int(time.time())
            await db.commit()
            await db.refresh(row)
            return FolderModel.model_validate(row)

    async def delete_folder_by_id_and_user_id(self, id: str, user_id: str) -> list[str]:
        rows = await self._rows_by_user(user_id)
        if not any(row.id == id for row in rows):
            return []
        by_parent: dict[str | None, list[Folder]] = {}
        for row in rows:
            by_parent.setdefault(row.parent_id, []).append(row)
        ids: list[str] = [id]

        def visit(parent_id: str):
            for child in by_parent.get(parent_id, []):
                ids.append(child.id)
                visit(child.id)

        visit(id)
        async with get_db() as db:
            await db.execute(delete(Folder).where(Folder.id.in_(ids), Folder.user_id == user_id))
            await db.commit()
        return ids

    def normalize_folder_name(self, name: str) -> str:
        name = re.sub(r"[\s_]+", " ", name)
        return name.strip().lower()

    async def search_folders_by_names(self, user_id: str, queries: list[str]) -> list[FolderModel]:
        normalized_queries = [self.normalize_folder_name(q) for q in queries]
        if not normalized_queries:
            return []
        rows = await self._rows_by_user(user_id)
        by_parent: dict[str | None, list[Folder]] = {}
        for row in rows:
            by_parent.setdefault(row.parent_id, []).append(row)
        results = {}

        def include_subtree(row: Folder):
            results[row.id] = FolderModel.model_validate(row)
            for child in by_parent.get(row.id, []):
                include_subtree(child)

        for row in rows:
            if self.normalize_folder_name(row.name) in normalized_queries:
                include_subtree(row)
        return list(results.values())

    async def search_folders_by_name_contains(self, user_id: str, query: str) -> list[FolderModel]:
        normalized_query = self.normalize_folder_name(query)
        rows = await self._rows_by_user(user_id)
        return [
            FolderModel.model_validate(row)
            for row in rows
            if normalized_query in self.normalize_folder_name(row.name)
        ]


Folders = FolderTable()
