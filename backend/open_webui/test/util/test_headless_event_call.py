"""The headless event caller must NEVER block on a socket ack.

A request-free (headless) generation — the autonomous queue drain — has no
originating socket session. The normal ``get_event_call`` does
``sio.call("events", ..., to=session_id)`` and AWAITS a client reply; with
``session_id=None`` that broadcasts to everyone and hangs forever waiting for an
ack nobody owns. ``get_headless_event_call`` must instead decline interactive
callbacks immediately without ever touching ``sio.call``.

``socket.main`` binds the DB engine at import, so DATABASE_URL is pointed at a
throwaway copy of the migrated dev DB before importing (same pattern as the other
util tests).
"""

import asyncio
import os

from test.util.db import configure_test_database

configure_test_database()
os.environ.pop("WEBSOCKET_REDIS_URL", None)

from open_webui.socket.main import get_headless_event_call  # noqa: E402
import open_webui.socket.main as socket_main  # noqa: E402


def test_headless_caller_declines_without_calling_sio(monkeypatch):
    called = {"sio_call": False}

    async def _boom_call(*a, **k):
        called["sio_call"] = True
        raise AssertionError("headless caller must not invoke sio.call")

    # Any access to sio.call should be a hard failure.
    monkeypatch.setattr(socket_main.sio, "call", _boom_call, raising=True)

    caller = get_headless_event_call(
        {"chat_id": "c1", "message_id": "m1", "session_id": None}
    )

    async def _run():
        return await asyncio.wait_for(
            caller({"type": "execute:tool", "data": {"name": "x"}}), timeout=2.0
        )

    result = asyncio.run(_run())
    assert result == {"status": False, "headless": True}
    assert called["sio_call"] is False
