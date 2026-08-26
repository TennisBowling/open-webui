"""Regression: container output-file duplication via a lost-update race on the
dedup ledger MUST NOT surface a duplicate card.

The dedup ledger that prevents a given outputs file from being re-imported every
turn lives in chat.meta["container_workspace"]["outputs"]. Before the fix,
import_changed_container_outputs did a read-modify-write with NO lock and a full
meta replace; two imports for the SAME chat_id + message_id (the real case: a
fanout rerun importing to the parent message — utils/subagent.py:311-313,1683)
both read the empty ledger, both imported the same on-disk file as a brand-new
descriptor (fresh uuid), and the merge (id-only) kept both → the user saw the
SAME file twice, one of them an orphaned "dead twin".

The fix is twofold and BOTH are exercised here:
  1. _merge_files dedups by content identity (workspace_path + sha256), so two
     fresh-uuid descriptors for the same content collapse to one card.
  2. the ledger is persisted via Chats.merge_container_workspace_outputs, which
     merges under a row lock (first-writer-wins for an identical hash) instead of
     clobbering — so the ledger stays consistent with the single surviving card.

This test drives the REAL import twice concurrently against one shared meta_store
+ message "files" list and asserts NO duplicate survives.

Run:
  cd backend && python -m pytest open_webui/test/util/test_repro_dup_ledger.py -x -q
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


def _merge_outputs_like_db(meta_store: dict, outputs_updates: dict, data_root, server_id):
    """Mirror Chats.merge_container_workspace_outputs: re-read the live ledger and
    merge only the given keys, first-writer-wins for an identical last_hash."""
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
    if data_root:
        cw_meta["data_root"] = data_root
    if server_id:
        cw_meta["server_id"] = server_id
    meta["container_workspace"] = cw_meta
    meta_store["meta"] = meta


def _patch_collaborators(monkeypatch, data_root: Path, meta_store: dict, message_store: dict):
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
    monkeypatch.setattr(
        cw.Chats, "merge_container_workspace_outputs", _fake_merge_outputs
    )
    monkeypatch.setattr(cw.Chats, "get_message_by_id_and_message_id", _fake_get_message)
    monkeypatch.setattr(
        cw.Chats, "upsert_message_to_chat_by_id_and_message_id", _fake_upsert_message
    )

    # Mirror _store_output_file: fresh uuid each call, but a realistic descriptor
    # that carries the container_workspace block (workspace_path + sha256) the
    # content-identity dedup keys on. A gate forces the two imports to interleave.
    store_gate = {"hold": None}

    async def _fake_store(req, usr, path, display_name, size, sha256,
                          workspace_path, chat_id, message_id, version):
        import uuid

        gate = store_gate["hold"]
        if gate is not None:
            await gate
        return {
            "type": "file",
            "id": str(uuid.uuid4()),
            "name": display_name,
            "url": f"/api/v1/files/{uuid.uuid4()}/content",
            "size": size,
            "container_workspace": {
                "workspace_path": workspace_path,
                "sha256": sha256,
                "version": version,
            },
        }

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)
    return store_gate


def test_concurrent_imports_do_not_duplicate(monkeypatch):
    """Two overlapping imports (same chat + message_id) on a never-before-seen
    outputs file → exactly ONE 'one.txt' card survives, and the ledger records one
    consistent version (no orphaned dead twin)."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    (outputs / "one.txt").write_text("the one and only output")

    meta_store = {"meta": {}}
    message_store = {"files": []}
    store_gate = _patch_collaborators(monkeypatch, data_root, meta_store, message_store)

    metadata_a = {
        "container_workspace_output_message_id": "m1",
        "message_id": "m1",
        "chat_id": "chat-1",
    }
    metadata_b = dict(metadata_a)

    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    async def _run():
        loop = asyncio.get_event_loop()
        release = loop.create_future()
        store_gate["hold"] = release

        task_a = asyncio.create_task(
            cw.import_changed_container_outputs(request, metadata_a, user)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # B runs fully first (reads the SAME still-empty ledger A read).
        store_gate["hold"] = None
        imported_b = await cw.import_changed_container_outputs(
            request, metadata_b, user
        )

        release.set_result(None)
        imported_a = await task_a
        return imported_a, imported_b

    imported_a, imported_b = asyncio.run(_run())

    # Each import still believed it had a new file (the race is real)...
    assert len(imported_a) == 1, imported_a
    assert len(imported_b) == 1, imported_b

    # ...but the MERGED message shows the file exactly ONCE (content-identity
    # dedup collapses the two fresh-uuid descriptors for identical content).
    final_files = message_store["files"]
    one_txt = [f for f in final_files if f.get("name") == "one.txt"]
    assert len(one_txt) == 1, f"duplicate survived into message: {final_files}"

    # The ledger records exactly one version and it is internally consistent.
    ledger = meta_store["meta"]["container_workspace"]["outputs"]
    assert list(ledger.keys()) == ["one.txt"], ledger
    versions = ledger["one.txt"]["versions"]
    assert len(versions) == 1, versions


def test_sequential_unchanged_reimport_is_noop(monkeypatch):
    """Control: a second import of the same unchanged file imports nothing and never
    duplicates (the incremental fast path / ledger still works post-fix)."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    (outputs / "one.txt").write_text("stable content")

    meta_store = {"meta": {}}
    message_store = {"files": []}
    _patch_collaborators(monkeypatch, data_root, meta_store, message_store)

    metadata = {
        "container_workspace_output_message_id": "m1",
        "message_id": "m1",
        "chat_id": "chat-1",
    }
    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    imported1 = asyncio.run(
        cw.import_changed_container_outputs(request, metadata, user)
    )
    imported2 = asyncio.run(
        cw.import_changed_container_outputs(request, metadata, user)
    )
    assert len(imported1) == 1
    assert imported2 == []
    assert len([f for f in message_store["files"] if f.get("name") == "one.txt"]) == 1
