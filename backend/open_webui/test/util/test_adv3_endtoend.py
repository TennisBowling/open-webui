"""ADVERSARIAL ROUND 3 — END-TO-END EXACTLY-ONCE (convergence check).

Invariant under attack (the WHOLE change set, after the round-2 frontend fix):

  With the PATCHED frontend ``mergeMessageFiles`` (dedup by id AND content-key),
  a given container output file appears EXACTLY ONCE both LIVE and on RELOAD for
  EVERY realistic sequence:   LIVE_cards == RELOAD_cards == 1.

What is REAL here (NOT mocked):
  * ``cw.import_changed_container_outputs`` — the real importer (real candidate
    scan, real stat fast-path, real content-hash dedup, real ledger persist via
    the real pure ``merge_container_outputs_state``, real ``cw._merge_files`` into
    message.files).
  * ``cw._merge_files`` — the real socket ``{type:"files"}`` DB side-effect
    (socket/main.py: ``_merge_files(message.files, incoming)``).
  * ``merge_container_outputs_state`` — the real pure ledger helper.

What is a FAITHFUL MIRROR (transcribed verbatim from the patched source):
  * ``_frontend_merge_message_files`` mirrors Chat.svelte ``mergeMessageFiles``
    (lines 4961-4992): id = f.id ?? f.url ?? f.content ?? JSON.stringify(f);
    content key = fileContentKey(f) = "cw" + <NUL> + workspace_path + <NUL> +
    sha256 (built with chr(0) to match the JS <NUL> runtime key); dedup by BOTH,
    existing-first. This is the round-2 violation's fix.

LIVE pipeline (from middleware.py 3390-3437 + Chat.svelte handlers):
  For each completion the backend emits container_output_files==imported on BOTH
  the {type:"files"} event (handler @2970) AND chat:completion (handler @5177).
  Each handler runs mergeMessageFiles(message.files, imported). So LIVE replays
  TWO mergeMessageFiles calls per completion, accumulating into message.files.

RELOAD = message.files read straight from the persisted store (after the real
socket _merge_files persist, which is what middleware 3435 reads back).

Run:
  cd backend && WEBUI_SECRET_KEY=test OAUTH_SESSION_TOKEN_ENCRYPTION_KEY=test \
    python3 -m pytest open_webui/test/util/test_adv3_endtoend.py -x -q
"""

import asyncio
import json as _json
import os
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

from test.util.db import configure_test_database

configure_test_database()

import open_webui.utils.container_workspace as cw  # noqa: E402
from open_webui.models.chats import merge_container_outputs_state  # noqa: E402  REAL pure helper


# ---------------------------------------------------------------------------
# Faithful in-memory chat backend. Ledger merge uses the REAL pure helper so the
# monotonic / identical-hash convergence rules run verbatim. The ledger write is
# guarded by a REAL asyncio.Lock and get_chat_by_id YIELDS (await asyncio.sleep)
# so two concurrent import coroutines genuinely both read the empty ledger before
# either writes — the real round-2 race, not a hand-faked stale snapshot.
# ---------------------------------------------------------------------------
class FakeChatBackend:
    def __init__(self):
        self.meta = {}                       # meta["container_workspace"]["outputs"] == the ledger
        self.messages: dict[str, dict] = {}  # message_id -> {"files": [...]}
        self._ledger_lock = asyncio.Lock()
        self.yield_on_read = False           # when True, get_chat_by_id yields the loop

    async def get_chat_by_id(self, _cid):
        if self.yield_on_read:
            # Force a real scheduler hand-off so a concurrent import runs and reads
            # the SAME (still-empty) ledger snapshot before this one persists.
            await asyncio.sleep(0)
        obj = MagicMock()
        obj.meta = dict(self.meta)  # snapshot copy, like a fresh DB read
        return obj

    async def merge_container_workspace_outputs(self, _cid, updates, data_root="", server_id=""):
        # Real merge_container_workspace_outputs holds a row lock (SELECT ... FOR
        # UPDATE), re-reads the LIVE ledger, merges, writes back. Mirror that: the
        # lock serializes, and we re-read self.meta (the live ledger) inside it.
        async with self._ledger_lock:
            cw_meta = dict(self.meta.get("container_workspace") or {})
            live = dict(cw_meta.get("outputs") or {})
            merged = merge_container_outputs_state(live, updates)  # REAL helper, live re-read
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
    monkeypatch.setattr(
        cw.Chats, "merge_container_workspace_outputs", backend.merge_container_workspace_outputs
    )
    monkeypatch.setattr(
        cw.Chats, "get_message_by_id_and_message_id", backend.get_message_by_id_and_message_id
    )
    monkeypatch.setattr(
        cw.Chats,
        "upsert_message_to_chat_by_id_and_message_id",
        backend.upsert_message_to_chat_by_id_and_message_id,
    )

    # Mirror the REAL _store_output_file descriptor contract (source 821-834):
    # ALWAYS id + url + container_workspace{workspace_path,sha256,version}; office
    # docs ALSO carry preview_file_id (both top-level and inside container_workspace).
    def _make_store(office_exts=(".docx", ".xlsx", ".pptx", ".doc")):
        async def _fake_store(
            req, usr, path, display_name, size, sha256, workspace_path, chat_id, message_id, version
        ):
            fid = str(uuid.uuid4())
            desc = {
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
            if os.path.splitext(display_name)[1].lower() in office_exts:
                preview_id = str(uuid.uuid4())
                desc["container_workspace"]["preview_file_id"] = preview_id
                desc["preview_file_id"] = preview_id
            return desc

        return _fake_store

    monkeypatch.setattr(cw, "_store_output_file", _make_store())


# ---------------------------------------------------------------------------
# VERBATIM mirror of the PATCHED frontend Chat.svelte (4961-4992).
# fileContentKey: "cw" + <NUL> + workspace_path + <NUL> + sha256, or None.
# mergeMessageFiles: dedup by id (id ?? url ?? content ?? JSON.stringify) AND
# content key, existing-first.
# ---------------------------------------------------------------------------
def _fe_file_content_key(f):
    if not isinstance(f, dict):
        return None
    cwblk = f.get("container_workspace")
    if isinstance(cwblk, dict) and cwblk.get("workspace_path") and cwblk.get("sha256"):
        # JS template literal `cw<NUL>${wp}<NUL>${sha}` -> chr(0) joins at runtime.
        return "cw" + chr(0) + str(cwblk["workspace_path"]) + chr(0) + str(cwblk["sha256"])
    return None


def _fe_id(f):
    # JS: f?.id ?? f?.url ?? f?.content ?? JSON.stringify(f)
    if not isinstance(f, dict):
        return _json.dumps(f, sort_keys=True)
    return (
        f.get("id")
        or f.get("url")
        or f.get("content")
        or _json.dumps(f, sort_keys=True)
    )


def _fe_merge_message_files(existing, incoming):
    merged = list(existing) if isinstance(existing, list) else []
    seen_ids = set()
    seen_content = set()
    for f in merged:
        fid = _fe_id(f)
        if fid:
            seen_ids.add(fid)
        ck = _fe_file_content_key(f)
        if ck:
            seen_content.add(ck)
    for f in incoming or []:
        fid = _fe_id(f)
        ck = _fe_file_content_key(f)
        if fid and fid in seen_ids:
            continue
        if ck and ck in seen_content:
            continue
        if fid:
            seen_ids.add(fid)
        if ck:
            seen_content.add(ck)
        merged.append(f)
    return merged


def _live_after_completion(prior_live_files, imported):
    """One completion: backend emits imported on BOTH {type:'files'} (handler
    @2970) AND chat:completion (handler @5177). Replay both mergeMessageFiles."""
    after_files_event = _fe_merge_message_files(prior_live_files, imported)
    after_completion = _fe_merge_message_files(after_files_event, imported)
    return after_completion


# ---------------------------------------------------------------------------
# VERBATIM socket {type:"files"} DB side-effect: _merge_files(msg.files, incoming).
# ---------------------------------------------------------------------------
def _socket_files_persist(backend, mid, incoming):
    msg = backend.messages.get(mid) or {}
    files = cw._merge_files(msg.get("files", []), list(incoming))
    backend.messages.setdefault(mid, {})["files"] = list(files)


def _cards(files, name):
    return [f for f in files if isinstance(f, dict) and f.get("name") == name]


def _metadata(message_id):
    return {
        "container_workspace_output_message_id": message_id,
        "message_id": message_id,
        "chat_id": "chat-1",
        "user_id": "user-1",
    }


def _run_turn(backend, request, user, message_id, content=None, prior_live=None):
    """One realistic turn: real import -> socket files persist. Returns
    (imported, live_files) where live_files is message.files after the LIVE
    handlers ran, accumulating onto prior_live (defaults to whatever the store
    already holds for this message — the realistic live starting point)."""
    imported = asyncio.run(
        cw.import_changed_container_outputs(request, _metadata(message_id), user, content=content)
    )
    if imported:
        _socket_files_persist(backend, message_id, imported)
    base = prior_live if prior_live is not None else []
    live = _live_after_completion(base, imported) if imported else list(base)
    return imported, live


def _make_env(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root = tmp / "containers"
    workspace = data_root / "chat-1" / "workspace"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    backend = FakeChatBackend()
    _patch(monkeypatch, backend, data_root)
    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"
    return backend, request, user, workspace, outputs


# ===========================================================================
# (1) single new file -> LIVE == RELOAD == 1.
# ===========================================================================
def test_1_single_new_file(monkeypatch):
    backend, request, user, workspace, outputs = _make_env(monkeypatch)
    (outputs / "report.csv").write_text("a,b,c\n1,2,3\n")

    imported, live = _run_turn(backend, request, user, "m1")
    assert len(imported) == 1, imported

    reload = backend.messages["m1"]["files"]
    assert len(_cards(reload, "report.csv")) == 1, reload
    assert len(_cards(live, "report.csv")) == 1, live
    assert len(reload) == 1 and len(live) == 1, (reload, live)


# ===========================================================================
# (2) TWO CONCURRENT fanout imports to the SAME message_id — the round-2
# violation. asyncio.gather + real Lock around the ledger write; get_chat_by_id
# yields so BOTH read the empty ledger. Must now be LIVE == RELOAD == 1.
# ===========================================================================
def test_2_concurrent_fanout_same_message(monkeypatch):
    backend, request, user, workspace, outputs = _make_env(monkeypatch)
    (outputs / "chart.png").write_bytes(b"\x89PNG concurrent fanout bytes")
    backend.yield_on_read = True  # force the interleave: both read empty ledger

    async def _both():
        # Two genuinely concurrent imports of the same workspace to the same msg.
        return await asyncio.gather(
            cw.import_changed_container_outputs(request, _metadata("m1"), user),
            cw.import_changed_container_outputs(request, _metadata("m1"), user),
        )

    imported_a, imported_b = asyncio.run(_both())

    # Both raced past the empty-ledger read, so BOTH imported (distinct file_id,
    # same content key). If the yield didn't interleave them, one may be empty —
    # assert at least one imported and capture both descriptor lists.
    all_imported = [d for lst in (imported_a, imported_b) for d in lst]
    assert all_imported, (imported_a, imported_b)
    # The race we want: two distinct-id descriptors for the same (path, sha256).
    if len(all_imported) == 2:
        assert all_imported[0]["id"] != all_imported[1]["id"]
        assert (
            all_imported[0]["container_workspace"]["workspace_path"]
            == all_imported[1]["container_workspace"]["workspace_path"]
        )
        assert (
            all_imported[0]["container_workspace"]["sha256"]
            == all_imported[1]["container_workspace"]["sha256"]
        )

    # Replay the socket persist for each import in the order they would emit.
    for lst in (imported_a, imported_b):
        if lst:
            _socket_files_persist(backend, "m1", lst)

    # LIVE: both completions' imported lists flow through the frontend handlers,
    # accumulating onto the same message.files. Replay completion A then B.
    live = []
    live = _live_after_completion(live, imported_a)
    live = _live_after_completion(live, imported_b)

    reload = backend.messages["m1"]["files"]
    print("CONCURRENT FANOUT  live:", len(_cards(live, "chart.png")),
          "reload:", len(_cards(reload, "chart.png")),
          "n_imported:", len(all_imported))
    assert len(_cards(reload, "chart.png")) == 1, reload
    assert len(_cards(live, "chart.png")) == 1, live
    assert len(reload) == 1 and len(live) == 1, (reload, live)


# ===========================================================================
# (3) multi-turn modify: v1 on m1, v2 on m2 (distinct names) -> each once, no
# cross-message dup.
# ===========================================================================
def test_3_multiturn_modify(monkeypatch):
    backend, request, user, workspace, outputs = _make_env(monkeypatch)
    target = outputs / "data.txt"

    target.write_text("version one\n")
    imported1, live_m1 = _run_turn(backend, request, user, "m1")
    assert len(imported1) == 1, imported1
    name_v1 = imported1[0]["name"]

    time.sleep(0.01)
    target.write_text("version two — different bytes entirely\n")
    os.utime(target, None)
    imported2, live_m2 = _run_turn(
        backend, request, user, "m2", content="see sandbox:/workspace/outputs/data.txt"
    )
    assert len(imported2) == 1, imported2
    name_v2 = imported2[0]["name"]

    files_m1 = backend.messages["m1"]["files"]
    files_m2 = backend.messages["m2"]["files"]

    assert name_v1 != name_v2, (name_v1, name_v2)
    assert len(files_m1) == 1 and files_m1[0]["name"] == name_v1, files_m1
    assert len(files_m2) == 1 and files_m2[0]["name"] == name_v2, files_m2
    assert len(live_m1) == 1 and len(live_m2) == 1, (live_m1, live_m2)
    # No cross-contamination.
    assert not _cards(files_m1, name_v2) and not _cards(live_m1, name_v2)
    assert not _cards(files_m2, name_v1) and not _cards(live_m2, name_v1)


# ===========================================================================
# (4) two DIFFERENT files in one turn -> 2 cards, each once, LIVE == RELOAD == 2.
# ===========================================================================
def test_4_two_distinct_files_one_turn(monkeypatch):
    backend, request, user, workspace, outputs = _make_env(monkeypatch)
    (outputs / "a.txt").write_text("alpha\n")
    (outputs / "b.txt").write_text("bravo\n")

    imported, live = _run_turn(backend, request, user, "m1")
    assert len(imported) == 2, imported

    reload = backend.messages["m1"]["files"]
    assert len(_cards(reload, "a.txt")) == 1 and len(_cards(reload, "b.txt")) == 1, reload
    assert len(reload) == 2, reload
    assert len(_cards(live, "a.txt")) == 1 and len(_cards(live, "b.txt")) == 1, live
    assert len(live) == 2, live
    assert {f["name"] for f in live} == {"a.txt", "b.txt"}


# ===========================================================================
# (5) office docx with preview_file_id preserved -> once each, preview intact
# LIVE and on RELOAD.
# ===========================================================================
def test_5_office_docx_preview(monkeypatch):
    backend, request, user, workspace, outputs = _make_env(monkeypatch)
    (outputs / "summary.docx").write_bytes(b"PK\x03\x04 fake docx bytes")

    imported, live = _run_turn(backend, request, user, "m1")
    assert len(imported) == 1, imported
    assert imported[0].get("preview_file_id"), imported[0]
    pid = imported[0]["preview_file_id"]

    reload = backend.messages["m1"]["files"]
    assert len(_cards(reload, "summary.docx")) == 1, reload
    assert len(_cards(live, "summary.docx")) == 1, live
    assert reload[0].get("preview_file_id") == pid, reload
    assert live[0].get("preview_file_id") == pid, live
    assert len(reload) == 1 and len(live) == 1


# ===========================================================================
# (6) regen/retry, file unchanged -> still 1 card LIVE and on RELOAD. Drives BOTH
# regen sub-cases (stat fast-path skip; mtime-touched-but-same-hash skip) and a
# stray re-emit of the original descriptor on the live side.
# ===========================================================================
def test_6_regen_unchanged(monkeypatch):
    backend, request, user, workspace, outputs = _make_env(monkeypatch)
    (outputs / "out.txt").write_text("stable content\n")

    imported1, live = _run_turn(backend, request, user, "m1")
    assert len(imported1) == 1, imported1

    # (a) stat unchanged -> fast-path skip, nothing imported.
    imported_fast, _ = _run_turn(backend, request, user, "m1", prior_live=live)
    assert imported_fast == [], imported_fast
    assert len(backend.messages["m1"]["files"]) == 1

    # (b) mtime touched, identical bytes -> re-hash detects same hash, skip.
    os.utime(outputs / "out.txt", None)
    time.sleep(0.01)
    os.utime(outputs / "out.txt", (time.time() + 5, time.time() + 5))
    imported_rehash, _ = _run_turn(backend, request, user, "m1", prior_live=live)
    assert imported_rehash == [], imported_rehash

    reload = backend.messages["m1"]["files"]
    assert len(_cards(reload, "out.txt")) == 1, reload
    assert len(reload) == 1

    # Live: even a stray re-emit of the original descriptor (same id AND same
    # content key) collapses to one.
    live2 = _live_after_completion(live, imported1)
    live2 = _live_after_completion(live2, imported1)
    assert len(_cards(live2, "out.txt")) == 1, live2
    assert len(live2) == 1, live2


if __name__ == "__main__":
    import sys
    import pytest

    sys.exit(pytest.main([__file__, "-x", "-q", "-s"]))
