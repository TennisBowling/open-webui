"""Tests for the live browser progress poller + host-side live-file reader.

The in-container browser daemon writes ``.cam/browser/live.jpg`` + ``state.json``
while an action runs. ``browser_progress_poller`` reads them host-side and pushes
``browser:frame`` (fire-and-forget, changed-only) events to the UI live panel;
``read_browser_live`` is the one-shot reader the reattach endpoint uses. The
poller intentionally does NOT emit persisted ``status`` breadcrumbs — each
browser action renders as its own inline tool-call card and the live panel shows
the real-time screenshot, so a status line would be redundant. These tests drive
the real functions against a temp workspace.
"""

import asyncio
import json
import tempfile
from pathlib import Path

from test.util.db import configure_test_database

configure_test_database()

import open_webui.utils.container_workspace as cw  # noqa: E402


def _live_dir(data_root: Path, chat_id: str) -> Path:
    d = data_root / chat_id / "workspace" / ".cam" / "browser"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(live_dir: Path, frame: bytes, state: dict):
    (live_dir / "live.jpg").write_bytes(frame)
    (live_dir / "state.json").write_text(json.dumps(state))


def test_read_browser_live_returns_frame_and_state():
    tmp = Path(tempfile.mkdtemp())
    live_dir = _live_dir(tmp, "chat1")
    _write(
        live_dir,
        b"\xff\xd8\xff\xe0JPEGDATA",
        {"action": "navigate", "url": "https://example.com", "phase": "loaded"},
    )
    out = cw.read_browser_live(str(tmp), "chat1")
    assert out is not None
    assert out["frame"].startswith("data:image/jpeg;base64,")
    assert out["state"]["phase"] == "loaded"
    assert out["stat"] is not None


def test_read_browser_live_none_when_absent():
    tmp = Path(tempfile.mkdtemp())
    assert cw.read_browser_live(str(tmp), "chat1") is None


def test_read_browser_live_rejects_bad_chat_id():
    tmp = Path(tempfile.mkdtemp())
    assert cw.read_browser_live(str(tmp), "../escape") is None
    assert cw.read_browser_live(str(tmp), "local:abc") is None


def test_poller_emits_changed_frames_only():
    async def run():
        tmp = Path(tempfile.mkdtemp())
        live_dir = _live_dir(tmp, "chat1")
        events = []

        async def emitter(e):
            events.append(e)

        _write(
            live_dir,
            b"\xff\xd8\xff\xe0FRAME1",
            {
                "action": "navigate",
                "target": "https://example.com",
                "url": "https://example.com",
                "phase": "navigating",
                "startedAt": 1,
                "elapsedMs": 100,
                "done": False,
            },
        )
        task = asyncio.create_task(
            cw.browser_progress_poller(
                data_root=str(tmp),
                chat_id="chat1",
                message_id="m1",
                session_id="s1",
                event_emitter=emitter,
                interval=0.2,
            )
        )
        await asyncio.sleep(0.35)
        n1 = len(events)

        # No change -> no new events.
        await asyncio.sleep(0.35)
        assert len(events) == n1, "identical frame/phase must not re-emit"

        # New bytes + phase change -> +1 frame.
        _write(
            live_dir,
            b"\xff\xd8\xff\xe0FRAME2-LONGER",
            {
                "action": "navigate",
                "target": "https://example.com",
                "url": "https://example.com",
                "phase": "loaded",
                "startedAt": 1,
                "elapsedMs": 900,
                "done": False,
            },
        )
        await asyncio.sleep(0.35)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        frames = [e for e in events if e["type"] == "browser:frame"]
        # Two live frames (frame1 + changed frame2) plus a terminal done frame
        # emitted on cancel so the panel freezes on the final view.
        live_frames = [f for f in frames if not f["data"].get("done")]
        done_frames = [f for f in frames if f["data"].get("done")]
        assert len(live_frames) == 2, f"expected 2 live frames, got {len(live_frames)}"
        assert len(done_frames) == 1, "expected one terminal done frame on cancel"
        assert frames[0]["data"]["frame"].startswith("data:image/jpeg;base64,")
        # The poller must NOT emit any persisted status breadcrumbs anymore — the
        # inline tool-call card + live panel make them redundant.
        statuses = [e for e in events if e["type"] == "status"]
        assert statuses == [], f"poller must not emit status events: {statuses}"
        # Live files are polled, never deleted.
        assert (live_dir / "live.jpg").exists()
        assert (live_dir / "state.json").exists()

    asyncio.run(run())


def test_poller_respects_max_fps(monkeypatch):
    async def run():
        tmp = Path(tempfile.mkdtemp())
        live_dir = _live_dir(tmp, "chat1")
        events = []

        async def emitter(e):
            events.append(e)

        monkeypatch.setattr(cw, "STREAM_BROWSER_FRAME_MAX_FPS", 1.0)
        _write(
            live_dir,
            b"\xff\xd8\xff\xe0FRAME1",
            {"action": "navigate", "url": "https://example.com", "phase": "navigating"},
        )
        task = asyncio.create_task(
            cw.browser_progress_poller(
                data_root=str(tmp),
                chat_id="chat1",
                message_id="m1",
                session_id="s1",
                event_emitter=emitter,
                interval=0.2,
            )
        )
        await asyncio.sleep(0.35)
        _write(
            live_dir,
            b"\xff\xd8\xff\xe0FRAME2",
            {"action": "navigate", "url": "https://example.com", "phase": "loaded"},
        )
        await asyncio.sleep(0.35)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        live_frames = [e for e in events if e["type"] == "browser:frame" and not e["data"].get("done")]
        assert len(live_frames) == 1

    asyncio.run(run())


def test_poller_omits_oversize_live_frame_but_keeps_metadata(monkeypatch):
    async def run():
        tmp = Path(tempfile.mkdtemp())
        live_dir = _live_dir(tmp, "chat1")
        events = []

        async def emitter(e):
            events.append(e)

        monkeypatch.setattr(cw, "STREAM_BROWSER_FRAME_MAX_BYTES", 8)
        _write(
            live_dir,
            b"\xff\xd8\xff\xe0FRAME-TOO-LARGE",
            {
                "action": "navigate",
                "url": "https://example.com",
                "phase": "navigating",
                "done": False,
            },
        )
        task = asyncio.create_task(
            cw.browser_progress_poller(
                data_root=str(tmp),
                chat_id="chat1",
                message_id="m1",
                session_id="s1",
                event_emitter=emitter,
                interval=0.2,
            )
        )
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        live_frames = [e for e in events if e["type"] == "browser:frame" and not e["data"].get("done")]
        assert live_frames
        assert live_frames[0]["data"].get("frame") is None
        assert live_frames[0]["data"].get("url") == "https://example.com"

    asyncio.run(run())


def test_poller_emits_terminal_done_frame_on_cancel():
    async def run():
        tmp = Path(tempfile.mkdtemp())
        live_dir = _live_dir(tmp, "chat1")
        events = []

        async def emitter(e):
            events.append(e)

        _write(
            live_dir,
            b"\xff\xd8\xff\xe0FRAME1",
            {
                "action": "navigate",
                "url": "https://example.com",
                "phase": "navigating",
                "startedAt": 1,
                "elapsedMs": 100,
                "done": False,
            },
        )
        task = asyncio.create_task(
            cw.browser_progress_poller(
                data_root=str(tmp),
                chat_id="chat1",
                message_id="m1",
                session_id="s1",
                event_emitter=emitter,
                interval=0.2,
            )
        )
        await asyncio.sleep(0.3)
        # Daemon writes a terminal state; then the tool call returns -> cancel.
        _write(
            live_dir,
            b"\xff\xd8\xff\xe0FRAME1-DONE",
            {
                "action": "navigate",
                "url": "https://example.com",
                "phase": "done",
                "startedAt": 1,
                "elapsedMs": 1800,
                "done": True,
            },
        )
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        done_frames = [
            e for e in events if e["type"] == "browser:frame" and e["data"].get("done")
        ]
        assert done_frames, "cancel must push a terminal done frame so the panel freezes"
        assert done_frames[-1]["data"]["done"] is True

    asyncio.run(run())


def test_poller_emits_no_status_for_fast_navigate():
    """A fast nav the poller only ever observed as "navigating" must still emit
    NO status events (the inline tool-call card carries the running/done state
    now) while still pushing a terminal done frame so the live panel freezes."""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        live_dir = _live_dir(tmp, "chat1")
        events = []

        async def emitter(e):
            events.append(e)

        # Only ever a navigating state; the daemon's terminal write never lands in
        # a poll window (simulating a sub-interval nav).
        _write(
            live_dir,
            b"\xff\xd8\xff\xe0FRAME1",
            {
                "action": "navigate",
                "target": "https://apnews.com",
                "url": "https://apnews.com/",
                "phase": "navigating",
                "startedAt": 1,
                "elapsedMs": 100,
                "done": False,
            },
        )
        task = asyncio.create_task(
            cw.browser_progress_poller(
                data_root=str(tmp),
                chat_id="chat1",
                message_id="m1",
                session_id="s1",
                event_emitter=emitter,
                interval=0.2,
            )
        )
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        statuses = [e for e in events if e["type"] == "status"]
        assert statuses == [], f"poller must not emit status events: {statuses}"
        # A terminal done frame still freezes the live panel on the final view.
        done_frames = [
            e for e in events if e["type"] == "browser:frame" and e["data"].get("done")
        ]
        assert done_frames, "cancel must push a terminal done frame so the panel freezes"

    asyncio.run(run())


def test_poller_treats_stale_state_as_terminal():
    """A daemon that restarted stamps the leftover state from a previous turn
    done+stale. The poller must emit any frame for it as done:true so a reloading
    tab freezes it (and never emits a status event for it)."""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        live_dir = _live_dir(tmp, "chat1")
        events = []

        async def emitter(e):
            events.append(e)

        _write(
            live_dir,
            b"\xff\xd8\xff\xe0STALEFRAME",
            {
                "action": "navigate",
                "target": "https://old.example",
                "url": "https://old.example/",
                "phase": "done",
                "startedAt": 1,
                "elapsedMs": 5,
                "done": True,
                "stale": True,
            },
        )
        task = asyncio.create_task(
            cw.browser_progress_poller(
                data_root=str(tmp),
                chat_id="chat1",
                message_id="m1",
                session_id="s1",
                event_emitter=emitter,
                interval=0.2,
            )
        )
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # The poller never emits status events at all.
        statuses = [e for e in events if e["type"] == "status"]
        assert statuses == [], f"poller must not emit status events: {statuses}"
        # Any frame emitted must be terminal.
        for e in events:
            if e["type"] == "browser:frame":
                assert e["data"].get("done") is True, e

    asyncio.run(run())


def test_poller_noop_without_emitter_or_data_root():
    async def run():
        tmp = Path(tempfile.mkdtemp())
        # No emitter -> returns immediately, no crash.
        await cw.browser_progress_poller(
            data_root=str(tmp),
            chat_id="chat1",
            message_id="m1",
            session_id=None,
            event_emitter=None,
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Subagent browser live-view wiring.
#
# A subagent drives the SHARED browser using the PARENT chat's container (its
# MCP X-Chat-Id header = parent chat_id), so the daemon writes live.jpg/state
# under the PARENT chat's workspace. Two fixes make the live side-panel update
# during subagent browsing:
#   1. middleware starts the poller against the workspace chat id
#      (container_workspace_chat_id, = parent) rather than the subagent's own
#      chat_id, so read_browser_live finds the frames the daemon actually wrote.
#   2. the subagent forwarding emitter re-routes browser:frame top-level to the
#      parent emitter (it would otherwise be dropped: it isn't a FORWARDED_TYPE
#      and the base emitter would scope it to the hidden subagent chat).
# These two tests pin both halves.
# ---------------------------------------------------------------------------


def test_poller_reads_parent_workspace_for_subagent():
    """The poller, given the resolved workspace chat id (parent), reads the
    frames the daemon wrote under the PARENT workspace — proving the middleware
    fix that passes container_workspace_chat_id surfaces real frames. With the
    pre-fix subagent chat_id it would read an empty dir and emit nothing."""

    async def run():
        tmp = Path(tempfile.mkdtemp())
        parent_chat_id = "parent_chat_abc"
        subagent_chat_id = "subagent_chat_xyz"
        # Daemon writes under the PARENT workspace (X-Chat-Id = parent).
        live_dir = _live_dir(tmp, parent_chat_id)
        _write(
            live_dir,
            b"\xff\xd8\xff\xe0SUBAGENT-FRAME",
            {
                "action": "navigate",
                "url": "https://example.com",
                "phase": "navigating",
                "startedAt": 1,
                "elapsedMs": 50,
                "done": False,
            },
        )

        # Pre-fix behavior: polling the subagent's own chat_id finds nothing.
        assert cw.read_browser_live(str(tmp), subagent_chat_id) is None

        # Post-fix behavior: middleware resolves the workspace chat id the same
        # way container_workspace.py does (container_workspace_chat_id first).
        meta = {
            "chat_id": subagent_chat_id,
            "container_workspace_chat_id": parent_chat_id,
        }
        resolved_chat_id = meta.get("container_workspace_chat_id") or meta.get(
            "chat_id"
        )
        assert resolved_chat_id == parent_chat_id

        events = []

        async def emitter(e):
            events.append(e)

        task = asyncio.create_task(
            cw.browser_progress_poller(
                data_root=str(tmp),
                chat_id=resolved_chat_id,
                message_id="parent_msg",
                session_id="s1",
                event_emitter=emitter,
                interval=0.2,
            )
        )
        await asyncio.sleep(0.35)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        frames = [e for e in events if e.get("type") == "browser:frame"]
        assert frames, "poller must surface frames from the parent workspace"
        assert any(e["data"].get("frame") for e in frames)

    asyncio.run(run())


def test_subagent_forwarding_emitter_routes_browser_frame_to_parent():
    """The subagent forwarding emitter must ship browser:frame TOP-LEVEL to the
    parent emitter (so it lands on the parent chat and reaches the parent UI's
    browser:frame handler) and must NOT wrap it in chat:subagent:update nor send
    it to the subagent (base) emitter scope."""
    import open_webui.utils.subagent as sa

    async def run():
        parent_events = []

        async def parent_emitter(e):
            parent_events.append(e)

        # base_emitter is get_event_emitter(subagent_socket_info); monkeypatch it
        # so we can assert browser:frame is NOT emitted to the subagent scope.
        base_events = []

        def fake_get_event_emitter(_info):
            async def _emit(e):
                base_events.append(e)

            return _emit

        orig = sa.get_event_emitter
        sa.get_event_emitter = fake_get_event_emitter
        try:
            emitter = await sa._build_forwarding_emitter(
                subagent_socket_info={
                    "user_id": "u1",
                    "session_id": "s1",
                    "chat_id": "subagent_chat",
                    "message_id": "subagent_msg",
                },
                parent_event_emitter=parent_emitter,
                subagent_meta={
                    "subagent_id": "sa1",
                    "num": 1,
                    "name": "researcher",
                    "parent_message_id": "parent_msg",
                },
                parent_chat_id="parent_chat",
                parent_message_id="parent_msg",
            )

            frame_event = {
                "type": "browser:frame",
                "data": {
                    "url": "https://example.com",
                    "phase": "navigating",
                    "frame": "data:image/jpeg;base64,AAA",
                    "done": False,
                },
            }
            await emitter(frame_event)
        finally:
            sa.get_event_emitter = orig

        # Reached the parent emitter, unwrapped (top-level browser:frame).
        assert len(parent_events) == 1, parent_events
        forwarded = parent_events[0]
        assert forwarded.get("type") == "browser:frame"
        assert forwarded["data"].get("frame") == "data:image/jpeg;base64,AAA"
        # NOT wrapped in the subagent envelope.
        assert forwarded.get("type") != "chat:subagent:update"
        # NOT emitted to the subagent (hidden chat) scope.
        assert base_events == [], "browser:frame must skip the subagent base emitter"

    asyncio.run(run())
