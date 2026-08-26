"""Tests for the live token-usage bar wire (token-usage:update).

Guards the fixes for "the token counter above the input never updates until
reload" plus its data-frugality requirements:

1. ALL-SESSION delivery: the push must reach every authenticated session, not
   just the elected primary — the old primary-only emit meant a phone chatting
   while another device held primary never saw the bar move (BroadcastChannel
   fan-out cannot cross devices), and token groups are instance-global anyway.
2. TRAILING COALESCE: a burst of per-round usage events collapses into at most
   one push per TOKEN_USAGE_PUSH_MIN_INTERVAL, and the delayed push reads the
   totals AFTER the burst (converges on final numbers).
3. CHANGED-ONLY payload: only groups whose payload changed since the last push
   are sent, along with the full `names` catalog so deletions propagate.
4. NO-OP suppression: nothing is emitted when nothing changed — including the
   no-groups-configured case.
"""

import asyncio
import copy

from test.util.db import configure_test_database

configure_test_database()
import os

os.environ.pop("WEBSOCKET_REDIS_URL", None)

from open_webui.socket import main as socket_main  # noqa: E402


def _group(usage_total, models=("m1",), limit=1000):
    return {
        "models": list(models),
        "limit": limit,
        "window_duration": None,
        "usage": {"in": usage_total // 2, "out": usage_total // 2, "total": usage_total},
        "next_reset_at": 4102444800,
        "reset_type": "daily",
    }


class _Harness:
    def __init__(self, groups_sequence, session_ids):
        self.groups_sequence = [copy.deepcopy(g) for g in groups_sequence]
        self.get_calls = 0
        self.emitted = []  # (payload, to_sid)
        self.session_ids = session_ids

    async def fake_get_token_groups(self):
        idx = min(self.get_calls, len(self.groups_sequence) - 1)
        self.get_calls += 1
        return copy.deepcopy(self.groups_sequence[idx])

    async def fake_emit(self, event, payload=None, to=None, **kwargs):
        assert event == "events"
        self.emitted.append((copy.deepcopy(payload), to))


def _drive(groups_sequence, session_ids, scenario):
    """Run `scenario(harness)` with the push machinery patched + reset."""
    harness = _Harness(groups_sequence, session_ids)

    saved_interval = socket_main.TOKEN_USAGE_PUSH_MIN_INTERVAL
    saved_get = socket_main.token_groups.get_token_groups
    saved_emit = socket_main.sio.emit
    saved_pool = dict(socket_main.SESSION_POOL)

    socket_main.TOKEN_USAGE_PUSH_MIN_INTERVAL = 0.05
    socket_main.token_groups.get_token_groups = harness.fake_get_token_groups
    socket_main.sio.emit = harness.fake_emit
    for sid in list(socket_main.SESSION_POOL.keys()):
        del socket_main.SESSION_POOL[sid]
    for i, sid in enumerate(session_ids):
        socket_main.SESSION_POOL[sid] = {"id": f"user-{i}"}
    socket_main._token_usage_push_last_at = 0.0
    socket_main._token_usage_push_task = None
    socket_main._token_usage_push_last_sent.clear()

    try:
        asyncio.run(scenario(harness))
    finally:
        socket_main.TOKEN_USAGE_PUSH_MIN_INTERVAL = saved_interval
        socket_main.token_groups.get_token_groups = saved_get
        socket_main.sio.emit = saved_emit
        for sid in list(socket_main.SESSION_POOL.keys()):
            del socket_main.SESSION_POOL[sid]
        for sid, v in saved_pool.items():
            socket_main.SESSION_POOL[sid] = v
        socket_main._token_usage_push_task = None
        socket_main._token_usage_push_last_sent.clear()
        socket_main._token_usage_push_last_at = 0.0
    return harness


def test_burst_coalesces_and_reaches_every_session():
    initial = {"quota": _group(100), "other": _group(5)}
    final = {"quota": _group(900), "other": _group(5)}

    async def scenario(h):
        # First push (immediate — interval long elapsed).
        socket_main.schedule_token_usage_push()
        await asyncio.sleep(0.02)
        # Burst of per-round usage events right behind it: must coalesce into
        # ONE trailing push that reads the post-burst totals.
        for _ in range(10):
            socket_main.schedule_token_usage_push()
        await asyncio.sleep(0.3)

    h = _drive([initial, final], ["sid-a", "sid-b"], scenario)

    # get_token_groups: once for the immediate push + once for the coalesced
    # trailing push — NOT once per scheduled event.
    assert h.get_calls == 2, f"expected 2 group reads, got {h.get_calls}"
    # 2 pushes × 2 sessions = 4 emits; every session got every push (all-session
    # delivery — no primary election involved).
    tos = [to for _, to in h.emitted]
    assert tos.count("sid-a") == 2 and tos.count("sid-b") == 2, tos
    # The trailing push carries the POST-burst totals and only the changed group.
    last_payload = h.emitted[-1][0]["data"]["data"]
    assert last_payload["groups"] == {"quota": _group(900)}, last_payload
    assert sorted(last_payload["names"]) == ["other", "quota"]
    # Every push carries an event_id (BroadcastChannel dedup on the client).
    assert all(p["data"].get("event_id") for p, _ in h.emitted)


def test_no_change_and_no_groups_suppress_the_emit():
    async def scenario(h):
        socket_main.schedule_token_usage_push()
        await asyncio.sleep(0.1)
        n_after_first = len(h.emitted)
        assert n_after_first == 1, h.emitted  # 1 session × first full push
        # Same totals again → nothing changed → no emit.
        socket_main.schedule_token_usage_push()
        await asyncio.sleep(0.15)
        assert len(h.emitted) == n_after_first, "unchanged groups were re-pushed"

    _drive([{"quota": _group(100)}], ["sid-a"], scenario)

    async def empty_scenario(h):
        socket_main.schedule_token_usage_push()
        await asyncio.sleep(0.15)
        assert h.emitted == [], "no-groups-configured case must not emit"

    _drive([{}], ["sid-a"], empty_scenario)


def test_group_deletion_propagates_via_names():
    both = {"quota": _group(100), "old": _group(50)}
    only_quota = {"quota": _group(100)}

    async def scenario(h):
        socket_main.schedule_token_usage_push()
        await asyncio.sleep(0.1)
        socket_main.schedule_token_usage_push()
        await asyncio.sleep(0.15)
        assert len(h.emitted) == 2, h.emitted
        last = h.emitted[-1][0]["data"]["data"]
        # Nothing changed in the surviving group, but the deletion must still
        # push a fresh catalog so the client can drop the removed group.
        assert last["groups"] == {}, last
        assert last["names"] == ["quota"], last

    _drive([both, only_quota], ["sid-a"], scenario)


if __name__ == "__main__":
    test_burst_coalesces_and_reaches_every_session()
    test_no_change_and_no_groups_suppress_the_emit()
    test_group_deletion_propagates_via_names()
    print("token usage push tests passed")
