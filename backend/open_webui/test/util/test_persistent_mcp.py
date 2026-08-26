import asyncio

import pytest

from open_webui.utils.mcp import persistent


class FakeClient:
    instances = []

    def __init__(self):
        self.connected = False
        self.disconnected = False
        self.kwargs = None
        self.calls = []
        self.__class__.instances.append(self)

    async def connect(self, **kwargs):
        if kwargs.get("command") == "broken":
            raise RuntimeError("replacement failed")
        self.connected = True
        self.kwargs = kwargs

    async def disconnect(self):
        self.disconnected = True

    async def list_tool_specs(self):
        return [{"name": "echo"}]

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        return {"name": name, "arguments": arguments}


def test_persistent_client_is_reused_and_closed(monkeypatch):
    async def run():
        FakeClient.instances = []
        monkeypatch.setattr(persistent, "MCPClient", FakeClient)
        manager = persistent.PersistentMCPManager()
        first = await manager.ensure("one", {"command": "fake"})
        second = await manager.ensure("one", {"command": "ignored"})

        assert first is second
        assert await first.list_tool_specs() == [{"name": "echo"}]
        assert await second.call_tool("echo", {"value": 1}) == {
            "name": "echo",
            "arguments": {"value": 1},
        }
        assert len(FakeClient.instances) == 1

        await manager.close()
        assert FakeClient.instances[0].disconnected is True

    asyncio.run(run())


def test_restart_replaces_process(monkeypatch):
    async def run():
        FakeClient.instances = []
        monkeypatch.setattr(persistent, "MCPClient", FakeClient)
        manager = persistent.PersistentMCPManager()
        captured_before_restart = await manager.ensure("one", {"command": "first"})
        returned_by_restart = await manager.restart("one", {"command": "second"})

        assert len(FakeClient.instances) == 2
        assert returned_by_restart is captured_before_restart
        assert FakeClient.instances[0].disconnected is True
        assert FakeClient.instances[1].kwargs["command"] == "second"

        # This is the production regression: a chat turn keeps the handle it
        # captured during setup, then a settings save restarts the stdio server.
        # Its later tool call must resolve to the replacement process.
        assert await captured_before_restart.call_tool("echo", {"value": 2}) == {
            "name": "echo",
            "arguments": {"value": 2},
        }
        assert FakeClient.instances[0].calls == []
        assert FakeClient.instances[1].calls == [("echo", {"value": 2}, {})]

        await manager.close()
        assert FakeClient.instances[1].disconnected is True

    asyncio.run(run())


def test_restart_drains_submitted_old_calls_while_new_calls_use_replacement(
    monkeypatch,
):
    async def run():
        old_call_started = asyncio.Event()
        release_old_call = asyncio.Event()

        class BlockingFakeClient(FakeClient):
            async def call_tool(self, name, arguments, **kwargs):
                self.calls.append((name, arguments, kwargs))
                if self.kwargs["command"] == "first" and name == "block":
                    old_call_started.set()
                    await release_old_call.wait()
                return {"name": name, "arguments": arguments}

        BlockingFakeClient.instances = []
        monkeypatch.setattr(persistent, "MCPClient", BlockingFakeClient)
        manager = persistent.PersistentMCPManager()
        captured = await manager.ensure("one", {"command": "first"})

        old_call = asyncio.create_task(captured.call_tool("block", {"old": True}))
        await old_call_started.wait()
        restarting = asyncio.create_task(
            manager.restart("one", {"command": "second"})
        )

        # Restart waits for the already-submitted old call to drain, but swaps
        # the ready replacement in first. New calls must not queue behind or hit
        # the retiring process.
        for _ in range(20):
            current = manager._entries.get("one")
            if current is not None and current.connect_kwargs["command"] == "second":
                break
            await asyncio.sleep(0)
        assert len(BlockingFakeClient.instances) == 2
        assert manager._entries["one"].connect_kwargs["command"] == "second"
        assert await captured.call_tool("echo", {"new": True}) == {
            "name": "echo",
            "arguments": {"new": True},
        }
        assert BlockingFakeClient.instances[1].calls == [
            ("echo", {"new": True}, {})
        ]
        assert restarting.done() is False

        release_old_call.set()
        await old_call
        assert await restarting is captured
        assert BlockingFakeClient.instances[0].disconnected is True

        await manager.close()

    asyncio.run(run())


def test_failed_restart_keeps_existing_process_and_handle(monkeypatch):
    async def run():
        FakeClient.instances = []
        monkeypatch.setattr(persistent, "MCPClient", FakeClient)
        manager = persistent.PersistentMCPManager()
        captured = await manager.ensure("one", {"command": "first"})

        with pytest.raises(RuntimeError, match="replacement failed"):
            await manager.restart("one", {"command": "broken"})

        assert len(FakeClient.instances) == 2
        assert FakeClient.instances[0].disconnected is False
        assert FakeClient.instances[1].disconnected is True
        assert await captured.call_tool("echo", {"still": "alive"}) == {
            "name": "echo",
            "arguments": {"still": "alive"},
        }
        assert FakeClient.instances[0].calls == [("echo", {"still": "alive"}, {})]

        await manager.close()
        assert FakeClient.instances[0].disconnected is True

    asyncio.run(run())
