"""MCP client teardown ordering.

Regression cover for a RuntimeError that surfaced as a chat producing NO
response at all:

    RuntimeError: Attempted to exit a cancel scope that isn't the current
    task's current cancel scope

`setup_mcp_tools` connects clients serially in ONE task, so each transport's
AnyIO cancel scopes nest inside the previous client's. AnyIO requires LIFO
unwinding. Both teardown sites iterated `mcp_clients.items()` — insertion order,
the exact opposite — so with two or more servers attached to a turn the first
disconnect blew up and left the stack half-unwound.
"""

import asyncio

from open_webui.utils.mcp.client import disconnect_mcp_clients


class _FakeClient:
    def __init__(self, name, order, fail=False):
        self.name = name
        self._order = order
        self._fail = fail

    async def disconnect(self):
        self._order.append(self.name)
        if self._fail:
            raise RuntimeError(
                "Attempted to exit a cancel scope that isn't the current "
                "task's current cancel scope"
            )


def test_clients_are_disconnected_in_reverse_connection_order():
    order = []
    clients = {
        "a": _FakeClient("a", order),
        "b": _FakeClient("b", order),
        "c": _FakeClient("c", order),
    }
    asyncio.run(disconnect_mcp_clients(clients))
    assert order == ["c", "b", "a"], "teardown must unwind the scope stack LIFO"


def test_one_failing_client_does_not_strand_the_others():
    order = []
    clients = {
        "a": _FakeClient("a", order),
        "b": _FakeClient("b", order, fail=True),
        "c": _FakeClient("c", order),
    }
    asyncio.run(disconnect_mcp_clients(clients))
    assert order == ["c", "b", "a"]


def test_empty_and_missing_are_no_ops():
    asyncio.run(disconnect_mcp_clients({}))
    asyncio.run(disconnect_mcp_clients(None))
