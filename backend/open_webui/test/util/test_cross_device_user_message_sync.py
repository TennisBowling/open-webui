"""Unit tests for cross-device user-prompt sync.

When two devices view the same chat, a prompt typed on one device must appear on
the other alongside the assistant stream. The backend co-delivers the newly
persisted user message as a stream-scoped ``chat:user-message`` event. These
tests pin the two load-bearing pieces of that contract WITHOUT a database or live
sockets:

* ``_normalize_files_for_portability`` — guarantees attachments carry a portable
  (``/api/v1/files/{id}/content`` or ``data:``) url so images/docs render on the
  other device (and on a plain reload), matching what is persisted.
* ``emit_chat_user_message`` — builds a stream-scoped envelope (so routing fans
  it to the chat's stream room + origin only, never the USER_POOL), keyed to the
  assistant id, and refuses to emit for local/temporary chats or id-less rows.

Pure unit tests: ``configure_test_database(required=False)`` sets a dummy
non-connecting DB url so importing the modules never touches Postgres. Async
helpers are driven with ``asyncio.run`` (the harness silently skips
``@pytest.mark.asyncio`` tests).
"""

import asyncio

from test.util.db import configure_test_database

configure_test_database(required=False)

from open_webui.utils.chat import _normalize_files_for_portability  # noqa: E402
from open_webui.socket import main as socket_main  # noqa: E402
from open_webui.socket.main import (  # noqa: E402
    emit_chat_user_message,
    STREAM_SCOPED_TYPES,
    _BATCHABLE_TYPES,
    _is_stream_scoped_payload,
)


# --- file portability -------------------------------------------------------


def test_blob_url_with_id_rewritten_to_portable():
    out = _normalize_files_for_portability(
        [{"type": "image", "url": "blob:http://localhost/abc", "id": "f123"}]
    )
    assert out[0]["url"] == "/api/v1/files/f123/content"
    # non-destructive: other fields preserved
    assert out[0]["type"] == "image"
    assert out[0]["id"] == "f123"


def test_data_url_is_kept():
    data_url = "data:image/png;base64,AAAA"
    out = _normalize_files_for_portability([{"type": "image", "url": data_url}])
    assert out[0]["url"] == data_url


def test_already_portable_url_is_kept():
    url = "/api/v1/files/f9/content"
    out = _normalize_files_for_portability([{"type": "image", "url": url, "id": "f9"}])
    assert out[0]["url"] == url


def test_document_without_url_backfilled_from_id():
    out = _normalize_files_for_portability(
        [{"type": "file", "name": "spec.pdf", "id": "doc7", "size": 10}]
    )
    assert out[0]["url"] == "/api/v1/files/doc7/content"
    assert out[0]["name"] == "spec.pdf"


def test_blob_url_without_id_left_untouched():
    # Non-portable but unrecoverable (no id) -> leave as-is rather than drop, so
    # the surgical insert never diverges from a plain reload.
    f = {"type": "image", "url": "blob:http://localhost/xyz"}
    out = _normalize_files_for_portability([f])
    assert out[0]["url"] == "blob:http://localhost/xyz"


def test_non_dict_and_non_list_inputs():
    assert _normalize_files_for_portability(None) is None
    assert _normalize_files_for_portability("nope") == "nope"
    mixed = _normalize_files_for_portability(["str", 5, {"id": "a"}])
    assert mixed[0] == "str" and mixed[1] == 5
    assert mixed[2]["url"] == "/api/v1/files/a/content"


def test_normalize_does_not_mutate_input():
    original = [{"type": "image", "url": "blob:x", "id": "f1"}]
    _normalize_files_for_portability(original)
    # original dict untouched (we copy per entry)
    assert original[0]["url"] == "blob:x"


# --- routing invariants -----------------------------------------------------


def test_user_message_type_is_stream_scoped():
    assert "chat:user-message" in STREAM_SCOPED_TYPES


def test_user_message_type_is_not_batchable():
    # Must bypass the delta batch/replay/ack machinery — it is a discrete event.
    assert "chat:user-message" not in _BATCHABLE_TYPES


# --- emit_chat_user_message -------------------------------------------------


def _capture_emit(monkeypatch_target=socket_main):
    captured = []

    async def _fake_emit_to_primary(user_id, payload):
        captured.append((user_id, payload))

    monkeypatch_target.emit_to_primary = _fake_emit_to_primary
    return captured


def test_emit_builds_stream_scoped_envelope():
    original = socket_main.emit_to_primary
    try:
        captured = _capture_emit()
        um = {
            "id": "u1",
            "parentId": "p0",
            "childrenIds": [],
            "role": "user",
            "content": "hi",
            "files": [{"type": "image", "url": "/api/v1/files/f1/content", "id": "f1"}],
            "models": ["gpt-x"],
            "timestamp": 123,
        }
        asyncio.run(
            emit_chat_user_message(
                "user-1", "chat-1", "sess-9", "assistant-1", um, "p0"
            )
        )
        assert len(captured) == 1
        user_id, payload = captured[0]
        assert user_id == "user-1"
        # envelope keyed to the ASSISTANT id so receivers align with the stream
        assert payload["chat_id"] == "chat-1"
        assert payload["message_id"] == "assistant-1"
        assert payload["session_id"] == "sess-9"
        data = payload["data"]
        assert data["type"] == "chat:user-message"
        assert data["user_message"] == um
        assert data["assistant_message_id"] == "assistant-1"
        assert data["leaf_message_id"] == "p0"
        # routes to the stream room (not USER_POOL)
        assert _is_stream_scoped_payload(payload) is True
    finally:
        socket_main.emit_to_primary = original


def test_emit_skips_local_chat():
    original = socket_main.emit_to_primary
    try:
        captured = _capture_emit()
        asyncio.run(
            emit_chat_user_message(
                "user-1", "local:abc", "sess", "a1", {"id": "u1"}, None
            )
        )
        assert captured == []
    finally:
        socket_main.emit_to_primary = original


def test_emit_skips_when_user_message_has_no_id():
    original = socket_main.emit_to_primary
    try:
        captured = _capture_emit()
        asyncio.run(
            emit_chat_user_message("user-1", "chat-1", "sess", "a1", {}, None)
        )
        asyncio.run(
            emit_chat_user_message("user-1", "chat-1", "sess", "a1", None, None)
        )
        assert captured == []
    finally:
        socket_main.emit_to_primary = original


def test_emit_skips_oversized_payload():
    """A multi-MB data: image (e.g. screen capture) must NOT be pushed on the
    unbatched socket frame — the emit is skipped and the receiver's loadChat()
    backstop syncs the (already persisted) prompt instead."""
    original = socket_main.emit_to_primary
    try:
        captured = _capture_emit()
        big = "data:image/png;base64," + ("A" * (socket_main.SOCKET_BATCH_MAX_BYTES + 4096))
        um = {
            "id": "u1",
            "role": "user",
            "content": "shot",
            "files": [{"type": "image", "url": big}],
        }
        asyncio.run(
            emit_chat_user_message("user-1", "chat-1", "sess", "a1", um, "p0")
        )
        assert captured == []
    finally:
        socket_main.emit_to_primary = original


# --- assemble gating (regression: dead emit on the normal send path) --------


def test_persisted_out_set_when_user_message_already_persisted():
    """Regression guard for the dead-emit bug.

    A normal interactive send pre-persists the user row (append_message op +
    awaited save) BEFORE calling /api/chat/completions, so by the time
    ``assemble_conversation_from_leaf`` runs, ``new_id`` is ALREADY in
    ``messages_map``. The cross-device emit must STILL be surfaced via
    ``persisted_out`` — otherwise the prompt bubble never reaches other devices
    except after a full reload (exactly the gap this feature closes).
    """
    import open_webui.utils.chat as chatmod

    user_row = {
        "id": "u1",
        "parentId": None,
        "childrenIds": [],
        "role": "user",
        "content": "hello from the phone",
        "files": [],
        "models": ["gpt-x"],
        "timestamp": 111,
    }

    async def _fake_map(chat_id):
        return {"u1": dict(user_row)}

    original_map = chatmod.Chats.get_messages_map_by_chat_id
    chatmod.Chats.get_messages_map_by_chat_id = _fake_map
    try:
        out: dict = {}
        asyncio.run(
            chatmod.assemble_conversation_from_leaf(
                "chat-1",
                "u1",
                new_user_message={
                    "id": "u1",
                    "parentId": None,
                    "role": "user",
                    "content": "hello from the phone",
                },
                model=None,
                request=None,
                user=None,
                persisted_out=out,
            )
        )
        assert out.get("user_message") is not None, "emit row not surfaced"
        um = out["user_message"]
        assert um["id"] == "u1"
        assert um["content"] == "hello from the phone"
        assert um["role"] == "user"
        assert out["leaf_message_id"] == "u1"
    finally:
        chatmod.Chats.get_messages_map_by_chat_id = original_map


def test_persisted_out_not_set_without_new_user_message():
    """Regenerate / plain re-walk (no new_user_message) must NOT surface a row,
    so it never emits a spurious cross-device prompt."""
    import open_webui.utils.chat as chatmod

    async def _fake_map(chat_id):
        return {
            "u1": {
                "id": "u1",
                "parentId": None,
                "childrenIds": ["a1"],
                "role": "user",
                "content": "hi",
                "files": [],
            },
            "a1": {
                "id": "a1",
                "parentId": "u1",
                "childrenIds": [],
                "role": "assistant",
                "content": "hello",
            },
        }

    original_map = chatmod.Chats.get_messages_map_by_chat_id
    chatmod.Chats.get_messages_map_by_chat_id = _fake_map
    try:
        out: dict = {}
        asyncio.run(
            chatmod.assemble_conversation_from_leaf(
                "chat-1",
                "a1",
                new_user_message=None,
                model=None,
                request=None,
                user=None,
                persisted_out=out,
            )
        )
        assert out.get("user_message") is None
    finally:
        chatmod.Chats.get_messages_map_by_chat_id = original_map

