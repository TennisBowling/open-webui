import logging
import time
from typing import Optional
import uuid

from open_webui.internal.db import Base, get_db
from open_webui.env import SRC_LOG_LEVELS

from open_webui.models.files import FileMetadataResponse


from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Text, delete, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

####################
# UserGroup DB Schema
####################


class Group(Base):
    __tablename__ = "group"

    id = Column(Text, unique=True, primary_key=True)
    user_id = Column(Text)

    name = Column(Text)
    description = Column(Text)

    data = Column(JSONB, nullable=True)
    meta = Column(JSONB, nullable=True)

    permissions = Column(JSONB, nullable=True)
    user_ids = Column(JSONB, nullable=True)

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class GroupUser(Base):
    __tablename__ = "group_user"

    group_id = Column(Text, primary_key=True)
    user_id = Column(Text, primary_key=True)
    created_at = Column(BigInteger)


class GroupModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_id: str

    name: str
    description: str

    data: Optional[dict] = None
    meta: Optional[dict] = None

    permissions: Optional[dict] = None
    user_ids: list[str] = []

    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch


####################
# Forms
####################


class GroupResponse(BaseModel):
    id: str
    user_id: str
    name: str
    description: str
    permissions: Optional[dict] = None
    data: Optional[dict] = None
    meta: Optional[dict] = None
    user_ids: list[str] = []
    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch


class GroupForm(BaseModel):
    name: str
    description: str
    permissions: Optional[dict] = None


class UserIdsForm(BaseModel):
    user_ids: Optional[list[str]] = None


class GroupUpdateForm(GroupForm, UserIdsForm):
    pass


class GroupTable:
    async def _hydrate_user_ids(self, db, groups: list[Group]) -> list[Group]:
        if not groups:
            return groups
        ids = [group.id for group in groups]
        rows = await db.execute(
            select(GroupUser.group_id, GroupUser.user_id).where(GroupUser.group_id.in_(ids))
        )
        by_group: dict[str, list[str]] = {group_id: [] for group_id in ids}
        for group_id, user_id in rows.all():
            by_group.setdefault(group_id, []).append(user_id)
        for group in groups:
            group.user_ids = by_group.get(group.id, [])
        return groups

    async def _set_group_users(self, db, group: Group, user_ids: list[str]) -> None:
        now = int(time.time())
        unique_ids = list(dict.fromkeys(user_ids or []))
        group.user_ids = unique_ids
        await db.execute(delete(GroupUser).where(GroupUser.group_id == group.id))
        if unique_ids:
            await db.execute(
                pg_insert(GroupUser)
                .values(
                    [
                        {"group_id": group.id, "user_id": user_id, "created_at": now}
                        for user_id in unique_ids
                    ]
                )
                .on_conflict_do_nothing()
            )

    async def insert_new_group(
        self, user_id: str, form_data: GroupForm
    ) -> Optional[GroupModel]:
        async with get_db() as db:
            group = GroupModel(
                **{
                    **form_data.model_dump(exclude_none=True),
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                }
            )

            try:
                result = Group(**group.model_dump())
                db.add(result)
                await db.commit()
                await db.refresh(result)
                if result:
                    return GroupModel.model_validate(result)
                else:
                    return None

            except Exception:
                return None

    async def get_groups(self) -> list[GroupModel]:
        async with get_db() as db:
            result = await db.execute(select(Group).order_by(Group.updated_at.desc()))
            groups = await self._hydrate_user_ids(db, list(result.scalars().all()))
            return [GroupModel.model_validate(group) for group in groups]

    async def get_groups_by_member_id(self, user_id: str) -> list[GroupModel]:
        async with get_db() as db:
            result = await db.execute(
                select(Group)
                .join(GroupUser, GroupUser.group_id == Group.id)
                .where(GroupUser.user_id == user_id)
                .order_by(Group.updated_at.desc())
            )
            groups = await self._hydrate_user_ids(db, list(result.scalars().all()))
            return [GroupModel.model_validate(group) for group in groups]

    async def get_group_by_id(self, id: str) -> Optional[GroupModel]:
        try:
            async with get_db() as db:
                result = await db.execute(select(Group).where(Group.id == id).limit(1))
                group = result.scalars().first()
                if group:
                    await self._hydrate_user_ids(db, [group])
                return GroupModel.model_validate(group) if group else None
        except Exception:
            return None

    async def get_group_user_ids_by_id(self, id: str) -> Optional[list[str]]:
        group = await self.get_group_by_id(id)
        if group:
            return group.user_ids
        else:
            return None

    async def update_group_by_id(
        self, id: str, form_data: GroupUpdateForm, overwrite: bool = False
    ) -> Optional[GroupModel]:
        try:
            async with get_db() as db:
                values = {
                    **form_data.model_dump(exclude_none=True),
                    "updated_at": int(time.time()),
                }
                user_ids = values.pop("user_ids", None)
                result = await db.execute(
                    update(Group)
                    .where(Group.id == id)
                    .values(**values)
                    .returning(Group)
                )
                group = result.scalars().first()
                if group and user_ids is not None:
                    await self._set_group_users(db, group, user_ids)
                await db.commit()
                if group:
                    await self._hydrate_user_ids(db, [group])
                return GroupModel.model_validate(group) if group else None
        except Exception as e:
            log.exception(e)
            return None

    async def delete_group_by_id(self, id: str) -> bool:
        try:
            async with get_db() as db:
                await db.execute(delete(Group).where(Group.id == id))
                await db.commit()
                return True
        except Exception:
            return False

    async def delete_all_groups(self) -> bool:
        async with get_db() as db:
            try:
                await db.execute(delete(Group))
                await db.commit()

                return True
            except Exception:
                return False

    async def remove_user_from_all_groups(self, user_id: str) -> bool:
        async with get_db() as db:
            try:
                result = await db.execute(
                    select(Group)
                    .join(GroupUser, GroupUser.group_id == Group.id)
                    .where(GroupUser.user_id == user_id)
                )
                groups = await self._hydrate_user_ids(db, list(result.scalars().all()))
                now = int(time.time())

                for group in groups:
                    ids = [uid for uid in (group.user_ids or []) if uid != user_id]
                    group.user_ids = ids
                    group.updated_at = now

                await db.execute(delete(GroupUser).where(GroupUser.user_id == user_id))

                await db.commit()

                return True
            except Exception:
                return False

    async def create_groups_by_group_names(
        self, user_id: str, group_names: list[str]
    ) -> list[GroupModel]:

        # check for existing groups
        async with get_db() as db:
            result = await db.execute(select(Group.name).where(Group.name.in_(group_names)))
            existing_group_names = set(result.scalars().all())

            new_groups = []
            now = int(time.time())

            for group_name in group_names:
                if group_name not in existing_group_names:
                    new_group = GroupModel(
                        id=str(uuid.uuid4()),
                        user_id=user_id,
                        name=group_name,
                        description="",
                        created_at=now,
                        updated_at=now,
                    )
                    try:
                        result = Group(**new_group.model_dump())
                        db.add(result)
                        await db.flush()
                        new_groups.append(GroupModel.model_validate(result))
                    except Exception as e:
                        log.exception(e)
                        continue
            await db.commit()
            return new_groups

    async def sync_groups_by_group_names(self, user_id: str, group_names: list[str]) -> bool:
        async with get_db() as db:
            try:
                result = await db.execute(select(Group).where(Group.name.in_(group_names)))
                groups = await self._hydrate_user_ids(db, list(result.scalars().all()))
                group_ids = [group.id for group in groups]

                # Remove user from groups not in the new list
                result = await db.execute(
                    select(Group)
                    .join(GroupUser, GroupUser.group_id == Group.id)
                    .where(GroupUser.user_id == user_id)
                )
                existing_groups = await self._hydrate_user_ids(
                    db, list(result.scalars().all())
                )
                now = int(time.time())

                for group in existing_groups:
                    if group.id not in group_ids:
                        group.user_ids = [uid for uid in (group.user_ids or []) if uid != user_id]
                        group.updated_at = now
                        await db.execute(
                            delete(GroupUser).where(
                                GroupUser.group_id == group.id,
                                GroupUser.user_id == user_id,
                            )
                        )

                # Add user to new groups
                for group in groups:
                    ids = list(group.user_ids or [])
                    if user_id not in ids:
                        ids.append(user_id)
                        group.user_ids = ids
                        group.updated_at = now
                        await db.execute(
                            pg_insert(GroupUser)
                            .values(group_id=group.id, user_id=user_id, created_at=now)
                            .on_conflict_do_nothing()
                        )

                await db.commit()
                return True
            except Exception as e:
                log.exception(e)
                return False

    async def add_users_to_group(
        self, id: str, user_ids: Optional[list[str]] = None
    ) -> Optional[GroupModel]:
        try:
            async with get_db() as db:
                result = await db.execute(select(Group).where(Group.id == id).limit(1))
                group = result.scalars().first()
                if not group:
                    return None

                group_user_ids = group.user_ids
                if not group_user_ids or not isinstance(group_user_ids, list):
                    group_user_ids = []

                group_user_ids = list(set(group_user_ids))  # Deduplicate

                for user_id in user_ids or []:
                    if user_id not in group_user_ids:
                        group_user_ids.append(user_id)

                group.user_ids = group_user_ids
                group.updated_at = int(time.time())
                await self._set_group_users(db, group, group_user_ids)
                await db.commit()
                await db.refresh(group)
                return GroupModel.model_validate(group)
        except Exception as e:
            log.exception(e)
            return None

    async def remove_users_from_group(
        self, id: str, user_ids: Optional[list[str]] = None
    ) -> Optional[GroupModel]:
        try:
            async with get_db() as db:
                result = await db.execute(select(Group).where(Group.id == id).limit(1))
                group = result.scalars().first()
                if not group:
                    return None

                group_user_ids = group.user_ids

                if not group_user_ids or not isinstance(group_user_ids, list):
                    return GroupModel.model_validate(group)

                group_user_ids = list(set(group_user_ids))  # Deduplicate

                for user_id in user_ids or []:
                    if user_id in group_user_ids:
                        group_user_ids.remove(user_id)

                group.user_ids = group_user_ids
                group.updated_at = int(time.time())
                await self._set_group_users(db, group, group_user_ids)

                await db.commit()
                await db.refresh(group)
                return GroupModel.model_validate(group)
        except Exception as e:
            log.exception(e)
            return None


Groups = GroupTable()
