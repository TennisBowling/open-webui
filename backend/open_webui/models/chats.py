import asyncio
import copy
import difflib
import logging
from open_webui.utils import fast_json as json
import re
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Optional

from open_webui.internal.db import (
    Base,
    get_db,
    run_sync_db,
    dumps_jsonb as _dumps_jsonb,
    strip_nul as _strip_nul_value,
)
from open_webui.models.tags import TagModel, Tag
from open_webui.models.folders import Folder
from open_webui.env import SRC_LOG_LEVELS
from open_webui.utils.lazy_blocks import (
    parse_reasoning_ref,
    reasoning_body_from_blocks,
    sanitize_content_blocks,
    slim_content_blocks_for_read,
    text_only_content_from_blocks,
)

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Text, JSON, Index
from sqlalchemy import or_, func, select, and_, text, case
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import exists
from sqlalchemy.sql.expression import bindparam

####################
# Chat DB Schema
####################

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


class ChatBranchConflictError(RuntimeError):
    """A guarded branch mutation no longer matches the rows it was built from."""

    def __init__(self, message: str, *, code: str = "chat_branch_conflict"):
        super().__init__(message)
        self.code = code


class ChatHistoryConflictError(RuntimeError):
    """A guarded hidden-chat transcript mutation lost its expected leaf."""

    def __init__(self, message: str, *, code: str = "chat_history_conflict"):
        super().__init__(message)
        self.code = code


class ChatMessageParentMissingError(RuntimeError):
    """A message write references a parent that is not durable in this chat."""

    def __init__(self, message_id: str, parent_id: str, *, self_parent: bool = False):
        self.message_id = str(message_id)
        self.parent_id = str(parent_id)
        self.code = (
            "chat_message_parent_cycle"
            if self_parent
            else "chat_message_parent_missing"
        )
        super().__init__(
            f"Message {self.message_id} cannot be its own parent."
            if self_parent
            else f"Parent message {self.parent_id} is not durable in this chat."
        )


####################
# sibling_stubs memoization (get_chat_meta_by_id_and_user_id)
#
# Rebuilding the full id/parent/children stub graph on every chat open is
# real but modest work at this deployment's scale (largest chats ~a few
# hundred messages; verified against the live DB, not most chats). A
# per-worker, bounded, in-process cache is enough here -- there's no shared
# Redis in this deployment (WEBSOCKET_MANAGER is not "redis"), and the cache
# key naturally self-invalidates because `updated_at` bumps on every graph
# mutation, so a stale entry can never be served. Not shared cross-worker,
# which is fine at this scale.
####################
_SIBLING_STUBS_CACHE_MAX_ENTRIES = 300
_sibling_stubs_cache: (
    "OrderedDict[tuple[str, Any], tuple[list[dict], Optional[str]]]"
) = OrderedDict()
_sibling_stubs_cache_lock = threading.Lock()


def _get_cached_sibling_stubs(chat_id: str, updated_at: Any):
    key = (chat_id, updated_at)
    with _sibling_stubs_cache_lock:
        entry = _sibling_stubs_cache.get(key)
        if entry is not None:
            _sibling_stubs_cache.move_to_end(key)
        return entry


def _set_cached_sibling_stubs(
    chat_id: str, updated_at: Any, value: tuple[list[dict], Optional[str]]
):
    key = (chat_id, updated_at)
    with _sibling_stubs_cache_lock:
        _sibling_stubs_cache[key] = value
        _sibling_stubs_cache.move_to_end(key)
        while len(_sibling_stubs_cache) > _SIBLING_STUBS_CACHE_MAX_ENTRIES:
            _sibling_stubs_cache.popitem(last=False)


def _invalidate_cached_sibling_stubs(chat_id: str):
    """Drop every cached entry for this chat, regardless of the `updated_at`
    second it was keyed to.

    The cache key is (chat_id, updated_at) with one-second resolution, but a
    single logical turn typically writes multiple rows (user message, then
    assistant placeholder, then status updates) within the same second, each
    bumping updated_at to the same second-value. A meta/stub read landing
    between two of those writes would cache the graph as of the FIRST write
    under that second's key; a second reader later in the SAME second (e.g.
    another tab/device opening the chat mid-turn) would then hit that cache
    and see a graph missing the second write's row, until the second ticks
    over. Explicitly invalidating at every graph-mutating write site closes
    that window at the source instead of relying on the key to naturally
    change.
    """
    with _sibling_stubs_cache_lock:
        for key in [k for k in _sibling_stubs_cache if k[0] == chat_id]:
            del _sibling_stubs_cache[key]


def _tag_id(name: str) -> str:
    return name.replace(" ", "_").lower()


def _sync_get_tag_by_name_and_user_id(
    db, name: str, user_id: str
) -> Optional[TagModel]:
    tag = db.get(Tag, {"id": _tag_id(name), "user_id": user_id})
    return TagModel.model_validate(tag) if tag else None


def _sync_insert_new_tag(db, name: str, user_id: str) -> Optional[TagModel]:
    tag = Tag(id=_tag_id(name), user_id=user_id, name=name)
    try:
        db.add(tag)
        db.commit()
        db.refresh(tag)
        return TagModel.model_validate(tag)
    except Exception:
        db.rollback()
        return _sync_get_tag_by_name_and_user_id(db, name, user_id)


def _sync_delete_tag_by_name_and_user_id(db, name: str, user_id: str) -> bool:
    try:
        tag = db.get(Tag, {"id": _tag_id(name), "user_id": user_id})
        if tag is None:
            return True
        db.delete(tag)
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False


def _sync_get_tags_by_ids_and_user_id(
    db, ids: list[str], user_id: str
) -> list[TagModel]:
    if not ids:
        return []
    rows = (
        db.execute(select(Tag).where(Tag.id.in_(ids), Tag.user_id == user_id))
        .scalars()
        .all()
    )
    return [TagModel.model_validate(row) for row in rows]


def _sync_search_folder_ids_by_names(user_id: str, names: list[str]) -> list[str]:
    normalized = {name.replace("_", " ").strip().lower() for name in names if name}
    if not normalized:
        return []
    with get_db() as db:
        rows = (
            db.execute(select(Folder).where(Folder.user_id == user_id)).scalars().all()
        )
        by_parent: dict[str | None, list[Folder]] = {}
        for row in rows:
            by_parent.setdefault(row.parent_id, []).append(row)

        results: set[str] = set()

        def include_subtree(row: Folder):
            results.add(row.id)
            for child in by_parent.get(row.id, []):
                include_subtree(child)

        for row in rows:
            if row.name.replace("_", " ").strip().lower() in normalized:
                include_subtree(row)
        return list(results)


class Chat(Base):
    __tablename__ = "chat"

    id = Column(String, primary_key=True)
    user_id = Column(String)
    title = Column(Text)
    chat = Column(JSON)

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)

    share_id = Column(Text, unique=True, nullable=True)
    archived = Column(Boolean, default=False)
    pinned = Column(Boolean, default=False, nullable=True)

    meta = Column(JSON, server_default="{}")
    folder_id = Column(Text, nullable=True)

    # Denormalized from meta.subagent_of and chat.models[0] so queries can hit
    # a real index instead of unpacking the (potentially 100+ MB) chat JSON.
    subagent_of = Column(String, nullable=True)
    model_id_primary = Column(String, nullable=True)

    # Set (to the automation's id) on the fresh hidden chat each scheduled run
    # creates. A separate column from subagent_of on purpose: that one holds a
    # parent CHAT id and drives the subagent reconcilers and the delete-cascade
    # walk, none of which apply here. Its only job is hiding the run chat from
    # the sidebar/search while leaving it openable at /c/{id}.
    automation_of = Column(String, nullable=True)

    # 0 = messages still live in `chat.chat.history.messages` (legacy);
    # 1 = messages live in the `chat_message` table and are hydrated on read.
    # Default 0 keeps unmigrated chats on the legacy path.
    messages_migrated = Column(Integer, nullable=False, server_default="0", default=0)

    # Unix seconds of the last write that left `chat.chat["queue"]` non-empty;
    # NULL whenever the queue is empty. Purely a cheap, indexed restatement of
    # "this chat is owed a drain" so the reconciler (sweep_pending_queues) can
    # find those chats without a table scan — reading the queue itself means
    # detoasting every chat blob. Maintained in ONE place, _persist_queue_blob,
    # in the same locked transaction as the queue write, so it cannot drift.
    # A column and not a blob key on purpose: the blob is whole-column
    # rewritten by ordinary autosaves (see update_chat_by_id, which re-injects
    # live queue/draining/question_states by hand to survive that).
    queue_armed_at = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("folder_id_idx", "folder_id"),
        Index("user_id_pinned_idx", "user_id", "pinned"),
        Index("user_id_archived_idx", "user_id", "archived"),
        # Leading column is the equality predicate, trailing column supports
        # the sidebar ORDER BY.
        Index("user_id_updated_at_idx", "user_id", "updated_at"),
        Index("folder_id_user_id_idx", "folder_id", "user_id"),
        # Partial index for the sidebar's default chat list query.
        Index(
            "chat_sidebar_default_idx",
            "user_id",
            "updated_at",
            postgresql_where=text(
                "archived = false AND folder_id IS NULL AND "
                "(pinned = false OR pinned IS NULL) AND subagent_of IS NULL "
                "AND automation_of IS NULL"
            ),
        ),
        # Holds only automation run chats, so the automations page's
        # "open this run" links resolve without scanning the chat table.
        Index(
            "chat_automation_of_idx",
            "automation_of",
            postgresql_where=text("automation_of IS NOT NULL"),
        ),
        # Holds only the chats currently owed a queue drain (see
        # queue_armed_at), so the reconciler's every-15s lookup stays a couple
        # of buffer hits no matter how big the table gets.
        Index(
            "chat_queue_armed_idx",
            "queue_armed_at",
            postgresql_where=text("queue_armed_at IS NOT NULL"),
            sqlite_where=text("queue_armed_at IS NOT NULL"),
        ),
    )


class ChatModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    title: str
    chat: dict

    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch

    share_id: Optional[str] = None
    archived: bool = False
    pinned: Optional[bool] = False

    meta: dict = {}
    folder_id: Optional[str] = None

    subagent_of: Optional[str] = None
    automation_of: Optional[str] = None
    model_id_primary: Optional[str] = None
    messages_migrated: int = 0

    @field_validator("chat", "meta", mode="before")
    @classmethod
    def _coerce_json_dict(cls, value):
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return value


class ChatMessage(Base):
    """One row per logical chat message, replacing the per-message entries
    embedded inside ``chat.chat.history.messages``. Only authoritative when
    the parent chat has ``messages_migrated = 1``.

    ``content_is_json = 1`` indicates that ``content`` is a JSON-encoded
    structure (e.g. multimodal parts list) and should be parsed on hydrate.
    ``meta`` stores any message-level fields that don't have dedicated
    columns (followUps, reasoning_details, error, selectedModelId, etc.) so
    round-tripping preserves the original message shape.
    """

    __tablename__ = "chat_message"

    chat_id = Column(String, primary_key=True)
    message_id = Column(String, primary_key=True)
    parent_id = Column(String, nullable=True)
    role = Column(String, nullable=True)
    content = Column(Text, nullable=True)
    content_is_json = Column(Integer, default=0)
    model = Column(String, nullable=True)
    timestamp = Column(BigInteger, nullable=True)
    sequence = Column(Integer, nullable=False)
    status_history = Column(JSON, nullable=True)
    meta = Column(JSON, nullable=True)

    __table_args__ = (
        Index("chat_message_chat_seq_idx", "chat_id", "sequence"),
        Index("chat_message_chat_parent_idx", "chat_id", "parent_id"),
    )


class ChatMessageModel(BaseModel):
    """Pydantic shape that mirrors the chat_message table for API responses."""

    model_config = ConfigDict(from_attributes=True)

    chat_id: str
    message_id: str
    parent_id: Optional[str] = None
    role: Optional[str] = None
    content: Optional[str] = None
    content_is_json: Optional[int] = 0
    model: Optional[str] = None
    timestamp: Optional[int] = None
    sequence: int = 0
    status_history: Optional[Any] = None
    meta: Optional[Any] = None


####################
# Forms
####################


class ChatForm(BaseModel):
    chat: dict
    folder_id: Optional[str] = None


class ChatImportForm(ChatForm):
    meta: Optional[dict] = {}
    pinned: Optional[bool] = False
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class ChatTitleMessagesForm(BaseModel):
    title: str
    messages: list[dict]


class ChatTitleForm(BaseModel):
    title: str


class ChatResponse(BaseModel):
    id: str
    user_id: str
    title: str
    chat: dict
    updated_at: int  # timestamp in epoch
    created_at: int  # timestamp in epoch
    share_id: Optional[str] = None  # id of the chat to be shared
    archived: bool
    pinned: Optional[bool] = False
    meta: dict = {}
    folder_id: Optional[str] = None


class ChatTitleIdResponse(BaseModel):
    id: str
    title: str
    updated_at: int
    created_at: int


class ChatSearchHit(BaseModel):
    id: str
    title: str
    updated_at: int
    created_at: int
    archived: bool = False
    pinned: bool = False
    folder_id: Optional[str] = None
    snippet: Optional[str] = None  # safe HTML, only <mark> tags allowed
    match_count: int = 0
    matched_message_id: Optional[str] = None
    matched_role: Optional[str] = None
    score: float = 0.0


class FacetBucket(BaseModel):
    id: str
    name: str
    count: int


class ChatSearchFacets(BaseModel):
    folders: list[FacetBucket] = []
    tags: list[FacetBucket] = []
    models: list[FacetBucket] = []


class ChatSearchResponse(BaseModel):
    total: int = 0
    hits: list[ChatSearchHit] = []
    facets: ChatSearchFacets = ChatSearchFacets()
    used_fuzzy: bool = False
    did_you_mean: Optional[str] = None


def _extract_content_text(content) -> str:
    """Extract plain text from a message content field (string or multimodal list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _build_search_text(title: str, chat_data: dict) -> str:
    """Build a bounded title + message body for Postgres search indexes."""
    parts = [title or ""]

    history = chat_data.get("history") or {}
    history_messages = history.get("messages") if isinstance(history, dict) else None
    if history_messages and isinstance(history_messages, dict):
        for msg in history_messages.values():
            if isinstance(msg, dict):
                parts.append(_extract_content_text(msg.get("content", "")))
    elif "messages" in chat_data:
        for msg in chat_data.get("messages") or []:
            if isinstance(msg, dict):
                parts.append(_extract_content_text(msg.get("content", "")))

    # Limit to 64KB to keep DB size reasonable
    return " ".join(parts).lower()[:65536]


def _iter_chat_messages(chat_data: dict):
    """Yield (message_id, role, content_text) for every message in a chat."""
    if not isinstance(chat_data, dict):
        return
    history = chat_data.get("history") or {}
    history_messages = history.get("messages") if isinstance(history, dict) else None
    if history_messages and isinstance(history_messages, dict):
        for mid, msg in history_messages.items():
            if isinstance(msg, dict):
                yield (
                    str(mid),
                    str(msg.get("role", "")),
                    _extract_content_text(msg.get("content", "")),
                )
    elif "messages" in chat_data:
        for idx, msg in enumerate(chat_data.get("messages") or []):
            if isinstance(msg, dict):
                yield (
                    str(msg.get("id", f"_{idx}")),
                    str(msg.get("role", "")),
                    _extract_content_text(msg.get("content", "")),
                )


def _upsert_chat_search(db, chat_id: str, title: str, chat_data: dict) -> None:
    """Refresh Postgres chat/message search rows for one chat.

    Dual-read aware: if the chat is migrated to the ``chat_message`` table,
    we pull message text from there. Otherwise we fall back to iterating
    the messages embedded in ``chat_data['history']['messages']`` (legacy
    chats and freshly-inserted chats both go through this path)."""
    migrated = _is_chat_migrated(db, chat_id)
    if migrated:
        # Hydrate a shallow copy so _build_search_text sees the real messages.
        try:
            # A failed optional search read must roll back to a savepoint; merely
            # catching a PostgreSQL statement error leaves the outer chat-write
            # transaction aborted.
            with db.begin_nested():
                messages = _chat_messages_from_table(db, chat_id)
            chat_for_body = dict(chat_data) if isinstance(chat_data, dict) else {}
            history_for_body = (
                dict(chat_for_body.get("history") or {})
                if isinstance(chat_for_body.get("history"), dict)
                else {}
            )
            history_for_body["messages"] = messages
            chat_for_body["history"] = history_for_body
        except Exception:
            chat_for_body = chat_data
    else:
        chat_for_body = chat_data

    body = _build_search_text(title, chat_for_body)

    try:
        with db.begin_nested():
            db.execute(
                text(
                    """
                    INSERT INTO chat_search (chat_id, title, body)
                    VALUES (:id, :title, :body)
                    ON CONFLICT (chat_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        body = EXCLUDED.body
                    """
                ),
                {"id": chat_id, "title": title or "", "body": body or ""},
            )
            db.execute(
                text("DELETE FROM chat_message_search WHERE chat_id = :id"),
                {"id": chat_id},
            )
            for mid, role, content in _iter_chat_messages(chat_for_body):
                if content:
                    db.execute(
                        text(
                            """
                            INSERT INTO chat_message_search (chat_id, message_id, role, content)
                            VALUES (:cid, :mid, :role, :content)
                            ON CONFLICT (chat_id, message_id) DO UPDATE SET
                                role = EXCLUDED.role,
                                content = EXCLUDED.content
                            """
                        ),
                        {
                            "cid": chat_id,
                            "mid": mid,
                            "role": role or "",
                            "content": content[:65536],
                        },
                    )
    except Exception:
        # Search staleness is preferable to losing the underlying chat write.
        pass


def _upsert_message_search(
    db, chat_id: str, message_id: str, role: Optional[str], content: Optional[str]
) -> None:
    """Refresh one message search row without rebuilding the whole chat body."""
    try:
        with db.begin_nested():
            db.execute(
                text(
                    """
                    INSERT INTO chat_message_search (chat_id, message_id, role, content)
                    VALUES (:cid, :mid, :role, :content)
                    ON CONFLICT (chat_id, message_id) DO UPDATE SET
                        role = EXCLUDED.role,
                        content = EXCLUDED.content
                    """
                ),
                {
                    "cid": chat_id,
                    "mid": message_id,
                    "role": role or "",
                    "content": (content or "")[:65536],
                },
            )
    except Exception:
        # Search staleness is preferable to losing the underlying chat write.
        pass


_chat_message_table_supported_cache: Optional[bool] = None


def _chat_message_table_supported(db) -> bool:
    """True when the new ``chat_message`` table + ``messages_migrated`` column
    are both present. Cached after first probe."""
    global _chat_message_table_supported_cache
    if _chat_message_table_supported_cache is not None:
        return _chat_message_table_supported_cache
    try:
        db.execute(text("SELECT messages_migrated FROM chat LIMIT 0"))
        db.execute(text("SELECT chat_id FROM chat_message LIMIT 0"))
        _chat_message_table_supported_cache = True
    except Exception:
        _chat_message_table_supported_cache = False
    return _chat_message_table_supported_cache


def _is_chat_migrated(db, chat_id: str) -> bool:
    """Probe whether one chat is migrated to the chat_message table.

    Cheap UPDATE/INSERT paths read this once per call; the table-presence
    check is cached so the only DB work is the per-chat lookup."""
    if not _chat_message_table_supported(db):
        return False
    try:
        row = db.execute(
            text("SELECT messages_migrated FROM chat WHERE id = :id"),
            {"id": chat_id},
        ).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


# Column order used by every SELECT against chat_message — keep in sync
# with `_row_to_message_dict`. The trailing xmin is the row's version for the
# incremental-open contract: Postgres bumps it on EVERY row update, so every
# writer (present and future) rotates the client-visible `_rev` with zero
# application bookkeeping — the same authority the chat-open ETag leans on.
_CHAT_MESSAGE_SELECT_COLS = (
    "message_id, parent_id, role, content, content_is_json, "
    "model, timestamp, status_history, meta, xmin::text"
)


_JSON_VALID_ESCAPE_NEXT = set(
    '"\\/bfnrt'
)  # chars that may legally follow a backslash (besides u)
_JSON_HEX = set("0123456789abcdefABCDEF")


def _lenient_json_loads(s: str):
    """Parse JSON text that may contain invalid backslash escapes.

    Legacy double-encoded ``meta`` carried over from the SQLite->Postgres
    migration can embed raw bytes (e.g. ``\\@`` / ``\\z`` / a malformed
    ``\\uXXXX``) that strict ``json.loads`` rejects. Try strict parsing first; on
    failure, escape every backslash that does NOT begin a valid JSON escape, then
    retry. Raises if still unrecoverable (callers treat that as "unparseable").
    """
    try:
        return json.loads(s)
    except Exception:
        pass
    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt in _JSON_VALID_ESCAPE_NEXT:
                out.append(ch)
                out.append(nxt)
                i += 2
                continue
            if (
                nxt == "u"
                and i + 6 <= n
                and all(c in _JSON_HEX for c in s[i + 2 : i + 6])
            ):
                out.append(s[i : i + 6])
                i += 6
                continue
            # Invalid escape (or a trailing lone backslash): escape the backslash
            # itself so the byte survives as data instead of breaking the parse.
            out.append("\\\\")
            i += 1
            continue
        out.append(ch)
        i += 1
    return json.loads("".join(out))


def _json_from_db(value):
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        # The Postgres driver (asyncpg) returns jsonb columns as raw TEXT, so we
        # parse here. A legacy double-encoded value is a jsonb STRING scalar that
        # holds the JSON text of the real object, so one decode yields ANOTHER
        # str — iterate until we reach the object/list. Bounded so a genuine JSON
        # string value (rare for our object/list columns) can't loop forever.
        result = value
        for _ in range(6):
            if not isinstance(result, str):
                break
            try:
                result = _lenient_json_loads(result)
            except Exception:
                return None
        return result if not isinstance(result, str) else None
    return None


def _row_to_message_dict(row) -> dict:
    """Convert one chat_message row tuple (matching _CHAT_MESSAGE_SELECT_COLS)
    into the JSON-shape message dict used by the API and the legacy
    ``history.messages`` map."""
    mid = row[0]
    parent_id = row[1]
    role = row[2]
    content_raw = row[3]
    is_json = row[4]
    model = row[5]
    ts = row[6]
    status_history_raw = row[7]
    meta_raw = row[8]

    if is_json and isinstance(content_raw, str):
        try:
            content = json.loads(content_raw)
        except Exception:
            content = content_raw
    else:
        content = content_raw if content_raw is not None else ""

    msg: dict = {
        "id": mid,
        "role": role or "",
        "content": content,
    }
    if parent_id is not None:
        msg["parentId"] = parent_id
    if model:
        msg["model"] = model
    if ts is not None:
        msg["timestamp"] = ts
    if status_history_raw:
        status_history = _json_from_db(status_history_raw)
        if isinstance(status_history, list):
            msg["statusHistory"] = status_history
    if meta_raw:
        extra = _json_from_db(meta_raw)
        if isinstance(extra, dict):
            # Don't let meta clobber the dedicated columns.
            for k, v in extra.items():
                if k not in msg:
                    msg[k] = v
    # Row version (xmin::text) — read-only wire metadata for the incremental
    # open. Set LAST so a stale `_rev` that ever leaked into meta can't win;
    # `_split_message_for_table` drops it on every write path.
    if len(row) > 9 and row[9] is not None:
        msg["_rev"] = row[9]
    return msg


_SLIM_TOOL_RESULT_THRESHOLD_BYTES = 1024


def strip_tool_result_bodies_from_message(message: dict) -> dict:
    if not isinstance(message, dict):
        return message
    out = dict(message)
    out.pop("tool_result_bodies", None)
    return out


def strip_tool_result_bodies_from_chat_dict(chat: dict) -> dict:
    if not isinstance(chat, dict):
        return chat
    out = copy.deepcopy(chat)
    history = out.get("history")
    messages = history.get("messages") if isinstance(history, dict) else None
    if isinstance(messages, dict):
        for mid, msg in list(messages.items()):
            messages[mid] = strip_tool_result_bodies_from_message(msg)
    if isinstance(out.get("messages"), list):
        out["messages"] = [
            strip_tool_result_bodies_from_message(m) for m in out["messages"]
        ]
    # Strip bandwidth-heavy extracted doc text from the chat-level files array on
    # this outbound (slim) projection — same treatment as the per-message slim
    # path and the meta response. Content is re-hydrated from the Files table on
    # demand; the retrieval context-assembler reads the unstripped stored row
    # directly (this projection is only applied to API responses), so falsy ''
    # content never breaks completion context.
    if isinstance(out.get("files"), list):
        out["files"] = _strip_file_data_content_from_files(out["files"])
    return out


def strip_tool_result_bodies_from_chat_model(chat: ChatModel) -> ChatModel:
    if not chat:
        return chat
    data = chat.model_dump()
    data["chat"] = strip_tool_result_bodies_from_chat_dict(data.get("chat") or {})
    return ChatModel(**data)


def _prune_tool_result_bodies_to_refs(bodies: dict, content_blocks: list) -> dict:
    """Filter a ``tool_result_bodies`` map down to only the refs still referenced by
    ``content_blocks``. Returns a NEW dict; both inputs are read-only.

    Explicit garbage-collection for the accumulate-only body map. Union-merge
    persistence (see ``upsert_message_to_chat_by_id_and_message_id``) deliberately
    never shrinks the durable map on its own — that monotonicity is what stops a
    mid-generation RAM-store wipe from corrupting the row — but it also means a
    retried / rewound / branched turn could otherwise let orphaned bodies (whose
    slim result is no longer present in the final blocks) accumulate on the row
    forever. Only the TERMINAL persists opt into this prune: collect every ref a
    final ``tool_calls`` result still points at (``result_ref`` first, else
    ``tool_call_id``) plus every reasoning stub's ``content_ref`` (reasoning
    text shares this body map — see utils/lazy_blocks.py) and keep exactly
    those keys."""
    if not isinstance(bodies, dict):
        return {}
    if not isinstance(content_blocks, list):
        return bodies
    referenced: set = set()
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "reasoning":
            ref = block.get("content_ref")
            if ref:
                referenced.add(str(ref))
            continue
        if block.get("type") != "tool_calls":
            continue
        for result in block.get("results") or []:
            if not isinstance(result, dict):
                continue
            ref = result.get("result_ref") or result.get("tool_call_id")
            if ref:
                referenced.add(str(ref))
    return {k: v for k, v in bodies.items() if str(k) in referenced}


def carry_tool_result_bodies_from_source(
    source_message: Optional[dict], new_message: dict
) -> None:
    """Attach the source message's ``tool_result_bodies`` to ``new_message`` (in
    place), pruned to the refs its ``content_blocks`` still reference.

    Server-side body carry for rewind/retry sibling branches (the
    ``copy_tool_result_bodies_from`` field on the ``append_message`` patch op):
    the sibling's kept prefix references the SAME result_refs as the source
    turn, and every body already lives on the source row — so copying row-to-row
    here replaces the old client round-trip (one GET per kept tool call, then a
    megabytes-heavy re-upload) that made rewind take seconds on agentic turns.
    Bodies explicitly provided on ``new_message`` win per-key.

    Sources persisted BEFORE the canonical slim form may hold a referenced body
    INLINE (full result content in ``content_blocks[].results``, reasoning text
    in the block itself) instead of in the side map — the client only ever saw
    the read-slimmed stubs, so the sibling's kept prefix arrives stubbed. Those
    refs are hydrated here from the source's inline blocks, so a rewind can
    never orphan a stub on the new branch."""
    if not isinstance(new_message, dict):
        return
    if not isinstance(source_message, dict):
        return
    src_bodies = source_message.get("tool_result_bodies")
    src_bodies = src_bodies if isinstance(src_bodies, dict) else {}
    new_blocks = new_message.get("content_blocks") or []

    pruned = _prune_tool_result_bodies_to_refs(src_bodies, new_blocks)
    provided = new_message.get("tool_result_bodies")
    merged = {**pruned, **provided} if isinstance(provided, dict) else dict(pruned)

    src_blocks = source_message.get("content_blocks") or []
    for block in new_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "reasoning":
            ref = block.get("content_ref")
            if ref and ref not in merged:
                body = reasoning_body_from_blocks(src_blocks, ref)
                if body is not None:
                    merged[str(ref)] = body
            continue
        if block.get("type") != "tool_calls":
            continue
        for result in block.get("results") or []:
            if not isinstance(result, dict):
                continue
            ref = result.get("result_ref") or result.get("tool_call_id")
            if not ref or str(ref) in merged:
                continue
            if not result.get("result_lazy") and result.get("content"):
                continue  # body still inline on the new message itself
            for src_block in src_blocks:
                if (
                    not isinstance(src_block, dict)
                    or src_block.get("type") != "tool_calls"
                ):
                    continue
                for src_result in src_block.get("results") or []:
                    if (
                        isinstance(src_result, dict)
                        and (
                            src_result.get("result_ref")
                            or src_result.get("tool_call_id")
                        )
                        == ref
                        and isinstance(src_result.get("content"), str)
                        and src_result.get("content")
                    ):
                        merged[str(ref)] = dict(src_result)
                        break
                if str(ref) in merged:
                    break

    if not merged:
        return
    new_message["tool_result_bodies"] = merged


def carry_reasoning_rounds_from_source(
    source_message: Optional[dict], new_message: dict
) -> None:
    """Attach the source ROW's ``reasoning_details_per_round`` (and legacy flat
    ``reasoning_details``) to ``new_message`` in place, replacing any
    client-carried copy.

    The per-round map is not part of the v2.1 stream protocol: a client's copy
    freezes at chat-open while ``content_blocks`` keep streaming, so a
    rewind/retry sibling seeded from the client arrives with a map that is
    STALE relative to the blocks it carries. ``_expand_assistant`` indexes the
    map by emission, so a stale map attaches the wrong round's reasoning to
    every emission past the stale point — and because the desync differs
    between the source row and the sibling, every later rebuild serializes
    different bytes and permanently breaks the provider prompt cache
    mid-history. The source row's map is server truth; it wins outright."""
    if not isinstance(new_message, dict) or not isinstance(source_message, dict):
        return
    if not new_message.get("content_blocks"):
        return
    src_rounds = source_message.get("reasoning_details_per_round")
    if isinstance(src_rounds, list) and src_rounds:
        new_message["reasoning_details_per_round"] = copy.deepcopy(src_rounds)
    src_flat = source_message.get("reasoning_details")
    if src_flat and not new_message.get("reasoning_details"):
        new_message["reasoning_details"] = copy.deepcopy(src_flat)


def _prune_message_tool_result_bodies_inplace(merged: dict) -> None:
    """Apply ``_prune_tool_result_bodies_to_refs`` to ``merged`` in place when it
    carries BOTH a dict ``tool_result_bodies`` and a list ``content_blocks`` — a
    no-op otherwise. Shared by the migrated and legacy upsert branches so the
    terminal-persist GC lives in exactly one place."""
    if not isinstance(merged, dict):
        return
    bodies = merged.get("tool_result_bodies")
    content_blocks = merged.get("content_blocks")
    if isinstance(bodies, dict) and isinstance(content_blocks, list):
        merged["tool_result_bodies"] = _prune_tool_result_bodies_to_refs(
            bodies, content_blocks
        )


def _strip_container_workspace_ids(cw):
    """Drop the internal chat/message ids + content hash from a
    ``container_workspace`` metadata dict, keeping the fields the shared preview
    and sandbox-link resolution actually need (workspace_path, version,
    preview_file_id, pdf_file_id, preview_status/error)."""
    if not isinstance(cw, dict):
        return cw
    return {k: v for k, v in cw.items() if k not in ("chat_id", "message_id", "sha256")}


def _sanitize_file_descriptor_container_ids(entry) -> None:
    """Strip internal container ids from every place a file descriptor can carry
    ``container_workspace`` metadata (descriptor root, ``file.data``,
    ``file.meta.data``, descriptor ``data``). Mutates ``entry`` in place."""
    if not isinstance(entry, dict):
        return
    if isinstance(entry.get("container_workspace"), dict):
        entry["container_workspace"] = _strip_container_workspace_ids(
            entry["container_workspace"]
        )
    edata = entry.get("data")
    if isinstance(edata, dict) and isinstance(edata.get("container_workspace"), dict):
        edata["container_workspace"] = _strip_container_workspace_ids(
            edata["container_workspace"]
        )
    inner = entry.get("file")
    if isinstance(inner, dict):
        idata = inner.get("data")
        if isinstance(idata, dict) and isinstance(
            idata.get("container_workspace"), dict
        ):
            idata["container_workspace"] = _strip_container_workspace_ids(
                idata["container_workspace"]
            )
        imeta = inner.get("meta")
        if isinstance(imeta, dict):
            md = imeta.get("data")
            if isinstance(md, dict) and isinstance(md.get("container_workspace"), dict):
                md["container_workspace"] = _strip_container_workspace_ids(
                    md["container_workspace"]
                )


def _strip_container_ids_from_chat_dict(chat_dict) -> None:
    """Walk every message file descriptor in a chat blob and strip internal
    container ids. Handles both message-storage shapes and content_blocks /
    tool-call result files. Mutates ``chat_dict`` in place (call on a copy)."""
    if not isinstance(chat_dict, dict):
        return
    messages = []
    history = chat_dict.get("history")
    if isinstance(history, dict) and isinstance(history.get("messages"), dict):
        messages.extend(history["messages"].values())
    if isinstance(chat_dict.get("messages"), list):
        messages.extend(chat_dict["messages"])
    for message in messages:
        if not isinstance(message, dict):
            continue
        for entry in message.get("files") or []:
            _sanitize_file_descriptor_container_ids(entry)
        for block in message.get("content_blocks") or []:
            if not isinstance(block, dict):
                continue
            for entry in block.get("files") or []:
                _sanitize_file_descriptor_container_ids(entry)
            if block.get("type") == "tool_calls":
                for result in block.get("results") or []:
                    if isinstance(result, dict):
                        for entry in result.get("files") or []:
                            _sanitize_file_descriptor_container_ids(entry)


def sanitize_shared_chat_model(
    chat: ChatModel, share_id: Optional[str] = None
) -> ChatModel:
    """Shape a chat for the PUBLIC shared-view response.

    On top of stripping tool-result bodies, this removes owner-private data that
    a shared link should never disclose to an anonymous viewer:
      - ``meta`` is reduced to only the ``share`` sub-object (the viewer needs
        ``meta.share.mode`` for the freshness hint); private organizational
        ``tags`` and any other meta keys are dropped.
      - ``user_id`` and ``id`` are neutralised to the opaque ``share_id`` token
        (falling back to the row id) so a live-mode share never leaks the
        owner's real user UUID or the original chat's internal id — snapshot and
        live links then return identical, non-identifying id semantics.
    The stored rows are untouched — only the outbound projection is shaped.
    """
    if not chat:
        return chat
    stripped = strip_tool_result_bodies_from_chat_model(chat)
    data = stripped.model_dump()

    # Strip internal container ids (chat_id/message_id/sha256) from every
    # message file descriptor so a generated-file share cannot leak the original
    # chat/message ids the top-level id-neutralisation deliberately hides.
    _strip_container_ids_from_chat_dict(data.get("chat"))

    # Shared views get the same lazy contract as the owner's chat open: tool
    # result bodies and heavy reasoning text ship as stubs and hydrate through
    # the share-scoped lazy endpoint (which reads the share row, where the
    # bodies remain). Anonymous viewers on slow links otherwise re-download
    # megabytes of collapsed-by-default content.
    share_history = (data.get("chat") or {}).get("history")
    share_messages = (
        share_history.get("messages") if isinstance(share_history, dict) else None
    )
    if isinstance(share_messages, dict):
        for mid, msg in list(share_messages.items()):
            if not isinstance(msg, dict):
                continue
            blocks = msg.get("content_blocks")
            if isinstance(blocks, list) and blocks:
                new_msg = dict(msg)
                new_msg["content_blocks"] = slim_content_blocks_for_read(blocks)
                content = new_msg.get("content")
                if isinstance(content, str) and content and "<details " in content:
                    # Legacy mirror row not yet rewritten by the canonicalize
                    # script: derive the canonical text-only content instead
                    # of shipping HTML.
                    new_msg["content"] = text_only_content_from_blocks(blocks)
                share_messages[mid] = new_msg

    meta = data.get("meta")
    share_meta = meta.get("share") if isinstance(meta, dict) else None
    data["meta"] = {"share": share_meta} if isinstance(share_meta, dict) else {}

    neutral_id = share_id or data.get("id")
    data["user_id"] = f"shared-{neutral_id}"
    if share_id:
        data["id"] = share_id
    return ChatModel(**data)


_SLIM_FILE_CONTENT_THRESHOLD_BYTES = 1024


def _strip_file_data_content_from_message(out: dict) -> dict:
    """Drop bandwidth-heavy extracted document text from a message on the slim
    read path.

    ``files[*].file.data.content`` holds the full extracted text of an uploaded
    document (a 30-page PDF is 50–150 KB). It is duplicated in the durable Files
    table, so it does NOT need to ride along on every chat open: the frontend
    re-hydrates it on demand via ``getFileById`` (FileItemModal / FilePreview),
    and the completion context-assembler falls back to ``Files.get_file_by_id``
    when the inline copy is empty (retrieval/utils.py:588). We replace large
    inline content with an EMPTY STRING (kept falsy on purpose so that fallback
    triggers) plus a ``content_stripped`` marker, instead of re-shipping it.

    Only the outbound API projection is touched — the stored row keeps the full
    content, so ``slim=false`` reads (admin/export) are unaffected.
    """
    files = out.get("files")
    new_files = _strip_file_data_content_from_files(files)
    if new_files is not files:
        out = dict(out)
        out["files"] = new_files
    return out


def _strip_file_data_content_from_files(files):
    """Replace large inline extracted-doc content in a file-descriptor array with
    an empty string. Shared by the per-message slim path and the chat-level
    ``files`` array (chat open / meta). Returns the SAME list object when nothing
    changed, and a new list otherwise (callers rely on identity to know if a
    copy is needed). Empty content stays ``''`` (falsy) on purpose so the
    retrieval context-assembler's ``Files.get_file_by_id`` fallback still fires;
    collection-type entries without inline content are left untouched."""
    if not isinstance(files, list) or not files:
        return files
    new_files = []
    changed = False
    for f in files:
        file_obj = f.get("file") if isinstance(f, dict) else None
        data = file_obj.get("data") if isinstance(file_obj, dict) else None
        content = data.get("content") if isinstance(data, dict) else None
        if (
            isinstance(content, str)
            and len(content.encode("utf-8")) > _SLIM_FILE_CONTENT_THRESHOLD_BYTES
        ):
            new_f = copy.deepcopy(f)
            new_data = new_f["file"]["data"]
            new_data["content_size"] = len(content.encode("utf-8"))
            new_data["content_stripped"] = True
            new_data["content"] = ""
            new_files.append(new_f)
            changed = True
        else:
            new_files.append(f)
    return new_files if changed else files


def _project_message_slim(
    msg: dict, is_current_leaf: bool = False, is_current_branch: bool = True
) -> dict:
    """Return a copy of ``msg`` with bandwidth-heavy fields stripped.

    - ALWAYS preserves ``originalContent`` — it carries the pre-edit version
      of edited messages and is critical for branch/edit history. Stripping
      it on non-current-leaf messages would lose user edit-history shadows
      on branch switches, so it must never be dropped on the slim path.
    - drops ``reasoning_details_per_round`` AND the flat ``reasoning_details``
      for non-leaf messages. Model replay always reads the stored row (both
      fields stay in the DB); the only client consumer is the rewind/retry
      context builder, which hydrates them on demand from the siblings
      endpoint (served with ``is_current_leaf=True``) before rebasing. On a
      60-message agentic tail these fields alone were ~10% of the payload.
    - slims ``content_blocks`` to the canonical lazy form (tool result bodies
      and closed reasoning text over the inline thresholds become fetch-on-
      expand stubs — see utils/lazy_blocks.py). A no-op for rows persisted in
      canonical form; only pre-canonical rows pay the copy.
    - projects legacy flattened ``content`` mirrors (rows not yet rewritten by
      the canonicalize script — still containing ``<details ...>`` markup) to
      the canonical text-only form derived from ``content_blocks``. Rendering,
      copy, TTS and edit all derive from ``content_blocks`` when present, so
      the HTML mirror was pure dead weight on the wire (~50% of a large tail).
      Rows already rewritten to text-only ``content`` pass through untouched.
    - replaces ``tool_calls[*].results[*].content`` larger than
      ``_SLIM_TOOL_RESULT_THRESHOLD_BYTES`` with a ``{tool_call_id, truncated,
      size}`` stub for non-current-branch turns; frontend hydrates on branch
      switch via the ``/chats/{id}/messages/{message_id}/siblings`` endpoint
      (``get_message_siblings``) which returns full, non-slim content.
    - strips large ``files[*].file.data.content`` (extracted document text) on
      ALL slim messages — it is re-hydrated from the Files table on demand, so
      re-shipping it on every chat open is pure waste (see
      ``_strip_file_data_content_from_message``).
    """
    if not isinstance(msg, dict):
        return msg
    out = strip_tool_result_bodies_from_message(msg)
    # NOTE: do NOT pop "originalContent" — see docstring.
    # Large web tool bodies are fetched lazily through the tool-result endpoint;
    # never include them in normal chat-message list payloads.
    if not is_current_leaf:
        out.pop("reasoning_details_per_round", None)
        out.pop("reasoning_details", None)

    blocks = out.get("content_blocks")
    if isinstance(blocks, list) and blocks:
        slim_blocks = slim_content_blocks_for_read(blocks)
        if slim_blocks is not blocks:
            out["content_blocks"] = slim_blocks
        content = out.get("content")
        if isinstance(content, str) and content and "<details " in content:
            # Legacy mirror row not yet rewritten by the canonicalize script:
            # derive the canonical text-only content instead of shipping HTML.
            out["content"] = text_only_content_from_blocks(blocks)

    # Legacy subagent_runs entries persisted whole inner transcripts on the
    # parent message (observed up to 27MB on a single row). The modern writer
    # stores only final_text + status; the card fetches the transcript from the
    # subagent's own (hidden) chat on expand. Project legacy entries down to
    # the modern shape — the stored row keeps everything.
    runs = out.get("subagent_runs")
    if isinstance(runs, dict):
        slim_runs = None
        for sid, run in runs.items():
            if isinstance(run, dict) and ("content_blocks" in run or "messages" in run):
                if slim_runs is None:
                    slim_runs = dict(runs)
                slim_runs[sid] = {
                    k: v
                    for k, v in run.items()
                    if k not in ("content_blocks", "messages")
                }
        if slim_runs is not None:
            out["subagent_runs"] = slim_runs

    if not is_current_branch:
        tool_calls = out.get("tool_calls")
        if isinstance(tool_calls, list):
            new_tcs = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    new_tcs.append(tc)
                    continue
                results = tc.get("results")
                if not isinstance(results, list):
                    new_tcs.append(tc)
                    continue
                new_results = []
                for r in results:
                    if not isinstance(r, dict):
                        new_results.append(r)
                        continue
                    content = r.get("content")
                    try:
                        content_str = (
                            content
                            if isinstance(content, str)
                            else json.dumps(content, default=str)
                        )
                        size = len(content_str.encode("utf-8"))
                    except Exception:
                        size = 0
                    if size > _SLIM_TOOL_RESULT_THRESHOLD_BYTES:
                        new_results.append(
                            {
                                "tool_call_id": r.get("tool_call_id") or tc.get("id"),
                                "truncated": True,
                                "size": size,
                            }
                        )
                    else:
                        new_results.append(r)
                new_tc = dict(tc)
                new_tc["results"] = new_results
                new_tcs.append(new_tc)
            out["tool_calls"] = new_tcs
    out = _strip_file_data_content_from_message(out)
    return out


def _normalize_message_graph(messages: dict) -> dict:
    """Repair lightweight graph invariants for legacy/corrupt rows.

    A stream-v2.1 race could create the assistant row via realtime upsert before
    the frontend's placeholder append landed. Those rows have saved
    content_blocks/model data but no role/parentId, which breaks branch
    pagination and makes the UI repeatedly try to load ancestors that the
    backend cannot find. Normalize the in-memory API shape so reads remain
    usable, and later writes will persist the corrected graph.
    """
    if not isinstance(messages, dict) or not messages:
        return messages

    ordered = list(messages.items())
    for _mid, msg in ordered:
        if not isinstance(msg, dict):
            continue
        if not isinstance(msg.get("childrenIds"), list):
            msg["childrenIds"] = []
        if not msg.get("role") and (
            msg.get("model")
            or msg.get("selectedModelId")
            or isinstance(msg.get("content_blocks"), list)
        ):
            msg["role"] = "assistant"

    for idx, (mid, msg) in enumerate(ordered):
        if (
            not isinstance(msg, dict)
            or msg.get("role") != "assistant"
            or msg.get("parentId")
        ):
            continue
        msg_ts = msg.get("timestamp") if isinstance(msg.get("timestamp"), int) else None
        parent_id = None
        for _pmid, candidate in reversed(ordered[:idx]):
            if not isinstance(candidate, dict) or candidate.get("role") != "user":
                continue
            cand_ts = (
                candidate.get("timestamp")
                if isinstance(candidate.get("timestamp"), int)
                else None
            )
            if msg_ts is None or cand_ts is None or cand_ts <= msg_ts:
                parent_id = candidate.get("id") or _pmid
                break
        if parent_id is None:
            continue
        msg["parentId"] = parent_id
        parent = messages.get(parent_id)
        if isinstance(parent, dict):
            children = (
                parent.get("childrenIds")
                if isinstance(parent.get("childrenIds"), list)
                else []
            )
            if mid not in children:
                children.append(mid)
            parent["childrenIds"] = children

    # parentId is the normalized relationship stored in its own column;
    # childrenIds is a denormalized UI convenience carried in meta. Rebuild it
    # on every migrated read so an interrupted/older writer cannot hide a
    # sibling by leaving the parent's cached child list stale.
    derived_children: dict[str, list[str]] = {
        str(mid): [] for mid, msg in ordered if isinstance(msg, dict)
    }
    for mid, msg in ordered:
        if not isinstance(msg, dict):
            continue
        parent_id = msg.get("parentId")
        if parent_id is None:
            continue
        parent_key = str(parent_id)
        message_key = str(mid)
        if parent_key != message_key and parent_key in derived_children:
            derived_children[parent_key].append(message_key)
    for mid, msg in ordered:
        if isinstance(msg, dict):
            msg["childrenIds"] = derived_children.get(str(mid), [])

    return messages


def _validate_message_graph_for_sync(messages: dict) -> None:
    """Reject destructive full-table syncs built from an incomplete graph.

    ``_sync_messages_to_table`` replaces every row for a chat.  A missing
    parent in its input therefore means the caller is holding a partial/stale
    snapshot, not that deleting the durable parent is acceptable.  Validate
    the complete graph *before* the first DELETE so a failed legacy save rolls
    back intact instead of silently committing a truncated history.
    """
    if not isinstance(messages, dict):
        raise ValueError("Chat history messages must be a mapping.")

    for mid, message in messages.items():
        if not isinstance(message, dict):
            raise ValueError(f"Chat message {mid} must be an object.")
        embedded_id = message.get("id")
        if embedded_id is not None and str(embedded_id) != str(mid):
            raise ValueError(f"Chat message key {mid} does not match id {embedded_id}.")

    message_ids = {str(mid) for mid in messages}
    parents: dict[str, Optional[str]] = {}
    for mid, message in messages.items():
        if not isinstance(message, dict):
            continue
        message_id = str(mid)
        parent_raw = message.get("parentId")
        parent_id = str(parent_raw) if parent_raw is not None else None
        if parent_id == message_id:
            raise ChatMessageParentMissingError(
                message_id, parent_id, self_parent=True
            )
        if parent_id is not None and parent_id not in message_ids:
            raise ChatMessageParentMissingError(message_id, parent_id)
        parents[message_id] = parent_id

    # Each node has at most one parent, so a parent-pointer walk detects every
    # possible cycle without trusting the denormalized childrenIds arrays.
    settled: set[str] = set()
    for start_id in parents:
        path: set[str] = set()
        cursor: Optional[str] = start_id
        while cursor is not None and cursor not in settled:
            if cursor in path:
                raise ChatMessageParentMissingError(cursor, cursor, self_parent=True)
            path.add(cursor)
            cursor = parents.get(cursor)
        settled.update(path)


def _pick_fallback_leaf(messages: dict) -> Optional[str]:
    """Pick a sensible leaf id when ``currentId`` is missing or dangling.

    Builds a children index from each row's ``parentId`` (rather than trusting
    the possibly-stale ``childrenIds`` field) and returns the newest message
    that has no children — i.e. an actual leaf of the tree. Falls back to the
    last-inserted message if no clean leaf is found.
    """
    if not isinstance(messages, dict) or not messages:
        return None

    has_children: set[str] = set()
    for m in messages.values():
        if not isinstance(m, dict):
            continue
        pid = m.get("parentId")
        if pid is not None and pid in messages:
            has_children.add(pid)

    last_leaf: Optional[str] = None
    for mid, m in messages.items():
        if not isinstance(m, dict):
            continue
        if mid not in has_children:
            last_leaf = mid
    if last_leaf is not None:
        return last_leaf

    # Pathological: every message has a child (cycle). Use last-inserted.
    try:
        return next(reversed(messages))
    except StopIteration:
        return None


def _chat_messages_from_table(db, chat_id: str) -> dict:
    """Reconstruct ``{message_id: message_dict}`` from chat_message rows for
    the given chat. Ordered by ``sequence`` so the dict iteration order
    matches the original message order.

    Returns ``{}`` if the chat has no rows (or the table isn't there)."""
    if not _chat_message_table_supported(db):
        return {}
    try:
        rows = db.execute(
            text(
                f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                "FROM chat_message WHERE chat_id = :cid ORDER BY sequence"
            ),
            {"cid": chat_id},
        ).fetchall()
    except Exception:
        return {}
    return _normalize_message_graph({r[0]: _row_to_message_dict(r) for r in rows})


def _hydrate_chat_messages(db, chat_obj) -> None:
    """If ``chat_obj`` is migrated, populate
    ``chat_obj.chat['history']['messages']`` from the chat_message table so
    callers that read the JSON shape continue to work. No-op for unmigrated
    chats (their JSON blob still holds the messages)."""
    if chat_obj is None:
        return
    # `chat_obj` is a SQLAlchemy ORM row; the messages_migrated attribute
    # might be missing on older sessions, so check defensively.
    try:
        migrated = bool(getattr(chat_obj, "messages_migrated", 0))
    except Exception:
        migrated = False
    if not migrated:
        return
    try:
        msgs = _chat_messages_from_table(db, chat_obj.id)
    except Exception:
        return
    # Modify the dict in place. SQLAlchemy may flush this back on commit;
    # since the messages_migrated flag will gate further writes there's no
    # risk of stale data, but we only mutate the in-memory dict that's about
    # to be serialized for the caller.
    chat_dict = chat_obj.chat if isinstance(chat_obj.chat, dict) else {}
    history = (
        chat_dict.get("history") if isinstance(chat_dict.get("history"), dict) else {}
    )
    history["messages"] = msgs
    # Repair currentId when missing OR dangling (points at an id not in msgs).
    # A dangling currentId is what produces the "chat cut off" symptom: the
    # frontend walk from history.currentId immediately hits an orphan and the
    # Loader gate's `history.messages[parentId] !== undefined` check is false,
    # so there's no recovery affordance. Save races (set_history_current_id
    # PATCHed before its append_message) are the usual cause.
    if msgs:
        current_id = history.get("currentId")
        if current_id is None or current_id not in msgs:
            fallback = _pick_fallback_leaf(msgs)
            if fallback is not None:
                if current_id is not None and current_id != fallback:
                    log.warning(
                        "Repaired dangling currentId=%s for chat=%s → %s",
                        current_id,
                        getattr(chat_obj, "id", "?"),
                        fallback,
                    )
                history["currentId"] = fallback
    chat_dict["history"] = history
    chat_obj.chat = chat_dict


def _peel_messages_off_chat_dict(
    chat_data: dict,
) -> tuple[dict, Optional[dict]]:
    """If ``chat_data['history']['messages']`` is a dict, return a copy of
    ``chat_data`` with that key removed along with the popped messages.
    Otherwise return ``(chat_data, None)``.

    Used by every write path that puts messages in the chat_message table
    so the on-disk JSON blob stays small."""
    if not isinstance(chat_data, dict):
        return chat_data, None
    history_in = chat_data.get("history") or {}
    msgs = history_in.get("messages") if isinstance(history_in, dict) else None
    if not isinstance(msgs, dict):
        return chat_data, None
    stored = dict(chat_data)
    stored_history = dict(history_in)
    stored_history.pop("messages", None)
    stored["history"] = stored_history
    return stored, msgs


def _next_sequence_for_chat(db, chat_id: str) -> int:
    """Return one past the current max(sequence) for the chat — the value to
    use for an INSERTed new message."""
    try:
        row = db.execute(
            text(
                "SELECT COALESCE(MAX(sequence), -1) + 1 FROM chat_message WHERE chat_id = :cid"
            ),
            {"cid": chat_id},
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _append_identity_matches(existing: dict, incoming: dict) -> bool:
    """Return whether an existing row is the same append retried over HTTP.

    Assistant placeholders carry stable generation/turn ids while streaming
    fields evolve. Other messages must match their immutable ancestry, role,
    content, attachment, and model inputs.
    """
    if existing.get("parentId") != incoming.get("parentId") or existing.get(
        "role"
    ) != incoming.get("role"):
        return False

    generation_id = incoming.get("generation_id")
    turn_id = incoming.get("turn_id")
    if generation_id and turn_id:
        return str(existing.get("generation_id") or "") == str(
            generation_id
        ) and str(existing.get("turn_id") or "") == str(turn_id)

    return all(
        existing.get(key) == incoming.get(key)
        for key in ("content", "model", "models", "files")
    )


def _split_message_for_table(message: dict) -> dict:
    """Slice an incoming message dict into the column shape used by
    chat_message. Returns a kwargs dict ready for INSERT/UPDATE binds.

    NUL sanitization for the jsonb/text binds is handled by ``_dumps_jsonb`` /
    ``_strip_nul_value`` (imported from ``internal.db`` so the engine-level
    ``json_serializer`` and these raw-SQL binds share one implementation)."""
    content = message.get("content", "")
    is_json = 0
    if not isinstance(content, str):
        try:
            content = _dumps_jsonb(content)
            is_json = 1
        except Exception:
            content = ""
            is_json = 0
    elif "\x00" in content:
        # The ``content`` TEXT column rejects a raw NUL byte.
        content = content.replace("\x00", "")

    ts_raw = message.get("timestamp")
    try:
        ts = int(ts_raw) if ts_raw is not None and ts_raw != "" else None
    except (TypeError, ValueError):
        ts = None

    parent_id = message.get("parentId")
    if parent_id is not None:
        parent_id = str(parent_id)

    model = message.get("model")
    model_str = str(model) if model else None

    status_history = message.get("statusHistory")
    status_history_json = (
        _dumps_jsonb(status_history) if status_history is not None else None
    )

    meta = {
        k: v
        for k, v in message.items()
        if k
        not in (
            "id",
            "parentId",
            "role",
            "content",
            "model",
            "timestamp",
            "statusHistory",
            # Read-only wire metadata (row xmin stamped by _row_to_message_dict
            # for the incremental open) — must never persist into meta, or a
            # frozen copy would shadow the live row version forever.
            "_rev",
        )
    }
    # `meta` is always a dict (built by the comprehension above). Guard anyway so
    # a future caller can never persist `meta` as a jsonb STRING scalar — the
    # double-encode corruption that `_json_from_db` has to defensively recover.
    if not meta:
        meta_json = None
    elif isinstance(meta, dict):
        meta_json = _dumps_jsonb(meta)
    else:
        log.warning(
            "_split_message_for_table: non-dict meta (%s); coercing to avoid jsonb-string",
            type(meta).__name__,
        )
        meta_json = _dumps_jsonb(meta) if isinstance(meta, (list,)) else None

    return {
        "parent_id": parent_id,
        "role": str(message.get("role", "")) or None,
        "content": content,
        "content_is_json": is_json,
        "model": model_str,
        "timestamp": ts,
        "status_history": status_history_json,
        "meta": meta_json,
    }


def _require_durable_message_parent(
    db,
    *,
    chat_id: str,
    message_id: str,
    parent_id: Optional[str],
    migrated: bool,
    legacy_messages: Optional[dict] = None,
) -> None:
    """Enforce the graph's parent-before-child durability invariant.

    The caller holds the chat-row lock, so a parent that exists here cannot be
    concurrently deleted before the child write commits.
    """
    if parent_id is None or parent_id == "":
        return
    parent_id = str(parent_id)
    message_id = str(message_id)
    if parent_id == message_id:
        raise ChatMessageParentMissingError(message_id, parent_id, self_parent=True)

    if migrated:
        parent_exists = (
            db.execute(
                text(
                    "SELECT 1 FROM chat_message "
                    "WHERE chat_id = :cid AND message_id = :pid"
                ),
                {"cid": chat_id, "pid": parent_id},
            ).first()
            is not None
        )
    else:
        parent_exists = isinstance(legacy_messages, dict) and isinstance(
            legacy_messages.get(parent_id), dict
        )

    if not parent_exists:
        raise ChatMessageParentMissingError(message_id, parent_id)


def _strip_prefix_syntax(
    search_text: str, user_id: str
) -> tuple[list[str], list[str], Optional[bool], Optional[bool], Optional[bool], str]:
    """Pull out hidden `tag:` / `folder:` / `pinned:` / `archived:` / `shared:`
    qualifiers from the raw text and return (tag_ids, folder_ids,
    pinned, archived, shared, remaining_text)."""
    words = search_text.split(" ")
    tag_ids = [
        w.replace("tag:", "").replace(" ", "_").lower()
        for w in words
        if w.startswith("tag:")
    ]
    folder_names = [w.replace("folder:", "") for w in words if w.startswith("folder:")]
    folder_ids = _sync_search_folder_ids_by_names(user_id, folder_names)

    pinned: Optional[bool] = None
    if "pinned:true" in words:
        pinned = True
    elif "pinned:false" in words:
        pinned = False

    archived: Optional[bool] = None
    if "archived:true" in words:
        archived = True
    elif "archived:false" in words:
        archived = False

    shared: Optional[bool] = None
    if "shared:true" in words:
        shared = True
    elif "shared:false" in words:
        shared = False

    remaining = " ".join(
        w
        for w in words
        if not (
            w.startswith("tag:")
            or w.startswith("folder:")
            or w.startswith("pinned:")
            or w.startswith("archived:")
            or w.startswith("shared:")
        )
    ).strip()
    return (tag_ids, folder_ids, pinned, archived, shared, remaining)


def _escape_html_text(s: str) -> str:
    """HTML-escape user content for safe interpolation alongside <mark>."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# Postgres ts_headline emits real `<mark>` and `</mark>` tags around hits. We HTML-escape
# everything else after the fact by splitting on the mark tags, escaping the
# segments, then re-joining. This is XSS-safe and trivial.
def _sanitize_snippet(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    parts = raw.split("<mark>")
    out: list[str] = []
    for i, part in enumerate(parts):
        if i == 0:
            out.append(_escape_html_text(part))
            continue
        inner, _, after = part.partition("</mark>")
        out.append("<mark>")
        out.append(_escape_html_text(inner))
        out.append("</mark>")
        out.append(_escape_html_text(after))
    return "".join(out)


def _fuzzy_snippet(raw: Optional[str], query: str) -> Optional[str]:
    """Build a short, safe excerpt around the closest word to a fuzzy query.

    ``ts_headline`` cannot highlight trigram matches because they do not have a
    tsquery. Find the closest word in Python, then mark a query-sized window
    around it. This only runs for the bounded, zero-lexical-results fallback.
    """
    if not raw or not query:
        return None

    words = list(re.finditer(r"\w+", raw, flags=re.UNICODE))
    query_words = re.findall(r"\w+", query.lower(), flags=re.UNICODE)
    if not words or not query_words:
        return None

    # A matching phrase normally contains at least one exact or near-exact
    # anchor. Locating that anchor first avoids an expensive all-windows scan of
    # a 64 KiB message while still putting the useful context in the excerpt.
    anchor_idx = max(
        range(len(words)),
        key=lambda idx: max(
            difflib.SequenceMatcher(
                None, query_word, words[idx].group(0).lower(), autojunk=False
            ).ratio()
            for query_word in query_words
        ),
    )
    window_words = max(1, len(query_words))
    mark_start_idx = max(0, anchor_idx - (window_words // 2))
    mark_end_idx = min(len(words) - 1, mark_start_idx + window_words - 1)
    mark_start = words[mark_start_idx].start()
    mark_end = words[mark_end_idx].end()

    excerpt_start = max(0, mark_start - 80)
    excerpt_end = min(len(raw), mark_end + 100)
    prefix = "…" if excerpt_start else ""
    suffix = "…" if excerpt_end < len(raw) else ""
    return (
        prefix
        + _escape_html_text(raw[excerpt_start:mark_start])
        + "<mark>"
        + _escape_html_text(raw[mark_start:mark_end])
        + "</mark>"
        + _escape_html_text(raw[mark_end:excerpt_end])
        + suffix
    )


def _apply_subagent_filter(query, db, include_subagents: bool):
    """Filter out chats whose ``subagent_of`` is set — those are research
    subagent chats spawned by the parent chat model and are hidden from the
    user's main chat list / search / pinned / archived views by default.

    ``automation_of`` chats (one per scheduled-task run) are hidden by exactly
    the same rule and for exactly the same reason: they are machine-created
    conversations the user reaches from their own surface (the automations
    page), not chats they started. Both predicates live here so every ORM call
    site gets them from one place."""
    if include_subagents:
        return query
    return query.filter(Chat.subagent_of.is_(None), Chat.automation_of.is_(None))


# ── Chat-search relevance tuning ─────────────────────────────────────────────
# Every rank term is normalized to [0,1) by ``ts_rank_cd(..., 32)`` (flag 32 =
# rank/(rank+1)), so the weights below are directly comparable rather than the
# old raw-ts_rank sum where bulk term-frequency in huge agentic chats dominated.
# The final relevance score is:
#     ( W_MSG*msg_rank + title_tier + min(BREADTH_COEFF*ln(1+n), BREADTH_CAP) )
#     * (1 + RECENCY_AMP * exp(-(now - updated_at) / RECENCY_TAU))
# Chats are ranked from the always-fresh ``chat_message_search`` rows + the live
# ``chat.title`` — NOT the ``chat_search.body`` blob, which goes stale on long
# streaming chats because the per-message write path never rebuilds it.
_SEARCH_W_MSG = 2.0  # best matching message (MAX normalized ts_rank_cd)
_SEARCH_W_TITLE_FTS = 2.5  # all query tokens appear in the title, in any order
_SEARCH_TITLE_EXACT = 3.0  # lower(title) == lower(query)
_SEARCH_TITLE_PREFIX = 1.5  # title starts with the query
_SEARCH_TITLE_CONTAINS = 0.8  # query is a substring of the title
# NOTE: title matching deliberately uses exact/prefix/substring only — NOT the
# pg_trgm `title % :q` similarity operator. That operator is unindexable inside
# the per-user candidate set and costs ~100ms/search on a 3500-chat user (it is
# computed per row); it also leaked zero-token matches on multi-word queries.
# Typo tolerance belongs in an explicit on-demand fallback, not the hot path.
_SEARCH_BREADTH_COEFF = 0.1  # * ln(1 + match_count): diminishing breadth reward
_SEARCH_BREADTH_CAP = 0.5  # hard cap so a 300-msg chat can't dwarf a precise hit
_SEARCH_RECENCY_AMP = 0.5  # newest chats get up to +50%, old ones ~+0%
_SEARCH_RECENCY_TAU = 2.6e6  # ~30-day decay; chat.updated_at is EPOCH SECONDS
_SEARCH_RRF_K = (
    30  # reciprocal-rank-fusion constant; lower = sharper top-rank dominance
)
_SEARCH_SEM_MSG_POOL = (
    1000  # top in-scope message embeddings fetched per semantic ANN query
)
# Semantic-fusion calibration (qwen3-vl-embedding-8b). Adversarial audit (2026-06-22) found this
# embedder compresses TRUE hits to cosine 0.08–0.20 while off-topic queries floor at ~0.42, so a
# single 0.40 ceiling admitted 540–660 of 1000 pooled candidates — flooding the hybrid with junk
# that then buried real lexical matches and broke sort=recent. Defence in depth, not one threshold:
#   - _SEARCH_SEM_MAX_DIST: drop the weakest/noise band outright (modest, since the cap does the work).
#   - _SEARCH_SEM_FUSE_LIMIT: HARD cap on how many (nearest) chats may inject a semantic term, so a
#     topic-dense corpus can't balloon the candidate set regardless of distance distribution.
#   - _SEARCH_SEM_WEIGHT: lexical prior (<1) — a keyword match outranks an equally-ranked pure-semantic chat.
# NB: this embedder also puts short/contentless fragments at VERY low distances (0.08–0.15), so no
# distance bar separates a typo/fragment from a real hit — sort=recent therefore restricts to lexical
# (keyword) matches only (see _search_chats_postgres_fts), since recency has no score to demote junk.
_SEARCH_SEM_MAX_DIST = 0.35
_SEARCH_SEM_FUSE_LIMIT = 50
_SEARCH_SEM_WEIGHT = 0.9
# Min substantive content length (chars, after stripping assistant <details> scaffolding) for a
# message embedding to be eligible as a SEMANTIC match. Audit (2026-06-22): contentless fragments
# ("whatever", "which one", "umm what") form a degenerate centroid sitting CLOSER (cosine 0.08–0.27)
# to typo/crosslingual/OOD queries than any real topic — and no distance bar separates them. Set to 14
# (not 30): the tight-matching junk is all <=13 chars, while legitimate short USER question-anchors
# ("who do i bet on" 15, "what is operation car wash" 26) are 15+; 14 strips the former and keeps the
# latter (a 30 floor regressed those into false-empties). Crosslingual junk is handled by the 0.35
# ceiling regardless. Mirrors CHAT_EMBED_MIN_CHARS in backfill_chat_embeddings.py.
_SEARCH_SEM_MIN_MSG_LEN = 14

# English stopwords dropped from the FTS query (NOT from title matching). websearch_to_tsquery
# ('simple') keeps function words and ANDs every term, so a giant pasted blob containing scattered
# common words ("who"/"should"/"on") matches almost any NL question and the lexical prior floats it
# over the real semantic hit. Dropping stopwords from the body tsquery defeats that without the
# migration a 'simple'->'english' regconfig change would need (the stored tsvector is generated 'simple').
_SEARCH_STOPWORDS = frozenset(
    """a an and any are as at be because been being but by can could did do does doing for from had
    has have having he her here hers him his how i if in into is it its just me my no nor not of off on
    once or other our out over own she should so some such than that the their them then there these
    they this those to too under until up very was we were what when where which while who whom why
    will with would you your""".split()
)


def _fts_query_text(raw_text: str) -> str:
    """Drop stopwords from a query before the body tsquery so a blob can't AND-match on scattered
    function words. Distinctive queries (no stopwords) are unchanged; an all-stopword query keeps its
    original text (never emptied); queries using FTS phrase operators (quotes) pass through untouched.
    """
    if not raw_text or '"' in raw_text:
        return raw_text
    kept = [w for w in raw_text.split() if w not in _SEARCH_STOPWORDS]
    return " ".join(kept) if kept else raw_text


def _pgvector_literal(vec: list[float]) -> str:
    """Format a vector as a pgvector text literal (``[f1,f2,...]``) for ``::vector`` casts.
    None / non-finite components are coerced to 0 so a malformed query embedding can't
    500 the search request."""
    import math

    return (
        "["
        + ",".join(
            (
                repr(float(x))
                if (isinstance(x, (int, float)) and math.isfinite(x))
                else "0"
            )
            for x in vec
        )
        + "]"
    )


_MIN_FTS_QUERY_LEN = 2  # single-char ASCII queries fall back to the plain list
_SEARCH_STMT_TIMEOUT_MS = 5000  # per-search Postgres statement_timeout (SET LOCAL)
_SEARCH_FUZZY_MAX_QUERY_LEN = 160
_SEARCH_FUZZY_WORD_THRESHOLD = 0.45


def merge_container_outputs_state(
    existing_outputs: dict, outputs_updates: dict
) -> dict:
    """Pure merge of container output-ledger entries (the testable core of
    ``ChatTable.merge_container_workspace_outputs``).

    ``existing_outputs`` is the live ledger (state_key -> version state) re-read
    under the row lock; ``outputs_updates`` is the subset this import changed.
    Returns the merged ledger. Rules, designed so concurrent imports converge
    rather than clobber:

    * SAME ``last_hash`` already recorded for a key (a concurrent import beat us, or
      this is a stat-only refresh): keep the existing version/versions/file_id; only
      advance the cheap stat cache so the incremental fast path stays warm.
    * DIFFERENT content: monotonic guard — never clobber an equal-or-newer
      ``version`` with our (possibly stale) snapshot. A racing import that already
      committed a version >= ours recorded the live content; overwriting it back to
      our older state would orphan that content and force a phantom re-import next
      turn. Sequential updates are strictly monotonic (existing.version <
      new.version), so this only ever rejects a racing stale writer.
    """
    outputs = dict(existing_outputs or {})
    for key, new_state in (outputs_updates or {}).items():
        if not isinstance(new_state, dict):
            continue
        existing = outputs.get(key)
        if (
            isinstance(existing, dict)
            and existing.get("last_hash")
            and existing.get("last_hash") == new_state.get("last_hash")
        ):
            merged_entry = dict(existing)
            for stat_field in ("stat_size", "stat_mtime_ns"):
                if stat_field in new_state:
                    merged_entry[stat_field] = new_state[stat_field]
            outputs[key] = merged_entry
            continue
        existing_ver = (
            int(existing.get("version") or 0) if isinstance(existing, dict) else 0
        )
        new_ver = int(new_state.get("version") or 0)
        if (
            isinstance(existing, dict)
            and existing.get("last_hash")
            and existing_ver >= new_ver
        ):
            continue
        outputs[key] = new_state
    return outputs


class ChatTable:
    def _enrich_chat_data(self, chat_data: dict) -> dict:
        """
        Enrich chat data with computed fields for better UX.
        - Auto-generate title from first user message if not provided
        - Populate model field on all messages in history
        """
        chat_data = chat_data.copy()

        # Auto-generate title from first user message if no title provided
        if not chat_data.get("title") or chat_data.get("title") == "New Chat":
            # Try to get title from messages
            messages = chat_data.get("messages", [])
            if messages:
                for msg in messages:
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        if content:
                            # Take first 50 chars of first user message
                            if len(content) > 50:
                                chat_data["title"] = content[:50] + "..."
                            else:
                                chat_data["title"] = content
                            break

        # Populate model field on all messages if models array exists
        models = chat_data.get("models", [])
        if models and len(models) > 0:
            default_model = models[0]  # Use first model as default

            # Populate model in history messages
            if "history" in chat_data and "messages" in chat_data["history"]:
                for msg_id, msg in chat_data["history"]["messages"].items():
                    if "model" not in msg or not msg["model"]:
                        msg["model"] = default_model

            # Populate model in messages array (fallback structure)
            if "messages" in chat_data:
                for msg in chat_data["messages"]:
                    if msg.get("role") == "assistant" and (
                        "model" not in msg or not msg["model"]
                    ):
                        msg["model"] = default_model

        return chat_data

    def insert_new_chat(self, user_id: str, form_data: ChatForm) -> Optional[ChatModel]:
        with get_db() as db:
            id = str(uuid.uuid4())

            # Enrich chat data before storing
            enriched_chat = self._enrich_chat_data(form_data.chat)

            title = enriched_chat.get("title", "New Chat")
            models = enriched_chat.get("models") or []

            # New chats are born migrated when the new table is available so
            # all subsequent writes go through the fast row-level path.
            born_migrated = _chat_message_table_supported(db)
            if born_migrated:
                stored_chat, init_messages = _peel_messages_off_chat_dict(enriched_chat)
            else:
                stored_chat, init_messages = enriched_chat, None

            chat = ChatModel(
                **{
                    "id": id,
                    "user_id": user_id,
                    "title": title,
                    "chat": stored_chat,
                    "folder_id": form_data.folder_id,
                    "created_at": int(time.time()),
                    "updated_at": int(time.time()),
                    "model_id_primary": models[0] if models else None,
                }
            )

            result = Chat(**chat.model_dump())
            if born_migrated:
                result.messages_migrated = 1
            db.add(result)
            # The chat row and its initial message graph are one durability
            # unit.  Committing the row first used to leave a born-migrated chat
            # with no user message when the later table sync failed; streaming
            # could then persist an assistant whose parent did not exist.
            db.flush()
            if born_migrated and init_messages:
                self._sync_messages_to_table(db, id, init_messages)

            _upsert_chat_search(db, id, title, enriched_chat)
            db.commit()

            _hydrate_chat_messages(db, result)
            return ChatModel.model_validate(result) if result else None

    def import_chat(
        self, user_id: str, form_data: ChatImportForm
    ) -> Optional[ChatModel]:
        with get_db() as db:
            id = str(uuid.uuid4())
            import_title = form_data.chat.get("title", "New Chat")
            models = form_data.chat.get("models") or []

            born_migrated = _chat_message_table_supported(db)
            if born_migrated:
                stored_chat, init_messages = _peel_messages_off_chat_dict(
                    form_data.chat
                )
            else:
                stored_chat, init_messages = form_data.chat, None

            chat = ChatModel(
                **{
                    "id": id,
                    "user_id": user_id,
                    "title": import_title,
                    "chat": stored_chat,
                    "meta": form_data.meta,
                    "pinned": form_data.pinned,
                    "folder_id": form_data.folder_id,
                    "created_at": (
                        form_data.created_at
                        if form_data.created_at
                        else int(time.time())
                    ),
                    "updated_at": (
                        form_data.updated_at
                        if form_data.updated_at
                        else int(time.time())
                    ),
                    "subagent_of": (form_data.meta or {}).get("subagent_of"),
                    "automation_of": (form_data.meta or {}).get("automation_of"),
                    "model_id_primary": models[0] if models else None,
                }
            )

            result = Chat(**chat.model_dump())
            if born_migrated:
                result.messages_migrated = 1
            db.add(result)
            db.flush()
            if born_migrated and init_messages:
                self._sync_messages_to_table(db, id, init_messages)

            _upsert_chat_search(db, id, import_title, form_data.chat)
            db.commit()

            _hydrate_chat_messages(db, result)
            return ChatModel.model_validate(result) if result else None

    def update_chat_by_id(self, id: str, chat: dict) -> Optional[ChatModel]:
        try:
            with get_db() as db:
                # Establish one global lock order for graph/body rewrites:
                # chat row first, message rows second. The subagent atomic
                # transition and rewind/adopt primitives use the same order.
                # Without this, update_chat_by_id could DELETE/INSERT message
                # rows while another transaction held a message row and waited
                # to update the chat pointer, producing a deadlock or stale
                # full-table resync.
                chat_item = (
                    db.query(Chat).filter(Chat.id == id).with_for_update().first()
                )
                if chat_item is None:
                    return None

                # Enrich chat data before updating
                enriched_chat = self._enrich_chat_data(chat)

                migrated = bool(
                    _chat_message_table_supported(db)
                    and getattr(chat_item, "messages_migrated", 0)
                )

                # A migrated chat's graph is owned exclusively by row-level
                # chat_message operations. A hydrated messages map is a READ
                # projection and must never become a replacement command here.
                # Full DELETE/INSERT sync is reserved for create/import before
                # a chat becomes concurrently mutable.
                stored_chat = enriched_chat
                if migrated:
                    peeled_chat, _ = _peel_messages_off_chat_dict(enriched_chat)
                    stored_chat = peeled_chat

                # `queue` and `draining` are SERVER-OWNED runtime state, mutated
                # only by the atomic queue helpers + the drain engine under a row
                # lock. This is a whole-column write, so letting the caller's blob
                # carry those two keys would roll them back to whatever (stale)
                # snapshot the caller is holding — clobbering a just-stamped drain
                # marker or a just-popped queue, defeating the FOR UPDATE
                # serialization the drain depends on (the frontend autosaves
                # set_history_current_id / set_models at clean completion, exactly
                # when maybe_drain_queue has just stamped a marker). Lock the row
                # and force the LIVE values in, so this write never disturbs them.
                # The FOR UPDATE serializes against the atomic helpers and is
                # released on commit just below.
                if chat_item is not None and not str(id).startswith("local:"):
                    live = self._lock_and_read_queue(db, id)
                    if live is not None and isinstance(stored_chat, dict):
                        live_queue, live_draining = live
                        # Force the LIVE value, but only for keys actually in play —
                        # either the caller's blob carries them (so we override a
                        # stale snapshot with live) OR the row has real server state
                        # (a non-empty queue / a live marker). Injecting
                        # queue:[]/draining:null into a chat that never touched the
                        # queue would just pollute the blob (and break exact-equality
                        # callers/tests) without protecting anything.
                        new_stored = dict(stored_chat)
                        if "queue" in new_stored or live_queue:
                            new_stored["queue"] = live_queue
                        if "draining" in new_stored or live_draining is not None:
                            new_stored["draining"] = live_draining
                        stored_chat = new_stored
                    # `question_states` (the ask_user card's durable drafts/answers)
                    # is the SAME class of server-owned runtime state: it is written
                    # ONLY by the atomic set_question_state_by_id under this very row
                    # lock. A whole-column write from a stale caller snapshot (the
                    # frontend autosaves set_history_current_id / set_models /
                    # append_message routinely — including while an ask_user card is
                    # blocking) would roll back a just-committed answer, and the
                    # blocked generation's poll would then hang until timeout. The row
                    # is already FOR UPDATE locked above, so read the LIVE value and
                    # force it in (mirrors the queue/draining handling).
                    if isinstance(stored_chat, dict):
                        live_qs = self._read_live_question_states(db, id)
                        if (
                            live_qs is not None
                            and stored_chat.get("question_states") != live_qs
                        ):
                            stored_chat = {**stored_chat, "question_states": live_qs}

                title = enriched_chat.get("title", "New Chat")
                models = enriched_chat.get("models") or []
                chat_item.chat = stored_chat
                chat_item.title = title
                chat_item.updated_at = int(time.time())
                chat_item.model_id_primary = models[0] if models else None
                _invalidate_cached_sibling_stubs(id)
                # subagent_of / automation_of are INTERNAL columns set only at
                # creation (import_chat, by the subagent launcher and the
                # automation runner). Do NOT re-derive them from the caller's meta
                # here: this method serves the user-facing POST /chats/{id} save,
                # and trusting client meta let any verified user stamp subagent_of
                # onto a chat (hiding it as a "ghost", and — before the cascade was
                # owner-scoped — arming a cross-account delete). The columns keep
                # their creation value across saves.
                db.commit()

                # Search maintenance: chat_message_search rows are maintained
                # ROW-LEVEL by every per-message write path (upsert, the patch
                # flush, row delete via cascade), so rebuild them here ONLY when
                # this update actually carried a messages dict (legacy blob save
                # / import-style resync). The unconditional rebuild used to run
                # on EVERY pointer-flip save — set_history_current_id fires
                # after every send — re-reading all N messages and re-inserting
                # N search rows (GIN maintenance dominating) at 250-430ms per
                # send on long chats: O(N) rework per turn, O(N²) over a chat's
                # life. Title-only saves are covered too: search reads the live
                # chat.title column (chat_search.body is write-only legacy).
                if not migrated:
                    _upsert_chat_search(db, id, title, enriched_chat)
                    try:
                        db.commit()
                    except Exception:
                        pass

                _hydrate_chat_messages(db, chat_item)
                return ChatModel.model_validate(chat_item)
        except Exception:
            return None

    def _sync_messages_to_table(self, db, chat_id: str, messages: dict) -> None:
        """Replace every chat_message row for ``chat_id`` with the given dict.

        Done as: DELETE then bulk INSERT, ordered by dict iteration order so
        ``sequence`` matches the on-disk layout. This is intentionally private
        to create/import initialization. Once a migrated chat is visible, every
        graph mutation must use row-level append/update/delete primitives.
        """
        if not _chat_message_table_supported(db):
            return
        _validate_message_graph_for_sync(messages)
        db.execute(
            text("DELETE FROM chat_message WHERE chat_id = :cid"),
            {"cid": chat_id},
        )
        for seq, (mid, msg) in enumerate(messages.items()):
            if not isinstance(msg, dict):
                continue
            cols = _split_message_for_table(msg)
            db.execute(
                text(
                    "INSERT INTO chat_message "
                    "(chat_id, message_id, parent_id, role, content, "
                    " content_is_json, model, timestamp, sequence, "
                    " status_history, meta) "
                    "VALUES (:cid, :mid, :pid, :role, :c, :ij, "
                    ":model, :ts, :seq, CAST(:sh AS jsonb), CAST(:meta AS jsonb))"
                ),
                {
                    "cid": chat_id,
                    "mid": str(mid),
                    "pid": cols["parent_id"],
                    "role": cols["role"],
                    "c": cols["content"],
                    "ij": cols["content_is_json"],
                    "model": cols["model"],
                    "ts": cols["timestamp"],
                    "seq": seq,
                    "sh": cols["status_history"],
                    "meta": cols["meta"],
                },
            )

    def update_chat_title_by_id(self, id: str, title: str) -> Optional[ChatModel]:
        # Targeted title-only update: it never calls the destructive message
        # graph sync. Migrated chats keep messages outside this small JSON body;
        # legacy chats retain their existing body unchanged except for title.
        # A raw NUL (0x00) is rejected by database text/JSON storage.
        if isinstance(title, str) and "\x00" in title:
            title = title.replace("\x00", "")
        try:
            with get_db() as db:
                chat_item = (
                    db.query(Chat).filter(Chat.id == id).with_for_update().first()
                )
                if chat_item is None:
                    return None

                updated_at = int(time.time())
                # Do not call update_chat_by_id here: reads hydrate migrated
                # message rows into this shape, and that generic path treats a
                # messages dict as an authoritative replacement. The row lock
                # serializes this JSON metadata write with other chat-body
                # writers without touching chat_message at all.
                stored_chat = (
                    dict(chat_item.chat) if isinstance(chat_item.chat, dict) else {}
                )
                stored_chat["title"] = title
                chat_item.title = title
                chat_item.chat = stored_chat
                chat_item.updated_at = updated_at

                # Title changes need a search-row refresh too.
                _upsert_chat_search(
                    db,
                    id,
                    title,
                    chat_item.chat if isinstance(chat_item.chat, dict) else {},
                )
                db.commit()

                _hydrate_chat_messages(db, chat_item)
                return ChatModel.model_validate(chat_item)
        except Exception:
            return None

    def update_chat_tags_by_id(
        self, id: str, tags: list[str], user
    ) -> Optional[ChatModel]:
        chat = self.get_chat_by_id(id)
        if chat is None:
            return None

        self.delete_all_tags_by_id_and_user_id(id, user.id)

        for tag in chat.meta.get("tags", []):
            if self.count_chats_by_tag_name_and_user_id(tag, user.id) == 0:
                with get_db() as db:
                    _sync_delete_tag_by_name_and_user_id(db, tag, user.id)

        for tag_name in tags:
            if tag_name.lower() == "none":
                continue

            self.add_chat_tag_by_id_and_user_id_and_tag_name(id, user.id, tag_name)
        return self.get_chat_by_id(id)

    # --- Message queue (autonomous server-driven drain) --------------------
    # The follow-up message queue lives at chat.chat["queue"] (a list of
    # self-contained send specs) and an in-flight marker at
    # chat.chat["draining"]. These helpers do targeted blob-field writes that
    # do NOT touch the chat_message table, mirroring update_chat_title_by_id,
    # so they are O(1) regardless of chat size.
    #
    # CONCURRENCY: every mutator below begins with `_lock_and_read_queue`, which
    # does `SELECT ... FOR UPDATE` on the chat row and reads the queue + marker
    # from that LOCKED row. The Postgres row lock serializes all queue mutators
    # against each other (and against the drain pop / clear) for the duration of
    # the transaction — so two tabs appending, a Stop-time steer downgrade racing
    # a chip removal, an edit racing a drain, etc. can no longer lost-update the
    # whole-array write. This holds on a single worker (the lock is held on one
    # connection across the event-loop yields inside run_sync_db) AND across
    # workers (a real DB row lock), with no app-level lock needed. Each write
    # touches ONLY the field it owns ({queue} OR {draining}, via
    # `_persist_queue_blob`), so a queue write can never carry a stale draining
    # marker forward and clobber a concurrent marker change (and vice versa) —
    # that is the lost-update at the root of the steer-downgrade-skip bug.

    # Sentinel so _persist_queue_blob can tell "write null" from "leave alone".
    _QUEUE_FIELD_UNSET = object()

    def _lock_and_read_queue(self, db, id: str):
        """``SELECT ... FOR UPDATE`` the chat row and return ``(queue, draining)``
        parsed from the LOCKED row, or None if the chat doesn't exist. The lock
        is held until the surrounding transaction commits, serializing every
        queue mutator. Values are cast to ``::text`` and json-parsed so the
        result is driver-independent."""
        row = db.execute(
            text(
                "SELECT COALESCE(chat->'queue', '[]'::jsonb)::text AS q, "
                "       (chat->'draining')::text AS d "
                "FROM chat WHERE id = :id FOR UPDATE"
            ),
            {"id": id},
        ).first()
        if row is None:
            return None
        try:
            queue = json.loads(row[0]) if row[0] else []
        except (TypeError, ValueError):
            queue = []
        if not isinstance(queue, list):
            queue = []
        try:
            draining = json.loads(row[1]) if row[1] else None
        except (TypeError, ValueError):
            draining = None
        return queue, draining

    def _persist_queue_blob(
        self, db, id: str, queue=_QUEUE_FIELD_UNSET, draining=_QUEUE_FIELD_UNSET
    ) -> None:
        """Write only the ``{queue}`` and/or ``{draining}`` keys (whichever are
        passed) in a single jsonb_set chain, then commit. The row is assumed
        already locked via ``_lock_and_read_queue`` in the same transaction.
        Touching only the provided keys is what prevents a queue write from
        clobbering the draining marker (and vice versa).

        Whenever the queue itself is written, the ``queue_armed_at`` COLUMN is
        restated from it in the same statement: a timestamp while the queue has
        items, NULL when it is empty. Every queue mutation in the system funnels
        through here under the row lock, so the flag cannot disagree with the
        queue it summarises — which is the whole point, since the drain
        reconciler trusts it instead of reading (and detoasting) every blob."""
        expr = "COALESCE(chat, '{}'::jsonb)"
        params = {"id": id}
        sets = []
        if queue is not self._QUEUE_FIELD_UNSET:
            expr = "jsonb_set(" + expr + ", '{queue}', CAST(:q AS jsonb), true)"
            queue_list = queue if isinstance(queue, list) else []
            params["q"] = _dumps_jsonb(queue_list)
            params["armed"] = int(time.time()) if queue_list else None
            sets.append("queue_armed_at = :armed")
        if draining is not self._QUEUE_FIELD_UNSET:
            expr = "jsonb_set(" + expr + ", '{draining}', CAST(:d AS jsonb), true)"
            params["d"] = _dumps_jsonb(draining)
        if "q" not in params and "d" not in params:
            return
        sets.insert(0, "chat = " + expr)
        db.execute(
            text("UPDATE chat SET " + ", ".join(sets) + " WHERE id = :id"), params
        )
        db.commit()

    def get_queue_state_by_id(self, id: str) -> Optional[dict]:
        """Return {"queue": [...], "draining": <marker|None>} for a chat, or
        None if the chat doesn't exist. Reads the raw blob without hydrating
        the message table."""
        try:
            with get_db() as db:
                chat_item = db.get(Chat, id)
                if chat_item is None:
                    return None
                blob = chat_item.chat if isinstance(chat_item.chat, dict) else {}
                queue = blob.get("queue")
                return {
                    "queue": queue if isinstance(queue, list) else [],
                    "draining": blob.get("draining"),
                }
        except Exception:
            return None

    def get_chat_ids_with_draining_marker(self) -> list[str]:
        """Return chat ids whose ``draining`` marker is a live object (mid-drain).

        Used by the SINGLE-WORKER orphan sweep, which has no cross-process
        ``draining_chats`` Redis set to enumerate. Bounded — only chats currently
        draining carry an object marker — so this never scans the whole table in
        practice. Without it, a single-worker crash between marking and the
        generation's terminal handler wedges the queue forever (the stale
        marker's response id matches no future completion, so every later drain's
        ownership guard bails)."""
        try:
            with get_db() as db:
                rows = db.execute(
                    text(
                        "SELECT id FROM chat "
                        "WHERE jsonb_typeof(chat->'draining') = 'object'"
                    )
                ).fetchall()
                return [r[0] for r in rows]
        except Exception:
            log.exception("get_chat_ids_with_draining_marker failed")
            return []

    def get_armed_queue_chats(self, limit: int = 50) -> list[dict]:
        """Return ``[{"id", "user_id", "queue_armed_at"}]`` for chats that are
        owed a queue drain, oldest debt first.

        Index-only in practice (``chat_queue_armed_idx`` holds just these rows),
        which is why the reconciler can afford to run every few seconds. The
        equivalent honest question asked of the blob —
        ``jsonb_array_length(chat->'queue') > 0`` — is a full scan that has to
        detoast every chat document (measured: ~6.5k buffers / 131ms here).

        Archived chats are excluded: archiving is the user putting a
        conversation away, which is not the moment to start generating in it."""
        try:
            with get_db() as db:
                rows = db.execute(
                    text(
                        "SELECT id, user_id, queue_armed_at FROM chat "
                        "WHERE queue_armed_at IS NOT NULL "
                        "AND (archived IS NULL OR archived = false) "
                        "ORDER BY queue_armed_at ASC LIMIT :limit"
                    ),
                    {"limit": limit},
                ).fetchall()
                return [
                    {"id": r[0], "user_id": r[1], "queue_armed_at": r[2]} for r in rows
                ]
        except Exception:
            log.exception("get_armed_queue_chats failed")
            return []

    def disarm_queue_by_id(self, id: str) -> None:
        """Clear ``queue_armed_at`` without touching the blob. Only for the
        reconciler's self-heal when it finds a flag standing over an empty
        queue (a row written before the flag existed, or a queue emptied by a
        path that predates it) — the flag is otherwise derived, never set by
        hand."""
        try:
            with get_db() as db:
                db.execute(
                    text(
                        "UPDATE chat SET queue_armed_at = NULL "
                        "WHERE id = :id AND queue_armed_at IS NOT NULL"
                    ),
                    {"id": id},
                )
                db.commit()
        except Exception:
            log.exception("disarm_queue_by_id failed for %s", id)

    def append_queue_item_by_id(self, id: str, item: dict) -> Optional[dict]:
        """Atomically append one item to the queue under the row lock. Returns
        the new queue state."""
        try:
            with get_db() as db:
                read = self._lock_and_read_queue(db, id)
                if read is None:
                    return None
                queue, draining = read
                queue = list(queue)
                queue.append(item)
                self._persist_queue_blob(db, id, queue=queue)
                return {"queue": queue, "draining": draining}
        except Exception:
            log.exception("append_queue_item_by_id failed for %s", id)
            return None

    def remove_queue_item_by_id(self, id: str, item_id: str) -> Optional[dict]:
        """Atomically remove a queue item by its id under the row lock. Returns
        the new queue state."""
        try:
            with get_db() as db:
                read = self._lock_and_read_queue(db, id)
                if read is None:
                    return None
                queue, draining = read
                new_queue = [
                    q
                    for q in queue
                    if not (isinstance(q, dict) and q.get("id") == item_id)
                ]
                self._persist_queue_blob(db, id, queue=new_queue)
                return {"queue": new_queue, "draining": draining}
        except Exception:
            log.exception("remove_queue_item_by_id failed for %s", id)
            return None

    def update_queue_item_by_id(self, id: str, item: dict) -> Optional[dict]:
        """Atomically replace the queue item whose id matches ``item['id']`` IN
        PLACE (position preserved), merging so fields the caller omits survive.
        No-op if the id isn't in the queue (it may have already drained). Used to
        edit a queued message's text WITHOUT moving it to the tail — the old
        remove+append path reordered the queue (changing drain order, or a
        steer's injection slot). Returns the new queue state."""
        item_id = item.get("id") if isinstance(item, dict) else None
        if not item_id:
            return None
        try:
            with get_db() as db:
                read = self._lock_and_read_queue(db, id)
                if read is None:
                    return None
                queue, draining = read
                found = False
                new_queue = []
                for q in queue:
                    if isinstance(q, dict) and q.get("id") == item_id:
                        new_queue.append({**q, **item})
                        found = True
                    else:
                        new_queue.append(q)
                if found:
                    self._persist_queue_blob(db, id, queue=new_queue)
                return {"queue": new_queue, "draining": draining}
        except Exception:
            log.exception("update_queue_item_by_id failed for %s", id)
            return None

    def pop_steer_items_by_id(self, id: str) -> list[dict]:
        """Atomically remove and return the queue items marked as steering
        (``mode == "steer"``), leaving every other item (``after_final``, or
        unmarked legacy items) in place and in order.

        Steering items are consumed by the agentic loop at a tool-call boundary
        (utils/middleware.py) to inject a mid-task user turn — NOT by the
        post-completion drain. Returning them in queue order preserves the order
        the user sent multiple rapid steers. The ``draining`` marker is left
        untouched: steering is orthogonal to drain ownership.

        Returns the popped steer items (possibly empty). Never raises — a failure
        here must not break the generation that polls it each round.
        """
        try:
            with get_db() as db:
                read = self._lock_and_read_queue(db, id)
                if read is None:
                    return []
                queue, _draining = read
                steer = [
                    q for q in queue if isinstance(q, dict) and q.get("mode") == "steer"
                ]
                if not steer:
                    return []
                remaining = [
                    q
                    for q in queue
                    if not (isinstance(q, dict) and q.get("mode") == "steer")
                ]
                self._persist_queue_blob(db, id, queue=remaining)
                return steer
        except Exception:
            log.exception("pop_steer_items_by_id failed for %s", id)
            return []

    def peek_steer_items_by_id(self, id: str) -> list[dict]:
        """Non-destructive read of pending ``mode == "steer"`` items, in queue
        order. Unlike ``pop_steer_items_by_id`` this does NOT remove them: the
        agentic loop peeks them at a tool boundary, injects them as ``user_steer``
        blocks, and only deletes them (``remove_steer_items_by_ids``) AFTER the
        next model round is actually accepted — so a cancel/error before that
        point leaves the steer in the queue to be downgraded to a follow-up
        rather than consumed into a dead round and lost (the steer-on-cancel
        bug). Never raises."""
        try:
            with get_db() as db:
                row = db.execute(
                    text(
                        "SELECT COALESCE(chat->'queue', '[]'::jsonb)::text "
                        "FROM chat WHERE id = :id"
                    ),
                    {"id": id},
                ).first()
                if row is None or not row[0]:
                    return []
                queue = json.loads(row[0])
                if not isinstance(queue, list):
                    return []
                return [
                    q for q in queue if isinstance(q, dict) and q.get("mode") == "steer"
                ]
        except Exception:
            log.exception("peek_steer_items_by_id failed for %s", id)
            return []

    def remove_steer_items_by_ids(self, id: str, ids: list[str]) -> Optional[dict]:
        """Atomically remove specific queue items by id under the row lock — the
        steers a generation already delivered to the model this round. Paired
        with ``peek_steer_items_by_id`` for the deferred-consume flow. Returns
        the new queue state."""
        id_set = {i for i in (ids or []) if i}
        if not id_set:
            return None
        try:
            with get_db() as db:
                read = self._lock_and_read_queue(db, id)
                if read is None:
                    return None
                queue, draining = read
                new_queue = [
                    q
                    for q in queue
                    if not (isinstance(q, dict) and q.get("id") in id_set)
                ]
                self._persist_queue_blob(db, id, queue=new_queue)
                return {"queue": new_queue, "draining": draining}
        except Exception:
            log.exception("remove_steer_items_by_ids failed for %s", id)
            return None

    def convert_steer_items_to_after_final_by_id(self, id: str) -> int:
        """Re-tag any queued ``mode == "steer"`` items as ``after_final`` in
        place (order preserved), returning the count converted.

        Called when a generation is STOPPED/cancelled mid-flight: a steer was
        meant to be injected at a tool-call boundary of *that* response, but the
        response is over. Rather than (a) silently dropping the user's typed text
        or (b) leaving it as a steer that would leak into whatever UNRELATED
        response runs next, we downgrade it to a normal follow-up — the same
        place an unconsumed steer lands when the model simply finishes with no
        further tools. The user sees it as a pending follow-up they can edit or
        remove. Never raises."""
        try:
            with get_db() as db:
                read = self._lock_and_read_queue(db, id)
                if read is None:
                    return 0
                queue, _draining = read
                converted = 0
                new_queue = []
                for q in queue:
                    if isinstance(q, dict) and q.get("mode") == "steer":
                        q = {**q, "mode": "after_final"}
                        converted += 1
                    new_queue.append(q)
                if converted:
                    self._persist_queue_blob(db, id, queue=new_queue)
                return converted
        except Exception:
            log.exception("convert_steer_items_to_after_final_by_id failed for %s", id)
            return 0

    def pop_queue_head_and_mark_draining_by_id(
        self, id: str, draining_marker_builder, expected_finished_response_id=None
    ) -> Optional[dict]:
        """Atomically advance the queue for a finishing generation.

        Ownership rule (single read-modify-write transaction):

        * If a ``draining`` marker exists AND it belongs to a DIFFERENT in-flight
          generation (``response_message_id`` != ``expected_finished_response_id``),
          do nothing — another generation owns the chat right now. This is the
          idempotency guard: a duplicate/stale completion of an already-superseded
          turn cannot pop a second item.
        * Otherwise (no marker, or the marker is the finishing generation's own),
          the finishing generation is allowed to advance: pop the head item, set a
          fresh marker via ``draining_marker_builder(item)``, and persist. If the
          queue is empty, clear the marker instead.

        Cross-worker serialization is the caller's job (a per-chat Redis lock);
        this method only guarantees a consistent single-connection RMW.

        Returns {"item": <popped|None>, "queue": [...], "draining": <marker|None>}.
        ``item`` is None when nothing was popped.
        """
        try:
            with get_db() as db:
                read = self._lock_and_read_queue(db, id)
                if read is None:
                    return None
                queue, draining = read

                # A marker owned by a different in-flight generation → bail.
                if (
                    isinstance(draining, dict)
                    and draining.get("response_message_id")
                    != expected_finished_response_id
                ):
                    return {"item": None, "queue": queue, "draining": draining}

                if not queue:
                    # Nothing to start next. Clear our own marker (if any) so the
                    # chat isn't left flagged as draining.
                    if draining is not None:
                        self._persist_queue_blob(db, id, queue=queue, draining=None)
                    return {"item": None, "queue": [], "draining": None}

                queue = list(queue)
                # Prefer the first still-pending STEER item if any. A steer that
                # arrived after the model's last tool call has no boundary left to
                # inject at and falls back to the drain; "steer" means "do this
                # next, urgently", so it should drain AHEAD of an earlier
                # after_final follow-up rather than behind it. Absent any steer
                # this is a plain head pop. (C21)
                steer_idx = next(
                    (
                        i
                        for i, q in enumerate(queue)
                        if isinstance(q, dict) and q.get("mode") == "steer"
                    ),
                    None,
                )
                item = queue.pop(steer_idx if steer_idx is not None else 0)
                marker = draining_marker_builder(item)
                self._persist_queue_blob(db, id, queue=queue, draining=marker)
                return {"item": item, "queue": queue, "draining": marker}
        except Exception:
            log.exception("pop_queue_head_and_mark_draining_by_id failed for %s", id)
            return None

    def clear_draining_by_id(
        self, id: str, expected_finished_response_id=None
    ) -> Optional[dict]:
        """Clear the in-flight draining marker, and — when we actually own/clear
        it — downgrade any pending ``mode == "steer"`` items to ``after_final``
        IN THE SAME locked transaction. Folding the downgrade in (rather than a
        separate convert call gated on a racy re-read of the marker) is what
        closes the steer-downgrade-skip race: a concurrent append can no longer
        resurrect the marker between the clear and the downgrade decision. If
        ``expected_finished_response_id`` is given and the marker belongs to a
        NEWER generation, do nothing — that generation is still live and owns
        both the marker and the steers. Idempotent. Returns
        ``{"queue", "draining", "cleared": bool, "converted": int}``; ``cleared``
        is False iff a newer generation's marker was left intact. Returns the
        same shape with ``"missing": True`` when the chat row is gone, and ``None``
        ONLY on an unexpected error — so callers can distinguish a deleted chat
        (safe to forget) from a transient failure (marker may still be set; keep
        tracking it for the sweep)."""
        try:
            with get_db() as db:
                read = self._lock_and_read_queue(db, id)
                if read is None:
                    # Row genuinely absent (deleted) — distinct from an exception.
                    return {
                        "queue": [],
                        "draining": None,
                        "cleared": False,
                        "converted": 0,
                        "missing": True,
                    }
                queue, draining = read
                if (
                    expected_finished_response_id is not None
                    and isinstance(draining, dict)
                    and draining.get("response_message_id")
                    != expected_finished_response_id
                ):
                    # Marker belongs to a newer generation — leave it AND its
                    # steers untouched.
                    return {
                        "queue": queue,
                        "draining": draining,
                        "cleared": False,
                        "converted": 0,
                    }
                # We own (or there is no) marker: clear it and downgrade any
                # orphaned steers to after_final so they don't leak into an
                # unrelated future generation's tool boundary.
                converted = 0
                new_queue = []
                for q in queue:
                    if isinstance(q, dict) and q.get("mode") == "steer":
                        q = {**q, "mode": "after_final"}
                        converted += 1
                    new_queue.append(q)
                self._persist_queue_blob(db, id, queue=new_queue, draining=None)
                return {
                    "queue": new_queue,
                    "draining": None,
                    "cleared": True,
                    "converted": converted,
                }
        except Exception:
            log.exception("clear_draining_by_id failed for %s", id)
            return None

    # --- ask_user question state -----------------------------------------
    # The built-in `ask_user` tool blocks the running generation while a human
    # answers an inline question card. The model-visible Q&A ultimately lives in
    # the assistant message's content_blocks (the tool call = the question, the
    # tool result = the answer). This blob field is the TRANSIENT delivery +
    # draft channel that bridges the still-running server-side generation and a
    # frontend that may reload, close, or open in another tab mid-answer:
    #
    #   chat.chat["question_states"][<tool_call_id>] = {
    #       "draft":   {<qIndex>: {"selected": [...], "other": "..."}},  # autosaved
    #       "answer":  {<qIndex>: {"selected": [...], "other": "..."}},  # on submit
    #       "skipped": <bool>,
    #       "submitted_at": <ts|None>,
    #   }
    #
    # Mirrors the queue helpers above: targeted jsonb_set, no message-table
    # re-sync, O(1) regardless of chat size. Never raise from the read path — a
    # failure there must not break the generation polling it each round.

    def _read_live_question_states(self, db, id: str) -> Optional[dict]:
        """Read the committed ``question_states`` map as a plain dict (or None if
        absent). Intended to be called while the chat row is already locked (e.g.
        after ``_lock_and_read_queue``) so the value is the live, committed one;
        ``::text`` + parse keeps it driver-independent."""
        row = db.execute(
            text("SELECT (chat->'question_states')::text FROM chat WHERE id = :id"),
            {"id": id},
        ).first()
        if row is None or row[0] is None:
            return None
        try:
            qs = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return qs if isinstance(qs, dict) else None

    def set_question_state_by_id(
        self, id: str, tool_call_id: str, patch: dict
    ) -> Optional[dict]:
        """Atomically merge ``patch`` into ``question_states[tool_call_id]``
        (read-modify-write of the blob). Used both for incremental draft saves
        (partial selections, autosaved as the user clicks) and for the terminal
        answer/skip submit. Shallow-merges top-level keys; ``draft`` is replaced
        wholesale by the caller (it already carries the full draft snapshot).
        Returns the new per-question entry, or None on failure.

        The chat row is ``SELECT ... FOR UPDATE`` locked for the duration of the
        transaction (mirrors the queue mutators' ``_lock_and_read_queue``), so
        concurrent writers on the SAME chat serialize. Without this, a debounced
        draft save still in flight when the user submits could read the blob
        BEFORE the answer was committed and then commit its stale snapshot last,
        clobbering the answer — leaving the blocked ``ask_user`` poll hanging
        until timeout. The lock also covers the two-tab case."""
        if not tool_call_id or not isinstance(patch, dict):
            return None
        try:
            with get_db() as db:
                # Lock + read the current map from the locked row. ::text + parse
                # keeps it driver-independent (same idiom as _lock_and_read_queue).
                row = db.execute(
                    text(
                        "SELECT COALESCE(chat->'question_states', '{}'::jsonb)::text "
                        "FROM chat WHERE id = :id FOR UPDATE"
                    ),
                    {"id": id},
                ).first()
                if row is None:
                    return None
                try:
                    states = json.loads(row[0]) if row[0] else {}
                except (TypeError, ValueError):
                    states = {}
                if not isinstance(states, dict):
                    states = {}
                existing = states.get(tool_call_id)
                existing = dict(existing) if isinstance(existing, dict) else {}
                existing.update(patch)
                states[tool_call_id] = existing
                # Write back the whole map under the still-held lock; commit
                # releases it. Touching only the {question_states} key keeps this
                # from clobbering concurrent message-table / queue writes.
                db.execute(
                    text(
                        "UPDATE chat SET chat = jsonb_set("
                        "  COALESCE(chat, '{}'::jsonb), '{question_states}', "
                        "  CAST(:s AS jsonb), true) WHERE id = :id"
                    ),
                    {"s": _dumps_jsonb(states), "id": id},
                )
                db.commit()
                return existing
        except Exception:
            log.exception("set_question_state_by_id failed for %s/%s", id, tool_call_id)
            return None

    def get_question_state_by_id(self, id: str, tool_call_id: str) -> Optional[dict]:
        """Return the full state entry for one question (draft/answer/skipped),
        or None if the chat or entry doesn't exist. Reads the raw blob without
        hydrating the message table. Never raises."""
        try:
            with get_db() as db:
                chat_item = db.get(Chat, id)
                if chat_item is None:
                    return None
                blob = chat_item.chat if isinstance(chat_item.chat, dict) else {}
                states = blob.get("question_states")
                if not isinstance(states, dict):
                    return None
                entry = states.get(tool_call_id)
                return entry if isinstance(entry, dict) else None
        except Exception:
            return None

    def get_question_answer_by_id(self, id: str, tool_call_id: str) -> Optional[dict]:
        """Return ``{"answer": {...}}`` or ``{"skipped": True}`` once the user
        has submitted, else None. This is the terminal signal the blocked
        ``ask_user`` callable polls for. Never raises."""
        entry = self.get_question_state_by_id(id, tool_call_id)
        if not isinstance(entry, dict):
            return None
        if entry.get("skipped"):
            return {"skipped": True}
        answer = entry.get("answer")
        if isinstance(answer, dict):
            return {"answer": answer, "submitted_at": entry.get("submitted_at")}
        return None

    def get_chat_title_by_id(self, id: str) -> Optional[str]:
        chat = self.get_chat_by_id(id)
        if chat is None:
            return None

        return chat.chat.get("title", "New Chat")

    def update_chat_meta_by_id(self, id: str, meta: dict) -> Optional[ChatModel]:
        try:
            with get_db() as db:
                chat = db.get(Chat, id)
                if chat is None:
                    return None

                chat.meta = meta if isinstance(meta, dict) else {}
                chat.subagent_of = chat.meta.get("subagent_of")
                chat.updated_at = int(time.time())
                db.commit()
                db.refresh(chat)
                _hydrate_chat_messages(db, chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def merge_container_workspace_outputs(
        self,
        id: str,
        outputs_updates: dict,
        data_root: str = "",
        server_id: str = "",
    ) -> bool:
        """Atomically merge container output-ledger entries into ``chat.meta``.

        The container output-import dedup ledger lives in
        ``chat.meta["container_workspace"]["outputs"]`` (``state_key`` -> version
        state). ``import_changed_container_outputs`` computes its updates from a
        snapshot read OUTSIDE any lock, so two concurrent imports (e.g. a fanout
        rerun importing to the same parent message) would clobber each other with a
        full-meta replace — dropping a file's dedup record and re-importing it as a
        duplicate. This locks the chat row, re-reads the CURRENT ledger, and merges
        ONLY the given ``state_key`` entries:

        * if the live ledger already records the SAME ``last_hash`` for a key, the
          existing version/versions/file_id are KEPT (first-writer-wins, so a racing
          duplicate never swaps the file_id under an already-recorded version) while
          the cheap stat cache is allowed to advance (keeping the incremental fast
          path warm);
        * otherwise the new state (a genuine new/changed version) is taken.

        Only the ``container_workspace`` subtree is touched, so concurrent writers of
        other meta (e.g. tags) are preserved. Returns True if anything was written.
        """
        if not outputs_updates:
            return False
        try:
            with get_db() as db:
                chat = db.query(Chat).filter(Chat.id == id).with_for_update().first()
                if chat is None:
                    return False
                meta = dict(chat.meta or {})
                container_meta = dict(meta.get("container_workspace") or {})
                outputs = merge_container_outputs_state(
                    container_meta.get("outputs") or {}, outputs_updates
                )
                container_meta["outputs"] = outputs
                if data_root:
                    container_meta["data_root"] = data_root
                if server_id:
                    container_meta["server_id"] = server_id
                meta["container_workspace"] = container_meta
                chat.meta = meta
                chat.subagent_of = meta.get("subagent_of")
                chat.updated_at = int(time.time())
                db.commit()
                return True
        except Exception:
            log.exception("merge_container_workspace_outputs failed for chat %s", id)
            return False

    def get_messages_map_by_chat_id(self, id: str) -> Optional[dict]:
        chat = self.get_chat_by_id(id)
        if chat is None:
            return None

        return chat.chat.get("history", {}).get("messages", {}) or {}

    def get_messages_with_subagent_runs_by_chat_id(self, id: str) -> dict:
        """Return only parent messages that carry durable subagent run state.

        The stranded-run reconciler needs to scan every branch, but loading the
        complete chat (including arbitrary message content and tool results) on
        each task-status poll is wasteful. Migrated chats filter on the JSONB
        metadata key in SQL; legacy chats filter the embedded map in memory.
        """
        with get_db() as db:
            if _is_chat_migrated(db, id):
                rows = db.execute(
                    text(
                        f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                        "FROM chat_message "
                        "WHERE chat_id = :cid "
                        "AND COALESCE(meta, '{}'::jsonb) ? 'subagent_runs' "
                        "ORDER BY sequence"
                    ),
                    {"cid": id},
                ).fetchall()
                messages = [_row_to_message_dict(row) for row in rows]
                return {
                    str(message.get("id")): message
                    for message in messages
                    if isinstance(message, dict) and message.get("id")
                }

            chat = db.get(Chat, id)
            if chat is None:
                return {}
            chat_dict = chat.chat if isinstance(chat.chat, dict) else {}
            history = (
                chat_dict.get("history")
                if isinstance(chat_dict.get("history"), dict)
                else {}
            )
            messages = (
                history.get("messages")
                if isinstance(history.get("messages"), dict)
                else {}
            )
            return {
                str(message_id): message
                for message_id, message in messages.items()
                if isinstance(message, dict)
                and isinstance(message.get("subagent_runs"), dict)
            }

    def get_message_by_id_and_message_id(
        self, id: str, message_id: str
    ) -> Optional[dict]:
        with get_db() as db:
            if _is_chat_migrated(db, id):
                row = db.execute(
                    text(
                        f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                        "FROM chat_message WHERE chat_id = :cid AND message_id = :mid"
                    ),
                    {"cid": id, "mid": message_id},
                ).fetchone()
                return _row_to_message_dict(row) if row is not None else None

        chat = self.get_chat_by_id(id)
        if chat is None:
            return None

        return chat.chat.get("history", {}).get("messages", {}).get(message_id)

    def prepare_subagent_turn_atomic(
        self,
        id: str,
        user_message: dict,
        assistant_message: dict,
        *,
        expected_current_id: Optional[str],
        reset_history: bool = False,
        revert_user_message_id: Optional[str] = None,
        revert_assistant_message_id: Optional[str] = None,
        expected_model_id: Optional[str] = None,
        set_model_id: Optional[str] = None,
    ) -> dict:
        """Atomically prepare one hidden subagent turn.

        The old subagent helpers hydrated the whole chat, edited the in-memory
        history, and called ``update_chat_by_id``. On migrated chats that method
        implements a full-table resync (DELETE every ``chat_message`` row, then
        INSERT the caller's snapshot), so a continue/rerun racing another turn
        could erase the winner's transcript.

        This primitive locks the hidden chat row, verifies the exact leaf the
        caller prepared from, optionally resets the transcript or reverts one
        user→assistant pair, appends the replacement pair, and advances
        ``history.currentId`` in one transaction. A context-fallback retry can
        additionally compare-and-set the hidden chat's canonical model in that
        same commit, so later continuations cannot observe a new transcript
        paired with the old model (or vice versa). The migrated path touches
        only the rows involved; the legacy path mutates the live blob under the
        same row lock.
        """
        if (
            not id
            or not isinstance(user_message, dict)
            or not isinstance(assistant_message, dict)
        ):
            raise ValueError("invalid subagent turn")
        if reset_history and revert_user_message_id:
            raise ValueError("cannot reset and revert a subagent turn together")

        user_message = copy.deepcopy(user_message)
        assistant_message = copy.deepcopy(assistant_message)
        user_message_id = str(user_message.get("id") or "")
        assistant_message_id = str(assistant_message.get("id") or "")
        if not user_message_id or not assistant_message_id:
            raise ValueError("subagent turn messages require ids")
        if user_message_id == assistant_message_id:
            raise ValueError("subagent turn message ids must differ")

        expected_current_id = str(expected_current_id) if expected_current_id else None
        revert_user_message_id = (
            str(revert_user_message_id) if revert_user_message_id else None
        )
        revert_assistant_message_id = (
            str(revert_assistant_message_id) if revert_assistant_message_id else None
        )
        expected_model_id = str(expected_model_id) if expected_model_id else None
        set_model_id = str(set_model_id) if set_model_id else None
        if expected_model_id and not set_model_id:
            raise ValueError("expected_model_id requires set_model_id")

        with get_db() as db:
            try:
                chat_row = (
                    db.query(Chat).filter(Chat.id == id).with_for_update().first()
                )
                if chat_row is None:
                    raise ChatHistoryConflictError(
                        "The subagent conversation no longer exists.",
                        code="subagent_chat_missing",
                    )

                chat_data = copy.deepcopy(
                    chat_row.chat if isinstance(chat_row.chat, dict) else {}
                )
                live_models = chat_data.get("models") or []
                live_model_id = (
                    str(live_models[0])
                    if isinstance(live_models, list) and live_models
                    else None
                )
                if set_model_id and live_model_id != expected_model_id:
                    raise ChatHistoryConflictError(
                        "The subagent model changed while its context fallback "
                        "was being prepared. Retry from the latest model.",
                        code="subagent_model_changed",
                    )
                history = dict(chat_data.get("history") or {})
                live_current_id = history.get("currentId")
                live_current_id = str(live_current_id) if live_current_id else None
                if live_current_id != expected_current_id:
                    raise ChatHistoryConflictError(
                        "The subagent conversation changed while this turn was "
                        "being prepared. Retry from its latest branch.",
                        code="subagent_history_changed",
                    )

                migrated = bool(
                    _chat_message_table_supported(db)
                    and getattr(chat_row, "messages_migrated", 0)
                )
                base_parent_id = live_current_id

                if migrated:
                    if reset_history:
                        db.execute(
                            text("DELETE FROM chat_message WHERE chat_id = :cid"),
                            {"cid": id},
                        )
                        base_parent_id = None
                    elif revert_user_message_id:
                        user_row = db.execute(
                            text(
                                "SELECT parent_id FROM chat_message "
                                "WHERE chat_id = :cid AND message_id = :mid "
                                "FOR UPDATE"
                            ),
                            {"cid": id, "mid": revert_user_message_id},
                        ).first()
                        if user_row is None:
                            raise ChatHistoryConflictError(
                                "The subagent turn to replace is no longer present.",
                                code="subagent_turn_missing",
                            )
                        base_parent_id = user_row[0] or None

                        remove_ids = [revert_user_message_id]
                        if revert_assistant_message_id:
                            assistant_row = db.execute(
                                text(
                                    "SELECT parent_id FROM chat_message "
                                    "WHERE chat_id = :cid AND message_id = :mid "
                                    "FOR UPDATE"
                                ),
                                {
                                    "cid": id,
                                    "mid": revert_assistant_message_id,
                                },
                            ).first()
                            if (
                                assistant_row is None
                                or str(assistant_row[0] or "") != revert_user_message_id
                            ):
                                raise ChatHistoryConflictError(
                                    "The subagent turn graph changed before it "
                                    "could be replaced.",
                                    code="subagent_turn_changed",
                                )
                            remove_ids.append(revert_assistant_message_id)

                        db.execute(
                            text(
                                "DELETE FROM chat_message "
                                "WHERE chat_id = :cid AND message_id IN :mids"
                            ).bindparams(bindparam("mids", expanding=True)),
                            {"cid": id, "mids": remove_ids},
                        )
                        if base_parent_id:
                            db.execute(
                                text(
                                    "UPDATE chat_message SET meta = jsonb_set("
                                    "  COALESCE(meta, '{}'::jsonb), "
                                    "  '{childrenIds}', "
                                    "  COALESCE(("
                                    "    SELECT jsonb_agg(value) "
                                    "    FROM jsonb_array_elements("
                                    "      COALESCE(meta->'childrenIds', '[]'::jsonb)"
                                    "    ) AS value "
                                    "    WHERE value <> to_jsonb(CAST(:removed AS text))"
                                    "  ), '[]'::jsonb), true"
                                    ") WHERE chat_id = :cid AND message_id = :pid"
                                ),
                                {
                                    "removed": revert_user_message_id,
                                    "cid": id,
                                    "pid": base_parent_id,
                                },
                            )

                    if base_parent_id:
                        parent_row = db.execute(
                            text(
                                f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                                "FROM chat_message "
                                "WHERE chat_id = :cid AND message_id = :mid "
                                "FOR UPDATE"
                            ),
                            {"cid": id, "mid": base_parent_id},
                        ).first()
                        if parent_row is None:
                            raise ChatHistoryConflictError(
                                "The subagent branch parent no longer exists.",
                                code="subagent_parent_missing",
                            )
                        parent_message = _row_to_message_dict(parent_row)
                        children = list(parent_message.get("childrenIds") or [])
                        if user_message_id not in children:
                            children.append(user_message_id)
                        db.execute(
                            text(
                                "UPDATE chat_message SET meta = jsonb_set("
                                "  COALESCE(meta, '{}'::jsonb), "
                                "  '{childrenIds}', CAST(:children AS jsonb), true"
                                ") WHERE chat_id = :cid AND message_id = :mid"
                            ),
                            {
                                "children": _dumps_jsonb(children),
                                "cid": id,
                                "mid": base_parent_id,
                            },
                        )

                    user_message["id"] = user_message_id
                    user_message["parentId"] = base_parent_id
                    user_message["childrenIds"] = [assistant_message_id]
                    assistant_message["id"] = assistant_message_id
                    assistant_message["parentId"] = user_message_id
                    assistant_message["childrenIds"] = []

                    sequence = _next_sequence_for_chat(db, id)
                    for offset, message in enumerate((user_message, assistant_message)):
                        cols = _split_message_for_table(message)
                        db.execute(
                            text(
                                "INSERT INTO chat_message "
                                "(chat_id, message_id, parent_id, role, content, "
                                " content_is_json, model, timestamp, sequence, "
                                " status_history, meta) "
                                "VALUES (:cid, :mid, :pid, :role, :content, "
                                " :is_json, :model, :timestamp, :sequence, "
                                " CAST(:status_history AS jsonb), "
                                " CAST(:meta AS jsonb))"
                            ),
                            {
                                "cid": id,
                                "mid": message["id"],
                                "pid": cols["parent_id"],
                                "role": cols["role"],
                                "content": cols["content"],
                                "is_json": cols["content_is_json"],
                                "model": cols["model"],
                                "timestamp": cols["timestamp"],
                                "sequence": sequence + offset,
                                "status_history": cols["status_history"],
                                "meta": cols["meta"],
                            },
                        )
                        _upsert_message_search(
                            db,
                            id,
                            message["id"],
                            message.get("role"),
                            _extract_content_text(message.get("content", "")),
                        )

                    history.pop("messages", None)
                else:
                    messages = dict(history.get("messages") or {})
                    if reset_history:
                        messages = {}
                        base_parent_id = None
                    elif revert_user_message_id:
                        prior_user = messages.get(revert_user_message_id)
                        if not isinstance(prior_user, dict):
                            raise ChatHistoryConflictError(
                                "The subagent turn to replace is no longer present.",
                                code="subagent_turn_missing",
                            )
                        base_parent_id = prior_user.get("parentId") or None
                        if revert_assistant_message_id:
                            prior_assistant = messages.get(revert_assistant_message_id)
                            if (
                                not isinstance(prior_assistant, dict)
                                or str(prior_assistant.get("parentId") or "")
                                != revert_user_message_id
                            ):
                                raise ChatHistoryConflictError(
                                    "The subagent turn graph changed before it "
                                    "could be replaced.",
                                    code="subagent_turn_changed",
                                )
                        messages.pop(revert_user_message_id, None)
                        if revert_assistant_message_id:
                            messages.pop(revert_assistant_message_id, None)
                        if base_parent_id and isinstance(
                            messages.get(base_parent_id), dict
                        ):
                            parent_message = dict(messages[base_parent_id])
                            parent_message["childrenIds"] = [
                                child
                                for child in (parent_message.get("childrenIds") or [])
                                if child != revert_user_message_id
                            ]
                            messages[base_parent_id] = parent_message

                    if base_parent_id:
                        parent_message = messages.get(base_parent_id)
                        if not isinstance(parent_message, dict):
                            raise ChatHistoryConflictError(
                                "The subagent branch parent no longer exists.",
                                code="subagent_parent_missing",
                            )
                        parent_message = dict(parent_message)
                        children = list(parent_message.get("childrenIds") or [])
                        if user_message_id not in children:
                            children.append(user_message_id)
                        parent_message["childrenIds"] = children
                        messages[base_parent_id] = parent_message

                    user_message["id"] = user_message_id
                    user_message["parentId"] = base_parent_id
                    user_message["childrenIds"] = [assistant_message_id]
                    assistant_message["id"] = assistant_message_id
                    assistant_message["parentId"] = user_message_id
                    assistant_message["childrenIds"] = []
                    messages[user_message_id] = user_message
                    messages[assistant_message_id] = assistant_message
                    history["messages"] = messages

                history["currentId"] = assistant_message_id
                chat_data["history"] = history
                if set_model_id:
                    chat_data["models"] = [set_model_id]
                    chat_row.model_id_primary = set_model_id
                chat_row.chat = chat_data
                chat_row.updated_at = int(time.time())
                db.commit()
                _invalidate_cached_sibling_stubs(id)
                return {
                    "current_id": assistant_message_id,
                    "user_message_id": user_message_id,
                    "assistant_message_id": assistant_message_id,
                    "model_id": set_model_id or live_model_id,
                }
            except Exception:
                db.rollback()
                raise

    def append_rewind_branch_atomic(
        self,
        id: str,
        source_message_id: str,
        branch_message: dict,
        *,
        user_id: str,
        expected_source_rev: Optional[str] = None,
        expected_current_id: Optional[str] = None,
        expected_related_leaves: Optional[list[dict]] = None,
        operation_id: Optional[str] = None,
    ) -> dict:
        """Atomically append one guarded assistant sibling and select it.

        This is the durable commit primitive used by multi-subagent rewind
        (both adoption and detached rerun). The caller prepares the complete
        sibling from a read-only snapshot. ``expected_current_id`` may be a
        later selected descendant of the source: that lets a repair on an older
        visible turn set the later work aside on its original branch. The caller
        proves the ancestry before entering this transaction; locking the chat
        row plus guarding both that current pointer and the source revision
        keeps the proof stable. This method then revalidates all source rows
        before changing anything, and commits the following as one transaction:

        * append the sibling message;
        * add it to the shared parent user's ``childrenIds``;
        * advance ``history.currentId`` to the sibling.

        ``expected_related_leaves`` guards hidden subagent chats whose selected
        answers were copied into the sibling. Each entry contains ``chat_id``,
        ``message_id``, and the observed chat/message ``xmin`` revisions. A
        concurrently regenerated or re-selected hidden answer therefore aborts
        the whole operation before a branch is created.

        ``operation_id`` makes an ambiguous HTTP retry idempotent. If the exact
        branch already exists with the same operation marker, the committed
        branch is returned without reapplying or moving any pointers.

        Raises ``ChatBranchConflictError`` for stale/conflicting guards. Other
        exceptions propagate after rollback; callers must never interpret a
        partial result as success.
        """
        if not id or not source_message_id or not isinstance(branch_message, dict):
            raise ValueError("invalid rewind branch request")

        branch_message = copy.deepcopy(branch_message)
        branch_message_id = str(branch_message.get("id") or "")
        if not branch_message_id:
            raise ValueError("rewind branch is missing an id")
        if branch_message_id == source_message_id:
            raise ValueError("rewind branch id must differ from its source")

        operation_id = str(operation_id or branch_message_id)
        expected_current_id = str(expected_current_id or source_message_id)
        expected_related_leaves = list(expected_related_leaves or [])

        with get_db() as db:
            try:
                # The chat row is the transaction's root lock. It serializes two
                # atomic repair attempts and gives us an authoritative currentId
                # to guard before either can append a sibling.
                chat_row = db.execute(
                    text(
                        "SELECT user_id, messages_migrated, "
                        "       chat->'history'->>'currentId', xmin::text, "
                        "       updated_at "
                        "FROM chat WHERE id = :id FOR UPDATE"
                    ),
                    {"id": id},
                ).first()
                if chat_row is None or str(chat_row[0] or "") != str(user_id):
                    raise ChatBranchConflictError(
                        "Parent chat is no longer accessible.",
                        code="rewind_parent_unavailable",
                    )

                migrated = bool(chat_row[1] and _chat_message_table_supported(db))

                def _operation_matches(message: Optional[dict]) -> bool:
                    marker = (
                        (
                            message.get("rewind_operation")
                            or message.get("rewind_adopt_operation")
                        )
                        if isinstance(message, dict)
                        else None
                    )
                    return (
                        isinstance(marker, dict)
                        and str(marker.get("id") or "") == operation_id
                        and str(marker.get("source_message_id") or "")
                        == source_message_id
                    )

                # Idempotency must be checked BEFORE the currentId guard: the
                # first successful commit deliberately moved currentId away from
                # source_message_id, so a transport retry would otherwise look
                # stale even though its exact operation already committed.
                existing_branch: Optional[dict] = None
                if migrated:
                    existing_row = db.execute(
                        text(
                            f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                            "FROM chat_message "
                            "WHERE chat_id = :cid AND message_id = :mid "
                            "FOR UPDATE"
                        ),
                        {"cid": id, "mid": branch_message_id},
                    ).first()
                    if existing_row is not None:
                        existing_branch = _row_to_message_dict(existing_row)
                else:
                    legacy_chat = db.execute(
                        text("SELECT chat::text FROM chat WHERE id = :id"),
                        {"id": id},
                    ).scalar()
                    try:
                        legacy_blob = json.loads(legacy_chat) if legacy_chat else {}
                    except Exception:
                        legacy_blob = {}
                    existing_branch = (
                        ((legacy_blob.get("history") or {}).get("messages") or {}).get(
                            branch_message_id
                        )
                        if isinstance(legacy_blob, dict)
                        else None
                    )

                if existing_branch is not None:
                    if _operation_matches(existing_branch):
                        # An ambiguous transport retry is safe to acknowledge only
                        # while this operation's branch is still the durable
                        # selection. Another tab may have navigated or committed a
                        # newer rewind after our first request succeeded. Returning
                        # the old branch as "success" in that state would make the
                        # retrying tab install it locally even though
                        # history.currentId points elsewhere, recreating a
                        # reload-only branch divergence.
                        if str(chat_row[2] or "") != branch_message_id:
                            raise ChatBranchConflictError(
                                "This rewind operation committed, but the parent "
                                "chat has since moved to another branch.",
                                code="rewind_operation_superseded",
                            )
                        return {
                            "message": existing_branch,
                            "idempotent": True,
                            "updated_at": chat_row[4],
                        }
                    raise ChatBranchConflictError(
                        "The requested rewind branch id already exists.",
                        code="rewind_branch_id_conflict",
                    )

                if str(chat_row[2] or "") != expected_current_id:
                    raise ChatBranchConflictError(
                        "The parent chat moved to another branch before the repair committed.",
                        code="rewind_parent_moved",
                    )

                source_message: Optional[dict] = None
                source_rev: Optional[str] = None
                legacy_blob: Optional[dict] = None
                if migrated:
                    source_row = db.execute(
                        text(
                            f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                            "FROM chat_message "
                            "WHERE chat_id = :cid AND message_id = :mid "
                            "FOR UPDATE"
                        ),
                        {"cid": id, "mid": source_message_id},
                    ).first()
                    if source_row is not None:
                        source_message = _row_to_message_dict(source_row)
                        source_rev = (
                            str(source_row[9]) if source_row[9] is not None else None
                        )
                else:
                    raw_blob = db.execute(
                        text("SELECT chat::text FROM chat WHERE id = :id"),
                        {"id": id},
                    ).scalar()
                    try:
                        legacy_blob = json.loads(raw_blob) if raw_blob else {}
                    except Exception:
                        legacy_blob = {}
                    source_message = (
                        ((legacy_blob.get("history") or {}).get("messages") or {}).get(
                            source_message_id
                        )
                        if isinstance(legacy_blob, dict)
                        else None
                    )
                    # For a legacy chat every message lives in the locked chat
                    # row, so its xmin is the source revision.
                    source_rev = str(chat_row[3]) if chat_row[3] is not None else None

                if not isinstance(source_message, dict):
                    raise ChatBranchConflictError(
                        "The source parent message no longer exists.",
                        code="rewind_source_missing",
                    )
                if expected_source_rev is not None and str(source_rev or "") != str(
                    expected_source_rev
                ):
                    raise ChatBranchConflictError(
                        "The source parent message changed before the repair committed.",
                        code="rewind_source_changed",
                    )

                # Lock hidden chats in deterministic id order, then verify both
                # their selected pointer/chat revision and selected message row
                # revision. This closes the preflight→commit race without copying
                # hidden transcripts into the parent transaction.
                related_by_id = {
                    str(item.get("chat_id") or ""): item
                    for item in expected_related_leaves
                    if isinstance(item, dict) and item.get("chat_id")
                }
                related_ids = sorted(related_by_id)
                if related_ids:
                    locked_related = (
                        db.execute(
                            select(Chat)
                            .where(Chat.id.in_(related_ids))
                            .order_by(Chat.id)
                            .with_for_update()
                        )
                        .scalars()
                        .all()
                    )
                    locked_map = {str(row.id): row for row in locked_related}
                    if set(locked_map) != set(related_ids):
                        raise ChatBranchConflictError(
                            "A repaired subagent chat no longer exists.",
                            code="rewind_child_missing",
                        )

                    for child_id in related_ids:
                        expected = related_by_id[child_id]
                        child = locked_map[child_id]
                        child_parent = getattr(child, "subagent_of", None) or (
                            child.meta if isinstance(child.meta, dict) else {}
                        ).get("subagent_of")
                        if str(child.user_id or "") != str(user_id) or str(
                            child_parent or ""
                        ) != str(id):
                            raise ChatBranchConflictError(
                                "A repaired subagent no longer belongs to this parent chat.",
                                code="rewind_child_parent_mismatch",
                            )

                        child_chat = child.chat if isinstance(child.chat, dict) else {}
                        child_history = (
                            child_chat.get("history")
                            if isinstance(child_chat.get("history"), dict)
                            else {}
                        )
                        expected_leaf_id = str(expected.get("message_id") or "")
                        if (
                            str(child_history.get("currentId") or "")
                            != expected_leaf_id
                        ):
                            raise ChatBranchConflictError(
                                "A repaired subagent selected a different answer before commit.",
                                code="rewind_child_selection_changed",
                            )

                        expected_chat_rev = expected.get("chat_rev")
                        if expected_chat_rev is not None:
                            live_chat_rev = db.execute(
                                text("SELECT xmin::text FROM chat WHERE id = :id"),
                                {"id": child_id},
                            ).scalar()
                            if str(live_chat_rev or "") != str(expected_chat_rev):
                                raise ChatBranchConflictError(
                                    "A repaired subagent chat changed before commit.",
                                    code="rewind_child_changed",
                                )

                        expected_message_rev = expected.get("message_rev")
                        if getattr(child, "messages_migrated", 0):
                            child_message_row = db.execute(
                                text(
                                    "SELECT xmin::text FROM chat_message "
                                    "WHERE chat_id = :cid AND message_id = :mid "
                                    "FOR UPDATE"
                                ),
                                {"cid": child_id, "mid": expected_leaf_id},
                            ).first()
                            if child_message_row is None:
                                raise ChatBranchConflictError(
                                    "A repaired subagent answer no longer exists.",
                                    code="rewind_child_answer_missing",
                                )
                            if expected_message_rev is not None and str(
                                child_message_row[0] or ""
                            ) != str(expected_message_rev):
                                raise ChatBranchConflictError(
                                    "A repaired subagent answer changed before commit.",
                                    code="rewind_child_answer_changed",
                                )

                # The branch and source must stay siblings. Never trust a stale
                # client parentId when the locked source row is authoritative.
                branch_message["id"] = branch_message_id
                branch_message["parentId"] = source_message.get("parentId")
                branch_message.setdefault("childrenIds", [])
                # Generic marker shared by rewind-adopt and rewind-rerun.
                # Continue recognizing the legacy rewind_adopt_operation above
                # so an ambiguous retry of an older committed request remains
                # idempotent after deployment.
                branch_message.pop("rewind_adopt_operation", None)
                operation_marker = branch_message.get("rewind_operation")
                operation_kind = (
                    operation_marker.get("kind")
                    if isinstance(operation_marker, dict)
                    else None
                )
                branch_message["rewind_operation"] = {
                    "id": operation_id,
                    "source_message_id": source_message_id,
                    **({"kind": operation_kind} if operation_kind else {}),
                }
                if isinstance(branch_message.get("content_blocks"), list):
                    branch_message["content_blocks"] = sanitize_content_blocks(
                        branch_message["content_blocks"]
                    )
                carry_tool_result_bodies_from_source(source_message, branch_message)
                carry_reasoning_rounds_from_source(source_message, branch_message)

                parent_message_id = source_message.get("parentId")
                commit_updated_at = int(time.time())
                if migrated:
                    if parent_message_id:
                        parent_row = db.execute(
                            text(
                                f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                                "FROM chat_message "
                                "WHERE chat_id = :cid AND message_id = :mid "
                                "FOR UPDATE"
                            ),
                            {"cid": id, "mid": parent_message_id},
                        ).first()
                        if parent_row is None:
                            raise ChatBranchConflictError(
                                "The rewind source's parent message is missing.",
                                code="rewind_graph_parent_missing",
                            )
                        parent_message = _row_to_message_dict(parent_row)
                        children = list(parent_message.get("childrenIds") or [])
                        if branch_message_id not in children:
                            children.append(branch_message_id)
                        db.execute(
                            text(
                                "UPDATE chat_message SET meta = jsonb_set("
                                "  COALESCE(meta, '{}'::jsonb), '{childrenIds}', "
                                "  CAST(:children AS jsonb), true"
                                ") WHERE chat_id = :cid AND message_id = :mid"
                            ),
                            {
                                "children": _dumps_jsonb(children),
                                "cid": id,
                                "mid": parent_message_id,
                            },
                        )

                    cols = _split_message_for_table(branch_message)
                    sequence = _next_sequence_for_chat(db, id)
                    db.execute(
                        text(
                            "INSERT INTO chat_message "
                            "(chat_id, message_id, parent_id, role, content, "
                            " content_is_json, model, timestamp, sequence, "
                            " status_history, meta) "
                            "VALUES (:cid, :mid, :pid, :role, :content, :is_json, "
                            " :model, :timestamp, :sequence, "
                            " CAST(:status_history AS jsonb), CAST(:meta AS jsonb))"
                        ),
                        {
                            "cid": id,
                            "mid": branch_message_id,
                            "pid": cols["parent_id"],
                            "role": cols["role"],
                            "content": cols["content"],
                            "is_json": cols["content_is_json"],
                            "model": cols["model"],
                            "timestamp": cols["timestamp"],
                            "sequence": sequence,
                            "status_history": cols["status_history"],
                            "meta": cols["meta"],
                        },
                    )
                    db.execute(
                        text(
                            "UPDATE chat SET "
                            "chat = jsonb_set("
                            "  COALESCE(chat, '{}'::jsonb) #- '{history,messages}', "
                            "  '{history,currentId}', to_jsonb(CAST(:mid AS text)), true"
                            "), updated_at = :updated_at "
                            "WHERE id = :id"
                        ),
                        {
                            "mid": branch_message_id,
                            "updated_at": commit_updated_at,
                            "id": id,
                        },
                    )
                else:
                    legacy_blob = copy.deepcopy(legacy_blob or {})
                    history = dict(legacy_blob.get("history") or {})
                    messages = dict(history.get("messages") or {})
                    if parent_message_id:
                        parent_message = messages.get(parent_message_id)
                        if not isinstance(parent_message, dict):
                            raise ChatBranchConflictError(
                                "The rewind source's parent message is missing.",
                                code="rewind_graph_parent_missing",
                            )
                        parent_message = dict(parent_message)
                        children = list(parent_message.get("childrenIds") or [])
                        if branch_message_id not in children:
                            children.append(branch_message_id)
                        parent_message["childrenIds"] = children
                        messages[parent_message_id] = parent_message
                    messages[branch_message_id] = branch_message
                    history["messages"] = messages
                    history["currentId"] = branch_message_id
                    legacy_blob["history"] = history
                    db.execute(
                        text(
                            "UPDATE chat SET chat = CAST(:chat AS jsonb), "
                            "updated_at = :updated_at WHERE id = :id"
                        ),
                        {
                            "chat": _dumps_jsonb(legacy_blob),
                            "updated_at": commit_updated_at,
                            "id": id,
                        },
                    )

                _upsert_message_search(
                    db,
                    id,
                    branch_message_id,
                    branch_message.get("role"),
                    _extract_content_text(branch_message.get("content", "")),
                )
                db.commit()
                _invalidate_cached_sibling_stubs(id)
                return {
                    "message": branch_message,
                    "idempotent": False,
                    "updated_at": commit_updated_at,
                }
            except Exception:
                db.rollback()
                raise

    def append_messages_atomic(
        self,
        id: str,
        messages: list[dict],
        *,
        current_id: Optional[str] = None,
        update_current_id: bool = False,
        models: Optional[list] = None,
        update_models: bool = False,
        user_id: Optional[str] = None,
    ) -> dict:
        """Append a message chain and select its leaf in one transaction.

        This is the storage boundary for sends, response regenerations, retries,
        and imported message pairs. ``parent_id`` is the durable graph edge;
        ``childrenIds`` is never consulted for correctness. Existing ids are
        accepted only as idempotent retries of the same logical append.
        """
        incoming_by_id: dict[str, dict] = {}
        for raw_message in messages:
            if not isinstance(raw_message, dict):
                raise ChatBranchConflictError(
                    "Every appended message must be an object.",
                    code="chat_message_append_invalid",
                )
            message = copy.deepcopy(raw_message)
            message_id = str(message.get("id") or "")
            if not message_id or message_id in incoming_by_id:
                raise ChatBranchConflictError(
                    "Appended message ids must be present and unique.",
                    code="chat_message_append_id_invalid",
                )
            message["id"] = message_id
            message["parentId"] = (
                str(message.get("parentId"))
                if message.get("parentId") is not None
                else None
            )
            if not message.get("role"):
                raise ChatBranchConflictError(
                    "An appended message requires a role.",
                    code="chat_message_append_role_missing",
                )
            message["childrenIds"] = []
            incoming_by_id[message_id] = message

        # Topologically order parents that are part of this same batch. This
        # keeps the API robust to caller order and rejects an in-batch cycle
        # before any row is written.
        ordered_messages: list[dict] = []
        visit_state: dict[str, int] = {}

        def visit(message_id: str) -> None:
            state = visit_state.get(message_id, 0)
            if state == 2:
                return
            if state == 1:
                raise ChatBranchConflictError(
                    "The appended messages contain a parent cycle.",
                    code="chat_message_parent_cycle",
                )
            visit_state[message_id] = 1
            message = incoming_by_id[message_id]
            parent_id = message.get("parentId")
            if parent_id in incoming_by_id:
                visit(str(parent_id))
            visit_state[message_id] = 2
            ordered_messages.append(message)

        for message_id in incoming_by_id:
            visit(message_id)

        with get_db() as db:
            try:
                chat_row = (
                    db.query(Chat).filter(Chat.id == id).with_for_update().first()
                )
                if chat_row is None or (
                    user_id is not None and str(chat_row.user_id or "") != str(user_id)
                ):
                    raise ChatBranchConflictError(
                        "The chat is no longer accessible.",
                        code="chat_access_prohibited",
                    )

                migrated = bool(
                    _chat_message_table_supported(db)
                    and getattr(chat_row, "messages_migrated", 0)
                )
                chat_data = (
                    copy.deepcopy(chat_row.chat)
                    if isinstance(chat_row.chat, dict)
                    else {}
                )
                history = dict(chat_data.get("history") or {})
                legacy_messages: Optional[dict] = None
                if migrated:
                    history.pop("messages", None)
                else:
                    legacy_messages = dict(history.get("messages") or {})

                next_sequence = _next_sequence_for_chat(db, id) if migrated else 0
                canonical_messages: list[dict] = []
                inserted_count = 0

                for incoming in ordered_messages:
                    message = copy.deepcopy(incoming)
                    message_id = str(message["id"])
                    copy_source_id = message.pop(
                        "_copy_tool_result_bodies_from", None
                    )

                    existing: Optional[dict]
                    if migrated:
                        existing_row = db.execute(
                            text(
                                f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                                "FROM chat_message "
                                "WHERE chat_id = :cid AND message_id = :mid "
                                "FOR UPDATE"
                            ),
                            {"cid": id, "mid": message_id},
                        ).first()
                        existing = (
                            _row_to_message_dict(existing_row)
                            if existing_row is not None
                            else None
                        )
                    else:
                        existing = legacy_messages.get(message_id)

                    if isinstance(existing, dict):
                        if not _append_identity_matches(existing, message):
                            raise ChatBranchConflictError(
                                "The appended message id is already in use.",
                                code="chat_message_append_id_conflict",
                            )
                        canonical_messages.append(existing)
                        continue

                    _require_durable_message_parent(
                        db,
                        chat_id=id,
                        message_id=message_id,
                        parent_id=message.get("parentId"),
                        migrated=migrated,
                        legacy_messages=legacy_messages,
                    )

                    if copy_source_id:
                        source_message: Optional[dict] = None
                        if migrated:
                            source_row = db.execute(
                                text(
                                    f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                                    "FROM chat_message "
                                    "WHERE chat_id = :cid AND message_id = :mid "
                                    "FOR UPDATE"
                                ),
                                {"cid": id, "mid": str(copy_source_id)},
                            ).first()
                            if source_row is not None:
                                source_message = _row_to_message_dict(source_row)
                        else:
                            source_message = legacy_messages.get(str(copy_source_id))
                        if not isinstance(source_message, dict):
                            raise ChatBranchConflictError(
                                "The retry source message no longer exists.",
                                code="chat_message_copy_source_missing",
                            )
                        carry_tool_result_bodies_from_source(source_message, message)
                        carry_reasoning_rounds_from_source(source_message, message)

                    if migrated:
                        cols = _split_message_for_table(message)
                        db.execute(
                            text(
                                "INSERT INTO chat_message "
                                "(chat_id, message_id, parent_id, role, content, "
                                " content_is_json, model, timestamp, sequence, "
                                " status_history, meta) "
                                "VALUES (:cid, :mid, :pid, :role, :content, "
                                " :is_json, :model, :timestamp, :sequence, "
                                " CAST(:status_history AS jsonb), "
                                " CAST(:meta AS jsonb))"
                            ),
                            {
                                "cid": id,
                                "mid": message_id,
                                "pid": cols["parent_id"],
                                "role": cols["role"],
                                "content": cols["content"],
                                "is_json": cols["content_is_json"],
                                "model": cols["model"],
                                "timestamp": cols["timestamp"],
                                "sequence": next_sequence,
                                "status_history": cols["status_history"],
                                "meta": cols["meta"],
                            },
                        )
                        next_sequence += 1
                    else:
                        parent_id = message.get("parentId")
                        if parent_id:
                            parent = dict(legacy_messages[parent_id])
                            children = list(parent.get("childrenIds") or [])
                            if message_id not in children:
                                children.append(message_id)
                            parent["childrenIds"] = children
                            legacy_messages[parent_id] = parent
                        legacy_messages[message_id] = message

                    _upsert_message_search(
                        db,
                        id,
                        message_id,
                        message.get("role"),
                        _extract_content_text(message.get("content", "")),
                    )
                    canonical_messages.append(message)
                    inserted_count += 1

                if update_current_id:
                    target_id = str(current_id) if current_id is not None else None
                    if target_id:
                        if migrated:
                            target_exists = db.execute(
                                text(
                                    "SELECT 1 FROM chat_message "
                                    "WHERE chat_id = :cid AND message_id = :mid"
                                ),
                                {"cid": id, "mid": target_id},
                            ).first()
                            if target_exists is None:
                                raise ChatBranchConflictError(
                                    "The selected appended branch does not exist.",
                                    code="chat_message_current_missing",
                                )
                        elif target_id not in legacy_messages:
                            raise ChatBranchConflictError(
                                "The selected appended branch does not exist.",
                                code="chat_message_current_missing",
                            )
                    history["currentId"] = target_id

                if update_models:
                    chat_data["models"] = list(models or [])
                    chat_row.model_id_primary = (models or [None])[0]

                if migrated:
                    history.pop("messages", None)
                else:
                    history["messages"] = legacy_messages
                chat_data["history"] = history

                updated_at = int(time.time())
                chat_row.chat = chat_data
                chat_row.updated_at = updated_at
                db.commit()
                _invalidate_cached_sibling_stubs(id)
                return {
                    "messages": canonical_messages,
                    "updated_at": updated_at,
                    "idempotent": inserted_count == 0,
                }
            except Exception:
                db.rollback()
                raise

    def fork_message_version_atomic(
        self,
        id: str,
        source_message_id: str,
        message_id: str,
        *,
        content: Any,
        files: Any = None,
        models: Optional[list] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """Atomically append and select one manually edited sibling version.

        The source row, not the client, determines the new version's parent and
        role. This makes editing an append-only graph operation at the storage
        boundary: the source can never be overwritten, a missing parent cannot
        create a new orphan, and the new row + currentId pointer commit together.
        """
        source_message_id = str(source_message_id)
        message_id = str(message_id)
        if not source_message_id or not message_id or source_message_id == message_id:
            raise ChatBranchConflictError(
                "A version requires distinct source and destination message ids.",
                code="message_version_identity_invalid",
            )

        with get_db() as db:
            try:
                chat_row = (
                    db.query(Chat).filter(Chat.id == id).with_for_update().first()
                )
                if chat_row is None or (
                    user_id is not None and str(chat_row.user_id or "") != str(user_id)
                ):
                    raise ChatBranchConflictError(
                        "The chat is no longer accessible.",
                        code="chat_access_prohibited",
                    )

                migrated = bool(
                    _chat_message_table_supported(db)
                    and getattr(chat_row, "messages_migrated", 0)
                )
                legacy_messages: Optional[dict] = None
                existing: Optional[dict] = None
                if migrated:
                    source_row = db.execute(
                        text(
                            f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                            "FROM chat_message "
                            "WHERE chat_id = :cid AND message_id = :mid "
                            "FOR UPDATE"
                        ),
                        {"cid": id, "mid": source_message_id},
                    ).first()
                    source = (
                        _row_to_message_dict(source_row)
                        if source_row is not None
                        else None
                    )
                    target_row = db.execute(
                        text(
                            f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                            "FROM chat_message "
                            "WHERE chat_id = :cid AND message_id = :mid "
                            "FOR UPDATE"
                        ),
                        {"cid": id, "mid": message_id},
                    ).first()
                    if target_row is not None:
                        existing = _row_to_message_dict(target_row)
                        if existing.get("sourceMessageId") != source_message_id:
                            raise ChatBranchConflictError(
                                "The destination message id is already in use.",
                                code="message_version_id_conflict",
                            )
                        version = existing
                    else:
                        version = None
                else:
                    chat_data = (
                        copy.deepcopy(chat_row.chat)
                        if isinstance(chat_row.chat, dict)
                        else {}
                    )
                    history = dict(chat_data.get("history") or {})
                    legacy_messages = dict(history.get("messages") or {})
                    source = legacy_messages.get(source_message_id)
                    existing = legacy_messages.get(message_id)
                    if isinstance(existing, dict):
                        if existing.get("sourceMessageId") != source_message_id:
                            raise ChatBranchConflictError(
                                "The destination message id is already in use.",
                                code="message_version_id_conflict",
                            )
                        version = existing
                    else:
                        version = None

                if not isinstance(source, dict):
                    raise ChatBranchConflictError(
                        "The message being edited no longer exists.",
                        code="message_version_source_missing",
                    )
                role = source.get("role")
                if role not in {"user", "assistant"}:
                    raise ChatBranchConflictError(
                        "Only user and assistant messages can be versioned.",
                        code="message_version_role_invalid",
                    )

                parent_id = source.get("parentId")
                if version is None:
                    version = {
                        "id": message_id,
                        "parentId": parent_id,
                        "childrenIds": [],
                        "role": role,
                        "content": content,
                        "sourceMessageId": source_message_id,
                        "manuallyEdited": True,
                        "timestamp": int(time.time()),
                    }
                    if role == "user":
                        if files is not None:
                            version["files"] = files
                        source_models = source.get("models")
                        if isinstance(models, list) and models:
                            version["models"] = models
                        elif isinstance(source_models, list):
                            version["models"] = source_models
                    else:
                        for key in ("model", "modelName", "modelIdx"):
                            if source.get(key) is not None:
                                version[key] = source.get(key)
                        version["done"] = True

                    _require_durable_message_parent(
                        db,
                        chat_id=id,
                        message_id=message_id,
                        parent_id=parent_id,
                        migrated=migrated,
                        legacy_messages=legacy_messages,
                    )

                    if migrated:
                        cols = _split_message_for_table(version)
                        db.execute(
                            text(
                                "INSERT INTO chat_message "
                                "(chat_id, message_id, parent_id, role, content, "
                                " content_is_json, model, timestamp, sequence, "
                                " status_history, meta) "
                                "VALUES (:cid, :mid, :pid, :role, :content, "
                                " :is_json, :model, :timestamp, :sequence, "
                                " CAST(:status_history AS jsonb), "
                                " CAST(:meta AS jsonb))"
                            ),
                            {
                                "cid": id,
                                "mid": message_id,
                                "pid": cols["parent_id"],
                                "role": cols["role"],
                                "content": cols["content"],
                                "is_json": cols["content_is_json"],
                                "model": cols["model"],
                                "timestamp": cols["timestamp"],
                                "sequence": _next_sequence_for_chat(db, id),
                                "status_history": cols["status_history"],
                                "meta": cols["meta"],
                            },
                        )
                    else:
                        if parent_id:
                            parent = dict(legacy_messages[parent_id])
                            children = list(parent.get("childrenIds") or [])
                            if message_id not in children:
                                children.append(message_id)
                            parent["childrenIds"] = children
                            legacy_messages[parent_id] = parent
                        legacy_messages[message_id] = version
                        history["messages"] = legacy_messages
                        chat_data["history"] = history

                if migrated:
                    chat_data = (
                        dict(chat_row.chat) if isinstance(chat_row.chat, dict) else {}
                    )
                history = dict(chat_data.get("history") or {})
                if migrated:
                    history.pop("messages", None)
                else:
                    history["messages"] = legacy_messages
                history["currentId"] = message_id
                chat_data["history"] = history
                updated_at = int(time.time())
                chat_row.chat = chat_data
                chat_row.updated_at = updated_at

                _upsert_message_search(
                    db,
                    id,
                    message_id,
                    version.get("role"),
                    _extract_content_text(version.get("content", "")),
                )
                db.commit()
                _invalidate_cached_sibling_stubs(id)
                return {
                    "message": version,
                    "updated_at": updated_at,
                    "idempotent": existing is not None,
                }
            except Exception:
                db.rollback()
                raise

    def upsert_message_to_chat_by_id_and_message_id(
        self,
        id: str,
        message_id: str,
        message: dict,
        return_model: bool = True,
        prune_tool_result_bodies: bool = False,
    ) -> Optional[ChatModel]:
        """Insert or merge a single message into a chat.

        ``return_model=True`` (default) returns the full refreshed ``ChatModel``.
        Building that return value requires re-reading EVERY message row and
        re-validating the whole chat (``_hydrate_chat_messages`` →
        ``_normalize_message_graph`` is up to O(N²)), which is pure waste on the
        streaming/agentic hot path where the caller discards the result. The
        agentic tool loop calls this 2× per round plus per checkpoint, so at N
        rounds the discarded hydration is O(N²)/O(N³) of dead work. Those callers
        pass ``return_model=False`` to commit and return ``None`` immediately.
        Structural/manual edits do not use this mutator; they go through the
        append-only fork transaction above."""
        # Sanitize message content for null characters before upserting
        if isinstance(message.get("content"), str):
            message["content"] = message["content"].replace("\x00", "")
        # Structural invariant: content_blocks is a dense list of dicts — this
        # is the single write path every message persist funnels through, so
        # null/non-dict entries (a degraded client's serialized array holes)
        # can never reach a durable row.
        if isinstance(message.get("content_blocks"), list):
            message["content_blocks"] = sanitize_content_blocks(
                message["content_blocks"]
            )

        with get_db() as db:
            # Lock the chat row before touching a message row. Whole-chat
            # rewrites and guarded subagent transitions use this same order,
            # avoiding the message→chat / chat→message deadlock cycle.
            chat_obj = db.query(Chat).filter(Chat.id == id).with_for_update().first()
            if chat_obj is None:
                return None

            migrated = bool(
                _chat_message_table_supported(db)
                and getattr(chat_obj, "messages_migrated", 0)
            )

            if migrated:
                # Fast path: write a single row to chat_message instead of a
                # full JSON read-modify-write. Look up the existing row (if
                # any) so we can spread the incoming partial dict on top —
                # same merge semantics as the legacy ``{**existing, **incoming}``.
                existing_row = db.execute(
                    text(
                        f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                        "FROM chat_message WHERE chat_id = :cid AND message_id = :mid"
                    ),
                    {"cid": id, "mid": message_id},
                ).fetchone()

                if existing_row is None:
                    merged = dict(message)
                    merged["id"] = message_id
                    if prune_tool_result_bodies:
                        _prune_message_tool_result_bodies_inplace(merged)

                    _require_durable_message_parent(
                        db,
                        chat_id=id,
                        message_id=message_id,
                        parent_id=merged.get("parentId"),
                        migrated=True,
                    )
                    seq = _next_sequence_for_chat(db, id)
                    cols = _split_message_for_table(merged)
                    db.execute(
                        text(
                            "INSERT INTO chat_message "
                            "(chat_id, message_id, parent_id, role, content, "
                            " content_is_json, model, timestamp, sequence, "
                            " status_history, meta) "
                            "VALUES (:cid, :mid, :pid, :role, :c, :ij, "
                            ":model, :ts, :seq, CAST(:sh AS jsonb), CAST(:meta AS jsonb))"
                        ),
                        {
                            "cid": id,
                            "mid": message_id,
                            "seq": seq,
                            "pid": cols["parent_id"],
                            "role": cols["role"],
                            "c": cols["content"],
                            "ij": cols["content_is_json"],
                            "model": cols["model"],
                            "ts": cols["timestamp"],
                            "sh": cols["status_history"],
                            "meta": cols["meta"],
                        },
                    )
                else:
                    existing_msg = _row_to_message_dict(existing_row)
                    merged = {**existing_msg, **message}
                    # tool_result_bodies is ACCUMULATE-ONLY. A checkpoint whose
                    # in-memory map is INCOMPLETE (the RAM store was wiped mid-
                    # generation, a prior attempt's bodies, or a write race) must
                    # never SHRINK the durable map — that exact shrink (a shallow
                    # {**existing, **incoming} replacing a full map with a near-
                    # empty one) corrupted a production row and bricked the turn
                    # with "Missing tool result body for ref ...". Union the
                    # incoming bodies OVER the existing ones so keys can only be
                    # added or refreshed, never dropped.
                    if isinstance(message.get("tool_result_bodies"), dict):
                        _existing_bodies = existing_msg.get("tool_result_bodies")
                        if isinstance(_existing_bodies, dict) and _existing_bodies:
                            merged["tool_result_bodies"] = {
                                **_existing_bodies,
                                **message["tool_result_bodies"],
                            }
                    merged["id"] = message_id
                    if prune_tool_result_bodies:
                        _prune_message_tool_result_bodies_inplace(merged)

                    # Validate the durable edge on *every* write, not only when
                    # the incoming partial explicitly changes parentId. A
                    # checkpoint is commonly content/meta-only; allowing it to
                    # update an already-orphaned row made a delete race durable
                    # and let the writer keep extending a broken branch.
                    _require_durable_message_parent(
                        db,
                        chat_id=id,
                        message_id=message_id,
                        parent_id=merged.get("parentId"),
                        migrated=True,
                    )
                    cols = _split_message_for_table(merged)
                    db.execute(
                        text(
                            "UPDATE chat_message SET "
                            "  parent_id = :pid, "
                            "  role = :role, "
                            "  content = :c, "
                            "  content_is_json = :ij, "
                            "  model = :model, "
                            "  timestamp = :ts, "
                            "  status_history = CAST(:sh AS jsonb), "
                            "  meta = CAST(:meta AS jsonb) "
                            "WHERE chat_id = :cid AND message_id = :mid"
                        ),
                        {
                            "cid": id,
                            "mid": message_id,
                            "pid": cols["parent_id"],
                            "role": cols["role"],
                            "c": cols["content"],
                            "ij": cols["content_is_json"],
                            "model": cols["model"],
                            "ts": cols["timestamp"],
                            "sh": cols["status_history"],
                            "meta": cols["meta"],
                        },
                    )

                _upsert_message_search(
                    db,
                    id,
                    message_id,
                    merged.get("role"),
                    _extract_content_text(merged.get("content", "")),
                )

                # Targeted JSON manipulation: only flip history.currentId so
                # the on-disk JSON stays consistent for the unmigrated read
                # path that might be re-enabled. Avoid touching the rest of
                # the 100+ MB JSON blob.
                try:
                    db.execute(
                        text(
                            "UPDATE chat SET "
                            "  chat = jsonb_set("
                            "    COALESCE(chat, '{}'::jsonb) #- '{history,messages}', "
                            "    '{history,currentId}', to_jsonb(CAST(:mid AS text)), true"
                            "  ), "
                            "  updated_at = :ts "
                            "WHERE id = :id"
                        ),
                        {"mid": message_id, "ts": int(time.time()), "id": id},
                    )
                except Exception:
                    # As a final fallback, just bump updated_at via UPDATE.
                    try:
                        db.execute(
                            text("UPDATE chat SET updated_at = :ts WHERE id = :id"),
                            {"ts": int(time.time()), "id": id},
                        )
                    except Exception:
                        pass

                try:
                    db.commit()
                except Exception:
                    db.rollback()
                    return None

                if not return_model:
                    # Hot path: skip the full re-hydration + Pydantic validation
                    # (the caller discards the model). This is the O(N²) the
                    # agentic loop pays per write.
                    return None

                refreshed = db.get(Chat, id)
                _hydrate_chat_messages(db, refreshed)
                try:
                    return ChatModel.model_validate(refreshed)
                except Exception:
                    return None

            # Legacy path: unmigrated chat. Hot path for streaming token
            # writes — we deliberately avoid ``update_chat_by_id`` here
            # because its ``_upsert_chat_search`` refreshes every message search
            # row in the chat. That is O(N) for an N-message chat.
            # Instead we patch the JSON in place and refresh only the one
            # changed message in ``chat_message_search``.
            try:
                chat_data = dict(chat_obj.chat or {})
                history = dict(chat_data.get("history") or {})
                messages_map = dict(history.get("messages") or {})

                _existing_leg = messages_map.get(message_id)
                if isinstance(_existing_leg, dict):
                    merged = {**_existing_leg, **message}
                    # Same accumulate-only union as the migrated branch: a partial
                    # write carrying an incomplete tool_result_bodies map must never
                    # shrink the durable one.
                    if isinstance(message.get("tool_result_bodies"), dict):
                        _existing_leg_bodies = (
                            _existing_leg.get("tool_result_bodies")
                            if isinstance(_existing_leg, dict)
                            else None
                        )
                        if (
                            isinstance(_existing_leg_bodies, dict)
                            and _existing_leg_bodies
                        ):
                            merged["tool_result_bodies"] = {
                                **_existing_leg_bodies,
                                **message["tool_result_bodies"],
                            }
                else:
                    merged = dict(message)

                merged["id"] = message_id
                parent_link_write = (
                    not isinstance(_existing_leg, dict) or "parentId" in message
                )
                _require_durable_message_parent(
                    db,
                    chat_id=id,
                    message_id=message_id,
                    parent_id=merged.get("parentId"),
                    migrated=False,
                    legacy_messages=messages_map,
                )
                if parent_link_write:

                    old_parent_id = (
                        _existing_leg.get("parentId")
                        if isinstance(_existing_leg, dict)
                        else None
                    )
                    new_parent_id = merged.get("parentId")
                    if old_parent_id and old_parent_id != new_parent_id:
                        old_parent = messages_map.get(old_parent_id)
                        if isinstance(old_parent, dict):
                            old_parent = dict(old_parent)
                            old_parent["childrenIds"] = [
                                child_id
                                for child_id in old_parent.get("childrenIds") or []
                                if child_id != message_id
                            ]
                            messages_map[old_parent_id] = old_parent
                    if new_parent_id:
                        new_parent = dict(messages_map[new_parent_id])
                        children = list(new_parent.get("childrenIds") or [])
                        if message_id not in children:
                            children.append(message_id)
                        new_parent["childrenIds"] = children
                        messages_map[new_parent_id] = new_parent

                messages_map[message_id] = merged

                if prune_tool_result_bodies:
                    _prune_message_tool_result_bodies_inplace(messages_map[message_id])

                history["messages"] = messages_map
                history["currentId"] = message_id
                chat_data["history"] = history

                chat_obj.chat = chat_data
                chat_obj.updated_at = int(time.time())
                _invalidate_cached_sibling_stubs(id)
                db.commit()

                merged = messages_map[message_id]
                role = str(merged.get("role", ""))
                content = _extract_content_text(merged.get("content", ""))

                # chat_search.body drifts slightly between message writes;
                # per-message search keeps recall correct in the meantime.
                _upsert_message_search(db, id, message_id, role, content)
                try:
                    db.commit()
                except Exception:
                    pass

                if not return_model:
                    return None

                return ChatModel.model_validate(chat_obj)
            except ChatMessageParentMissingError:
                raise
            except Exception:
                return None

    def _lock_chat_message_for_update(
        self, db, id: str, message_id: str
    ) -> tuple[Optional[Chat], bool, dict, bool]:
        """Lock ``chat`` then one message and return its live representation.

        Every graph/body transaction in this model follows chat→message lock
        order. Keeping the read and the subsequent mutator/write in the same DB
        transaction makes the operation lossless across multiple server workers;
        the async per-message lock remains only a cheap same-process queue.
        """
        chat_obj = db.query(Chat).filter(Chat.id == id).with_for_update().first()
        if chat_obj is None:
            return None, False, {}, False

        migrated = bool(
            _chat_message_table_supported(db)
            and getattr(chat_obj, "messages_migrated", 0)
        )
        if migrated:
            row = db.execute(
                text(
                    f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                    "FROM chat_message "
                    "WHERE chat_id = :cid AND message_id = :mid "
                    "FOR UPDATE"
                ),
                {"cid": id, "mid": message_id},
            ).first()
            return (
                chat_obj,
                True,
                _row_to_message_dict(row) if row is not None else {},
                row is not None,
            )

        chat_data = chat_obj.chat if isinstance(chat_obj.chat, dict) else {}
        messages = (chat_data.get("history") or {}).get("messages") or {}
        existing = messages.get(message_id) if isinstance(messages, dict) else None
        return (
            chat_obj,
            False,
            copy.deepcopy(existing) if isinstance(existing, dict) else {},
            isinstance(existing, dict),
        )

    def update_message_fields_atomic(
        self, id: str, message_id: str, mutator: Callable[[dict], Optional[dict]]
    ) -> Optional[dict]:
        """Read a message, compute a partial update from it, and merge-write it
        back — as one logical operation.

        ``mutator`` receives the CURRENT persisted message dict (``{}`` if the
        message doesn't exist yet) and returns a partial-update dict of the
        top-level keys to change (or ``None``/empty to skip the write). The
        partial is then merged exactly like ``upsert_message_to_chat_by_id_and_message_id``.

        The read, mutator, and merge-write run while the chat/message rows are
        locked in one DB transaction, so the guarantee holds across workers as
        well as within this process.

        Returns the merged run/patch the mutator produced (its return value),
        or ``None`` when nothing was written.
        """
        try:
            with get_db() as db:
                chat_obj, _migrated, existing, _row_exists = (
                    self._lock_chat_message_for_update(db, id, message_id)
                )
                if chat_obj is None:
                    return None
                try:
                    partial = mutator(existing)
                except Exception:
                    log.exception("update_message_fields_atomic mutator failed")
                    db.rollback()
                    return None
                if not partial:
                    db.rollback()
                    return None
                # get_db() is context-local inside run_sync_db, so the nested
                # upsert reuses this same locked transaction and commits it.
                self.upsert_message_to_chat_by_id_and_message_id(
                    id, message_id, partial, return_model=False
                )
                return partial
        except Exception:
            log.exception(
                "update_message_fields_atomic failed for %s/%s", id, message_id
            )
            return None

    def update_generation_message_if_current(
        self,
        id: str,
        message_id: str,
        generation_id: str,
        turn_id: str,
        fields: dict,
        *,
        create_if_missing: bool = False,
        require_unfinished: bool = False,
    ) -> bool:
        """Merge fields only while one generation still owns an assistant row.

        Assistant message ids are intentionally reused by continue/retry flows.
        Delayed Stop requests and detached terminal finalizers must therefore
        compare both pieces of run identity under the same run lock as the
        write. Queue startup may opt into creating a missing row; the chat-row
        lock serializes that creation against every competing generation write.

        ``require_unfinished`` additionally refuses a row that already reached
        ``done``. Stop uses it so a click that lands in the moment a turn
        completes cannot relabel a complete answer as cancelled. A live run
        always carries ``done: False`` (every start path stamps it), so this
        only ever rejects work that genuinely finished first.
        """
        generation_id = str(generation_id or "")
        turn_id = str(turn_id or "")
        if (
            not id
            or not message_id
            or not generation_id
            or not turn_id
            or not isinstance(fields, dict)
            or not fields
        ):
            return False

        def _update_if_current(message: dict) -> Optional[dict]:
            if message:
                if message.get("role") != "assistant":
                    return None
                if str(message.get("generation_id") or "") != generation_id:
                    return None
                if str(message.get("turn_id") or "") != turn_id:
                    return None
                if require_unfinished and message.get("done") is True:
                    return None
            elif not create_if_missing or fields.get("role") != "assistant":
                return None
            return {
                **copy.deepcopy(fields),
                "generation_id": generation_id,
                "turn_id": turn_id,
            }

        return bool(
            self.update_message_fields_atomic(id, message_id, _update_if_current)
        )

    def mark_generation_stopped_if_current(
        self,
        id: str,
        message_id: str,
        generation_id: str,
        turn_id: str,
        *,
        require_unfinished: bool = False,
    ) -> bool:
        return self.update_generation_message_if_current(
            id,
            message_id,
            generation_id,
            turn_id,
            {"done": True, "userStopped": True},
            require_unfinished=require_unfinished,
        )

    def update_message_subagent_run_atomic(
        self,
        id: str,
        message_id: str,
        subagent_id: str,
        mutator: Callable[[dict], Optional[dict]],
        *,
        precondition: Optional[Callable[[Chat, dict], bool]] = None,
        cross_message_precondition: Optional[Callable[[dict[str, dict]], bool]] = None,
        touch_chat: bool = False,
    ) -> Optional[dict]:
        """Like ``update_message_fields_atomic``, but writes ONLY the single
        ``subagent_runs[subagent_id]`` key (and an optional content_blocks /
        content placeholder patch) via targeted ``jsonb_set`` — never
        re-serializing the whole N-entry ``subagent_runs`` map.

        ``mutator`` is the SAME read-modify-write callable the parent path uses:
        it receives the CURRENT persisted message dict (``{}`` if absent) and
        returns a partial-update dict. It may include the full merged
        ``subagent_runs`` map (so launch slot-reservation can see siblings under
        the lock) plus optional top-level ``content_blocks`` / ``content`` keys.
        We extract only ``subagent_runs[subagent_id]`` from the returned map and
        persist JUST that one run with ``jsonb_set`` — turning the per-run write
        from O(N) serialization (whole map) into O(1) (one run). This removes the
        O(N²) meta write-amplification on big parallel fan-outs.

        Correctness: the chat row and message row are locked before the live
        message is read, and remain locked through the targeted write/commit.
        ``precondition``, when supplied, runs against those exact locked rows
        before the mutator. ``cross_message_precondition`` receives the messages
        on the selected branch while that same root chat lock is held. It is
        used for invariants that span run keys/messages, such as allowing only
        one active turn to mutate a hidden subagent transcript. This lets
        user-initiated repair/rerun paths verify their guards in the same
        transaction that writes the claim, rather than relying on a racy
        preflight read. This is cross-worker atomic; the async per-message lock
        merely avoids needless same-process DB contention.

        ``touch_chat`` rotates the root row validator inside this same commit.
        User-visible message-only operations such as direct result adoption use
        it so cancellation cannot land after the message commit but before a
        separate ETag bump.

        Falls back to the whole-message write (``update_message_fields_atomic``
        semantics) for legacy (non-migrated) chats, where there is no per-message
        row to ``jsonb_set`` against. Returns the partial the mutator produced,
        or ``None`` when nothing was written.
        """
        try:
            with get_db() as db:
                chat_obj, migrated, existing, row_exists = (
                    self._lock_chat_message_for_update(db, id, message_id)
                )
                if chat_obj is None:
                    return None
                if precondition is not None:
                    try:
                        if not precondition(chat_obj, existing):
                            db.rollback()
                            return None
                    except Exception:
                        log.exception(
                            "update_message_subagent_run_atomic precondition failed"
                        )
                        db.rollback()
                        return None
                if cross_message_precondition is not None:
                    try:
                        if migrated:
                            branch_rows = db.execute(
                                text(
                                    "SELECT message_id, parent_id, meta "
                                    "FROM chat_message WHERE chat_id = :cid"
                                ),
                                {"cid": id},
                            ).all()
                            all_messages = {}
                            for row in branch_rows:
                                meta = row[2] if isinstance(row[2], dict) else {}
                                all_messages[str(row[0])] = {
                                    **meta,
                                    "id": str(row[0]),
                                    "parentId": row[1],
                                }
                        else:
                            chat_data = (
                                chat_obj.chat if isinstance(chat_obj.chat, dict) else {}
                            )
                            all_messages = (chat_data.get("history") or {}).get(
                                "messages"
                            ) or {}
                            all_messages = (
                                all_messages if isinstance(all_messages, dict) else {}
                            )

                        chat_data = (
                            chat_obj.chat if isinstance(chat_obj.chat, dict) else {}
                        )
                        current_id = str(
                            ((chat_data.get("history") or {}).get("currentId")) or ""
                        )
                        selected_messages: dict[str, dict] = {}
                        seen: set[str] = set()
                        while (
                            current_id
                            and current_id in all_messages
                            and current_id not in seen
                        ):
                            seen.add(current_id)
                            current = all_messages.get(current_id)
                            if not isinstance(current, dict):
                                break
                            selected_messages[current_id] = current
                            current_id = str(current.get("parentId") or "")

                        if not cross_message_precondition(selected_messages):
                            db.rollback()
                            return None
                    except Exception:
                        log.exception(
                            "update_message_subagent_run_atomic "
                            "cross-message precondition failed"
                        )
                        db.rollback()
                        return None
                try:
                    partial = mutator(existing)
                except Exception:
                    log.exception("update_message_subagent_run_atomic mutator failed")
                    db.rollback()
                    return None
                if not partial:
                    db.rollback()
                    return None

                runs = partial.get("subagent_runs")
                run = (
                    runs.get(subagent_id)
                    if isinstance(runs, dict) and subagent_id
                    else None
                )
                # If the mutator did not produce a target run, or the message is
                # legacy/not inserted yet, merge the full partial while the same
                # chat lock remains held. The nested helper reuses this session.
                if not isinstance(run, dict) or not migrated or not row_exists:
                    # Legacy JSON-blob chat: no chat_message row to target.
                    self.upsert_message_to_chat_by_id_and_message_id(
                        id, message_id, partial, return_model=False
                    )
                    return partial

                # Targeted write: serialize ONLY the one changed run (O(1)),
                # never the whole subagent_runs map. NOTE: jsonb_set CANNOT create
                # a missing INTERMEDIATE object, so a naive
                # jsonb_set(meta, '{subagent_runs,sid}', ...) SILENTLY NO-OPS on the
                # first write (when subagent_runs doesn't exist yet) — losing the
                # run. Branch in SQL: when subagent_runs exists, target the one key
                # (merging, preserving siblings); otherwise create subagent_runs
                # seeded with just this run. Both O(1) in the one serialized run.
                _res = db.execute(
                    text(
                        "UPDATE chat_message SET meta = CASE "
                        "  WHEN jsonb_exists(meta, 'subagent_runs') "
                        "  THEN jsonb_set(meta, ARRAY['subagent_runs', :sid], "
                        "                 CAST(:run AS jsonb), true) "
                        "  ELSE jsonb_set(COALESCE(meta, '{}'::jsonb), "
                        "                 '{subagent_runs}', "
                        "                 jsonb_build_object(:sid, CAST(:run AS jsonb)), "
                        "                 true) "
                        "END "
                        "WHERE chat_id = :cid AND message_id = :mid"
                    ),
                    {
                        "sid": str(subagent_id),
                        "run": _dumps_jsonb(run),
                        "cid": id,
                        "mid": message_id,
                    },
                )
                if (getattr(_res, "rowcount", 0) or 0) == 0:
                    # Defensive fallback: the row was locked above, so this
                    # should only be reachable after unusual trigger behavior.
                    # Keep the same transaction/locks while doing the full merge.
                    self.upsert_message_to_chat_by_id_and_message_id(
                        id, message_id, partial, return_model=False
                    )
                    return partial

                # The placeholder patch (content_blocks / content) is the only
                # other thing the mutator can change. content_blocks lives in
                # meta; content is the dedicated column. Write each only when
                # present, each as its own targeted update — still never
                # touching the rest of the meta blob.
                if "content_blocks" in partial:
                    db.execute(
                        text(
                            "UPDATE chat_message SET "
                            "  meta = jsonb_set("
                            "    COALESCE(meta, '{}'::jsonb), "
                            "    '{content_blocks}', "
                            "    CAST(:cb AS jsonb), true"
                            "  ) "
                            "WHERE chat_id = :cid AND message_id = :mid"
                        ),
                        {
                            "cb": _dumps_jsonb(partial.get("content_blocks")),
                            "cid": id,
                            "mid": message_id,
                        },
                    )
                if "content" in partial:
                    new_content = partial.get("content")
                    is_json = 0
                    if not isinstance(new_content, str):
                        try:
                            new_content = _dumps_jsonb(new_content)
                            is_json = 1
                        except Exception:
                            new_content = ""
                            is_json = 0
                    elif "\x00" in new_content:
                        new_content = new_content.replace("\x00", "")
                    db.execute(
                        text(
                            "UPDATE chat_message SET "
                            "  content = :c, content_is_json = :ij "
                            "WHERE chat_id = :cid AND message_id = :mid"
                        ),
                        {
                            "c": new_content,
                            "ij": is_json,
                            "cid": id,
                            "mid": message_id,
                        },
                    )

                if touch_chat:
                    db.execute(
                        text("UPDATE chat SET updated_at = :ts WHERE id = :id"),
                        {"ts": int(time.time()), "id": id},
                    )

                try:
                    db.commit()
                except Exception:
                    db.rollback()
                    return None
            return partial
        except Exception:
            log.exception(
                "update_message_subagent_run_atomic targeted write failed for "
                "%s/%s/%s",
                id,
                message_id,
                subagent_id,
            )
            return None

    def merge_message_tool_result_bodies(
        self, id: str, message_id: str, bodies: dict
    ) -> bool:
        """Durably UNION a batch of large tool-result bodies into one message's
        ``tool_result_bodies`` map with a single targeted ``jsonb_set`` — never
        re-serializing the rest of ``meta``.

        This is the per-round write-through durability layer for stream protocol
        v2.1's lazy tool results. A large web_search/web_fetch body lives out-of-
        line (the socket RAM store + this persisted map) while the slim result in
        ``content_blocks`` only keeps a ref; the model rehydrates it from the map on
        the next round. Previously a body was only persisted at the coarse
        checkpoint / final save, so a server restart or RAM-store wipe mid-turn lost
        every body accumulated so far and the next round raised "Missing tool result
        body for ref ...". Merging each round's new bodies straight into the durable
        row closes that window.

        Accumulate-only, exactly like the union in
        ``upsert_message_to_chat_by_id_and_message_id``: the ``||`` jsonb concat
        keeps every existing key and overwrites only the incoming ones, so a partial
        batch can never SHRINK the durable map. Runs under the per-(chat, message)
        async write lock (its name is in ``_MESSAGE_SCOPED_WRITE_METHODS``) so it
        can't interleave with the upsert's read-whole-meta → write-whole-meta.

        Returns True on a durable write (or benign INSERT/legacy fallback), False on
        failure — the caller keeps the batch buffered and retries next round.
        """
        if not isinstance(bodies, dict) or not bodies:
            return False
        # Coerce keys to str and keep only dict values: the map is
        # tool_call_id -> body dict, and a stray scalar would corrupt the concat.
        clean = {str(k): v for k, v in bodies.items() if k and isinstance(v, dict)}
        if not clean:
            return False
        try:
            with get_db() as db:
                if not _is_chat_migrated(db, id):
                    # Legacy JSON-blob chat: no chat_message row to jsonb_set
                    # against. The full upsert's union semantics (see above) make
                    # this partial write a MERGE, not a wholesale replace.
                    self.upsert_message_to_chat_by_id_and_message_id(
                        id,
                        message_id,
                        {"tool_result_bodies": clean},
                        return_model=False,
                    )
                    return True

                # Targeted union: COALESCE the existing map (or an empty object),
                # concat the incoming batch on top (|| = shallow union, incoming
                # wins on a key collision), and jsonb_set it straight back — never
                # touching the rest of meta. _dumps_jsonb is NUL-safe: fetched web
                # content can contain \x00, which a raw jsonb write would reject.
                _res = db.execute(
                    text(
                        "UPDATE chat_message SET meta = jsonb_set("
                        "  COALESCE(meta, '{}'::jsonb), "
                        "  '{tool_result_bodies}', "
                        "  COALESCE(meta->'tool_result_bodies', '{}'::jsonb) "
                        "    || CAST(:bodies AS jsonb), "
                        "  true"
                        ") "
                        "WHERE chat_id = :cid AND message_id = :mid"
                    ),
                    {
                        "bodies": _dumps_jsonb(clean),
                        "cid": id,
                        "mid": message_id,
                    },
                )
                if (getattr(_res, "rowcount", 0) or 0) == 0:
                    # The assistant chat_message row isn't inserted yet — the first
                    # round can beat the first checkpoint. Fall back to the full
                    # upsert (which INSERTs the row) so the bodies are never lost;
                    # union semantics still apply on later merges.
                    db.rollback()
                    self.upsert_message_to_chat_by_id_and_message_id(
                        id,
                        message_id,
                        {"tool_result_bodies": clean},
                        return_model=False,
                    )
                    return True

                try:
                    db.commit()
                except Exception:
                    db.rollback()
                    return False
            return True
        except Exception:
            log.exception(
                "merge_message_tool_result_bodies failed for %s/%s", id, message_id
            )
            return False

    def add_message_status_to_chat_by_id_and_message_id(
        self, id: str, message_id: str, status: dict
    ) -> Optional[ChatModel]:
        # Status updates don't touch message content, so search rows are already
        # current.
        try:
            with get_db() as db:
                chat_obj = db.get(Chat, id)
                if chat_obj is None:
                    return None

                migrated = bool(
                    _chat_message_table_supported(db)
                    and getattr(chat_obj, "messages_migrated", 0)
                )

                if migrated:
                    # Update only the status_history column on the chat_message row.
                    row = db.execute(
                        text(
                            "SELECT status_history FROM chat_message "
                            "WHERE chat_id = :cid AND message_id = :mid"
                        ),
                        {"cid": id, "mid": message_id},
                    ).fetchone()
                    if row is None:
                        # Message doesn't exist; nothing to update.
                        return ChatModel.model_validate(chat_obj)
                    parsed = _json_from_db(row[0]) if row[0] else None
                    cur_sh = parsed if isinstance(parsed, list) else []
                    cur_sh.append(status)
                    db.execute(
                        text(
                            "UPDATE chat_message SET status_history = CAST(:sh AS jsonb) "
                            "WHERE chat_id = :cid AND message_id = :mid"
                        ),
                        {"sh": _dumps_jsonb(cur_sh), "cid": id, "mid": message_id},
                    )
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()
                        return None
                    refreshed = db.get(Chat, id)
                    _hydrate_chat_messages(db, refreshed)
                    try:
                        return ChatModel.model_validate(refreshed)
                    except Exception:
                        return None

                # Legacy path: patch the JSON in place; skip FTS rebuild.
                chat_data = dict(chat_obj.chat or {})
                history = dict(chat_data.get("history") or {})
                messages_map = dict(history.get("messages") or {})

                if message_id in messages_map:
                    msg = dict(messages_map[message_id])
                    status_history = list(msg.get("statusHistory", []))
                    status_history.append(status)
                    msg["statusHistory"] = status_history
                    messages_map[message_id] = msg

                history["messages"] = messages_map
                chat_data["history"] = history

                chat_obj.chat = chat_data
                chat_obj.updated_at = int(time.time())
                _invalidate_cached_sibling_stubs(id)
                db.commit()

                return ChatModel.model_validate(chat_obj)
        except Exception:
            return None

    def insert_shared_chat_by_chat_id(self, chat_id: str) -> Optional[ChatModel]:
        with get_db() as db:
            # Get the existing chat to share
            chat = db.get(Chat, chat_id)
            # Hydrate before reading chat.chat so the shared clone gets the
            # real messages (otherwise migrated chats would share an empty
            # history because the on-disk JSON has no messages dict).
            _hydrate_chat_messages(db, chat)
            # Check if the chat is already shared
            if chat.share_id:
                # The clone's user_id is "shared-{chat_id}", never the literal
                # "shared" — look it up by the share_id (clone id) directly.
                existing = self.get_chat_by_id(chat.share_id)
                if existing:
                    return existing
                # share_id points at a deleted clone (orphaned state) — fall
                # through to mint a fresh clone and re-point share_id below,
                # self-healing instead of returning None (which 500s the caller).
            # Create a new chat with the same data, but with a new ID
            shared_chat = ChatModel(
                **{
                    "id": str(uuid.uuid4()),
                    "user_id": f"shared-{chat_id}",
                    "title": chat.title,
                    "chat": chat.chat,
                    "meta": chat.meta,
                    "pinned": chat.pinned,
                    "folder_id": chat.folder_id,
                    "created_at": chat.created_at,
                    "updated_at": int(time.time()),
                }
            )
            shared_result = Chat(**shared_chat.model_dump())
            db.add(shared_result)
            db.commit()
            db.refresh(shared_result)

            # Update the original chat with the share_id
            result = (
                db.query(Chat)
                .filter_by(id=chat_id)
                .update({"share_id": shared_chat.id})
            )
            db.commit()
            return shared_chat if (shared_result and result) else None

    def update_shared_chat_by_chat_id(self, chat_id: str) -> Optional[ChatModel]:
        try:
            with get_db() as db:
                chat = db.get(Chat, chat_id)
                _hydrate_chat_messages(db, chat)
                shared_chat = (
                    db.query(Chat).filter_by(user_id=f"shared-{chat_id}").first()
                )

                if shared_chat is None:
                    return self.insert_shared_chat_by_chat_id(chat_id)

                shared_chat.title = chat.title
                shared_chat.chat = chat.chat
                shared_chat.meta = chat.meta
                shared_chat.pinned = chat.pinned
                shared_chat.folder_id = chat.folder_id
                shared_chat.updated_at = int(time.time())
                db.commit()
                db.refresh(shared_chat)

                return ChatModel.model_validate(shared_chat)
        except Exception:
            return None

    def delete_shared_chat_by_chat_id(self, chat_id: str) -> bool:
        try:
            with get_db() as db:
                db.query(Chat).filter_by(user_id=f"shared-{chat_id}").delete()
                db.commit()

                return True
        except Exception:
            return False

    def unarchive_all_chats_by_user_id(self, user_id: str) -> bool:
        try:
            with get_db() as db:
                db.query(Chat).filter_by(user_id=user_id).update({"archived": False})
                db.commit()
                return True
        except Exception:
            return False

    def update_chat_share_id_by_id(
        self, id: str, share_id: Optional[str]
    ) -> Optional[ChatModel]:
        try:
            with get_db() as db:
                chat = db.get(Chat, id)
                chat.share_id = share_id
                db.commit()
                db.refresh(chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def toggle_chat_pinned_by_id(self, id: str) -> Optional[ChatModel]:
        try:
            with get_db() as db:
                chat = db.get(Chat, id)
                chat.pinned = not chat.pinned
                chat.updated_at = int(time.time())
                db.commit()
                db.refresh(chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def toggle_chat_archive_by_id(self, id: str) -> Optional[ChatModel]:
        try:
            with get_db() as db:
                chat = db.get(Chat, id)
                chat.archived = not chat.archived
                chat.updated_at = int(time.time())
                db.commit()
                db.refresh(chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def archive_all_chats_by_user_id(self, user_id: str) -> bool:
        try:
            with get_db() as db:
                db.query(Chat).filter_by(user_id=user_id).update({"archived": True})
                db.commit()
                return True
        except Exception:
            return False

    def get_archived_chat_list_by_user_id(
        self,
        user_id: str,
        filter: Optional[dict] = None,
        skip: int = 0,
        limit: int = 50,
        include_subagents: bool = False,
    ) -> list[ChatModel]:

        with get_db() as db:
            query = db.query(Chat).filter_by(user_id=user_id, archived=True)
            query = _apply_subagent_filter(query, db, include_subagents)

            if filter:
                query_key = filter.get("query")
                if query_key:
                    query = query.filter(Chat.title.ilike(f"%{query_key}%"))

                order_by = filter.get("order_by")
                direction = filter.get("direction")

                if order_by and direction and getattr(Chat, order_by):
                    if direction.lower() == "asc":
                        query = query.order_by(getattr(Chat, order_by).asc())
                    elif direction.lower() == "desc":
                        query = query.order_by(getattr(Chat, order_by).desc())
                    else:
                        raise ValueError("Invalid direction for ordering")
            else:
                query = query.order_by(Chat.updated_at.desc())

            if skip:
                query = query.offset(skip)
            if limit:
                query = query.limit(limit)

            all_chats = query.all()
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def get_chat_list_by_user_id(
        self,
        user_id: str,
        include_archived: bool = False,
        filter: Optional[dict] = None,
        skip: int = 0,
        limit: int = 50,
        include_subagents: bool = False,
    ) -> list[ChatModel]:
        with get_db() as db:
            query = db.query(Chat).filter_by(user_id=user_id)
            if not include_archived:
                query = query.filter_by(archived=False)
            query = _apply_subagent_filter(query, db, include_subagents)

            if filter:
                query_key = filter.get("query")
                if query_key:
                    query = query.filter(Chat.title.ilike(f"%{query_key}%"))

                order_by = filter.get("order_by")
                direction = filter.get("direction")

                if order_by and direction and getattr(Chat, order_by):
                    if direction.lower() == "asc":
                        query = query.order_by(getattr(Chat, order_by).asc())
                    elif direction.lower() == "desc":
                        query = query.order_by(getattr(Chat, order_by).desc())
                    else:
                        raise ValueError("Invalid direction for ordering")
            else:
                query = query.order_by(Chat.updated_at.desc())

            if skip:
                query = query.offset(skip)
            if limit:
                query = query.limit(limit)

            all_chats = query.all()
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def get_chat_title_id_list_by_user_id(
        self,
        user_id: str,
        include_archived: bool = False,
        include_folders: bool = False,
        include_pinned: bool = False,
        skip: Optional[int] = None,
        limit: Optional[int] = None,
        include_subagents: bool = False,
    ) -> list[ChatTitleIdResponse]:
        with get_db() as db:
            query = db.query(Chat).filter_by(user_id=user_id)

            if not include_folders:
                query = query.filter_by(folder_id=None)

            if not include_pinned:
                query = query.filter(or_(Chat.pinned == False, Chat.pinned == None))

            if not include_archived:
                query = query.filter_by(archived=False)

            query = _apply_subagent_filter(query, db, include_subagents)

            query = query.order_by(Chat.updated_at.desc()).with_entities(
                Chat.id, Chat.title, Chat.updated_at, Chat.created_at
            )

            if skip:
                query = query.offset(skip)
            if limit:
                query = query.limit(limit)

            all_chats = query.all()

            # result has to be destructured from sqlalchemy `row` and mapped to a dict since the `ChatModel`is not the returned dataclass.
            return [
                ChatTitleIdResponse.model_validate(
                    {
                        "id": chat[0],
                        "title": chat[1],
                        "updated_at": chat[2],
                        "created_at": chat[3],
                    }
                )
                for chat in all_chats
            ]

    def get_chat_list_by_chat_ids(
        self, chat_ids: list[str], skip: int = 0, limit: int = 50
    ) -> list[ChatModel]:
        with get_db() as db:
            all_chats = (
                db.query(Chat)
                .filter(Chat.id.in_(chat_ids))
                .filter_by(archived=False)
                .order_by(Chat.updated_at.desc())
                .all()
            )
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def get_chat_by_id(self, id: str) -> Optional[ChatModel]:
        try:
            with get_db() as db:
                chat = db.get(Chat, id)
                if chat is None:
                    return None
                _hydrate_chat_messages(db, chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def get_chat_by_share_id(self, id: str) -> Optional[ChatModel]:
        try:
            with get_db() as db:
                # it is possible that the shared link was deleted. hence,
                # we check if the chat is still shared by checking if a chat with the share_id exists
                chat = db.query(Chat).filter_by(share_id=id).first()

                if chat:
                    return self.get_chat_by_id(chat.id)
                else:
                    return None
        except Exception:
            return None

    def resolve_shared_chat(
        self, share_id: str, admin_access: bool = False
    ) -> Optional[ChatModel]:
        """Unified clone-vs-original resolution for ALL shared-chat viewers.

        Reads the per-share mode from the ORIGINAL chat's
        ``meta['share']['mode']`` (default ``'snapshot'`` when absent):
          - ``'live'``     -> the LIVE original (hydrated).
          - ``'snapshot'`` -> the FROZEN clone (graceful fallback to the
                               original if the clone was orphaned).

        ``admin_access`` preserves the admin raw-chat-id lookup when the
        supplied id is not actually a share_id (mirrors the old admin branch).
        """
        try:
            with get_db() as db:
                original = db.query(Chat).filter_by(share_id=share_id).first()
                if original is None:
                    original_id = None
                    mode = None
                else:
                    original_id = original.id
                    mode = ((original.meta or {}).get("share") or {}).get(
                        "mode"
                    ) or "snapshot"

            if original_id is None:
                return self.get_chat_by_id(share_id) if admin_access else None

            if mode == "live":
                return self.get_chat_by_id(original_id)

            # snapshot: serve ONLY the frozen clone. If the clone is missing
            # (orphaned share_id), return None so the link reads as not-found —
            # NEVER fall back to the LIVE original, which would silently expose
            # un-frozen messages the owner meant to keep private.
            return self.get_chat_by_id(share_id)
        except Exception:
            return None

    def get_share_owner_id(self, share_id: str) -> Optional[str]:
        """The REAL user_id of the chat behind a share_id.

        Used to authorize share-scoped file access: a file surfaced through a
        share must actually belong to the share's owner, otherwise a user could
        inject an arbitrary (victim-owned) file id into their own shared chat's
        message JSON and read it. Returns None when share_id is not a real share.
        """
        try:
            with get_db() as db:
                original = db.query(Chat).filter_by(share_id=share_id).first()
                return original.user_id if original else None
        except Exception:
            return None

    @staticmethod
    def get_referenced_file_ids(chat: Optional[ChatModel]) -> set:
        """Collect every file id referenced anywhere in a chat's messages.

        Defensive against non-dict entries and both message-storage shapes
        (``history.messages`` dict + legacy ``messages`` list). Used to
        authorize share-scoped file access by membership.
        """
        ids: set = set()

        def _add(value):
            if isinstance(value, str) and value:
                ids.add(value)

        def _collect_from_files(files):
            if not isinstance(files, list):
                return
            for entry in files:
                if not isinstance(entry, dict):
                    continue
                _add(entry.get("id"))
                inner = entry.get("file")
                if isinstance(inner, dict):
                    _add(inner.get("id"))
                # Generated office docs (docx/xlsx/pptx) render their preview from
                # a SEPARATE converted-PDF File whose id lives in the workspace/data
                # metadata, not in files[].id. Authorize those too, or the shared
                # preview iframe 404s.
                for container in (
                    entry.get("container_workspace"),
                    inner.get("data") if isinstance(inner, dict) else None,
                    entry.get("data"),
                ):
                    if not isinstance(container, dict):
                        continue
                    _add(container.get("preview_file_id"))
                    _add(container.get("pdf_file_id"))
                    nested_cw = container.get("container_workspace")
                    if isinstance(nested_cw, dict):
                        _add(nested_cw.get("preview_file_id"))
                        _add(nested_cw.get("pdf_file_id"))

        try:
            chat_data = getattr(chat, "chat", None) if chat is not None else None
            if not isinstance(chat_data, dict):
                return ids

            messages = []
            history = chat_data.get("history")
            if isinstance(history, dict):
                hist_msgs = history.get("messages")
                if isinstance(hist_msgs, dict):
                    messages.extend(hist_msgs.values())
            legacy = chat_data.get("messages")
            if isinstance(legacy, list):
                messages.extend(legacy)

            for message in messages:
                if not isinstance(message, dict):
                    continue
                _collect_from_files(message.get("files"))

                for block in message.get("content_blocks") or []:
                    if not isinstance(block, dict):
                        continue
                    _collect_from_files(block.get("files"))
                    if block.get("type") == "tool_calls":
                        for result in block.get("results") or []:
                            if not isinstance(result, dict):
                                continue
                            _collect_from_files(result.get("files"))
        except Exception:
            pass

        return ids

    def update_chat_share_mode(self, chat_id: str, mode: str) -> Optional[ChatModel]:
        """Persist the per-share mode on the ORIGINAL chat's meta."""
        try:
            with get_db() as db:
                chat = db.get(Chat, chat_id)
                if chat is None:
                    return None
                meta = {**(chat.meta or {})}
                share = {**(meta.get("share") or {})}
                share["mode"] = mode
                meta["share"] = share
                chat.meta = meta
                db.commit()
                db.refresh(chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def get_chat_by_id_and_user_id(self, id: str, user_id: str) -> Optional[ChatModel]:
        try:
            with get_db() as db:
                chat = db.query(Chat).filter_by(id=id, user_id=user_id).first()
                if chat is None:
                    return None
                _hydrate_chat_messages(db, chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def user_owns_chat(self, id: str, user_id: str) -> bool:
        """Ownership check that does NOT load the chat blob or hydrate messages.

        Use this for endpoints that only need to authorize access — e.g. the
        paginated ``/chats/{id}/messages`` route, where hydrating a 10k-message
        chat just to authorize a 100-message page would defeat the point.
        """
        with get_db() as db:
            row = db.execute(
                text("SELECT 1 FROM chat WHERE id = :id AND user_id = :uid LIMIT 1"),
                {"id": id, "uid": user_id},
            ).fetchone()
            return row is not None

    def get_chat_open_validator(self, id: str, user_id: str) -> Optional[dict]:
        """Light validator for the conditional (If-None-Match) chat open.

        One indexed PK lookup returning 4 scalars — never hydrates the chat
        blob or touches chat_message. ``xmin`` is the Postgres row-version
        system column: it changes on EVERY UPDATE of the chat row, which is
        what makes the ETag catch the mutators that deliberately don't bump
        ``updated_at`` (tags, queue/steer/drain, question states, share,
        bulk archive) as well as same-second double-edits.

        Returns None when the chat isn't readable by this user OR on any
        error — the caller then falls through to the authoritative full path
        (which 401s if needed). This method is purely an optimization and is
        NEVER the not-found authority.
        """
        try:
            with get_db() as db:
                row = db.execute(
                    text(
                        "SELECT xmin::text AS v, updated_at, "
                        "chat->'history'->>'currentId' AS current_id, "
                        "(jsonb_typeof(chat->'draining') = 'object') AS draining "
                        "FROM chat WHERE id = :id AND user_id = :uid LIMIT 1"
                    ),
                    {"id": id, "uid": user_id},
                ).first()
        except Exception:
            log.exception("get_chat_open_validator failed for %s", id)
            return None
        if row is None:
            return None
        return {
            "xmin": row[0],
            "updated_at": row[1],
            "current_id": row[2],
            "draining": bool(row[3]),
        }

    def touch_updated_at(self, id: str) -> None:
        """Bump ONLY updated_at (and thereby xmin) on the chat row.

        Used at subagent-rerun completion: the rerun result is written to the
        parent message via a chat_message-only jsonb_set (no chat-row write),
        so without this bump the conditional-open ETag would keep serving 304s
        with the pre-rerun subagent output, and other devices' sidebars would
        never learn the chat changed.
        """
        try:
            with get_db() as db:
                db.execute(
                    text("UPDATE chat SET updated_at = :ts WHERE id = :id"),
                    {"ts": int(time.time()), "id": id},
                )
                db.commit()
        except Exception:
            log.exception("touch_updated_at failed for %s", id)

    def set_history_current_id_atomic(
        self, id: str, message_id: Optional[str]
    ) -> Optional[int]:
        """Move only ``history.currentId`` under the live chat-row lock.

        Branch navigation used to hydrate the complete chat body, edit one
        pointer in that stale snapshot, and feed the blob to
        ``update_chat_by_id``. Even after migrated messages were peeled away,
        that whole-column write could roll back an unrelated concurrent body
        change (models/files/params/tags). This operation updates exactly the
        pointer key, verifies a non-null target exists, and rotates the chat
        validator without rewriting any message/body state.
        """
        message_id = str(message_id) if message_id else None
        try:
            with get_db() as db:
                chat_row = (
                    db.query(Chat).filter(Chat.id == id).with_for_update().first()
                )
                if chat_row is None:
                    return None

                migrated = bool(
                    _chat_message_table_supported(db)
                    and getattr(chat_row, "messages_migrated", 0)
                )
                if message_id:
                    if migrated:
                        target = db.execute(
                            text(
                                "SELECT 1 FROM chat_message "
                                "WHERE chat_id = :cid AND message_id = :mid "
                                "FOR UPDATE"
                            ),
                            {"cid": id, "mid": message_id},
                        ).first()
                        if target is None:
                            return None
                    else:
                        chat_data = (
                            chat_row.chat if isinstance(chat_row.chat, dict) else {}
                        )
                        messages = (chat_data.get("history") or {}).get(
                            "messages"
                        ) or {}
                        if not isinstance(messages, dict) or message_id not in messages:
                            return None

                updated_at = int(time.time())
                # Replace the history object with itself plus currentId. Unlike
                # jsonb_set(..., '{history,currentId}', ..., true), this also
                # works for a malformed legacy blob whose intermediate
                # ``history`` object is absent.
                db.execute(
                    text(
                        "UPDATE chat SET chat = jsonb_set("
                        "  COALESCE(chat, '{}'::jsonb), "
                        "  '{history}', "
                        "  COALESCE(chat->'history', '{}'::jsonb) "
                        "    || jsonb_build_object('currentId', CAST(:mid AS text)), "
                        "  true"
                        "), updated_at = :updated_at WHERE id = :id"
                    ),
                    {"mid": message_id, "updated_at": updated_at, "id": id},
                )
                db.commit()
                _invalidate_cached_sibling_stubs(id)
                return updated_at
        except Exception:
            log.exception("set_history_current_id_atomic failed for %s", id)
            return None

    def get_chat_messages_paginated(
        self, chat_id: str, skip: int = 0, limit: int = 100
    ) -> list[dict]:
        """Return a slice of messages for a chat, ordered by ``sequence``.

        Migrated chats: direct LIMIT/OFFSET on chat_message.
        Unmigrated chats: read the JSON blob and slice the dict's ordered
        values, so the API shape is identical regardless of storage path.
        """
        with get_db() as db:
            if _is_chat_migrated(db, chat_id):
                try:
                    rows = db.execute(
                        text(
                            f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                            "FROM chat_message WHERE chat_id = :cid "
                            "ORDER BY sequence LIMIT :lim OFFSET :sk"
                        ),
                        {"cid": chat_id, "lim": int(limit), "sk": int(skip)},
                    ).fetchall()
                except Exception:
                    rows = []
                return [_row_to_message_dict(r) for r in rows]

            # Fallback: not migrated, or table missing. Slice the JSON blob.
            chat_obj = db.get(Chat, chat_id)
            if chat_obj is None:
                return []
            chat_dict = chat_obj.chat if isinstance(chat_obj.chat, dict) else {}
            history = chat_dict.get("history") if isinstance(chat_dict, dict) else None
            msgs = history.get("messages") if isinstance(history, dict) else None
            if isinstance(msgs, dict):
                items = list(msgs.values())
                return items[skip : skip + limit]
            if isinstance(chat_dict.get("messages"), list):
                return chat_dict["messages"][skip : skip + limit]
            return []

    def get_chat_messages_overview(self, chat_id: str) -> list[dict]:
        """Return the complete branch graph with short, body-only previews.

        This is deliberately separate from chat-open metadata: the overview is
        opened rarely, while sibling ids are needed on every chat load.  The
        projection never reads message ``meta`` (tool bodies, reasoning, files,
        subagent transcripts), so even a large agentic chat remains a small and
        safe response.
        """

        def project(
            message_id: str,
            parent_id: Optional[str],
            role: Optional[str],
            content: Any,
            content_is_json: bool,
            model: Optional[str],
            timestamp: Optional[int],
            sequence: int,
        ) -> dict:
            decoded = content
            if content_is_json and isinstance(content, str):
                try:
                    decoded = json.loads(content)
                except Exception:
                    # Migrated overview queries intentionally cap content at
                    # the database boundary. A large structured payload may
                    # therefore be incomplete JSON; show an empty preview
                    # instead of shipping/printing a partial data URL or blob.
                    decoded = ""
            preview = re.sub(r"\s+", " ", _extract_content_text(decoded)).strip()
            if len(preview) > 280:
                preview = f"{preview[:277]}..."
            return {
                "id": message_id,
                "parentId": parent_id,
                "role": role or "",
                "model": model,
                "timestamp": timestamp,
                "sequence": sequence,
                "preview": preview,
            }

        with get_db() as db:
            if _is_chat_migrated(db, chat_id):
                rows = db.execute(
                    text(
                        "SELECT message_id, parent_id, role, LEFT(content, 4096), "
                        "content_is_json, model, timestamp, sequence "
                        "FROM chat_message WHERE chat_id = :cid ORDER BY sequence"
                    ),
                    {"cid": chat_id},
                ).fetchall()
                return [project(*row) for row in rows]

            chat_obj = db.get(Chat, chat_id)
            chat_dict = (
                chat_obj.chat
                if chat_obj is not None and isinstance(chat_obj.chat, dict)
                else {}
            )
            history = chat_dict.get("history") or {}
            messages = history.get("messages") if isinstance(history, dict) else None
            if not isinstance(messages, dict):
                raw_messages = chat_dict.get("messages")
                messages = {
                    str(message.get("id") or index): message
                    for index, message in enumerate(raw_messages or [])
                    if isinstance(message, dict)
                }
            return [
                project(
                    str(message_id),
                    message.get("parentId"),
                    message.get("role"),
                    message.get("content", ""),
                    False,
                    message.get("model"),
                    message.get("timestamp"),
                    sequence,
                )
                for sequence, (message_id, message) in enumerate(messages.items())
                if isinstance(message, dict)
            ]

    def get_chat_meta_by_id_and_user_id(self, id: str, user_id: str) -> Optional[dict]:
        """Return chat metadata + sibling stubs only (no message content).

        Sibling stubs include id, parentId, childrenIds, role, and lightweight
        model/modelIdx identity for every message. They render branch and
        multi-model navigation without shipping message bodies. ``childrenIds``
        is derived from authoritative ``parent_id`` links.
        """
        with get_db() as db:
            chat = db.query(Chat).filter_by(id=id, user_id=user_id).first()
            if chat is None:
                return None

            chat_dict = chat.chat if isinstance(chat.chat, dict) else {}
            history = chat_dict.get("history") or {}
            current_id = history.get("currentId") if isinstance(history, dict) else None

            sibling_stubs: list[dict] = []
            orphan_parent_count = 0
            migrated = bool(
                _chat_message_table_supported(db)
                and getattr(chat, "messages_migrated", 0)
            )

            cached_stubs = _get_cached_sibling_stubs(id, chat.updated_at)

            if cached_stubs is not None:
                sibling_stubs, current_id = cached_stubs
            elif migrated:
                try:
                    rows = db.execute(
                        text(
                            "SELECT message_id, parent_id, role, model, "
                            "CASE WHEN meta->>'modelIdx' ~ '^[0-9]+$' "
                            "THEN CAST(meta->>'modelIdx' AS integer) ELSE NULL END "
                            "FROM chat_message WHERE chat_id = :cid ORDER BY sequence"
                        ),
                        {"cid": id},
                    ).fetchall()
                except Exception:
                    rows = []

                message_ids = [r[0] for r in rows]
                message_id_set = set(message_ids)
                children_index: dict[str, list[str]] = {}
                for mid, pid, _role, _model, _model_idx in rows:
                    if pid is not None:
                        children_index.setdefault(pid, []).append(mid)
                        if pid not in message_id_set:
                            orphan_parent_count += 1

                for mid, pid, role, model, model_idx in rows:
                    stub = {
                        "id": mid,
                        "parentId": pid,
                        "childrenIds": children_index.get(mid, []),
                        "role": role,
                    }
                    if model:
                        stub["model"] = model
                    if model_idx is not None:
                        stub["modelIdx"] = model_idx
                    sibling_stubs.append(stub)

                if message_ids and (
                    current_id is None or current_id not in message_id_set
                ):
                    fallback = None
                    parents_with_children = set(children_index.keys())
                    for mid in message_ids:
                        if mid not in parents_with_children:
                            fallback = mid
                    fallback = fallback or message_ids[-1]
                    if current_id is not None and current_id != fallback:
                        log.warning(
                            "Repaired dangling currentId=%s for chat=%s -> %s (meta)",
                            current_id,
                            id,
                            fallback,
                        )
                    current_id = fallback
            else:
                messages_map = (
                    history.get("messages") if isinstance(history, dict) else None
                )

                if isinstance(messages_map, dict):
                    children_index: dict[str, list[str]] = {}
                    for mid, m in messages_map.items():
                        if not isinstance(m, dict):
                            continue
                        pid = m.get("parentId")
                        if pid is not None:
                            children_index.setdefault(pid, []).append(mid)
                    for mid, m in messages_map.items():
                        if not isinstance(m, dict):
                            continue
                        pid = m.get("parentId")
                        if pid is not None and pid not in messages_map:
                            orphan_parent_count += 1
                        stored_children = m.get("childrenIds")
                        children = (
                            stored_children
                            if isinstance(stored_children, list) and stored_children
                            else children_index.get(mid, [])
                        )
                        stub = {
                            "id": mid,
                            "parentId": pid,
                            "childrenIds": children,
                            "role": m.get("role"),
                        }
                        if m.get("model"):
                            stub["model"] = m.get("model")
                        if m.get("modelIdx") is not None:
                            stub["modelIdx"] = m.get("modelIdx")
                        sibling_stubs.append(stub)

                    if messages_map and (
                        current_id is None or current_id not in messages_map
                    ):
                        fallback = _pick_fallback_leaf(messages_map)
                        if fallback is not None:
                            if current_id is not None and current_id != fallback:
                                log.warning(
                                    "Repaired dangling currentId=%s for chat=%s -> %s (meta)",
                                    current_id,
                                    id,
                                    fallback,
                                )
                            current_id = fallback

            if cached_stubs is None:
                if orphan_parent_count:
                    log.warning(
                        "Chat %s has %d message(s) with parentId pointing to a missing row",
                        id,
                        orphan_parent_count,
                    )
                _set_cached_sibling_stubs(
                    id, chat.updated_at, (sibling_stubs, current_id)
                )

            return {
                "id": chat.id,
                "title": chat.title,
                "updated_at": chat.updated_at,
                "created_at": chat.created_at,
                "params": chat_dict.get("params") or {},
                "models": chat_dict.get("models") or [],
                # Strip bandwidth-heavy extracted doc text from chat-level files
                # (re-hydrated from the Files table on demand), same as the
                # per-message slim path.
                "files": _strip_file_data_content_from_files(
                    chat_dict.get("files") or []
                ),
                "queue": chat_dict.get("queue") or [],
                "history": {
                    "currentId": current_id,
                    "sibling_stubs": sibling_stubs,
                },
            }

    def get_chat_messages_branch(
        self,
        chat_id: str,
        leaf_message_id: str,
        before_message_id: Optional[str] = None,
        limit: int = 7,
    ) -> list[dict]:
        """Return the last ``limit`` ancestors on the branch ending at
        ``leaf_message_id``, oldest-first.

        Walks ``parent_id`` from leaf to root using the in-memory messages
        map (works for both migrated and legacy chats). If
        ``before_message_id`` is given, returns the ``limit`` ancestors
        immediately older than that anchor (exclusive) — used for upward
        scroll pagination.
        """
        with get_db() as db:
            if _is_chat_migrated(db, chat_id):
                max_count = max(1, int(limit)) if limit and limit > 0 else 10000

                cursor = leaf_message_id
                if before_message_id:
                    row = db.execute(
                        text(
                            "SELECT parent_id FROM chat_message "
                            "WHERE chat_id = :cid AND message_id = :mid"
                        ),
                        {"cid": chat_id, "mid": before_message_id},
                    ).fetchone()
                    cursor = row[0] if row is not None else leaf_message_id
                if not cursor:
                    return []

                # One recursive CTE instead of up to 60 sequential single-row
                # SELECTs (each a full driver round-trip through the sync-DB
                # bridge): the whole leaf-to-root walk executes server-side in
                # well under a millisecond. The `seen` path array is the cycle
                # guard — a self-parented row (the get_message_list spin
                # incident) terminates the recursion instead of looping.
                rows = db.execute(
                    text(
                        "WITH RECURSIVE walk AS ("
                        "  SELECT m.message_id, m.parent_id, m.role, m.content,"
                        "         m.content_is_json, m.model, m.timestamp,"
                        "         m.status_history, m.meta, m.xmin::text AS rev,"
                        "         1 AS depth, ARRAY[m.message_id] AS seen"
                        "  FROM chat_message m"
                        "  WHERE m.chat_id = :cid AND m.message_id = :start"
                        "  UNION ALL"
                        "  SELECT m.message_id, m.parent_id, m.role, m.content,"
                        "         m.content_is_json, m.model, m.timestamp,"
                        "         m.status_history, m.meta, m.xmin::text,"
                        "         w.depth + 1, w.seen || m.message_id"
                        "  FROM chat_message m"
                        "  JOIN walk w ON m.chat_id = :cid AND m.message_id = w.parent_id"
                        "  WHERE w.depth < :lim AND NOT (m.message_id = ANY(w.seen))"
                        ") "
                        "SELECT message_id, parent_id, role, content, content_is_json,"
                        "       model, timestamp, status_history, meta, rev "
                        "FROM walk ORDER BY depth DESC"
                    ),
                    {"cid": chat_id, "start": cursor, "lim": max_count},
                ).fetchall()
                return [_row_to_message_dict(r) for r in rows]

        messages_map = self.get_messages_map_by_chat_id(chat_id) or {}
        if not messages_map:
            return []

        chain: list[dict] = []
        seen: set[str] = set()
        cursor = leaf_message_id
        while cursor and cursor in messages_map and cursor not in seen:
            seen.add(cursor)
            msg = messages_map[cursor]
            if isinstance(msg, dict):
                m = dict(msg)
                m.setdefault("id", cursor)
                chain.append(m)
                parent = msg.get("parentId")
            else:
                parent = None
            cursor = parent

        chain.reverse()

        if before_message_id:
            try:
                idx = next(
                    i for i, m in enumerate(chain) if m.get("id") == before_message_id
                )
            except StopIteration:
                idx = len(chain)
            start = max(0, idx - max(1, int(limit)))
            return chain[start:idx]

        if limit and limit > 0:
            return chain[-int(limit) :]
        return chain

    def get_chat_messages_branch_manifest(
        self, chat_id: str, leaf_message_id: str, limit: int = 25
    ) -> Optional[list[dict]]:
        """Lean row-version manifest of the branch window ending at
        ``leaf_message_id``: ``[{id, parentId, role, rev}]`` oldest-first,
        where ``rev`` is the row's ``xmin`` (bumped by Postgres on every row
        update — see ``_CHAT_MESSAGE_SELECT_COLS``). The incremental open
        (Contract 3) diffs it against the client's stored ``_rev`` values and
        re-downloads only changed/missing rows; deletions surface as absence.

        Returns ``None`` for legacy blob-stored chats (no rows to version) —
        the router falls back to embedding the full branch page."""
        with get_db() as db:
            if not _is_chat_migrated(db, chat_id):
                return None
            max_count = max(1, int(limit)) if limit and limit > 0 else 10000
            if not leaf_message_id:
                return []
            # Single recursive CTE (see get_chat_messages_branch for rationale;
            # the `seen` array is the self-parent cycle guard).
            rows = db.execute(
                text(
                    "WITH RECURSIVE walk AS ("
                    "  SELECT m.message_id, m.parent_id, m.role, m.xmin::text AS rev,"
                    "         1 AS depth, ARRAY[m.message_id] AS seen"
                    "  FROM chat_message m"
                    "  WHERE m.chat_id = :cid AND m.message_id = :start"
                    "  UNION ALL"
                    "  SELECT m.message_id, m.parent_id, m.role, m.xmin::text,"
                    "         w.depth + 1, w.seen || m.message_id"
                    "  FROM chat_message m"
                    "  JOIN walk w ON m.chat_id = :cid AND m.message_id = w.parent_id"
                    "  WHERE w.depth < :lim AND NOT (m.message_id = ANY(w.seen))"
                    ") "
                    "SELECT message_id, parent_id, role, rev FROM walk ORDER BY depth DESC"
                ),
                {"cid": chat_id, "start": leaf_message_id, "lim": max_count},
            ).fetchall()
            return [
                {"id": r[0], "parentId": r[1], "role": r[2], "rev": r[3]} for r in rows
            ]

    def get_messages_by_ids(self, chat_id: str, message_ids: list[str]) -> list[dict]:
        """Batch-fetch specific message rows — the incremental open's second
        round-trip, downloading exactly the rows whose ``rev`` didn't match.
        Unknown ids are silently dropped; order is not guaranteed (the client
        reassembles by id against the manifest)."""
        ids = [str(m) for m in (message_ids or []) if m]
        if not ids:
            return []
        with get_db() as db:
            rows = db.execute(
                text(
                    f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                    "FROM chat_message WHERE chat_id = :cid AND message_id IN :mids"
                ).bindparams(bindparam("mids", expanding=True)),
                {"cid": chat_id, "mids": ids},
            ).fetchall()
            return [_row_to_message_dict(r) for r in rows]

    def delete_message_with_relink_atomic(
        self, chat_id: str, message_id: str
    ) -> Optional[dict]:
        """Delete one logical message pair and relink its surviving descendants.

        This is the authoritative implementation of the UI's delete-message
        operation.  The selected row and each of its direct children are
        deleted; their children are re-parented to the selected row's parent.

        The entire graph transition runs under the parent ``chat`` row lock,
        which is the first lock taken by every message writer.  A caller must
        additionally close chat-work admission and wait for active writers
        before entering this method; the database lock then makes the actual
        delete/relink/currentId/search update indivisible across tabs and server
        workers.  In particular, there is no interval in which a generation
        checkpoint can observe only half of the mutation and recreate an
        assistant whose user parent has already been deleted.
        """
        message_id = str(message_id or "")
        if not chat_id or not message_id:
            return None

        with get_db() as db:
            try:
                chat_obj = (
                    db.query(Chat)
                    .filter(Chat.id == chat_id)
                    .with_for_update()
                    .first()
                )
                if chat_obj is None:
                    return None

                migrated = bool(
                    _chat_message_table_supported(db)
                    and getattr(chat_obj, "messages_migrated", 0)
                )
                if migrated:
                    rows = db.execute(
                        text(
                            f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                            "FROM chat_message WHERE chat_id = :cid "
                            "ORDER BY sequence FOR UPDATE"
                        ),
                        {"cid": chat_id},
                    ).fetchall()
                    messages = {row[0]: _row_to_message_dict(row) for row in rows}
                else:
                    chat_data = copy.deepcopy(chat_obj.chat or {})
                    history = dict(chat_data.get("history") or {})
                    messages = copy.deepcopy(history.get("messages") or {})

                target = messages.get(message_id)
                if not isinstance(target, dict):
                    return {
                        "deleted_ids": [],
                        "relinked_ids": [],
                        "current_id": (
                            ((chat_obj.chat or {}).get("history") or {}).get(
                                "currentId"
                            )
                        ),
                        "updated_at": chat_obj.updated_at,
                    }

                # parent_id columns are the durable edge authority.  childrenIds
                # is a denormalized navigation index and may be stale after an
                # interrupted legacy write, so derive the mutation neighborhood
                # from parentId and rewrite only the rows whose index changes.
                children_by_parent: dict[str, list[str]] = {}
                for mid, message in messages.items():
                    if not isinstance(message, dict):
                        continue
                    parent = message.get("parentId")
                    if parent:
                        children_by_parent.setdefault(str(parent), []).append(mid)

                parent_id = target.get("parentId")
                parent_id = str(parent_id) if parent_id else None
                child_ids = list(children_by_parent.get(message_id, []))
                grandchild_ids: list[str] = []
                for child_id in child_ids:
                    grandchild_ids.extend(children_by_parent.get(child_id, []))

                deleted_ids = [message_id, *child_ids]
                deleted_set = set(deleted_ids)
                surviving_messages = {
                    mid: message
                    for mid, message in messages.items()
                    if mid not in deleted_set and isinstance(message, dict)
                }

                if parent_id:
                    parent = surviving_messages.get(parent_id)
                    if not isinstance(parent, dict):
                        raise ChatMessageParentMissingError(message_id, parent_id)
                    parent = dict(parent)
                    new_children = [
                        child_id
                        for child_id in children_by_parent.get(parent_id, [])
                        if child_id not in deleted_set
                    ]
                    for grandchild_id in grandchild_ids:
                        if grandchild_id not in new_children:
                            new_children.append(grandchild_id)
                    parent["childrenIds"] = new_children
                    surviving_messages[parent_id] = parent

                for grandchild_id in grandchild_ids:
                    grandchild = surviving_messages.get(grandchild_id)
                    if not isinstance(grandchild, dict):
                        continue
                    grandchild = dict(grandchild)
                    if parent_id is None:
                        grandchild.pop("parentId", None)
                    else:
                        grandchild["parentId"] = parent_id
                    grandchild["childrenIds"] = list(
                        children_by_parent.get(grandchild_id, [])
                    )
                    surviving_messages[grandchild_id] = grandchild

                # Fail closed if this transition would commit any dangling edge.
                # The graph may have arrived with unrelated corruption; a delete
                # must never hide or compound it.
                for mid, message in surviving_messages.items():
                    surviving_parent = message.get("parentId")
                    if surviving_parent and surviving_parent not in surviving_messages:
                        raise ChatMessageParentMissingError(mid, surviving_parent)

                chat_data = copy.deepcopy(chat_obj.chat or {})
                history = dict(chat_data.get("history") or {})
                current_id = history.get("currentId")
                if current_id in deleted_set:
                    current_id = parent_id
                history["currentId"] = current_id

                if migrated:
                    if deleted_ids:
                        db.execute(
                            text(
                                "DELETE FROM chat_message "
                                "WHERE chat_id = :cid AND message_id IN :mids"
                            ).bindparams(bindparam("mids", expanding=True)),
                            {"cid": chat_id, "mids": deleted_ids},
                        )

                    changed_ids = list(
                        dict.fromkeys(
                            [*([parent_id] if parent_id else []), *grandchild_ids]
                        )
                    )
                    for changed_id in changed_ids:
                        message = surviving_messages.get(changed_id)
                        if not isinstance(message, dict):
                            continue
                        cols = _split_message_for_table(message)
                        db.execute(
                            text(
                                "UPDATE chat_message SET "
                                "parent_id = :pid, meta = CAST(:meta AS jsonb) "
                                "WHERE chat_id = :cid AND message_id = :mid"
                            ),
                            {
                                "cid": chat_id,
                                "mid": changed_id,
                                "pid": cols["parent_id"],
                                "meta": cols["meta"],
                            },
                        )
                    # Migrated rows are authoritative; never put the hydrated
                    # message map back into the compact chat JSON.
                    history.pop("messages", None)
                else:
                    history["messages"] = surviving_messages

                chat_data["history"] = history
                chat_obj.chat = chat_data
                chat_obj.updated_at = int(time.time())
                _upsert_chat_search(db, chat_id, chat_obj.title, chat_data)
                db.commit()
                _invalidate_cached_sibling_stubs(chat_id)
                return {
                    "deleted_ids": deleted_ids,
                    "relinked_ids": grandchild_ids,
                    "current_id": current_id,
                    "updated_at": chat_obj.updated_at,
                }
            except Exception:
                db.rollback()
                raise

    def get_message_siblings(self, chat_id: str, message_id: str) -> list[dict]:
        """Return the messages that share a parent with ``message_id``
        (including ``message_id`` itself), full content.
        """
        messages_map = self.get_messages_map_by_chat_id(chat_id) or {}
        target = messages_map.get(message_id)
        if not isinstance(target, dict):
            return []
        parent_id = target.get("parentId")
        sibs: list[dict] = []
        for mid, m in messages_map.items():
            if not isinstance(m, dict):
                continue
            if m.get("parentId") == parent_id:
                copy_m = dict(m)
                copy_m.setdefault("id", mid)
                sibs.append(copy_m)
        return sibs

    def _project_title_ids(self, rows) -> list[ChatTitleIdResponse]:
        return [
            ChatTitleIdResponse(
                id=r[0],
                title=r[1] or "",
                updated_at=r[2] or 0,
                created_at=r[3] or 0,
            )
            for r in rows
        ]

    def get_chats(self, skip: int = 0, limit: int = 50) -> list[ChatTitleIdResponse]:
        with get_db() as db:
            query = (
                db.query(Chat)
                .with_entities(Chat.id, Chat.title, Chat.updated_at, Chat.created_at)
                .order_by(Chat.updated_at.desc())
                .limit(limit)
                .offset(skip)
            )
            return self._project_title_ids(query.all())

    def get_chats_with_data(self, skip: int = 0, limit: int = 50) -> list[ChatModel]:
        """Heavy variant for admin export — pulls the full chat JSON."""
        with get_db() as db:
            query = (
                db.query(Chat)
                .order_by(Chat.updated_at.desc())
                .limit(limit)
                .offset(skip)
            )
            return [ChatModel.model_validate(c) for c in query.all()]

    def get_chats_by_user_id(
        self, user_id: str, include_subagents: bool = False
    ) -> list[ChatTitleIdResponse]:
        with get_db() as db:
            query = db.query(Chat).filter_by(user_id=user_id)
            query = _apply_subagent_filter(query, db, include_subagents)
            query = query.with_entities(
                Chat.id, Chat.title, Chat.updated_at, Chat.created_at
            ).order_by(Chat.updated_at.desc())
            return self._project_title_ids(query.all())

    def get_chats_with_data_by_user_id(
        self, user_id: str, include_subagents: bool = False
    ) -> list[ChatModel]:
        """Heavy variant for export — pulls the full chat JSON. Avoid in
        sidebar / list views."""
        with get_db() as db:
            query = db.query(Chat).filter_by(user_id=user_id)
            query = _apply_subagent_filter(query, db, include_subagents)
            return [
                ChatModel.model_validate(c)
                for c in query.order_by(Chat.updated_at.desc()).all()
            ]

    def get_pinned_chats_by_user_id(
        self, user_id: str, include_subagents: bool = False
    ) -> list[ChatTitleIdResponse]:
        with get_db() as db:
            query = db.query(Chat).filter_by(
                user_id=user_id, pinned=True, archived=False
            )
            query = _apply_subagent_filter(query, db, include_subagents)
            query = query.with_entities(
                Chat.id, Chat.title, Chat.updated_at, Chat.created_at
            ).order_by(Chat.updated_at.desc())
            return self._project_title_ids(query.all())

    def get_archived_chats_by_user_id(
        self, user_id: str, include_subagents: bool = False
    ) -> list[ChatTitleIdResponse]:
        with get_db() as db:
            query = db.query(Chat).filter_by(user_id=user_id, archived=True)
            query = _apply_subagent_filter(query, db, include_subagents)
            query = query.with_entities(
                Chat.id, Chat.title, Chat.updated_at, Chat.created_at
            ).order_by(Chat.updated_at.desc())
            return self._project_title_ids(query.all())

    def get_archived_chats_with_data_by_user_id(
        self, user_id: str, include_subagents: bool = False
    ) -> list[ChatModel]:
        """Heavy variant for archive export — pulls the full chat JSON."""
        with get_db() as db:
            query = db.query(Chat).filter_by(user_id=user_id, archived=True)
            query = _apply_subagent_filter(query, db, include_subagents)
            return [
                ChatModel.model_validate(c)
                for c in query.order_by(Chat.updated_at.desc()).all()
            ]

    def search_chats(
        self,
        user_id: str,
        search_text: str,
        *,
        folder_ids: Optional[list[str]] = None,
        tag_ids: Optional[list[str]] = None,
        pinned: Optional[bool] = None,
        archived: Optional[bool] = None,
        shared: Optional[bool] = None,
        updated_after: Optional[int] = None,
        updated_before: Optional[int] = None,
        sort: str = "relevance",
        skip: int = 0,
        limit: int = 30,
        include_subagents: bool = False,
        query_vector: Optional[list[float]] = None,
    ) -> "ChatSearchResponse":
        """Postgres search backed by tsvector GIN plus pg_trgm fallback. When a
        ``query_vector`` is supplied, the lexical ranking is RRF-fused with a
        semantic (vchordrq ANN) ranking over message embeddings."""
        raw_text = (search_text or "").replace("\u0000", "").strip().lower()

        # Hidden power-user prefix syntax
        extra_tags, extra_folders, p_pin, p_arc, p_shr, raw_text = _strip_prefix_syntax(
            raw_text, user_id
        )
        tag_ids = list(tag_ids or []) + extra_tags
        folder_ids = list(folder_ids or []) + extra_folders
        if pinned is None:
            pinned = p_pin
        if archived is None:
            archived = p_arc
        if shared is None:
            shared = p_shr

        with get_db() as db:
            if db.bind.dialect.name != "postgresql":
                raise RuntimeError("Chat search requires the Postgres-only runtime")
            return self._search_chats_postgres_fts(
                db,
                user_id=user_id,
                raw_text=raw_text,
                folder_ids=folder_ids,
                tag_ids=tag_ids,
                pinned=pinned,
                archived=archived,
                shared=shared,
                updated_after=updated_after,
                updated_before=updated_before,
                sort=sort,
                skip=skip,
                limit=limit,
                include_subagents=include_subagents,
                query_vector=query_vector,
            )

    def get_chats_by_user_id_and_search_text(
        self,
        user_id: str,
        search_text: str,
        include_archived: bool = False,
        skip: int = 0,
        limit: int = 60,
        include_subagents: bool = False,
    ) -> list[ChatModel]:
        """Legacy shim: adapt ``search_chats`` results to the older list shape
        that a few internal call sites still rely on."""
        archived_filter: Optional[bool] = None if include_archived else False
        resp = self.search_chats(
            user_id,
            search_text,
            archived=archived_filter,
            skip=skip,
            limit=limit,
            include_subagents=include_subagents,
        )
        if not resp.hits:
            return []
        with get_db() as db:
            ids = [h.id for h in resp.hits]
            order = {cid: i for i, cid in enumerate(ids)}
            rows = db.query(Chat).filter(Chat.id.in_(ids)).all()
            rows.sort(key=lambda r: order.get(r.id, 0))
            return [ChatModel.model_validate(r) for r in rows]

    def _build_chat_filter_sql(
        self,
        *,
        user_id: str,
        folder_ids: Optional[list[str]],
        tag_ids: Optional[list[str]],
        pinned: Optional[bool],
        archived: Optional[bool],
        shared: Optional[bool],
        updated_after: Optional[int],
        updated_before: Optional[int],
        include_subagents: bool,
    ) -> tuple[str, dict]:
        clauses = ["c.user_id = :user_id"]
        params: dict = {"user_id": user_id}

        if not include_subagents:
            clauses.append("c.subagent_of IS NULL")
            clauses.append("c.automation_of IS NULL")

        if archived is True:
            clauses.append("c.archived = true")
        elif archived is False:
            clauses.append("c.archived = false")

        if pinned is True:
            clauses.append("c.pinned = true")
        elif pinned is False:
            clauses.append("(c.pinned = false OR c.pinned IS NULL)")

        if shared is True:
            clauses.append("c.share_id IS NOT NULL")
        elif shared is False:
            clauses.append("c.share_id IS NULL")

        if folder_ids:
            phs = ",".join(f":fid_{i}" for i in range(len(folder_ids)))
            clauses.append(f"c.folder_id IN ({phs})")
            for i, fid in enumerate(folder_ids):
                params[f"fid_{i}"] = fid

        if tag_ids:
            for i, tid in enumerate(tag_ids):
                clauses.append(
                    f"EXISTS (SELECT 1 FROM jsonb_array_elements_text("
                    f"COALESCE(c.meta->'tags', '[]'::jsonb)) AS t WHERE t = :tid_{i})"
                )
                params[f"tid_{i}"] = tid

        if updated_after:
            clauses.append("c.updated_at >= :updated_after")
            params["updated_after"] = updated_after
        if updated_before:
            clauses.append("c.updated_at <= :updated_before")
            params["updated_before"] = updated_before

        return (" AND ".join(clauses), params)

    def _postgres_filtered_list(
        self, db, filter_sql: str, filter_params: dict, skip: int, limit: int
    ) -> "ChatSearchResponse":
        # Wrapped in try/except like the FTS path: this runs for the empty-query +
        # filter case (e.g. a SearchModal facet/tag click), and a malformed filter
        # must degrade to an empty result, never 500 the modal.
        try:
            total = (
                db.execute(
                    text(f"SELECT COUNT(*) FROM chat c WHERE {filter_sql}"),
                    filter_params,
                ).scalar()
                or 0
            )
            rows = db.execute(
                text(
                    f"SELECT c.id, c.title, c.updated_at, c.created_at, c.archived, c.pinned, c.folder_id "
                    f"FROM chat c WHERE {filter_sql} "
                    f"ORDER BY c.updated_at DESC LIMIT :limit OFFSET :skip"
                ),
                {**filter_params, "limit": limit, "skip": skip},
            ).fetchall()
        except SQLAlchemyError:
            log.exception("chat filtered-list query failed")
            return ChatSearchResponse(total=0, hits=[], facets=ChatSearchFacets())
        hits = [
            ChatSearchHit(
                id=r[0],
                title=r[1] or "",
                updated_at=r[2] or 0,
                created_at=r[3] or 0,
                archived=bool(r[4]),
                pinned=bool(r[5]),
                folder_id=r[6],
            )
            for r in rows
        ]
        return ChatSearchResponse(
            total=int(total),
            hits=hits,
            facets=self._postgres_facets(db, [h.id for h in hits]),
        )

    def _search_chats_postgres_fts(
        self,
        db,
        *,
        user_id: str,
        raw_text: str,
        folder_ids: Optional[list[str]],
        tag_ids: Optional[list[str]],
        pinned: Optional[bool],
        archived: Optional[bool],
        shared: Optional[bool],
        updated_after: Optional[int],
        updated_before: Optional[int],
        sort: str,
        skip: int,
        limit: int,
        include_subagents: bool,
        query_vector: Optional[list[float]] = None,
    ) -> "ChatSearchResponse":
        # Cap every search query so a pathological/over-broad term can't hold its
        # pooled connection (and a sync threadpool worker) indefinitely. SET LOCAL
        # scopes it to this transaction only — a plain SET would leak onto the
        # pooled asyncpg connection and throttle unrelated queries.
        db.execute(text(f"SET LOCAL statement_timeout = '{_SEARCH_STMT_TIMEOUT_MS}ms'"))

        filter_sql, filter_params = self._build_chat_filter_sql(
            user_id=user_id,
            folder_ids=folder_ids,
            tag_ids=tag_ids,
            pinned=pinned,
            archived=archived,
            shared=shared,
            updated_after=updated_after,
            updated_before=updated_before,
            include_subagents=include_subagents,
        )
        # Empty queries, and single-character ASCII queries (worst FTS/trigram
        # selectivity, never useful), fall back to the plain recency-ordered list.
        # Non-ASCII single chars (CJK/logographic words) still search.
        if not raw_text or (len(raw_text) < _MIN_FTS_QUERY_LEN and raw_text.isascii()):
            return self._postgres_filtered_list(
                db, filter_sql, filter_params, skip, limit
            )

        order_sql = (
            "h.updated_at DESC, h.id"
            if sort == "recent"
            else "h.score DESC, h.updated_at DESC, h.id"
        )
        # :q drives title exact/prefix/substring matching (full phrase preserved); :q_fts drives the
        # body tsquery with stopwords dropped so a blob can't AND-match on scattered function words.
        fts_text = _fts_query_text(raw_text)
        params = {
            **filter_params,
            "q": raw_text,
            "q_fts": fts_text,
            "limit": limit,
            "skip": skip,
        }
        # Drive everything from the per-user ``filtered`` set and reach the search
        # tables by their chat_id PK — this keeps cost proportional to the user's
        # own corpus instead of scanning every user's rows, and lets the planner
        # use the PK / GIN indexes. Ranking comes from the always-fresh
        # chat_message_search rows + live chat.title (never the stale chat_search.body).
        common_ctes = f"""
            WITH q AS (SELECT websearch_to_tsquery('simple', :q_fts) AS query),
            filtered AS (
                SELECT c.id, c.title, c.updated_at, c.created_at, c.archived, c.pinned, c.folder_id
                FROM chat c
                WHERE {filter_sql}
            ),
            msg_hits AS (
                SELECT
                    ms.chat_id AS id,
                    MAX(ts_rank_cd(ms.search_vector, q.query, 32)) AS msg_rank,
                    COUNT(*) AS match_count
                FROM chat_message_search ms
                CROSS JOIN q
                JOIN filtered f ON f.id = ms.chat_id
                WHERE ms.search_vector @@ q.query
                GROUP BY ms.chat_id
            ),
            scored AS (
                SELECT
                    f.id, f.title, f.updated_at, f.created_at, f.archived, f.pinned, f.folder_id,
                    (
                        (
                            COALESCE(m.msg_rank, 0) * {_SEARCH_W_MSG}
                          + ts_rank_cd(to_tsvector('simple', COALESCE(f.title, '')), q.query, 32) * {_SEARCH_W_TITLE_FTS}
                          + CASE
                                WHEN lower(f.title) = lower(:q)                THEN {_SEARCH_TITLE_EXACT}
                                WHEN lower(f.title) LIKE lower(:q) || '%'      THEN {_SEARCH_TITLE_PREFIX}
                                WHEN position(lower(:q) IN lower(f.title)) > 0 THEN {_SEARCH_TITLE_CONTAINS}
                                ELSE 0
                            END
                          + LEAST(ln(1 + COALESCE(m.match_count, 0)) * {_SEARCH_BREADTH_COEFF}, {_SEARCH_BREADTH_CAP})
                        )
                        * (1 + {_SEARCH_RECENCY_AMP} * exp(-(extract(epoch FROM now()) - f.updated_at) / {_SEARCH_RECENCY_TAU}))
                    ) AS score,
                    COALESCE(m.match_count, 0) AS match_count
                FROM filtered f
                CROSS JOIN q
                LEFT JOIN msg_hits m ON m.id = f.id
                WHERE m.id IS NOT NULL
                   OR to_tsvector('simple', COALESCE(f.title, '')) @@ q.query
                   OR position(lower(:q) IN lower(f.title)) > 0
            )"""

        # Single-token queries are lexical-only. The semantic arm adds no measured value to
        # one-word queries (an exact word is already handled lexically) but a misspelled word
        # is the dominant typo signature: it has no lexical match yet sits at a deceptively LOW
        # embedding distance to unrelated content (typos measured TIGHTER than real paraphrases),
        # so fusing it floods the page with confident junk. Reserving semantic for multi-word
        # paraphrases/questions kills the typo flood and the one-word tail-slot leak at once.
        # Hyphens/slashes are normalized to spaces first so a compound like "gpu-passthrough"
        # counts as multi-token and keeps its semantic arm (else the gate + FTS-phrase-strictness
        # would drop it to zero results).
        if (
            query_vector is not None
            and len(raw_text.replace("-", " ").replace("/", " ").split()) < 2
        ):
            query_vector = None

        if query_vector:
            # Hybrid: fuse the lexical ranking with a semantic (vchordrq ANN) ranking.
            # Audit-hardened (2026-06-22):
            #   * the semantic arm is CAPPED to the _SEARCH_SEM_FUSE_LIMIT nearest chats under
            #     _SEARCH_SEM_MAX_DIST, so a topic-dense corpus can't flood the candidate set;
            #   * fusion takes the MAX (GREATEST) of the two RRF terms, NOT their sum — so a chat
            #     can't win by being mediocre on BOTH axes; a dominant single-axis match (e.g. an
            #     exact-title lexical #1) keeps the top score instead of being buried by double-dippers;
            #   * a lexical prior (_SEARCH_SEM_WEIGHT < 1) breaks ties toward keyword matches;
            #   * sort=recent recency-orders ONLY genuine lexical (keyword) matches — never the
            #     semantic pool (recency has no relevance score to demote weak matches, and this
            #     embedder puts contentless fragments at distances too low for any bar to exclude).
            params["qvec"] = _pgvector_literal(query_vector)
            db.execute(text("SET LOCAL vchordrq.prefilter = on"))
            recent_match_where = "WHERE cand.has_lex" if sort == "recent" else ""
            sql = f"""{common_ctes},
            lex AS (
                SELECT s.*, ROW_NUMBER() OVER (ORDER BY s.score DESC, s.updated_at DESC) AS lex_rank
                FROM scored s
            ),
            sem AS (
                SELECT sg.id, ROW_NUMBER() OVER (ORDER BY sg.d) AS sem_rank
                FROM (
                    SELECT tm.chat_id AS id, MIN(tm.dist) AS d
                    FROM (
                        SELECT e.chat_id, (e.embedding <=> (:qvec)::vector) AS dist
                        FROM chat_message_embedding e
                        WHERE e.user_id = :user_id
                          AND e.embedding IS NOT NULL
                          AND e.chat_id IN (SELECT id FROM filtered)
                          AND (e.msg_len IS NULL OR e.msg_len >= {_SEARCH_SEM_MIN_MSG_LEN})
                        ORDER BY e.embedding <=> (:qvec)::vector
                        LIMIT {_SEARCH_SEM_MSG_POOL}
                    ) tm
                    GROUP BY tm.chat_id
                ) sg
                WHERE sg.d < {_SEARCH_SEM_MAX_DIST}
                ORDER BY sg.d
                LIMIT {_SEARCH_SEM_FUSE_LIMIT}
            ),
            cand AS (
                SELECT
                    COALESCE(lex.id, sem.id) AS id,
                    GREATEST(
                        COALESCE(1.0 / ({_SEARCH_RRF_K} + lex.lex_rank), 0),
                        COALESCE({_SEARCH_SEM_WEIGHT} / ({_SEARCH_RRF_K} + sem.sem_rank), 0)
                    ) AS score,
                    COALESCE(lex.match_count, 0) AS match_count,
                    (lex.id IS NOT NULL) AS has_lex
                FROM lex FULL OUTER JOIN sem ON lex.id = sem.id
            ),
            hits AS (
                SELECT
                    f.id, f.title, f.updated_at, f.created_at, f.archived, f.pinned, f.folder_id,
                    cand.score, cand.match_count, COUNT(*) OVER () AS total_n
                FROM cand JOIN filtered f ON f.id = cand.id
                {recent_match_where}
            )
            SELECT h.id, h.title, h.updated_at, h.created_at, h.archived, h.pinned, h.folder_id,
                   h.score, h.match_count, h.total_n
            FROM hits h
            ORDER BY {order_sql}
            LIMIT :limit OFFSET :skip
        """
        else:
            sql = f"""{common_ctes},
            hits AS (
                SELECT s.*, COUNT(*) OVER () AS total_n
                FROM scored s
            )
            SELECT h.id, h.title, h.updated_at, h.created_at, h.archived, h.pinned, h.folder_id,
                   h.score, h.match_count, h.total_n
            FROM hits h
            ORDER BY {order_sql}
            LIMIT :limit OFFSET :skip
        """
        try:
            rows = db.execute(text(sql), params).fetchall()
        except SQLAlchemyError:
            # Timed-out / malformed query aborts the transaction; return an empty
            # result rather than 500-ing the modal, and do NOT fall through to the
            # snippet/facet queries on the now-aborted session.
            log.exception("chat search query failed for user %s", user_id)
            return ChatSearchResponse(total=0, hits=[], facets=ChatSearchFacets())

        # Exact lexical search is the fast, high-precision path. Only pay for a
        # trigram pass when it found nothing (and semantic search did not rescue
        # the query). This catches ordinary misspellings without letting fuzzy
        # noise dilute good results or slow every keystroke.
        if (
            not rows
            and 2 < len(raw_text) <= _SEARCH_FUZZY_MAX_QUERY_LEN
            and '"' not in raw_text
        ):
            return self._search_chats_postgres_fuzzy(
                db,
                raw_text=raw_text,
                filter_sql=filter_sql,
                filter_params=filter_params,
                sort=sort,
                skip=skip,
                limit=limit,
            )

        total = int(rows[0][-1]) if rows else 0
        ids = [r[0] for r in rows]
        try:
            # Use the stopword-stripped query so the highlighted message is one that actually
            # matched the body tsquery (else a stripped-query hit would get no snippet).
            snippets = self._postgres_enrich_snippets(db, ids, fts_text)
        except SQLAlchemyError:
            snippets = {}
        hits = [
            ChatSearchHit(
                id=r[0],
                title=r[1] or "",
                updated_at=r[2] or 0,
                created_at=r[3] or 0,
                archived=bool(r[4]),
                pinned=bool(r[5]),
                folder_id=r[6],
                score=float(r[7] or 0),
                match_count=int(r[8] or 0),
                snippet=snippets.get(r[0], {}).get("snippet"),
                matched_message_id=snippets.get(r[0], {}).get("matched_message_id"),
                matched_role=snippets.get(r[0], {}).get("matched_role"),
            )
            for r in rows
        ]
        return ChatSearchResponse(
            total=total, hits=hits, facets=self._postgres_facets(db, ids)
        )

    def _search_chats_postgres_fuzzy(
        self,
        db,
        *,
        raw_text: str,
        filter_sql: str,
        filter_params: dict,
        sort: str,
        skip: int,
        limit: int,
    ) -> "ChatSearchResponse":
        """Bounded pg_trgm fallback for queries with zero exact results."""
        db.execute(
            text(
                "SET LOCAL pg_trgm.word_similarity_threshold = "
                f"'{_SEARCH_FUZZY_WORD_THRESHOLD}'"
            )
        )
        order_sql = (
            "h.updated_at DESC, h.id"
            if sort == "recent"
            else "h.score DESC, h.updated_at DESC, h.id"
        )
        sql = f"""
            WITH filtered AS (
                SELECT c.id, c.title, c.updated_at, c.created_at, c.archived, c.pinned, c.folder_id
                FROM chat c
                WHERE {filter_sql}
            ),
            title_hits AS (
                SELECT f.id, word_similarity(:q, f.title) AS title_rank
                FROM filtered f
                JOIN chat_search cs ON cs.chat_id = f.id
                WHERE cs.title %> :q
            ),
            message_candidates AS (
                SELECT
                    ms.chat_id AS id,
                    ms.message_id,
                    ms.role,
                    ms.content,
                    word_similarity(:q, ms.content) AS msg_rank,
                    COUNT(*) OVER (PARTITION BY ms.chat_id) AS match_count
                FROM chat_message_search ms
                JOIN filtered f ON f.id = ms.chat_id
                WHERE ms.content %> :q
            ),
            message_hits AS (
                SELECT DISTINCT ON (id)
                    id, message_id, role, content, msg_rank, match_count
                FROM message_candidates
                ORDER BY id, msg_rank DESC, message_id
            ),
            scored AS (
                SELECT
                    f.id, f.title, f.updated_at, f.created_at, f.archived, f.pinned, f.folder_id,
                    GREATEST(
                        COALESCE(t.title_rank * 1.35, 0),
                        COALESCE(m.msg_rank, 0)
                    )
                    * (1 + 0.15 * exp(
                        -(extract(epoch FROM now()) - f.updated_at) / {_SEARCH_RECENCY_TAU}
                    )) AS score,
                    COALESCE(m.match_count, 0) AS match_count,
                    m.message_id,
                    m.role,
                    m.content
                FROM filtered f
                LEFT JOIN title_hits t ON t.id = f.id
                LEFT JOIN message_hits m ON m.id = f.id
                WHERE t.id IS NOT NULL OR m.id IS NOT NULL
            ),
            hits AS (
                SELECT s.*, COUNT(*) OVER () AS total_n
                FROM scored s
            )
            SELECT
                h.id, h.title, h.updated_at, h.created_at, h.archived, h.pinned, h.folder_id,
                h.score, h.match_count, h.message_id, h.role, h.content, h.total_n
            FROM hits h
            ORDER BY {order_sql}
            LIMIT :limit OFFSET :skip
        """
        try:
            rows = db.execute(
                text(sql),
                {
                    **filter_params,
                    "q": raw_text,
                    "limit": limit,
                    "skip": skip,
                },
            ).fetchall()
        except SQLAlchemyError:
            log.exception("chat fuzzy-search query failed")
            return ChatSearchResponse(total=0, hits=[], facets=ChatSearchFacets())

        total = int(rows[0][12]) if rows else 0
        ids = [r[0] for r in rows]
        hits = [
            ChatSearchHit(
                id=r[0],
                title=r[1] or "",
                updated_at=r[2] or 0,
                created_at=r[3] or 0,
                archived=bool(r[4]),
                pinned=bool(r[5]),
                folder_id=r[6],
                score=float(r[7] or 0),
                match_count=int(r[8] or 0),
                matched_message_id=r[9],
                matched_role=r[10],
                snippet=_fuzzy_snippet(r[11], raw_text),
            )
            for r in rows
        ]
        return ChatSearchResponse(
            total=total,
            hits=hits,
            facets=self._postgres_facets(db, ids),
            used_fuzzy=bool(hits),
            did_you_mean=raw_text if hits else None,
        )

    def _postgres_enrich_snippets(
        self, db, chat_ids: list[str], raw_text: str
    ) -> dict[str, dict]:
        if not chat_ids:
            return {}
        rows = db.execute(
            text(
                """
                WITH q AS (SELECT websearch_to_tsquery('simple', :q) AS query), ranked AS (
                    SELECT
                        ms.chat_id,
                        ms.message_id,
                        ms.role,
                        ms.content,
                        ROW_NUMBER() OVER (
                            PARTITION BY ms.chat_id
                            ORDER BY ts_rank_cd(ms.search_vector, q.query, 32) DESC, ms.message_id
                        ) AS rn
                    FROM chat_message_search ms CROSS JOIN q
                    WHERE ms.chat_id = ANY(:ids) AND ms.search_vector @@ q.query
                )
                SELECT
                    ranked.chat_id,
                    ranked.message_id,
                    ranked.role,
                    ts_headline('simple', ranked.content, q.query,
                        'StartSel=<mark>, StopSel=</mark>, MaxWords=18, MinWords=8') AS snippet
                FROM ranked CROSS JOIN q
                WHERE ranked.rn = 1
                """
            ),
            {"q": raw_text, "ids": chat_ids},
        ).fetchall()
        return {
            r[0]: {
                "matched_message_id": r[1],
                "matched_role": r[2],
                "snippet": _sanitize_snippet(r[3]),
            }
            for r in rows
        }

    def _postgres_facets(self, db, chat_ids: list[str]) -> "ChatSearchFacets":
        if not chat_ids:
            return ChatSearchFacets()
        params = {"ids": chat_ids}
        try:
            folder_rows = db.execute(
                text(
                    """
                    SELECT c.folder_id, f.name, COUNT(*)
                    FROM chat c LEFT JOIN folder f ON f.id = c.folder_id
                    WHERE c.id = ANY(:ids) AND c.folder_id IS NOT NULL
                    GROUP BY c.folder_id, f.name
                    ORDER BY COUNT(*) DESC LIMIT 20
                    """
                ),
                params,
            ).fetchall()
            folders = [
                FacetBucket(id=r[0], name=(r[1] or r[0]), count=int(r[2]))
                for r in folder_rows
            ]
        except Exception:
            folders = []
        try:
            tag_rows = db.execute(
                text(
                    """
                    SELECT tag.value, COUNT(*)
                    FROM chat c
                    CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(c.meta->'tags', '[]'::jsonb)) AS tag(value)
                    WHERE c.id = ANY(:ids)
                    GROUP BY tag.value ORDER BY COUNT(*) DESC LIMIT 20
                    """
                ),
                params,
            ).fetchall()
            tags = [
                FacetBucket(id=str(r[0]), name=str(r[0]), count=int(r[1]))
                for r in tag_rows
                if r[0]
            ]
        except Exception:
            tags = []
        try:
            model_rows = db.execute(
                text(
                    """
                    SELECT c.model_id_primary, COUNT(*)
                    FROM chat c
                    WHERE c.id = ANY(:ids) AND c.model_id_primary IS NOT NULL
                    GROUP BY c.model_id_primary ORDER BY COUNT(*) DESC LIMIT 20
                    """
                ),
                params,
            ).fetchall()
            models = [
                FacetBucket(id=str(r[0]), name=str(r[0]), count=int(r[1]))
                for r in model_rows
                if r[0]
            ]
        except Exception:
            models = []
        return ChatSearchFacets(folders=folders, tags=tags, models=models)

    def get_chats_by_folder_id_and_user_id(
        self, folder_id: str, user_id: str, skip: int = 0, limit: int = 60
    ) -> list[ChatModel]:
        with get_db() as db:
            query = db.query(Chat).filter_by(folder_id=folder_id, user_id=user_id)
            query = query.filter(or_(Chat.pinned == False, Chat.pinned == None))
            query = query.filter_by(archived=False)
            # P1_1: hidden subagent chats must never surface in folder views.
            query = _apply_subagent_filter(query, db, False)

            query = query.order_by(Chat.updated_at.desc())

            if skip:
                query = query.offset(skip)
            if limit:
                query = query.limit(limit)

            all_chats = query.all()
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def get_chats_by_folder_ids_and_user_id(
        self, folder_ids: list[str], user_id: str
    ) -> list[ChatModel]:
        with get_db() as db:
            query = db.query(Chat).filter(
                Chat.folder_id.in_(folder_ids), Chat.user_id == user_id
            )
            query = query.filter(or_(Chat.pinned == False, Chat.pinned == None))
            query = query.filter_by(archived=False)
            # P1_1: hidden subagent chats must never surface in folder views.
            query = _apply_subagent_filter(query, db, False)

            query = query.order_by(Chat.updated_at.desc())

            all_chats = query.all()
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def update_chat_folder_id_by_id_and_user_id(
        self, id: str, user_id: str, folder_id: str
    ) -> Optional[ChatModel]:
        try:
            with get_db() as db:
                chat = db.get(Chat, id)
                chat.folder_id = folder_id
                chat.updated_at = int(time.time())
                chat.pinned = False
                db.commit()
                db.refresh(chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def get_chat_tags_by_id_and_user_id(self, id: str, user_id: str) -> list[TagModel]:
        with get_db() as db:
            chat = db.get(Chat, id)
            tags = chat.meta.get("tags", [])
            return _sync_get_tags_by_ids_and_user_id(db, tags, user_id)

    def get_chat_list_by_user_id_and_tag_name(
        self, user_id: str, tag_name: str, skip: int = 0, limit: int = 50
    ) -> list[ChatModel]:
        with get_db() as db:
            query = db.query(Chat).filter_by(user_id=user_id)
            tag_id = tag_name.replace(" ", "_").lower()

            query = query.filter(
                text(
                    "EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(Chat.meta->'tags', '[]'::jsonb)) elem WHERE elem = :tag_id)"
                )
            ).params(tag_id=tag_id)
            # P1_1: hidden subagent chats must never surface in tag views.
            query = _apply_subagent_filter(query, db, False)

            all_chats = query.all()
            log.debug(f"all_chats: {all_chats}")
            return [ChatModel.model_validate(chat) for chat in all_chats]

    def add_chat_tag_by_id_and_user_id_and_tag_name(
        self, id: str, user_id: str, tag_name: str
    ) -> Optional[ChatModel]:
        try:
            with get_db() as db:
                tag = _sync_get_tag_by_name_and_user_id(db, tag_name, user_id)
                if tag is None:
                    tag = _sync_insert_new_tag(db, tag_name, user_id)
                if tag is None:
                    return None
                chat = db.get(Chat, id)

                tag_id = tag.id
                if tag_id not in chat.meta.get("tags", []):
                    chat.meta = {
                        **chat.meta,
                        "tags": list(set(chat.meta.get("tags", []) + [tag_id])),
                    }

                db.commit()
                db.refresh(chat)
                return ChatModel.model_validate(chat)
        except Exception:
            return None

    def count_chats_by_tag_name_and_user_id(self, tag_name: str, user_id: str) -> int:
        with get_db() as db:  # Assuming `get_db()` returns a session object
            query = db.query(Chat).filter_by(user_id=user_id, archived=False)

            # Normalize the tag_name for consistency
            tag_id = tag_name.replace(" ", "_").lower()

            query = query.filter(
                text(
                    "EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(Chat.meta->'tags', '[]'::jsonb)) elem WHERE elem = :tag_id)"
                )
            ).params(tag_id=tag_id)

            # Get the count of matching records
            count = query.count()

            # Debugging output for inspection
            log.info(f"Count of chats for tag '{tag_name}': {count}")

            return count

    def count_chats_by_folder_id_and_user_id(self, folder_id: str, user_id: str) -> int:
        with get_db() as db:
            query = db.query(Chat).filter_by(user_id=user_id)

            query = query.filter_by(folder_id=folder_id)
            count = query.count()

            log.info(f"Count of chats for folder '{folder_id}': {count}")
            return count

    def count_chats_by_user_id(self, user_id: str) -> int:
        with get_db() as db:
            count = db.query(Chat).filter_by(user_id=user_id, archived=False).count()
            return count

    def delete_tag_by_id_and_user_id_and_tag_name(
        self, id: str, user_id: str, tag_name: str
    ) -> bool:
        try:
            with get_db() as db:
                chat = db.get(Chat, id)
                tags = chat.meta.get("tags", [])
                tag_id = tag_name.replace(" ", "_").lower()

                tags = [tag for tag in tags if tag != tag_id]
                chat.meta = {
                    **chat.meta,
                    "tags": list(set(tags)),
                }
                db.commit()
                return True
        except Exception:
            return False

    def delete_all_tags_by_id_and_user_id(self, id: str, user_id: str) -> bool:
        try:
            with get_db() as db:
                chat = db.get(Chat, id)
                chat.meta = {
                    **chat.meta,
                    "tags": [],
                }
                db.commit()

                return True
        except Exception:
            return False

    def _collect_subagent_descendant_ids(
        self, db, parent_id: str, owner_user_id=None
    ) -> list[str]:
        """All transitive subagent child-chat ids (subagent_of chain) under
        ``parent_id``. Subagents are separate Chat rows with their own message /
        search / embedding rows keyed on their OWN id, so the parent's FK cascade
        never reaches them — without an explicit delete they leak forever (hidden
        from the sidebar by the ``subagent_of IS NULL`` filter). Recursive so a
        subagent that itself launched subagents is also collected.

        When ``owner_user_id`` is given, descendants are restricted to that owner.
        ``subagent_of`` is a client-settable column (a verified user can stamp it on
        their own chat), so a malicious user could point their chat's subagent_of at
        ANOTHER user's chat; scoping the cascade to the parent's owner means such a
        cross-user pointer can never drag the victim's chat into this delete. A
        genuine subagent chat always shares its parent's owner, so this never drops
        a legitimate descendant."""
        uid_root = " AND user_id = :uid" if owner_user_id else ""
        uid_join = " AND c.user_id = :uid" if owner_user_id else ""
        params = {"pid": parent_id}
        if owner_user_id:
            params["uid"] = owner_user_id
        try:
            rows = db.execute(
                text(
                    "WITH RECURSIVE descendants AS ("
                    "  SELECT id FROM chat WHERE subagent_of = :pid" + uid_root + " "
                    "  UNION "
                    "  SELECT c.id FROM chat c "
                    "  JOIN descendants d ON c.subagent_of = d.id"
                    + uid_join
                    + ") SELECT id FROM descendants"
                ),
                params,
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            # Non-Postgres backends / missing recursive support: fall back to a
            # single non-recursive level so the common case still cleans up.
            try:
                rows = db.execute(
                    text("SELECT id FROM chat WHERE subagent_of = :pid" + uid_root),
                    params,
                ).fetchall()
                return [r[0] for r in rows]
            except Exception:
                return []

    def delete_chat_by_id(self, id: str) -> bool:
        try:
            with get_db() as db:
                # Scope the subagent cascade to the parent's OWNER so a client-stamped
                # cross-user subagent_of can't make this delete reach another user's
                # chat (P2_1). A real subagent chat shares the parent's owner.
                _owner_row = db.query(Chat.user_id).filter_by(id=id).first()
                _owner = _owner_row[0] if _owner_row else None
                sub_ids = (
                    self._collect_subagent_descendant_ids(db, id, _owner)
                    if _owner
                    else []
                )
                if sub_ids:
                    db.query(Chat).filter(Chat.id.in_(sub_ids)).delete(
                        synchronize_session=False
                    )
                db.query(Chat).filter_by(id=id).delete()
                db.commit()

                return True and self.delete_shared_chat_by_chat_id(id)
        except Exception:
            return False

    def delete_chat_by_id_and_user_id(self, id: str, user_id: str) -> bool:
        try:
            with get_db() as db:
                # Only cascade subagent children when the caller actually owns the
                # parent — mirror the ownership scoping of the parent delete below.
                owns = (
                    db.query(Chat.id).filter_by(id=id, user_id=user_id).first()
                    is not None
                )
                if owns:
                    # Descendants restricted to THIS user (P2_1): a client-stamped
                    # subagent_of pointing at another user's chat can never be dragged
                    # into the cascade.
                    sub_ids = self._collect_subagent_descendant_ids(db, id, user_id)
                    if sub_ids:
                        db.query(Chat).filter(
                            Chat.id.in_(sub_ids), Chat.user_id == user_id
                        ).delete(synchronize_session=False)
                db.query(Chat).filter_by(id=id, user_id=user_id).delete()
                db.commit()

                return True and self.delete_shared_chat_by_chat_id(id)
        except Exception:
            return False

    def delete_chats_by_user_id(self, user_id: str) -> bool:
        try:
            with get_db() as db:
                self.delete_shared_chats_by_user_id(user_id)

                db.query(Chat).filter_by(user_id=user_id).delete()
                db.commit()

                return True
        except Exception:
            return False

    def delete_chats_by_user_id_and_folder_id(
        self, user_id: str, folder_id: str
    ) -> bool:
        try:
            with get_db() as db:
                db.query(Chat).filter_by(user_id=user_id, folder_id=folder_id).delete()
                db.commit()

                return True
        except Exception:
            return False

    def delete_shared_chats_by_user_id(self, user_id: str) -> bool:
        try:
            with get_db() as db:
                chats_by_user = db.query(Chat).filter_by(user_id=user_id).all()
                shared_chat_ids = [f"shared-{chat.id}" for chat in chats_by_user]

                db.query(Chat).filter(Chat.user_id.in_(shared_chat_ids)).delete()
                db.commit()

                return True
        except Exception:
            return False

    # ── Chat-message embeddings (semantic search) admin ─────────────────────
    def count_message_embeddings(self) -> dict:
        """Counts for the admin embeddings dashboard:
        - ``embedded``: messages with a valid stored vector.
        - ``failed``:   permanent-failure marker rows (embedding IS NULL — a healthy
                        embedder couldn't embed them; not retried unless content changes).
        - ``pending``:  eligible messages still missing a fresh vector (never embedded
                        OR content edited since). Mirrors the sweep's OWN selection
                        predicate exactly, so the number matches what it will process.
        Postgres-only (jsonb / regexp). Returns zeros (logged) if the table is absent
        or the scan times out."""
        # Reuse the canonical SQL fragments from the sweep so 'pending' can never drift
        # from what run_sweep actually selects.
        from open_webui.scripts.backfill_chat_embeddings import (
            _NONEMPTY_SQL,
            _SRC_HASH_SQL,
        )

        try:
            with get_db() as db:
                # Cap the scan so a huge chat_message table can't pin a connection.
                db.execute(text("SET LOCAL statement_timeout = '20000ms'"))
                embedded = (
                    db.execute(
                        text(
                            "SELECT COUNT(*) FROM chat_message_embedding WHERE embedding IS NOT NULL"
                        )
                    ).scalar()
                    or 0
                )
                failed = (
                    db.execute(
                        text(
                            "SELECT COUNT(*) FROM chat_message_embedding WHERE embedding IS NULL"
                        )
                    ).scalar()
                    or 0
                )
                pending = (
                    db.execute(
                        text(
                            f"""
                            SELECT COUNT(*)
                            FROM chat_message cm
                            JOIN chat c ON c.id = cm.chat_id
                            LEFT JOIN chat_message_embedding e
                                   ON e.chat_id = cm.chat_id AND e.message_id = cm.message_id
                            WHERE c.subagent_of IS NULL
                              AND c.automation_of IS NULL
                              AND cm.role IN ('user','assistant')
                              AND {_NONEMPTY_SQL}
                              AND (e.message_id IS NULL OR e.content_hash <> {_SRC_HASH_SQL})
                            """
                        )
                    ).scalar()
                    or 0
                )
            return {
                "embedded": int(embedded),
                "failed": int(failed),
                "pending": int(pending),
            }
        except Exception:
            log.exception("count_message_embeddings failed")
            return {"embedded": 0, "failed": 0, "pending": 0}

    def delete_all_message_embeddings(self) -> int:
        """Throw out ALL stored chat-message embeddings (rebuild). Every deleted row
        immediately re-qualifies as pending, so the next healthy sweep re-embeds it —
        used when the embedding model changes. Returns the number of rows deleted."""
        with get_db() as db:
            res = db.execute(text("DELETE FROM chat_message_embedding"))
            db.commit()
            return int(res.rowcount or 0)

    def delete_failed_message_embeddings(self) -> int:
        """Clear ONLY the permanent-failure markers (embedding IS NULL). Those rows are
        otherwise skipped forever (their content_hash still matches), so clearing them
        makes the messages re-qualify as pending and the next sweep retries them — used
        after fixing/upgrading a flaky embedder. Returns the number of markers cleared.
        """
        with get_db() as db:
            res = db.execute(
                text("DELETE FROM chat_message_embedding WHERE embedding IS NULL")
            )
            db.commit()
            return int(res.rowcount or 0)


# Methods whose writes target a single (chat_id, message_id) and therefore must
# be serialized against each other so concurrent subagent fan-out branches don't
# lose updates to the shared parent-message `meta` JSON. The lock lives on the
# event loop (acquired BEFORE the threadpool dispatch) so blocked writers never
# occupy a threadpool worker — an unbounded subagent fan-out can't exhaust the
# pool. args[0] is the chat_id, args[1] the message_id for all of these.
_MESSAGE_SCOPED_WRITE_METHODS = frozenset(
    {
        "upsert_message_to_chat_by_id_and_message_id",
        "update_message_fields_atomic",
        "update_generation_message_if_current",
        "mark_generation_stopped_if_current",
        # Targeted single-run subagent write — runs the same read-modify-write
        # mutator under the per-message lock as update_message_fields_atomic, so
        # it must serialize on the same (chat, message) key to stay lossless.
        "update_message_subagent_run_atomic",
        # Read-modify-writes status_history; serialize with the upserts on the same
        # (chat, message) so concurrent status events / subagent upserts can't lose
        # each other's updates.
        "add_message_status_to_chat_by_id_and_message_id",
        # Per-round tool_result_bodies write-through. The full-upsert fallback path
        # (row-not-yet-inserted, and the legacy chat path) does read-whole-meta →
        # write-whole-meta; an UNserialized concurrent merge would land between that
        # read and write and be clobbered, so this must serialize on the SAME
        # per-(chat, message) lock as the upserts/checkpoints.
        "merge_message_tool_result_bodies",
        # Multi-subagent repair commits a guarded sibling + graph pointer in one
        # DB transaction. Key it by (chat, source_message) so it cannot interleave
        # with this process's source-message writers before its xmin guard runs;
        # DB row locks provide the cross-worker guarantee.
        "append_rewind_branch_atomic",
    }
)


class _MessageWriteLockRegistry:
    """Refcounted per-(chat_id, message_id) ``asyncio.Lock`` registry.

    A lock is created on first use and reaped once no coroutine holds or waits
    on it, so the registry doesn't grow without bound over the process lifetime.
    Single-process only (the deployment runs one uvicorn worker); cross-process
    safety would need a DB advisory lock, but the field-scoped merge already
    keeps each write to a single subagent_runs key.
    """

    def __init__(self):
        self._locks: dict[tuple, list] = {}

    def _acquire_entry(self, key):
        entry = self._locks.get(key)
        if entry is None:
            entry = [asyncio.Lock(), 0]
            self._locks[key] = entry
        entry[1] += 1
        return entry

    def _release_entry(self, key):
        entry = self._locks.get(key)
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] <= 0 and not entry[0].locked():
            self._locks.pop(key, None)

    def lock_for(self, chat_id, message_id):
        registry = self
        key = (chat_id, message_id)

        class _Ctx:
            async def __aenter__(self):
                self._entry = registry._acquire_entry(key)
                try:
                    await self._entry[0].acquire()
                except BaseException:
                    # Cancelled/failed while parked in acquire(): __aexit__ will NOT
                    # run (entry never completed), so release the refcount taken
                    # above or the per-(chat,message) lock entry leaks (never reaped,
                    # unbounded growth under churn + repeated Stops).
                    registry._release_entry(key)
                    raise
                return self

            async def __aexit__(self, *exc):
                self._entry[0].release()
                registry._release_entry(key)
                return False

        return _Ctx()


_message_write_locks = _MessageWriteLockRegistry()


class _AsyncChatTableProxy:
    def __init__(self, impl: ChatTable):
        self._impl = impl

    def __getattr__(self, name):
        attr = getattr(self._impl, name)
        if not callable(attr) or name.startswith("_"):
            return attr

        if name in _MESSAGE_SCOPED_WRITE_METHODS:

            async def _wrapped(*args, **kwargs):
                chat_id = args[0] if len(args) > 0 else kwargs.get("id")
                message_id = args[1] if len(args) > 1 else kwargs.get("message_id")
                # Defensive: if we can't key the lock, fall back to an unlocked
                # call rather than crashing (shouldn't happen for these methods).
                if chat_id is None or message_id is None:
                    return await run_sync_db(lambda: attr(*args, **kwargs))
                async with _message_write_locks.lock_for(chat_id, message_id):
                    return await run_sync_db(lambda: attr(*args, **kwargs))

            return _wrapped

        async def _wrapped(*args, **kwargs):
            return await run_sync_db(lambda: attr(*args, **kwargs))

        return _wrapped


Chats = _AsyncChatTableProxy(ChatTable())
