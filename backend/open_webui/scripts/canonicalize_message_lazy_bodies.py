#!/usr/bin/env python3
"""Canonicalize chat_message rows to the lazy message-body contract.

Rows persisted before the canonical slim form (see utils/lazy_blocks.py) carry
their heavy display content inline — full tool results inside
``meta.content_blocks[].results``, reasoning text inside reasoning blocks, and
both AGAIN inside the flattened ``content`` mirror's ``<details>`` attributes.
On a 60-message agentic chat open that shipped 1.5MB+ of collapsed-by-default
bytes; the read-path projection now strips it per request, but the storage
stays fat and the strip costs CPU on every open.

This script rewrites those rows once, into exactly what a fresh write produces:

- ``meta.content_blocks``: tool results / closed reasoning over the inline
  thresholds become lazy stubs (``result_ref`` / ``content_ref``).
- ``meta.tool_result_bodies``: gains the split bodies, UNION-merged (``||``) —
  never shrinks, matching the accumulate-only invariant.
- ``content``: the flattened ``<details ...>`` HTML mirror is replaced with the
  text-only projection of the (slimmed) blocks — the same form a fresh write
  now persists (see ``text_only_content_from_blocks``). Rows whose ``content``
  is already text-only (no ``<details `` markup) are left byte-identical.

Skipped on purpose:
- subagent inner chats (``chat.subagent_of IS NOT NULL``): their rows are read
  wholesale by the parent transcript card, which renders reasoning straight
  from the block (matches the write-path ``subagent_inner`` exemption);
- ``meta.subagent_runs``: projected lean at read time, stored data untouched;
- unmigrated chats (messages live in the JSON blob; read projection covers).

Idempotent (canonical rows produce no diff) and resumable (keyset pagination,
per-batch commits). Run with --dry-run first for stats.

Usage:
    DATABASE_URL=postgresql+asyncpg://... python -m open_webui.scripts.canonicalize_message_lazy_bodies [--dry-run] [--batch-size 200]
"""

import argparse
import asyncio
import logging
import sys

from sqlalchemy import text

from open_webui.internal.db import get_db
from open_webui.utils import fast_json as json
from open_webui.utils.lazy_blocks import (
    split_reasoning_bodies,
    split_tool_result_bodies,
    text_only_content_from_blocks,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def _dumps_jsonb(value) -> str:
    # Raw NULs are rejected by Postgres jsonb; strip both the escape and any
    # raw control char that sneaked into old rows (same policy as models/chats).
    return json.dumps(value).replace("\\u0000", "")


def _canonicalize_row(content: str, meta: dict):
    """Return (new_content, new_blocks, new_bodies) or (None, None, None) when
    the row is already canonical."""
    blocks = meta.get("content_blocks") if isinstance(meta, dict) else None
    if not isinstance(blocks, list) or not blocks:
        return None, None, None

    slim_blocks, tool_bodies = split_tool_result_bodies(blocks)
    slim_blocks, reasoning_bodies = split_reasoning_bodies(slim_blocks)
    bodies = {**tool_bodies, **reasoning_bodies}

    if isinstance(content, str) and "<details " in content:
        new_content = text_only_content_from_blocks(slim_blocks)
    else:
        new_content = content

    blocks_changed = slim_blocks != blocks
    content_changed = new_content != content
    if not blocks_changed and not content_changed:
        return None, None, None
    return new_content, slim_blocks, bodies


async def run(dry_run: bool, batch_size: int) -> None:
    last_key = ("", "")
    scanned = rewritten = bytes_moved = bytes_dropped = 0

    while True:
        async with get_db() as db:
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT cm.chat_id, cm.message_id, cm.content,
                               cm.meta::text AS meta_text
                        FROM chat_message cm
                        JOIN chat c ON c.id = cm.chat_id
                        WHERE (cm.chat_id, cm.message_id) > (:lc, :lm)
                          AND cm.role = 'assistant'
                          AND c.subagent_of IS NULL
                          AND cm.meta ? 'content_blocks'
                          AND COALESCE(cm.content_is_json, 0) = 0
                          AND (
                            cm.content LIKE '%<details %'
                            OR length(cm.content) > 2048
                            OR length(cm.meta->>'content_blocks') > 2048
                          )
                        ORDER BY cm.chat_id, cm.message_id
                        LIMIT :lim
                        """
                    ),
                    {"lc": last_key[0], "lm": last_key[1], "lim": batch_size},
                )
            ).fetchall()

            if not rows:
                break

            for chat_id, message_id, content, meta_text in rows:
                last_key = (chat_id, message_id)
                scanned += 1
                try:
                    meta = json.loads(meta_text) if meta_text else None
                except Exception:
                    log.warning("unparseable meta on %s/%s", chat_id, message_id)
                    continue
                if not isinstance(meta, dict):
                    continue
                try:
                    new_content, new_blocks, bodies = _canonicalize_row(
                        content or "", meta
                    )
                except Exception:
                    log.exception("skipping %s/%s", chat_id, message_id)
                    continue
                if new_blocks is None and new_content is None:
                    continue

                old_size = len(content or "") + len(
                    json.dumps(meta.get("content_blocks"))
                )
                new_size = len(new_content or "") + len(json.dumps(new_blocks))
                bytes_moved += sum(len(json.dumps(b)) for b in bodies.values())
                bytes_dropped += max(0, old_size - new_size)
                rewritten += 1

                if dry_run:
                    continue

                await db.execute(
                    text(
                        """
                        UPDATE chat_message
                        SET content = :content,
                            meta = jsonb_set(
                                jsonb_set(
                                    meta,
                                    '{content_blocks}',
                                    CAST(:blocks AS jsonb),
                                    true
                                ),
                                '{tool_result_bodies}',
                                COALESCE(meta->'tool_result_bodies', '{}'::jsonb)
                                    || CAST(:bodies AS jsonb),
                                true
                            )
                        WHERE chat_id = :cid AND message_id = :mid
                        """
                    ),
                    {
                        "content": (new_content or "").replace("\x00", ""),
                        "blocks": _dumps_jsonb(new_blocks),
                        "bodies": _dumps_jsonb(bodies),
                        "cid": chat_id,
                        "mid": message_id,
                    },
                )

                # chat_message_search is application-maintained (no DB trigger
                # keeps it in sync with chat_message — verified via pg_trigger),
                # populated at write time from the same `content` this UPDATE
                # just replaced. Refresh it here the same way
                # models/chats.py:_upsert_message_search does, or these rows'
                # search text would stay pinned to the pre-canonicalization
                # HTML mirror until the message is rewritten by the app (which
                # never happens for closed messages).
                await db.execute(
                    text(
                        """
                        INSERT INTO chat_message_search (chat_id, message_id, role, content)
                        VALUES (:cid, :mid, 'assistant', :content)
                        ON CONFLICT (chat_id, message_id) DO UPDATE SET
                            role = EXCLUDED.role,
                            content = EXCLUDED.content
                        """
                    ),
                    {
                        "cid": chat_id,
                        "mid": message_id,
                        "content": (new_content or "")[:65536],
                    },
                )

            if not dry_run:
                await db.commit()

        log.info(
            "progress: scanned=%d rewritten=%d moved_to_bodies=%.1fMB dropped_from_wire=%.1fMB (at %s)",
            scanned,
            rewritten,
            bytes_moved / 1e6,
            bytes_dropped / 1e6,
            last_key[0][:8],
        )

    log.info(
        "%s: scanned=%d rewritten=%d moved_to_bodies=%.1fMB dropped=%.1fMB",
        "DRY RUN" if dry_run else "DONE",
        scanned,
        rewritten,
        bytes_moved / 1e6,
        bytes_dropped / 1e6,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()
    asyncio.run(run(args.dry_run, args.batch_size))


if __name__ == "__main__":
    main()
