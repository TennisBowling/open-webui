import sqlite3, json
DB="/home/tennisbowling/.local/lib/python3.12/site-packages/open_webui/data/webui.db"
CHAT="b21bac46-5f89-41dd-a177-589327372abb"
c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
meta=json.loads(c.execute("select meta from chat_message where chat_id=? and role='assistant' order by sequence limit 1",(CHAT,)).fetchone()["meta"])
runs=meta["subagent_runs"]
for k,run in runs.items():
    if isinstance(run,dict) and run.get("subagent_id")=="254eb3c6-99be-4eda-a1b3-4aded160aa4f":
        print("name=",run.get("name"),"| num=",run.get("num"),"| status=",run.get("status"))
