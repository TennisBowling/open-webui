"""Regression: the sandbox-link scan must import files ONLY from /workspace/outputs.

`_sandbox_linked_files` used to scan for `sandbox:/workspace/<rel>` references in
the assistant content and import any referenced regular file whose `rel` did NOT
start with `inputs/` — leaking workspace-root files, `.cam/` files, and scratch
dirs into the chat. The documented contract (DEFAULT_CONTAINER_SYSTEM_PROMPT) is
that user-facing files live ONLY under /workspace/outputs. The fix confines the
sandbox-link branch to the `outputs/` subtree.

These tests drive the real `import_changed_container_outputs` against a temp
workspace with the heavy collaborators (Storage/DB/reclaim/store/ledger-merge)
mocked, and assert ONLY outputs/ paths are ever stored.
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
    (workspace / "outputs").mkdir(parents=True, exist_ok=True)
    (workspace / "inputs").mkdir(parents=True, exist_ok=True)
    return data_root, workspace


def _drive(monkeypatch, data_root: Path, content: str, meta_store: dict):
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

    async def _fake_get_chat_by_id(*_a, **_k):
        chat_obj.meta = meta_store["meta"]
        return chat_obj

    async def _fake_merge_outputs(_cid, outputs_updates, data_root="", server_id=""):
        meta = dict(meta_store["meta"])
        cw_meta = dict(meta.get("container_workspace") or {})
        outputs = dict(cw_meta.get("outputs") or {})
        for k, v in outputs_updates.items():
            outputs[k] = v
        cw_meta["outputs"] = outputs
        meta["container_workspace"] = cw_meta
        meta_store["meta"] = meta
        return True

    async def _fake_get_message(*_a, **_k):
        return {}

    async def _fake_upsert_message(*a, **k):
        return None

    monkeypatch.setattr(cw.Chats, "get_chat_by_id", _fake_get_chat_by_id)
    monkeypatch.setattr(
        cw.Chats, "merge_container_workspace_outputs", _fake_merge_outputs
    )
    monkeypatch.setattr(cw.Chats, "get_message_by_id_and_message_id", _fake_get_message)
    monkeypatch.setattr(
        cw.Chats, "upsert_message_to_chat_by_id_and_message_id", _fake_upsert_message
    )

    stored = []

    async def _fake_store(req, usr, path, display_name, size, sha256, workspace_path, *rest):
        stored.append(workspace_path)
        return {"id": f"file-{len(stored)}", "name": display_name}

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)

    metadata = {
        "container_workspace_output_message_id": "m1",
        "message_id": "m1",
        "chat_id": "chat-1",
    }
    monkeypatch.setattr(cw, "_workspace_chat_id", lambda _m: "chat-1")

    imported = asyncio.run(
        cw.import_changed_container_outputs(request, metadata, user, content=content)
    )
    return imported, stored


def test_outputs_file_via_sandbox_link_imports_control(monkeypatch):
    """Control: a real outputs/ file imports (covered by plain outputs scan too)."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    (workspace / "outputs" / "report.txt").write_text("hello")

    imported, stored = _drive(
        monkeypatch, data_root, "see sandbox:/workspace/outputs/report.txt", {"meta": {}}
    )
    assert stored == ["outputs/report.txt"]


def test_workspace_root_file_via_sandbox_link_is_confined(monkeypatch):
    """A file at the workspace ROOT referenced via a sandbox link must NOT import."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    (workspace / "secret_notes.txt").write_text("not meant for the user")

    imported, stored = _drive(
        monkeypatch, data_root, "see sandbox:/workspace/secret_notes.txt", {"meta": {}}
    )
    assert stored == [], f"non-outputs file leaked in: {stored}"


def test_cam_internal_file_via_sandbox_link_is_confined(monkeypatch):
    """A .cam/ internal file referenced via a sandbox link must NOT import."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    cam_dir = workspace / ".cam" / "browser"
    cam_dir.mkdir(parents=True, exist_ok=True)
    (cam_dir / "seed-state.json").write_text("{}")

    imported, stored = _drive(
        monkeypatch,
        data_root,
        "ref sandbox:/workspace/.cam/browser/seed-state.json",
        {"meta": {}},
    )
    assert stored == [], f".cam internal file leaked in: {stored}"


def test_scratch_dir_file_via_sandbox_link_is_confined(monkeypatch):
    """An arbitrary scratch dir file referenced via a sandbox link must NOT import."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    scratch = workspace / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "wip.txt").write_text("work in progress")

    imported, stored = _drive(
        monkeypatch, data_root, "ref sandbox:/workspace/scratch/wip.txt", {"meta": {}}
    )
    assert stored == [], f"scratch file leaked in: {stored}"


def test_inputs_file_is_excluded_control(monkeypatch):
    """Control: inputs/ is excluded — should NOT import."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    (workspace / "inputs" / "upload.txt").write_text("user upload")

    imported, stored = _drive(
        monkeypatch, data_root, "ref sandbox:/workspace/inputs/upload.txt", {"meta": {}}
    )
    assert stored == []


def test_outputs_lookalike_sibling_dir_is_confined(monkeypatch):
    """A sibling dir whose name merely STARTS WITH 'outputs' (e.g. outputs_old/)
    must NOT be treated as the outputs subtree."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    sibling = workspace / "outputs_old"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "stale.txt").write_text("stale")

    imported, stored = _drive(
        monkeypatch, data_root, "ref sandbox:/workspace/outputs_old/stale.txt", {"meta": {}}
    )
    assert stored == [], f"outputs-lookalike dir leaked in: {stored}"


def test_outputs_traversal_escape_is_confined(monkeypatch):
    """A traversal that string-matches 'outputs/' but RESOLVES outside it
    (outputs/../secret.txt) must NOT import."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    (workspace / "secret.txt").write_text("escaped")

    imported, stored = _drive(
        monkeypatch, data_root, "ref sandbox:/workspace/outputs/../secret.txt", {"meta": {}}
    )
    assert stored == [], f"traversal escape leaked in: {stored}"
