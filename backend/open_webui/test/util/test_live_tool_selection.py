import asyncio

from open_webui import tasks
from open_webui.utils.live_tool_selection import (
    build_tool_selection_change_block,
    normalize_live_tool_selection,
)
from open_webui.utils.messages import blocks_to_api_messages


def test_normalize_live_tool_selection_bounds_and_filters_shape():
    normalized = normalize_live_tool_selection(
        {
            "operation_id": " op-1 ",
            "selection_ids": [
                "server:mcp:container",
                "server:mcp:container",
                "feature:web_search",
                123,
            ],
            "tool_ids": ["server:mcp:container", None],
            "tool_servers": [{"url": "https://tools.example"}, "invalid"],
            "features": {
                "web_search": 1,
                "subagents": False,
                "untrusted_feature": True,
            },
            "labels": {
                "server:mcp:container": "Container",
                # Built-in labels are server-owned, not client-owned.
                "feature:web_search": "Pretend label",
            },
            "params": {"subagentExternalToolsEnabled": 0, "ignored": True},
        }
    )

    assert normalized == {
        "operation_id": "op-1",
        "revision": 0,
        "selection_ids": ["server:mcp:container", "feature:web_search"],
        "tool_ids": ["server:mcp:container"],
        "tool_servers": [{"url": "https://tools.example"}],
        "features": {"web_search": True, "subagents": False},
        "labels": {
            "server:mcp:container": "Container",
            "feature:web_search": "Web Search",
        },
        "params": {"subagentExternalToolsEnabled": False},
    }


def test_change_block_is_one_net_added_removed_event():
    current = normalize_live_tool_selection(
        {
            "selection_ids": ["tool:a", "feature:web_search"],
            "labels": {"tool:a": "Alpha"},
        }
    )
    updated = normalize_live_tool_selection(
        {
            "operation_id": "latest",
            "selection_ids": ["feature:web_search", "tool:b"],
            "labels": {"tool:b": "Beta"},
        }
    )

    block = build_tool_selection_change_block(current, updated)

    assert block == {
        "type": "tool_selection_change",
        "operation_id": "latest",
        "added": [{"id": "tool:b", "name": "Beta"}],
        "removed": [{"id": "tool:a", "name": "Alpha"}],
    }
    assert build_tool_selection_change_block(updated, updated) is None


def test_pending_selection_replaces_older_snapshot_before_boundary():
    async def scenario():
        task_id = "live-tools-task"
        tasks.item_tasks["chat-live-tools"] = [task_id]
        try:
            first = await tasks.set_pending_tool_selection(
                None,
                task_id,
                {
                    "operation_id": "first",
                    "revision": 1,
                    "selection_ids": ["tool:a"],
                },
            )
            second = await tasks.set_pending_tool_selection(
                None,
                task_id,
                {
                    "operation_id": "second",
                    "revision": 2,
                    "selection_ids": ["tool:b"],
                },
            )
            consumed = await tasks.pop_pending_tool_selection(None, task_id)
            empty = await tasks.pop_pending_tool_selection(None, task_id)
        finally:
            tasks.pending_tool_selections.pop(task_id, None)
            tasks.pending_tool_selection_revisions.pop(task_id, None)
            tasks.item_tasks.pop("chat-live-tools", None)

        assert first["status"] is True
        assert second["status"] is True
        assert consumed == {
            "operation_id": "second",
            "revision": 2,
            "selection_ids": ["tool:b"],
        }
        assert empty is None

    asyncio.run(scenario())


def test_older_selection_cannot_reappear_after_newer_snapshot_is_consumed():
    async def scenario():
        task_id = "live-tools-ordered-task"
        tasks.item_tasks["chat-live-tools"] = [task_id]
        latest = {
            "operation_id": "latest",
            "revision": 20,
            "selection_ids": ["tool:latest"],
        }
        older = {
            "operation_id": "older",
            "revision": 10,
            "selection_ids": ["tool:older"],
        }
        try:
            accepted = await tasks.set_pending_tool_selection(None, task_id, latest)
            consumed = await tasks.pop_pending_tool_selection(None, task_id)
            stale = await tasks.set_pending_tool_selection(None, task_id, older)
            resurrected = await tasks.pop_pending_tool_selection(None, task_id)
        finally:
            tasks.pending_tool_selections.pop(task_id, None)
            tasks.pending_tool_selection_revisions.pop(task_id, None)
            tasks.item_tasks.pop("chat-live-tools", None)

        assert accepted["superseded"] is False
        assert consumed == latest
        assert stale["status"] is True
        assert stale["superseded"] is True
        assert resurrected is None

    asyncio.run(scenario())


def test_tool_selection_event_never_enters_provider_transcript():
    output = blocks_to_api_messages(
        [
            {
                "role": "assistant",
                "content_blocks": [
                    {"type": "text", "content": "Before."},
                    {
                        "type": "tool_selection_change",
                        "added": [{"id": "tool:b", "name": "Beta"}],
                        "removed": [{"id": "tool:a", "name": "Alpha"}],
                    },
                    {"type": "text", "content": "After."},
                ],
            }
        ]
    )

    provider_text = "\n".join(str(message.get("content") or "") for message in output)
    assert "Before." in provider_text
    assert "After." in provider_text
    assert "Alpha" not in provider_text
    assert "Beta" not in provider_text
