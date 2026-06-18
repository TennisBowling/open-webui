import sqlite3, json
DB="/home/tennisbowling/.local/lib/python3.12/site-packages/open_webui/data/webui.db"
CHAT="b21bac46-5f89-41dd-a177-589327372abb"
c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
rows=c.execute("select message_id,role,meta from chat_message where chat_id=? and role='assistant' order by sequence",(CHAT,)).fetchall()
r=rows[0]
meta=json.loads(r["meta"])
cbs=meta["content_blocks"]; runs=meta["subagent_runs"]

# 1. find all empty results and whether the same subagent has a non-empty result anywhere
sa_contentlens={}  # subagent_id -> list of (tcid, len)
empties=[]
for bi,b in enumerate(cbs):
    if not isinstance(b,dict) or b.get("type")!="tool_calls": continue
    for res in b.get("results") or []:
        if not isinstance(res,dict): continue
        sid=res.get("subagent_id"); tcid=res.get("tool_call_id")
        ct=res.get("content"); clen=len(ct) if isinstance(ct,str) else -1
        sa_contentlens.setdefault(sid,[]).append((tcid,clen,bi))
        if clen<=0:
            empties.append((bi,tcid,sid))

print("=== EMPTY launch/continuation results ===")
for bi,tcid,sid in empties:
    siblings=sa_contentlens.get(sid,[])
    best=max((l for _,l,_ in siblings), default=0)
    print(f"block#{bi} tcid={tcid} sid={sid}  -> sibling result lens for same sid: {[l for _,l,_ in siblings]} (best={best})")

# 2. show the 31-char contents
print("\n=== 31-char results decoded ===")
for bi,b in enumerate(cbs):
    if not isinstance(b,dict) or b.get("type")!="tool_calls": continue
    for res in b.get("results") or []:
        if isinstance(res,dict) and isinstance(res.get("content"),str) and len(res["content"])<=60:
            print(f"block#{bi} tcid={res.get('tool_call_id')} content={res['content']!r}")

# 3. subagent_runs for the failing subagent + all runs whose result is empty
print("\n=== runs for empty subagents ===")
empty_sids={sid for _,_,sid in empties}
for k,run in runs.items():
    if not isinstance(run,dict): continue
    if run.get("subagent_id") in empty_sids:
        ft=run.get("final_text"); ftlen=len(ft) if isinstance(ft,str) else (-1 if ft is None else -2)
        print(f"key={k!r} sid={run.get('subagent_id')} status={run.get('status')} tcid={run.get('tool_call_id')} continuation={run.get('continuation')} ft_len={ftlen} stale={run.get('stale')} err={bool(run.get('error'))}")
