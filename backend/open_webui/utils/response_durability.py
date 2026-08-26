"""Pure helpers for durable chat-response handoff.

Terminal delivery is allowed to happen only after the canonical response can be
reconstructed from storage. These helpers keep the legacy text response and the
v2.1 content-block snapshot paths aligned without importing the chat middleware.
"""

from typing import Any, Mapping, Optional


def is_selection_metadata_only_completion(data: Any) -> bool:
    """Return whether a completion payload contains only model/usage metadata.

    A non-streaming provider response may carry ``selected_model_id`` alongside
    ``choices``, ``content`` or ``done``. Treating that as a selection-only event
    drops the actual answer and terminal signal.
    """

    if not isinstance(data, Mapping) or "selected_model_id" not in data:
        return False

    response_keys = {"choices", "content", "content_blocks", "done", "error"}
    return response_keys.isdisjoint(data.keys())


def text_content_blocks(content: Any) -> list[dict[str, Any]]:
    """Project a legacy text answer into the canonical content-block shape."""

    if not isinstance(content, str) or not content:
        return []
    return [{"type": "text", "content": content}]


def content_blocks_from_message(
    message: Optional[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return stored blocks, falling back to a legacy text projection."""

    if not isinstance(message, Mapping):
        return []
    blocks = message.get("content_blocks")
    if isinstance(blocks, list) and blocks:
        return blocks
    return text_content_blocks(message.get("content"))
