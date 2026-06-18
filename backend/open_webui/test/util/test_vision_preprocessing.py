"""Unit tests for server-side vision/PDF preprocessing (the headless port of the
client's preprocessing path).

Verifies:
* non-vision model + configured preprocessor → user message content is rewritten
  with the OCR analysis, and the idempotency flag is set,
* vision-capable model → no preprocessing (content untouched),
* no preprocessor configured → no preprocessing,
* idempotency: a message already flagged vision_processed/pdf_processed is not
  rewritten again (no double-prepend),
* image OCR failure degrades to text-only (content unchanged, no raise),
* PDF OCR failure raises (so the drain PAUSES).

The OCR model call (``generate_chat_completion``) is monkeypatched — these are
pure logic tests, no network or DB.
"""

import asyncio

import pytest

chat_utils = pytest.importorskip("open_webui.utils.chat")


class _FakeUser:
    id = "user-1"


def _patch_ocr(monkeypatch, response_text="OCR RESULT", raises=None):
    async def _fake_generate(request, form_data, user, bypass_filter=False):
        if raises is not None:
            raise raises
        return {"choices": [{"message": {"content": response_text}}]}

    monkeypatch.setattr(chat_utils, "generate_chat_completion", _fake_generate)


def _patch_persist_noop(monkeypatch):
    # Don't hit the DB; just confirm the in-place mutation of the message.
    monkeypatch.setattr(
        chat_utils.Chats,
        "upsert_message_to_chat_by_id_and_message_id",
        lambda *a, **k: None,
    )


_NONVISION_MODEL = {
    "info": {
        "meta": {
            "capabilities": {"vision": False},
            "vision_preprocessor_model_id": "ocr-model",
        }
    }
}
_VISION_MODEL = {"info": {"meta": {"capabilities": {"vision": True}}}}


def _img_message():
    return {
        "id": "u1",
        "role": "user",
        "content": "what is in this image?",
        "files": [{"type": "image", "id": "img1", "url": "/api/v1/files/img1/content"}],
    }


def _pdf_message():
    return {
        "id": "u1",
        "role": "user",
        "content": "summarize this",
        "files": [{"type": "file", "id": "pdf1", "name": "doc.pdf", "url": "/api/v1/files/pdf1/content"}],
    }


def test_image_preprocessing_rewrites_content(monkeypatch):
    _patch_ocr(monkeypatch, "A red square")
    _patch_persist_noop(monkeypatch)
    msg = _img_message()
    asyncio.run(
        chat_utils.preprocess_nonvision_files(
            object(), _FakeUser(), "chat-1", msg, _NONVISION_MODEL
        )
    )
    assert msg["vision_processed"] is True
    assert msg["content"].startswith("[Vision Analysis:\nA red square\n]")
    assert "what is in this image?" in msg["content"]


def test_pdf_preprocessing_rewrites_content(monkeypatch):
    _patch_ocr(monkeypatch, "Quarterly report")
    _patch_persist_noop(monkeypatch)
    msg = _pdf_message()
    asyncio.run(
        chat_utils.preprocess_nonvision_files(
            object(), _FakeUser(), "chat-1", msg, _NONVISION_MODEL
        )
    )
    assert msg["pdf_processed"] is True
    assert msg["content"].startswith("[PDF Analysis (1 pages):\nQuarterly report\n]")
    assert "summarize this" in msg["content"]


def test_vision_model_is_not_preprocessed(monkeypatch):
    _patch_ocr(monkeypatch, "should not run")
    _patch_persist_noop(monkeypatch)
    msg = _img_message()
    original = msg["content"]
    asyncio.run(
        chat_utils.preprocess_nonvision_files(
            object(), _FakeUser(), "chat-1", msg, _VISION_MODEL
        )
    )
    assert msg["content"] == original
    assert "vision_processed" not in msg


def test_no_preprocessor_configured_is_skipped(monkeypatch):
    _patch_ocr(monkeypatch, "should not run")
    _patch_persist_noop(monkeypatch)
    model = {"info": {"meta": {"capabilities": {"vision": False}}}}  # no preprocessor id
    msg = _img_message()
    original = msg["content"]
    asyncio.run(
        chat_utils.preprocess_nonvision_files(
            object(), _FakeUser(), "chat-1", msg, model
        )
    )
    assert msg["content"] == original


def test_idempotent_already_processed_image(monkeypatch):
    _patch_ocr(monkeypatch, "SECOND RUN")
    _patch_persist_noop(monkeypatch)
    msg = _img_message()
    msg["vision_processed"] = True
    msg["content"] = "[Vision Analysis:\nFIRST\n]\n\nwhat is in this image?"
    before = msg["content"]
    asyncio.run(
        chat_utils.preprocess_nonvision_files(
            object(), _FakeUser(), "chat-1", msg, _NONVISION_MODEL
        )
    )
    # No double-prepend.
    assert msg["content"] == before
    assert msg["content"].count("[Vision Analysis:") == 1


def test_image_ocr_failure_degrades_to_text_only(monkeypatch):
    _patch_ocr(monkeypatch, raises=RuntimeError("provider down"))
    _patch_persist_noop(monkeypatch)
    msg = _img_message()
    original = msg["content"]
    # Must NOT raise — images degrade gracefully.
    asyncio.run(
        chat_utils.preprocess_nonvision_files(
            object(), _FakeUser(), "chat-1", msg, _NONVISION_MODEL
        )
    )
    assert msg["content"] == original
    assert msg["vision_processed"] is False


def test_pdf_ocr_failure_raises(monkeypatch):
    _patch_ocr(monkeypatch, raises=RuntimeError("provider down"))
    _patch_persist_noop(monkeypatch)
    msg = _pdf_message()
    # PDF failure is fatal — must raise so the drain PAUSES.
    with pytest.raises(RuntimeError):
        asyncio.run(
            chat_utils.preprocess_nonvision_files(
                object(), _FakeUser(), "chat-1", msg, _NONVISION_MODEL
            )
        )
