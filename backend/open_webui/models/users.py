import time
from typing import Optional

from open_webui.internal.db import Base, JSONField, get_db


from open_webui.env import DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL
from open_webui.models.chats import Chats
from open_webui.models.groups import Groups
from open_webui.utils.misc import throttle


from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Date, String, Text
from sqlalchemy import delete, func, or_, select, update

import datetime

####################
# User DB Schema
####################


class User(Base):
    __tablename__ = "user"

    id = Column(String, primary_key=True)
    name = Column(String)

    email = Column(String)
    username = Column(String(50), nullable=True)

    role = Column(String)
    profile_image_url = Column(Text)

    bio = Column(Text, nullable=True)
    gender = Column(Text, nullable=True)
    date_of_birth = Column(Date, nullable=True)

    info = Column(JSONField, nullable=True)
    settings = Column(JSONField, nullable=True)

    api_key = Column(String, nullable=True, unique=True)
    oauth_sub = Column(Text, unique=True)

    last_active_at = Column(BigInteger)

    updated_at = Column(BigInteger)
    created_at = Column(BigInteger)


class UserSettings(BaseModel):
    ui: Optional[dict] = {}
    model_config = ConfigDict(extra="allow")
    pass


class UserModel(BaseModel):
    id: str
    name: str

    email: str
    username: Optional[str] = None

    role: str = "pending"
    profile_image_url: str

    bio: Optional[str] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime.date] = None

    info: Optional[dict] = None
    settings: Optional[UserSettings] = None

    api_key: Optional[str] = None
    oauth_sub: Optional[str] = None

    last_active_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch
    created_at: int  # timestamp in epoch

    model_config = ConfigDict(from_attributes=True)


####################
# Forms
####################


class UpdateProfileForm(BaseModel):
    profile_image_url: str
    name: str
    bio: Optional[str] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime.date] = None


class UserListResponse(BaseModel):
    users: list[UserModel]
    total: int


class UserInfoResponse(BaseModel):
    id: str
    name: str
    email: str
    role: str


class UserIdNameResponse(BaseModel):
    id: str
    name: str


class UserInfoListResponse(BaseModel):
    users: list[UserInfoResponse]
    total: int


class UserIdNameListResponse(BaseModel):
    users: list[UserIdNameResponse]
    total: int


class UserResponse(BaseModel):
    id: str
    name: str
    email: str
    role: str
    profile_image_url: str


class UserNameResponse(BaseModel):
    id: str
    name: str
    role: str
    profile_image_url: str


class UserRoleUpdateForm(BaseModel):
    id: str
    role: str


class UserUpdateForm(BaseModel):
    role: str
    name: str
    email: str
    profile_image_url: str
    password: Optional[str] = None


class UsersTable:
    async def insert_new_user(
        self,
        id: str,
        name: str,
        email: str,
        profile_image_url: str = "/user.png",
        role: str = "pending",
        oauth_sub: Optional[str] = None,
    ) -> Optional[UserModel]:
        async with get_db() as db:
            user = UserModel(
                **{
                    "id": id,
                    "name": name,
                    "email": email,
                    "role": role,
                    "profile_image_url": profile_image_url,
                    "last_active_at": int(time.time()),
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                    "oauth_sub": oauth_sub,
                }
            )
            result = User(**user.model_dump())
            db.add(result)
            await db.commit()
            await db.refresh(result)
            if result:
                return UserModel.model_validate(result)
            else:
                return None

    async def get_user_by_id(self, id: str) -> Optional[UserModel]:
        try:
            async with get_db() as db:
                result = await db.execute(select(User).where(User.id == id).limit(1))
                user = result.scalars().first()
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    async def get_user_by_api_key(self, api_key: str) -> Optional[UserModel]:
        try:
            async with get_db() as db:
                result = await db.execute(
                    select(User).where(User.api_key == api_key).limit(1)
                )
                user = result.scalars().first()
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    async def get_user_by_email(self, email: str) -> Optional[UserModel]:
        try:
            async with get_db() as db:
                result = await db.execute(select(User).where(User.email == email).limit(1))
                user = result.scalars().first()
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    async def get_user_by_oauth_sub(self, sub: str) -> Optional[UserModel]:
        try:
            async with get_db() as db:
                result = await db.execute(
                    select(User).where(User.oauth_sub == sub).limit(1)
                )
                user = result.scalars().first()
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    async def get_users(
        self,
        filter: Optional[dict] = None,
        skip: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict:
        async with get_db() as db:
            stmt = select(User)

            if filter:
                query_key = filter.get("query")
                if query_key:
                    stmt = stmt.where(
                        or_(
                            User.name.ilike(f"%{query_key}%"),
                            User.email.ilike(f"%{query_key}%"),
                        )
                    )

                order_by = filter.get("order_by")
                direction = filter.get("direction")

                if order_by == "name":
                    if direction == "asc":
                        stmt = stmt.order_by(User.name.asc())
                    else:
                        stmt = stmt.order_by(User.name.desc())
                elif order_by == "email":
                    if direction == "asc":
                        stmt = stmt.order_by(User.email.asc())
                    else:
                        stmt = stmt.order_by(User.email.desc())

                elif order_by == "created_at":
                    if direction == "asc":
                        stmt = stmt.order_by(User.created_at.asc())
                    else:
                        stmt = stmt.order_by(User.created_at.desc())

                elif order_by == "last_active_at":
                    if direction == "asc":
                        stmt = stmt.order_by(User.last_active_at.asc())
                    else:
                        stmt = stmt.order_by(User.last_active_at.desc())

                elif order_by == "updated_at":
                    if direction == "asc":
                        stmt = stmt.order_by(User.updated_at.asc())
                    else:
                        stmt = stmt.order_by(User.updated_at.desc())
                elif order_by == "role":
                    if direction == "asc":
                        stmt = stmt.order_by(User.role.asc())
                    else:
                        stmt = stmt.order_by(User.role.desc())

            else:
                stmt = stmt.order_by(User.created_at.desc())

            if skip:
                stmt = stmt.offset(skip)
            if limit:
                stmt = stmt.limit(limit)

            result = await db.execute(stmt)
            users = result.scalars().all()
            total = await db.scalar(select(func.count()).select_from(User))
            return {
                "users": [UserModel.model_validate(user) for user in users],
                "total": total or 0,
            }

    async def get_users_by_user_ids(self, user_ids: list[str]) -> list[UserModel]:
        async with get_db() as db:
            result = await db.execute(select(User).where(User.id.in_(user_ids)))
            users = result.scalars().all()
            return [UserModel.model_validate(user) for user in users]

    async def get_num_users(self) -> Optional[int]:
        async with get_db() as db:
            return await db.scalar(select(func.count()).select_from(User))

    async def has_users(self) -> bool:
        async with get_db() as db:
            count = await db.scalar(select(func.count()).select_from(User))
            return bool(count)

    async def get_first_user(self) -> Optional[UserModel]:
        try:
            async with get_db() as db:
                result = await db.execute(select(User).order_by(User.created_at).limit(1))
                user = result.scalars().first()
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    async def get_user_webhook_url_by_id(self, id: str) -> Optional[str]:
        try:
            async with get_db() as db:
                result = await db.execute(select(User).where(User.id == id).limit(1))
                user = result.scalars().first()

                if not user or user.settings is None:
                    return None
                else:
                    return (
                        user.settings.get("ui", {})
                        .get("notifications", {})
                        .get("webhook_url", None)
                    )
        except Exception:
            return None

    async def update_user_role_by_id(self, id: str, role: str) -> Optional[UserModel]:
        try:
            async with get_db() as db:
                result = await db.execute(
                    update(User).where(User.id == id).values(role=role).returning(User)
                )
                await db.commit()
                user = result.scalars().first()
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    async def update_user_profile_image_url_by_id(
        self, id: str, profile_image_url: str
    ) -> Optional[UserModel]:
        try:
            async with get_db() as db:
                result = await db.execute(
                    update(User)
                    .where(User.id == id)
                    .values(profile_image_url=profile_image_url)
                    .returning(User)
                )
                await db.commit()
                user = result.scalars().first()
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    @throttle(DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL)
    async def update_user_last_active_by_id(self, id: str) -> Optional[UserModel]:
        try:
            async with get_db() as db:
                result = await db.execute(
                    update(User)
                    .where(User.id == id)
                    .values(last_active_at=int(time.time()))
                    .returning(User)
                )
                await db.commit()
                user = result.scalars().first()
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    async def update_user_oauth_sub_by_id(
        self, id: str, oauth_sub: str
    ) -> Optional[UserModel]:
        try:
            async with get_db() as db:
                result = await db.execute(
                    update(User)
                    .where(User.id == id)
                    .values(oauth_sub=oauth_sub)
                    .returning(User)
                )
                await db.commit()
                user = result.scalars().first()
                return UserModel.model_validate(user) if user else None
        except Exception:
            return None

    async def update_user_by_id(self, id: str, updated: dict) -> Optional[UserModel]:
        try:
            async with get_db() as db:
                result = await db.execute(
                    update(User).where(User.id == id).values(**updated).returning(User)
                )
                await db.commit()
                user = result.scalars().first()
                return UserModel.model_validate(user) if user else None
        except Exception as e:
            print(e)
            return None

    async def update_user_settings_by_id(self, id: str, updated: dict) -> Optional[UserModel]:
        try:
            async with get_db() as db:
                result = await db.execute(select(User).where(User.id == id).limit(1))
                user = result.scalars().first()
                if not user:
                    return None
                user_settings = user.settings

                if user_settings is None:
                    user_settings = {}

                user_settings.update(updated)

                user.settings = user_settings
                db.add(user)
                await db.commit()
                await db.refresh(user)
                return UserModel.model_validate(user)
        except Exception:
            return None

    async def delete_user_by_id(self, id: str) -> bool:
        try:
            # Remove User from Groups
            await Groups.remove_user_from_all_groups(id)

            # Delete User Chats
            result = await Chats.delete_chats_by_user_id(id)
            if result:
                async with get_db() as db:
                    # Delete User
                    await db.execute(delete(User).where(User.id == id))
                    await db.commit()

                return True
            else:
                return False
        except Exception:
            return False

    async def update_user_api_key_by_id(self, id: str, api_key: str) -> bool:
        try:
            async with get_db() as db:
                result = await db.execute(
                    update(User).where(User.id == id).values(api_key=api_key)
                )
                await db.commit()
                return result.rowcount == 1
        except Exception:
            return False

    async def get_user_api_key_by_id(self, id: str) -> Optional[str]:
        try:
            async with get_db() as db:
                result = await db.execute(select(User.api_key).where(User.id == id).limit(1))
                return result.scalar_one_or_none()
        except Exception:
            return None

    async def get_valid_user_ids(self, user_ids: list[str]) -> list[str]:
        async with get_db() as db:
            result = await db.execute(select(User.id).where(User.id.in_(user_ids)))
            users = result.scalars().all()
            return list(users)

    async def get_super_admin_user(self) -> Optional[UserModel]:
        async with get_db() as db:
            result = await db.execute(select(User).where(User.role == "admin").limit(1))
            user = result.scalars().first()
            if user:
                return UserModel.model_validate(user)
            else:
                return None


Users = UsersTable()
