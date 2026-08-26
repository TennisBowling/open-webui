import logging
import time
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, Column, Index, String, Text, delete, select

from open_webui.env import SRC_LOG_LEVELS
from open_webui.internal.db import Base, JSONField, get_db

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


class PushSubscription(Base):
    """One browser/PWA Web Push endpoint.

    ``endpoint`` is unique because it IS the device's identity as far as the
    push service is concerned. It is deliberately not scoped by user: on a
    shared device a second account subscribing produces the SAME endpoint, and
    the row has to migrate to the new owner rather than duplicate — otherwise
    the previous user's runs would keep notifying the device.
    """

    __tablename__ = "push_subscription"

    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    endpoint = Column(Text, nullable=False, unique=True)
    keys = Column(JSONField, nullable=False)
    user_agent = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (Index("push_subscription_user_id_idx", "user_id"),)


class PushSubscriptionModel(BaseModel):
    id: str
    user_id: str
    endpoint: str
    keys: dict = Field(default_factory=dict)
    user_agent: Optional[str] = None
    created_at: int

    model_config = ConfigDict(from_attributes=True)


class PushSubscriptionsTable:
    async def upsert(
        self,
        user_id: str,
        endpoint: str,
        keys: dict,
        user_agent: Optional[str] = None,
    ) -> Optional[PushSubscriptionModel]:
        try:
            async with get_db() as db:
                row = (
                    await db.execute(
                        select(PushSubscription).where(
                            PushSubscription.endpoint == endpoint
                        )
                    )
                ).scalars().first()
                if row is None:
                    row = PushSubscription(
                        id=str(uuid.uuid4()),
                        user_id=user_id,
                        endpoint=endpoint,
                        keys=keys,
                        user_agent=user_agent,
                        created_at=int(time.time()),
                    )
                    db.add(row)
                else:
                    row.user_id = user_id
                    row.keys = keys
                    row.user_agent = user_agent
                await db.commit()
                await db.refresh(row)
                return PushSubscriptionModel.model_validate(row)
        except Exception:
            log.exception("Error upserting push subscription")
            return None

    async def get_by_user_id(self, user_id: str) -> list[PushSubscriptionModel]:
        async with get_db() as db:
            rows = (
                await db.execute(
                    select(PushSubscription).where(PushSubscription.user_id == user_id)
                )
            ).scalars().all()
            return [PushSubscriptionModel.model_validate(row) for row in rows]

    async def delete_by_endpoint(self, endpoint: str) -> bool:
        try:
            async with get_db() as db:
                result = await db.execute(
                    delete(PushSubscription).where(
                        PushSubscription.endpoint == endpoint
                    )
                )
                await db.commit()
                return result.rowcount > 0
        except Exception:
            log.exception("Error deleting push subscription")
            return False

    async def delete_by_user_and_endpoint(self, user_id: str, endpoint: str) -> bool:
        try:
            async with get_db() as db:
                result = await db.execute(
                    delete(PushSubscription).where(
                        PushSubscription.user_id == user_id,
                        PushSubscription.endpoint == endpoint,
                    )
                )
                await db.commit()
                return result.rowcount > 0
        except Exception:
            log.exception("Error deleting push subscription")
            return False


PushSubscriptions = PushSubscriptionsTable()
