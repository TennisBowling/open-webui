"""Focused tests for the container-workspace input staging root fixes.

Two regressions were fixed in ``prepare_container_workspace_for_turn``:

1. PHANTOM ANNOUNCE — the frontend re-sends the chat-wide sticky file set with
   every request (so the container keeps access to historical attachments), but
   the backend used to re-copy AND re-announce every one of them on every turn:
   each message the user sent got a fresh ``<document>Uploaded to location ...``
   block appended even though nothing was attached (e.g. "Strava Activity
   Data_9.zip" — the counter suffix was the re-copy churn). Only files attached
   to THIS user message may be announced.
2. RECORD REUSE — an already-copied (immutable) file id must reuse its existing
   input record instead of creating another numbered copy on disk.
"""

from types import SimpleNamespace

import pytest

from test.util.db import configure_test_database

configure_test_database()

cw = pytest.importorskip("open_webui.utils.container_workspace")


class FakeConfig:
    ENABLE_CONTAINER_WORKSPACE_SYNC = True
    CONTAINER_DATA_ROOT = ""
    CONTAINER_MCP_SERVER_ID = "srv1"


class FakeApp:
    def __init__(self, config):
        self.state = SimpleNamespace(config=config)


class FakeRequest:
    def __init__(self, config):
        self.app = FakeApp(config)


def _file_item(file_id: str, name: str) -> dict:
    return {"type": "file", "id": file_id, "name": name, "url": f"/files/{file_id}"}


def _record(file_id: str, name: str) -> dict:
    return {
        "file_id": file_id,
        "original_name": name,
        "workspace_path": f"inputs/{name}",
        "size": 3,
        "sha256": "abc",
        "content_type": "text/plain",
        "message_id": "user-1",
    }


def _make_stubs(monkeypatch, tmp_path, user_message: dict, file_bytes: dict):
    """Monkeypatch the external world for prepare_container_workspace_for_turn."""
    monkeypatch.setattr(cw, "is_container_workspace_active", lambda *a, **k: True)

    async def _get_message(chat_id, message_id):
        return {
            "user-1": user_message,
            "assist-1": {"id": "assist-1", "parentId": "user-1"},
        }.get(message_id)

    monkeypatch.setattr(cw.Chats, "get_message_by_id_and_message_id", _get_message)

    async def _get_file(file_id):
        return SimpleNamespace(
            user_id="u1",
            path=f"stored/{file_id}",
            filename=f"{file_id}.txt",
            meta={"name": file_bytes.get(file_id, ("name", None))[0], "content_type": "text/plain"},
        )

    monkeypatch.setattr(cw.Files, "get_file_by_id", _get_file)
    monkeypatch.setattr(
        cw.Storage,
        "get_file",
        lambda path: str(tmp_path / path),
    )
    upserts = []
    async def _upsert(chat_id, message_id, patch, return_model=True):
        upserts.append((chat_id, message_id, patch))
        return None

    monkeypatch.setattr(cw.Chats, "upsert_message_to_chat_by_id_and_message_id", _upsert)
    return upserts


def _run(monkeypatch, tmp_path, metadata, user_message, form_messages):
    import asyncio

    config = FakeConfig()
    config.CONTAINER_DATA_ROOT = str(tmp_path / "containers")
    request = FakeRequest(config)
    form_data = {"messages": form_messages}
    asyncio.run(
        cw.prepare_container_workspace_for_turn(
            request,
            metadata,
            form_data,
            SimpleNamespace(id="u1", role="user"),
            None,
        )
    )
    return form_data


def test_only_this_messages_files_are_announced(monkeypatch, tmp_path):
    # The sticky re-send carries an OLD file (sticky-file) plus a NEW file the
    # user attached to THIS message (new-file). Only the new one may be
    # announced to the model.
    metadata = {
        "chat_id": "chatA",
        "message_id": "assist-1",
        "files": [_file_item("sticky-file", "old.zip"), _file_item("new-file", "new.fit")],
    }
    user_message = {"id": "user-1", "role": "user", "files": [_file_item("new-file", "new.fit")]}
    (tmp_path / "stored").mkdir()
    (tmp_path / "stored" / "sticky-file").write_text("OLD")
    (tmp_path / "stored" / "new-file").write_text("NEW")

    upserts = _make_stubs(
        monkeypatch,
        tmp_path,
        user_message,
        {"sticky-file": ("old.zip", "OLD"), "new-file": ("new.fit", "NEW")},
    )
    prompt = _run(
        monkeypatch,
        tmp_path,
        metadata,
        user_message,
        [{"role": "user", "content": "hola"}],
    )

    content = prompt["messages"][-1]["content"]
    assert "new.fit" in content
    assert "old.zip" not in content, "sticky old file must NOT be re-announced"


def test_sticky_file_is_not_reannounced_without_new_attachments(monkeypatch, tmp_path):
    # The exact phantom case: a later message with NO attachments. The payload
    # still carries the sticky file set, but nothing may be appended.
    metadata = {
        "chat_id": "chatA",
        "message_id": "assist-1",
        "files": [_file_item("sticky-file", "old.zip")],
    }
    user_message = {"id": "user-1", "role": "user", "files": []}
    (tmp_path / "stored").mkdir()
    (tmp_path / "stored" / "sticky-file").write_text("OLD")

    _make_stubs(monkeypatch, tmp_path, user_message, {"sticky-file": ("old.zip", "OLD")})
    prompt = _run(
        monkeypatch,
        tmp_path,
        metadata,
        user_message,
        [{"role": "user", "content": "hola"}],
    )

    assert prompt["messages"][-1]["content"] == "hola", (
        "no document block may be appended for a message with no attachments"
    )


def test_prior_copy_is_reused_not_recopied(monkeypatch, tmp_path):
    # The same sticky file id must reuse its existing record: the inputs dir
    # keeps ONE copy and the message's records carry no duplicates.
    metadata = {
        "chat_id": "chatA",
        "message_id": "assist-1",
        "files": [_file_item("sticky-file", "old.zip"), _file_item("new-file", "new.fit")],
    }
    user_message = {
        "id": "user-1",
        "role": "user",
        "files": [_file_item("new-file", "new.fit")],
        "container_workspace_inputs": [_record("sticky-file", "old.zip")],
    }
    (tmp_path / "stored").mkdir()
    (tmp_path / "stored" / "sticky-file").write_text("OLD")
    (tmp_path / "stored" / "new-file").write_text("NEW")

    upserts = _make_stubs(
        monkeypatch,
        tmp_path,
        user_message,
        {"sticky-file": ("old.zip", "OLD"), "new-file": ("new.fit", "NEW")},
    )
    # Simulate the PREVIOUS turn's copy: the reused record's file already
    # exists in the inputs dir (reuse means no second copy is made).
    inputs_dir = tmp_path / "containers" / "chatA" / "workspace" / "inputs"
    inputs_dir.mkdir(parents=True)
    (inputs_dir / "old.zip").write_text("OLD")

    _run(
        monkeypatch,
        tmp_path,
        metadata,
        user_message,
        [{"role": "user", "content": "hola"}],
    )

    names = sorted(p.name for p in inputs_dir.iterdir())
    assert names == ["new.fit", "old.zip"], f"unexpected copies: {names}"
    assert all("_2" not in n for n in names), "no numbered re-copies"

    # The persisted merged records have exactly one entry per file id.
    assert len(upserts) == 1
    merged = upserts[0][2]["container_workspace_inputs"]
    ids = [r["file_id"] for r in merged]
    assert ids.count("sticky-file") == 1
    assert ids.count("new-file") == 1


def test_sticky_only_turn_persists_nothing_new(monkeypatch, tmp_path):
    # When the payload carries ONLY files already copied for THIS message, the
    # merged records are unchanged and no upsert is needed.
    metadata = {
        "chat_id": "chatA",
        "message_id": "assist-1",
        "files": [_file_item("sticky-file", "old.zip")],
    }
    user_message = {
        "id": "user-1",
        "role": "user",
        "files": [],
        "container_workspace_inputs": [_record("sticky-file", "old.zip")],
    }
    upserts = _make_stubs(monkeypatch, tmp_path, user_message, {})
    _run(
        monkeypatch,
        tmp_path,
        metadata,
        user_message,
        [{"role": "user", "content": "hola"}],
    )
    assert upserts == []
