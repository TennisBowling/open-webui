"""Adversarial verification of the LEDGER invariant for container output import.

INVARIANT: the ledger persisted via merge_container_workspace_outputs
  (a) keeps the incremental fast path working so an UNCHANGED file is never
      re-imported on a later turn;
  (b) records a genuine NEW version (changed content) so its descriptor is
      produced and named distinctly (one_1.txt) — NOT silently dropped by the
      first-writer-wins clause (which only applies to IDENTICAL last_hash);
  (c) under concurrency converges without leaving an orphan that causes a
      future-turn re-import.

Each test drives the REAL cw.import_changed_container_outputs and mirrors
chats.py merge_container_workspace_outputs EXACTLY in the mock (copied
semantics). _hash_file is wrapped to count re-hashes (detect a perpetual
re-hash loop / stat-cache failing to advance).

The concurrency test (c) makes asyncio.to_thread run INLINE and stubs
_stat_file/_hash_file off an injected "disk view" so the interleaving is
DETERMINISTIC (the real thread pool makes the A-vs-B hash timing a coin flip,
which is exactly what makes a flaky re-import hard to see in production).

Run:
  cd backend && WEBUI_SECRET_KEY=test OAUTH_SESSION_TOKEN_ENCRYPTION_KEY=test \
    python3 -m pytest open_webui/test/util/test_adv_ledger.py -x -q
"""

import asyncio
import hashlib
import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

from test.util.db import configure_test_database

configure_test_database()

import open_webui.utils.container_workspace as cw  # noqa: E402


# ---------------------------------------------------------------------------
# Use the REAL pure merge core (models/chats.py merge_container_outputs_state) so
# this mock can never drift from production semantics.
# ---------------------------------------------------------------------------
from open_webui.models.chats import merge_container_outputs_state  # noqa: E402


def _merge_outputs_like_db(meta_store, outputs_updates, data_root, server_id):
    meta = dict(meta_store["meta"])
    cw_meta = dict(meta.get("container_workspace") or {})
    cw_meta["outputs"] = merge_container_outputs_state(
        cw_meta.get("outputs") or {}, outputs_updates
    )
    if data_root:
        cw_meta["data_root"] = data_root
    if server_id:
        cw_meta["server_id"] = server_id
    meta["container_workspace"] = cw_meta
    meta_store["meta"] = meta


def _setup_workspace(tmp: Path):
    data_root = tmp / "containers"
    workspace = data_root / "chat-1" / "workspace"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    return data_root, workspace, outputs


def _base_patch(monkeypatch, data_root, meta_store, message_store):
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


def _md():
    return {
        "container_workspace_output_message_id": "m1",
        "message_id": "m1",
        "chat_id": "chat-1",
    }


# ===========================================================================
# (a) + (b): unchanged → fast path; changed → genuine NEW version, distinct name
# ===========================================================================
def test_unchanged_then_changed_new_version_distinct_name(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    f = outputs / "one.txt"
    f.write_text("v1 content")

    meta_store = {"meta": {}}
    message_store = {"files": []}
    hash_counter = {}
    _base_patch(monkeypatch, data_root, meta_store, message_store)

    real_hash = cw._hash_file

    def _counting_hash(path):
        hash_counter[str(path)] = hash_counter.get(str(path), 0) + 1
        return real_hash(path)

    monkeypatch.setattr(cw, "_hash_file", _counting_hash)

    async def _fake_store(req, usr, path, dn, size, sha, wp, cid, mid, ver):
        return {
            "type": "file", "id": str(uuid.uuid4()), "name": dn,
            "url": f"/api/v1/files/{uuid.uuid4()}/content", "size": size,
            "container_workspace": {"workspace_path": wp, "sha256": sha, "version": ver},
        }

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)

    request, user = MagicMock(), MagicMock()
    user.id = "user-1"

    imported1 = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert len(imported1) == 1 and imported1[0]["name"] == "one.txt", imported1

    imported2 = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert imported2 == [], f"unchanged file re-imported: {imported2}"

    f.write_text("v2 DIFFERENT content")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    imported3 = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert len(imported3) == 1, f"changed content not imported: {imported3}"
    assert imported3[0]["name"] == "one_1.txt", (
        f"new version must get a DISTINCT display name, got {imported3[0]['name']}"
    )

    ledger = meta_store["meta"]["container_workspace"]["outputs"]["one.txt"]
    assert ledger["version"] == 2, ledger
    assert [v["display_name"] for v in ledger["versions"]] == ["one.txt", "one_1.txt"]

    one_cards = [x for x in message_store["files"] if x.get("name") in ("one.txt", "one_1.txt")]
    assert len(one_cards) == 2, message_store["files"]

    imported4 = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert imported4 == [], f"v2 re-imported on stable turn: {imported4}"


# ===========================================================================
# (a) stat-refresh: touch mtime only (same bytes) → no re-import; first-writer-
# wins stat advance must warm the cache so the NEXT turn does NOT re-hash.
# ===========================================================================
def test_stat_refresh_advances_cache_no_perpetual_rehash(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    f = outputs / "doc.txt"
    f.write_text("stable bytes")

    meta_store = {"meta": {}}
    message_store = {"files": []}
    hash_counter = {}
    _base_patch(monkeypatch, data_root, meta_store, message_store)

    real_hash = cw._hash_file

    def _counting_hash(path):
        hash_counter[str(path)] = hash_counter.get(str(path), 0) + 1
        return real_hash(path)

    monkeypatch.setattr(cw, "_hash_file", _counting_hash)

    async def _fake_store(req, usr, path, dn, size, sha, wp, cid, mid, ver):
        return {
            "type": "file", "id": str(uuid.uuid4()), "name": dn,
            "url": "u", "size": size,
            "container_workspace": {"workspace_path": wp, "sha256": sha, "version": ver},
        }

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)

    request, user = MagicMock(), MagicMock()
    user.id = "user-1"
    key = str(f.resolve())

    asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert hash_counter.get(key) == 1, hash_counter

    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
    imported2 = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert imported2 == [], imported2
    assert hash_counter.get(key) == 2, hash_counter

    imported3 = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert imported3 == [], imported3
    assert hash_counter.get(key) == 2, (
        f"stat cache did NOT advance — perpetual re-hash: {hash_counter}"
    )
    assert len([x for x in message_store["files"] if x.get("name") == "doc.txt"]) == 1


# ===========================================================================
# (c) DETERMINISTIC concurrent imports of the SAME path with DIFFERENT content,
# where the import that read the OLDER content commits its ledger entry LAST.
#
# Interleaving (forced via inline to_thread + injected disk view + a gate):
#   A reads disk=AAAA, hashes AAAA, then parks in _store_output_file.
#   B reads disk=BBBB, hashes BBBB, stores, merges ledger -> last_hash=BBBB.
#   A unblocks, stores AAAA, merges. first-writer-wins compares live BBBB vs
#   A's AAAA -> MISS -> takes A's STALE state (last_hash=AAAA, stat for AAAA).
# Disk is now stably BBBB. Invariant (c) requires the ledger to converge so the
# NEXT (stable) turn does NOT re-import. Assert exactly that.
# ===========================================================================
def test_concurrent_stale_writer_last_no_future_reimport(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    f = outputs / "race.txt"
    f.write_text("seed")  # make the real _output_files rglob see the path

    meta_store = {"meta": {}}
    message_store = {"files": []}
    _base_patch(monkeypatch, data_root, meta_store, message_store)

    A = hashlib.sha256(b"AAAA").hexdigest()
    B = hashlib.sha256(b"BBBB").hexdigest()

    # Injected disk view: _stat_file/_hash_file read THIS, not the real bytes,
    # so we control exactly what each import "sees".
    disk = {"content": "seed", "mtime": 1}
    monkeypatch.setattr(cw, "_stat_file", lambda p: (len(disk["content"]), disk["mtime"]))
    monkeypatch.setattr(
        cw, "_hash_file",
        lambda p: (len(disk["content"]), hashlib.sha256(disk["content"].encode()).hexdigest()),
    )

    # Run to_thread inline so the A/B interleave is fully deterministic (no real
    # thread-pool scheduling). Each call captures the disk view at its call site.
    async def _inline_to_thread(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(cw.asyncio, "to_thread", _inline_to_thread)

    gate_a = {"f": None}

    async def _fake_store(req, usr, path, dn, size, sha, wp, cid, mid, ver):
        if sha == A and gate_a["f"] is not None:
            await gate_a["f"]
        return {
            "type": "file", "id": str(uuid.uuid4()), "name": dn, "url": "u",
            "size": size,
            "container_workspace": {"workspace_path": wp, "sha256": sha, "version": ver},
        }

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)

    request, user = MagicMock(), MagicMock()
    user.id = "user-1"

    async def _run():
        loop = asyncio.get_event_loop()
        gate_a["f"] = loop.create_future()

        disk["content"], disk["mtime"] = "AAAA", 1000
        task_a = asyncio.create_task(
            cw.import_changed_container_outputs(request, _md(), user)
        )
        # Pump until A is parked inside _store_output_file (gate not yet resolved).
        for _ in range(20):
            await asyncio.sleep(0)

        # Genuine concurrent edit: disk becomes BBBB; B imports it fully.
        disk["content"], disk["mtime"] = "BBBB", 2000
        imported_b = await cw.import_changed_container_outputs(request, _md(), user)

        # A commits LAST with its stale AAAA snapshot.
        gate_a["f"].set_result(None)
        imported_a = await task_a
        return imported_a, imported_b

    imported_a, imported_b = asyncio.run(_run())

    assert len(imported_a) == 1 and imported_a[0]["container_workspace"]["sha256"] == A
    assert len(imported_b) == 1 and imported_b[0]["container_workspace"]["sha256"] == B

    ledger = meta_store["meta"]["container_workspace"]["outputs"]["race.txt"]

    # The decisive check: disk is now stable at BBBB/mtime 2000. A future turn
    # MUST NOT re-import. If A's stale write left last_hash=AAAA + stat for the
    # AAAA snapshot, the stat fast path misses (mtime differs) AND the content
    # hash differs from the ledger, so BBBB is imported AGAIN — a phantom new
    # version of a file the user already has.
    disk["content"], disk["mtime"] = "BBBB", 2000
    imported_next = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert imported_next == [], (
        "stale concurrent writer-last left an inconsistent ledger -> re-import "
        f"next turn: imported={[x['container_workspace']['sha256'][:8] for x in imported_next]} "
        f"ledger last_hash={ledger.get('last_hash', '')[:8]} "
        f"stat_mtime_ns={ledger.get('stat_mtime_ns')} disk_mtime=2000"
    )
