import logging
import time
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Boolean, Column, Text, delete, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert

from open_webui.env import SRC_LOG_LEVELS
from open_webui.internal.db import Base, JSONField, get_db
from open_webui.models.groups import Groups
from open_webui.models.users import UserResponse, Users
from open_webui.utils.access_control import has_access

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


class ModelParams(BaseModel):
    model_config = ConfigDict(extra="allow")


class ModelMeta(BaseModel):
    profile_image_url: Optional[str] = "/static/favicon.png"
    description: Optional[str] = None
    cache_control_ephemeral: Optional[bool] = True
    capabilities: Optional[dict] = None
    model_config = ConfigDict(extra="allow")


class Model(Base):
    __tablename__ = "model"

    id = Column(Text, primary_key=True)
    user_id = Column(Text)
    base_model_id = Column(Text, nullable=True)
    name = Column(Text)
    params = Column(JSONField)
    meta = Column(JSONField)
    access_control = Column(JSONB, nullable=True)
    is_active = Column(Boolean, default=True)
    updated_at = Column(BigInteger)
    created_at = Column(BigInteger)


class ModelModel(BaseModel):
    id: str
    user_id: str
    base_model_id: Optional[str] = None
    name: str
    params: ModelParams
    meta: ModelMeta
    access_control: Optional[dict] = None
    is_active: bool
    updated_at: int
    created_at: int
    model_config = ConfigDict(from_attributes=True)


class ModelUserResponse(ModelModel):
    user: Optional[UserResponse] = None


class ModelResponse(ModelModel):
    pass


class ModelForm(BaseModel):
    id: str
    base_model_id: Optional[str] = None
    name: str
    meta: ModelMeta
    params: ModelParams
    access_control: Optional[dict] = None
    is_active: bool = True


class ModelsTable:
    async def insert_new_model(
        self, form_data: ModelForm, user_id: str
    ) -> Optional[ModelModel]:
        now = int(time.time())
        row = Model(
            **form_data.model_dump(),
            user_id=user_id,
            created_at=now,
            updated_at=now,
        )
        try:
            async with get_db() as db:
                db.add(row)
                await db.commit()
                await db.refresh(row)
                return ModelModel.model_validate(row)
        except Exception as e:
            log.exception(f"Failed to insert a new model: {e}")
            return None

    async def get_all_models(self) -> list[ModelModel]:
        async with get_db() as db:
            rows = (await db.execute(select(Model))).scalars().all()
            return [ModelModel.model_validate(row) for row in rows]

    async def get_models_by_ids(self, ids: list[str]) -> dict[str, ModelModel]:
        if not ids:
            return {}
        try:
            async with get_db() as db:
                rows = (await db.execute(select(Model).where(Model.id.in_(ids)))).scalars().all()
                return {row.id: ModelModel.model_validate(row) for row in rows}
        except Exception as e:
            log.exception(f"Failed to get models by ids: {e}")
            return {}

    async def get_models(self) -> list[ModelUserResponse]:
        async with get_db() as db:
            rows = (
                await db.execute(select(Model).where(Model.base_model_id.is_not(None)))
            ).scalars().all()
        user_ids = list({row.user_id for row in rows})
        users = await Users.get_users_by_user_ids(user_ids) if user_ids else []
        users_dict = {user.id: user for user in users}
        return [
            ModelUserResponse.model_validate(
                {
                    **ModelModel.model_validate(row).model_dump(),
                    "user": users_dict[row.user_id].model_dump()
                    if row.user_id in users_dict
                    else None,
                }
            )
            for row in rows
        ]

    async def get_base_models(self) -> list[ModelModel]:
        async with get_db() as db:
            rows = (
                await db.execute(select(Model).where(Model.base_model_id.is_(None)))
            ).scalars().all()
            return [ModelModel.model_validate(row) for row in rows]

    async def get_models_by_user_id(
        self, user_id: str, permission: str = "write"
    ) -> list[ModelUserResponse]:
        models = await self.get_models()
        user_group_ids = {group.id for group in await Groups.get_groups_by_member_id(user_id)}
        return [
            model
            for model in models
            if model.user_id == user_id
            or has_access(user_id, permission, model.access_control, user_group_ids)
        ]

    async def get_model_by_id(self, id: str) -> Optional[ModelModel]:
        try:
            async with get_db() as db:
                row = await db.get(Model, id)
                return ModelModel.model_validate(row) if row else None
        except Exception:
            return None

    async def toggle_model_by_id(self, id: str) -> Optional[ModelModel]:
        try:
            async with get_db() as db:
                row = await db.get(Model, id)
                if not row:
                    return None
                row.is_active = not row.is_active
                row.updated_at = int(time.time())
                await db.commit()
                await db.refresh(row)
                return ModelModel.model_validate(row)
        except Exception:
            return None

    async def update_model_by_id(self, id: str, model: ModelForm) -> Optional[ModelModel]:
        try:
            async with get_db() as db:
                row = (
                    await db.execute(
                        update(Model)
                        .where(Model.id == id)
                        .values(**model.model_dump(exclude={"id"}), updated_at=int(time.time()))
                        .returning(Model)
                    )
                ).scalars().first()
                await db.commit()
                return ModelModel.model_validate(row) if row else None
        except Exception as e:
            log.exception(f"Failed to update the model by id {id}: {e}")
            return None

    async def delete_model_by_id(self, id: str) -> bool:
        try:
            async with get_db() as db:
                await db.execute(delete(Model).where(Model.id == id))
                await db.commit()
                return True
        except Exception:
            return False

    async def delete_all_models(self) -> bool:
        try:
            async with get_db() as db:
                await db.execute(delete(Model))
                await db.commit()
                return True
        except Exception:
            return False

    async def sync_models(self, user_id: str, models: list[ModelModel]) -> list[ModelModel]:
        try:
            now = int(time.time())
            rows = [
                {**model.model_dump(), "user_id": user_id, "updated_at": now}
                for model in models
            ]
            new_ids = [model.id for model in models]
            async with get_db() as db:
                if rows:
                    stmt = pg_insert(Model).values(rows)
                    await db.execute(
                        stmt.on_conflict_do_update(
                            index_elements=[Model.id],
                            set_={
                                "user_id": stmt.excluded.user_id,
                                "base_model_id": stmt.excluded.base_model_id,
                                "name": stmt.excluded.name,
                                "params": stmt.excluded.params,
                                "meta": stmt.excluded.meta,
                                "access_control": stmt.excluded.access_control,
                                "is_active": stmt.excluded.is_active,
                                "updated_at": stmt.excluded.updated_at,
                            },
                        )
                    )
                if new_ids:
                    await db.execute(delete(Model).where(Model.id.not_in(new_ids)))
                else:
                    await db.execute(delete(Model))
                await db.commit()
                all_rows = (await db.execute(select(Model))).scalars().all()
                return [ModelModel.model_validate(row) for row in all_rows]
        except Exception as e:
            log.exception(f"Error syncing models for user {user_id}: {e}")
            return []


Models = ModelsTable()
