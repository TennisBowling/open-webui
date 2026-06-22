#!/usr/bin/env python3
"""Fix legacy Gemini C reasoning-token output miscounts on PostgreSQL.

Dry-run by default. Use --apply to update rows and write a rollback JSON file.
Use --rollback <file> to restore touched row values.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import text

from open_webui.internal.db import get_db

BROKEN_MODELS = ("gemini-3.1-pro-preview", "gemini-3-flash-preview")
_TOP = (
    "completion_tokens",
    "completion_tokens_details",
    "prompt_tokens",
    "prompt_tokens_details",
    "total_tokens",
)
_PD = ("cached_tokens",)
_CD = ("reasoning_tokens",)


def _exact_schema(u) -> bool:
    if not isinstance(u, dict) or tuple(sorted(u.keys())) != _TOP:
        return False
    pd = u.get("prompt_tokens_details")
    cd = u.get("completion_tokens_details")
    return (
        isinstance(pd, dict)
        and tuple(sorted(pd.keys())) == _PD
        and isinstance(cd, dict)
        and tuple(sorted(cd.keys())) == _CD
    )


def is_broken(usage: dict) -> int:
    if not _exact_schema(usage):
        return 0
    try:
        r = int(usage["completion_tokens_details"]["reasoning_tokens"] or 0)
        p = int(usage["prompt_tokens"] or 0)
        c = int(usage["completion_tokens"] or 0)
        t = int(usage["total_tokens"] or 0)
    except Exception:
        return 0
    return r if r > 0 and t == p + c + r and t != p + c else 0


def _json(value):
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value) if value else {}
    return value


async def scan(db):
    ev_fixes = []
    conv_delta = defaultdict(int)
    daily_delta = defaultdict(int)
    modu_delta = defaultdict(int)
    modg_delta = defaultdict(int)

    rows = (
        await db.execute(
            text(
                """
                SELECT id, user_id, attributed_chat_id, model_id,
                       completion_tokens, raw_usage, created_at
                FROM token_usage_event
                WHERE model_id = ANY(:models)
                """
            ),
            {"models": list(BROKEN_MODELS)},
        )
    ).mappings().all()

    for row in rows:
        usage = _json(row["raw_usage"])
        delta = is_broken(usage)
        if not delta:
            continue
        old_c = int(row["completion_tokens"] or 0)
        new_usage = dict(usage)
        new_usage["completion_tokens"] = old_c + delta
        ev_fixes.append(
            {
                "id": row["id"],
                "old_completion": old_c,
                "new_completion": old_c + delta,
                "old_raw_usage": usage,
                "new_raw_usage": new_usage,
                "delta": delta,
                "chat_id": row["attributed_chat_id"],
                "user_id": row["user_id"],
                "model_id": row["model_id"],
                "created_at": row["created_at"],
            }
        )
        day = datetime.fromtimestamp(row["created_at"], timezone.utc).strftime("%Y-%m-%d")
        if row["attributed_chat_id"]:
            conv_delta[row["attributed_chat_id"]] += delta
        if row["user_id"]:
            daily_delta[(row["user_id"], day)] += delta
            modu_delta[(row["user_id"], row["model_id"])] += delta
        modg_delta[row["model_id"]] += delta

    last_fixes = []
    for chat_id in conv_delta:
        last = (
            await db.execute(
                text(
                    """
                    SELECT model_id, completion_tokens, raw_usage
                    FROM token_usage_event
                    WHERE attributed_chat_id = :chat_id
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """
                ),
                {"chat_id": chat_id},
            )
        ).mappings().first()
        if not last or last["model_id"] not in BROKEN_MODELS:
            continue
        delta = is_broken(_json(last["raw_usage"]))
        if not delta:
            continue
        current = (
            await db.execute(
                text("SELECT last_output_tokens FROM conversation_token_usage WHERE chat_id = :chat_id"),
                {"chat_id": chat_id},
            )
        ).mappings().first()
        if current and int(current["last_output_tokens"] or 0) == int(last["completion_tokens"] or 0):
            last_fixes.append(
                {
                    "chat_id": chat_id,
                    "old_last_output": int(last["completion_tokens"] or 0),
                    "new_last_output": int(last["completion_tokens"] or 0) + delta,
                }
            )

    msg_fixes = []
    rows = (
        await db.execute(
            text(
                """
                SELECT chat_id, message_id, meta
                FROM chat_message
                WHERE model = ANY(:models) AND meta ? 'usage'
                """
            ),
            {"models": list(BROKEN_MODELS)},
        )
    ).mappings().all()
    for row in rows:
        meta = _json(row["meta"])
        usage = meta.get("usage") or {}
        delta = is_broken(usage)
        if not delta:
            continue
        new_meta = dict(meta)
        new_usage = dict(usage)
        new_usage["completion_tokens"] = int(new_usage.get("completion_tokens") or 0) + delta
        new_meta["usage"] = new_usage
        msg_fixes.append(
            {
                "chat_id": row["chat_id"],
                "message_id": row["message_id"],
                "old_meta": meta,
                "new_meta": new_meta,
                "delta": delta,
            }
        )

    return {
        "events": ev_fixes,
        "messages": msg_fixes,
        "last_fixes": last_fixes,
        "conv_delta": dict(conv_delta),
        "daily_delta": {"|".join(k): v for k, v in daily_delta.items()},
        "model_user_delta": {"|".join(k): v for k, v in modu_delta.items()},
        "model_global_delta": dict(modg_delta),
    }


def summarize(plan: dict) -> str:
    return json.dumps(
        {
            "events": len(plan["events"]),
            "messages": len(plan["messages"]),
            "event_delta": sum(e["delta"] for e in plan["events"]),
            "message_delta": sum(m["delta"] for m in plan["messages"]),
            "conversation_rows": len(plan["conv_delta"]),
            "daily_rows": len(plan["daily_delta"]),
            "model_user_rows": len(plan["model_user_delta"]),
            "model_global_rows": len(plan["model_global_delta"]),
        },
        indent=2,
    )


async def apply_plan(db, plan: dict, rollback_path: str) -> None:
    with open(rollback_path, "w") as f:
        json.dump(plan, f)

    for event in plan["events"]:
        await db.execute(
            text(
                """
                UPDATE token_usage_event
                SET completion_tokens = :completion,
                    raw_usage = CAST(:raw_usage AS jsonb)
                WHERE id = :id
                """
            ),
            {
                "id": event["id"],
                "completion": event["new_completion"],
                "raw_usage": json.dumps(event["new_raw_usage"]),
            },
        )
    for msg in plan["messages"]:
        await db.execute(
            text(
                """
                UPDATE chat_message
                SET meta = CAST(:meta AS jsonb)
                WHERE chat_id = :chat_id AND message_id = :message_id
                """
            ),
            {
                "chat_id": msg["chat_id"],
                "message_id": msg["message_id"],
                "meta": json.dumps(msg["new_meta"]),
            },
        )
    for chat_id, delta in plan["conv_delta"].items():
        await db.execute(
            text(
                """
                UPDATE conversation_token_usage
                SET total_output_tokens = total_output_tokens + :delta
                WHERE chat_id = :chat_id
                """
            ),
            {"chat_id": chat_id, "delta": delta},
        )
    for row in plan["last_fixes"]:
        await db.execute(
            text(
                """
                UPDATE conversation_token_usage
                SET last_output_tokens = :new_value
                WHERE chat_id = :chat_id AND last_output_tokens = :old_value
                """
            ),
            row,
        )
    for key, delta in plan["daily_delta"].items():
        user_id, day = key.split("|", 1)
        await db.execute(
            text(
                """
                UPDATE daily_token_usage
                SET total_output_tokens = total_output_tokens + :delta
                WHERE user_id = :user_id AND date = :day
                """
            ),
            {"user_id": user_id, "day": day, "delta": delta},
        )
    for key, delta in plan["model_user_delta"].items():
        user_id, model_id = key.split("|", 1)
        await db.execute(
            text(
                """
                UPDATE model_token_usage
                SET total_output_tokens = total_output_tokens + :delta
                WHERE user_id = :user_id AND model_id = :model_id
                """
            ),
            {"user_id": user_id, "model_id": model_id, "delta": delta},
        )
    for model_id, delta in plan["model_global_delta"].items():
        await db.execute(
            text(
                """
                UPDATE model_token_usage
                SET total_output_tokens = total_output_tokens + :delta
                WHERE user_id IS NULL AND model_id = :model_id
                """
            ),
            {"model_id": model_id, "delta": delta},
        )
    await db.commit()


async def rollback(db, rollback_path: str) -> None:
    with open(rollback_path) as f:
        plan = json.load(f)
    for event in plan.get("events", []):
        await db.execute(
            text(
                """
                UPDATE token_usage_event
                SET completion_tokens = :completion,
                    raw_usage = CAST(:raw_usage AS jsonb)
                WHERE id = :id
                """
            ),
            {
                "id": event["id"],
                "completion": event["old_completion"],
                "raw_usage": json.dumps(event["old_raw_usage"]),
            },
        )
    for msg in plan.get("messages", []):
        await db.execute(
            text(
                """
                UPDATE chat_message
                SET meta = CAST(:meta AS jsonb)
                WHERE chat_id = :chat_id AND message_id = :message_id
                """
            ),
            {"chat_id": msg["chat_id"], "message_id": msg["message_id"], "meta": json.dumps(msg["old_meta"])},
        )
    for chat_id, delta in plan.get("conv_delta", {}).items():
        await db.execute(
            text("UPDATE conversation_token_usage SET total_output_tokens = total_output_tokens - :delta WHERE chat_id = :chat_id"),
            {"chat_id": chat_id, "delta": delta},
        )
    for row in plan.get("last_fixes", []):
        await db.execute(
            text("UPDATE conversation_token_usage SET last_output_tokens = :old_last_output WHERE chat_id = :chat_id"),
            row,
        )
    for key, delta in plan.get("daily_delta", {}).items():
        user_id, day = key.split("|", 1)
        await db.execute(
            text("UPDATE daily_token_usage SET total_output_tokens = total_output_tokens - :delta WHERE user_id = :user_id AND date = :day"),
            {"user_id": user_id, "day": day, "delta": delta},
        )
    for key, delta in plan.get("model_user_delta", {}).items():
        user_id, model_id = key.split("|", 1)
        await db.execute(
            text("UPDATE model_token_usage SET total_output_tokens = total_output_tokens - :delta WHERE user_id = :user_id AND model_id = :model_id"),
            {"user_id": user_id, "model_id": model_id, "delta": delta},
        )
    for model_id, delta in plan.get("model_global_delta", {}).items():
        await db.execute(
            text("UPDATE model_token_usage SET total_output_tokens = total_output_tokens - :delta WHERE user_id IS NULL AND model_id = :model_id"),
            {"model_id": model_id, "delta": delta},
        )
    await db.commit()


async def amain() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback")
    args = parser.parse_args()

    async with get_db() as db:
        if args.rollback:
            await rollback(db, args.rollback)
            print(f"rolled back from {args.rollback}")
            return
        plan = await scan(db)
        print(summarize(plan))
        if not args.apply:
            await db.rollback()
            print("dry-run only; pass --apply to write changes")
            return
        path = f"fix_gemini_rollback_{int(time.time())}.json"
        await apply_plan(db, plan, path)
        print(f"applied; rollback file: {path}")


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        sys.exit(130)
