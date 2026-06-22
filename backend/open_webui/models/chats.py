import asyncio
import copy
import logging
from open_webui.utils import fast_json as json
import time
import uuid
from typing import Any, Callable, Optional

from open_webui.internal.db import Base, get_db, run_sync_db
from open_webui.models.tags import TagModel, Tag
from open_webui.models.folders import Folder
from open_webui.env import SRC_LOG_LEVELS

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


def _tag_id(name: str) -> str:
    return name.replace(" ", "_").lower()


def _sync_get_tag_by_name_and_user_id(db, name: str, user_id: str) -> Optional[TagModel]:
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


def _sync_get_tags_by_ids_and_user_id(db, ids: list[str], user_id: str) -> list[TagModel]:
    if not ids:
        return []
    rows = db.execute(select(Tag).where(Tag.id.in_(ids), Tag.user_id == user_id)).scalars().all()
    return [TagModel.model_validate(row) for row in rows]


def _sync_search_folder_ids_by_names(user_id: str, names: list[str]) -> list[str]:
    normalized = {name.replace("_", " ").strip().lower() for name in names if name}
    if not normalized:
        return []
    with get_db() as db:
        rows = db.execute(select(Folder).where(Folder.user_id == user_id)).scalars().all()
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

    # 0 = messages still live in `chat.chat.history.messages` (legacy);
    # 1 = messages live in the `chat_message` table and are hydrated on read.
    # Default 0 keeps unmigrated chats on the legacy path.
    messages_migrated = Column(Integer, nullable=False, server_default="0", default=0)

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
                "(pinned = false OR pinned IS NULL) AND subagent_of IS NULL"
            ),
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
# with `_row_to_message_dict`.
_CHAT_MESSAGE_SELECT_COLS = (
    "message_id, parent_id, role, content, content_is_json, "
    "model, timestamp, status_history, meta"
)


def _json_from_db(value):
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
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
        out["messages"] = [strip_tool_result_bodies_from_message(m) for m in out["messages"]]
    return out


def strip_tool_result_bodies_from_chat_model(chat: ChatModel) -> ChatModel:
    if not chat:
        return chat
    data = chat.model_dump()
    data["chat"] = strip_tool_result_bodies_from_chat_dict(data.get("chat") or {})
    return ChatModel(**data)


def _project_message_slim(
    msg: dict, is_current_leaf: bool = False, is_current_branch: bool = True
) -> dict:
    """Return a copy of ``msg`` with bandwidth-heavy fields stripped.

    - ALWAYS preserves ``originalContent`` — it carries the pre-edit version
      of edited messages and is critical for branch/edit history. Stripping
      it on non-current-leaf messages would lose user edit-history shadows
      on branch switches, so it must never be dropped on the slim path.
    - drops ``reasoning_details_per_round`` for non-leaf messages (structured
      per-round copy used only for next-send context assembly; old branches
      keep the flat ``reasoning_details`` for replay)
    - replaces ``tool_calls[*].results[*].content`` larger than
      ``_SLIM_TOOL_RESULT_THRESHOLD_BYTES`` with a ``{tool_call_id, truncated,
      size}`` stub for non-current-branch turns; frontend hydrates on branch
      switch via the ``/chats/{id}/messages/{message_id}/siblings`` endpoint
      (``get_message_siblings``) which returns full, non-slim content.
    """
    if not isinstance(msg, dict):
        return msg
    out = strip_tool_result_bodies_from_message(msg)
    # NOTE: do NOT pop "originalContent" — see docstring.
    # Large web tool bodies are fetched lazily through the tool-result endpoint;
    # never include them in normal chat-message list payloads.
    if not is_current_leaf:
        out.pop("reasoning_details_per_round", None)

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
                                "tool_call_id": r.get("tool_call_id")
                                or tc.get("id"),
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
        if (
            not msg.get("role")
            and (
                msg.get("model")
                or msg.get("selectedModelId")
                or isinstance(msg.get("content_blocks"), list)
            )
        ):
            msg["role"] = "assistant"

    for idx, (mid, msg) in enumerate(ordered):
        if not isinstance(msg, dict) or msg.get("role") != "assistant" or msg.get("parentId"):
            continue
        msg_ts = msg.get("timestamp") if isinstance(msg.get("timestamp"), int) else None
        parent_id = None
        for _pmid, candidate in reversed(ordered[:idx]):
            if not isinstance(candidate, dict) or candidate.get("role") != "user":
                continue
            cand_ts = candidate.get("timestamp") if isinstance(candidate.get("timestamp"), int) else None
            if msg_ts is None or cand_ts is None or cand_ts <= msg_ts:
                parent_id = candidate.get("id") or _pmid
                break
        if parent_id is None:
            continue
        msg["parentId"] = parent_id
        parent = messages.get(parent_id)
        if isinstance(parent, dict):
            children = parent.get("childrenIds") if isinstance(parent.get("childrenIds"), list) else []
            if mid not in children:
                children.append(mid)
            parent["childrenIds"] = children

    return messages


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
    history = chat_dict.get("history") if isinstance(chat_dict.get("history"), dict) else {}
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
                        current_id, getattr(chat_obj, "id", "?"), fallback,
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
            text("SELECT COALESCE(MAX(sequence), -1) + 1 FROM chat_message WHERE chat_id = :cid"),
            {"cid": chat_id},
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _split_message_for_table(message: dict) -> dict:
    """Slice an incoming message dict into the column shape used by
    chat_message. Returns a kwargs dict ready for INSERT/UPDATE binds."""
    content = message.get("content", "")
    is_json = 0
    if not isinstance(content, str):
        try:
            content = json.dumps(content)
            is_json = 1
        except Exception:
            content = ""
            is_json = 0

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
        json.dumps(status_history) if status_history is not None else None
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
        )
    }
    meta_json = json.dumps(meta) if meta else None

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


def _strip_prefix_syntax(search_text: str, user_id: str) -> tuple[
    list[str], list[str], Optional[bool], Optional[bool], Optional[bool], str
]:
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


def _apply_subagent_filter(query, db, include_subagents: bool):
    """Filter out chats whose ``subagent_of`` is set — those are research
    subagent chats spawned by the parent chat model and are hidden from the
    user's main chat list / search / pinned / archived views by default."""
    if include_subagents:
        return query
    return query.filter(Chat.subagent_of.is_(None))


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
_SEARCH_W_MSG = 2.0            # best matching message (MAX normalized ts_rank_cd)
_SEARCH_TITLE_EXACT = 3.0     # lower(title) == lower(query)
_SEARCH_TITLE_PREFIX = 1.5    # title starts with the query
_SEARCH_TITLE_CONTAINS = 0.8  # query is a substring of the title
# NOTE: title matching deliberately uses exact/prefix/substring only — NOT the
# pg_trgm `title % :q` similarity operator. That operator is unindexable inside
# the per-user candidate set and costs ~100ms/search on a 3500-chat user (it is
# computed per row); it also leaked zero-token matches on multi-word queries.
# Typo tolerance belongs in an explicit on-demand fallback, not the hot path.
_SEARCH_BREADTH_COEFF = 0.1   # * ln(1 + match_count): diminishing breadth reward
_SEARCH_BREADTH_CAP = 0.5     # hard cap so a 300-msg chat can't dwarf a precise hit
_SEARCH_RECENCY_AMP = 0.5     # newest chats get up to +50%, old ones ~+0%
_SEARCH_RECENCY_TAU = 2.6e6   # ~30-day decay; chat.updated_at is EPOCH SECONDS
_SEARCH_RRF_K = 60            # reciprocal-rank-fusion constant (lexical + semantic)
_SEARCH_SEM_MSG_POOL = 1000   # top in-scope message embeddings fetched per semantic ANN query
# Max cosine distance for a message embedding to count as a semantic match. Measured on the
# live corpus (qwen3-vl-embedding-8b): genuine matches land <=0.30 (strong) / <0.40 (topical),
# while queries with NO real match bottom out at a ~0.42 "noise floor" — the nearest thing is
# just short, generic, centre-of-space lint ("what to do", "how can i do that"). Applied AFTER
# the ANN scan (on the per-chat MIN), so a no-match query contributes nothing semantic and
# degrades to lexical instead of surfacing greetings. No effect on the index scan / latency.
_SEARCH_SEM_MAX_DIST = 0.40


def _pgvector_literal(vec: list[float]) -> str:
    """Format a vector as a pgvector text literal (``[f1,f2,...]``) for ``::vector`` casts.
    None / non-finite components are coerced to 0 so a malformed query embedding can't
    500 the search request."""
    import math

    return (
        "["
        + ",".join(
            (repr(float(x)) if (isinstance(x, (int, float)) and math.isfinite(x)) else "0")
            for x in vec
        )
        + "]"
    )
_MIN_FTS_QUERY_LEN = 2        # single-char ASCII queries fall back to the plain list
_SEARCH_STMT_TIMEOUT_MS = 5000  # per-search Postgres statement_timeout (SET LOCAL)


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
                    if msg.get("role") == "assistant" and ("model" not in msg or not msg["model"]):
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
            db.commit()

            if born_migrated and init_messages:
                self._sync_messages_to_table(db, id, init_messages)
                try:
                    db.commit()
                except Exception:
                    pass

            _upsert_chat_search(db, id, title, enriched_chat)
            try:
                db.commit()
            except Exception:
                pass

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
                stored_chat, init_messages = _peel_messages_off_chat_dict(form_data.chat)
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
                    "model_id_primary": models[0] if models else None,
                }
            )

            result = Chat(**chat.model_dump())
            if born_migrated:
                result.messages_migrated = 1
            db.add(result)
            db.commit()

            if born_migrated and init_messages:
                self._sync_messages_to_table(db, id, init_messages)
                try:
                    db.commit()
                except Exception:
                    pass

            _upsert_chat_search(db, id, import_title, form_data.chat)
            try:
                db.commit()
            except Exception:
                pass

            _hydrate_chat_messages(db, result)
            return ChatModel.model_validate(result) if result else None

    def update_chat_by_id(self, id: str, chat: dict) -> Optional[ChatModel]:
        try:
            with get_db() as db:
                chat_item = db.get(Chat, id)

                # Enrich chat data before updating
                enriched_chat = self._enrich_chat_data(chat)

                migrated = bool(
                    _chat_message_table_supported(db)
                    and getattr(chat_item, "messages_migrated", 0)
                )

                # For migrated chats, sync the messages dict to the
                # chat_message table and strip them out of the on-disk JSON
                # so the blob stays small. The hydrate path will re-attach
                # them on read.
                stored_chat = enriched_chat
                if migrated:
                    peeled_chat, peeled_msgs = _peel_messages_off_chat_dict(enriched_chat)
                    if peeled_msgs is not None:
                        self._sync_messages_to_table(db, id, peeled_msgs)
                        stored_chat = peeled_chat

                title = enriched_chat.get("title", "New Chat")
                models = enriched_chat.get("models") or []
                chat_item.chat = stored_chat
                chat_item.title = title
                chat_item.updated_at = int(time.time())
                chat_item.model_id_primary = models[0] if models else None
                # Re-derive subagent_of in case the caller passed an updated meta
                # (subagent.py does this on the metadata-patch path).
                meta = chat.get("meta") if isinstance(chat, dict) else None
                if isinstance(meta, dict):
                    chat_item.subagent_of = meta.get("subagent_of")
                db.commit()

                _upsert_chat_search(db, id, title, enriched_chat)
                try:
                    db.commit()
                except Exception:
                    pass

                _hydrate_chat_messages(db, chat_item)
                return ChatModel.model_validate(chat_item)
        except Exception:
            return None

    def _sync_messages_to_table(
        self, db, chat_id: str, messages: dict
    ) -> None:
        """Replace every chat_message row for ``chat_id`` with the given dict.

        Done as: DELETE then bulk INSERT, ordered by dict iteration order so
        ``sequence`` matches the on-disk layout. Keeping this O(N) is fine
        because ``update_chat_by_id`` is only a hot path for legacy callers;
        the per-message upsert path uses the fast row-level write.
        """
        if not _chat_message_table_supported(db):
            return
        try:
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
        except Exception:
            # If anything goes wrong, let the JSON path retain the messages —
            # we don't disturb messages_migrated, just the table sync.
            pass

    def update_chat_title_by_id(self, id: str, title: str) -> Optional[ChatModel]:
        # Targeted title-only update: doesn't touch the messages table or
        # the on-disk JSON body beyond the title key, so it's O(1) regardless
        # of chat size.
        try:
            with get_db() as db:
                chat_item = db.get(Chat, id)
                if chat_item is None:
                    return None

                db.execute(
                    text(
                        "UPDATE chat SET "
                        "  title = :t, "
                        "  chat = jsonb_set(COALESCE(chat, '{}'::jsonb), '{title}', to_jsonb(CAST(:t AS text)), true), "
                        "  updated_at = :ts "
                        "WHERE id = :id"
                    ),
                    {"t": title, "ts": int(time.time()), "id": id},
                )
                db.commit()
                db.refresh(chat_item)

                # Title changes need a search-row refresh too.
                _upsert_chat_search(
                    db, id, title, chat_item.chat if isinstance(chat_item.chat, dict) else {}
                )
                try:
                    db.commit()
                except Exception:
                    pass

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
    # so they are O(1) regardless of chat size. Callers serialize concurrent
    # access with a per-chat lock (see utils/chat_queue.py); these methods only
    # guarantee a consistent single read-modify-write of the JSON body.

    def _write_queue_fields(
        self, db, id: str, chat_item, queue: list, draining
    ) -> None:
        """Persist queue + draining without re-syncing message rows."""
        db.execute(
            text(
                "UPDATE chat SET "
                "  chat = jsonb_set("
                "    jsonb_set(COALESCE(chat, '{}'::jsonb), '{queue}', CAST(:q AS jsonb), true), "
                "    '{draining}', CAST(:d AS jsonb), true"
                "  ) "
                "WHERE id = :id"
            ),
            {
                "q": json.dumps(queue if isinstance(queue, list) else []),
                "d": json.dumps(draining),
                "id": id,
            },
        )
        db.commit()
        db.refresh(chat_item)

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

    def append_queue_item_by_id(self, id: str, item: dict) -> Optional[dict]:
        """Atomically append one item to the queue (read-modify-write of the
        blob). Avoids the whole-array clobber two tabs would cause with
        set_queue. Returns the new queue state."""
        try:
            with get_db() as db:
                chat_item = db.get(Chat, id)
                if chat_item is None:
                    return None
                blob = chat_item.chat if isinstance(chat_item.chat, dict) else {}
                queue = blob.get("queue")
                queue = list(queue) if isinstance(queue, list) else []
                queue.append(item)
                self._write_queue_fields(
                    db, id, chat_item, queue, blob.get("draining")
                )
                return {"queue": queue, "draining": blob.get("draining")}
        except Exception:
            log.exception("append_queue_item_by_id failed for %s", id)
            return None

    def remove_queue_item_by_id(self, id: str, item_id: str) -> Optional[dict]:
        """Atomically remove a queue item by its id. Returns the new queue
        state."""
        try:
            with get_db() as db:
                chat_item = db.get(Chat, id)
                if chat_item is None:
                    return None
                blob = chat_item.chat if isinstance(chat_item.chat, dict) else {}
                queue = blob.get("queue")
                queue = [
                    q
                    for q in (queue if isinstance(queue, list) else [])
                    if isinstance(q, dict) and q.get("id") != item_id
                ]
                self._write_queue_fields(
                    db, id, chat_item, queue, blob.get("draining")
                )
                return {"queue": queue, "draining": blob.get("draining")}
        except Exception:
            log.exception("remove_queue_item_by_id failed for %s", id)
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
                chat_item = db.get(Chat, id)
                if chat_item is None:
                    return []
                blob = chat_item.chat if isinstance(chat_item.chat, dict) else {}
                queue = blob.get("queue")
                queue = list(queue) if isinstance(queue, list) else []
                steer = [
                    q
                    for q in queue
                    if isinstance(q, dict) and q.get("mode") == "steer"
                ]
                if not steer:
                    return []
                remaining = [
                    q
                    for q in queue
                    if not (isinstance(q, dict) and q.get("mode") == "steer")
                ]
                self._write_queue_fields(
                    db, id, chat_item, remaining, blob.get("draining")
                )
                return steer
        except Exception:
            log.exception("pop_steer_items_by_id failed for %s", id)
            return []

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
                chat_item = db.get(Chat, id)
                if chat_item is None:
                    return 0
                blob = chat_item.chat if isinstance(chat_item.chat, dict) else {}
                queue = blob.get("queue")
                queue = list(queue) if isinstance(queue, list) else []
                converted = 0
                new_queue = []
                for q in queue:
                    if isinstance(q, dict) and q.get("mode") == "steer":
                        q = {**q, "mode": "after_final"}
                        converted += 1
                    new_queue.append(q)
                if converted:
                    self._write_queue_fields(
                        db, id, chat_item, new_queue, blob.get("draining")
                    )
                return converted
        except Exception:
            log.exception(
                "convert_steer_items_to_after_final_by_id failed for %s", id
            )
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
                chat_item = db.get(Chat, id)
                if chat_item is None:
                    return None
                blob = chat_item.chat if isinstance(chat_item.chat, dict) else {}
                draining = blob.get("draining")
                queue = blob.get("queue")
                queue = list(queue) if isinstance(queue, list) else []

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
                        self._write_queue_fields(db, id, chat_item, queue, None)
                    return {"item": None, "queue": [], "draining": None}

                item = queue.pop(0)
                marker = draining_marker_builder(item)
                self._write_queue_fields(db, id, chat_item, queue, marker)
                return {"item": item, "queue": queue, "draining": marker}
        except Exception:
            log.exception(
                "pop_queue_head_and_mark_draining_by_id failed for %s", id
            )
            return None

    def clear_draining_by_id(
        self, id: str, expected_finished_response_id=None
    ) -> Optional[dict]:
        """Clear the in-flight draining marker. If ``expected_finished_response_id``
        is given, only clear when the marker belongs to that generation (so an
        errored/cancelled turn doesn't wipe a newer turn's marker). Idempotent.
        Returns the new queue state."""
        try:
            with get_db() as db:
                chat_item = db.get(Chat, id)
                if chat_item is None:
                    return None
                blob = chat_item.chat if isinstance(chat_item.chat, dict) else {}
                queue = blob.get("queue")
                queue = list(queue) if isinstance(queue, list) else []
                draining = blob.get("draining")
                if (
                    expected_finished_response_id is not None
                    and isinstance(draining, dict)
                    and draining.get("response_message_id")
                    != expected_finished_response_id
                ):
                    # Marker belongs to a newer generation — leave it.
                    return {"queue": queue, "draining": draining}
                self._write_queue_fields(db, id, chat_item, queue, None)
                return {"queue": queue, "draining": None}
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

    def _write_question_states(self, db, id: str, chat_item, states: dict) -> None:
        """Persist the whole question_states map via a targeted blob write."""
        db.execute(
            text(
                "UPDATE chat SET "
                "  chat = jsonb_set(COALESCE(chat, '{}'::jsonb), '{question_states}', "
                "                   CAST(:s AS jsonb), true) "
                "WHERE id = :id"
            ),
            {
                "s": json.dumps(states if isinstance(states, dict) else {}),
                "id": id,
            },
        )
        db.commit()
        db.refresh(chat_item)

    def set_question_state_by_id(
        self, id: str, tool_call_id: str, patch: dict
    ) -> Optional[dict]:
        """Atomically merge ``patch`` into ``question_states[tool_call_id]``
        (read-modify-write of the blob). Used both for incremental draft saves
        (partial selections, autosaved as the user clicks) and for the terminal
        answer/skip submit. Shallow-merges top-level keys; ``draft`` is replaced
        wholesale by the caller (it already carries the full draft snapshot).
        Returns the new per-question entry, or None on failure."""
        if not tool_call_id or not isinstance(patch, dict):
            return None
        try:
            with get_db() as db:
                chat_item = db.get(Chat, id)
                if chat_item is None:
                    return None
                blob = chat_item.chat if isinstance(chat_item.chat, dict) else {}
                states = blob.get("question_states")
                states = dict(states) if isinstance(states, dict) else {}
                existing = states.get(tool_call_id)
                existing = dict(existing) if isinstance(existing, dict) else {}
                existing.update(patch)
                states[tool_call_id] = existing
                self._write_question_states(db, id, chat_item, states)
                return existing
        except Exception:
            log.exception(
                "set_question_state_by_id failed for %s/%s", id, tool_call_id
            )
            return None

    def get_question_state_by_id(
        self, id: str, tool_call_id: str
    ) -> Optional[dict]:
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

    def get_question_answer_by_id(
        self, id: str, tool_call_id: str
    ) -> Optional[dict]:
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

    def get_messages_map_by_chat_id(self, id: str) -> Optional[dict]:
        chat = self.get_chat_by_id(id)
        if chat is None:
            return None

        return chat.chat.get("history", {}).get("messages", {}) or {}

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

    def upsert_message_to_chat_by_id_and_message_id(
        self, id: str, message_id: str, message: dict, return_model: bool = True
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
        Exactly one caller in the codebase (the non-streaming message-edit
        endpoint in ``routers/chats.py``) uses the return value and keeps the
        default."""
        # Sanitize message content for null characters before upserting
        if isinstance(message.get("content"), str):
            message["content"] = message["content"].replace("\x00", "")

        with get_db() as db:
            chat_obj = db.get(Chat, id)
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
                    merged["id"] = message_id
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

                if message_id in messages_map:
                    messages_map[message_id] = {**messages_map[message_id], **message}
                else:
                    messages_map[message_id] = message

                history["messages"] = messages_map
                history["currentId"] = message_id
                chat_data["history"] = history

                chat_obj.chat = chat_data
                chat_obj.updated_at = int(time.time())
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
            except Exception:
                return None

    def update_message_fields_atomic(
        self, id: str, message_id: str, mutator: Callable[[dict], Optional[dict]]
    ) -> Optional[dict]:
        """Read a message, compute a partial update from it, and merge-write it
        back — as one logical operation.

        ``mutator`` receives the CURRENT persisted message dict (``{}`` if the
        message doesn't exist yet) and returns a partial-update dict of the
        top-level keys to change (or ``None``/empty to skip the write). The
        partial is then merged exactly like ``upsert_message_to_chat_by_id_and_message_id``.

        The whole method runs under the per-message async write lock (applied by
        ``_AsyncChatTableProxy``), so the read the mutator sees and the write it
        produces cannot be interleaved by another fan-out branch's write to the
        same parent message. This is what makes concurrent ``subagent_runs``
        merges (and the parent's ``content_blocks`` checkpoint writes) lossless:
        every writer reads the latest committed state and only replaces the keys
        it actually computed from that fresh read.

        Returns the merged run/patch the mutator produced (its return value),
        or ``None`` when nothing was written.
        """
        existing = self.get_message_by_id_and_message_id(id, message_id) or {}
        try:
            partial = mutator(existing)
        except Exception:
            log.exception("update_message_fields_atomic mutator failed")
            return None
        if not partial:
            return None
        self.upsert_message_to_chat_by_id_and_message_id(
            id, message_id, partial, return_model=False
        )
        return partial

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
                        {"sh": json.dumps(cur_sh), "cid": id, "mid": message_id},
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
                return self.get_chat_by_id_and_user_id(chat.share_id, "shared")
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

    def get_chat_meta_by_id_and_user_id(
        self, id: str, user_id: str
    ) -> Optional[dict]:
        """Return chat metadata + sibling stubs only (no message content).

        Sibling stubs: ``[{id, parentId, childrenIds, role}]`` for every
        message in the chat — IDs only, used to render branch navigation
        without shipping the full message bodies. ``childrenIds`` is derived
        from ``parent_id`` when not stored directly on the message.
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

            if migrated:
                try:
                    rows = db.execute(
                        text(
                            "SELECT message_id, parent_id, role "
                            "FROM chat_message WHERE chat_id = :cid ORDER BY sequence"
                        ),
                        {"cid": id},
                    ).fetchall()
                except Exception:
                    rows = []

                message_ids = [r[0] for r in rows]
                message_id_set = set(message_ids)
                children_index: dict[str, list[str]] = {}
                for mid, pid, _role in rows:
                    if pid is not None:
                        children_index.setdefault(pid, []).append(mid)
                        if pid not in message_id_set:
                            orphan_parent_count += 1

                for mid, pid, role in rows:
                    sibling_stubs.append(
                        {
                            "id": mid,
                            "parentId": pid,
                            "childrenIds": children_index.get(mid, []),
                            "role": role,
                        }
                    )

                if message_ids and (current_id is None or current_id not in message_id_set):
                    fallback = None
                    parents_with_children = set(children_index.keys())
                    for mid in message_ids:
                        if mid not in parents_with_children:
                            fallback = mid
                    fallback = fallback or message_ids[-1]
                    if current_id is not None and current_id != fallback:
                        log.warning(
                            "Repaired dangling currentId=%s for chat=%s -> %s (meta)",
                            current_id, id, fallback,
                        )
                    current_id = fallback
            else:
                messages_map = history.get("messages") if isinstance(history, dict) else None

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
                        sibling_stubs.append(
                            {
                                "id": mid,
                                "parentId": pid,
                                "childrenIds": children,
                                "role": m.get("role"),
                            }
                        )

                    if messages_map and (current_id is None or current_id not in messages_map):
                        fallback = _pick_fallback_leaf(messages_map)
                        if fallback is not None:
                            if current_id is not None and current_id != fallback:
                                log.warning(
                                    "Repaired dangling currentId=%s for chat=%s -> %s (meta)",
                                    current_id, id, fallback,
                                )
                            current_id = fallback

            if orphan_parent_count:
                log.warning(
                    "Chat %s has %d message(s) with parentId pointing to a missing row",
                    id, orphan_parent_count,
                )

            return {
                "id": chat.id,
                "title": chat.title,
                "updated_at": chat.updated_at,
                "created_at": chat.created_at,
                "params": chat_dict.get("params") or {},
                "models": chat_dict.get("models") or [],
                "files": chat_dict.get("files") or [],
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

                def fetch_message(message_id: Optional[str]) -> Optional[dict]:
                    if not message_id:
                        return None
                    row = db.execute(
                        text(
                            f"SELECT {_CHAT_MESSAGE_SELECT_COLS} "
                            "FROM chat_message WHERE chat_id = :cid AND message_id = :mid"
                        ),
                        {"cid": chat_id, "mid": message_id},
                    ).fetchone()
                    return _row_to_message_dict(row) if row is not None else None

                cursor = leaf_message_id
                if before_message_id:
                    before_message = fetch_message(before_message_id)
                    cursor = (
                        before_message.get("parentId")
                        if isinstance(before_message, dict)
                        else leaf_message_id
                    )

                chain: list[dict] = []
                seen: set[str] = set()
                while cursor and cursor not in seen and len(chain) < max_count:
                    seen.add(cursor)
                    msg = fetch_message(cursor)
                    if not isinstance(msg, dict):
                        break
                    chain.append(msg)
                    cursor = msg.get("parentId")

                chain.reverse()
                return chain

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
            return chain[-int(limit):]
        return chain

    def get_message_siblings(
        self, chat_id: str, message_id: str
    ) -> list[dict]:
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
                id=r[0], title=r[1] or "", updated_at=r[2] or 0, created_at=r[3] or 0,
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

    def get_chats_with_data(
        self, skip: int = 0, limit: int = 50
    ) -> list[ChatModel]:
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
                    f"EXISTS (SELECT 1 FROM json_array_elements_text(c.meta->'tags') AS t WHERE t = :tid_{i})"
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
        total = db.execute(
            text(f"SELECT COUNT(*) FROM chat c WHERE {filter_sql}"),
            filter_params,
        ).scalar() or 0
        rows = db.execute(
            text(
                f"SELECT c.id, c.title, c.updated_at, c.created_at, c.archived, c.pinned, c.folder_id "
                f"FROM chat c WHERE {filter_sql} "
                f"ORDER BY c.updated_at DESC LIMIT :limit OFFSET :skip"
            ),
            {**filter_params, "limit": limit, "skip": skip},
        ).fetchall()
        hits = [
            ChatSearchHit(
                id=r[0], title=r[1] or "", updated_at=r[2] or 0, created_at=r[3] or 0,
                archived=bool(r[4]), pinned=bool(r[5]), folder_id=r[6],
            )
            for r in rows
        ]
        return ChatSearchResponse(total=int(total), hits=hits, facets=self._postgres_facets(db, [h.id for h in hits]))

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
            return self._postgres_filtered_list(db, filter_sql, filter_params, skip, limit)

        order_sql = "h.updated_at DESC, h.id" if sort == "recent" else "h.score DESC, h.updated_at DESC, h.id"
        params = {**filter_params, "q": raw_text, "limit": limit, "skip": skip}
        # Drive everything from the per-user ``filtered`` set and reach the search
        # tables by their chat_id PK — this keeps cost proportional to the user's
        # own corpus instead of scanning every user's rows, and lets the planner
        # use the PK / GIN indexes. Ranking comes from the always-fresh
        # chat_message_search rows + live chat.title (never the stale chat_search.body).
        common_ctes = f"""
            WITH q AS (SELECT websearch_to_tsquery('simple', :q) AS query),
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
                LEFT JOIN msg_hits m ON m.id = f.id
                WHERE m.id IS NOT NULL
                   OR position(lower(:q) IN lower(f.title)) > 0
            )"""

        if query_vector:
            # Hybrid: fuse the lexical ranking with a semantic (vchordrq ANN) ranking
            # via reciprocal-rank fusion. The semantic side pulls the user's nearest
            # message embeddings, collapses to best-per-chat, and ranks; a chat
            # surfaces if it matches lexically OR semantically.
            params["qvec"] = _pgvector_literal(query_vector)
            db.execute(text("SET LOCAL vchordrq.prefilter = on"))
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
                        ORDER BY e.embedding <=> (:qvec)::vector
                        LIMIT {_SEARCH_SEM_MSG_POOL}
                    ) tm
                    GROUP BY tm.chat_id
                ) sg
                WHERE sg.d < {_SEARCH_SEM_MAX_DIST}
            ),
            cand AS (
                SELECT
                    COALESCE(lex.id, sem.id) AS id,
                    COALESCE(1.0 / ({_SEARCH_RRF_K} + lex.lex_rank), 0)
                  + COALESCE(1.0 / ({_SEARCH_RRF_K} + sem.sem_rank), 0) AS score,
                    COALESCE(lex.match_count, 0) AS match_count
                FROM lex FULL OUTER JOIN sem ON lex.id = sem.id
            ),
            hits AS (
                SELECT
                    f.id, f.title, f.updated_at, f.created_at, f.archived, f.pinned, f.folder_id,
                    cand.score, cand.match_count, COUNT(*) OVER () AS total_n
                FROM cand JOIN filtered f ON f.id = cand.id
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

        total = int(rows[0][-1]) if rows else 0
        ids = [r[0] for r in rows]
        try:
            snippets = self._postgres_enrich_snippets(db, ids, raw_text)
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
        return ChatSearchResponse(total=total, hits=hits, facets=self._postgres_facets(db, ids))

    def _postgres_enrich_snippets(self, db, chat_ids: list[str], raw_text: str) -> dict[str, dict]:
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
            folders = [FacetBucket(id=r[0], name=(r[1] or r[0]), count=int(r[2])) for r in folder_rows]
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
            tags = [FacetBucket(id=str(r[0]), name=str(r[0]), count=int(r[1])) for r in tag_rows if r[0]]
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
            models = [FacetBucket(id=str(r[0]), name=str(r[0]), count=int(r[1])) for r in model_rows if r[0]]
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
                    "EXISTS (SELECT 1 FROM json_array_elements_text(Chat.meta->'tags') elem WHERE elem = :tag_id)"
                )
            ).params(tag_id=tag_id)

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
                    "EXISTS (SELECT 1 FROM json_array_elements_text(Chat.meta->'tags') elem WHERE elem = :tag_id)"
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

    def delete_chat_by_id(self, id: str) -> bool:
        try:
            with get_db() as db:
                db.query(Chat).filter_by(id=id).delete()
                db.commit()

                return True and self.delete_shared_chat_by_chat_id(id)
        except Exception:
            return False

    def delete_chat_by_id_and_user_id(self, id: str, user_id: str) -> bool:
        try:
            with get_db() as db:
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
                await self._entry[0].acquire()
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
