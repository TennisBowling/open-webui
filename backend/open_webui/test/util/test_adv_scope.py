"""Adversarial SCOPE verification for `_sandbox_linked_files` / import path.

INVARIANT: a sandbox link may import ONLY files whose RESOLVED path is under
<workspace>/outputs. Nothing else in the workspace can leak in.

We go BEYOND test_repro_scope_sandbox.py: URL-encoded traversal, symlinked
`outputs` dir, nested symlink inside outputs pointing out, case lookalikes,
trailing punctuation in the regex capture, and content_blocks nesting.

We attack at two levels:
  (A) _sandbox_linked_files() directly (the confinement primitive), and
  (B) the full import_changed_container_outputs() (does any leaked path actually
      reach _store_output_file -> the chat?).
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


# --------------------------------------------------------------------------
# (A) direct primitive attacks
# --------------------------------------------------------------------------


def _linked(workspace: Path, content=None, content_blocks=None):
    return [
        p.relative_to(workspace).as_posix() if _under(p, workspace) else str(p)
        for p in cw._sandbox_linked_files(workspace, content, content_blocks)
    ]


def _under(p: Path, base: Path) -> bool:
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False


def test_positive_control_outputs_file_links():
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    (workspace / "outputs" / "report.txt").write_text("hi")
    got = _linked(workspace, "see sandbox:/workspace/outputs/report.txt")
    assert got == ["outputs/report.txt"], got


def test_url_encoded_traversal_rejected():
    """sandbox:/workspace/outputs/%2e%2e/secret.txt  (unquote -> outputs/../secret)."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    (workspace / "secret.txt").write_text("x")
    got = cw._sandbox_linked_files(
        workspace, "sandbox:/workspace/outputs/%2e%2e/secret.txt", None
    )
    assert got == [], got


def test_double_url_encoded_traversal_rejected():
    """%252e -> %2e (single unquote) -> stays literal '%2e', not '..'. Either way
    no real file at that name, so nothing imports. Also encoded slash variant."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    (workspace / "secret.txt").write_text("x")
    got = cw._sandbox_linked_files(
        workspace, "sandbox:/workspace/outputs%2f%2e%2e%2fsecret.txt", None
    )
    assert got == [], got


def test_encoded_traversal_deep_rejected():
    """outputs/a/%2e%2e/%2e%2e/x.txt -> outputs/a/../../x.txt -> escapes."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    (workspace / "outputs" / "a").mkdir(parents=True, exist_ok=True)
    (workspace / "x.txt").write_text("x")
    got = cw._sandbox_linked_files(
        workspace, "sandbox:/workspace/outputs/a/%2e%2e/%2e%2e/x.txt", None
    )
    assert got == [], got


def test_outputs_dir_is_symlink_to_external_rejected():
    """If `outputs` itself is a symlink to an EXTERNAL dir (a container can do
    `rm -rf outputs && ln -s X outputs` on the bind mount), the sandbox-link scan
    must refuse it rather than import a file resolving outside the workspace."""
    tmp = Path(tempfile.mkdtemp())
    data_root = tmp / "containers"
    workspace = data_root / "chat-1" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "inputs").mkdir(parents=True, exist_ok=True)
    external = tmp / "external_secret_store"
    external.mkdir(parents=True, exist_ok=True)
    (external / "leak.txt").write_text("EXTERNAL SECRET")
    os.symlink(external, workspace / "outputs")

    got = cw._sandbox_linked_files(
        workspace, "sandbox:/workspace/outputs/leak.txt", None
    )
    assert got == [], f"symlinked outputs dir leaked external file: {got}"


def test_nested_symlink_inside_outputs_pointing_out_rejected():
    """outputs/link -> <workspace>/secretdir ; sandbox link to outputs/link/f.txt.
    path.resolve() follows link out; outputs_root does NOT (outputs is real).
    relative_to() must REJECT."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    secret = workspace / "secretdir"
    secret.mkdir(parents=True, exist_ok=True)
    (secret / "f.txt").write_text("secret")
    os.symlink(secret, workspace / "outputs" / "link")
    got = cw._sandbox_linked_files(
        workspace, "sandbox:/workspace/outputs/link/f.txt", None
    )
    assert got == [], [str(p.resolve()) for p in got]


def test_symlink_file_inside_outputs_pointing_out_rejected():
    """outputs/leak.txt is a SYMLINK file -> <workspace>/secret.txt. path.is_symlink()
    is True -> rejected at the symlink guard."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    (workspace / "secret.txt").write_text("secret")
    os.symlink(workspace / "secret.txt", workspace / "outputs" / "leak.txt")
    got = cw._sandbox_linked_files(
        workspace, "sandbox:/workspace/outputs/leak.txt", None
    )
    assert got == [], [str(p.resolve()) for p in got]


def test_case_lookalike_OUTPUTS_rejected():
    """OUTPUTS/ (uppercase) is not 'outputs/' at string level -> rejected.
    On a case-insensitive FS the real dir may still differ; assert no import."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    upper = workspace / "OUTPUTS"
    try:
        upper.mkdir(parents=True, exist_ok=True)
        (upper / "u.txt").write_text("u")
    except FileExistsError:
        # case-insensitive FS: OUTPUTS == outputs; put the file in real outputs
        (workspace / "outputs" / "u.txt").write_text("u")
    got = cw._sandbox_linked_files(
        workspace, "sandbox:/workspace/OUTPUTS/u.txt", None
    )
    # string check requires lowercase 'outputs/'; uppercase must be rejected.
    assert got == [], [str(p.resolve()) for p in got]


def test_lookalike_outputsX_rejected():
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    d = workspace / "outputsX"
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.txt").write_text("x")
    got = cw._sandbox_linked_files(
        workspace, "sandbox:/workspace/outputsX/x.txt", None
    )
    assert got == [], [str(p.resolve()) for p in got]


def test_file_literally_named_outputs_at_root_rejected():
    """A regular file named exactly 'outputs' at workspace root. rel == 'outputs'
    passes the string gate, path.is_file() True, but resolve() == <ws>/outputs
    which IS outputs_root; relative_to(outputs_root) on the dir-as-file...
    Document behavior: it must NOT leak a non-outputs-subtree file."""
    tmp = Path(tempfile.mkdtemp())
    data_root = tmp / "containers"
    workspace = data_root / "chat-1" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "inputs").mkdir(parents=True, exist_ok=True)
    # NOTE: cannot have both a file and dir named outputs; make outputs a FILE.
    (workspace / "outputs").write_text("I am a file not a dir")
    got = cw._sandbox_linked_files(workspace, "sandbox:/workspace/outputs", None)
    # outputs_root = (root/outputs).resolve() == the file path; rel 'outputs'
    # resolves to the same path; relative_to(self) -> '.' succeeds. The file IS
    # 'outputs' itself, so this is not an out-of-subtree leak (it resolves to the
    # outputs path exactly). Document it; it is benign for the invariant.
    resolved = [str(p) for p in got]
    print("OUTPUTS-AS-FILE:", resolved)
    assert all(
        p.resolve() == (workspace / "outputs").resolve() for p in got
    ), resolved


def test_trailing_punctuation_in_regex_capture():
    """The regex stops at whitespace, ) and ]. A markdown link
    [x](sandbox:/workspace/outputs/r.txt) captures 'outputs/r.txt' (')' excluded).
    But a trailing '.' or '?query' is INCLUDED in the capture -> wrong filename ->
    no import (fine). Verify the clean markdown case still imports."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    (workspace / "outputs" / "r.txt").write_text("r")
    got = _linked(workspace, "[link](sandbox:/workspace/outputs/r.txt)")
    assert got == ["outputs/r.txt"], got


def test_content_blocks_nested_traversal_rejected():
    """A traversal link buried in a nested tool-result content block must be
    reached by _iter_text_values AND still confined."""
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    (workspace / "secret.txt").write_text("s")
    (workspace / "outputs" / "ok.txt").write_text("ok")
    blocks = [
        {
            "type": "tool_result",
            "content": [
                {"type": "text", "text": "junk"},
                {
                    "type": "reasoning",
                    "nested": {
                        "deep": "see sandbox:/workspace/outputs/../secret.txt and "
                        "sandbox:/workspace/outputs/ok.txt"
                    },
                },
            ],
        }
    ]
    got = _linked(workspace, None, blocks)
    assert "outputs/ok.txt" in got, got
    assert all("secret" not in g for g in got), got


def test_content_blocks_encoded_traversal_rejected():
    tmp = Path(tempfile.mkdtemp())
    _, workspace = _setup_workspace(tmp)
    (workspace / "secret.txt").write_text("s")
    blocks = [{"x": {"y": "sandbox:/workspace/outputs/%2e%2e/secret.txt"}}]
    got = cw._sandbox_linked_files(workspace, None, blocks)
    assert got == [], [str(p) for p in got]


# --------------------------------------------------------------------------
# (B) full import path: prove no leaked path reaches _store_output_file
# --------------------------------------------------------------------------


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
        # Mirror the REAL descriptor shape including the container_workspace block.
        stored.append({"workspace_path": workspace_path, "resolved": str(path.resolve())})
        return {
            "id": f"file-{len(stored)}",
            "name": display_name,
            "container_workspace": {"workspace_path": workspace_path, "sha256": sha256, "version": 1},
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
    return imported, stored


def test_full_import_encoded_traversal_no_leak(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    (workspace / "secret.txt").write_text("EXTERNAL")
    imported, stored = _drive(
        monkeypatch, data_root, "sandbox:/workspace/outputs/%2e%2e/secret.txt"
    )
    assert stored == [], stored
    assert imported == [], imported


def test_full_import_nested_symlink_no_leak(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    secret = workspace / "secretdir"
    secret.mkdir(parents=True, exist_ok=True)
    (secret / "f.txt").write_text("secret")
    os.symlink(secret, workspace / "outputs" / "link")
    imported, stored = _drive(
        monkeypatch, data_root, "sandbox:/workspace/outputs/link/f.txt"
    )
    assert stored == [], stored


def test_full_import_outputs_symlink_external(monkeypatch):
    """End-to-end: outputs is a symlink to an external dir. The full import path must
    NOT leak any external file into the chat (both _sandbox_linked_files and
    _output_files refuse a symlinked outputs dir)."""
    tmp = Path(tempfile.mkdtemp())
    data_root = tmp / "containers"
    workspace = data_root / "chat-1" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "inputs").mkdir(parents=True, exist_ok=True)
    external = tmp / "external_secret_store"
    external.mkdir(parents=True, exist_ok=True)
    (external / "leak.txt").write_text("EXTERNAL SECRET")
    os.symlink(external, workspace / "outputs")
    imported, stored = _drive(
        monkeypatch, data_root, "sandbox:/workspace/outputs/leak.txt"
    )
    assert stored == [], f"symlinked outputs dir leaked external file end-to-end: {stored}"
    assert imported == [], imported


def test_full_import_positive_control(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    data_root, workspace = _setup_workspace(tmp)
    (workspace / "outputs" / "good.txt").write_text("good")
    imported, stored = _drive(
        monkeypatch, data_root, "sandbox:/workspace/outputs/good.txt"
    )
    assert [s["workspace_path"] for s in stored] == ["outputs/good.txt"], stored
