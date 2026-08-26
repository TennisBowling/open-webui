"""Regression: no state-key collision / duplication across the two candidate scans.

import_changed_container_outputs builds candidates from two scans: _output_files
(state_key relative to outputs/) and _sandbox_linked_files (now confined to the
outputs/ subtree). Before the fix, a sandbox link to a workspace-ROOT file with
the same basename as an outputs file mapped both to the same ledger key
("one.txt"), corrupting version history and re-importing every turn. Confining the
sandbox scan to outputs/ removes the only path that produced a non-outputs
candidate, so the collision can no longer form.

Run:
  cd backend && python -m pytest open_webui/test/util/test_repro_dup_statekey.py -x -q
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


def _drive(monkeypatch, data_root: Path, meta_store: dict, *, content=None,
           content_blocks=None):
    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    monkeypatch.setattr(cw, "is_container_workspace_active", lambda *a, **k: True)
    monkeypatch.setattr(
        cw, "_settings", lambda *_a, **_k: (MagicMock(), str(data_root), "srv-1")
    )

    async def _noop_reclaim(*a, **k):
        return None

    monkeypatch.setattr(cw, "_reclaim_outputs", _noop_reclaim)

    msg_store = meta_store.setdefault("msg", {"files": []})

    async def _fake_get_chat_by_id(*_a, **_k):
        obj = MagicMock()
        obj.meta = meta_store["meta"]
        return obj

    async def _fake_merge_outputs(_cid, outputs_updates, data_root="", server_id=""):
        meta = dict(meta_store["meta"])
        cw_meta = dict(meta.get("container_workspace") or {})
        outputs = dict(cw_meta.get("outputs") or {})
        for key, new_state in outputs_updates.items():
            existing = outputs.get(key)
            if (
                isinstance(existing, dict)
                and existing.get("last_hash")
                and existing.get("last_hash") == (new_state or {}).get("last_hash")
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
        return True

    async def _fake_get_message(*_a, **_k):
        return dict(msg_store)

    async def _fake_upsert_message(_cid, _mid, patch, **k):
        msg_store.update(patch)

    monkeypatch.setattr(cw.Chats, "get_chat_by_id", _fake_get_chat_by_id)
    monkeypatch.setattr(
        cw.Chats, "merge_container_workspace_outputs", _fake_merge_outputs
    )
    monkeypatch.setattr(cw.Chats, "get_message_by_id_and_message_id", _fake_get_message)
    monkeypatch.setattr(
        cw.Chats, "upsert_message_to_chat_by_id_and_message_id", _fake_upsert_message
    )

    store_calls = {"n": 0, "names": []}

    async def _fake_store(req, usr, path, display_name, size, sha256,
                          workspace_path, chat_id, message_id, version):
        store_calls["n"] += 1
        store_calls["names"].append(display_name)
        return {
            "id": f"file-{store_calls['n']}",
            "name": display_name,
            "type": "file",
            "url": f"/api/v1/files/file-{store_calls['n']}/content",
            "container_workspace": {
                "workspace_path": workspace_path,
                "sha256": sha256,
                "version": version,
            },
        }

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)

    metadata = {
        "container_workspace_output_message_id": "m1",
        "message_id": "m1",
        "chat_id": "chat-1",
    }
    monkeypatch.setattr(cw, "_workspace_chat_id", lambda _m: "chat-1")

    imported = asyncio.run(
        cw.import_changed_container_outputs(
            request, metadata, user, content=content, content_blocks=content_blocks
        )
    )
    return imported, store_calls, msg_store


def test_sandbox_link_to_outputs_file_is_deduped(monkeypatch):
    """A sandbox link pointing at the SAME outputs/ file the outputs scan already
    found is deduped by resolved path → exactly ONE import."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    (outputs / "one.txt").write_text("A")

    content = "Here is your file sandbox:/workspace/outputs/one.txt"
    meta_store = {"meta": {}}
    imported, store, msg = _drive(monkeypatch, data_root, meta_store, content=content)

    assert len(imported) == 1, f"expected 1, got {len(imported)}"
    assert len(msg["files"]) == 1


def test_no_collision_workspace_root_is_confined(monkeypatch):
    """outputs/one.txt ("A") AND a DIFFERENT workspace-root one.txt ("B") referenced
    via a sandbox link: the root file is OUTSIDE outputs/ and must be rejected, so
    only ONE file imports and the ledger has exactly one clean key."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    (outputs / "one.txt").write_text("A")
    (workspace / "one.txt").write_text("BBBBB")

    content = "modified root sandbox:/workspace/one.txt"
    meta_store = {"meta": {}}
    imported, store, msg = _drive(monkeypatch, data_root, meta_store, content=content)

    # Only the outputs/ file imports; the root one.txt is confined out.
    assert len(imported) == 1, f"expected 1 import, got {len(imported)}: {store['names']}"
    state = meta_store["meta"]["container_workspace"]["outputs"]
    assert list(state.keys()) == ["one.txt"], state.keys()
    versions = state["one.txt"]["versions"]
    assert len(versions) == 1, versions
    assert state["one.txt"]["workspace_path"] == "outputs/one.txt"


def test_confined_no_reimport_across_turns(monkeypatch):
    """Turn 1 imports outputs/one.txt. Turn 2 it is UNCHANGED but a workspace-root
    one.txt ("B") appears and is referenced — B is confined out, and the unchanged
    outputs file is NOT re-imported. Turn 3 (nothing changed) imports nothing."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    (outputs / "one.txt").write_text("A")

    meta_store = {"meta": {}}
    imported1, store1, msg1 = _drive(monkeypatch, data_root, meta_store)
    assert len(imported1) == 1

    (workspace / "one.txt").write_text("BBBBB")
    content = "root sandbox:/workspace/one.txt"
    imported2, store2, msg2 = _drive(monkeypatch, data_root, meta_store, content=content)
    # B is confined out; outputs/one.txt unchanged → nothing new imports.
    assert imported2 == [], f"unexpected re-import: {store2['names']}"

    imported3, store3, msg3 = _drive(monkeypatch, data_root, meta_store)
    assert imported3 == [], f"unexpected re-import turn 3: {store3['names']}"

    # Across all turns the file appears exactly once in the message.
    one_txt = [f for f in msg3["files"] if f.get("name") == "one.txt"]
    assert len(one_txt) == 1, msg3["files"]
