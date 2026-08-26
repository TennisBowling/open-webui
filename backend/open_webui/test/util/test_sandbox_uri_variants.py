"""Sandbox URI slash-variant handling for the view_image tool and the workspace
file-attachment scan.

`sandbox:` has no registered authority, so the number of slashes after the colon
is not semantically meaningful — `sandbox:/workspace/...`, `sandbox://workspace/...`,
`sandbox:///workspace/...` and `sandbox:workspace/...` all denote the same file.
These tests pin that every variant is parsed identically (the bug that broke the
frontend preview link was exactly this single-vs-multi-slash mismatch).

The async end-to-end test is driven with asyncio.run() rather than
@pytest.mark.asyncio because this environment has no pytest-asyncio plugin (such
tests are silently skipped). _sandbox_rel_path itself is pure/synchronous.
"""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest

from open_webui.utils import view_image_tool as view_image_module
from open_webui.utils.view_image_tool import (
    ViewImageError,
    ViewImageTools,
    _sandbox_rel_path,
)
from open_webui.utils import container_workspace as cw


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


# ---------------------------------------------------------------------------
# _sandbox_rel_path: every slash variant -> the same relative path
# ---------------------------------------------------------------------------

def test_sandbox_rel_path_all_slash_variants_equivalent():
    expected = "outputs/chart.png"
    assert _sandbox_rel_path("sandbox:/workspace/outputs/chart.png") == expected
    assert _sandbox_rel_path("sandbox://workspace/outputs/chart.png") == expected
    assert _sandbox_rel_path("sandbox:///workspace/outputs/chart.png") == expected
    assert _sandbox_rel_path("sandbox:workspace/outputs/chart.png") == expected
    # Case-insensitive scheme + surrounding whitespace tolerated.
    assert _sandbox_rel_path("  SANDBOX:/workspace/outputs/chart.png  ") == expected


def test_sandbox_rel_path_percent_decoded():
    assert _sandbox_rel_path("sandbox:/workspace/outputs/my%20file.png") == "outputs/my file.png"


def test_sandbox_rel_path_preserves_traversal_for_downstream_confinement():
    # _sandbox_rel_path only EXTRACTS the path; the ../ must survive so the
    # downstream relative_to(root) confinement can reject it. (It must not be
    # silently normalised away here.)
    assert _sandbox_rel_path("sandbox:/workspace/../secret.png") == "../secret.png"


def test_sandbox_rel_path_rejects_non_sandbox_and_empty_root():
    for bad in (
        "https://example.com/a.png",
        "/workspace/outputs/a.png",  # bare path, no scheme
        "sandbox:/workspaces/a.png",  # 'workspaces' is not the workspace root segment
        "file:///workspace/a.png",
    ):
        with pytest.raises(ViewImageError):
            _sandbox_rel_path(bad)
    # Root with no file part is an error regardless of slash count.
    for root_only in ("sandbox:/workspace", "sandbox://workspace", "sandbox:///workspace"):
        with pytest.raises(ViewImageError):
            _sandbox_rel_path(root_only)


# ---------------------------------------------------------------------------
# _SANDBOX_WORKSPACE_RE: the attachment scan also matches every variant
# ---------------------------------------------------------------------------

def test_sandbox_workspace_re_matches_every_variant():
    for href in (
        "sandbox:/workspace/outputs/a.txt",
        "sandbox://workspace/outputs/a.txt",
        "sandbox:///workspace/outputs/a.txt",
        "sandbox:workspace/outputs/a.txt",
    ):
        m = cw._SANDBOX_WORKSPACE_RE.search(f"see [a]({href})")
        assert m is not None, href
        assert m.group(1) == "outputs/a.txt", href


# ---------------------------------------------------------------------------
# End-to-end: a triple-slash sandbox image URL now resolves through view_image
# ---------------------------------------------------------------------------

class _FakeApp:
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
        return f"/api/v1/files/{kwargs['id']}/content"


class _FakeRequest:
    def __init__(self, data_root=""):
        self.app = _FakeApp(data_root)


def test_view_image_accepts_triple_slash_sandbox_url(tmp_path, monkeypatch):
    workspace = tmp_path / "chatA" / "workspace" / "outputs"
    workspace.mkdir(parents=True)
    (workspace / "chart.png").write_bytes(PNG_BYTES)

    stored = []

    def fake_upload_file(file_obj, filename, metadata):
        data = file_obj.read()
        stored.append({"filename": filename, "data": data})
        return len(data), f"stored/{filename}"

    async def fake_reclaim_outputs(*args, **kwargs):
        return None

    async def fake_insert_new_file(user_id, form_data):
        return SimpleNamespace(id=form_data.id, user_id=user_id)

    monkeypatch.setattr(view_image_module.Storage, "upload_file", fake_upload_file)
    monkeypatch.setattr(view_image_module.Files, "insert_new_file", fake_insert_new_file)
    monkeypatch.setattr(view_image_module, "_reclaim_outputs", fake_reclaim_outputs)

    result = asyncio.run(
        ViewImageTools().view_image(
            "sandbox:///workspace/outputs/chart.png",  # triple slash (was rejected)
            __request__=_FakeRequest(str(tmp_path)),
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
    )

    assert isinstance(result, dict), result
    assert result["vision_attachments"][0]["url"].startswith("/api/v1/files/")
    assert stored and stored[0]["data"] == PNG_BYTES
