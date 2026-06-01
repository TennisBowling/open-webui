import json

import pytest

chat_utils = pytest.importorskip("open_webui.utils.chat")


def _assemble(monkeypatch, files, container_workspace_active=True):
    monkeypatch.setattr(
        chat_utils.Chats,
        "get_messages_map_by_chat_id",
        lambda _chat_id: {
            "user-1": {
                "id": "user-1",
                "parentId": None,
                "role": "user",
                "content": "inspect these files",
                "files": files,
            }
        },
    )
    return chat_utils.assemble_conversation_from_leaf(
        "chat-1",
        "user-1",
        model={"info": {"meta": {"capabilities": {"vision": True}}}},
        container_workspace_active=container_workspace_active,
    )


def test_container_workspace_keeps_image_vision_part_without_document_parts(
    monkeypatch,
):
    out = _assemble(
        monkeypatch,
        [
            {
                "type": "image",
                "id": "image-file",
                "name": "plot.png",
                "url": "/api/v1/files/image-file/content",
            },
            {
                "type": "file",
                "id": "pdf-file",
                "name": "paper.pdf",
                "url": "/api/v1/files/pdf-file/content",
            },
            {
                "type": "file",
                "id": "doc-file",
                "name": "report.docx",
                "url": "/api/v1/files/doc-file/content",
            },
        ],
        container_workspace_active=True,
    )

    assert len(out) == 1
    parts = out[0]["content"]
    assert parts == [
        {"type": "text", "text": "inspect these files"},
        {
            "type": "image_url",
            "image_url": {"url": "/api/v1/files/image-file/content"},
        },
        {
            "type": "file",
            "file": {
                "filename": "paper.pdf",
                "file_data": "/api/v1/files/pdf-file/content",
            },
        },
    ]
    assert "report.docx" not in json.dumps(parts)


def test_container_workspace_pdf_message_keeps_native_pdf_file_part(monkeypatch):
    out = _assemble(
        monkeypatch,
        [
            {
                "type": "file",
                "id": "pdf-file",
                "name": "paper.pdf",
                "url": "/api/v1/files/pdf-file/content",
            },
            {
                "type": "file",
                "id": "doc-file",
                "name": "report.docx",
                "url": "/api/v1/files/doc-file/content",
            },
        ],
        container_workspace_active=True,
    )

    assert out == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect these files"},
                {
                    "type": "file",
                    "file": {
                        "filename": "paper.pdf",
                        "file_data": "/api/v1/files/pdf-file/content",
                    },
                },
            ],
        }
    ]


def test_container_workspace_office_document_only_message_stays_text_only(monkeypatch):
    out = _assemble(
        monkeypatch,
        [
            {
                "type": "file",
                "id": "doc-file",
                "name": "report.docx",
                "url": "/api/v1/files/doc-file/content",
            },
        ],
        container_workspace_active=True,
    )

    assert out == [{"role": "user", "content": "inspect these files"}]


def test_non_container_workspace_keeps_existing_image_and_file_parts(monkeypatch):
    out = _assemble(
        monkeypatch,
        [
            {
                "type": "image",
                "id": "image-file",
                "name": "plot.png",
                "url": "/api/v1/files/image-file/content",
            },
            {
                "type": "file",
                "id": "pdf-file",
                "name": "paper.pdf",
                "url": "/api/v1/files/pdf-file/content",
            },
            {
                "type": "file",
                "id": "doc-file",
                "name": "report.docx",
                "url": "/api/v1/files/doc-file/content",
            },
        ],
        container_workspace_active=False,
    )

    parts = out[0]["content"]
    assert [part["type"] for part in parts] == ["text", "image_url", "file", "file"]
    assert parts[1]["image_url"] == {"url": "/api/v1/files/image-file/content"}
    assert parts[2]["file"]["filename"] == "paper.pdf"
    assert parts[3]["file"]["filename"] == "report.docx"
    assert parts[3]["file"]["processing_mode"] == "text"
