from open_webui.utils.chat_transport import (
    is_persisted_chat_generation,
    should_attach_chat_event_transport,
)


def test_saved_chat_generation_does_not_depend_on_socket_session():
    metadata = {
        "chat_id": "chat-1",
        "message_id": "assistant-1",
        "session_id": None,
    }

    assert is_persisted_chat_generation(metadata)
    assert should_attach_chat_event_transport(metadata)


def test_local_chat_without_session_keeps_direct_transport():
    metadata = {
        "chat_id": "local:temporary",
        "message_id": "assistant-1",
        "session_id": None,
    }

    assert not is_persisted_chat_generation(metadata)
    assert not should_attach_chat_event_transport(metadata)


def test_session_scoped_local_chat_can_still_emit_events():
    metadata = {
        "chat_id": "local:temporary",
        "message_id": "assistant-1",
        "session_id": "socket-1",
    }

    assert not is_persisted_chat_generation(metadata)
    assert should_attach_chat_event_transport(metadata)


def test_openai_compatible_request_without_chat_identity_stays_direct():
    metadata = {
        "chat_id": None,
        "message_id": None,
        "session_id": None,
    }

    assert not is_persisted_chat_generation(metadata)
    assert not should_attach_chat_event_transport(metadata)
