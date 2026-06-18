"""Tests for the subagent reliability fixes:

* ``reconcile_block_results_from_runs`` — backend write-path backfill of missing/
  empty subagent tool results from the durable ``subagent_runs`` mirror.
* ``_total_text_block_len`` — the order-independent productivity signal the
  agentic loop's empty-round retry keys off.
* ``AGENTIC_EMPTY_ROUND_MAX_RETRIES`` env default/override.

These are pure-function tests (no DB / no event loop needed). DB env is still
pointed at a temp copy in case importing the modules touches it.
"""

import os
import shutil
import tempfile

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "subagent_reliability_test.db")
_HERE = os.path.dirname(__file__)
_DEV_DB = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "data", "webui.db"))
if os.path.exists(_DEV_DB):
    shutil.copy(_DEV_DB, _DB_PATH)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB_PATH}")

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


def test_empty_round_retry_env_default_is_three():
    from open_webui.env import AGENTIC_EMPTY_ROUND_MAX_RETRIES

    assert AGENTIC_EMPTY_ROUND_MAX_RETRIES == 3


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
        msg = (
            "was cancelled or timed out"
            if isinstance(exc, asyncio.CancelledError)
            else f"failed: {exc}"
        )
        return {"tool_call_id": tcid, "content": f"Tool '{name}' {msg}."}

    call = {"id": "c1", "function": {"name": "subagent_launch"}}
    r_cancel = result_for_failed_call(call, asyncio.CancelledError())
    assert r_cancel["tool_call_id"] == "c1"
    assert r_cancel["content"].strip()
    assert "cancelled" in r_cancel["content"]

    r_err = result_for_failed_call(call, RuntimeError("boom"))
    assert "boom" in r_err["content"]


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


# -- Streaming chat-completion timeout (no 5-min total cap) -------------------
# The upstream streaming session must NOT inherit aiohttp's 5-minute `total`
# default, which cancelled long subagents mid-generation. We pin the env shape so
# a regression that reintroduces a total cap is caught.


def test_stream_timeout_uses_sock_read_not_total():
    from open_webui.env import (
        AIOHTTP_CLIENT_TIMEOUT,
        AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ,
    )
    import aiohttp

    # Overall total is unbounded (None) by default — long generations never get
    # a hard wall-clock cut-off.
    assert AIOHTTP_CLIENT_TIMEOUT is None
    # The dead-connection guard is a positive integer by default.
    assert isinstance(AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ, int)
    assert AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ > 0

    t = aiohttp.ClientTimeout(
        total=None, sock_read=AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ
    )
    assert t.total is None
    assert t.sock_read == AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ


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

    assert _visible_nonstreaming_reasoning(
        {"reasoning": " shown ", "reasoning_details": [{"summary": "fallback"}]}
    ) == "shown"


def test_visible_nonstreaming_reasoning_ignores_non_string_reasoning_field():
    from open_webui.utils.middleware import _visible_nonstreaming_reasoning

    assert _visible_nonstreaming_reasoning(
        {"reasoning": {"not": "text"}, "reasoning_details": [{"summary": "fallback"}]}
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
