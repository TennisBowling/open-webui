"""Purge code interpreter blocks from stored chat messages.

Revision ID: 7b5f4d2a9c31
Revises: f1a2b3c4d5e6
Create Date: 2026-05-31

Converts stored ``content_blocks`` entries of type ``code_interpreter`` into
plain text blocks containing markdown code fences and any saved output. Also
rewrites legacy serialized ``<details type="code_interpreter">`` snippets in
message content so old chats do not require a special renderer.
"""

import html
import json
import re
from typing import Any

import sqlalchemy as sa
from alembic import op, context


revision = "7b5f4d2a9c31"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


_DETAILS_RE = re.compile(
    r'<details\s+type="code_interpreter"(?P<attrs>[^>]*)>(?P<inner>.*?)</details>',
    re.IGNORECASE | re.DOTALL,
)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
_SUMMARY_RE = re.compile(r"<summary>.*?</summary>\s*", re.IGNORECASE | re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)


def _parse_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _decode_output_attr(raw: str) -> Any:
    decoded = html.unescape(raw or "")
    try:
        return json.loads(decoded)
    except Exception:
        return decoded


def _parse_attrs(attrs: str) -> dict[str, str]:
    return {key: value for key, value in _ATTR_RE.findall(attrs or "")}


def _stringify_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _stringify_output(output: Any) -> str:
    if output in (None, ""):
        return ""
    if isinstance(output, dict):
        parts: list[str] = []
        seen = set()
        for key in ("stdout", "stderr", "result", "output", "error"):
            value = output.get(key)
            if value in (None, ""):
                continue
            seen.add(key)
            parts.append(f"{key}:\n{_stringify_value(value).rstrip()}")
        extra = {key: value for key, value in output.items() if key not in seen}
        if extra:
            parts.append(_stringify_value(extra).rstrip())
        return "\n\n".join(part for part in parts if part)
    return _stringify_value(output)


def _block_to_text(block: dict[str, Any]) -> str:
    attrs = block.get("attributes") if isinstance(block.get("attributes"), dict) else {}
    lang = (attrs.get("lang") or "").strip()
    code = _stringify_value(block.get("content") or "").rstrip()
    text = f"```{lang}\n{code}\n```"
    output = _stringify_output(block.get("output")).rstrip()
    if output:
        text = f"{text}\n\n```output\n{output}\n```"
    return text


def _append_text_block(blocks: list[dict[str, Any]], text: str) -> None:
    text = text.strip()
    if not text:
        return
    if blocks and blocks[-1].get("type") == "text":
        existing = (blocks[-1].get("content") or "").rstrip()
        blocks[-1]["content"] = f"{existing}\n\n{text}" if existing else text
        return
    blocks.append({"type": "text", "content": text})


def _normalize_content_blocks(blocks: Any) -> tuple[Any, bool]:
    if not isinstance(blocks, list):
        return blocks, False
    changed = False
    out: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "code_interpreter":
            _append_text_block(out, _block_to_text(block))
            changed = True
        else:
            out.append(block)
    return out, changed


def _legacy_details_to_markdown(content: Any) -> tuple[Any, bool]:
    if not isinstance(content, str) or "code_interpreter" not in content:
        return content, False

    changed = False

    def replace(match: re.Match) -> str:
        nonlocal changed
        changed = True
        attrs = _parse_attrs(match.group("attrs") or "")
        inner = _SUMMARY_RE.sub("", match.group("inner") or "").strip()
        lang = ""
        code = inner
        fence = _CODE_FENCE_RE.search(inner)
        if fence:
            lang = (fence.group(1) or "").strip()
            code = fence.group(2).rstrip()
        output = _decode_output_attr(attrs.get("output", "")) if attrs.get("output") else None
        return _block_to_text(
            {"type": "text", "content": code, "attributes": {"lang": lang}, "output": output}
        )

    return _DETAILS_RE.sub(replace, content), changed


def _normalize_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    changed = False
    blocks, blocks_changed = _normalize_content_blocks(message.get("content_blocks"))
    if blocks_changed:
        message["content_blocks"] = blocks
        changed = True
    content, content_changed = _legacy_details_to_markdown(message.get("content"))
    if content_changed:
        message["content"] = content
        changed = True
    return changed


def _normalize_chat_payload(chat_data: Any) -> bool:
    if not isinstance(chat_data, dict):
        return False
    changed = False
    history = chat_data.get("history")
    messages = history.get("messages") if isinstance(history, dict) else None
    if isinstance(messages, dict):
        for message in messages.values():
            changed = _normalize_message(message) or changed
    if isinstance(chat_data.get("messages"), list):
        for message in chat_data["messages"]:
            changed = _normalize_message(message) or changed
    return changed


def _update_chat_json(bind) -> None:
    rows = bind.execute(sa.text("SELECT id, chat FROM chat")).fetchall()
    for row in rows:
        chat_id = row[0]
        raw_chat = row[1]
        chat_data = _parse_json(raw_chat)
        if not _normalize_chat_payload(chat_data):
            continue
        bind.execute(
            sa.text("UPDATE chat SET chat = :chat WHERE id = :id"),
            {"chat": json.dumps(chat_data, ensure_ascii=False), "id": chat_id},
        )


def _update_chat_message_rows(bind) -> None:
    inspector = sa.inspect(bind)
    if "chat_message" not in inspector.get_table_names():
        return
    rows = bind.execute(
        sa.text("SELECT chat_id, message_id, content, meta FROM chat_message")
    ).fetchall()
    for row in rows:
        chat_id, message_id, content_raw, meta_raw = row
        changed = False
        content, content_changed = _legacy_details_to_markdown(content_raw)
        changed = content_changed or changed

        meta = _parse_json(meta_raw) or {}
        if isinstance(meta, dict):
            blocks, blocks_changed = _normalize_content_blocks(meta.get("content_blocks"))
            if blocks_changed:
                meta["content_blocks"] = blocks
                changed = True
        else:
            meta = meta_raw

        if not changed:
            continue
        bind.execute(
            sa.text(
                "UPDATE chat_message SET content = :content, meta = :meta "
                "WHERE chat_id = :chat_id AND message_id = :message_id"
            ),
            {
                "content": content,
                "meta": json.dumps(meta, ensure_ascii=False) if isinstance(meta, dict) else meta,
                "chat_id": chat_id,
                "message_id": message_id,
            },
        )


def upgrade():
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    _update_chat_json(bind)
    _update_chat_message_rows(bind)


def downgrade():
    pass
