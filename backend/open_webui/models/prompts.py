import time
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, String, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB

from open_webui.internal.db import Base, get_db
from open_webui.models.groups import Groups
from open_webui.models.users import UserResponse, Users
from open_webui.utils.access_control import has_access


class Prompt(Base):
    __tablename__ = "prompt"

    command = Column(String, primary_key=True)
    user_id = Column(String)
    title = Column(Text)
    content = Column(Text)
    timestamp = Column(BigInteger)
    access_control = Column(JSONB, nullable=True)


class PromptModel(BaseModel):
    command: str
    user_id: str
    title: str
    content: str
    timestamp: int
    access_control: Optional[dict] = None
    model_config = ConfigDict(from_attributes=True)


class PromptUserResponse(PromptModel):
    user: Optional[UserResponse] = None


class PromptForm(BaseModel):
    command: str
    title: str
    content: str
    access_control: Optional[dict] = None


class PromptsTable:
    async def insert_new_prompt(
        self, user_id: str, form_data: PromptForm
    ) -> Optional[PromptModel]:
        prompt = Prompt(
            user_id=user_id,
            **form_data.model_dump(),
            timestamp=int(time.time()),
        )
        try:
            async with get_db() as db:
                db.add(prompt)
                await db.commit()
                await db.refresh(prompt)
                return PromptModel.model_validate(prompt)
        except Exception:
            return None

    async def get_prompt_by_command(self, command: str) -> Optional[PromptModel]:
        try:
            async with get_db() as db:
                prompt = await db.get(Prompt, command)
                return PromptModel.model_validate(prompt) if prompt else None
        except Exception:
            return None

    async def get_prompts(self) -> list[PromptUserResponse]:
        async with get_db() as db:
            prompts = (
                await db.execute(select(Prompt).order_by(Prompt.timestamp.desc()))
            ).scalars().all()

        user_ids = list({prompt.user_id for prompt in prompts})
        users = await Users.get_users_by_user_ids(user_ids) if user_ids else []
        users_dict = {user.id: user for user in users}

        return [
            PromptUserResponse.model_validate(
                {
                    **PromptModel.model_validate(prompt).model_dump(),
                    "user": users_dict[prompt.user_id].model_dump()
                    if prompt.user_id in users_dict
                    else None,
                }
            )
            for prompt in prompts
        ]

    async def get_prompts_by_user_id(
        self, user_id: str, permission: str = "write"
    ) -> list[PromptUserResponse]:
        prompts = await self.get_prompts()
        user_group_ids = {group.id for group in await Groups.get_groups_by_member_id(user_id)}

        return [
            prompt
            for prompt in prompts
            if prompt.user_id == user_id
            or has_access(user_id, permission, prompt.access_control, user_group_ids)
        ]

    async def update_prompt_by_command(
        self, command: str, form_data: PromptForm
    ) -> Optional[PromptModel]:
        try:
            async with get_db() as db:
                prompt = await db.get(Prompt, command)
                if not prompt:
                    return None
                prompt.title = form_data.title
                prompt.content = form_data.content
                prompt.access_control = form_data.access_control
                prompt.timestamp = int(time.time())
                await db.commit()
                await db.refresh(prompt)
                return PromptModel.model_validate(prompt)
        except Exception:
            return None

    async def delete_prompt_by_command(self, command: str) -> bool:
        try:
            async with get_db() as db:
                await db.execute(delete(Prompt).where(Prompt.command == command))
                await db.commit()
                return True
        except Exception:
            return False


Prompts = PromptsTable()
