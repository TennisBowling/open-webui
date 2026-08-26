import logging
import time
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, String, Text, delete, select, update
from sqlalchemy.dialects.postgresql import JSONB

from open_webui.env import SRC_LOG_LEVELS
from open_webui.internal.db import Base, JSONField, get_db
from open_webui.models.groups import Groups
from open_webui.models.users import UserResponse, Users
from open_webui.utils.access_control import has_access

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


class Tool(Base):
    __tablename__ = "tool"

    id = Column(String, primary_key=True)
    user_id = Column(String)
    name = Column(Text)
    content = Column(Text)
    specs = Column(JSONField)
    meta = Column(JSONField)
    valves = Column(JSONField)
    access_control = Column(JSONB, nullable=True)
    updated_at = Column(BigInteger)
    created_at = Column(BigInteger)


class ToolMeta(BaseModel):
    description: Optional[str] = None
    manifest: Optional[dict] = {}
    parallelizable: Optional[bool] = False
    # Optional user-set icon (data URL or absolute URL) rendered wherever the
    # tool surfaces in the UI. Declared explicitly because this model drops
    # unknown keys.
    icon: Optional[str] = None


class ToolModel(BaseModel):
    id: str
    user_id: str
    name: str
    content: str
    specs: list[dict]
    meta: ToolMeta
    access_control: Optional[dict] = None
    updated_at: int
    created_at: int
    model_config = ConfigDict(from_attributes=True)


class ToolUserModel(ToolModel):
    user: Optional[UserResponse] = None


class ToolResponse(BaseModel):
    id: str
    user_id: str
    name: str
    meta: ToolMeta
    access_control: Optional[dict] = None
    updated_at: int
    created_at: int


class ToolUserResponse(ToolResponse):
    user: Optional[UserResponse] = None
    model_config = ConfigDict(extra="allow")


class ToolForm(BaseModel):
    id: str
    name: str
    content: str
    meta: ToolMeta
    access_control: Optional[dict] = None


class ToolValves(BaseModel):
    valves: Optional[dict] = None


class ToolsTable:
    async def insert_new_tool(
        self, user_id: str, form_data: ToolForm, specs: list[dict]
    ) -> Optional[ToolModel]:
        now = int(time.time())
        row = Tool(
            **form_data.model_dump(),
            specs=specs,
            user_id=user_id,
            updated_at=now,
            created_at=now,
        )
        try:
            async with get_db() as db:
                db.add(row)
                await db.commit()
                await db.refresh(row)
                return ToolModel.model_validate(row)
        except Exception as e:
            log.exception(f"Error creating a new tool: {e}")
            return None

    async def get_tool_by_id(self, id: str) -> Optional[ToolModel]:
        try:
            async with get_db() as db:
                row = await db.get(Tool, id)
                return ToolModel.model_validate(row) if row else None
        except Exception:
            return None

    async def get_tools(self) -> list[ToolUserModel]:
        async with get_db() as db:
            rows = (await db.execute(select(Tool).order_by(Tool.updated_at.desc()))).scalars().all()
        user_ids = list({row.user_id for row in rows})
        users = await Users.get_users_by_user_ids(user_ids) if user_ids else []
        users_dict = {user.id: user for user in users}
        return [
            ToolUserModel.model_validate(
                {
                    **ToolModel.model_validate(row).model_dump(),
                    "user": users_dict[row.user_id].model_dump() if row.user_id in users_dict else None,
                }
            )
            for row in rows
        ]

    async def get_tools_by_user_id(self, user_id: str, permission: str = "write") -> list[ToolUserModel]:
        tools = await self.get_tools()
        user_group_ids = {group.id for group in await Groups.get_groups_by_member_id(user_id)}
        return [
            tool
            for tool in tools
            if tool.user_id == user_id or has_access(user_id, permission, tool.access_control, user_group_ids)
        ]

    async def get_tool_valves_by_id(self, id: str) -> Optional[dict]:
        async with get_db() as db:
            row = await db.get(Tool, id)
            return row.valves if row and row.valves else {}

    async def update_tool_valves_by_id(self, id: str, valves: dict) -> Optional[ToolValves]:
        async with get_db() as db:
            row = await db.get(Tool, id)
            if not row:
                return None
            row.valves = valves
            row.updated_at = int(time.time())
            await db.commit()
            await db.refresh(row)
            return ToolModel.model_validate(row)

    async def get_user_valves_by_id_and_user_id(self, id: str, user_id: str) -> Optional[dict]:
        try:
            user = await Users.get_user_by_id(user_id)
            user_settings = user.settings.model_dump() if user and user.settings else {}
            return user_settings.get("tools", {}).get("valves", {}).get(id, {})
        except Exception as e:
            log.exception(f"Error getting user values by id {id} and user_id {user_id}: {e}")
            return None

    async def update_user_valves_by_id_and_user_id(self, id: str, user_id: str, valves: dict) -> Optional[dict]:
        try:
            user = await Users.get_user_by_id(user_id)
            user_settings = user.settings.model_dump() if user and user.settings else {}
            user_settings.setdefault("tools", {}).setdefault("valves", {})[id] = valves
            await Users.update_user_by_id(user_id, {"settings": user_settings})
            return user_settings["tools"]["valves"][id]
        except Exception as e:
            log.exception(f"Error updating user valves by id {id} and user_id {user_id}: {e}")
            return None

    async def update_tool_by_id(self, id: str, updated: dict) -> Optional[ToolModel]:
        async with get_db() as db:
            row = (
                await db.execute(
                    update(Tool)
                    .where(Tool.id == id)
                    .values(**updated, updated_at=int(time.time()))
                    .returning(Tool)
                )
            ).scalars().first()
            await db.commit()
            return ToolModel.model_validate(row) if row else None

    async def delete_tool_by_id(self, id: str) -> bool:
        async with get_db() as db:
            result = await db.execute(delete(Tool).where(Tool.id == id))
            await db.commit()
            return result.rowcount > 0


Tools = ToolsTable()
