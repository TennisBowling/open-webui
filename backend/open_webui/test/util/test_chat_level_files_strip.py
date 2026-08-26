"""Item 4: chat-level ``files[*].file.data.content`` must be stripped to a falsy
empty string on the outbound (slim) projection — same treatment the per-message
slim path already applies — so a chat open does not re-ship the full extracted
document text (duplicated in the durable Files table) over a metered link.

Two surfaces are covered here without a DB:
 * ``strip_tool_result_bodies_from_chat_dict`` — the shared outbound projection
   used by the full ``GET /chats/{id}`` path (and every other full-chat API
   response), which now also strips the chat-level ``files`` array;
 * the strip stays ``''`` (falsy) so retrieval/utils.py's ``get_file_by_id``
   fallback still fires, and the SOURCE dict is never mutated.

The meta-response surface (``get_chat_meta_by_id_and_user_id``) reuses the same
``_strip_file_data_content_from_files`` helper; that helper's falsy-'' contract
is exercised directly below too.
"""

import copy

from test.util.db import configure_test_database

configure_test_database(required=False)

from open_webui.models.chats import (  # noqa: E402
    strip_tool_result_bodies_from_chat_dict,
    _strip_file_data_content_from_files,
    _SLIM_FILE_CONTENT_THRESHOLD_BYTES as THRESHOLD,
)


def _file_entry(file_id: str, content: str) -> dict:
    return {
        "type": "file",
        "id": file_id,
        "name": f"{file_id}.pdf",
        "file": {
            "id": file_id,
            "filename": f"{file_id}.pdf",
            "data": {"content": content, "metadata": {"k": "v"}},
        },
    }


def test_chat_dict_strips_chat_level_files_to_falsy_empty_string():
    big = "x" * (THRESHOLD + 500)
    chat = {
        "title": "t",
        "files": [_file_entry("f1", big)],
        "history": {"currentId": "m0", "messages": {}},
    }
    snapshot = copy.deepcopy(chat)

    out = strip_tool_result_bodies_from_chat_dict(chat)
    data = out["files"][0]["file"]["data"]

    assert data["content"] == ""
    assert not data["content"]  # falsy — the retrieval fallback contract
    assert data["content_stripped"] is True
    assert data["content_size"] == len(big.encode("utf-8"))
    # source dict untouched (projection deep-copies)
    assert chat == snapshot
    assert chat["files"][0]["file"]["data"]["content"] == big


def test_chat_dict_small_content_and_no_files_are_passthrough():
    small = "tiny"
    chat = {"files": [_file_entry("f1", small)], "history": {"messages": {}}}
    out = strip_tool_result_bodies_from_chat_dict(chat)
    assert out["files"][0]["file"]["data"]["content"] == small
    assert "content_stripped" not in out["files"][0]["file"]["data"]

    # no files key at all -> no files key injected
    chat_no_files = {"title": "t", "history": {"messages": {}}}
    out2 = strip_tool_result_bodies_from_chat_dict(chat_no_files)
    assert "files" not in out2


def test_collection_entry_without_inline_content_untouched():
    # A collection-type file reference carries no file.data.content; it must be
    # left exactly as-is (nothing to strip, no marker added).
    coll = {"type": "collection", "id": "c1", "name": "Docs"}
    chat = {"files": [coll], "history": {"messages": {}}}
    out = strip_tool_result_bodies_from_chat_dict(chat)
    assert out["files"][0] == coll


def test_strip_helper_returns_same_object_when_nothing_changed():
    files = [_file_entry("f1", "short"), {"type": "image", "url": "x"}]
    assert _strip_file_data_content_from_files(files) is files
