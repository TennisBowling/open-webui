"""ADVERSARIAL ROUND 2 — FANOUT live-vs-reload divergence probe.

This isolates the one suspicious result from test_adv2_endtoend.py: in the
fanout-rerun concurrent-import case the PERSISTED/RELOAD list dedups to 1 card (via
_merge_files content-key), but the LIVE frontend list (id-first dedup only) shows 2
distinct-id cards for the SAME logical output file.

Here we drive TWO genuinely-concurrent imports with asyncio.gather (no manual
ledger juggling) against a backend whose ledger merge uses the REAL
merge_container_outputs_state and whose chat-row "lock" is a real asyncio.Lock, so
the two imports interleave their (read snapshot -> hash -> store -> persist) exactly
as production would. We then ask:

  * how many DISTINCT-id descriptors of the same (workspace_path, sha256) did the
    two imports emit? (the live frontend sees the UNION of both emits)
  * RELOAD (persisted message.files via _merge_files): how many cards?
  * LIVE (frontend id-first merge of both emits): how many cards?

If imports==2 and LIVE==2 while RELOAD==1, the END-TO-END NO-DUP invariant is
VIOLATED on the LIVE side for the fanout case (the invariant requires exactly once
"live AND on reload").

Run:
  cd backend && WEBUI_SECRET_KEY=test OAUTH_SESSION_TOKEN_ENCRYPTION_KEY=test \
    python3 -m pytest open_webui/test/util/test_adv2_fanout_live.py -x -q -s
"""

import asyncio
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

from test.util.db import configure_test_database

configure_test_database()

import open_webui.utils.container_workspace as cw  # noqa: E402
from open_webui.models.chats import merge_container_outputs_state  # noqa: E402


class ConcurrentBackend:
    """Mirrors the row-locked ledger write + the message.files store. The ledger
    merge uses the REAL pure helper; the lock is a real asyncio.Lock so two
    concurrent imports serialize their WRITES but each still read its own stale
    snapshot at the top of import_changed_container_outputs (which reads meta via
    get_chat_by_id BEFORE acquiring any lock — exactly the prod race window)."""

    def __init__(self):
        self.meta = {}
        self.messages: dict[str, dict] = {}
        self._ledger_lock = asyncio.Lock()
        self._msg_lock = asyncio.Lock()
        # Force a real interleave: after the first import reads the snapshot, yield
        # so the second import also reads the SAME (empty) snapshot before either
        # writes. A small barrier on get_chat_by_id reproduces the race window.
        self._snapshot_reads = 0

    async def get_chat_by_id(self, _cid):
        # Let BOTH concurrent imports read the ledger snapshot before either writes.
        self._snapshot_reads += 1
        await asyncio.sleep(0)  # yield to the other task
        obj = MagicMock()
        obj.meta = dict(self.meta)  # snapshot copy (what the importer read)
        return obj

    async def merge_container_workspace_outputs(self, _cid, updates, data_root="", server_id=""):
        async with self._ledger_lock:  # row lock: re-read live ledger, merge
            cw_meta = dict(self.meta.get("container_workspace") or {})
            live = dict(cw_meta.get("outputs") or {})
            merged = merge_container_outputs_state(live, updates)
            cw_meta["outputs"] = merged
            new_meta = dict(self.meta)
            new_meta["container_workspace"] = cw_meta
            self.meta = new_meta
        return True

    async def get_message_by_id_and_message_id(self, _cid, mid):
        msg = self.messages.get(mid)
        if msg is None:
            return None
        return {"files": list(msg.get("files", []))}

    async def upsert_message_to_chat_by_id_and_message_id(self, _cid, mid, partial, **k):
        async with self._msg_lock:
            msg = self.messages.setdefault(mid, {})
            if "files" in partial:
                msg["files"] = list(partial["files"])
        return None


def _patch(monkeypatch, backend, data_root):
    monkeypatch.setattr(cw, "is_container_workspace_active", lambda *a, **k: True)
    monkeypatch.setattr(cw, "_settings", lambda *_a, **_k: (True, str(data_root), "srv-1"))
    monkeypatch.setattr(cw, "_workspace_chat_id", lambda _m: "chat-1")

    async def _noop_reclaim(*a, **k):
        return None

    monkeypatch.setattr(cw, "_reclaim_outputs", _noop_reclaim)
    monkeypatch.setattr(cw.Chats, "get_chat_by_id", backend.get_chat_by_id)
    monkeypatch.setattr(cw.Chats, "merge_container_workspace_outputs", backend.merge_container_workspace_outputs)
    monkeypatch.setattr(cw.Chats, "get_message_by_id_and_message_id", backend.get_message_by_id_and_message_id)
    monkeypatch.setattr(cw.Chats, "upsert_message_to_chat_by_id_and_message_id", backend.upsert_message_to_chat_by_id_and_message_id)

    async def _fake_store(req, usr, path, display_name, size, sha256, workspace_path, chat_id, message_id, version):
        fid = str(uuid.uuid4())
        # add a yield so the store step interleaves with the other import too.
        await asyncio.sleep(0)
        return {
            "type": "file",
            "id": fid,
            "name": display_name,
            "url": f"/api/v1/files/{fid}/content",
            "size": size,
            "status": "uploaded",
            "container_workspace": {
                "workspace_path": workspace_path,
                "sha256": sha256,
                "version": version,
            },
        }

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)


def _key_of(f):
    return f.get("id") or f.get("url") or f.get("content") or ("json", repr(sorted(f.items())))


def _file_content_key(f):
    cw_block = f.get("container_workspace") if isinstance(f, dict) else None
    if isinstance(cw_block, dict) and cw_block.get("workspace_path") and cw_block.get("sha256"):
        return f"cw\x00{cw_block['workspace_path']}\x00{cw_block['sha256']}"
    return None


def _frontend_merge(message_files, event_files):
    # Mirrors the patched Chat.svelte mergeMessageFiles: dedup by id AND by content
    # identity (workspace_path + sha256), so two distinct-id descriptors for the
    # same content collapse to one LIVE card (matching the backend _merge_files).
    merged = list(message_files or [])
    seen_ids = set()
    seen_content = set()
    for f in merged:
        k = _key_of(f)
        if k:
            seen_ids.add(k)
        ck = _file_content_key(f)
        if ck:
            seen_content.add(ck)
    for f in event_files or []:
        k = _key_of(f)
        ck = _file_content_key(f)
        if k and k in seen_ids:
            continue
        if ck and ck in seen_content:
            continue
        if k:
            seen_ids.add(k)
        if ck:
            seen_content.add(ck)
        merged.append(f)
    return merged


def _socket_files_persist(backend, mid, incoming):
    # Mirror the real socket handler: it is itself async and runs under the same
    # event loop; here we apply it after both imports (the persisted union).
    msg = backend.messages.get(mid) or {}
    files = cw._merge_files(msg.get("files", []), list(incoming))
    backend.messages.setdefault(mid, {})["files"] = list(files)


def test_fanout_two_concurrent_imports_live_vs_reload(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root = tmp / "containers"
    outputs = data_root / "chat-1" / "workspace" / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "chart.png").write_bytes(b"\x89PNG identical content for both imports")

    backend = ConcurrentBackend()
    _patch(monkeypatch, backend, data_root)

    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    def _meta():
        return {
            "container_workspace_output_message_id": "m1",
            "message_id": "m1",
            "chat_id": "chat-1",
            "user_id": "user-1",
        }

    async def _both():
        # Two genuinely-concurrent imports to the SAME message_id (fanout rerun).
        return await asyncio.gather(
            cw.import_changed_container_outputs(request, _meta(), user),
            cw.import_changed_container_outputs(request, _meta(), user),
        )

    imported_a, imported_b = asyncio.run(_both())

    all_emitted = list(imported_a) + list(imported_b)
    print("import_a:", len(imported_a), "import_b:", len(imported_b))
    print("emitted descriptors:", len(all_emitted),
          "ids:", [d["id"] for d in all_emitted])

    # Persist BOTH emits through the socket {type:"files"} handler (as both call
    # sites would fire). Order doesn't matter for _merge_files content-key dedup.
    for imp in (imported_a, imported_b):
        if imp:
            _socket_files_persist(backend, "m1", imp)

    reloaded = backend.messages.get("m1", {}).get("files", [])

    # LIVE: the frontend receives both emits (both {type:files} and chat:completion
    # for each import) and merges id-first into the in-memory message.files.
    live = []
    for imp in (imported_a, imported_b):
        live = _frontend_merge(live, imp)   # {type:"files"} handler
        live = _frontend_merge(live, imp)   # chat:completion handler

    reload_cards = [f for f in reloaded if f.get("name") == "chart.png"]
    live_cards = [f for f in live if f.get("name") == "chart.png"]

    print("RELOAD chart.png cards:", len(reload_cards))
    print("LIVE   chart.png cards:", len(live_cards))

    # The persisted/reload truth: content-key dedup in _merge_files -> 1 card.
    assert len(reload_cards) == 1, f"reload should be 1, got {reload_cards}"

    # The invariant requires EXACTLY ONCE live AND on reload. After the frontend
    # mergeMessageFiles fix (content-key dedup mirroring the backend), even when the
    # race genuinely minted two distinct-id descriptors of the same content, the
    # LIVE merge collapses them -> 1 card. LIVE == RELOAD == 1.
    if len(all_emitted) >= 2 and len({d["id"] for d in all_emitted}) >= 2:
        print("NOTE: race produced two distinct descriptors; content-key dedup must collapse them live.")
    assert len(live_cards) == 1, (
        f"LIVE must match RELOAD (exactly one card); got live={len(live_cards)} "
        f"reload={len(reload_cards)} from {len(all_emitted)} emitted descriptors."
    )
