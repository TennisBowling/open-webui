import sqlite3, json
DB="/home/tennisbowling/.local/lib/python3.12/site-packages/open_webui/data/webui.db"
CHAT="b21bac46-5f89-41dd-a177-589327372abb"
TARGET="call_VElL4AawN7g0NYS9ButWyP11"
c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
# migrated chat_message rows
rows=c.execute("select message_id,role,content_is_json,meta,sequence from chat_message where chat_id=? order by sequence",(CHAT,)).fetchall()
print("== chat_message rows:",len(rows))
for r in rows:
    meta=json.loads(r["meta"]) if r["meta"] else {}
    cbs=meta.get("content_blocks")
    runs=meta.get("subagent_runs")
    print(f"\n--- seq={r['sequence']} mid={r['message_id']} role={r['role']} cbs={'list['+str(len(cbs))+']' if isinstance(cbs,list) else type(cbs).__name__} runs={'dict['+str(len(runs))+']' if isinstance(runs,dict) else type(runs).__name__}")
    if isinstance(cbs,list):
        for bi,b in enumerate(cbs):
            if not isinstance(b,dict): continue
            if b.get("type")=="tool_calls":
                calls=b.get("content") or []
                results=b.get("results") or []
                callids=[ (cc.get("id") if isinstance(cc,dict) else None) for cc in calls]
                print(f"   block#{bi} tool_calls: callids={callids}")
                for res in results:
                    if isinstance(res,dict):
                        ct=res.get("content")
                        clen=len(ct) if isinstance(ct,str) else (-1 if ct is None else -2)
                        print(f"      result tcid={res.get('tool_call_id')} subagent_id={res.get('subagent_id')} content_len={clen}")
    if isinstance(runs,dict):
        for k,run in runs.items():
            if not isinstance(run,dict): continue
            ft=run.get("final_text")
            ftlen=len(ft) if isinstance(ft,str) else (-1 if ft is None else -2)
            print(f"   run key={k!r} status={run.get('status')} tcid={run.get('tool_call_id')} subagent_id={run.get('subagent_id')} ft_len={ftlen} continuation={run.get('continuation')}")
print("\n\n== searching for TARGET",TARGET)
