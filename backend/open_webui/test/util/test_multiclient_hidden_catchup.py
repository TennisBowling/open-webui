"""Multi-client sync (G1): the document-hidden stream-room subscriber catch-up.

A hidden subscriber has its live token deltas suppressed for perf, but instead of
freezing until chat:done it is nudged with a coalesced chat:stream:sync_required at
most once per STREAM_HIDDEN_CATCHUP_MS so a passively-watched second screen / phone
stays near-live. These tests pin that behavior without a live socket server:

  - _should_emit_stream_payload_to_sid suppresses a live delta to a hidden subscriber
    AND arms a catch-up task for it
  - the arming is debounced (one pending task per (sid, message_id))
  - the worker emits a sync_required ONLY while the sub is still hidden + subscribed
    and the stream is still in_progress; it is a no-op once visible / done / gone
  - _cancel_hidden_catchup_for_message tears the tasks down on stream teardown
"""

from test.util.db import configure_test_database

configure_test_database()

import asyncio  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import open_webui.socket.main as sm  # noqa: E402


CHAT = "chat-1"
SID = "sid-hidden"
MSG = "msg-1"


def _reset(monkeypatch, *, visible: bool, status: str = "in_progress"):
    """Isolate module state for one scenario."""
    # Cancel any leftovers and reset the task registry.
    for t in list(sm._HIDDEN_CATCHUP_TASKS.values()):
        t.cancel()
    sm._HIDDEN_CATCHUP_TASKS.clear()

    emit = AsyncMock()
    monkeypatch.setattr(sm.sio, "emit", emit)
    monkeypatch.setattr(sm, "STREAM_HIDDEN_CATCHUP_MS", 50, raising=False)
    monkeypatch.setattr(sm, "SESSION_POOL", {SID: {"id": "user-1"}}, raising=False)
    monkeypatch.setattr(
        sm,
        "STREAM_SUBSCRIPTION_STATE",
        {CHAT: {SID: {"visible": visible, "capabilities": {"ack": True}}}},
        raising=False,
    )
    monkeypatch.setattr(
        sm, "STREAM_STATE", {MSG: {"status": status, "chat_id": CHAT}}, raising=False
    )
    monkeypatch.setattr(sm, "STREAM_VERSION", {MSG: 7}, raising=False)
    return emit


def _delta_payload(version=5):
    return {
        "chat_id": CHAT,
        "message_id": MSG,
        "data": {
            "type": "chat:delta",
            "data": {"message_id": MSG, "version": version, "op": "text_append",
                     "payload": {"block_idx": 0, "text": "hi"}},
        },
    }


def _sync_required_types(emit_mock):
    out = []
    for call in emit_mock.await_args_list:
        args = call.args
        if len(args) >= 2 and isinstance(args[1], dict):
            data = args[1].get("data") or {}
            out.append(data.get("type"))
    return out


def test_hidden_subscriber_delta_suppressed_and_catchup_armed(monkeypatch):
    emit = _reset(monkeypatch, visible=False)

    async def go():
        allowed = await sm._should_emit_stream_payload_to_sid(SID, _delta_payload())
        # suppressed (not delivered live) ...
        assert allowed is False
        # ... but a catch-up nudge was armed for this (sid, message)
        assert (SID, MSG) in sm._HIDDEN_CATCHUP_TASKS
        # let the worker fire
        await asyncio.sleep(0.12)
        assert "chat:stream:sync_required" in _sync_required_types(emit)
        # task cleared itself so a later suppressed delta can re-arm it
        assert (SID, MSG) not in sm._HIDDEN_CATCHUP_TASKS

    asyncio.run(go())


def test_visible_subscriber_gets_delta_and_no_catchup(monkeypatch):
    emit = _reset(monkeypatch, visible=True)

    async def go():
        allowed = await sm._should_emit_stream_payload_to_sid(SID, _delta_payload())
        assert allowed is True  # visible → delivered live
        assert (SID, MSG) not in sm._HIDDEN_CATCHUP_TASKS
        await asyncio.sleep(0.12)
        assert "chat:stream:sync_required" not in _sync_required_types(emit)

    asyncio.run(go())


def test_schedule_is_debounced(monkeypatch):
    _reset(monkeypatch, visible=False)

    async def go():
        sm._schedule_hidden_catchup(SID, CHAT, MSG)
        first = sm._HIDDEN_CATCHUP_TASKS.get((SID, MSG))
        sm._schedule_hidden_catchup(SID, CHAT, MSG)  # rapid re-arm
        second = sm._HIDDEN_CATCHUP_TASKS.get((SID, MSG))
        assert first is second  # same pending task, not a duplicate
        await asyncio.sleep(0.12)

    asyncio.run(go())


def test_worker_noop_when_stream_finished(monkeypatch):
    emit = _reset(monkeypatch, visible=False, status="done")

    async def go():
        sm._schedule_hidden_catchup(SID, CHAT, MSG)
        await asyncio.sleep(0.12)
        # stream already terminal → no nudge emitted
        assert "chat:stream:sync_required" not in _sync_required_types(emit)

    asyncio.run(go())


def test_cancel_for_message_tears_down(monkeypatch):
    _reset(monkeypatch, visible=False)

    async def go():
        sm._schedule_hidden_catchup(SID, CHAT, MSG)
        assert (SID, MSG) in sm._HIDDEN_CATCHUP_TASKS
        sm._cancel_hidden_catchup_for_message(MSG)
        assert (SID, MSG) not in sm._HIDDEN_CATCHUP_TASKS
        await asyncio.sleep(0.02)

    asyncio.run(go())
