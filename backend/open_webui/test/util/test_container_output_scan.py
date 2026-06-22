"""Tests for the incremental container-output scan + thread offload (Fix 6).

``import_changed_container_outputs`` runs at the end of every turn. Previously it
re-hashed the ENTIRE outputs tree each turn and, for office docs, ran a
synchronous LibreOffice subprocess (up to 120s) ON THE EVENT LOOP — stalling
every other chat on the worker. The fix:
  * skip re-hashing a file whose size+mtime are unchanged since last turn
    (incremental fast path; falls back to a full hash when state is absent), and
  * offload the blocking hash/store work (incl. the subprocess) to a thread.

These tests drive the real async function against a temp workspace with the heavy
collaborators (Storage/DB/reclaim) mocked, and spy on ``_hash_file`` /
``_store_output_file`` to assert the scan decisions.
"""

import asyncio
import os
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


def _drive(monkeypatch, tmp: Path, data_root: Path, meta_store: dict):
    """Run import_changed_container_outputs once with collaborators mocked.
    `meta_store["meta"]` carries the persisted chat.meta across calls so the
    incremental state survives between turns (like the real DB would)."""
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

    chat_obj = MagicMock()
    chat_obj.meta = meta_store["meta"]

    # The async runtime migration made every `Chats.*` accessor (and
    # `_store_output_file`) a coroutine; production awaits them, so the stubs
    # must be `async def`. `_hash_file` stays sync — it's driven via
    # `asyncio.to_thread`.
    async def _fake_get_chat_by_id(*_a, **_k):
        return chat_obj

    async def _fake_update_chat_meta(_cid, m):
        meta_store["meta"] = m

    async def _fake_get_message(*_a, **_k):
        return {}

    async def _fake_upsert_message(*a, **k):
        return None

    monkeypatch.setattr(cw.Chats, "get_chat_by_id", _fake_get_chat_by_id)
    monkeypatch.setattr(cw.Chats, "update_chat_meta_by_id", _fake_update_chat_meta)
    monkeypatch.setattr(
        cw.Chats, "get_message_by_id_and_message_id", _fake_get_message
    )
    monkeypatch.setattr(
        cw.Chats,
        "upsert_message_to_chat_by_id_and_message_id",
        _fake_upsert_message,
    )

    # Store step: return a stable descriptor without touching Storage/DB/subproc.
    store_calls = {"n": 0}

    async def _fake_store(req, usr, path, display_name, size, sha256, *rest):
        store_calls["n"] += 1
        return {"id": f"file-{store_calls['n']}", "name": display_name}

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)

    hash_calls = {"n": 0}
    real_hash = cw._hash_file

    def _counting_hash(path):
        hash_calls["n"] += 1
        return real_hash(path)

    monkeypatch.setattr(cw, "_hash_file", _counting_hash)

    metadata = {
        "container_workspace_output_message_id": "m1",
        "message_id": "m1",
        "chat_id": "chat-1",
    }
    # _workspace_chat_id reads chat_id from metadata; ensure it resolves.
    monkeypatch.setattr(cw, "_workspace_chat_id", lambda _m: "chat-1")

    imported = asyncio.run(
        cw.import_changed_container_outputs(request, metadata, user)
    )
    return imported, store_calls, hash_calls


def test_new_file_imports_then_unchanged_skips_rehash(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    f = outputs / "report.txt"
    f.write_text("hello world")

    meta_store = {"meta": {}}

    # Turn 1: new file → imported, hashed once, stored once.
    imported1, store1, hash1 = _drive(monkeypatch, tmp, data_root, meta_store)
    assert len(imported1) == 1
    assert store1["n"] == 1
    assert hash1["n"] == 1

    # Turn 2: file untouched (same size+mtime) → skipped WITHOUT re-hashing.
    imported2, store2, hash2 = _drive(monkeypatch, tmp, data_root, meta_store)
    assert imported2 == []
    assert store2["n"] == 0
    assert hash2["n"] == 0  # incremental fast path: no re-hash


def test_modified_file_reimports(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    f = outputs / "data.csv"
    f.write_text("a,b,c")

    meta_store = {"meta": {}}
    imported1, store1, _ = _drive(monkeypatch, tmp, data_root, meta_store)
    assert len(imported1) == 1

    # Modify the file (new content + bump mtime so the stat fast path misses).
    f.write_text("a,b,c,d,e,f")
    os.utime(f, (f.stat().st_atime + 5, f.stat().st_mtime + 5))

    imported2, store2, hash2 = _drive(monkeypatch, tmp, data_root, meta_store)
    assert len(imported2) == 1  # re-imported as a new version
    assert hash2["n"] == 1  # changed stat → did re-hash
