"""Tests for the subagent reliability fixes:

* ``reconcile_block_results_from_runs`` — backend write-path backfill of missing/
  empty subagent tool results from the durable ``subagent_runs`` mirror.
* ``_total_text_block_len`` — the order-independent productivity signal the
  agentic loop's empty-round retry keys off.
* ``AGENTIC_EMPTY_ROUND_MAX_RETRIES`` env default/override.

These are pure-function tests (no DB / no event loop needed). DB env is still
pointed at a temp copy in case importing the modules touches it.
"""

from unittest.mock import patch

import pytest

from test.util.db import configure_test_database

configure_test_database()

from open_webui.utils import subagent as sub  # noqa: E402
from open_webui.utils.subagent import reconcile_block_results_from_runs  # noqa: E402


def _block(call_id, name="subagent_launch", results=None):
    return {
        "type": "tool_calls",
        "content": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
        ],
        **({"results": results} if results is not None else {}),
    }


def _result_for(blocks, call_id):
    for b in blocks:
        for r in b.get("results") or []:
            if r.get("tool_call_id") == call_id:
                return r
    return None


def test_reconcile_backfills_missing_result_row():
    blocks = [_block("c1", results=[])]
    runs = {"sa1": {"subagent_id": "sa1", "tool_call_id": "c1", "status": "done", "final_text": "answer"}}
    changed = reconcile_block_results_from_runs(blocks, runs)
    assert changed is True
    r = _result_for(blocks, "c1")
    assert r is not None
    assert r["content"] == "answer"
    assert r["subagent_id"] == "sa1"


def test_reconcile_fills_empty_existing_result_by_subagent_id():
    blocks = [_block("c2", results=[{"tool_call_id": "c2", "content": "", "subagent_id": "sa2"}])]
    runs = {"sa2": {"subagent_id": "sa2", "status": "done", "final_text": "filled"}}
    changed = reconcile_block_results_from_runs(blocks, runs)
    assert changed is True
    assert _result_for(blocks, "c2")["content"] == "filled"


def test_reconcile_does_not_override_present_content():
    blocks = [_block("c3", results=[{"tool_call_id": "c3", "content": "keep me", "subagent_id": "sa3"}])]
    runs = {"sa3": {"subagent_id": "sa3", "status": "done", "final_text": "other"}}
    changed = reconcile_block_results_from_runs(blocks, runs)
    assert changed is False
    assert _result_for(blocks, "c3")["content"] == "keep me"


def test_reconcile_skips_non_done_or_empty_runs():
    blocks = [_block("c4", results=[{"tool_call_id": "c4", "content": "", "subagent_id": "sa4"}])]
    runs = {"sa4": {"subagent_id": "sa4", "status": "error", "final_text": ""}}
    changed = reconcile_block_results_from_runs(blocks, runs)
    assert changed is False
    assert _result_for(blocks, "c4")["content"] == ""


def test_reconcile_partial_fanout_recovers_only_missing():
    # 3 launches; only the middle one's result landed. Reconcile fills the other 2.
    blocks = [
        {
            "type": "tool_calls",
            "content": [
                {"id": "a", "type": "function", "function": {"name": "subagent_launch", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "subagent_launch", "arguments": "{}"}},
                {"id": "c", "type": "function", "function": {"name": "subagent_launch", "arguments": "{}"}},
            ],
            "results": [{"tool_call_id": "b", "content": "B done", "subagent_id": "sb"}],
        }
    ]
    runs = {
        "sa": {"subagent_id": "sa", "tool_call_id": "a", "status": "done", "final_text": "A done"},
        "sb": {"subagent_id": "sb", "tool_call_id": "b", "status": "done", "final_text": "B done"},
        "sc": {"subagent_id": "sc", "tool_call_id": "c", "status": "done", "final_text": "C done"},
    }
    changed = reconcile_block_results_from_runs(blocks, runs)
    assert changed is True
    assert _result_for(blocks, "a")["content"] == "A done"
    assert _result_for(blocks, "b")["content"] == "B done"
    assert _result_for(blocks, "c")["content"] == "C done"


def test_reconcile_ignores_non_subagent_calls():
    blocks = [_block("c5", name="web_search", results=[])]
    runs = {"sa5": {"subagent_id": "sa5", "tool_call_id": "c5", "status": "done", "final_text": "x"}}
    changed = reconcile_block_results_from_runs(blocks, runs)
    assert changed is False


def test_reconcile_no_runs_is_noop():
    blocks = [_block("c6", results=[])]
    assert reconcile_block_results_from_runs(blocks, {}) is False
    assert reconcile_block_results_from_runs(blocks, None) is False


# -- empty-round productivity signal -----------------------------------------


def test_total_text_block_len_counts_only_text_blocks():
    from open_webui.utils.middleware import _total_text_block_len

    blocks = [
        {"type": "reasoning", "content": "thinking hard"},
        {"type": "tool_calls", "content": []},
        {"type": "text", "content": "  hello  "},
        {"type": "text", "content": "world"},
    ]
    # "hello" (5, stripped) + "world" (5) = 10; reasoning/tool_calls ignored.
    assert _total_text_block_len(blocks) == 10


def test_total_text_block_len_empty_and_whitespace():
    from open_webui.utils.middleware import _total_text_block_len

    assert _total_text_block_len([]) == 0
    assert _total_text_block_len([{"type": "text", "content": "   "}]) == 0
    assert _total_text_block_len([{"type": "reasoning", "content": "x"}]) == 0


def test_empty_round_retry_env_default_is_five():
    from open_webui.env import AGENTIC_EMPTY_ROUND_MAX_RETRIES

    assert AGENTIC_EMPTY_ROUND_MAX_RETRIES == 5


# -- Parallel tool-call cancellation isolation -------------------------------
# A single parallel subagent self-cancelling (its own timeout / a dead inner
# stream) must NOT cancel its siblings or tear down the parent. These tests pin
# the asyncio semantics the middleware fix relies on: gather(return_exceptions=
# True) captures a child CancelledError as a result instead of propagating, and
# siblings still complete.

import asyncio


def test_gather_return_exceptions_isolates_child_cancel():
    async def _run():
        async def ok(v):
            await asyncio.sleep(0)
            return v

        async def self_cancel():
            raise asyncio.CancelledError()

        results = await asyncio.gather(
            ok("a"), self_cancel(), ok("c"), return_exceptions=True
        )
        return results

    results = asyncio.run(_run())
    assert results[0] == "a"
    assert isinstance(results[1], asyncio.CancelledError)
    assert results[2] == "c"  # sibling AFTER the cancelled one still completed


def test_failed_call_converts_to_nonempty_error_result_shape():
    # Mirror of _result_for_failed_call: a failed/cancelled call yields a
    # non-empty content string keyed to the tool_call_id so the model sees it.
    def result_for_failed_call(tool_call, exc):
        tcid = tool_call.get("id", "")
        name = tool_call.get("function", {}).get("name", "")
        if isinstance(exc, asyncio.CancelledError):
            msg = "was interrupted before it returned"
        elif isinstance(exc, asyncio.TimeoutError):
            msg = "timed out before it returned"
        else:
            msg = f"failed: {exc}"
        return {"tool_call_id": tcid, "content": f"Tool '{name}' {msg}."}

    call = {"id": "c1", "function": {"name": "subagent_launch"}}
    r_cancel = result_for_failed_call(call, asyncio.CancelledError())
    assert r_cancel["tool_call_id"] == "c1"
    assert r_cancel["content"].strip()
    assert "interrupted" in r_cancel["content"]

    r_timeout = result_for_failed_call(call, asyncio.TimeoutError())
    assert "timed out" in r_timeout["content"]

    r_err = result_for_failed_call(call, RuntimeError("boom"))
    assert "boom" in r_err["content"]


def test_subagent_cancel_classification_uses_parent_task_context():
    class FakeTask:
        def __init__(self, cancelling):
            self._cancelling = cancelling

        def cancelling(self):
            return self._cancelling

    token = sub.current_tool_parent_task_var.set(FakeTask(0))
    try:
        assert sub._subagent_cancel_is_from_parent_task() is False
    finally:
        sub.current_tool_parent_task_var.reset(token)

    token = sub.current_tool_parent_task_var.set(FakeTask(1))
    try:
        assert sub._subagent_cancel_is_from_parent_task() is True
    finally:
        sub.current_tool_parent_task_var.reset(token)


def test_isolated_child_cancellation_is_consumed_for_recovery():
    async def go():
        task = asyncio.current_task()
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            cleared = sub._clear_isolated_child_cancellation()
            assert cleared >= 1
            assert task.cancelling() == 0
            # A recovery await should now run normally instead of immediately
            # receiving the stale cancellation again.
            await asyncio.sleep(0)
            return "recovered"

    assert asyncio.run(go()) == "recovered"


# -- Genuine vs spurious subagent cancellation -------------------------------
# A CancelledError inside a subagent is a GENUINE user-stop only when the parent
# task is actually being cancelled (current_task().cancelling() > 0). A spurious
# per-subagent cancel (an inner stream closing) leaves the parent NOT cancelling,
# and should be retried as an error rather than marking a working subagent as
# "stopped". These pin the asyncio semantics the fix relies on.


def test_uncancelled_task_reports_not_cancelling():
    async def _run():
        return asyncio.current_task().cancelling()

    assert asyncio.run(_run()) == 0


def test_cancelled_task_reports_cancelling():
    results = {}

    async def _inner():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # On a real cancel, cancelling() is > 0 here — the signal the
            # subagent cancel-handler uses to honor a genuine user stop.
            results["cancelling"] = asyncio.current_task().cancelling()
            raise

    async def _run():
        t = asyncio.create_task(_inner())
        await asyncio.sleep(0)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert results.get("cancelling", 0) > 0


# -- Streaming chat-completion timeout (no hidden wall/gap cap) ---------------
# The upstream streaming session must NOT inherit aiohttp's 5-minute `total`
# default, and by default it also must not have a hidden sock_read gap cap.


def test_stream_timeout_uses_sock_read_not_total():
    from open_webui.env import (
        AIOHTTP_CLIENT_TIMEOUT,
        AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ,
    )
    import aiohttp

    # Overall total is unbounded (None) by default — long generations never get
    # a hard wall-clock cut-off.
    assert AIOHTTP_CLIENT_TIMEOUT is None
    # The dead-connection guard is opt-in; default is fully unbounded.
    assert AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ is None

    t = aiohttp.ClientTimeout(
        total=None, sock_read=AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ
    )
    assert t.total is None
    assert t.sock_read is None


# -- Subagent provider non-streaming -----------------------------------------


def test_subagent_provider_stream_default_is_disabled():
    from open_webui.env import SUBAGENT_PROVIDER_STREAM

    assert SUBAGENT_PROVIDER_STREAM is False


def test_openai_provider_stream_override_disables_stream_and_stream_options():
    from open_webui.routers.openai import _apply_provider_stream_override

    payload = {"stream": True, "stream_options": {"include_usage": True}}
    override = _apply_provider_stream_override(payload, {"provider_stream": False})

    assert override is False
    assert payload["stream"] is False
    assert "stream_options" not in payload


def test_openai_provider_stream_override_leaves_normal_payload_untouched():
    from open_webui.routers.openai import _apply_provider_stream_override

    payload = {"stream": True, "stream_options": {"include_usage": True}}
    override = _apply_provider_stream_override(payload, {})

    assert override is None
    assert payload == {"stream": True, "stream_options": {"include_usage": True}}


def test_subagent_nonstreaming_dict_routes_through_agentic_loop():
    from open_webui.utils.middleware import (
        _should_handle_nonstreaming_response_in_agentic_loop,
    )

    response = {"choices": [{"message": {"content": "ok"}}]}
    assert _should_handle_nonstreaming_response_in_agentic_loop(
        response,
        {"stream": True},
        {"subagent_inner": True, "provider_stream": False},
    ) is True

    assert _should_handle_nonstreaming_response_in_agentic_loop(
        response,
        {"stream": True},
        {"subagent_inner": False, "provider_stream": False},
    ) is False

    assert _should_handle_nonstreaming_response_in_agentic_loop(
        response,
        {"stream": True},
        {"subagent_inner": True, "provider_stream": True},
    ) is True

    assert _should_handle_nonstreaming_response_in_agentic_loop(
        {"error": {"message": "bad"}},
        {"stream": True},
        {"subagent_inner": True, "provider_stream": True},
    ) is False


def test_nonstreaming_length_with_no_text_or_tools_is_terminal_error():
    from open_webui.utils.middleware import _nonstreaming_round_length_error

    assert _nonstreaming_round_length_error(
        {"choices": [{"finish_reason": "length", "message": {"content": None}}]}
    )
    assert _nonstreaming_round_length_error(
        {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
    ) is None
    assert _nonstreaming_round_length_error(
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"tool_calls": [{"id": "call_1"}]},
                }
            ]
        }
    ) is None



def test_visible_reasoning_from_nonstreaming_reasoning_details_summary():
    from open_webui.utils.middleware import _visible_reasoning_from_details

    assert _visible_reasoning_from_details(
        [
            {"type": "reasoning.summary", "summary": " step one "},
            {"type": "reasoning.encrypted", "data": "opaque", "id": "rs_1"},
            {"type": "reasoning.text", "text": "step two"},
        ]
    ) == "step one\n\nstep two"


def test_visible_reasoning_from_nonstreaming_reasoning_details_ignores_encrypted_only():
    from open_webui.utils.middleware import _visible_reasoning_from_details

    assert _visible_reasoning_from_details(
        [{"type": "reasoning.encrypted", "data": "opaque", "id": "rs_1"}]
    ) == ""



def test_visible_nonstreaming_reasoning_prefers_string_reasoning_fields():
    from open_webui.utils.middleware import _visible_nonstreaming_reasoning

    assert asyncio.run(
        _visible_nonstreaming_reasoning(
            {"reasoning": " shown ", "reasoning_details": [{"summary": "fallback"}]}
        )
    ) == "shown"


def test_visible_nonstreaming_reasoning_ignores_non_string_reasoning_field():
    from open_webui.utils.middleware import _visible_nonstreaming_reasoning

    assert asyncio.run(
        _visible_nonstreaming_reasoning(
            {"reasoning": {"not": "text"}, "reasoning_details": [{"summary": "fallback"}]}
        )
    ) == "fallback"



def test_mcp_disconnect_closes_stack_under_pending_task_cancellation():
    import asyncio
    from open_webui.utils.mcp.client import MCPClient

    observed = {}

    class FakeStack:
        async def aclose(self):
            observed["cancelling_during_close"] = asyncio.current_task().cancelling()
            await asyncio.sleep(0)

    async def _run():
        client = MCPClient()
        client.exit_stack = FakeStack()
        task = asyncio.current_task()
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            await client.disconnect()
            observed["cancelling_after_close"] = task.cancelling()
            raise

    try:
        asyncio.run(_run())
    except asyncio.CancelledError:
        pass

    assert observed["cancelling_during_close"] == 0
    assert observed["cancelling_after_close"] > 0


# ---------------------------------------------------------------------------
# Tier 0: atomic subagent_runs merge + slot reservation (the lost-update fix).
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

import open_webui.utils.subagent as subagent_module  # noqa: E402


def test_critical_subagent_write_waits_through_repeated_cancellation():
    async def scenario():
        outer = asyncio.current_task()

        async def critical_write():
            await asyncio.sleep(0)
            outer.cancel()
            await asyncio.sleep(0.001)
            outer.cancel()
            await asyncio.sleep(0.001)
            return "committed"

        task = asyncio.create_task(critical_write())
        cancellation_seen = (
            await subagent_module._wait_shielded_task_to_completion(task)
        )
        return cancellation_seen, task.result()

    assert asyncio.run(scenario()) == (True, "committed")


class _FakeAtomicChats:
    """Stand-in for the Chats async proxy that runs the mutator against a single
    shared message dict (read-mutate-write), mirroring update_message_fields_atomic.
    Because each call re-reads the live message, a correct mutator never loses a
    sibling entry — which is exactly what the real per-message lock guarantees."""

    def __init__(self, message=None):
        self.message = dict(message or {})

    async def update_message_fields_atomic(self, _id, _mid, mutator):
        partial = mutator(dict(self.message))  # fresh read each call
        if partial:
            self.message.update(partial)
        return partial


def test_upsert_reserves_distinct_num_and_name_and_keeps_siblings(monkeypatch):
    fake = _FakeAtomicChats({"id": "pm1"})
    monkeypatch.setattr(subagent_module, "Chats", fake)

    # Two parallel launches reserve their slot from the SAME fresh map. The
    # second sees the first → distinct num + disambiguated name (no "Subagent 1"
    # collision, no duplicate "Berkeley").
    a = asyncio.run(
        subagent_module._upsert_subagent_run(
            "pc1", "pm1", "saA",
            {"subagent_id": "saA", "status": "running", "started_at": 100},
            sync_placeholder=False,
            reserve={"desired_name": "Berkeley", "other_runs": {}},
        )
    )
    b = asyncio.run(
        subagent_module._upsert_subagent_run(
            "pc1", "pm1", "saB",
            {"subagent_id": "saB", "status": "running", "started_at": 101},
            sync_placeholder=False,
            reserve={"desired_name": "Berkeley", "other_runs": {}},
        )
    )
    assert a["num"] == 1 and a["name"] == "Berkeley"
    assert b["num"] == 2 and b["name"] == "Berkeley_2"

    runs = fake.message["subagent_runs"]
    assert set(runs) == {"saA", "saB"}  # neither sibling lost


def test_upsert_terminal_preserves_started_at_and_siblings(monkeypatch):
    fake = _FakeAtomicChats({"id": "pm1"})
    monkeypatch.setattr(subagent_module, "Chats", fake)

    asyncio.run(
        subagent_module._upsert_subagent_run(
            "pc1", "pm1", "saA",
            {"subagent_id": "saA", "status": "running", "started_at": 100},
            sync_placeholder=False,
        )
    )
    asyncio.run(
        subagent_module._upsert_subagent_run(
            "pc1", "pm1", "saB",
            {"subagent_id": "saB", "status": "running", "started_at": 101},
            sync_placeholder=False,
        )
    )
    # saA finishes. The terminal patch carries no started_at of its own, but the
    # atomic merge over the prior entry keeps it — so the timer always has both
    # bounds (this is the "Done vs Researched for 7m" root cause).
    asyncio.run(
        subagent_module._upsert_subagent_run(
            "pc1", "pm1", "saA",
            {"subagent_id": "saA", "status": "done", "ended_at": 200, "final_text": "x"},
            sync_placeholder=False,
        )
    )
    runs = fake.message["subagent_runs"]
    assert runs["saA"]["started_at"] == 100  # not lost on terminal write
    assert runs["saA"]["ended_at"] == 200
    assert runs["saA"]["status"] == "done"
    assert "saB" in runs  # concurrent sibling untouched


def test_upsert_skips_local_chat(monkeypatch):
    fake = _FakeAtomicChats({"id": "pm1"})
    monkeypatch.setattr(subagent_module, "Chats", fake)
    out = asyncio.run(
        subagent_module._upsert_subagent_run(
            "local:abc", "pm1", "saA", {"status": "running"}, sync_placeholder=False
        )
    )
    assert out is None
    assert "subagent_runs" not in fake.message


def test_upsert_never_reports_uncommitted_mutator_state_as_durable(monkeypatch):
    class FailedWriter:
        async def update_message_subagent_run_atomic(
            self, _chat_id, _message_id, _entry_key, mutator
        ):
            # The mutator runs and fills its in-memory holder, but the DB layer
            # reports no committed patch (missing row / failed commit).
            mutator({})
            return None

    monkeypatch.setattr(subagent_module, "Chats", FailedWriter())
    out = asyncio.run(
        subagent_module._upsert_subagent_run(
            "pc1",
            "pm1",
            "sa1",
            {"subagent_id": "sa1", "status": "running"},
            sync_placeholder=False,
        )
    )
    assert out is None


def test_rerun_claim_cas_is_idempotent_only_for_its_own_generation(monkeypatch):
    fake = _FakeAtomicChats(
        {
            "subagent_runs": {
                "sa1": {
                    "subagent_id": "sa1",
                    "status": "running",
                    "rerun_id": "generation-1",
                }
            }
        }
    )
    monkeypatch.setattr(subagent_module, "Chats", fake)

    same = asyncio.run(
        subagent_module._upsert_subagent_run(
            "pc1",
            "pm1",
            "sa1",
            {
                "status": "running",
                "rerun": True,
                "rerun_id": "generation-1",
            },
            sync_placeholder=False,
            cas_block_if_running=True,
        )
    )
    assert same["rerun_id"] == "generation-1"

    with pytest.raises(subagent_module.SubagentRerunBlockedError):
        asyncio.run(
            subagent_module._upsert_subagent_run(
                "pc1",
                "pm1",
                "sa1",
                {
                    "status": "running",
                    "rerun": True,
                    "rerun_id": "generation-2",
                },
                sync_placeholder=False,
                cas_block_if_running=True,
            )
        )


def test_message_write_lock_serializes_same_key():
    """The per-(chat,message) lock must give mutual exclusion for the same key
    and run different keys concurrently."""
    from open_webui.models.chats import _message_write_locks

    order = []

    async def worker(key, tag, hold):
        async with _message_write_locks.lock_for("c", key):
            order.append(f"{tag}-enter")
            await asyncio.sleep(hold)
            order.append(f"{tag}-exit")

    async def _run():
        # Two workers on the SAME key must not interleave.
        await asyncio.gather(
            worker("m1", "a", 0.02),
            worker("m1", "b", 0.0),
        )

    asyncio.run(_run())
    # 'a' acquired first; 'b' cannot enter until 'a' exits.
    assert order == ["a-enter", "a-exit", "b-enter", "b-exit"]


def test_message_write_lock_reaped_when_idle():
    from open_webui.models.chats import _message_write_locks

    async def _run():
        async with _message_write_locks.lock_for("c", "reapme"):
            pass

    asyncio.run(_run())
    # No coroutine holds/waits → the entry is reaped (no unbounded growth).
    assert ("c", "reapme") not in _message_write_locks._locks


def test_emit_subagent_terminal_builds_error_event():
    captured = []

    async def emitter(payload):
        captured.append(payload)

    asyncio.run(
        subagent_module._emit_subagent_terminal(
            emitter,
            {"subagent_id": "sa1", "tool_call_id": "tc1"},
            status="error",
            message="boom",
        )
    )
    assert len(captured) == 1
    data = captured[0]["data"]
    assert captured[0]["type"] == "chat:subagent:update"
    assert data["subagent_id"] == "sa1"
    inner = data["inner_event"]
    assert inner["type"] == "chat:message:error"
    assert inner["data"]["error"] == "boom"


def test_emit_subagent_terminal_builds_cancel_event():
    captured = []

    async def emitter(payload):
        captured.append(payload)

    asyncio.run(
        subagent_module._emit_subagent_terminal(
            emitter, {"subagent_id": "sa1"}, status="cancelled"
        )
    )
    assert captured[0]["data"]["inner_event"]["type"] == "chat:tasks:cancel"


def test_parent_terminal_sweep_does_not_own_detached_rerun():
    state = {
        "subagent_runs": {
            "inline": {
                "subagent_id": "inline",
                "status": "running",
                "started_at": 10,
            },
            "redo": {
                "subagent_id": "redo",
                "status": "running",
                "started_at": 11,
                "rerun": True,
                "rerun_id": "redo-generation",
            },
        },
        "content_blocks": [],
    }

    async def fake_atomic(_chat_id, _message_id, mutator):
        partial = mutator(dict(state))
        if partial:
            state.update(partial)
        return partial

    async def go():
        with patch.object(
            subagent_module.Chats,
            "update_message_fields_atomic",
            side_effect=fake_atomic,
        ):
            return await subagent_module.sweep_subagent_runs_terminal(
                "parent-chat", "parent-message", fallback_status="cancelled"
            )

    assert asyncio.run(go()) is True
    assert state["subagent_runs"]["inline"]["status"] == "cancelled"
    assert state["subagent_runs"]["redo"]["status"] == "running"
    assert state["subagent_runs"]["redo"]["rerun_id"] == "redo-generation"
