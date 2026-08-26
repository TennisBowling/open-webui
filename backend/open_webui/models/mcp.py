import base64
import hashlib
import json
import logging
import time
from typing import Any, Optional

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, Boolean, Column, Index, String, Text, delete, select, update

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


class MCPConnectionWithHeaders(MCPConnectionModel):
    """Owner-visible connection data used to edit custom request metadata.

    Headers and env entries must round-trip through the settings form so the
    owner can see/edit what's already saved — they're only ever exposed to the
    connection's own owner (or an admin), same trust boundary as the rest of
    this endpoint. Bearer keys and OAuth grants remain write-only since they're
    single opaque values with no editing value in showing them back.
    """

    headers: list[dict[str, str]] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


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
    async def insert_new_connection(
        self, user_id: str, form_data: MCPConnectionForm
    ) -> Optional[MCPConnectionModel]:
        now = int(time.time())
        base_id = _slug_id(form_data.id or form_data.name)
        user_digest = hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:8]
        connection_id = f"{base_id}-{user_digest}"
        if await self.get_connection_by_id(connection_id):
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
            async with get_db() as db:
                row = MCPConnection(**row_data)
                db.add(row)
                await db.commit()
                await db.refresh(row)
                return _public_from_row(row)
        except Exception:
            log.exception("Error creating MCP connection")
            return None

    async def get_connection_by_id(
        self, id: str, *, include_secrets: bool = False
    ) -> Optional[MCPConnectionModel | MCPConnectionWithSecrets]:
        try:
            async with get_db() as db:
                row = await db.get(MCPConnection, id)
                if not row:
                    return None
                return _with_secrets_from_row(row) if include_secrets else _public_from_row(row)
        except Exception:
            log.exception("Error getting MCP connection")
            return None

    async def get_connection_by_id_and_user_id(
        self, id: str, user_id: str, *, include_secrets: bool = False
    ) -> Optional[MCPConnectionModel | MCPConnectionWithSecrets]:
        try:
            async with get_db() as db:
                row = await db.get(MCPConnection, id)
                if not row or row.user_id != user_id:
                    return None
                return _with_secrets_from_row(row) if include_secrets else _public_from_row(row)
        except Exception:
            log.exception("Error getting MCP connection")
            return None

    async def get_connections_by_user_id(
        self, user_id: str, *, include_secrets: bool = False, enabled_only: bool = False
    ) -> list[MCPConnectionModel | MCPConnectionWithSecrets]:
        async with get_db() as db:
            stmt = select(MCPConnection).where(MCPConnection.user_id == user_id)
            if enabled_only:
                stmt = stmt.where(MCPConnection.enabled == True)
            rows = (await db.execute(stmt.order_by(MCPConnection.updated_at.desc()))).scalars().all()
            return [
                _with_secrets_from_row(row) if include_secrets else _public_from_row(row)
                for row in rows
            ]

    async def update_connection_by_id_and_user_id(
        self, id: str, user_id: str, updated: dict
    ) -> Optional[MCPConnectionModel]:
        secret_updates = {}
        updated = dict(updated)
        for field_name in ("key", "headers", "env", "oauth"):
            if field_name in updated:
                secret_updates[field_name] = encrypt_secret(updated.pop(field_name))
        try:
            async with get_db() as db:
                row = (
                    await db.execute(
                        update(MCPConnection)
                        .where(MCPConnection.id == id, MCPConnection.user_id == user_id)
                        .values({**updated, **secret_updates, "updated_at": int(time.time())})
                        .returning(MCPConnection)
                    )
                ).scalars().first()
                await db.commit()
                return _public_from_row(row) if row else None
        except Exception:
            log.exception("Error updating MCP connection")
            return None

    async def update_oauth_by_id_and_user_id(
        self, id: str, user_id: str, oauth: dict
    ) -> Optional[MCPConnectionModel]:
        return await self.update_connection_by_id_and_user_id(id, user_id, {"oauth": oauth})

    async def _mutate_oauth_under_lock(
        self, id: str, user_id: str, mutate
    ) -> bool:
        """Read-modify-write the oauth blob under a row lock (SELECT ... FOR UPDATE).

        Every token write goes through here so a concurrent refresh / callback
        cannot clobber the whole oauth blob from a stale in-memory snapshot
        (audit A2). ``mutate(oauth_dict)`` mutates the decrypted dict in place;
        returning the (possibly new) dict to persist, or None to abort.
        """
        try:
            async with get_db() as db:
                row = (
                    await db.execute(
                        select(MCPConnection)
                        .where(
                            MCPConnection.id == id,
                            MCPConnection.user_id == user_id,
                        )
                        .with_for_update()
                    )
                ).scalars().first()
                if not row:
                    return False
                oauth = decrypt_secret(row.oauth, {}) or {}
                result = mutate(oauth)
                if result is None:
                    return False
                row.oauth = encrypt_secret(result)
                row.updated_at = int(time.time())
                await db.commit()
                return True
        except Exception:
            log.exception("Error mutating MCP oauth under lock")
            return False

    async def merge_oauth_tokens_by_id_and_user_id(
        self, id: str, user_id: str, tokens: dict
    ) -> bool:
        """Atomically set ``oauth['tokens']`` (re-reading the rest of the blob).

        Prevents a stale snapshot from overwriting concurrently-written fields
        such as ``client_info`` / ``resource_metadata`` while persisting a freshly
        rotated refresh token.
        """
        def _set(oauth: dict) -> dict:
            oauth["tokens"] = tokens
            return oauth

        return await self._mutate_oauth_under_lock(id, user_id, _set)

    async def merge_oauth_fields_by_id_and_user_id(
        self, id: str, user_id: str, set_fields: dict, drop_fields: Optional[list] = None
    ) -> bool:
        """Merge auth-flow metadata into the oauth blob under the row lock.

        Used by the OAuth /start flow to persist newly-minted fields (state,
        code_verifier, client_info, ...) while leaving ``tokens`` (and any other
        untouched key) intact. Going through the SELECT..FOR UPDATE re-read means
        a concurrently-rotated refresh token is NOT clobbered by ``/start``'s
        stale pre-network snapshot (audit A2 / grant-revocation hazard).
        """

        def _merge(oauth: dict) -> dict:
            oauth.update(set_fields)
            for key in drop_fields or []:
                oauth.pop(key, None)
            return oauth

        return await self._mutate_oauth_under_lock(id, user_id, _merge)

    async def clear_oauth_tokens_by_id_and_user_id(
        self, id: str, user_id: str
    ) -> bool:
        """Atomically drop ``oauth['tokens']`` so ``authenticated`` flips to false.

        Used when a refresh fails terminally (invalid_grant): the dead tokens
        must be removed and re-auth forced, while preserving ``client_info`` so
        the next OAuth start can reuse the DCR registration.
        """
        def _clear(oauth: dict) -> dict:
            oauth.pop("tokens", None)
            oauth.pop("state", None)
            oauth.pop("code_verifier", None)
            return oauth

        return await self._mutate_oauth_under_lock(id, user_id, _clear)

    # --- Admin governance (by id, not user-scoped) -----------------------------

    async def get_all_connections(self) -> list[MCPConnectionModel]:
        """All users' connections (metadata only, no secrets) for admin oversight.

        Sets the ``authenticated`` flag for oauth_2.1 connections by inspecting
        (but never returning) the encrypted oauth blob.
        """
        async with get_db() as db:
            rows = (
                await db.execute(
                    select(MCPConnection).order_by(MCPConnection.updated_at.desc())
                )
            ).scalars().all()
            result: list[MCPConnectionModel] = []
            for row in rows:
                model = _public_from_row(row)
                if (row.auth_type or "none") == "oauth_2.1":
                    oauth = decrypt_secret(row.oauth, {}) or {}
                    model.authenticated = bool(
                        (oauth.get("tokens") or {}).get("access_token")
                    )
                result.append(model)
            return result

    async def set_enabled_by_id(
        self, id: str, enabled: bool
    ) -> Optional[MCPConnectionModel]:
        try:
            async with get_db() as db:
                row = (
                    await db.execute(
                        update(MCPConnection)
                        .where(MCPConnection.id == id)
                        .values(enabled=enabled, updated_at=int(time.time()))
                        .returning(MCPConnection)
                    )
                ).scalars().first()
                await db.commit()
                return _public_from_row(row) if row else None
        except Exception:
            log.exception("Error setting MCP connection enabled (admin)")
            return None

    async def admin_clear_oauth_tokens_by_id(self, id: str) -> bool:
        try:
            async with get_db() as db:
                row = (
                    await db.execute(
                        select(MCPConnection)
                        .where(MCPConnection.id == id)
                        .with_for_update()
                    )
                ).scalars().first()
                if not row:
                    return False
                oauth = decrypt_secret(row.oauth, {}) or {}
                oauth.pop("tokens", None)
                oauth.pop("state", None)
                oauth.pop("code_verifier", None)
                row.oauth = encrypt_secret(oauth)
                row.updated_at = int(time.time())
                await db.commit()
                return True
        except Exception:
            log.exception("Error clearing MCP oauth (admin)")
            return False

    async def delete_by_id(self, id: str) -> bool:
        try:
            async with get_db() as db:
                result = await db.execute(
                    delete(MCPConnection).where(MCPConnection.id == id)
                )
                await db.commit()
                return result.rowcount > 0
        except Exception:
            log.exception("Error deleting MCP connection (admin)")
            return False

    async def delete_connection_by_id_and_user_id(self, id: str, user_id: str) -> bool:
        try:
            async with get_db() as db:
                result = await db.execute(
                    delete(MCPConnection).where(MCPConnection.id == id, MCPConnection.user_id == user_id)
                )
                await db.commit()
                return result.rowcount > 0
        except Exception:
            log.exception("Error deleting MCP connection")
            return False


MCPConnections = MCPConnectionsTable()
