import base64
import hashlib
import json
import logging
import time
import uuid
from typing import List, Optional

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, Index, Text, delete, select, update

from open_webui.env import OAUTH_SESSION_TOKEN_ENCRYPTION_KEY, SRC_LOG_LEVELS
from open_webui.internal.db import Base, get_db

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


class OAuthSession(Base):
    __tablename__ = "oauth_session"

    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False)
    provider = Column(Text, nullable=False)
    token = Column(Text, nullable=False)
    expires_at = Column(BigInteger, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_oauth_session_user_id", "user_id"),
        Index("idx_oauth_session_expires_at", "expires_at"),
        Index("idx_oauth_session_user_provider", "user_id", "provider"),
    )


class OAuthSessionModel(BaseModel):
    id: str
    user_id: str
    provider: str
    token: dict
    expires_at: int
    created_at: int
    updated_at: int

    model_config = ConfigDict(from_attributes=True)


class OAuthSessionResponse(BaseModel):
    id: str
    user_id: str
    provider: str
    expires_at: int


class OAuthSessionTable:
    def __init__(self):
        self.encryption_key = OAUTH_SESSION_TOKEN_ENCRYPTION_KEY
        if not self.encryption_key:
            raise Exception("OAUTH_SESSION_TOKEN_ENCRYPTION_KEY is not set")

        if len(self.encryption_key) != 44:
            key_bytes = hashlib.sha256(self.encryption_key.encode()).digest()
            self.encryption_key = base64.urlsafe_b64encode(key_bytes)
        else:
            self.encryption_key = self.encryption_key.encode()

        self.fernet = Fernet(self.encryption_key)

    def _encrypt_token(self, token) -> str:
        return self.fernet.encrypt(json.dumps(token).encode()).decode()

    def _decrypt_token(self, token: str):
        return json.loads(self.fernet.decrypt(token.encode()).decode())

    def _model_from_row(self, row: OAuthSession) -> OAuthSessionModel:
        row.token = self._decrypt_token(row.token)
        return OAuthSessionModel.model_validate(row)

    async def create_session(
        self,
        user_id: str,
        provider: str,
        token: dict,
    ) -> Optional[OAuthSessionModel]:
        try:
            async with get_db() as db:
                current_time = int(time.time())
                row = OAuthSession(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    provider=provider,
                    token=self._encrypt_token(token),
                    expires_at=token.get("expires_at"),
                    created_at=current_time,
                    updated_at=current_time,
                )
                db.add(row)
                await db.commit()
                await db.refresh(row)
                row.token = token
                return OAuthSessionModel.model_validate(row)
        except Exception as e:
            log.error(f"Error creating OAuth session: {e}")
            return None

    async def get_session_by_id(self, session_id: str) -> Optional[OAuthSessionModel]:
        try:
            async with get_db() as db:
                row = await db.get(OAuthSession, session_id)
                return self._model_from_row(row) if row else None
        except Exception as e:
            log.error(f"Error getting OAuth session by ID: {e}")
            return None

    async def get_session_by_id_and_user_id(
        self, session_id: str, user_id: str
    ) -> Optional[OAuthSessionModel]:
        try:
            async with get_db() as db:
                row = (
                    await db.execute(
                        select(OAuthSession).where(
                            OAuthSession.id == session_id,
                            OAuthSession.user_id == user_id,
                        ).limit(1)
                    )
                ).scalars().first()
                return self._model_from_row(row) if row else None
        except Exception as e:
            log.error(f"Error getting OAuth session by ID: {e}")
            return None

    async def get_session_by_provider_and_user_id(
        self, provider: str, user_id: str
    ) -> Optional[OAuthSessionModel]:
        try:
            async with get_db() as db:
                row = (
                    await db.execute(
                        select(OAuthSession).where(
                            OAuthSession.provider == provider,
                            OAuthSession.user_id == user_id,
                        ).limit(1)
                    )
                ).scalars().first()
                return self._model_from_row(row) if row else None
        except Exception as e:
            log.error(f"Error getting OAuth session by provider and user ID: {e}")
            return None

    async def get_sessions_by_user_id(self, user_id: str) -> List[OAuthSessionModel]:
        try:
            async with get_db() as db:
                rows = (
                    await db.execute(select(OAuthSession).where(OAuthSession.user_id == user_id))
                ).scalars().all()
                return [self._model_from_row(row) for row in rows]
        except Exception as e:
            log.error(f"Error getting OAuth sessions by user ID: {e}")
            return []

    async def update_session_by_id(
        self, session_id: str, token: dict
    ) -> Optional[OAuthSessionModel]:
        try:
            async with get_db() as db:
                row = (
                    await db.execute(
                        update(OAuthSession)
                        .where(OAuthSession.id == session_id)
                        .values(
                            token=self._encrypt_token(token),
                            expires_at=token.get("expires_at"),
                            updated_at=int(time.time()),
                        )
                        .returning(OAuthSession)
                    )
                ).scalars().first()
                await db.commit()
                return self._model_from_row(row) if row else None
        except Exception as e:
            log.error(f"Error updating OAuth session tokens: {e}")
            return None

    async def delete_session_by_id(self, session_id: str) -> bool:
        try:
            async with get_db() as db:
                result = await db.execute(delete(OAuthSession).where(OAuthSession.id == session_id))
                await db.commit()
                return result.rowcount > 0
        except Exception as e:
            log.error(f"Error deleting OAuth session: {e}")
            return False

    async def delete_sessions_by_user_id(self, user_id: str) -> bool:
        try:
            async with get_db() as db:
                await db.execute(delete(OAuthSession).where(OAuthSession.user_id == user_id))
                await db.commit()
                return True
        except Exception as e:
            log.error(f"Error deleting OAuth sessions by user ID: {e}")
            return False


OAuthSessions = OAuthSessionTable()
