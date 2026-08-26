import asyncio
from types import SimpleNamespace

from open_webui.utils.response_durability import (
    content_blocks_from_message,
    is_selection_metadata_only_completion,
    text_content_blocks,
)


def test_selection_metadata_event_can_take_the_compact_path():
    assert is_selection_metadata_only_completion(
        {"selected_model_id": "gemini", "usage": {"completion_tokens": 5}}
    )


def test_nonstreaming_answer_with_selected_model_is_not_metadata_only():
    assert not is_selection_metadata_only_completion(
        {
            "selected_model_id": "gemini",
            "content": "Durable answer",
            "done": True,
        }
    )


def test_nonstreaming_choices_with_selected_model_are_not_dropped():
    assert not is_selection_metadata_only_completion(
        {
            "selected_model_id": "gemini",
            "choices": [{"message": {"content": "Durable answer"}}],
        }
    )


def test_legacy_content_has_a_canonical_snapshot_projection():
    expected = [{"type": "text", "content": "Durable answer"}]
    assert text_content_blocks("Durable answer") == expected
    assert content_blocks_from_message({"content": "Durable answer"}) == expected


def test_existing_content_blocks_remain_authoritative():
    blocks = [
        {"type": "reasoning", "content": "Work"},
        {"type": "text", "content": "Answer"},
    ]
    assert (
        content_blocks_from_message({"content": "legacy", "content_blocks": blocks})
        == blocks
    )


def test_v21_translator_passes_nonstreaming_selected_model_answer_through():
    from open_webui.socket.main import clear_stream_state
    from open_webui.utils.middleware import _wrap_event_emitter_v21

    async def run():
        delivered = []

        async def inner(event):
            delivered.append(event)

        payload = {
            "type": "chat:completion",
            "data": {
                "selected_model_id": "gemini",
                "content": "Durable answer",
                "done": True,
            },
        }
        emitter = _wrap_event_emitter_v21(
            inner,
            {
                "message_id": "nonstreaming-selected-model-test",
                "chat_id": "chat-1",
                "user_id": "user-1",
                "session_id": None,
            },
        )
        try:
            await emitter(payload)
            assert delivered == [payload]
        finally:
            clear_stream_state("nonstreaming-selected-model-test", delay=0)
            await asyncio.sleep(0)

    asyncio.run(run())


def test_terminal_snapshot_recovers_legacy_text(monkeypatch):
    from open_webui.routers import streams

    async def run():
        async def get_chat(chat_id, user_id):
            return SimpleNamespace(id=chat_id, user_id=user_id)

        async def get_message(chat_id, message_id):
            return {
                "id": message_id,
                "role": "assistant",
                "content": "Durable answer",
                "done": True,
            }

        monkeypatch.setattr(streams.Chats, "get_chat_by_id_and_user_id", get_chat)
        monkeypatch.setattr(
            streams.Chats, "get_message_by_id_and_message_id", get_message
        )
        monkeypatch.setattr(streams, "get_stream_state", lambda _message_id: {})
        monkeypatch.setattr(streams, "stream_run_get", lambda _message_id: 0)
        monkeypatch.setattr(streams, "STREAM_VERSION", {})

        snapshot = await streams.get_stream_snapshot(
            "assistant-1",
            chat_id="chat-1",
            user=SimpleNamespace(id="user-1"),
        )

        assert snapshot["status"] == "done"
        assert snapshot["content_blocks"] == [
            {"type": "text", "content": "Durable answer"}
        ]

    asyncio.run(run())
