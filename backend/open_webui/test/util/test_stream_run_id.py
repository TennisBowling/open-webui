"""Stream RUN id (epoch) protocol tests.

A retry / "Continue Response" reuses the SAME message_id but resets the
version space to 0 and wipes the replay buffer. The run id makes that reset
explicit on the wire so clients can distinguish "stale duplicate delta"
from "new run started" — without it, a client holding the old run's high
version silently dropped every delta of the new run (frozen/empty response)
or spliced new-run ops onto old-run blocks (reasoning rendered as answer
text).
"""

import asyncio

from open_webui.socket import main as socket_main
from open_webui.socket.main import (
    STREAM_RUN_LOCAL,
    _make_delta_batch2_envelope,
    _stamp_generation_identity,
    _stamp_stream_run,
    append_stream_replay_event,
    get_active_streams_for_chat,
    get_stream_replay_events,
    stream_run_get,
    stream_version_incr,
    stream_version_init,
)


MSG = "run-id-test-message"
CHAT = "run-id-test-chat"


def _init(message_id=MSG):
    return stream_version_init(
        message_id, chat_id=CHAT, user_id="u1", session_id="s1", content_blocks=[]
    )


def _delta_envelope(message_id, version, op="text_append", payload=None):
    return {
        "chat_id": CHAT,
        "message_id": message_id,
        "session_id": "s1",
        "data": {
            "type": "chat:delta",
            "data": {
                "message_id": message_id,
                "version": version,
                "op": op,
                "payload": payload or {"block_idx": 0, "text": "x"},
            },
        },
    }


def teardown_function():
    socket_main._delete_stream_state_now(MSG)


def test_run_minted_at_init_and_monotonic_across_retries():
    _init()
    first = stream_run_get(MSG)
    assert first > 0
    _init()
    second = stream_run_get(MSG)
    assert second > first  # a retry on the same id always advances the run


def test_run_survives_clock_regression():
    _init()
    first = stream_run_get(MSG)
    # Simulate a wall clock far in the future having minted the current run;
    # the next init must still advance, never regress.
    STREAM_RUN_LOCAL[MSG] = first + 10_000_000
    _init()
    assert stream_run_get(MSG) > first + 10_000_000


def test_stamp_stream_run_on_delta_done_and_cancel():
    _init()
    run = stream_run_get(MSG)
    delta = _delta_envelope(MSG, 1)
    _stamp_stream_run(delta)
    assert delta["data"]["data"]["run"] == run

    done = {
        "chat_id": CHAT,
        "message_id": MSG,
        "data": {"type": "chat:done", "data": {"message_id": MSG, "version": 5}},
    }
    _stamp_stream_run(done)
    assert done["data"]["data"]["run"] == run

    cancel = {
        "chat_id": CHAT,
        "message_id": MSG,
        "data": {
            "type": "chat:tasks:cancel",
            "data": {"generation_id": "generation-1"},
        },
    }
    _stamp_stream_run(cancel)
    assert cancel["data"]["data"]["run"] == run

    # Non-stream event types are left untouched.
    other = {
        "chat_id": CHAT,
        "message_id": MSG,
        "data": {"type": "chat:title", "data": {"message_id": MSG}},
    }
    _stamp_stream_run(other)
    assert "run" not in other["data"]["data"]


def test_generation_identity_stamps_only_lifecycle_events():
    request_info = {
        "generation_id": "generation-1",
        "turn_id": "attempt-1",
    }
    cancel = _stamp_generation_identity({"type": "chat:tasks:cancel"}, request_info)
    assert cancel["data"] == {
        "generation_id": "generation-1",
        "turn_id": "attempt-1",
    }

    completion = _stamp_generation_identity(
        {"type": "chat:completion", "data": {"done": True}}, request_info
    )
    assert completion["data"] == {
        "done": True,
        "generation_id": "generation-1",
        "turn_id": "attempt-1",
    }

    status = {"type": "status", "data": {"description": "working"}}
    assert _stamp_generation_identity(status, request_info) is status


def test_replay_run_mismatch_forces_snapshot():
    async def scenario():
        _init()
        old_run = stream_run_get(MSG)
        for _ in range(3):
            version = stream_version_incr(MSG)
            env = _delta_envelope(MSG, version)
            _stamp_stream_run(env)
            await append_stream_replay_event(env)

        # Client caught up on the old run.
        ok = await get_stream_replay_events(MSG, 3, run=old_run)
        assert ok["status"] == "ok" and ok["events"] == []

        # Retry: same message id, new run, version space reset.
        _init()
        new_run = stream_run_get(MSG)
        assert new_run > old_run
        version = stream_version_incr(MSG)
        env = _delta_envelope(MSG, version)
        _stamp_stream_run(env)
        await append_stream_replay_event(env)

        # A client still on the OLD run must be told to snapshot, not "ok".
        stale = await get_stream_replay_events(MSG, 3, run=old_run)
        assert stale["snapshot_required"] is True
        assert stale["run"] == new_run

        # A client on the new run replays normally.
        fresh = await get_stream_replay_events(MSG, 0, run=new_run)
        assert fresh["status"] == "ok"
        assert [e["data"]["version"] for e in fresh["events"]] == [1]

    asyncio.run(scenario())


def test_replay_stale_epoch_without_run_forces_snapshot():
    """Legacy client (no run param) holding an after_version ABOVE the live
    counter used to get back {status: ok, events: []} — it then believed it
    was caught up and froze forever. Must now force a snapshot."""

    async def scenario():
        _init()
        version = stream_version_incr(MSG)
        env = _delta_envelope(MSG, version)
        _stamp_stream_run(env)
        await append_stream_replay_event(env)

        res = await get_stream_replay_events(MSG, 500)
        assert res["snapshot_required"] is True

    asyncio.run(scenario())


def test_active_streams_report_run():
    _init()
    run = stream_run_get(MSG)
    streams = get_active_streams_for_chat(CHAT)
    entry = next(s for s in streams if s["message_id"] == MSG)
    assert entry["run"] == run


def test_batch2_envelope_carries_group_run():
    _init()
    run = stream_run_get(MSG)
    batch = []
    for version in (1, 2):
        env = _delta_envelope(MSG, version)
        _stamp_stream_run(env)
        batch.append(env)
    envelope = _make_delta_batch2_envelope(batch)
    groups = envelope["data"]["groups"]
    assert len(groups) == 1
    assert groups[0]["run"] == run
    # Frames stay compact — run is carried once per group, not per frame.
    assert all(len(frame) == 4 for frame in groups[0]["deltas"])
