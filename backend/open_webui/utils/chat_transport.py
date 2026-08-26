"""Transport policy for chat generations.

Socket presence is a delivery detail, not a persistence/lifecycle decision.
Saved-chat generations always run as registered background work so their
assistant row, stream state, and terminal result remain recoverable across
disconnects and reloads. Temporary/local and OpenAI-compatible requests without
a saved chat identity keep the direct response path.
"""

from typing import Any, Mapping


def is_persisted_chat_generation(metadata: Mapping[str, Any]) -> bool:
    chat_id = str(metadata.get("chat_id") or "")
    message_id = str(metadata.get("message_id") or "")
    return bool(chat_id and message_id and not chat_id.startswith("local:"))


def should_attach_chat_event_transport(metadata: Mapping[str, Any]) -> bool:
    chat_id = str(metadata.get("chat_id") or "")
    message_id = str(metadata.get("message_id") or "")
    if not chat_id or not message_id:
        return False

    return bool(
        metadata.get("session_id")
        or metadata.get("headless")
        or is_persisted_chat_generation(metadata)
    )
