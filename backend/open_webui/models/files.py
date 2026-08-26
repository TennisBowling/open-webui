import logging
import json
import time
from typing import Optional

from open_webui.internal.db import Base, get_db, dumps_jsonb
from open_webui.env import SRC_LOG_LEVELS
from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Index, String, Text, delete, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

####################
# Files DB Schema
####################


class File(Base):
    __tablename__ = "file"
    id = Column(String, primary_key=True)
    user_id = Column(String)
    hash = Column(Text, nullable=True)

    filename = Column(Text)
    path = Column(Text, nullable=True)

    data = Column(JSONB, nullable=True)
    meta = Column(JSONB, nullable=True)

    access_control = Column(JSONB, nullable=True)

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)

    __table_args__ = (
        # WHERE user_id = ... (Files.get_files_by_user_id)
        Index("file_user_id_idx", "user_id"),
    )


class FileModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    hash: Optional[str] = None

    filename: str
    path: Optional[str] = None

    data: Optional[dict] = None
    meta: Optional[dict] = None

    access_control: Optional[dict] = None

    created_at: Optional[int]  # timestamp in epoch
    updated_at: Optional[int]  # timestamp in epoch


####################
# Forms
####################


class FileMeta(BaseModel):
    name: Optional[str] = None
    content_type: Optional[str] = None
    size: Optional[int] = None

    model_config = ConfigDict(extra="allow")


class FileModelResponse(BaseModel):
    id: str
    user_id: str
    hash: Optional[str] = None

    filename: str
    data: Optional[dict] = None
    meta: FileMeta

    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch

    model_config = ConfigDict(extra="allow")


class FileMetadataResponse(BaseModel):
    id: str
    hash: Optional[str] = None
    meta: dict
    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch


class FileForm(BaseModel):
    id: str
    hash: Optional[str] = None
    filename: str
    path: str
    data: dict = {}
    meta: dict = {}
    access_control: Optional[dict] = None


class FilesTable:
    async def insert_new_file(self, user_id: str, form_data: FileForm) -> Optional[FileModel]:
        async with get_db() as db:
            file = FileModel(
                **{
                    **form_data.model_dump(),
                    "user_id": user_id,
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                }
            )

            try:
                result = File(**file.model_dump())
                db.add(result)
                await db.commit()
                await db.refresh(result)
                if result:
                    return FileModel.model_validate(result)
                else:
                    return None
            except IntegrityError as e:
                # A concurrent request with the same (user_id, upload_id) won
                # the race on file_user_id_upload_id_unique_idx (see migration
                # 12dd367080c3). The other request's row is now committed —
                # look it up and return it instead of failing this one, so
                # the upload endpoint stays idempotent even under a genuine
                # near-simultaneous retry, not just a sequential one.
                await db.rollback()
                upload_id = (form_data.meta or {}).get("data", {}).get("upload_id")
                if upload_id:
                    existing = await self.get_file_by_user_id_and_upload_id(
                        user_id, upload_id
                    )
                    if existing:
                        # The upload handler detects that this returned id differs
                        # from form_data.id and deletes the losing storage object.
                        return existing
                log.exception(f"Error inserting a new file (integrity error): {e}")
                return None
            except Exception as e:
                log.exception(f"Error inserting a new file: {e}")
                return None

    async def get_file_by_id(self, id: str) -> Optional[FileModel]:
        async with get_db() as db:
            try:
                file = await db.get(File, id)
                return FileModel.model_validate(file)
            except Exception:
                return None

    async def get_file_by_id_and_user_id(self, id: str, user_id: str) -> Optional[FileModel]:
        async with get_db() as db:
            try:
                result = await db.execute(
                    select(File).where(File.id == id, File.user_id == user_id).limit(1)
                )
                file = result.scalars().first()
                if file:
                    return FileModel.model_validate(file)
                else:
                    return None
            except Exception:
                return None

    async def get_file_by_user_id_and_upload_id(
        self, user_id: str, upload_id: str
    ) -> Optional[FileModel]:
        """Idempotency lookup for client-retried uploads.

        The client tags every upload attempt with a stable `upload_id` (reused
        across retries of the same logical upload) stored at
        `meta.data.upload_id`. If a retry lands after the original request
        already succeeded server-side (but the response was lost, e.g. on
        flaky cellular), this lets the handler return the existing row instead
        of inserting a duplicate.
        """
        if not upload_id:
            return None
        async with get_db() as db:
            try:
                result = await db.execute(
                    select(File)
                    .where(
                        File.user_id == user_id,
                        File.meta["data"]["upload_id"].astext == upload_id,
                    )
                    .order_by(File.created_at.desc())
                    .limit(1)
                )
                file = result.scalars().first()
                if file:
                    return FileModel.model_validate(file)
                else:
                    return None
            except Exception:
                return None

    async def get_file_metadata_by_id(self, id: str) -> Optional[FileMetadataResponse]:
        async with get_db() as db:
            try:
                file = await db.get(File, id)
                return FileMetadataResponse(
                    id=file.id,
                    hash=file.hash,
                    meta=file.meta,
                    created_at=file.created_at,
                    updated_at=file.updated_at,
                )
            except Exception:
                return None

    async def get_files(self) -> list[FileModel]:
        async with get_db() as db:
            result = await db.execute(select(File))
            return [FileModel.model_validate(file) for file in result.scalars().all()]

    async def check_access_by_user_id(self, id, user_id, permission="write") -> bool:
        file = await self.get_file_by_id(id)
        if not file:
            return False
        if file.user_id == user_id:
            return True
        # Implement additional access control logic here as needed
        return False

    async def get_files_by_ids(self, ids: list[str]) -> list[FileModel]:
        async with get_db() as db:
            result = await db.execute(
                select(File).where(File.id.in_(ids)).order_by(File.updated_at.desc())
            )
            return [FileModel.model_validate(file) for file in result.scalars().all()]

    async def get_file_metadatas_by_ids(self, ids: list[str]) -> list[FileMetadataResponse]:
        async with get_db() as db:
            result = await db.execute(
                select(File.id, File.hash, File.meta, File.created_at, File.updated_at)
                .where(File.id.in_(ids))
                .order_by(File.updated_at.desc())
            )
            return [
                FileMetadataResponse(
                    id=row.id,
                    hash=row.hash,
                    meta=row.meta,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in result.all()
            ]

    async def get_files_by_user_id(self, user_id: str) -> list[FileModel]:
        async with get_db() as db:
            result = await db.execute(select(File).where(File.user_id == user_id))
            return [FileModel.model_validate(file) for file in result.scalars().all()]

    async def update_file_hash_by_id(self, id: str, hash: str) -> Optional[FileModel]:
        async with get_db() as db:
            try:
                file = await db.get(File, id)
                file.hash = hash
                file.updated_at = int(time.time())
                await db.commit()
                await db.refresh(file)

                return FileModel.model_validate(file)
            except Exception:
                return None

    async def update_file_data_by_id(self, id: str, data: dict) -> Optional[FileModel]:
        async with get_db() as db:
            try:
                row = await db.execute(
                    text(
                        'UPDATE "file" '
                        'SET data = COALESCE(data, \'{}\'::jsonb) || CAST(:payload AS jsonb), '
                        '    updated_at = :updated_at '
                        'WHERE id = :id '
                        'RETURNING id, user_id, hash, filename, path, data, meta, access_control, created_at, updated_at'
                    ),
                    {
                        "id": id,
                        "payload": dumps_jsonb(data),
                        "updated_at": int(time.time()),
                    },
                )
                await db.commit()
                file = row.mappings().first()
                return FileModel(**dict(file)) if file else None
            except Exception as e:

                return None

    async def update_file_metadata_by_id(self, id: str, meta: dict) -> Optional[FileModel]:
        async with get_db() as db:
            try:
                row = await db.execute(
                    text(
                        'UPDATE "file" '
                        'SET meta = COALESCE(meta, \'{}\'::jsonb) || CAST(:payload AS jsonb), '
                        '    updated_at = :updated_at '
                        'WHERE id = :id '
                        'RETURNING id, user_id, hash, filename, path, data, meta, access_control, created_at, updated_at'
                    ),
                    {
                        "id": id,
                        "payload": dumps_jsonb(meta),
                        "updated_at": int(time.time()),
                    },
                )
                await db.commit()
                file = row.mappings().first()
                return FileModel(**dict(file)) if file else None
            except Exception:
                return None

    async def delete_file_by_id(self, id: str) -> bool:
        async with get_db() as db:
            try:
                await db.execute(delete(File).where(File.id == id))
                await db.commit()

                return True
            except Exception:
                return False

    async def delete_all_files(self) -> bool:
        async with get_db() as db:
            try:
                await db.execute(delete(File))
                await db.commit()

                return True
            except Exception:
                return False


Files = FilesTable()
