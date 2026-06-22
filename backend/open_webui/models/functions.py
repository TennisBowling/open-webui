import logging
import time
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Boolean, Column, Index, String, Text, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from open_webui.env import SRC_LOG_LEVELS
from open_webui.internal.db import Base, JSONField, get_db
from open_webui.models.users import UserModel, Users

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


class Function(Base):
    __tablename__ = "function"

    id = Column(String, primary_key=True)
    user_id = Column(String)
    name = Column(Text)
    type = Column(Text)
    content = Column(Text)
    meta = Column(JSONField)
    valves = Column(JSONField)
    is_active = Column(Boolean)
    is_global = Column(Boolean)
    updated_at = Column(BigInteger)
    created_at = Column(BigInteger)

    __table_args__ = (Index("is_global_idx", "is_global"),)


class FunctionMeta(BaseModel):
    description: Optional[str] = None
    manifest: Optional[dict] = {}
    model_config = ConfigDict(extra="allow")


class FunctionModel(BaseModel):
    id: str
    user_id: str
    name: str
    type: str
    content: str
    meta: FunctionMeta
    is_active: bool = False
    is_global: bool = False
    updated_at: int
    created_at: int
    model_config = ConfigDict(from_attributes=True)


class FunctionWithValvesModel(FunctionModel):
    valves: Optional[dict] = None


class FunctionUserResponse(FunctionModel):
    user: Optional[UserModel] = None


class FunctionResponse(BaseModel):
    id: str
    user_id: str
    type: str
    name: str
    meta: FunctionMeta
    is_active: bool
    is_global: bool
    updated_at: int
    created_at: int


class FunctionForm(BaseModel):
    id: str
    name: str
    content: str
    meta: FunctionMeta


class FunctionValves(BaseModel):
    valves: Optional[dict] = None


class FunctionsTable:
    async def insert_new_function(
        self, user_id: str, type: str, form_data: FunctionForm
    ) -> Optional[FunctionModel]:
        now = int(time.time())
        row = Function(
            **form_data.model_dump(),
            user_id=user_id,
            type=type,
            is_active=False,
            is_global=False,
            updated_at=now,
            created_at=now,
        )
        try:
            async with get_db() as db:
                db.add(row)
                await db.commit()
                await db.refresh(row)
                return FunctionModel.model_validate(row)
        except Exception as e:
            log.exception(f"Error creating a new function: {e}")
            return None

    async def sync_functions(
        self, user_id: str, functions: list[FunctionWithValvesModel]
    ) -> list[FunctionWithValvesModel]:
        try:
            now = int(time.time())
            rows = [
                {**func.model_dump(), "user_id": user_id, "updated_at": now}
                for func in functions
            ]
            new_ids = [func.id for func in functions]
            async with get_db() as db:
                if rows:
                    stmt = pg_insert(Function).values(rows)
                    await db.execute(
                        stmt.on_conflict_do_update(
                            index_elements=[Function.id],
                            set_={
                                "user_id": stmt.excluded.user_id,
                                "name": stmt.excluded.name,
                                "type": stmt.excluded.type,
                                "content": stmt.excluded.content,
                                "meta": stmt.excluded.meta,
                                "valves": stmt.excluded.valves,
                                "is_active": stmt.excluded.is_active,
                                "is_global": stmt.excluded.is_global,
                                "updated_at": stmt.excluded.updated_at,
                            },
                        )
                    )
                await db.execute(delete(Function).where(Function.id.not_in(new_ids)) if new_ids else delete(Function))
                await db.commit()
                all_rows = (await db.execute(select(Function))).scalars().all()
                return [FunctionWithValvesModel.model_validate(row) for row in all_rows]
        except Exception as e:
            log.exception(f"Error syncing functions for user {user_id}: {e}")
            return []

    async def get_function_by_id(self, id: str) -> Optional[FunctionModel]:
        try:
            async with get_db() as db:
                row = await db.get(Function, id)
                return FunctionModel.model_validate(row) if row else None
        except Exception:
            return None

    async def get_functions(
        self, active_only=False, include_valves=False
    ) -> list[FunctionModel | FunctionWithValvesModel]:
        async with get_db() as db:
            stmt = select(Function)
            if active_only:
                stmt = stmt.where(Function.is_active == True)
            rows = (await db.execute(stmt)).scalars().all()
            model = FunctionWithValvesModel if include_valves else FunctionModel
            return [model.model_validate(row) for row in rows]

    async def get_function_list(self) -> list[FunctionUserResponse]:
        async with get_db() as db:
            rows = (await db.execute(select(Function).order_by(Function.updated_at.desc()))).scalars().all()
        user_ids = list({row.user_id for row in rows})
        users = await Users.get_users_by_user_ids(user_ids) if user_ids else []
        users_dict = {user.id: user for user in users}
        return [
            FunctionUserResponse.model_validate(
                {
                    **FunctionModel.model_validate(row).model_dump(),
                    "user": users_dict[row.user_id].model_dump() if row.user_id in users_dict else None,
                }
            )
            for row in rows
        ]

    async def get_functions_by_type(self, type: str, active_only=False) -> list[FunctionModel]:
        async with get_db() as db:
            stmt = select(Function).where(Function.type == type)
            if active_only:
                stmt = stmt.where(Function.is_active == True)
            rows = (await db.execute(stmt)).scalars().all()
            return [FunctionModel.model_validate(row) for row in rows]

    async def get_global_filter_functions(self) -> list[FunctionModel]:
        async with get_db() as db:
            rows = (
                await db.execute(
                    select(Function).where(
                        Function.type == "filter",
                        Function.is_active == True,
                        Function.is_global == True,
                    )
                )
            ).scalars().all()
            return [FunctionModel.model_validate(row) for row in rows]

    async def get_global_action_functions(self) -> list[FunctionModel]:
        async with get_db() as db:
            rows = (
                await db.execute(
                    select(Function).where(
                        Function.type == "action",
                        Function.is_active == True,
                        Function.is_global == True,
                    )
                )
            ).scalars().all()
            return [FunctionModel.model_validate(row) for row in rows]

    async def get_function_valves_by_id(self, id: str) -> Optional[dict]:
        async with get_db() as db:
            row = await db.get(Function, id)
            return row.valves if row and row.valves else {}

    async def update_function_valves_by_id(
        self, id: str, valves: dict
    ) -> Optional[FunctionValves]:
        async with get_db() as db:
            row = await db.get(Function, id)
            if not row:
                return None
            row.valves = valves
            row.updated_at = int(time.time())
            await db.commit()
            await db.refresh(row)
            return FunctionWithValvesModel.model_validate(row)

    async def update_function_metadata_by_id(self, id: str, metadata: dict) -> Optional[FunctionModel]:
        async with get_db() as db:
            row = await db.get(Function, id)
            if not row:
                return None
            row.meta = {**(row.meta or {}), **metadata}
            row.updated_at = int(time.time())
            await db.commit()
            await db.refresh(row)
            return FunctionModel.model_validate(row)

    async def get_user_valves_by_id_and_user_id(self, id: str, user_id: str) -> Optional[dict]:
        try:
            user = await Users.get_user_by_id(user_id)
            user_settings = user.settings.model_dump() if user and user.settings else {}
            return user_settings.get("functions", {}).get("valves", {}).get(id, {})
        except Exception as e:
            log.exception(f"Error getting user values by id {id} and user id {user_id}")
            return None

    async def update_user_valves_by_id_and_user_id(
        self, id: str, user_id: str, valves: dict
    ) -> Optional[dict]:
        try:
            user = await Users.get_user_by_id(user_id)
            user_settings = user.settings.model_dump() if user and user.settings else {}
            user_settings.setdefault("functions", {}).setdefault("valves", {})[id] = valves
            await Users.update_user_by_id(user_id, {"settings": user_settings})
            return user_settings["functions"]["valves"][id]
        except Exception as e:
            log.exception(f"Error updating user valves by id {id} and user_id {user_id}: {e}")
            return None

    async def update_function_by_id(self, id: str, updated: dict) -> Optional[FunctionModel]:
        async with get_db() as db:
            row = (
                await db.execute(
                    update(Function)
                    .where(Function.id == id)
                    .values(**updated, updated_at=int(time.time()))
                    .returning(Function)
                )
            ).scalars().first()
            await db.commit()
            return FunctionModel.model_validate(row) if row else None

    async def deactivate_all_functions(self) -> Optional[bool]:
        async with get_db() as db:
            await db.execute(update(Function).values(is_active=False, updated_at=int(time.time())))
            await db.commit()
            return True

    async def delete_function_by_id(self, id: str) -> bool:
        async with get_db() as db:
            result = await db.execute(delete(Function).where(Function.id == id))
            await db.commit()
            return result.rowcount > 0


Functions = FunctionsTable()
