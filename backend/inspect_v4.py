import sqlite3, json, re
DB="/home/tennisbowling/.local/lib/python3.12/site-packages/open_webui/data/webui.db"
CHAT="b21bac46-5f89-41dd-a177-589327372abb"
c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
meta=json.loads(c.execute("select meta from chat_message where chat_id=? and role='assistant' order by sequence limit 1",(CHAT,)).fetchone()["meta"])
runs=meta["subagent_runs"]
# the failing + empties
TARGETS=["254eb3c6-99be-4eda-a1b3-4aded160aa4f","28398ab6-04d7-4e57-89fe-465dbfccb942#call_HQwjLGcDDzN5WHICzp5BM0xt","28398ab6-04d7-4e57-89fe-465dbfccb942"]
for k,run in runs.items():
    if not isinstance(run,dict): continue
    if run.get("status")=="cancelled" or run.get("continuation"):
        print(f"key={k!r}")
        print(f"   sid={run.get('subagent_id')} assistant_msg_id={run.get('assistant_msg_id')} user_msg_id={run.get('user_msg_id')} status={run.get('status')} cont={run.get('continuation')} stale={run.get('stale')}")

# Now: for subagent 254eb3c6, dump the hidden assistant msg content_blocks text blocks fully
print("\n=== 254eb3c6 hidden assistant text/tool-result inventory ===")
rows=c.execute("select message_id,role,meta,sequence from chat_message where chat_id='254eb3c6-99be-4eda-a1b3-4aded160aa4f' order by sequence").fetchall()
for r in rows:
    if r["role"]!="assistant": continue
    m=json.loads(r["meta"]) if r["meta"] else {}
    cbs=m.get("content_blocks") or []
    print(f"msg {r['message_id']} seq={r['sequence']} blocks={len(cbs)}")
    for i,b in enumerate(cbs):
        if not isinstance(b,dict): continue
        t=b.get("type")
        if t=="text":
            cc=b.get("content") or ""
            print(f"  [{i}] text len={len(cc)} preview={cc[:120]!r}")
        elif t=="tool_calls":
            res=b.get("results") or []
            for rr in res:
                if isinstance(rr,dict):
                    ct=rr.get("content"); print(f"  [{i}] tool_result len={len(ct) if isinstance(ct,str) else -1}")
