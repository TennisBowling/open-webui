"""A turn stopped while a persistent MCP child initializes must not poison it.

Regression: ``ensure()`` awaited the entry's shared ``ready`` future bare, so
the first cancelled waiter cancelled the future itself and every later turn
using that connection died instantly with CancelledError ("The model request
was interrupted before a response could be completed.").
"""

import asyncio

import pytest

from open_webui.utils.mcp import persistent


class _SlowInitClient:
    def __init__(self, *args, **kwargs):
        self.session = object()

    async def connect(self, **kwargs):
        await asyncio.sleep(0.2)

    async def list_tool_specs(self):
        return [{"name": "t"}]

    async def disconnect(self):
        return None


@pytest.fixture(autouse=True)
def _fake_client(monkeypatch):
    monkeypatch.setattr(persistent, "MCPClient", _SlowInitClient)
    monkeypatch.setattr(persistent, "_transport_alive", lambda client: True)


def test_cancelled_waiter_does_not_poison_ready():
    async def scenario():
        manager = persistent.PersistentMCPManager()
        first = asyncio.create_task(manager.ensure("k", {}))
        await asyncio.sleep(0.05)
        first.cancel()  # user hits Stop while the child is initializing
        with pytest.raises(asyncio.CancelledError):
            await first

        proxy = await asyncio.wait_for(manager.ensure("k", {}), timeout=2)
        assert await proxy.list_tool_specs() == [{"name": "t"}]
        await manager.stop("k")

    asyncio.run(scenario())
