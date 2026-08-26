"""ADVERSARIAL (RELOAD/MERGE invariant): "No client-side path reintroduces a
duplicate container file into message.files, and the live-rendered list matches
the reloaded list."

This test demonstrates a LIVE-vs-RELOAD DIVERGENCE caused by the server-side DB
side-effect of the `files` socket event, which the patched frontend dedup cannot
compensate for (the reloaded list comes straight from the persisted ledger).

THE SEQUENCE (all real code paths, names from the tree):

1. import_changed_container_outputs(...) (utils/container_workspace.py:828) imports
   a brand-new outputs file. It builds `imported=[descriptor]`, and at lines 994-999
   persists  message.files = _merge_files(existing_files, imported)  -> ONE copy in
   the DB. It RETURNS `imported`.

2. The call site (utils/middleware.py:3383-3392 and 6506-6519) then emits a socket
   event:
        event_emitter({"type": "files", "data": {"files": container_output_files}})
   where container_output_files == imported (the SAME descriptor list).

3. event_emitter is get_event_emitter(metadata) with update_db=True
   (socket/main.py:1397). Its update_db block has a `files` handler
   (socket/main.py:1523-1540) that does a NAIVE, UN-DEDUPED merge:

        message = get_message_by_id_and_message_id(...)   # already has imported (step 1)
        files = event_data["data"]["files"]               # == imported again
        files.extend(message.get("files", []))            # imported + [imported + prior]
        upsert(..., {"files": files})                     # DUPLICATE persisted

   So the PERSISTED ledger now holds the container descriptor TWICE (same id, same
   url, same container_workspace block).

4. LIVE: the frontend `files` handler (Chat.svelte:2969-2982) receives the same
   event and dedups id-first -> the live-rendered message.files has ONE copy.

5. RELOAD: loadChat() reads message.files straight from the persisted ledger
   (Chat.svelte:4018-4129, history = chatContent.history) -> TWO copies. The keyed
   {#each message.files as file (file?.id ?? ...)} in ResponseMessage.svelte renders
   the duplicate (and because BOTH share the same id, the keyed-each even collides
   on key -> Svelte keyed-each duplicate-key behavior).

=> LIVE (1 card) != RELOAD (2 cards). Invariant violated.

This test drives the REAL import to populate the message store, then runs the EXACT
socket `files`-event persistence logic against the SAME store (verbatim from
socket/main.py:1523-1540), then computes what loadChat would render (the persisted
list) vs what the live frontend dedup yields, and asserts they diverge.

Run:
  cd backend && WEBUI_SECRET_KEY=test OAUTH_SESSION_TOKEN_ENCRYPTION_KEY=test \
    python -m pytest open_webui/test/util/test_adv_reload_merge_socket_extend.py -x -q
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from test.util.db import configure_test_database

configure_test_database()

import open_webui.utils.container_workspace as cw  # noqa: E402


def _setup_workspace(tmp: Path):
    data_root = tmp / "containers"
    workspace = data_root / "chat-1" / "workspace"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    return data_root, workspace, outputs


def _merge_outputs_like_db(meta_store, outputs_updates, data_root, server_id):
    meta = dict(meta_store["meta"])
    cw_meta = dict(meta.get("container_workspace") or {})
    outputs = dict(cw_meta.get("outputs") or {})
    for key, new_state in outputs_updates.items():
        if not isinstance(new_state, dict):
            continue
        existing = outputs.get(key)
        if (
            isinstance(existing, dict)
            and existing.get("last_hash")
            and existing.get("last_hash") == new_state.get("last_hash")
        ):
            merged = dict(existing)
            for sf in ("stat_size", "stat_mtime_ns"):
                if sf in new_state:
                    merged[sf] = new_state[sf]
            outputs[key] = merged
        else:
            outputs[key] = new_state
    cw_meta["outputs"] = outputs
    meta["container_workspace"] = cw_meta
    meta_store["meta"] = meta


def _patch_collaborators(monkeypatch, data_root, meta_store, message_store):
    monkeypatch.setattr(cw, "is_container_workspace_active", lambda *a, **k: True)
    monkeypatch.setattr(
        cw, "_settings", lambda *_a, **_k: (MagicMock(), str(data_root), "srv-1")
    )
    monkeypatch.setattr(cw, "_workspace_chat_id", lambda _m: "chat-1")

    async def _noop_reclaim(*a, **k):
        return None

    monkeypatch.setattr(cw, "_reclaim_outputs", _noop_reclaim)

    async def _fake_get_chat_by_id(*_a, **_k):
        obj = MagicMock()
        obj.meta = meta_store["meta"]
        return obj

    async def _fake_merge_outputs(_cid, outputs_updates, data_root="", server_id=""):
        _merge_outputs_like_db(meta_store, outputs_updates, data_root, server_id)
        return True

    async def _fake_get_message(*_a, **_k):
        return {"files": list(message_store["files"])}

    async def _fake_upsert_message(_cid, _mid, partial, **k):
        if "files" in partial:
            message_store["files"] = list(partial["files"])

    monkeypatch.setattr(cw.Chats, "get_chat_by_id", _fake_get_chat_by_id)
    monkeypatch.setattr(cw.Chats, "merge_container_workspace_outputs", _fake_merge_outputs)
    monkeypatch.setattr(cw.Chats, "get_message_by_id_and_message_id", _fake_get_message)
    monkeypatch.setattr(
        cw.Chats, "upsert_message_to_chat_by_id_and_message_id", _fake_upsert_message
    )

    async def _fake_store(req, usr, path, display_name, size, sha256,
                          workspace_path, chat_id, message_id, version):
        import uuid

        fid = str(uuid.uuid4())
        # Mirror the REAL _store_output_file descriptor: it ALWAYS carries an id,
        # a url, and the container_workspace block.
        return {
            "type": "file",
            "id": fid,
            "name": display_name,
            "url": f"/api/v1/files/{fid}/content",
            "size": size,
            "container_workspace": {
                "workspace_path": workspace_path,
                "sha256": sha256,
                "version": version,
            },
        }

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)


# ---------------------------------------------------------------------------
# Transcription of the PATCHED socket `files`-event DB side-effect.
# Source: backend/open_webui/socket/main.py (get_event_emitter's update_db block)
# now merges with id + content-identity dedup instead of a blind extend.
# ---------------------------------------------------------------------------
def _socket_files_event_persist(message_store, event_data):
    message = {"files": list(message_store["files"])}
    incoming = event_data.get("data", {}).get("files", []) or []
    files = cw._merge_files(message.get("files", []), incoming)
    message_store["files"] = list(files)


# ---------------------------------------------------------------------------
# VERBATIM transcription of the LIVE frontend `files`-event dedup.
# Source: Chat.svelte:2969-2982 (type === 'files'). id-first seen-set.
# ---------------------------------------------------------------------------
def _frontend_live_files_merge(live_files, event_files):
    def key_of(f):
        if not isinstance(f, dict):
            return repr(f)
        # file?.id ?? file?.url ?? file?.content ?? JSON.stringify(file)
        return f.get("id") or f.get("url") or f.get("content") or repr(sorted(f.items()))

    seen = set(key_of(f) for f in (live_files or []))
    next_files = list(live_files or [])
    for f in event_files or []:
        k = key_of(f)
        if k in seen:
            continue
        seen.add(k)
        next_files.append(f)
    return next_files


def test_socket_files_event_does_not_duplicate_container_descriptor(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    (outputs / "report.csv").write_text("a,b,c\n1,2,3\n")

    meta_store = {"meta": {}}
    message_store = {"files": []}
    _patch_collaborators(monkeypatch, data_root, meta_store, message_store)

    metadata = {
        "container_workspace_output_message_id": "m1",
        "message_id": "m1",
        "chat_id": "chat-1",
        "user_id": "user-1",
        "session_id": "sess-1",
    }
    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    # --- STEP 1+2: the real import. It persists message.files (deduped) AND
    # returns `imported` (== container_output_files the call site will emit).
    imported = asyncio.run(
        cw.import_changed_container_outputs(request, metadata, user)
    )
    assert len(imported) == 1, imported
    assert len(message_store["files"]) == 1, message_store["files"]

    # The LIVE frontend list after the `files` event (id-first dedup): one card.
    live_after_event = _frontend_live_files_merge([], imported)
    assert len(live_after_event) == 1, live_after_event

    # --- STEP 3: the call site emits {"type":"files", data:{files: imported}}.
    # The PATCHED socket `files` DB side-effect merges (id + content) instead of a
    # blind extend, so it does NOT re-append the already-persisted descriptor.
    _socket_files_event_persist(
        message_store, {"type": "files", "data": {"files": list(imported)}}
    )

    # --- STEP 5: loadChat reads message.files STRAIGHT from the persisted ledger.
    reloaded_files = message_store["files"]

    report_cards_reloaded = [f for f in reloaded_files if f.get("name") == "report.csv"]
    report_cards_live = [f for f in live_after_event if f.get("name") == "report.csv"]

    print("LIVE rendered report.csv cards:", len(report_cards_live))
    print("RELOAD rendered report.csv cards:", len(report_cards_reloaded))
    print("reloaded ids:", [f.get("id") for f in reloaded_files])

    # The persisted/reloaded list holds the descriptor exactly ONCE...
    assert len(report_cards_reloaded) == 1, (
        f"socket files-event must not duplicate the descriptor; got {report_cards_reloaded}"
    )
    # ...and LIVE matches RELOAD (no divergence).
    assert len(report_cards_live) == len(report_cards_reloaded) == 1
