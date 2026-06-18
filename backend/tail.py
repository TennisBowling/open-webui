import sqlite3, json
DB="/home/tennisbowling/.local/lib/python3.12/site-packages/open_webui/data/webui.db"
SID="254eb3c6-99be-4eda-a1b3-4aded160aa4f"
c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
rows=c.execute("select message_id,role,meta,sequence from chat_message where chat_id=? and role='assistant' order by sequence desc",(SID,)).fetchall()
r=rows[0]
m=json.loads(r["meta"])
cbs=m.get("content_blocks") or []
print(f"latest assistant msg {r['message_id']} seq={r['sequence']} total_blocks={len(cbs)}")
print("=== last 6 blocks ===")
for b in cbs[-6:]:
    if not isinstance(b,dict): continue
    t=b.get("type")
    if t=="text":
        print(f"  text: content_len={len(b.get('content') or '')} repr={ (b.get('content') or '')[:80]!r}")
    elif t=="reasoning":
        print(f"  reasoning: content_len={len(b.get('content') or '')}")
    elif t=="tool_calls":
        calls=b.get('content') or []
        names=[(cc.get('function') or {}).get('name') for cc in calls if isinstance(cc,dict)]
        print(f"  tool_calls: n={len(calls)} names={names} has_results={bool(b.get('results'))}")
    else:
        print(f"  {t}")
# count tool_calls rounds
tc_rounds=sum(1 for b in cbs if isinstance(b,dict) and b.get("type")=="tool_calls")
print(f"\ntotal tool_calls rounds in this msg = {tc_rounds}")
# total tool invocations
inv=sum(len(b.get('content') or []) for b in cbs if isinstance(b,dict) and b.get("type")=="tool_calls")
print(f"total individual tool invocations = {inv}")
