"""ROUND-2 adversarial verification of the LEDGER-MONOTONIC invariant.

The round-1 fix (V4) added a monotonic guard to merge_container_outputs_state:

    if existing.last_hash present AND existing.version >= new.version
       AND different hash:  SKIP (keep existing)

My job: prove this guard NEVER wrongly rejects a legitimate update, and that
the fast path / new-version behavior is intact. I attack the guard from five
angles, using the REAL pure helper (so the test can't drift from production):

    from open_webui.models.chats import merge_container_outputs_state

and, where the question is "what version does import actually emit", the REAL
cw.import_changed_container_outputs (mocked exactly like test_adv_ledger.py /
chats.py merge_container_workspace_outputs).

Run:
  cd backend && WEBUI_SECRET_KEY=test OAUTH_SESSION_TOKEN_ENCRYPTION_KEY=test \
    python3 -m pytest open_webui/test/util/test_adv2_ledger.py -x -q
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
from open_webui.models.chats import merge_container_outputs_state  # noqa: E402


# ---------------------------------------------------------------------------
# Harness mirroring chats.py merge_container_workspace_outputs EXACTLY by
# delegating to the REAL pure helper (copied from test_adv_ledger.py).
# ---------------------------------------------------------------------------
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


def _state(version, last_hash, **extra):
    """Build a ledger entry the way import_changed_container_outputs writes one."""
    s = {
        "workspace_path": "outputs/one.txt",
        "last_hash": last_hash,
        "version": version,
        "versions": [{"version": version, "sha256": last_hash}],
        "stat_size": extra.pop("stat_size", 10),
        "stat_mtime_ns": extra.pop("stat_mtime_ns", 1000),
    }
    s.update(extra)
    return s


# ===========================================================================
# ATTACK 1 (PURE): strictly sequential v1->v2->v3, distinct hashes. The guard
# must NEVER fire (existing.version < new.version each step). Ledger ends v3.
# ===========================================================================
def test_pure_sequential_versions_never_suppressed():
    hA, hB, hC = "aaaa", "bbbb", "cccc"
    ledger = {}
    ledger = merge_container_outputs_state(ledger, {"k": _state(1, hA)})
    assert ledger["k"]["version"] == 1 and ledger["k"]["last_hash"] == hA

    ledger = merge_container_outputs_state(ledger, {"k": _state(2, hB)})
    assert ledger["k"]["version"] == 2 and ledger["k"]["last_hash"] == hB, ledger

    ledger = merge_container_outputs_state(ledger, {"k": _state(3, hC)})
    assert ledger["k"]["version"] == 3 and ledger["k"]["last_hash"] == hC, ledger


# ===========================================================================
# ATTACK 2 (PURE): the version=0 corner. Could a legitimate changed-content
# update with new.version==0 be wrongly skipped because existing.version(1)>=0?
#
# First, prove import NEVER emits version 0 for a changed file (it computes
# int(state.version or 0)+1 >= 1). Second, document that the helper, if FED a
# 0-version update against an existing v1 with a DIFFERENT hash, DOES skip it —
# and prove that's unreachable from import (so not a live bug), while a 0 vs
# EMPTY existing (no last_hash) is still taken (genuine first insert).
# ===========================================================================
def test_pure_version_zero_only_skipped_when_unreachable_from_import():
    # First insert with no prior state: version 0 with content is still TAKEN
    # (guard requires existing.last_hash present).
    ledger = merge_container_outputs_state({}, {"k": {"last_hash": "x", "version": 0}})
    assert ledger["k"]["last_hash"] == "x", "first insert must always land"

    # A 0-version DIFFERENT-content update vs an existing v1 IS skipped by the
    # guard. This is only safe because import never produces it; assert that.
    ledger2 = merge_container_outputs_state(
        {"k": _state(1, "aaaa")}, {"k": {"last_hash": "zzzz", "version": 0}}
    )
    assert ledger2["k"]["last_hash"] == "aaaa", "guard skips ver0-vs-ver1 diff hash"
    # -> the import-path proof that ver0 is unreachable is in ATTACK 4 below
    # (every import emits >=1). So this skip can never harm a real update.


# ===========================================================================
# ATTACK 3 (REAL import): stat-refresh then change.
#   v1(hashA, stat1) -> touch mtime, same bytes (hashA, stat2): keep v1, advance
#   stat -> then real content change (hashB): must record v2.
# Drive through the REAL import + REAL helper. Assert the full sequence.
# ===========================================================================
def test_real_stat_refresh_then_change_records_v2(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    f = outputs / "one.txt"
    f.write_text("AAAA")

    meta_store = {"meta": {}}
    message_store = {"files": []}
    _base_patch(monkeypatch, data_root, meta_store, message_store)

    async def _fake_store(req, usr, path, dn, size, sha, wp, cid, mid, ver):
        return {
            "type": "file", "id": str(uuid.uuid4()), "name": dn, "url": "u",
            "size": size,
            "container_workspace": {"workspace_path": wp, "sha256": sha, "version": ver},
        }

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)

    request, user = MagicMock(), MagicMock()
    user.id = "user-1"
    key = "one.txt"

    # v1
    imp1 = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert len(imp1) == 1
    led = meta_store["meta"]["container_workspace"]["outputs"][key]
    assert led["version"] == 1, led
    v1_stat_mtime = led["stat_mtime_ns"]

    # stat-refresh: touch mtime, identical bytes -> keep v1, advance stat cache
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
    imp2 = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert imp2 == [], f"identical bytes re-imported: {imp2}"
    led = meta_store["meta"]["container_workspace"]["outputs"][key]
    assert led["version"] == 1, "stat refresh must not bump version"
    assert led["last_hash"] == hashlib.sha256(b"AAAA").hexdigest()
    assert led["stat_mtime_ns"] != v1_stat_mtime, "stat cache did NOT advance"

    # genuine content change -> v2 recorded
    f.write_text("BBBB DIFFERENT")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
    imp3 = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert len(imp3) == 1, f"changed content after stat refresh not imported: {imp3}"
    led = meta_store["meta"]["container_workspace"]["outputs"][key]
    assert led["version"] == 2, led
    assert led["last_hash"] == hashlib.sha256(b"BBBB DIFFERENT").hexdigest()

    # stable turn: no re-import
    imp4 = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert imp4 == [], f"v2 re-imported on stable turn: {imp4}"


# ===========================================================================
# ATTACK 4 (REAL import): prove version emitted is ALWAYS >= 1, and after the
# stat-refresh keep-v1 the NEXT change emits v2 (not v1) — i.e. the version
# counter is read from the PERSISTED ledger, so the guard's new>existing
# precondition holds for every legitimate sequential change.
#
# This nails the ATTACK-2 claim: import never emits version 0.
# ===========================================================================
def test_real_import_version_monotonic_from_persisted_ledger(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    f = outputs / "one.txt"
    f.write_text("c0")

    meta_store = {"meta": {}}
    message_store = {"files": []}
    _base_patch(monkeypatch, data_root, meta_store, message_store)

    seen_versions = []

    async def _fake_store(req, usr, path, dn, size, sha, wp, cid, mid, ver):
        seen_versions.append(ver)
        return {
            "type": "file", "id": str(uuid.uuid4()), "name": dn, "url": "u",
            "size": size,
            "container_workspace": {"workspace_path": wp, "sha256": sha, "version": ver},
        }

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)
    request, user = MagicMock(), MagicMock()
    user.id = "user-1"

    for i in range(1, 5):
        f.write_text(f"content-{i}-XYZ")
        st = f.stat()
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + i * 9_000_000_000))
        asyncio.run(cw.import_changed_container_outputs(request, _md(), user))

    # Each real change emitted a strictly increasing version starting at 1.
    assert seen_versions == [1, 2, 3, 4], seen_versions
    assert all(v >= 1 for v in seen_versions), "import must never emit version 0"
    led = meta_store["meta"]["container_workspace"]["outputs"]["one.txt"]
    assert led["version"] == 4, led


# ===========================================================================
# ATTACK 5 (REAL import, DETERMINISTIC race): the V4 case — two writers, the
# stale writer commits LAST with an EQUAL version and DIFFERENT content. The
# guard must keep the earlier-committed (live) content and the next stable turn
# must NOT re-import. This re-proves V4 stays fixed via the REAL helper.
#
# Critically also asserts the EQUAL-version path is exercised: both A and B
# compute version 1 off the empty ledger, so the guard's `existing_ver >=
# new_ver` is 1 >= 1 (the boundary that, if it were `>`, would let A clobber B).
# ===========================================================================
def test_real_concurrent_equal_version_stale_last_no_reimport(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    f = outputs / "race.txt"
    f.write_text("seed")

    meta_store = {"meta": {}}
    message_store = {"files": []}
    _base_patch(monkeypatch, data_root, meta_store, message_store)

    A = hashlib.sha256(b"AAAA").hexdigest()
    B = hashlib.sha256(b"BBBB").hexdigest()

    disk = {"content": "seed", "mtime": 1}
    monkeypatch.setattr(cw, "_stat_file", lambda p: (len(disk["content"]), disk["mtime"]))
    monkeypatch.setattr(
        cw, "_hash_file",
        lambda p: (len(disk["content"]), hashlib.sha256(disk["content"].encode()).hexdigest()),
    )

    async def _inline_to_thread(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(cw.asyncio, "to_thread", _inline_to_thread)

    gate_a = {"f": None}
    emitted_versions = {}

    async def _fake_store(req, usr, path, dn, size, sha, wp, cid, mid, ver):
        emitted_versions[sha] = ver
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
        for _ in range(20):
            await asyncio.sleep(0)
        disk["content"], disk["mtime"] = "BBBB", 2000
        imported_b = await cw.import_changed_container_outputs(request, _md(), user)
        gate_a["f"].set_result(None)
        imported_a = await task_a
        return imported_a, imported_b

    imported_a, imported_b = asyncio.run(_run())
    assert len(imported_a) == 1 and imported_a[0]["container_workspace"]["sha256"] == A
    assert len(imported_b) == 1 and imported_b[0]["container_workspace"]["sha256"] == B

    # Both computed version 1 off the empty ledger -> the guard's EQUAL-version
    # boundary (1 >= 1) is what rejects the stale A write.
    assert emitted_versions[A] == 1 and emitted_versions[B] == 1, emitted_versions

    ledger = meta_store["meta"]["container_workspace"]["outputs"]["race.txt"]
    # B committed first with version 1/last_hash B; A's stale version-1 write is
    # rejected by the monotonic guard (1 >= 1, different hash).
    assert ledger["last_hash"] == B, (
        f"stale equal-version writer clobbered live content: {ledger['last_hash'][:8]}"
    )

    disk["content"], disk["mtime"] = "BBBB", 2000
    imported_next = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert imported_next == [], (
        "V4 regressed: stale writer-last left ledger inconsistent -> re-import. "
        f"ledger last_hash={ledger.get('last_hash','')[:8]} "
        f"stat_mtime_ns={ledger.get('stat_mtime_ns')} disk_mtime=2000"
    )


# ===========================================================================
# ATTACK 6 (REAL import, LIVELOCK self-heal): could the guard PERMANENTLY
# suppress a genuine new version across turns? Worst case:
#   - Live ledger is at version V (last_hash=H_live) because a racing writer
#     committed it.
#   - This worker's local snapshot is STALE at version V-1, so it computes a
#     new update at version V with a DIFFERENT hash H_stale. Guard: V >= V ->
#     SKIP. The update is dropped THIS turn.
# Self-heal requirement: on the NEXT turn the worker re-reads the now-live
# ledger (version V), recomputes version = V+1 > V, and the guard passes.
# Construct it with the REAL import driving two interleaved writers, then a
# clean follow-up turn, and assert the file converges (no perpetual drop).
# ===========================================================================
def test_real_guard_self_heals_no_permanent_suppression(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    f = outputs / "live.txt"
    f.write_text("seed")

    meta_store = {"meta": {}}
    message_store = {"files": []}
    _base_patch(monkeypatch, data_root, meta_store, message_store)

    H1 = hashlib.sha256(b"V1").hexdigest()
    HA = hashlib.sha256(b"VA").hexdigest()  # stale worker's content
    HB = hashlib.sha256(b"VB").hexdigest()  # racing winner's content
    HC = hashlib.sha256(b"VC").hexdigest()  # the eventual stable content

    disk = {"content": "seed", "mtime": 1}
    monkeypatch.setattr(cw, "_stat_file", lambda p: (len(disk["content"]), disk["mtime"]))
    monkeypatch.setattr(
        cw, "_hash_file",
        lambda p: (len(disk["content"]), hashlib.sha256(disk["content"].encode()).hexdigest()),
    )

    async def _inline_to_thread(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(cw.asyncio, "to_thread", _inline_to_thread)

    gate = {"f": None}

    async def _fake_store(req, usr, path, dn, size, sha, wp, cid, mid, ver):
        if sha == HA and gate["f"] is not None:
            await gate["f"]
        return {
            "type": "file", "id": str(uuid.uuid4()), "name": dn, "url": "u",
            "size": size,
            "container_workspace": {"workspace_path": wp, "sha256": sha, "version": ver},
        }

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)
    request, user = MagicMock(), MagicMock()
    user.id = "user-1"

    # Establish version 1 (H1) cleanly so the next race is at the V>=V boundary
    # with V=2 (more realistic than V=1).
    disk["content"], disk["mtime"] = "V1", 100
    asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert meta_store["meta"]["container_workspace"]["outputs"]["live.txt"]["version"] == 1

    async def _race():
        loop = asyncio.get_event_loop()
        gate["f"] = loop.create_future()
        # Stale worker A reads VA (would be version 2), parks in store.
        disk["content"], disk["mtime"] = "VA", 200
        task_a = asyncio.create_task(
            cw.import_changed_container_outputs(request, _md(), user)
        )
        for _ in range(20):
            await asyncio.sleep(0)
        # Racing winner B reads VB (also version 2 off live ledger v1), commits.
        disk["content"], disk["mtime"] = "VB", 300
        await cw.import_changed_container_outputs(request, _md(), user)
        # A unblocks, commits stale version-2/HA -> guard 2>=2 SKIP.
        gate["f"].set_result(None)
        await task_a

    asyncio.run(_race())
    ledger = meta_store["meta"]["container_workspace"]["outputs"]["live.txt"]
    assert ledger["last_hash"] == HB and ledger["version"] == 2, (
        f"racing winner B should hold v2: {ledger['last_hash'][:8]} v{ledger['version']}"
    )

    # SELF-HEAL: a later turn with genuinely new content VC. The worker now
    # re-reads the live ledger (version 2), computes version 3 > 2, guard passes.
    disk["content"], disk["mtime"] = "VC", 400
    imported = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert len(imported) == 1 and imported[0]["container_workspace"]["sha256"] == HC
    ledger = meta_store["meta"]["container_workspace"]["outputs"]["live.txt"]
    assert ledger["version"] == 3 and ledger["last_hash"] == HC, (
        f"guard PERMANENTLY suppressed a later genuine update (livelock): {ledger}"
    )

    # And it's stable afterward.
    imported2 = asyncio.run(cw.import_changed_container_outputs(request, _md(), user))
    assert imported2 == [], imported2
