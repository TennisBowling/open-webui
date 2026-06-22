"""Regression + hardening tests for the tool-result image/file path.

Root incident: a `browser_snapshot` MCP tool returns an `image/png` block. Persisting
it went `process_tool_result` -> `get_file_url_from_base64` -> `get_image_url_from_base64`
-> `upload_image`, and `upload_image` did `file_item.id`. But `upload_file_handler` was
changed to return a **dict** (`{"status": True, **model_dump()}`), so `.id` raised
``'dict' object has no attribute 'id'``. That raise was unguarded at the
`process_tool_result` call sites, so it tore down the whole turn (and each subagent that
hit a browser image tool errored "after retry"). See the gyms-research chat forensics.

These were sync tests, but the async runtime migration made ``process_tool_result``,
``upload_image``, ``upload_audio`` and the ``get_*_url_from_base64`` helpers
coroutines (they ``await`` the DB/file handlers). The calls are therefore driven
with ``asyncio.run(...)`` and any monkeypatched helper that production awaits is an
``async def`` so the await resolves. ``uploaded_file_id`` stays synchronous.
"""

import asyncio
import tempfile
import types

from test.util.db import configure_test_database

configure_test_database()

import pytest  # noqa: E402

from open_webui.routers.files import uploaded_file_id  # noqa: E402
from open_webui.routers import images as images_mod  # noqa: E402
from open_webui.utils import files as utils_files_mod  # noqa: E402
from open_webui.utils import middleware as mw  # noqa: E402


# --------------------------------------------------------------------------- #
# uploaded_file_id: accept dict OR model, raise cleanly on bad input
# --------------------------------------------------------------------------- #
def test_uploaded_file_id_accepts_dict():
    # This is the exact shape upload_file_handler returns today.
    assert uploaded_file_id({"status": True, "id": "abc123", "filename": "x"}) == "abc123"


def test_uploaded_file_id_accepts_model_like():
    obj = types.SimpleNamespace(id="def456")
    assert uploaded_file_id(obj) == "def456"


def test_uploaded_file_id_raises_on_none():
    with pytest.raises(ValueError):
        uploaded_file_id(None)


def test_uploaded_file_id_raises_on_missing_id():
    with pytest.raises(ValueError):
        uploaded_file_id({"status": True})  # no id key
    with pytest.raises(ValueError):
        uploaded_file_id({"status": True, "id": ""})  # empty id


# --------------------------------------------------------------------------- #
# upload_image / upload_audio must NOT crash on the dict-returning handler.
# This is the direct regression test for 'dict' object has no attribute 'id'.
# --------------------------------------------------------------------------- #
class _FakeApp:
    def __init__(self):
        self.state = types.SimpleNamespace()

    def url_path_for(self, name, id):
        return f"/api/v1/files/{id}/content"


class _FakeRequest:
    def __init__(self):
        self.app = _FakeApp()


class _FakeUser:
    id = "user-1"
    email = "u@example.com"
    name = "U"
    role = "user"


def test_upload_image_handles_dict_return(monkeypatch):
    captured = {}

    async def fake_handler(request, file, metadata, process, user, **kwargs):
        # Mirror the real return shape: a dict, NOT a FileModel.
        captured["filename"] = file.filename
        return {"status": True, "id": "img-999", "filename": "generated"}

    monkeypatch.setattr(images_mod, "upload_file_handler", fake_handler)

    url = asyncio.run(
        images_mod.upload_image(
            _FakeRequest(), b"\x89PNG\r\n", "image/png", {"chat_id": "c"}, _FakeUser()
        )
    )
    assert url == "/api/v1/files/img-999/content"


def test_upload_audio_handles_dict_return(monkeypatch):
    async def fake_handler(request, file, metadata, process, user, **kwargs):
        return {"status": True, "id": "aud-111", "filename": "generated"}

    monkeypatch.setattr(utils_files_mod, "upload_file_handler", fake_handler)

    url = asyncio.run(
        utils_files_mod.upload_audio(
            _FakeRequest(), b"RIFF....", "audio/wav", {"chat_id": "c"}, _FakeUser()
        )
    )
    assert url == "/api/v1/files/aud-111/content"


# --------------------------------------------------------------------------- #
# get_image_url_from_base64 mime broadening: jpeg/webp must not be dropped.
# --------------------------------------------------------------------------- #
def test_get_image_url_accepts_non_png(monkeypatch):
    seen = {}

    async def fake_upload_image(request, image_data, content_type, metadata, user):
        seen["content_type"] = content_type
        return "/api/v1/files/jpeg-1/content"

    monkeypatch.setattr(utils_files_mod, "upload_image", fake_upload_image)

    # a 1x1 jpeg header is enough; load_b64_image_data only needs valid base64
    import base64

    payload = base64.b64encode(b"\xff\xd8\xff\xe0jpegbytes").decode()
    url = asyncio.run(
        utils_files_mod.get_image_url_from_base64(
            _FakeRequest(), f"data:image/jpeg;base64,{payload}", {}, _FakeUser()
        )
    )
    assert url == "/api/v1/files/jpeg-1/content"
    assert seen["content_type"] == "image/jpeg"


def test_get_file_url_routes_image_and_audio(monkeypatch):
    async def fake_image_url(*a, **k):
        return "IMG"

    async def fake_audio_url(*a, **k):
        return "AUD"

    monkeypatch.setattr(utils_files_mod, "get_image_url_from_base64", fake_image_url)
    monkeypatch.setattr(utils_files_mod, "get_audio_url_from_base64", fake_audio_url)
    assert (
        asyncio.run(
            utils_files_mod.get_file_url_from_base64(
                None, "data:image/webp;base64,AAAA", {}, None
            )
        )
        == "IMG"
    )
    assert (
        asyncio.run(
            utils_files_mod.get_file_url_from_base64(
                None, "data:audio/mpeg;base64,AAAA", {}, None
            )
        )
        == "AUD"
    )
    # Unsupported scheme -> None (no crash)
    assert (
        asyncio.run(
            utils_files_mod.get_file_url_from_base64(
                None, "data:application/pdf;base64,AAAA", {}, None
            )
        )
        is None
    )


# --------------------------------------------------------------------------- #
# process_tool_result: MCP image happy path, raise-is-best-effort, no None urls.
# --------------------------------------------------------------------------- #
def _mcp_image_result():
    # An MCP tool result list as browser_snapshot returns: [text, image].
    return [
        {"type": "text", "text": "snapshot text"},
        {"type": "image", "mimeType": "image/png", "data": "QUJD"},
    ]


def test_process_tool_result_image_happy_path(monkeypatch):
    async def fake_file_url(*a, **k):
        return "/files/shot-1/content"

    monkeypatch.setattr(mw, "get_file_url_from_base64", fake_file_url)
    (
        tool_result,
        files,
        embeds,
        vision,
        meta,
    ) = asyncio.run(
        mw.process_tool_result(
            _FakeRequest(),
            "browser_snapshot",
            _mcp_image_result(),
            "mcp",
            metadata={"chat_id": "c", "message_id": "m"},
            user=_FakeUser(),
            model={"info": {"meta": {"capabilities": {"vision": True}}}},
        )
    )
    assert any(
        f.get("url") == "/files/shot-1/content" for f in files
    ), "image attachment should be persisted"
    assert vision and vision[0]["url"] == "/files/shot-1/content"
    assert "snapshot text" in tool_result


def test_process_tool_result_image_persist_failure_is_best_effort(monkeypatch):
    async def boom(*a, **k):
        raise AttributeError("'dict' object has no attribute 'id'")

    monkeypatch.setattr(mw, "get_file_url_from_base64", boom)

    # MUST NOT raise — the text result is preserved, attachment dropped.
    (
        tool_result,
        files,
        embeds,
        vision,
        meta,
    ) = asyncio.run(
        mw.process_tool_result(
            _FakeRequest(),
            "browser_snapshot",
            _mcp_image_result(),
            "mcp",
            metadata={"chat_id": "c", "message_id": "m"},
            user=_FakeUser(),
            model={"info": {"meta": {"capabilities": {"vision": True}}}},
        )
    )
    assert files == [], "failed image upload must not leave a broken file entry"
    assert vision == [], "failed image upload must not leave a broken vision attachment"
    assert "snapshot text" in tool_result, "text content must survive"


def test_process_tool_result_image_none_url_not_appended(monkeypatch):
    # Unsupported mime -> helper returns None -> no {"url": None} entry.
    async def none_url(*a, **k):
        return None

    monkeypatch.setattr(mw, "get_file_url_from_base64", none_url)
    _, files, _, vision, _ = asyncio.run(
        mw.process_tool_result(
            _FakeRequest(),
            "browser_snapshot",
            _mcp_image_result(),
            "mcp",
            metadata={"chat_id": "c", "message_id": "m"},
            user=_FakeUser(),
            model=None,
        )
    )
    assert files == []
    assert vision == []
