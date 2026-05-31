import base64
import hashlib
import json
import logging
import time
from typing import Any, Optional

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, Boolean, Column, Index, String, Text

from open_webui.env import OAUTH_SESSION_TOKEN_ENCRYPTION_KEY, SRC_LOG_LEVELS
from open_webui.internal.db import Base, JSONField, get_db


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


def _fernet() -> Fernet:
    key: bytes | str = OAUTH_SESSION_TOKEN_ENCRYPTION_KEY
    if not key:
        raise RuntimeError("OAUTH_SESSION_TOKEN_ENCRYPTION_KEY is not set")
    if isinstance(key, str):
        if len(key) != 44:
            key = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
        else:
            key = key.encode()
    return Fernet(key)


FERNET = _fernet()


def encrypt_secret(value: Any) -> Optional[str]:
    if value is None:
        return None
    return FERNET.encrypt(json.dumps(value).encode("utf-8")).decode("utf-8")


def decrypt_secret(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(FERNET.decrypt(value.encode("utf-8")).decode("utf-8"))
    except Exception:
        log.exception("Failed to decrypt MCP secret")
        return default


class MCPConnection(Base):
    __tablename__ = "mcp_connection"

    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    transport = Column(String, nullable=False)
    url = Column(Text, nullable=True)
    command = Column(Text, nullable=True)
    args = Column(JSONField, nullable=True)
    cwd = Column(Text, nullable=True)
    auth_type = Column(String, nullable=False, default="none")
    key = Column(Text, nullable=True)
    headers = Column(Text, nullable=True)
    env = Column(Text, nullable=True)
    oauth = Column(Text, nullable=True)
    policy = Column(JSONField, nullable=True)
    tool_filters = Column(JSONField, nullable=True)
    meta = Column(JSONField, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    updated_at = Column(BigInteger, nullable=False)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("idx_mcp_connection_user_id", "user_id"),
        Index("idx_mcp_connection_user_transport", "user_id", "transport"),
    )


class MCPConnectionModel(BaseModel):
    id: str
    user_id: str
    name: str
    description: Optional[str] = None
    transport: str = "remote_http"
    url: Optional[str] = None
    command: Optional[str] = None
    args: list[str] = Field(default_factory=list)
    cwd: Optional[str] = None
    auth_type: str = "none"
    policy: dict = Field(default_factory=dict)
    tool_filters: dict = Field(default_factory=dict)
    meta: dict = Field(default_factory=dict)
    enabled: bool = True
    authenticated: Optional[bool] = None
    updated_at: int
    created_at: int

    model_config = ConfigDict(from_attributes=True)


class MCPConnectionWithSecrets(MCPConnectionModel):
    key: Optional[str] = None
    headers: list[dict[str, str]] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    oauth: dict = Field(default_factory=dict)


class MCPConnectionForm(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    transport: str = "remote_http"
    url: Optional[str] = None
    command: Optional[str] = None
    args: list[str] = Field(default_factory=list)
    cwd: Optional[str] = None
    auth_type: str = "none"
    key: Optional[str] = None
    headers: list[dict[str, str]] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    policy: dict = Field(default_factory=dict)
    tool_filters: dict = Field(default_factory=dict)
    meta: dict = Field(default_factory=dict)
    enabled: bool = True


def _public_from_row(row: MCPConnection) -> MCPConnectionModel:
    return MCPConnectionModel.model_validate(
        {
            "id": row.id,
            "user_id": row.user_id,
            "name": row.name,
            "description": row.description,
            "transport": row.transport,
            "url": row.url,
            "command": row.command,
            "args": row.args or [],
            "cwd": row.cwd,
            "auth_type": row.auth_type or "none",
            "policy": row.policy or {},
            "tool_filters": row.tool_filters or {},
            "meta": row.meta or {},
            "enabled": bool(row.enabled),
            "updated_at": row.updated_at,
            "created_at": row.created_at,
        }
    )


def _with_secrets_from_row(row: MCPConnection) -> MCPConnectionWithSecrets:
    public = _public_from_row(row).model_dump()
    return MCPConnectionWithSecrets.model_validate(
        {
            **public,
            "key": decrypt_secret(row.key),
            "headers": decrypt_secret(row.headers, []) or [],
            "env": decrypt_secret(row.env, {}) or {},
            "oauth": decrypt_secret(row.oauth, {}) or {},
        }
    )


def _slug_id(value: str) -> str:
    value = (value or "").strip().lower()
    out = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        elif ch.isspace() or ch in (".", "/"):
            out.append("-")
    slug = "".join(out).strip("-")[:64]
    return slug or hashlib.sha1(str(time.time()).encode()).hexdigest()[:12]


class MCPConnectionsTable:
    def insert_new_connection(
        self, user_id: str, form_data: MCPConnectionForm
    ) -> Optional[MCPConnectionModel]:
        now = int(time.time())
        base_id = _slug_id(form_data.id or form_data.name)
        user_digest = hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:8]
        connection_id = f"{base_id}-{user_digest}"
        if self.get_connection_by_id(connection_id):
            connection_id = f"{base_id}-{user_digest}-{hashlib.sha1(str(now).encode()).hexdigest()[:6]}"
        row_data = {
            **form_data.model_dump(exclude={"key", "headers", "env"}),
            "id": connection_id,
            "user_id": user_id,
            "args": form_data.args or [],
            "policy": form_data.policy or {},
            "tool_filters": form_data.tool_filters or {},
            "meta": form_data.meta or {},
            "key": encrypt_secret(form_data.key),
            "headers": encrypt_secret(form_data.headers or []),
            "env": encrypt_secret(form_data.env or {}),
            "oauth": encrypt_secret({}),
            "updated_at": now,
            "created_at": now,
        }
        try:
            with get_db() as db:
                result = MCPConnection(**row_data)
                db.add(result)
                db.commit()
                db.refresh(result)
                return _public_from_row(result)
        except Exception:
            log.exception("Error creating MCP connection")
            return None

    def get_connection_by_id(
        self, id: str, *, include_secrets: bool = False
    ) -> Optional[MCPConnectionModel | MCPConnectionWithSecrets]:
        try:
            with get_db() as db:
                row = db.get(MCPConnection, id)
                if not row:
                    return None
                return _with_secrets_from_row(row) if include_secrets else _public_from_row(row)
        except Exception:
            log.exception("Error getting MCP connection")
            return None

    def get_connection_by_id_and_user_id(
        self, id: str, user_id: str, *, include_secrets: bool = False
    ) -> Optional[MCPConnectionModel | MCPConnectionWithSecrets]:
        try:
            with get_db() as db:
                row = db.get(MCPConnection, id)
                if not row or row.user_id != user_id:
                    return None
                return _with_secrets_from_row(row) if include_secrets else _public_from_row(row)
        except Exception:
            log.exception("Error getting MCP connection")
            return None

    def get_connections_by_user_id(
        self, user_id: str, *, include_secrets: bool = False, enabled_only: bool = False
    ) -> list[MCPConnectionModel | MCPConnectionWithSecrets]:
        with get_db() as db:
            query = db.query(MCPConnection).filter_by(user_id=user_id)
            if enabled_only:
                query = query.filter_by(enabled=True)
            rows = query.order_by(MCPConnection.updated_at.desc()).all()
            return [
                _with_secrets_from_row(row) if include_secrets else _public_from_row(row)
                for row in rows
            ]

    def update_connection_by_id_and_user_id(
        self, id: str, user_id: str, updated: dict
    ) -> Optional[MCPConnectionModel]:
        secret_updates = {}
        for field_name in ("key", "headers", "env", "oauth"):
            if field_name in updated:
                secret_updates[field_name] = encrypt_secret(updated.pop(field_name))
        try:
            with get_db() as db:
                row = db.get(MCPConnection, id)
                if not row or row.user_id != user_id:
                    return None
                db.query(MCPConnection).filter_by(id=id, user_id=user_id).update(
                    {**updated, **secret_updates, "updated_at": int(time.time())}
                )
                db.commit()
            return self.get_connection_by_id_and_user_id(id, user_id)
        except Exception:
            log.exception("Error updating MCP connection")
            return None

    def update_oauth_by_id_and_user_id(
        self, id: str, user_id: str, oauth: dict
    ) -> Optional[MCPConnectionModel]:
        return self.update_connection_by_id_and_user_id(id, user_id, {"oauth": oauth})

    def delete_connection_by_id_and_user_id(self, id: str, user_id: str) -> bool:
        try:
            with get_db() as db:
                deleted = db.query(MCPConnection).filter_by(id=id, user_id=user_id).delete()
                db.commit()
                return deleted > 0
        except Exception:
            log.exception("Error deleting MCP connection")
            return False


MCPConnections = MCPConnectionsTable()
