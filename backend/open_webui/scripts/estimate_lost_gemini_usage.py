"""Estimate token usage for the handful of gemini-C requests where the provider
never sent a usage total (genuine provider data loss). DRY RUN by default — pass
--apply to write.

Input estimate: the prompt_tokens of the same-chat real event closest in time to
the undercounted message (captures the real context size incl. system+tools).
Output estimate: tiktoken (o200k_base) of the message's visible content.
Inserted rows are marked source_type='estimated_backfill', raw_usage.estimated=true,
embedded_cost=NULL (priced by the rate card at read time). Aggregates are bumped by
the delta. last_* snapshots are NOT touched.
"""
import asyncio, sys, uuid, json, time
import asyncpg, tiktoken

DSN = "postgresql://tennisbowling:tennispass@192.168.10.2:5432/openllm"
APPLY = "--apply" in sys.argv
ENC = tiktoken.get_encoding("o200k_base")
NONZERO = "(prompt_tokens<>0 OR completion_tokens<>0 OR total_tokens<>0 OR COALESCE(cache_read_tokens,0)<>0)"


def tok(s: str) -> int:
    if not s:
        return 0
    try:
        return len(ENC.encode(s))
    except Exception:
        return max(1, len(s) // 4)


async def output_text(conn, message_id):
    row = await conn.fetchrow(
        "SELECT content, meta FROM chat_message WHERE message_id=$1", message_id
    )
    if not row:
        return ""
    parts = []
    if row["content"]:
        parts.append(row["content"])
    meta = row["meta"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    # include any assistant text inside content_blocks not already in content
    for b in (meta or {}).get("content_blocks", []) or []:
        if isinstance(b, dict) and b.get("type") in ("text", "reasoning"):
            t = b.get("content") or b.get("text") or ""
            if t and t not in (row["content"] or ""):
                parts.append(t)
    return "\n".join(parts)


async def main():
    conn = await asyncpg.connect(DSN)
    # SET A: gemini-C messages with output but ZERO real usage event anywhere.
    rows = await conn.fetch(f"""
        WITH oj AS (
          SELECT message_id, max(attributed_chat_id) chat_id, max(user_id) user_id,
                 max(model_id) model_id, min(created_at) ts
          FROM token_usage_event
          WHERE model_id IN ('gemini-3.1-pro-preview','gemini-3.5-flash') AND message_id IS NOT NULL
          GROUP BY message_id
          HAVING count(*) FILTER (WHERE prompt_tokens>0)=0
             AND count(*) FILTER (WHERE prompt_tokens=0 AND completion_tokens=0 AND total_tokens=0)>0
        )
        SELECT oj.* FROM oj
        JOIN chat_message cm ON cm.message_id=oj.message_id
        WHERE NOT EXISTS (SELECT 1 FROM token_usage_event e WHERE e.message_id=oj.message_id AND {NONZERO})
          AND length(COALESCE(cm.content,'')) > 50
    """)

    plan = []
    for r in rows:
        # input proxy: same-chat real event closest in time
        proxy = await conn.fetchrow(f"""
            SELECT prompt_tokens, cache_read_tokens FROM token_usage_event
            WHERE attributed_chat_id=$1 AND {NONZERO}
            ORDER BY abs(created_at-$2) ASC, created_at DESC LIMIT 1
        """, r["chat_id"], r["ts"])
        in_est = int(proxy["prompt_tokens"]) if proxy else 0
        out_est = tok(await output_text(conn, r["message_id"]))
        plan.append({
            "message_id": r["message_id"], "chat_id": r["chat_id"], "user_id": r["user_id"],
            "model_id": r["model_id"], "ts": int(r["ts"]),
            "input": in_est, "output": out_est, "total": in_est + out_est,
        })

    print(f"=== {len(plan)} undercounted gemini requests (provider sent no usage) ===")
    for p in plan:
        print(f"  msg {p['message_id'][:8]} chat {p['chat_id'][:8]} model {p['model_id']:24s} "
              f"in~{p['input']:>8} out~{p['output']:>6} total~{p['total']:>8}")
    print(f"--- estimated recovery: input={sum(p['input'] for p in plan):,}  "
          f"output={sum(p['output'] for p in plan):,}  total={sum(p['total'] for p in plan):,}")

    if not APPLY:
        print("\nDRY RUN — no writes. Re-run with --apply to insert.")
        await conn.close()
        return

    now = int(time.time())
    async with conn.transaction():
        for p in plan:
            await conn.execute("""
                INSERT INTO token_usage_event
                  (id,user_id,source_chat_id,attributed_chat_id,message_id,model_id,
                   prompt_tokens,completion_tokens,total_tokens,cache_read_tokens,request_count,
                   source_type,raw_usage,embedded_cost,created_at)
                VALUES ($1,$2,$3,$3,$4,$5,$6,$7,$8,0,1,'estimated_backfill',$9,NULL,$10)
            """, str(uuid.uuid4()), p["user_id"], p["chat_id"], p["message_id"], p["model_id"],
                 p["input"], p["output"], p["total"],
                 json.dumps({"estimated": True, "reason": "provider_sent_no_usage"}), p["ts"])
            # bump conversation totals (NOT last_*), message_count
            await conn.execute("""
                UPDATE conversation_token_usage SET
                  total_input_tokens=total_input_tokens+$2, total_output_tokens=total_output_tokens+$3,
                  total_tokens=total_tokens+$4, message_count=message_count+1 WHERE chat_id=$1
            """, p["chat_id"], p["input"], p["output"], p["total"])
            # bump daily (user, UTC day of ts)
            await conn.execute("""
                UPDATE daily_token_usage SET
                  total_input_tokens=total_input_tokens+$3, total_output_tokens=total_output_tokens+$4,
                  total_tokens=total_tokens+$5, message_count=message_count+1
                WHERE user_id=$1 AND date=to_char(to_timestamp($2) AT TIME ZONE 'UTC','YYYY-MM-DD')
            """, p["user_id"], p["ts"], p["input"], p["output"], p["total"])
            # bump model (per-user and global)
            for uid in (p["user_id"], None):
                await conn.execute("""
                    UPDATE model_token_usage SET
                      total_input_tokens=total_input_tokens+$3, total_output_tokens=total_output_tokens+$4,
                      total_tokens=total_tokens+$5, message_count=message_count+1
                    WHERE model_id=$2 AND user_id IS NOT DISTINCT FROM $1
                """, uid, p["model_id"], p["input"], p["output"], p["total"])
    print(f"\nAPPLIED: inserted {len(plan)} estimated events + bumped aggregates.")
    await conn.close()


asyncio.run(main())
