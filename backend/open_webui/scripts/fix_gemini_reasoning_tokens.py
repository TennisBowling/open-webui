#!/usr/bin/env python3
"""Retroactively fix the "C" provider (bare-id) Gemini reasoning-token miscount.

Background
----------
A custom upstream provider (models marked with a " C" suffix in the UI; stored
under BARE model-ids with no ``/`` prefix) reported usage where
``completion_tokens`` EXCLUDED reasoning tokens, while ``total_tokens`` had
already folded them in. The canonical convention (what every prefixed provider
and gpt-5.5 already follow) is ``reasoning_tokens <= completion_tokens`` and
``total_tokens == prompt_tokens + completion_tokens``.

ONLY two models are affected:
    gemini-3.1-pro-preview   (Gemini 3.1 Pro C)
    gemini-3-flash-preview   (Gemini 3 Flash C)

gpt-5.5 is INTENTIONALLY EXCLUDED: it shares the schema shape but its
completion_tokens already includes reasoning (verified via tiktoken on real
message content). "Fixing" it would double-count ~2.18M tokens.

The fix (per broken row): ``completion_tokens += reasoning_tokens``.
``total_tokens`` is left UNCHANGED (the provider already summed it correctly).

Detection is by EXACT SCHEMA FINGERPRINT, not arithmetic — see ``is_broken``.

Surfaces patched
----------------
1. token_usage_event           — rewrite completion_tokens + raw_usage JSON
2. chat_message.meta.usage     — rewrite usage.completion_tokens in meta JSON
3. conversation_token_usage    — relative bump of total_output_tokens (+ last_output_tokens where the latest event was broken)
4. daily_token_usage           — relative bump of total_output_tokens
5. model_token_usage           — relative bump of total_output_tokens (user-rows + global NULL row)

The legacy ``chat.chat`` JSON blob is deliberately NOT touched: all affected
chats are migrated (messages_migrated=1), so the live read hydrates from the
chat_message table and the blob's usage is dead.

Idempotency & live-DB safety
----------------------------
* The event/message selector only matches the OLD broken format
  (``total == prompt + completion + reasoning`` and ``total != prompt + completion``).
  After a fix a row has ``total == prompt + completion`` and will not re-match.
* Aggregate deltas are derived from the broken EVENTS found at apply time, inside
  the same transaction that fixes them. A second run finds zero broken events,
  so it adds zero. Aggregate writes use atomic ``col = col + :delta`` so they
  never clobber the server's concurrent live increments.
* A precise rollback JSON (only the touched rows' original values) is written
  before applying. Restore with ``--rollback <file>``. This is preferred over a
  3GB file copy, which on restore would lose chats written after the fix.

Usage
-----
    python fix_gemini_reasoning_tokens.py                 # dry-run (default), writes nothing
    python fix_gemini_reasoning_tokens.py --apply         # apply, writing a rollback file first
    python fix_gemini_reasoning_tokens.py --rollback fix_gemini_rollback_*.json
    python fix_gemini_reasoning_tokens.py --db /path/to/webui.db --dry-run
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

BROKEN_MODELS = ("gemini-3.1-pro-preview", "gemini-3-flash-preview")

# Exact broken-provider usage schema. ANY deviation => not this provider.
_TOP = ("completion_tokens", "completion_tokens_details", "prompt_tokens",
        "prompt_tokens_details", "total_tokens")
_PD = ("cached_tokens",)
_CD = ("reasoning_tokens",)


def _exact_schema(u) -> bool:
    if not isinstance(u, dict) or tuple(sorted(u.keys())) != _TOP:
        return False
    pd = u.get("prompt_tokens_details")
    cd = u.get("completion_tokens_details")
    return (isinstance(pd, dict) and tuple(sorted(pd.keys())) == _PD
            and isinstance(cd, dict) and tuple(sorted(cd.keys())) == _CD)


def _reasoning(u) -> int:
    try:
        return int(u["completion_tokens_details"]["reasoning_tokens"] or 0)
    except Exception:
        return 0


def is_broken(usage: dict) -> int:
    """Return reasoning_tokens (>0) if this usage blob is the OLD broken format,
    else 0. Old format: exact schema, reasoning>0, total==p+c+R and total!=p+c."""
    if not _exact_schema(usage):
        return 0
    R = _reasoning(usage)
    if R <= 0:
        return 0
    try:
        p = int(usage["prompt_tokens"] or 0)
        c = int(usage["completion_tokens"] or 0)
        t = int(usage["total_tokens"] or 0)
    except Exception:
        return 0
    if t == p + c + R and t != p + c:
        return R
    return 0


def _default_db_path() -> str:
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "webui.db")
    guess = "/home/tennisbowling/.local/lib/python3.12/site-packages/open_webui/data/webui.db"
    if os.path.exists(guess):
        return guess
    return os.path.join(os.getcwd(), "webui.db")


def _connect(db_path: str, read_only: bool) -> sqlite3.Connection:
    if read_only:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
    else:
        con = sqlite3.connect(db_path, timeout=60)
        con.execute("PRAGMA busy_timeout=60000")
    con.row_factory = sqlite3.Row
    return con


# ----------------------------------------------------------------------------
# Scan: find broken rows and compute everything needed for dry-run + apply
# ----------------------------------------------------------------------------

def scan(con):
    cur = con.cursor()
    placeholders = ",".join("?" * len(BROKEN_MODELS))

    # --- token_usage_event ---
    ev_fixes = []  # (id, old_completion, new_completion, old_raw_usage_str, new_raw_usage_str, R)
    conv_delta = defaultdict(int)        # attributed_chat_id -> sum R
    daily_delta = defaultdict(int)       # (user_id, 'YYYY-MM-DD') -> sum R
    modu_delta = defaultdict(int)        # (user_id, model_id) -> sum R
    modg_delta = defaultdict(int)        # model_id -> sum R

    rows = cur.execute(
        f"""SELECT id, user_id, attributed_chat_id, model_id,
                   prompt_tokens, completion_tokens, total_tokens, raw_usage, created_at
            FROM token_usage_event WHERE model_id IN ({placeholders})""",
        BROKEN_MODELS,
    ).fetchall()
    for r in rows:
        raw = r["raw_usage"]
        try:
            u = json.loads(raw) if raw else {}
        except Exception:
            continue
        R = is_broken(u)
        if not R:
            continue
        old_c = int(r["completion_tokens"])
        new_c = old_c + R
        new_u = dict(u)
        new_u["completion_tokens"] = new_c
        ev_fixes.append((
            r["id"], old_c, new_c,
            raw if isinstance(raw, str) else json.dumps(u),
            json.dumps(new_u),
            R,
        ))
        d = datetime.fromtimestamp(r["created_at"], timezone.utc).strftime("%Y-%m-%d")
        if r["attributed_chat_id"]:
            conv_delta[r["attributed_chat_id"]] += R
        if r["user_id"]:
            daily_delta[(r["user_id"], d)] += R
            modu_delta[(r["user_id"], r["model_id"])] += R
        modg_delta[r["model_id"]] += R

    # --- last_output_tokens: chats whose LATEST event is a broken gemini call ---
    last_fixes = []  # (chat_id, old_last_output, new_last_output)
    for chat_id in conv_delta.keys():
        last = cur.execute(
            """SELECT model_id, prompt_tokens, completion_tokens, total_tokens, raw_usage
               FROM token_usage_event WHERE attributed_chat_id=?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (chat_id,),
        ).fetchone()
        if not last or last["model_id"] not in BROKEN_MODELS:
            continue
        try:
            u = json.loads(last["raw_usage"]) if last["raw_usage"] else {}
        except Exception:
            continue
        R = is_broken(u)
        if not R:
            continue
        ctu = cur.execute(
            "SELECT last_output_tokens FROM conversation_token_usage WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        if not ctu:
            continue
        old_last = int(last["completion_tokens"])
        # Only bump when the stored last_output still equals the broken value
        # (i.e. we won't clobber a newer correct message).
        if int(ctu["last_output_tokens"] or 0) == old_last:
            last_fixes.append((chat_id, old_last, old_last + R))

    # --- chat_message.meta.usage ---
    msg_fixes = []  # (chat_id, message_id, old_meta_str, new_meta_str, R)
    cms = cur.execute(
        f"""SELECT chat_id, message_id, model, meta FROM chat_message
            WHERE model IN ({placeholders}) AND meta LIKE '%reasoning_tokens%'""",
        BROKEN_MODELS,
    ).fetchall()
    for r in cms:
        meta_raw = r["meta"]
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except Exception:
            continue
        u = meta.get("usage") if isinstance(meta, dict) else None
        R = is_broken(u) if isinstance(u, dict) else 0
        if not R:
            continue
        new_meta = dict(meta)
        new_usage = dict(u)
        new_usage["completion_tokens"] = int(u["completion_tokens"]) + R
        new_meta["usage"] = new_usage
        msg_fixes.append((
            r["chat_id"], r["message_id"],
            meta_raw if isinstance(meta_raw, str) else json.dumps(meta),
            json.dumps(new_meta),
            R,
        ))

    return {
        "ev_fixes": ev_fixes,
        "conv_delta": conv_delta,
        "daily_delta": daily_delta,
        "modu_delta": modu_delta,
        "modg_delta": modg_delta,
        "last_fixes": last_fixes,
        "msg_fixes": msg_fixes,
    }


def print_summary(s):
    ev = s["ev_fixes"]
    ev_out = sum(x[5] for x in ev)
    msg = s["msg_fixes"]
    msg_out = sum(x[4] for x in msg)
    by_model = defaultdict(lambda: [0, 0])
    print("=" * 78)
    print("SCAN SUMMARY (gemini-3.1-pro-preview, gemini-3-flash-preview; gpt-5.5 excluded)")
    print("=" * 78)
    print(f"\ntoken_usage_event rows to fix : {len(ev):,}   (+{ev_out:,} output tokens)")
    print(f"chat_message rows to fix      : {len(msg):,}   (+{msg_out:,} output tokens)")
    print(f"\nAggregate relative bumps (total_output_tokens only; total_tokens unchanged):")
    print(f"  conversation_token_usage    : {len(s['conv_delta']):,} chats   (+{sum(s['conv_delta'].values()):,})")
    print(f"    of which last_output bumps: {len(s['last_fixes']):,} chats (latest event was broken)")
    print(f"  daily_token_usage           : {len(s['daily_delta']):,} (user,date)   (+{sum(s['daily_delta'].values()):,})")
    print(f"  model_token_usage (user)    : {len(s['modu_delta']):,} rows   (+{sum(s['modu_delta'].values()):,})")
    print(f"  model_token_usage (global)  : {len(s['modg_delta']):,} rows   (+{sum(s['modg_delta'].values()):,})")
    for m, v in s["modg_delta"].items():
        print(f"      {m:28s} +{v:,}")
    # consistency check: every aggregate dimension should sum to the same event output delta
    sums = {
        "events": ev_out,
        "conv": sum(s["conv_delta"].values()),
        "daily": sum(s["daily_delta"].values()),
        "modu": sum(s["modu_delta"].values()),
        "modg": sum(s["modg_delta"].values()),
    }
    ok = len(set(sums.values())) == 1
    print(f"\nconsistency (all event-derived deltas equal): {sums}  -> {'OK' if ok else 'MISMATCH!'}")
    if not ok:
        print("  WARNING: aggregate dimensions disagree; investigate before applying.")
    return ok


# ----------------------------------------------------------------------------
# Apply
# ----------------------------------------------------------------------------

def apply(con, s, rollback_path: str):
    cur = con.cursor()

    # Build rollback payload (original values only for touched rows).
    rollback = {
        "created_at": int(time.time()),
        "db_note": "Reverse with: fix_gemini_reasoning_tokens.py --rollback <thisfile>",
        "events": [{"id": x[0], "old_completion_tokens": x[1], "old_raw_usage": x[3]} for x in s["ev_fixes"]],
        "messages": [{"chat_id": x[0], "message_id": x[1], "old_meta": x[2]} for x in s["msg_fixes"]],
        "conv_delta": {k: v for k, v in s["conv_delta"].items()},
        "daily_delta": {f"{k[0]}\t{k[1]}": v for k, v in s["daily_delta"].items()},
        "modu_delta": {f"{k[0]}\t{k[1]}": v for k, v in s["modu_delta"].items()},
        "modg_delta": {k: v for k, v in s["modg_delta"].items()},
        "last_fixes": [{"chat_id": c, "old": o, "new": n} for (c, o, n) in s["last_fixes"]],
    }
    with open(rollback_path, "w") as f:
        json.dump(rollback, f)
    print(f"Rollback file written: {rollback_path}  "
          f"({len(rollback['events'])} events, {len(rollback['messages'])} messages)")

    cur.execute("BEGIN IMMEDIATE")
    try:
        # 1. token_usage_event
        for _id, _oldc, newc, _oldraw, newraw, _R in s["ev_fixes"]:
            cur.execute(
                "UPDATE token_usage_event SET completion_tokens=?, raw_usage=? WHERE id=?",
                (newc, newraw, _id),
            )
        # 2. chat_message
        for chat_id, message_id, _oldmeta, newmeta, _R in s["msg_fixes"]:
            cur.execute(
                "UPDATE chat_message SET meta=? WHERE chat_id=? AND message_id=?",
                (newmeta, chat_id, message_id),
            )
        # 3. conversation_token_usage (relative; never clobbers concurrent increments)
        for chat_id, delta in s["conv_delta"].items():
            cur.execute(
                "UPDATE conversation_token_usage "
                "SET total_output_tokens = total_output_tokens + ?, updated_at = updated_at "
                "WHERE chat_id=?",
                (delta, chat_id),
            )
        # 3b. last_output_tokens (conditional set — idempotent)
        for chat_id, old_last, new_last in s["last_fixes"]:
            cur.execute(
                "UPDATE conversation_token_usage SET last_output_tokens=? "
                "WHERE chat_id=? AND last_output_tokens=?",
                (new_last, chat_id, old_last),
            )
        # 4. daily_token_usage
        for (user_id, d), delta in s["daily_delta"].items():
            cur.execute(
                "UPDATE daily_token_usage SET total_output_tokens = total_output_tokens + ? "
                "WHERE user_id=? AND date=?",
                (delta, user_id, d),
            )
        # 5. model_token_usage (user rows)
        for (user_id, model_id), delta in s["modu_delta"].items():
            cur.execute(
                "UPDATE model_token_usage SET total_output_tokens = total_output_tokens + ? "
                "WHERE user_id=? AND model_id=?",
                (delta, user_id, model_id),
            )
        # 5b. model_token_usage (global NULL row)
        for model_id, delta in s["modg_delta"].items():
            cur.execute(
                "UPDATE model_token_usage SET total_output_tokens = total_output_tokens + ? "
                "WHERE user_id IS NULL AND model_id=?",
                (delta, model_id),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    print("Applied and committed.")


def rollback(con, rollback_path: str):
    with open(rollback_path) as f:
        rb = json.load(f)
    cur = con.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        for e in rb["events"]:
            cur.execute(
                "UPDATE token_usage_event SET completion_tokens=?, raw_usage=? WHERE id=?",
                (e["old_completion_tokens"], e["old_raw_usage"], e["id"]),
            )
        for m in rb["messages"]:
            cur.execute(
                "UPDATE chat_message SET meta=? WHERE chat_id=? AND message_id=?",
                (m["old_meta"], m["chat_id"], m["message_id"]),
            )
        for chat_id, delta in rb["conv_delta"].items():
            cur.execute(
                "UPDATE conversation_token_usage SET total_output_tokens = total_output_tokens - ? WHERE chat_id=?",
                (delta, chat_id),
            )
        for lf in rb["last_fixes"]:
            cur.execute(
                "UPDATE conversation_token_usage SET last_output_tokens=? WHERE chat_id=? AND last_output_tokens=?",
                (lf["old"], lf["chat_id"], lf["new"]),
            )
        for key, delta in rb["daily_delta"].items():
            user_id, d = key.split("\t")
            cur.execute(
                "UPDATE daily_token_usage SET total_output_tokens = total_output_tokens - ? WHERE user_id=? AND date=?",
                (delta, user_id, d),
            )
        for key, delta in rb["modu_delta"].items():
            user_id, model_id = key.split("\t")
            cur.execute(
                "UPDATE model_token_usage SET total_output_tokens = total_output_tokens - ? WHERE user_id=? AND model_id=?",
                (delta, user_id, model_id),
            )
        for model_id, delta in rb["modg_delta"].items():
            cur.execute(
                "UPDATE model_token_usage SET total_output_tokens = total_output_tokens - ? WHERE user_id IS NULL AND model_id=?",
                (delta, model_id),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    print(f"Rolled back {len(rb['events'])} events, {len(rb['messages'])} messages, and aggregate deltas.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=_default_db_path(), help="Path to webui.db")
    ap.add_argument("--apply", action="store_true", help="Apply the fix (default is dry-run)")
    ap.add_argument("--rollback", metavar="FILE", help="Reverse a prior apply using its rollback JSON")
    args = ap.parse_args()

    print(f"DB: {args.db}")
    if not os.path.exists(args.db):
        print("ERROR: db not found", file=sys.stderr)
        sys.exit(1)

    if args.rollback:
        con = _connect(args.db, read_only=False)
        try:
            rollback(con, args.rollback)
        finally:
            con.close()
        return

    # Scan is always read-only.
    ro = _connect(args.db, read_only=True)
    try:
        s = scan(ro)
    finally:
        ro.close()
    ok = print_summary(s)

    if not args.apply:
        print("\nDRY RUN — no changes written. Re-run with --apply to apply.")
        return

    if not ok:
        print("\nAborting apply due to consistency mismatch.", file=sys.stderr)
        sys.exit(2)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    rollback_path = os.path.join(os.path.dirname(os.path.abspath(args.db)),
                                 f"fix_gemini_rollback_{ts}.json")
    con = _connect(args.db, read_only=False)
    try:
        apply(con, s, rollback_path)
    finally:
        con.close()
    print("\nDone. Verify with a fresh dry-run (should report 0 rows to fix).")


if __name__ == "__main__":
    main()
