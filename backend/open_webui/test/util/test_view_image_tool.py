from __future__ import annotations

from types import SimpleNamespace
import base64

import pytest

from open_webui.utils import view_image_tool as view_image_module
from open_webui.utils.middleware import _should_enable_view_image_tool
from open_webui.utils.view_image_tool import ViewImageTools


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class FakeApp:
    def __init__(self, data_root=""):
        self.state = SimpleNamespace(
            http_session=None,
            config=SimpleNamespace(
                ENABLE_CONTAINER_WORKSPACE_SYNC=True,
                CONTAINER_DATA_ROOT=data_root,
                CONTAINER_MCP_SERVER_ID="container",
            ),
        )

    def url_path_for(self, name: str, **kwargs):
        assert name == "get_file_content_by_id"
        return f"/api/v1/files/{kwargs['id']}/content"


class FakeRequest:
    def __init__(self, data_root=""):
        self.app = FakeApp(data_root)


def _patch_file_store(monkeypatch):
    stored = []

    async def fake_reclaim_outputs(*args, **kwargs):
        return None

    def fake_upload_file(file_obj, filename, metadata):
        data = file_obj.read()
        stored.append({"filename": filename, "data": data, "metadata": metadata})
        return len(data), f"stored/{filename}"

    def fake_insert_new_file(user_id, form_data):
        return SimpleNamespace(id=form_data.id, user_id=user_id)

    monkeypatch.setattr(view_image_module.Storage, "upload_file", fake_upload_file)
    monkeypatch.setattr(view_image_module.Files, "insert_new_file", fake_insert_new_file)
    monkeypatch.setattr(view_image_module, "_reclaim_outputs", fake_reclaim_outputs)
    return stored


@pytest.mark.asyncio
async def test_view_image_accepts_safe_sandbox_image(tmp_path, monkeypatch):
    workspace = tmp_path / "chatA" / "workspace" / "outputs"
    workspace.mkdir(parents=True)
    (workspace / "chart.png").write_bytes(PNG_BYTES)
    stored = _patch_file_store(monkeypatch)

    result = await ViewImageTools().view_image(
        "sandbox:/workspace/outputs/chart.png",
        detail="high",
        __request__=FakeRequest(str(tmp_path)),
        __user__={"id": "user-1", "email": "u@example.com", "name": "User"},
        __metadata__={
            "chat_id": "chatA",
            "container_workspace": {
                "chat_id": "chatA",
                "data_root": str(tmp_path),
                "server_id": "container",
            },
        },
    )

    assert isinstance(result, dict)
    assert result["content"].startswith("Image attached for visual inspection")
    assert result["vision_attachments"][0]["url"].startswith("/api/v1/files/")
    assert result["vision_attachments"][0]["detail"] == "high"
    assert stored[0]["data"] == PNG_BYTES


@pytest.mark.asyncio
async def test_view_image_rejects_sandbox_traversal(tmp_path, monkeypatch):
    _patch_file_store(monkeypatch)

    result = await ViewImageTools().view_image(
        "sandbox:/workspace/../secret.png",
        __request__=FakeRequest(str(tmp_path)),
        __user__={"id": "user-1"},
        __metadata__={
            "chat_id": "chatA",
            "container_workspace": {"chat_id": "chatA", "data_root": str(tmp_path)},
        },
    )

    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "escapes" in result


@pytest.mark.asyncio
async def test_view_image_rejects_sandbox_symlink(tmp_path, monkeypatch):
    root = tmp_path / "chatA" / "workspace"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_BYTES)
    (root / "link.png").symlink_to(outside)
    _patch_file_store(monkeypatch)

    result = await ViewImageTools().view_image(
        "sandbox:/workspace/link.png",
        __request__=FakeRequest(str(tmp_path)),
        __user__={"id": "user-1"},
        __metadata__={
            "chat_id": "chatA",
            "container_workspace": {"chat_id": "chatA", "data_root": str(tmp_path)},
        },
    )

    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "symlink" in result


@pytest.mark.asyncio
async def test_view_image_accepts_mocked_public_image_url(monkeypatch):
    stored = _patch_file_store(monkeypatch)

    async def fake_fetch(_request, source):
        return PNG_BYTES, "image/png", "remote.png"

    monkeypatch.setattr(view_image_module, "_fetch_web_image_bytes", fake_fetch)

    result = await ViewImageTools().view_image(
        "https://example.com/remote.png",
        __request__=FakeRequest(),
        __user__={"id": "user-1"},
        __metadata__={"chat_id": "chatA"},
    )

    assert isinstance(result, dict)
    assert result["vision_attachments"][0]["source"] == "https://example.com/remote.png"
    assert stored[0]["filename"].endswith("remote.png")


@pytest.mark.asyncio
async def test_view_image_rejects_mocked_non_image_url(monkeypatch):
    _patch_file_store(monkeypatch)

    async def fake_fetch(_request, source):
        return b"<html>not an image</html>", "text/html", "not-image.png"

    monkeypatch.setattr(view_image_module, "_fetch_web_image_bytes", fake_fetch)

    result = await ViewImageTools().view_image(
        "https://example.com/not-image.png",
        __request__=FakeRequest(),
        __user__={"id": "user-1"},
        __metadata__={"chat_id": "chatA"},
    )

    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "supported image" in result


@pytest.mark.asyncio
async def test_view_image_rejects_mocked_oversized_url(monkeypatch):
    _patch_file_store(monkeypatch)

    async def fake_fetch(_request, source):
        raise view_image_module.ViewImageError("image is too large (limit 20 MB)")

    monkeypatch.setattr(view_image_module, "_fetch_web_image_bytes", fake_fetch)

    result = await ViewImageTools().view_image(
        "https://example.com/huge.png",
        __request__=FakeRequest(),
        __user__={"id": "user-1"},
        __metadata__={"chat_id": "chatA"},
    )

    assert result == "Error: image is too large (limit 20 MB)"


def test_view_image_activation_requires_vision_model():
    request = FakeRequest("/tmp/root")
    metadata = {"chat_id": "chatA"}
    model = {"info": {"meta": {"capabilities": {"vision": False}}}}

    assert not _should_enable_view_image_tool(
        request, model, metadata, ["builtin:web_search"]
    )


def test_view_image_activation_requires_web_or_container_tool():
    request = FakeRequest("/tmp/root")
    model = {"info": {"meta": {"capabilities": {"vision": True}}}}

    assert _should_enable_view_image_tool(
        request, model, {"chat_id": "chatA"}, ["builtin:web_search"]
    )
    assert not _should_enable_view_image_tool(request, model, {"chat_id": "chatA"}, [])
    assert _should_enable_view_image_tool(
        request,
        model,
        {"chat_id": "chatA"},
        ["server:mcp:container"],
    )
