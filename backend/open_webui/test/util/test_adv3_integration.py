"""ADVERSARIAL ROUND 3 — HOLISTIC-CORRECTNESS of the container-output change set.

This file owns the *integration-correctness* surface of the final patched state:
the changed_keys / merge_container_workspace_outputs rewrite of
``import_changed_container_outputs`` plus the pure ledger helper
``merge_container_outputs_state``.

Every test drives the REAL ``cw.import_changed_container_outputs`` over a REAL
temp filesystem and a faithful fake chat backend whose ledger merge delegates to
the REAL pure helper, so the test can't drift from production. The fake backend
RECORDS every ``merge_container_workspace_outputs`` call so we can assert exactly
which state-keys were persisted (the crux of the "stat-only refresh records
changed_keys" claim — if that path forgot changed_keys, the fast-path stat cache
would never warm and every quiet turn would re-hash the whole tree).

Claims under attack (from the invariant brief):

  A. A normal single-turn import RETURNS the imported list AND persists message
     files exactly once; ledger persisted exactly once with the changed key.
  B. ``merge_container_outputs_state`` survives empty / None / missing-version /
     non-dict inputs without crashing and with sane results.
  C. A STAT-ONLY refresh (same bytes, touched mtime) DOES record changed_keys —
     i.e. ``merge_container_workspace_outputs`` is CALLED with that key and the
     refreshed stat is actually persisted to the ledger — so the next turn hits
     the fast path. Imported list is empty (nothing shown), but the ledger warms.
  D. A truly-unchanged turn (stat matches) persists NOTHING (no ledger write, no
     message write) and returns []. (Confirms changed_keys stays empty so the
     `if changed_keys:` and `if imported:` guards both correctly no-op.)
  E. data_root + server_id are forwarded to the ledger persist on EVERY write
     path (new-version and stat-refresh), and the imported list is returned.

Run:
  cd backend && WEBUI_SECRET_KEY=test OAUTH_SESSION_TOKEN_ENCRYPTION_KEY=test \
    python3 -m pytest open_webui/test/util/test_adv3_integration.py -x -q
"""

import asyncio
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
# Faithful in-memory chat backend. Ledger merge uses the REAL pure helper, and
# every merge call is recorded (key set + data_root/server_id) for assertions.
# ---------------------------------------------------------------------------
class FakeChatBackend:
    def __init__(self):
        self.meta = {}
        self.messages: dict[str, dict] = {}
        # Recorded merge_container_workspace_outputs calls:
        self.merge_calls: list[dict] = []
        self.upsert_calls: list[dict] = []

    async def get_chat_by_id(self, _cid):
        obj = MagicMock()
        obj.meta = self.meta
        return obj

    async def merge_container_workspace_outputs(
        self, _cid, updates, data_root="", server_id=""
    ):
        # Record the EXACT updates the importer chose to persist.
        self.merge_calls.append(
            {
                "keys": set(updates.keys()),
                "updates": {k: dict(v) for k, v in updates.items()},
                "data_root": data_root,
                "server_id": server_id,
            }
        )
        cw_meta = dict(self.meta.get("container_workspace") or {})
        live = dict(cw_meta.get("outputs") or {})
        merged = merge_container_outputs_state(live, updates)  # REAL helper
        cw_meta["outputs"] = merged
        if data_root:
            cw_meta["data_root"] = data_root
        if server_id:
            cw_meta["server_id"] = server_id
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
        self.upsert_calls.append({"mid": mid, "partial": dict(partial)})
        msg = self.messages.setdefault(mid, {})
        if "files" in partial:
            msg["files"] = list(partial["files"])
        return None


def _patch(monkeypatch, backend, data_root):
    monkeypatch.setattr(cw, "is_container_workspace_active", lambda *a, **k: True)
    monkeypatch.setattr(
        cw, "_settings", lambda *_a, **_k: (True, str(data_root), "srv-1")
    )
    monkeypatch.setattr(cw, "_workspace_chat_id", lambda _m: "chat-1")

    async def _noop_reclaim(*a, **k):
        return None

    monkeypatch.setattr(cw, "_reclaim_outputs", _noop_reclaim)
    monkeypatch.setattr(cw.Chats, "get_chat_by_id", backend.get_chat_by_id)
    monkeypatch.setattr(
        cw.Chats,
        "merge_container_workspace_outputs",
        backend.merge_container_workspace_outputs,
    )
    monkeypatch.setattr(
        cw.Chats,
        "get_message_by_id_and_message_id",
        backend.get_message_by_id_and_message_id,
    )
    monkeypatch.setattr(
        cw.Chats,
        "upsert_message_to_chat_by_id_and_message_id",
        backend.upsert_message_to_chat_by_id_and_message_id,
    )

    # Mirror the REAL _store_output_file descriptor contract (ALWAYS
    # container_workspace{workspace_path,sha256,version}; preview only office).
    def _fake_store(
        req, usr, path, display_name, size, sha256, workspace_path,
        chat_id, message_id, version,
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
        if os.path.splitext(display_name)[1].lower() in (".docx", ".xlsx", ".pptx"):
            pid = str(uuid.uuid4())
            desc["container_workspace"]["preview_file_id"] = pid
            desc["preview_file_id"] = pid
        return desc

    async def _astore(*a, **k):
        return _fake_store(*a, **k)

    monkeypatch.setattr(cw, "_store_output_file", _astore)


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


def _meta(mid):
    return {
        "container_workspace_output_message_id": mid,
        "message_id": mid,
        "chat_id": "chat-1",
        "user_id": "user-1",
    }


def _import(request, user, mid, content=None):
    return asyncio.run(
        cw.import_changed_container_outputs(request, _meta(mid), user, content=content)
    )


# ===========================================================================
# A. Normal single-turn import returns imported and persists once.
# ===========================================================================
def test_single_turn_returns_imported_and_persists_once(monkeypatch):
    backend, request, user, workspace, outputs = _make_env(monkeypatch)
    (outputs / "report.csv").write_text("a,b,c\n1,2,3\n")

    imported = _import(request, user, "m1")

    # Returns the imported list.
    assert isinstance(imported, list) and len(imported) == 1
    assert imported[0]["name"] == "report.csv"
    assert imported[0]["container_workspace"]["version"] == 1

    # Ledger persisted EXACTLY once, with exactly the one changed state-key.
    assert len(backend.merge_calls) == 1, backend.merge_calls
    call = backend.merge_calls[0]
    assert call["keys"] == {"report.csv"}, call["keys"]
    assert call["data_root"] and call["server_id"] == "srv-1"
    # The persisted ledger entry carries the new version + last_hash.
    entry = call["updates"]["report.csv"]
    assert entry["version"] == 1 and entry.get("last_hash")

    # message.files written exactly once with one card.
    assert len(backend.upsert_calls) == 1, backend.upsert_calls
    assert len(backend.messages["m1"]["files"]) == 1


# ===========================================================================
# B. Pure helper: empty / None / missing-version / non-dict robustness.
# ===========================================================================
def test_pure_helper_handles_degenerate_inputs():
    # Empty + empty.
    assert merge_container_outputs_state({}, {}) == {}
    # None existing, None updates -> empty dict (not crash).
    assert merge_container_outputs_state(None, None) == {}
    # None existing, real update -> takes the update.
    out = merge_container_outputs_state(None, {"k": {"last_hash": "h", "version": 1}})
    assert out["k"]["version"] == 1
    # Non-dict update value is skipped (not crash).
    out = merge_container_outputs_state({}, {"k": "not-a-dict", "j": ["nope"]})
    assert out == {}
    # Missing 'version' on both sides: existing has last_hash but NO version,
    # update has different hash and no version. existing_ver=0, new_ver=0; guard
    # is `existing_ver >= new_ver` (0>=0 True) so the SAME-content branch is not
    # hit (diff hash) and the monotonic guard KEEPS existing. No crash either way.
    existing = {"k": {"last_hash": "old"}}
    out = merge_container_outputs_state(existing, {"k": {"last_hash": "new"}})
    # existing has last_hash and existing_ver(0) >= new_ver(0): keep existing.
    assert out["k"]["last_hash"] == "old"
    # But if existing has NO last_hash, monotonic guard does not fire -> take new.
    out2 = merge_container_outputs_state({"k": {}}, {"k": {"last_hash": "new"}})
    assert out2["k"]["last_hash"] == "new"
    # Same-hash refresh path: keep existing version/file_id, advance stat only.
    existing = {"k": {"last_hash": "h", "version": 3, "file_id": "F", "stat_size": 1}}
    out3 = merge_container_outputs_state(
        existing, {"k": {"last_hash": "h", "stat_size": 99, "stat_mtime_ns": 5}}
    )
    assert out3["k"]["version"] == 3 and out3["k"]["file_id"] == "F"
    assert out3["k"]["stat_size"] == 99 and out3["k"]["stat_mtime_ns"] == 5
    # version present as None on the new update (the `int(... or 0)` path).
    out4 = merge_container_outputs_state(
        {}, {"k": {"last_hash": "n", "version": None}}
    )
    assert out4["k"]["last_hash"] == "n"


# ===========================================================================
# C. Stat-only refresh DOES record changed_keys: same bytes, touched mtime.
#    The importer must CALL merge_container_workspace_outputs with that key and
#    persist the refreshed stat (warming the fast path), while importing NOTHING.
# ===========================================================================
def test_stat_only_refresh_records_changed_keys(monkeypatch):
    backend, request, user, workspace, outputs = _make_env(monkeypatch)
    target = outputs / "out.txt"
    target.write_text("stable bytes\n")

    # Turn 1: real import (version 1). Records stat in ledger.
    imported1 = _import(request, user, "m1")
    assert len(imported1) == 1
    assert len(backend.merge_calls) == 1
    ledger_after_1 = dict(backend.meta["container_workspace"]["outputs"]["out.txt"])
    assert ledger_after_1["version"] == 1
    stat_size_1 = ledger_after_1.get("stat_size")
    stat_mtime_1 = ledger_after_1.get("stat_mtime_ns")
    assert stat_size_1 is not None and stat_mtime_1 is not None

    # Now corrupt the recorded mtime so the fast path MISSES (forces a re-hash),
    # but leave the file bytes identical so the hash matches -> stat-refresh path.
    outs = dict(backend.meta["container_workspace"]["outputs"])
    e = dict(outs["out.txt"])
    e["stat_mtime_ns"] = (stat_mtime_1 or 0) + 123456  # deliberately wrong
    outs["out.txt"] = e
    cwm = dict(backend.meta["container_workspace"])
    cwm["outputs"] = outs
    backend.meta = {**backend.meta, "container_workspace": cwm}

    # Touch the file's real mtime to a NEW value (bytes unchanged) so the actual
    # current stat differs from BOTH the recorded (corrupted) value and turn-1.
    new_mt = time.time() + 10
    os.utime(target, (new_mt, new_mt))

    calls_before = len(backend.merge_calls)
    imported2 = _import(request, user, "m2")

    # Nothing IMPORTED (same content): no new card shown.
    assert imported2 == [], imported2
    # But the ledger WAS persisted (changed_keys was populated on the refresh
    # path). This is the crux: a forgotten changed_keys here would skip the merge
    # call entirely and never warm the fast path.
    assert len(backend.merge_calls) == calls_before + 1, (
        "stat-only refresh must persist the ledger (changed_keys populated)",
        backend.merge_calls,
    )
    refresh_call = backend.merge_calls[-1]
    assert refresh_call["keys"] == {"out.txt"}, refresh_call["keys"]
    # The persisted entry advanced the stat cache to the file's CURRENT stat,
    # and kept version 1 (no version bump on a same-hash refresh).
    cur_size = int(target.stat().st_size)
    cur_mtime = int(target.stat().st_mtime_ns)
    persisted = backend.meta["container_workspace"]["outputs"]["out.txt"]
    assert persisted["version"] == 1, persisted
    assert persisted["stat_size"] == cur_size, persisted
    assert persisted["stat_mtime_ns"] == cur_mtime, persisted

    # No message write happened (imported was empty).
    assert all(c["mid"] != "m2" for c in backend.upsert_calls), backend.upsert_calls

    # Next turn with the warm cache: pure fast-path skip, no merge call at all.
    calls_before2 = len(backend.merge_calls)
    imported3 = _import(request, user, "m3")
    assert imported3 == []
    assert len(backend.merge_calls) == calls_before2, (
        "warm fast path must not re-hash or persist",
        backend.merge_calls[calls_before2:],
    )


# ===========================================================================
# D. Truly-unchanged turn (stat matches) persists NOTHING and returns [].
# ===========================================================================
def test_unchanged_turn_no_persist(monkeypatch):
    backend, request, user, workspace, outputs = _make_env(monkeypatch)
    (outputs / "a.txt").write_text("hello\n")

    imported1 = _import(request, user, "m1")
    assert len(imported1) == 1
    merges_after_1 = len(backend.merge_calls)
    upserts_after_1 = len(backend.upsert_calls)

    # Re-run against a fresh message, file totally untouched: fast-path skip.
    imported2 = _import(request, user, "m2")
    assert imported2 == []
    # No ledger write (changed_keys empty) and no message write (imported empty).
    assert len(backend.merge_calls) == merges_after_1, backend.merge_calls
    assert len(backend.upsert_calls) == upserts_after_1, backend.upsert_calls


# ===========================================================================
# E. New-version path also forwards data_root/server_id and returns imported,
#    AND a sandbox-linked outputs/ file in content is imported once.
# ===========================================================================
def test_new_version_and_sandbox_link_forward_and_return(monkeypatch):
    backend, request, user, workspace, outputs = _make_env(monkeypatch)
    target = outputs / "data.txt"
    target.write_text("v1\n")

    imported1 = _import(request, user, "m1")
    assert len(imported1) == 1 and backend.merge_calls[-1]["server_id"] == "srv-1"
    assert backend.merge_calls[-1]["data_root"]

    # Modify bytes + bump mtime: a genuine new version.
    time.sleep(0.01)
    target.write_text("v2 different bytes\n")
    os.utime(target, None)
    imported2 = _import(
        request, user, "m2", content="updated sandbox:/workspace/outputs/data.txt"
    )
    assert len(imported2) == 1
    assert imported2[0]["container_workspace"]["version"] == 2
    new_call = backend.merge_calls[-1]
    assert new_call["keys"] == {"data.txt"}
    assert new_call["updates"]["data.txt"]["version"] == 2
    assert new_call["data_root"] and new_call["server_id"] == "srv-1"

    # Sandbox-linked file OUTSIDE outputs/ must NOT be smuggled in. Create an
    # inputs/ file and reference it; the importer must ignore it (outputs-only).
    (workspace / "inputs").mkdir(parents=True, exist_ok=True)
    (workspace / "inputs" / "secret.txt").write_text("should never import\n")
    merges_before = len(backend.merge_calls)
    imported3 = _import(
        request, user, "m3", content="see sandbox:/workspace/inputs/secret.txt"
    )
    assert imported3 == [], imported3
    assert len(backend.merge_calls) == merges_before, "inputs/ link must not import"
