"""Unit tests for slim-projection stripping of uploaded-document text.

``files[*].file.data.content`` holds the full extracted text of an attached
document. It is duplicated in the durable Files table, so re-shipping it on
every chat open is pure waste on a low-bandwidth link. ``_project_message_slim``
now strips large inline content on the read path.

The load-bearing contract these tests guard:

* stripped content becomes an EMPTY STRING (falsy) — NOT a marker string — so the
  completion context-assembler's ``if file.data.content: ... else get_file_by_id``
  fallback (retrieval/utils.py) still re-reads the full text from the Files table;
* the SOURCE message is never mutated (the projection must not corrupt the
  cached/DB-layer dict it was handed);
* small inline content and non-file entries are left untouched.

Pure model-layer logic — no DB, sockets, or Postgres required.
"""

import copy

from test.util.db import configure_test_database

configure_test_database(required=False)

from open_webui.models.chats import (  # noqa: E402
    _project_message_slim,
    _strip_file_data_content_from_message,
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


def test_large_content_stripped_to_falsy_empty_string():
    big = "x" * (THRESHOLD + 500)
    msg = {"id": "m1", "role": "user", "files": [_file_entry("f1", big)]}

    out = _strip_file_data_content_from_message(msg)
    data = out["files"][0]["file"]["data"]

    assert data["content"] == ""  # empty
    assert not data["content"]  # falsy — the retrieval fallback contract
    assert data["content_stripped"] is True
    assert data["content_size"] == len(big.encode("utf-8"))
    # metadata is preserved so the file chip still renders
    assert data["metadata"] == {"k": "v"}


def test_small_content_preserved():
    small = "tiny inline note"
    msg = {"id": "m1", "role": "user", "files": [_file_entry("f1", small)]}

    out = _strip_file_data_content_from_message(msg)
    data = out["files"][0]["file"]["data"]

    assert data["content"] == small
    assert "content_stripped" not in data


def test_source_message_not_mutated():
    big = "y" * (THRESHOLD + 10)
    msg = {"id": "m1", "role": "user", "files": [_file_entry("f1", big)]}
    snapshot = copy.deepcopy(msg)

    _strip_file_data_content_from_message(msg)

    assert msg == snapshot  # projection returns a copy, never edits in place
    assert msg["files"][0]["file"]["data"]["content"] == big


def test_non_file_and_edge_entries_untouched():
    big = "z" * (THRESHOLD + 1)
    msg = {
        "id": "m1",
        "files": [
            {"type": "image", "url": "data:image/png;base64,AAAA"},
            "not-a-dict",
            {"type": "file", "id": "f3"},  # no file/data keys
            _file_entry("f4", big),
        ],
    }

    out = _strip_file_data_content_from_message(msg)

    assert out["files"][0] == {"type": "image", "url": "data:image/png;base64,AAAA"}
    assert out["files"][1] == "not-a-dict"
    assert out["files"][2] == {"type": "file", "id": "f3"}
    assert out["files"][3]["file"]["data"]["content"] == ""


def test_no_files_passthrough():
    msg = {"id": "m1", "role": "assistant", "content": "hello"}
    assert _strip_file_data_content_from_message(msg) is msg


def test_project_message_slim_strips_file_content_on_current_branch():
    # The tail loader calls _project_message_slim with is_current_branch=True,
    # so file stripping must fire regardless of branch.
    big = "w" * (THRESHOLD + 100)
    msg = {
        "id": "m1",
        "role": "user",
        "content": "see attached",
        "files": [_file_entry("f1", big)],
    }

    out = _project_message_slim(msg, is_current_leaf=True, is_current_branch=True)

    assert out["files"][0]["file"]["data"]["content"] == ""
    assert out["files"][0]["file"]["data"]["content_stripped"] is True
    # original untouched
    assert msg["files"][0]["file"]["data"]["content"] == big
