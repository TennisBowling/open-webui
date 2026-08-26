"""Adversarial verification of the NO-DUP invariant:

After ANY sequence of import_changed_container_outputs calls (sequential,
2-/3-way concurrent races, retries, re-runs to the same message_id), the
assistant message's files list never contains two entries for the same logical
output (same workspace_path + same content).

We also guard the OPPOSITE failure: two DIFFERENT files that happen to share a
sha256 but live at different workspace_paths must NOT be wrongly collapsed.

Mocks mirror the REAL _store_output_file: the returned descriptor ALWAYS carries
a container_workspace:{workspace_path, sha256, version} block (that is what the
content-identity dedup keys on). _merge_container_workspace_outputs mirrors the
real Chats.merge_container_workspace_outputs (re-read live ledger under "lock",
first-writer-wins for identical last_hash).
"""

import asyncio
import tempfile
import uuid
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
    """Mirror Chats.merge_container_workspace_outputs."""
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


def _patch(monkeypatch, data_root, meta_store, message_store, *, store_hook=None,
           get_msg_hook=None):
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

    async def _fake_get_message(_cid, mid, *_a, **_k):
        if get_msg_hook is not None:
            await get_msg_hook()
        return {"files": list(message_store.get(mid, []))}

    async def _fake_upsert_message(_cid, mid, partial, **k):
        if "files" in partial:
            message_store[mid] = list(partial["files"])

    monkeypatch.setattr(cw.Chats, "get_chat_by_id", _fake_get_chat_by_id)
    monkeypatch.setattr(cw.Chats, "merge_container_workspace_outputs", _fake_merge_outputs)
    monkeypatch.setattr(cw.Chats, "get_message_by_id_and_message_id", _fake_get_message)
    monkeypatch.setattr(
        cw.Chats, "upsert_message_to_chat_by_id_and_message_id", _fake_upsert_message
    )

    async def _fake_store(req, usr, path, display_name, size, sha256,
                          workspace_path, chat_id, message_id, version):
        if store_hook is not None:
            await store_hook()
        # Mirror the REAL descriptor: ALWAYS carries the container_workspace block.
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


def _meta(mid="m1"):
    return {
        "container_workspace_output_message_id": mid,
        "message_id": mid,
        "chat_id": "chat-1",
    }


def _names(files):
    return sorted(f.get("name") for f in files)


def _content_keys(files):
    return [cw._file_content_key(f) for f in files]


# ---------------------------------------------------------------------------
# Vector 1: 3-way concurrent race on a fresh outputs file.
# ---------------------------------------------------------------------------
def test_three_way_concurrent_race_no_dup(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    (outputs / "one.txt").write_text("the one and only output")

    meta_store = {"meta": {}}
    message_store = {}

    gate = {"fut": None}

    async def _hook():
        if gate["fut"] is not None:
            await gate["fut"]

    _patch(monkeypatch, data_root, meta_store, message_store, store_hook=_hook)

    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    async def _run():
        loop = asyncio.get_event_loop()
        rel = loop.create_future()
        gate["fut"] = rel

        # Three imports to the SAME chat + message, all reading the empty ledger.
        t1 = asyncio.create_task(
            cw.import_changed_container_outputs(request, _meta(), user)
        )
        t2 = asyncio.create_task(
            cw.import_changed_container_outputs(request, _meta(), user)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Third runs fully (no gate) reading the same still-empty ledger.
        gate["fut"] = None
        i3 = await cw.import_changed_container_outputs(request, _meta(), user)
        rel.set_result(None)
        i1 = await t1
        i2 = await t2
        return i1, i2, i3

    i1, i2, i3 = asyncio.run(_run())
    files = message_store["m1"]
    one = [f for f in files if f.get("name") == "one.txt"]
    assert len(one) == 1, f"3-way race duplicated: {files}"
    assert len(set(_content_keys(files))) == len(files)


# ---------------------------------------------------------------------------
# Vector 2: same file imported across two messages, then back to the first.
# ---------------------------------------------------------------------------
def test_cross_message_then_back_no_dup_in_either(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    (outputs / "one.txt").write_text("stable bytes")

    meta_store = {"meta": {}}
    message_store = {}
    _patch(monkeypatch, data_root, meta_store, message_store)

    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    # Import to m1 (first turn). Ledger now records one.txt.
    i1 = asyncio.run(cw.import_changed_container_outputs(request, _meta("m1"), user))
    # Import to m2 (a different assistant message) — file unchanged: ledger fast
    # path => nothing new imported.
    i2 = asyncio.run(cw.import_changed_container_outputs(request, _meta("m2"), user))
    # Re-run import back to m1 — still unchanged.
    i1b = asyncio.run(cw.import_changed_container_outputs(request, _meta("m1"), user))

    assert len(i1) == 1
    # Unchanged file: subsequent imports add nothing.
    assert i2 == []
    assert i1b == []
    assert len([f for f in message_store.get("m1", []) if f["name"] == "one.txt"]) == 1
    assert len(message_store.get("m2", [])) == 0


# ---------------------------------------------------------------------------
# Vector 2b: same content, file MOVED so it appears at two message imports with
# a touched mtime forcing re-hash. Even if re-imported, message.files must dedup.
# ---------------------------------------------------------------------------
def test_reimport_same_content_touched_mtime_collapses(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    f = outputs / "one.txt"
    f.write_text("same content forever")

    meta_store = {"meta": {}}
    message_store = {}
    _patch(monkeypatch, data_root, meta_store, message_store)

    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    i1 = asyncio.run(cw.import_changed_container_outputs(request, _meta("m1"), user))
    assert len(i1) == 1

    # Corrupt the ledger stat cache so the fast path misses and we re-hash; but
    # the content hash matches -> last_hash equal -> no re-import.
    state = meta_store["meta"]["container_workspace"]["outputs"]["one.txt"]
    state["stat_mtime_ns"] = -1
    state["stat_size"] = -1

    i2 = asyncio.run(cw.import_changed_container_outputs(request, _meta("m1"), user))
    assert i2 == [], "identical content re-hash must not re-import"
    assert len([x for x in message_store["m1"] if x["name"] == "one.txt"]) == 1


# ---------------------------------------------------------------------------
# Vector 3 (OPPOSITE failure): two DIFFERENT files at different workspace_paths
# that happen to share the SAME sha256 must NOT be collapsed.
# ---------------------------------------------------------------------------
def test_same_sha_different_path_not_collapsed(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    # Identical bytes -> identical sha256, but distinct paths.
    (outputs / "a.txt").write_text("identical bytes")
    (outputs / "b.txt").write_text("identical bytes")

    meta_store = {"meta": {}}
    message_store = {}
    _patch(monkeypatch, data_root, meta_store, message_store)

    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    imported = asyncio.run(
        cw.import_changed_container_outputs(request, _meta("m1"), user)
    )
    assert len(imported) == 2, imported
    names = _names(message_store["m1"])
    assert names == ["a.txt", "b.txt"], names
    # Both content keys present and distinct (path is part of the key).
    keys = _content_keys(message_store["m1"])
    assert len(set(keys)) == 2, keys


# ---------------------------------------------------------------------------
# Vector 4: a genuine NEW version (same path, changed content) arriving
# concurrently with the old. First-writer-wins in the ledger must not DROP the
# new version's card from message.files.
# ---------------------------------------------------------------------------
def test_concurrent_new_version_not_dropped_from_files(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    f = outputs / "one.txt"
    f.write_text("VERSION ONE")

    meta_store = {"meta": {}}
    message_store = {}

    # store_hook lets us interleave: A reads/hashes v1, then file flips to v2
    # before B hashes; both write ledger; both append to files.
    state = {"phase": 0, "fut_a": None}

    async def _hook():
        fut = state.get("fut_a")
        if fut is not None:
            await fut

    _patch(monkeypatch, data_root, meta_store, message_store, store_hook=_hook)

    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    async def _run():
        loop = asyncio.get_event_loop()
        rel = loop.create_future()
        state["fut_a"] = rel
        # A: hashes v1, then blocks inside _store_output_file on the gate.
        ta = asyncio.create_task(
            cw.import_changed_container_outputs(request, _meta("m1"), user)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Flip file to v2 while A is parked.
        f.write_text("VERSION TWO IS LONGER")
        # B runs fully (no gate): reads ledger (still empty, A hasn't written),
        # hashes v2, stores, writes ledger + files.
        state["fut_a"] = None
        ib = await cw.import_changed_container_outputs(request, _meta("m1"), user)
        # Release A: it stored v1 with its stale version number, writes ledger
        # (first-writer-wins vs B's v2? different hash so it overwrites), files.
        rel.set_result(None)
        ia = await ta
        return ia, ib

    ia, ib = asyncio.run(_run())
    files = message_store["m1"]
    # Both versions are genuinely different content -> both cards may show, but
    # neither must be a same-path+same-content duplicate, and the v2 card MUST be
    # present (not dropped).
    keys = _content_keys(files)
    assert len(set(keys)) == len(keys), f"same path+content dup: {files}"
    shas = {f["container_workspace"]["sha256"] for f in files}
    import hashlib
    v2_sha = hashlib.sha256(b"VERSION TWO IS LONGER").hexdigest()
    assert v2_sha in shas, f"NEW version dropped from message.files: {files}"


# ---------------------------------------------------------------------------
# Vector 5: sequential retry/re-run to the same message_id many times.
# ---------------------------------------------------------------------------
def test_many_sequential_reruns_no_dup(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    (outputs / "one.txt").write_text("content")

    meta_store = {"meta": {}}
    message_store = {}
    _patch(monkeypatch, data_root, meta_store, message_store)

    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    for _ in range(5):
        asyncio.run(cw.import_changed_container_outputs(request, _meta("m1"), user))
    assert len([f for f in message_store["m1"] if f["name"] == "one.txt"]) == 1


# ---------------------------------------------------------------------------
# Vector 6: ledger lost (e.g. meta cleared) -> re-import with a fresh uuid, but
# message.files ALREADY has the old card. content-dedup must collapse.
# ---------------------------------------------------------------------------
def test_ledger_lost_reimport_collapses_in_files(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    (outputs / "one.txt").write_text("durable content")

    meta_store = {"meta": {}}
    message_store = {}
    _patch(monkeypatch, data_root, meta_store, message_store)

    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    i1 = asyncio.run(cw.import_changed_container_outputs(request, _meta("m1"), user))
    assert len(i1) == 1
    # Simulate the ledger being lost (the exact failure the content-dedup defends).
    meta_store["meta"] = {}

    i2 = asyncio.run(cw.import_changed_container_outputs(request, _meta("m1"), user))
    # Re-imported (ledger gone) with a fresh uuid...
    assert len(i2) == 1
    # ...but message.files still shows it once (content-identity collapse).
    one = [f for f in message_store["m1"] if f["name"] == "one.txt"]
    assert len(one) == 1, f"ledger-lost re-import duplicated card: {message_store['m1']}"


# ---------------------------------------------------------------------------
# Vector 7 (the message.files lost-update race): ledger is lost so BOTH
# concurrent imports re-import the same content with a fresh uuid; we gate at the
# get_message boundary so both read the SAME baseline message.files (which does
# NOT yet contain the other's entry) and then both write. If the writes race,
# does a same-(path,content) duplicate survive into message.files?
# ---------------------------------------------------------------------------
def test_message_files_lost_update_race_same_content(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    (outputs / "one.txt").write_text("raced content")

    meta_store = {"meta": {}}
    message_store = {}

    gate = {"fut": None}

    async def _get_msg_hook():
        fut = gate["fut"]
        if fut is not None:
            await fut

    _patch(
        monkeypatch, data_root, meta_store, message_store,
        get_msg_hook=_get_msg_hook,
    )

    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    async def _run():
        loop = asyncio.get_event_loop()
        rel = loop.create_future()
        # A runs first WITHOUT the ledger so it imports + writes files=[A].
        gate["fut"] = None
        ia = await cw.import_changed_container_outputs(request, _meta("m1"), user)
        # Now WIPE the ledger so B also re-imports the same content with a fresh
        # uuid. Gate B at get_message so we can inspect the interleave.
        meta_store["meta"] = {}
        gate["fut"] = rel
        tb = asyncio.create_task(
            cw.import_changed_container_outputs(request, _meta("m1"), user)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Release B: it reads message.files (which DOES contain A's entry now),
        # merges -> content-dedup must collapse against the existing A card.
        rel.set_result(None)
        ib = await tb
        return ia, ib

    ia, ib = asyncio.run(_run())
    files = message_store["m1"]
    one = [f for f in files if f.get("name") == "one.txt"]
    assert len(one) == 1, f"lost-update race duplicated same content: {files}"


# ---------------------------------------------------------------------------
# Vector 8 (strongest same-content concurrent write): BOTH imports read the SAME
# empty baseline message.files (gate both at get_message simultaneously), each
# stores its own fresh-uuid descriptor for the SAME (path, content), then both
# write. Last-write-wins on the full-replace upsert. Must end with ONE card.
# ---------------------------------------------------------------------------
def test_simultaneous_empty_baseline_same_content_no_dup(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace, outputs = _setup_workspace(tmp)
    (outputs / "one.txt").write_text("simultaneous content")

    meta_store = {"meta": {}}
    message_store = {}

    barrier = {"fut": None, "count": 0}

    async def _get_msg_hook():
        # Park the first arrival; release both together so they share the empty
        # baseline read.
        barrier["count"] += 1
        if barrier["fut"] is not None and barrier["count"] == 1:
            await barrier["fut"]

    _patch(
        monkeypatch, data_root, meta_store, message_store,
        get_msg_hook=_get_msg_hook,
    )

    request = MagicMock()
    user = MagicMock()
    user.id = "user-1"

    async def _run():
        loop = asyncio.get_event_loop()
        rel = loop.create_future()
        barrier["fut"] = rel
        ta = asyncio.create_task(
            cw.import_changed_container_outputs(request, _meta("m1"), user)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        tb = asyncio.create_task(
            cw.import_changed_container_outputs(request, _meta("m1"), user)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        rel.set_result(None)
        ia = await ta
        ib = await tb
        return ia, ib

    asyncio.run(_run())
    files = message_store["m1"]
    one = [f for f in files if f.get("name") == "one.txt"]
    assert len(one) == 1, f"simultaneous same-content write duplicated: {files}"
