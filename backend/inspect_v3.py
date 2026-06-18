import sqlite3, json, re
DB="/home/tennisbowling/.local/lib/python3.12/site-packages/open_webui/data/webui.db"
SIDS=["254eb3c6-99be-4eda-a1b3-4aded160aa4f","28398ab6-04d7-4e57-89fe-465dbfccb942","9eaf3298-15bf-415f-92f7-cb06d59b0ec1"]
c=sqlite3.connect(DB); c.row_factory=sqlite3.Row

def strip_details(s):
    # remove all <details>...</details> blocks
    return re.sub(r"<details.*?</details>", "", s, flags=re.S)

for SID in SIDS:
    print(f"\n========== hidden chat {SID} ==========")
    rows=c.execute("select message_id,role,content,content_is_json,meta,sequence from chat_message where chat_id=? order by sequence",(SID,)).fetchall()
    print("rows:",len(rows))
    for r in rows:
        meta=json.loads(r["meta"]) if r["meta"] else {}
        cbs=meta.get("content_blocks")
        raw=r["content"] or ""
        prose=strip_details(raw).strip() if isinstance(raw,str) else ""
        # also compute trailing text from cbs
        trailing=""
        if isinstance(cbs,list):
            buff=[]
            for b in reversed(cbs):
                if not isinstance(b,dict): continue
                t=b.get("type")
                if t=="text": buff.insert(0,b.get("content") or "")
                elif t in ("tool_calls","code_interpreter"): break
                elif t=="reasoning": continue
            trailing="\n".join(buff).strip()
        types=[b.get("type") for b in cbs if isinstance(b,dict)] if isinstance(cbs,list) else None
        print(f"  seq={r['sequence']} role={r['role']} raw_len={len(raw)} prose_after_strip={len(prose)} cbs_types={types} trailing_text_len={len(trailing)}")
        if trailing:
            print(f"     trailing preview: {trailing[:200]!r}")
        if prose and not trailing:
            print(f"     PROSE-NO-TRAILING preview: {prose[:200]!r}")
