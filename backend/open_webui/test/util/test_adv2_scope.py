"""ROUND-2 adversarial SCOPE-SYMLINK verification.

Round-1 (test_adv_scope.py) hammered _sandbox_linked_files. This round targets
the GAPS round-1 left, and re-checks that the symlink fixes did NOT over-block:

  1. _output_files() directly — round-1 barely touched it. Cover:
       - real nested dirs (outputs/a/b/c.txt) ALL import (no over-block)
       - many regular files all import
       - a real outputs dir containing a SYMLINKED FILE -> regular files still
         listed, symlink skipped (per-file is_symlink guard)
       - a real outputs dir containing a SYMLINKED SUBDIR -> external file NOT
         walked / NOT leaked (rglob must not import through a dir symlink)
       - symlinked outputs dir -> external: returns [] (V5)
       - symlinked outputs dir -> INTERNAL (inputs): returns [] (don't leak
         user uploads as outputs; is_symlink checked before is_dir)
       - a FILE literally named 'outputs' at workspace root -> not a dir -> []
  2. _sandbox_linked_files() over-block checks the round-1 file under-tested:
       - real nested outputs/a/b/c.txt link imports
       - rel == 'outputs' when outputs is a real DIR -> path.is_file() False ->
         skipped (the dir is never a "file" to import)
       - a symlinked SUBDIR inside a real outputs dir: alongside REAL files still
         import; the link-through is rejected (combined over/under block)
  3. Full import_changed_container_outputs end-to-end positive controls:
       - real nested outputs tree imports every real file, nothing else.

A finding is an OVER-block (a legit real outputs file rejected) or an
UNDER-block (any file resolving outside <workspace>/outputs imported).
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
    (workspace / "outputs").mkdir(parents=True, exist_ok=True)
    (workspace / "inputs").mkdir(parents=True, exist_ok=True)
    return data_root, workspace


def _rel_outputs(workspace: Path):
    """_output_files paths relative to outputs dir, posix sorted."""
    outputs = workspace / "outputs"
    out = []
    for p in cw._output_files(outputs):
        try:
            out.append(p.relative_to(outputs).as_posix())
        except Exception:
            out.append(str(p))
    return sorted(out)


# ===========================================================================
# (1) _output_files: legit imports MUST NOT be over-blocked
# ===========================================================================


def test_output_files_real_nested_dirs_all_import():
    """outputs/a/b/c.txt with real intermediate dirs imports the leaf."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    deep = workspace / "outputs" / "a" / "b"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "c.txt").write_text("c")
    (workspace / "outputs" / "top.txt").write_text("top")
    got = _rel_outputs(workspace)
    assert got == ["a/b/c.txt", "top.txt"], got


def test_output_files_many_regular_files_all_import():
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    expect = []
    for i in range(25):
        sub = workspace / "outputs" / f"d{i % 5}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"f{i}.txt").write_text(str(i))
        expect.append(f"d{i % 5}/f{i}.txt")
    got = _rel_outputs(workspace)
    assert got == sorted(expect), (got, sorted(expect))


def test_output_files_skips_symlinked_file_keeps_regulars():
    """A real outputs dir containing a symlinked file (pointing out) plus real
    files: the symlink is skipped, every regular file still imports."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    (workspace / "secret.txt").write_text("SECRET")
    (workspace / "outputs" / "real1.txt").write_text("r1")
    (workspace / "outputs" / "real2.txt").write_text("r2")
    os.symlink(workspace / "secret.txt", workspace / "outputs" / "leak.txt")
    got = _rel_outputs(workspace)
    assert got == ["real1.txt", "real2.txt"], got
    assert "leak.txt" not in got, got


def test_output_files_symlinked_subdir_not_walked():
    """A real outputs dir containing a SYMLINKED SUBDIR -> external location.
    rglob must NOT walk into it and import the external file. Real files
    alongside still import."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    ext = tmp / "external"
    (ext / "nested").mkdir(parents=True, exist_ok=True)
    (ext / "secret.txt").write_text("SECRET")
    (ext / "nested" / "deep_secret.txt").write_text("DEEP SECRET")
    os.symlink(ext, workspace / "outputs" / "link")
    (workspace / "outputs" / "real.txt").write_text("real")
    got = _rel_outputs(workspace)
    # only the real file; nothing reachable through the dir symlink
    assert got == ["real.txt"], got
    assert all("secret" not in g for g in got), got


def test_output_files_symlinked_outputs_dir_external_returns_empty():
    """V5: outputs is a symlink -> external dir. _output_files returns []."""
    tmp = Path(tempfile.mkdtemp())
    data_root = tmp / "containers"
    workspace = data_root / "chat-1" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    ext = tmp / "external"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "leak.txt").write_text("EXTERNAL")
    os.symlink(ext, workspace / "outputs")
    assert cw._output_files(workspace / "outputs") == []


def test_output_files_symlinked_outputs_dir_to_inputs_returns_empty():
    """outputs is a symlink -> the workspace's OWN inputs dir. is_symlink() is
    checked before is_dir(), so this returns [] even though it resolves inside
    the workspace. (Don't surface user uploads as generated outputs.)"""
    tmp = Path(tempfile.mkdtemp())
    data_root = tmp / "containers"
    workspace = data_root / "chat-1" / "workspace"
    (workspace / "inputs").mkdir(parents=True, exist_ok=True)
    (workspace / "inputs" / "private_upload.txt").write_text("USER UPLOAD")
    os.symlink(workspace / "inputs", workspace / "outputs")
    assert (workspace / "outputs").is_dir()  # resolves to a real dir
    assert (workspace / "outputs").is_symlink()
    assert cw._output_files(workspace / "outputs") == []


def test_output_files_file_named_outputs_returns_empty():
    """A regular FILE named 'outputs' (not a dir). not is_dir() -> []."""
    tmp = Path(tempfile.mkdtemp())
    data_root = tmp / "containers"
    workspace = data_root / "chat-1" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "outputs").write_text("I am a file")
    assert cw._output_files(workspace / "outputs") == []


def test_output_files_missing_outputs_dir_returns_empty():
    tmp = Path(tempfile.mkdtemp())
    workspace = tmp / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    assert cw._output_files(workspace / "outputs") == []


# ===========================================================================
# (2) _sandbox_linked_files over/under-block round-2 gaps
# ===========================================================================


def _linked(workspace: Path, content=None, content_blocks=None):
    outputs = workspace / "outputs"
    out = []
    for p in cw._sandbox_linked_files(workspace, content, content_blocks):
        try:
            out.append(p.relative_to(workspace).as_posix())
        except Exception:
            out.append(str(p))
    return out


def test_sandbox_real_nested_link_imports():
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    deep = workspace / "outputs" / "a" / "b"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "c.txt").write_text("c")
    got = _linked(workspace, "see sandbox:/workspace/outputs/a/b/c.txt")
    assert got == ["outputs/a/b/c.txt"], got


def test_sandbox_rel_exactly_outputs_dir_skipped():
    """rel == 'outputs' when outputs is a real DIR: path.is_file() is False ->
    skipped (a directory is never imported as a file)."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    (workspace / "outputs" / "real.txt").write_text("r")
    got = cw._sandbox_linked_files(workspace, "sandbox:/workspace/outputs", None)
    assert got == [], [str(p) for p in got]


def test_sandbox_symlinked_subdir_rejected_real_alongside_imports():
    """Combined over+under block: outputs/link -> external (rejected), but
    outputs/real.txt referenced in the same content still imports."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    ext = tmp / "external"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "secret.txt").write_text("SECRET")
    os.symlink(ext, workspace / "outputs" / "link")
    (workspace / "outputs" / "real.txt").write_text("real")
    content = (
        "good sandbox:/workspace/outputs/real.txt and "
        "bad sandbox:/workspace/outputs/link/secret.txt"
    )
    got = _linked(workspace, content)
    assert got == ["outputs/real.txt"], got
    assert all("secret" not in g for g in got), got


def test_sandbox_outputs_symlink_to_inputs_rejected():
    """outputs symlinked -> inputs: _sandbox_linked_files refuses the whole scan
    (is_symlink) so a sandbox:/workspace/outputs/<upload> can't surface a user
    input as an output."""
    tmp = Path(tempfile.mkdtemp())
    data_root = tmp / "containers"
    workspace = data_root / "chat-1" / "workspace"
    (workspace / "inputs").mkdir(parents=True, exist_ok=True)
    (workspace / "inputs" / "upload.txt").write_text("UPLOAD")
    os.symlink(workspace / "inputs", workspace / "outputs")
    got = cw._sandbox_linked_files(
        workspace, "sandbox:/workspace/outputs/upload.txt", None
    )
    assert got == [], [str(p) for p in got]


# ===========================================================================
# (3) full import end-to-end positive controls (no over-block)
# ===========================================================================


def _drive(monkeypatch, data_root: Path, content: str):
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
    chat_obj.meta = {}

    async def _fake_get_chat_by_id(*_a, **_k):
        return chat_obj

    async def _fake_merge_outputs(*a, **k):
        return True

    upserted = {}

    async def _fake_get_message(*_a, **_k):
        return {}

    async def _fake_upsert_message(cid, mid, payload, **k):
        upserted.update(payload)
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
        # Mirror the REAL _store_output_file descriptor (always has a
        # container_workspace block with workspace_path/sha256/version).
        stored.append({"workspace_path": workspace_path, "resolved": str(path.resolve())})
        return {
            "type": "file",
            "id": f"file-{len(stored)}",
            "name": display_name,
            "container_workspace": {
                "workspace_path": workspace_path,
                "sha256": sha256,
                "version": 1,
            },
        }

    monkeypatch.setattr(cw, "_store_output_file", _fake_store)
    monkeypatch.setattr(cw, "_workspace_chat_id", lambda _m: "chat-1")

    metadata = {
        "container_workspace_output_message_id": "m1",
        "message_id": "m1",
        "chat_id": "chat-1",
    }
    imported = asyncio.run(
        cw.import_changed_container_outputs(request, metadata, user, content=content)
    )
    return imported, stored, upserted


def test_full_import_real_nested_tree_imports_all(monkeypatch):
    """outputs/a/b/c.txt + outputs/top.txt: both real files imported, nothing
    else; the sandbox link in content adds nothing new beyond the scan."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    deep = workspace / "outputs" / "a" / "b"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "c.txt").write_text("c")
    (workspace / "outputs" / "top.txt").write_text("top")
    imported, stored, upserted = _drive(
        monkeypatch, data_root, "sandbox:/workspace/outputs/top.txt"
    )
    paths = sorted(s["workspace_path"] for s in stored)
    assert paths == ["outputs/a/b/c.txt", "outputs/top.txt"], paths
    assert len(imported) == 2, imported
    # files written to message contain exactly the 2 imported descriptors
    assert len(upserted.get("files", [])) == 2, upserted


def test_full_import_real_outputs_symlinked_file_inside_no_leak(monkeypatch):
    """outputs has a real file AND a symlinked file pointing out. Only the real
    file imports; the symlink leaks nothing."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    (workspace / "secret.txt").write_text("SECRET")
    (workspace / "outputs" / "real.txt").write_text("real")
    os.symlink(workspace / "secret.txt", workspace / "outputs" / "leak.txt")
    imported, stored, _ = _drive(
        monkeypatch, data_root, "sandbox:/workspace/outputs/leak.txt"
    )
    paths = sorted(s["workspace_path"] for s in stored)
    assert paths == ["outputs/real.txt"], paths


def test_full_import_file_named_outputs_only_imports_itself(monkeypatch):
    """A regular FILE named exactly 'outputs' at workspace root, referenced via
    sandbox:/workspace/outputs. _output_files refuses it (not a dir). The sandbox
    link resolves to the outputs path itself (relative_to gives '.'), so it CAN
    import — but only that one file (named 'outputs'), never anything else in the
    workspace. Confirm: no out-of-subtree leak; at most the 'outputs' file."""
    tmp = Path(tempfile.mkdtemp())
    data_root = tmp / "containers"
    workspace = data_root / "chat-1" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "inputs").mkdir(parents=True, exist_ok=True)
    (workspace / "secret.txt").write_text("SECRET - must not leak")
    (workspace / "outputs").write_text("I am a file named outputs")
    imported, stored, _ = _drive(
        monkeypatch, data_root, "sandbox:/workspace/outputs"
    )
    # Everything stored must resolve to the 'outputs' file itself; never secret.
    for s in stored:
        assert s["resolved"] == str((workspace / "outputs").resolve()), s
    assert all("secret" not in s["resolved"] for s in stored), stored
    assert all(s["workspace_path"] == "outputs" for s in stored), stored


def test_full_import_symlinked_subdir_inside_outputs_no_leak(monkeypatch):
    """outputs/link -> external dir; outputs/real.txt is real. The external file
    must not import via either the rglob scan or the sandbox link."""
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    ext = tmp / "external"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "secret.txt").write_text("SECRET")
    os.symlink(ext, workspace / "outputs" / "link")
    (workspace / "outputs" / "real.txt").write_text("real")
    imported, stored, _ = _drive(
        monkeypatch,
        data_root,
        "sandbox:/workspace/outputs/real.txt sandbox:/workspace/outputs/link/secret.txt",
    )
    paths = sorted(s["workspace_path"] for s in stored)
    assert paths == ["outputs/real.txt"], paths
    assert all("secret" not in p for p in paths), paths
