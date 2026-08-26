"""Regression tests for access-controlled file caching and upload idempotency."""

import asyncio
import io
from types import SimpleNamespace

from test.util.db import configure_test_database

configure_test_database()

from open_webui.routers import files as files_router  # noqa: E402


class _UploadResult:
    def __init__(self, file_id: str):
        self.id = file_id

    def model_dump(self):
        return {"id": self.id, "filename": "note.txt"}


def _stored_file(tmp_path):
    path = tmp_path / "private.txt"
    path.write_text("private")
    return SimpleNamespace(
        id="file-1",
        user_id="user-1",
        filename="private.txt",
        path=str(path),
        content={"content": "private"},
        meta={"name": "private.txt", "content_type": "text/plain"},
    )


def _route_endpoint(path):
    return next(
        route.endpoint for route in files_router.router.routes if route.path == path
    )


def test_owner_file_response_is_not_stored(tmp_path, monkeypatch):
    stored_file = _stored_file(tmp_path)

    async def get_file(file_id):
        return stored_file

    monkeypatch.setattr(files_router.Files, "get_file_by_id", get_file)
    response = asyncio.run(
        _route_endpoint("/{id}/content")(
            "file-1",
            user=SimpleNamespace(id="user-1", role="user"),
            attachment=False,
            w=None,
        )
    )

    assert response.headers["cache-control"] == "private, no-store"


def test_shared_file_response_is_not_stored(tmp_path, monkeypatch):
    stored_file = _stored_file(tmp_path)

    async def authorize(share_id, file_id, user):
        return stored_file

    monkeypatch.setattr(files_router, "_authorize_shared_file", authorize)
    response = asyncio.run(
        files_router.get_shared_file_content_by_id(
            "share-1",
            "file-1",
            user=None,
            attachment=False,
            w=None,
        )
    )

    assert response.headers["cache-control"] == "private, no-store"


def test_legacy_file_response_is_not_stored(tmp_path, monkeypatch):
    stored_file = _stored_file(tmp_path)

    async def get_file(file_id):
        return stored_file

    monkeypatch.setattr(files_router.Files, "get_file_by_id", get_file)
    response = asyncio.run(
        _route_endpoint("/{id}/content/{file_name}")(
            "file-1",
            user=SimpleNamespace(id="user-1", role="user"),
        )
    )

    assert response.headers["cache-control"] == "private, no-store"


def test_losing_concurrent_upload_deletes_its_storage(monkeypatch):
    deleted_paths = []

    async def no_existing_upload(user_id, upload_id):
        return None

    async def return_concurrent_winner(user_id, form_data):
        assert form_data.id != "winning-file-id"
        return _UploadResult("winning-file-id")

    monkeypatch.setattr(
        files_router.Files,
        "get_file_by_user_id_and_upload_id",
        no_existing_upload,
    )
    monkeypatch.setattr(files_router.Files, "insert_new_file", return_concurrent_winner)
    monkeypatch.setattr(
        files_router.Storage,
        "upload_file",
        lambda stream, filename, tags: (4, "storage/losing-object"),
    )
    monkeypatch.setattr(files_router.Storage, "delete_file", deleted_paths.append)
    monkeypatch.setattr(
        files_router, "file_needs_extraction", lambda content_type, extension: False
    )

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(config=SimpleNamespace(ALLOWED_FILE_EXTENSIONS=[]))
        )
    )
    upload = SimpleNamespace(
        filename="note.txt",
        content_type="text/plain",
        file=io.BytesIO(b"data"),
    )
    user = SimpleNamespace(
        id="user-1",
        email="user@example.com",
        name="User",
    )

    result = asyncio.run(
        files_router.upload_file_handler(
            request,
            file=upload,
            metadata={"upload_id": "stable-retry-id"},
            process=False,
            process_in_background=False,
            user=user,
        )
    )

    assert result["id"] == "winning-file-id"
    assert deleted_paths == ["storage/losing-object"]


def test_successful_upload_does_not_delete_its_storage(monkeypatch):
    deleted_paths = []
    winner = _UploadResult("same-file-id")

    monkeypatch.setattr(files_router.Storage, "delete_file", deleted_paths.append)

    files_router._cleanup_losing_upload(
        winner,
        uploaded_id="same-file-id",
        uploaded_path="storage/winner",
    )

    assert deleted_paths == []
