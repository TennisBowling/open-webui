"""Tests for the 2026-06-27 subagent "random stop / Stopped" hardening pass.

Two root causes, two fix families:

FIX 1 — deterministic provider errors (the trigger). A long research subagent
can accumulate context until the provider rejects the active model. Re-issuing
that same request on that model cannot succeed, so the round-level retry remains
disabled. Input-context errors are classified separately so the subagent runner
can hand the failed turn to its configured long-context successor; output-token
truncation remains terminal without switching models.

FIX 2 — stranded 'running' entries (the "Stopped" symptom). A detached rerun whose
task DIED (server restart / a cancel that truncated the terminal write) leaves the
parent ``subagent_runs`` entry at ``status='running'`` forever; only the frontend
downgrades it to "Stopped" on reload. ``reconcile_stranded_subagent_runs`` heals it
durably — liveness-gated so a genuinely in-flight run is never stomped.

Pure-function / async-via-asyncio.run tests (no live DB; the atomic writer and
hidden-chat reads are mocked so the logic runs purely in-memory).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from test.util.db import configure_test_database

configure_test_database()

from open_webui.utils import subagent as sub  # noqa: E402
from open_webui.routers import subagents as subagent_router  # noqa: E402
from open_webui.models.chats import Chats  # noqa: E402
from open_webui.utils.subagent import SubagentNonRetryableError  # noqa: E402
from open_webui.utils.middleware import (  # noqa: E402
    _is_context_fallback_provider_error,
    _is_context_limit_provider_error,
    _is_nonretryable_provider_error,
    _provider_error_payload,
    _provider_error_text,
    _safe_error_response_text,
)


# ---------------------------------------------------------------------------
# FIX 1 — error classification
# ---------------------------------------------------------------------------


def test_classifier_context_window_variants_are_nonretryable():
    # Exact persisted shape + wording observed in the live DB for the failed
    # gpt-5.6-terra subagent turns that were manually retried on Gemini.
    observed_error = {
        "content": (
            "Your input exceeds the context window of this model. "
            "Please adjust your input and try again."
        )
    }
    assert _is_context_limit_provider_error(observed_error)
    assert _is_nonretryable_provider_error(observed_error)

    # The same provider wording can also arrive nested under ``message`` or as
    # a raw string depending on which completion path surfaced the failure.
    assert _is_nonretryable_provider_error(
        {
            "message": "Your input exceeds the context window of this model. "
            "Please adjust your input and try again."
        }
    )
    assert _is_nonretryable_provider_error(
        "Your input exceeds the context window of this model."
    )
    # Other providers' phrasings.
    assert _is_nonretryable_provider_error(
        "This model's maximum context length is 8192 tokens"
    )
    assert _is_nonretryable_provider_error(
        "prompt is too long: 250000 tokens > 200000 maximum"
    )
    assert _is_nonretryable_provider_error("context_length_exceeded")
    # finish_reason=length surfaced by _nonstreaming_round_length_error.
    assert _is_nonretryable_provider_error(
        "Model reached the completion token limit before producing final text"
    )


def test_context_classifier_excludes_output_limit_and_transient_failures():
    assert _is_context_limit_provider_error(
        "Your input exceeds the context window of this model."
    )
    assert _is_context_limit_provider_error(
        {"error": {"message": "context_length_exceeded"}}
    )
    assert not _is_context_limit_provider_error(
        "Model reached the completion token limit before producing final text"
    )
    assert not _is_context_limit_provider_error("502 Server Error")


def test_terminal_model_turn_request_error_is_nonretryable_but_not_context_limit():
    observed = "Requests ending with a model turn are not supported."
    assert _is_nonretryable_provider_error(observed)
    assert not _is_context_limit_provider_error(observed)


def test_retry_exhausted_provider_masking_errors_are_context_fallback_eligible():
    # Exact terminal messages observed on the recent gpt-5.6-terra subagents.
    # These remain retryable at the individual provider-call level, but after
    # all round retries are exhausted the subagent may hand off to its configured
    # long-context model.
    empty = "The model returned no response after retrying 5 times. Please try again."
    reset = (
        "upstream connect error or disconnect/reset before headers. "
        "reset reason: connection termination"
    )
    assert _is_context_fallback_provider_error(empty)
    assert _is_context_fallback_provider_error(reset)
    assert not _is_nonretryable_provider_error(empty)
    assert not _is_nonretryable_provider_error(reset)

    empty_payload = _provider_error_payload(
        empty, retries_exhausted=True, empty_response=True
    )
    reset_payload = _provider_error_payload(reset, retries_exhausted=True)
    assert empty_payload == {
        "content": empty,
        "code": "empty_response_retries_exhausted",
        "retry_exhausted": True,
    }
    assert reset_payload == {
        "content": reset,
        "code": "provider_connection_retries_exhausted",
        "retry_exhausted": True,
    }
    assert _is_context_fallback_provider_error(empty_payload)
    assert _is_context_fallback_provider_error(reset_payload)


def test_error_payload_preserves_explicit_context_code_without_retry_exhaustion():
    payload = _provider_error_payload(
        {
            "error": {
                "code": "context_length_exceeded",
                "message": "request too large",
            }
        }
    )
    assert payload == {
        "content": "request too large",
        "code": "context_length_exceeded",
    }
    assert _is_context_limit_provider_error(payload)


def test_classifier_transient_errors_stay_retryable():
    for msg in (
        "Bad gateway",
        "502 Server Error",
        "Rate limit exceeded, please try again later",
        "Read timed out.",
        "Connection reset by peer",
        "Provider returned an error during streaming.",
        "Internal server error",
        "",
        None,
    ):
        assert _is_nonretryable_provider_error(msg) is False, msg


def test_provider_error_text_unwraps_nested_shapes():
    assert _provider_error_text("plain") == "plain"
    assert _provider_error_text({"content": "c"}) == "c"
    assert _provider_error_text({"message": "m"}) == "m"
    assert _provider_error_text({"detail": "d"}) == "d"
    # OpenAI-style nested {"error": {"message": ...}}
    assert _provider_error_text({"error": {"message": "nested"}}) == "nested"
    assert _provider_error_text(None) == ""


def test_safe_error_response_text_reads_body_and_dict():
    class Resp:
        body = b'{"error":{"message":"exceeds the context window"}}'

    assert "context window" in (_safe_error_response_text(Resp()) or "")
    assert "context length" in (
        _safe_error_response_text({"error": {"message": "context length"}}) or ""
    )
    # A consumed/streaming response with no readable body -> None (treated retryable).
    assert _safe_error_response_text(object()) is None
    assert _is_nonretryable_provider_error(_safe_error_response_text(object())) is False


def test_nonretryable_error_is_exception_subclass():
    # Must subclass Exception (so existing ``except Exception`` handlers catch it)
    # AND RuntimeError (so callers can keep treating it as a generation failure).
    assert issubclass(SubagentNonRetryableError, Exception)
    assert issubclass(SubagentNonRetryableError, RuntimeError)
    e = SubagentNonRetryableError("ctx window")
    assert isinstance(e, Exception)
    assert str(e) == "ctx window"


# ---------------------------------------------------------------------------
# Timeout fix — non-streaming requests get a GENEROUS sock_read, not the tight
# streaming one (so a slow non-streaming subagent round isn't cut off, while a
# dead socket is still eventually caught instead of hanging forever).
# ---------------------------------------------------------------------------


def test_nonstream_sock_read_is_generous_relative_to_stream():
    import aiohttp
    from open_webui.env import (
        AIOHTTP_CLIENT_TIMEOUT_STREAM_SOCK_READ as STREAM,
        AIOHTTP_CLIENT_TIMEOUT_NONSTREAM_SOCK_READ as NONSTREAM,
    )

    # A non-streaming body arrives only when generation finishes, so its sock_read
    # must be far more generous than the streaming (inter-token) one.
    assert NONSTREAM is None or (
        isinstance(NONSTREAM, int) and isinstance(STREAM, int) and NONSTREAM > STREAM
    )

    # Mirror the inline selection in generate_chat_completion: total stays None
    # (never a wall-clock cap), only sock_read differs by mode.
    def _timeout_for(provider_payload_stream):
        sock_read = STREAM if provider_payload_stream else NONSTREAM
        return aiohttp.ClientTimeout(total=None, sock_read=sock_read)

    stream_t = _timeout_for(True)
    nonstream_t = _timeout_for(False)
    assert stream_t.total is None and nonstream_t.total is None
    assert stream_t.sock_read == STREAM
    assert nonstream_t.sock_read == NONSTREAM


# ---------------------------------------------------------------------------
# FIX 2 — _stranded_running_candidates (pure selector)
# ---------------------------------------------------------------------------


def _chat(messages: dict):
    return SimpleNamespace(id="parent1", chat={"history": {"messages": messages}})


def test_candidates_pick_running_without_ended_at():
    chat = _chat(
        {
            "m1": {
                "subagent_runs": {
                    "s_run": {"status": "running", "subagent_id": "s_run",
                              "assistant_msg_id": "a1", "started_at": 100},
                    "s_done": {"status": "done", "subagent_id": "s_done"},
                    "s_err": {"status": "error", "subagent_id": "s_err"},
                    "s_cancel": {"status": "cancelled", "subagent_id": "s_cancel"},
                    # 'running' but already stamped ended_at -> NOT stranded.
                    "s_ended": {"status": "running", "ended_at": 5,
                                "subagent_id": "s_ended"},
                }
            }
        }
    )
    cands = sub._stranded_running_candidates(chat, set())
    keys = {c["entry_key"] for c in cands}
    assert keys == {"s_run"}
    c = cands[0]
    assert c["subagent_id"] == "s_run"
    assert c["assistant_msg_id"] == "a1"
    assert c["started_at"] == 100
    assert c["message_id"] == "m1"


def test_candidates_skip_live_rerun_keys_and_missing_sid():
    chat = _chat(
        {
            "m1": {
                "subagent_runs": {
                    "live_rerun": {"status": "running", "subagent_id": "x"},
                    "no_sid": {"status": "running"},  # no subagent_id/chat_id
                    "ok": {"status": "running", "subagent_id": "y"},
                }
            }
        }
    )
    cands = sub._stranded_running_candidates(chat, {"live_rerun"})
    assert {c["entry_key"] for c in cands} == {"ok"}


def test_candidates_skip_by_subagent_id_protects_launch_entry_of_continuation_redo():
    # Round-1 adversarial finding: a from_launch redo clicked on a CONTINUATION card
    # registers its redis id under the continuation key `{sid}#{tcid}`, but flips the
    # LAUNCH entry (bare `sid`) to 'running' and wipes the hidden chat. Keying the skip
    # only on the literal continuation key would miss the LIVE launch entry and stomp
    # it. The subagent-id gate must protect ALL entries of that subagent.
    sid = "sa1"
    chat = _chat(
        {
            "m1": {
                "subagent_runs": {
                    # LAUNCH entry (key == bare sid) flipped to running by the redo.
                    sid: {"status": "running", "subagent_id": sid,
                          "assistant_msg_id": "a1", "started_at": 100},
                    # The continuation that was clicked (also of sid).
                    f"{sid}#tc9": {"status": "running", "subagent_id": sid},
                    # An unrelated, genuinely stranded subagent — must still heal.
                    "sa2": {"status": "running", "subagent_id": "sa2"},
                }
            }
        }
    )
    # redis only knows the CLICKED continuation key.
    rerun_keys = {f"{sid}#tc9"}
    live_rerun_sids = {k.split("#", 1)[0] for k in rerun_keys}  # -> {"sa1"}
    cands = sub._stranded_running_candidates(chat, rerun_keys, live_rerun_sids)
    # The launch entry (sid) is protected by the subagent-id gate; only the unrelated
    # sa2 remains a candidate.
    assert {c["entry_key"] for c in cands} == {"sa2"}


def test_candidates_subagent_id_gate_is_noop_without_live_reruns():
    chat = _chat(
        {"m1": {"subagent_runs": {"a": {"status": "running", "subagent_id": "sa_a"}}}}
    )
    # Default empty live_rerun_sids -> nothing protected, entry is a candidate.
    cands = sub._stranded_running_candidates(chat, set())
    assert {c["entry_key"] for c in cands} == {"a"}


def test_candidates_scan_multiple_messages_and_falls_back_to_chat_id():
    chat = _chat(
        {
            "m1": {"subagent_runs": {"a": {"status": "running", "subagent_id": "sa_a"}}},
            "m2": {"subagent_runs": {"b": {"status": "running", "chat_id": "sa_b"}}},
            "m3": {"role": "user"},  # no subagent_runs
        }
    )
    cands = sub._stranded_running_candidates(chat, set())
    by_key = {c["entry_key"]: c for c in cands}
    assert set(by_key) == {"a", "b"}
    assert by_key["a"]["subagent_id"] == "sa_a"
    assert by_key["b"]["subagent_id"] == "sa_b"  # chat_id fallback


# ---------------------------------------------------------------------------
# FIX 2 — _terminalize_stranded_entry (conditional atomic write)
# ---------------------------------------------------------------------------


def _run_terminalize(existing, *, entry_key, started_at, final_text):
    captured = {}

    async def fake_atomic(
        chat_id, msg_id, target_entry_key, mutator, *, touch_chat=False
    ):
        assert target_entry_key == entry_key
        assert touch_chat is True
        captured["out"] = mutator(existing)
        return captured["out"]

    async def go():
        with patch.object(
            sub.Chats,
            "update_message_subagent_run_atomic",
            side_effect=fake_atomic,
        ):
            wrote = await sub._terminalize_stranded_entry(
                "parent1", "m1", entry_key,
                started_at=started_at, final_text=final_text,
            )
        return wrote, captured.get("out")

    return asyncio.run(go())


def test_terminalize_recovers_final_text_as_done():
    ex = {"subagent_runs": {"s": {"status": "running", "started_at": 100}}}
    wrote, out = _run_terminalize(
        ex, entry_key="s", started_at=100, final_text="the answer"
    )
    r = out["subagent_runs"]["s"]
    assert wrote is True
    assert r["status"] == "done"
    assert r["final_text"] == "the answer"
    assert r["ended_at"] is not None


def test_terminalize_recovered_answer_replaces_parent_tool_result_atomically():
    ex = {
        "subagent_runs": {
            "s": {
                "status": "running",
                "started_at": 100,
                "subagent_id": "hidden",
                "tool_call_id": "tc1",
            }
        },
        "content_blocks": [
            {
                "type": "tool_calls",
                "content": [
                    {
                        "id": "tc1",
                        "function": {"name": "subagent_launch"},
                    }
                ],
                "results": [
                    {
                        "tool_call_id": "tc1",
                        "subagent_id": "hidden",
                        "content": "old failed result",
                        "error": True,
                    }
                ],
            }
        ],
    }
    wrote, out = _run_terminalize(
        ex, entry_key="s", started_at=100, final_text="recovered answer"
    )

    assert wrote is True
    result = out["content_blocks"][0]["results"][0]
    assert result["content"] == "recovered answer"
    assert "error" not in result


def test_terminalize_no_answer_is_cancelled_no_final_text():
    ex = {"subagent_runs": {"s": {"status": "running", "started_at": 100}}}
    wrote, out = _run_terminalize(ex, entry_key="s", started_at=100, final_text="")
    r = out["subagent_runs"]["s"]
    assert wrote is True
    assert r["status"] == "cancelled"
    assert "final_text" not in r


def test_terminalize_promotes_entrys_own_preserved_final_text_when_scrape_empty():
    # Round-2 adversarial finding: a from_launch redo preserves the prior answer on
    # the ENTRY (C5) but wipes the hidden chat, so the scrape returns '' after a task
    # death. We must NOT mislabel 'cancelled' and hide the answer — promote to 'done'
    # off the entry's own final_text (mirrors sweep_subagent_runs_terminal).
    ex = {"subagent_runs": {"s": {"status": "running", "started_at": 100,
                                  "final_text": "the prior answer"}}}
    wrote, out = _run_terminalize(ex, entry_key="s", started_at=100, final_text="")
    r = out["subagent_runs"]["s"]
    assert wrote is True
    assert r["status"] == "done"
    assert r["final_text"] == "the prior answer"


def test_terminalize_fresh_scrape_wins_over_stale_entry_final_text():
    # A non-empty fresh scrape is preferred over the entry's own (older) final_text.
    ex = {"subagent_runs": {"s": {"status": "running", "started_at": 100,
                                  "final_text": "stale"}}}
    wrote, out = _run_terminalize(ex, entry_key="s", started_at=100, final_text="fresh")
    r = out["subagent_runs"]["s"]
    assert r["status"] == "done"
    assert r["final_text"] == "fresh"


def test_terminalize_skips_when_reclaimed_with_new_started_at():
    # A rerun re-claimed the entry between the idle check and this write (fresh
    # started_at) — must NOT be stomped.
    ex = {"subagent_runs": {"s": {"status": "running", "started_at": 999}}}
    wrote, out = _run_terminalize(ex, entry_key="s", started_at=100, final_text="x")
    assert wrote is False
    assert out is None


def test_terminalize_skips_when_already_terminal():
    ex = {"subagent_runs": {"s": {"status": "done", "started_at": 100,
                                  "final_text": "real"}}}
    wrote, out = _run_terminalize(ex, entry_key="s", started_at=100, final_text="x")
    assert wrote is False
    assert out is None


def test_terminalize_skips_when_ended_at_present():
    ex = {"subagent_runs": {"s": {"status": "running", "ended_at": 7,
                                  "started_at": 100}}}
    wrote, out = _run_terminalize(ex, entry_key="s", started_at=100, final_text="x")
    assert wrote is False


# ---------------------------------------------------------------------------
# FIX 2 — reconcile_stranded_subagent_runs (orchestration)
# ---------------------------------------------------------------------------


def _reconcile(chat, *, parent_live, rerun_keys, generating, final_text,
               sub_chat=SimpleNamespace(id="hidden")):
    """Drive reconcile with hidden-chat reads + atomic writer + broadcast mocked."""
    writes = []
    broadcasts = []

    async def fake_atomic(
        chat_id, msg_id, _entry_key, mutator, *, touch_chat=False
    ):
        assert touch_chat is True
        # Apply against the live in-memory message so the conditional re-check
        # (still-running + same started_at) runs for real.
        existing = chat.chat["history"]["messages"][msg_id]
        out = mutator(existing)
        if out is not None:
            existing.update(out)
            writes.append((msg_id, out))
        return out

    async def fake_get_chat(cid):
        return sub_chat

    async def fake_extract(cid, aid):
        return final_text

    async def fake_broadcast(pcid, mid, uid):
        broadcasts.append((pcid, mid, uid))

    async def go():
        with patch.object(sub.Chats, "get_chat_by_id", side_effect=fake_get_chat), \
             patch.object(sub.Chats, "update_message_subagent_run_atomic", side_effect=fake_atomic), \
             patch.object(sub, "_subagent_inner_chat_generating", return_value=generating), \
             patch.object(sub, "_extract_final_text", side_effect=fake_extract), \
             patch.object(sub, "broadcast_subagent_terminals", side_effect=fake_broadcast):
            healed = await sub.reconcile_stranded_subagent_runs(
                chat, parent_live=parent_live,
                live_rerun_entry_keys=rerun_keys, user_id="u1",
            )
        return healed, writes, broadcasts

    return asyncio.run(go())


def _one_running_chat():
    return _chat(
        {
            "m1": {
                "subagent_runs": {
                    "s": {"status": "running", "subagent_id": "hidden",
                          "assistant_msg_id": "a1", "started_at": 100}
                }
            }
        }
    )


def test_reconcile_early_out_when_parent_live():
    chat = _one_running_chat()
    healed, writes, broadcasts = _reconcile(
        chat, parent_live=True, rerun_keys=[], generating=False, final_text="x"
    )
    assert healed == 0
    assert writes == [] and broadcasts == []
    # Entry untouched.
    assert chat.chat["history"]["messages"]["m1"]["subagent_runs"]["s"]["status"] == "running"


def test_reconcile_skips_generating_hidden_chat():
    chat = _one_running_chat()
    healed, writes, broadcasts = _reconcile(
        chat, parent_live=False, rerun_keys=[], generating=True, final_text="x"
    )
    assert healed == 0
    assert writes == []


def test_reconcile_skips_live_rerun_entry():
    chat = _one_running_chat()
    healed, writes, broadcasts = _reconcile(
        chat, parent_live=False, rerun_keys=["s"], generating=False, final_text="x"
    )
    assert healed == 0
    assert writes == []


def test_reconcile_terminalizes_idle_stranded_recovering_answer():
    chat = _one_running_chat()
    healed, writes, broadcasts = _reconcile(
        chat, parent_live=False, rerun_keys=[], generating=False, final_text="recovered"
    )
    assert healed == 1
    r = chat.chat["history"]["messages"]["m1"]["subagent_runs"]["s"]
    assert r["status"] == "done"
    assert r["final_text"] == "recovered"
    assert broadcasts == [("parent1", "m1", "u1")]


def test_reconcile_terminalizes_idle_stranded_no_answer_to_cancelled():
    chat = _one_running_chat()
    healed, writes, broadcasts = _reconcile(
        chat, parent_live=False, rerun_keys=[], generating=False, final_text=""
    )
    assert healed == 1
    r = chat.chat["history"]["messages"]["m1"]["subagent_runs"]["s"]
    assert r["status"] == "cancelled"
    assert broadcasts == [("parent1", "m1", "u1")]


def test_reconcile_deleted_hidden_chat_terminalizes_cancelled():
    chat = _one_running_chat()
    healed, writes, broadcasts = _reconcile(
        chat, parent_live=False, rerun_keys=[], generating=False, final_text="x",
        sub_chat=None,  # hidden chat deleted
    )
    assert healed == 1
    r = chat.chat["history"]["messages"]["m1"]["subagent_runs"]["s"]
    assert r["status"] == "cancelled"


def test_reconcile_no_candidates_no_broadcast():
    chat = _chat({"m1": {"subagent_runs": {"s": {"status": "done"}}}})
    healed, writes, broadcasts = _reconcile(
        chat, parent_live=False, rerun_keys=[], generating=False, final_text="x"
    )
    assert healed == 0
    assert broadcasts == []


def test_reconcile_protects_launch_entry_when_continuation_redo_is_live():
    # End-to-end of the round-1 finding: reconcile must derive live_rerun_sids from the
    # CLICKED continuation key and NOT terminalize the same subagent's live launch entry
    # (its hidden chat is mid-wipe, so generating=False would otherwise let it through).
    sid = "sa1"
    chat = _chat(
        {
            "m1": {
                "subagent_runs": {
                    sid: {"status": "running", "subagent_id": sid,
                          "assistant_msg_id": "a1", "started_at": 100},
                }
            }
        }
    )
    healed, writes, broadcasts = _reconcile(
        chat,
        parent_live=False,
        rerun_keys=[f"{sid}#tc9"],  # redis only has the clicked continuation key
        generating=False,           # hidden chat looks idle (currentId=None mid-wipe)
        final_text="x",
    )
    assert healed == 0
    assert writes == [] and broadcasts == []
    assert chat.chat["history"]["messages"]["m1"]["subagent_runs"][sid]["status"] == "running"


# ---------------------------------------------------------------------------
# Continue guard — stale running entries must not wedge future continues
# ---------------------------------------------------------------------------


def test_continue_reconcile_helper_blocks_when_hidden_chat_generating():
    chat = _chat(
        {
            "m1": {
                "subagent_runs": {
                    "s": {"status": "running", "subagent_id": "hidden", "started_at": 100}
                }
            }
        }
    )
    terminalized = []

    async def fake_terminalize(*args, **kwargs):
        terminalized.append((args, kwargs))
        return True

    async def go():
        with patch.object(sub, "_subagent_inner_chat_generating", return_value=True), \
             patch.object(sub, "_terminalize_stranded_entry", side_effect=fake_terminalize):
            return await sub._reconcile_idle_running_turns_for_subagent(
                chat, SimpleNamespace(id="hidden"), "hidden"
            )

    assert asyncio.run(go()) is False
    assert terminalized == []


def test_continue_reconcile_helper_does_not_cancel_fresh_setup_claim():
    chat = _chat(
        {
            "m1": {
                "subagent_runs": {
                    "s": {
                        "status": "running",
                        "subagent_id": "hidden",
                        "started_at": 1_000,
                    }
                }
            }
        }
    )
    terminalized = []

    async def fake_terminalize(*args, **kwargs):
        terminalized.append((args, kwargs))
        return True

    async def go():
        with patch.object(
            sub, "_subagent_inner_chat_generating", return_value=False
        ), patch.object(sub.time, "time", return_value=1_005), patch.object(
            sub, "_terminalize_stranded_entry", side_effect=fake_terminalize
        ):
            return await sub._reconcile_idle_running_turns_for_subagent(
                chat, SimpleNamespace(id="hidden"), "hidden"
            )

    assert asyncio.run(go()) is False
    assert terminalized == []
    assert sub._running_entry_may_be_in_setup(
        {"status": "running", "started_at": 1_000}, now=1_005
    )
    assert not sub._running_entry_may_be_in_setup(
        {"status": "running", "started_at": 1_000}, now=1_100
    )


def test_continue_reconcile_helper_terminalizes_idle_stale_running_turns():
    chat = _chat(
        {
            "m1": {
                "subagent_runs": {
                    "s": {
                        "status": "running",
                        "subagent_id": "hidden",
                        "assistant_msg_id": "a1",
                        "started_at": 100,
                    },
                    "other": {"status": "running", "subagent_id": "other"},
                }
            }
        }
    )
    terminalized = []

    async def fake_extract(cid, aid):
        assert (cid, aid) == ("hidden", "a1")
        return "recovered answer"

    async def fake_terminalize(parent_chat_id, message_id, entry_key, **kwargs):
        terminalized.append((parent_chat_id, message_id, entry_key, kwargs))
        return True

    async def go():
        with patch.object(sub, "_subagent_inner_chat_generating", return_value=False), \
             patch.object(sub, "_extract_final_text", side_effect=fake_extract), \
             patch.object(sub, "_terminalize_stranded_entry", side_effect=fake_terminalize):
            return await sub._reconcile_idle_running_turns_for_subagent(
                chat, SimpleNamespace(id="hidden"), "hidden"
            )

    assert asyncio.run(go()) is True
    assert terminalized == [
        (
            "parent1",
            "m1",
            "s",
            {"started_at": 100, "final_text": "recovered answer"},
        )
    ]


# ---------------------------------------------------------------------------
# Targeted detached-rerun stop endpoint
# ---------------------------------------------------------------------------


def _fake_request(redis=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=redis)))


def test_stop_subagent_rerun_matches_continuation_and_launch_keys():
    stopped = []

    async def fake_get_parent(chat_id, user_id):
        assert (chat_id, user_id) == ("chat1", "u1")
        return SimpleNamespace(id="chat1")

    async def fake_item_keys(redis, prefix):
        assert prefix == "subagent-rerun:chat1:"
        return [
            "subagent-rerun:chat1:sa1#call2",
            "subagent-rerun:chat1:other",
        ]

    async def fake_task_ids(redis, item_id):
        return {
            "subagent-rerun:chat1:sa1#call2": ["t1", "t2"],
            "subagent-rerun:chat1:sa1": ["t2", "t3"],
            "subagent-rerun:chat1:other": ["other"],
        }.get(item_id, [])

    async def fake_stop(redis, task_id):
        stopped.append(task_id)
        return {"status": True}

    async def go():
        with patch.object(
            Chats,
            "get_chat_by_id_and_user_id",
            side_effect=fake_get_parent,
        ), patch.object(
            subagent_router, "list_item_keys_by_prefix", side_effect=fake_item_keys
        ), patch.object(
            subagent_router, "list_task_ids_by_item_id", side_effect=fake_task_ids
        ), patch.object(subagent_router, "stop_task", side_effect=fake_stop):
            return await subagent_router.stop_subagent_rerun(
                _fake_request(),
                subagent_router.SubagentRerunStopForm(
                    parent_chat_id="chat1",
                    parent_message_id="m1",
                    entry_key="sa1",
                    subagent_id="sa1",
                ),
                user=SimpleNamespace(id="u1"),
            )

    res = asyncio.run(go())
    assert res == {"status": True, "task_ids": ["t1", "t2", "t3"], "stopped": 3}
    assert stopped == ["t1", "t2", "t3"]


def test_stop_subagent_rerun_reconciles_when_task_registry_empty():
    reconciles = []
    parent = SimpleNamespace(id="chat1")

    async def fake_get_parent(chat_id, user_id):
        return parent

    async def fake_item_keys(redis, prefix):
        return []

    async def fake_task_ids(redis, item_id):
        return []

    async def fake_reconcile(parent_chat, **kwargs):
        reconciles.append((parent_chat, kwargs))
        return 1

    async def go():
        with patch.object(
            Chats,
            "get_chat_by_id_and_user_id",
            side_effect=fake_get_parent,
        ), patch.object(
            subagent_router, "list_item_keys_by_prefix", side_effect=fake_item_keys
        ), patch.object(
            subagent_router, "list_task_ids_by_item_id", side_effect=fake_task_ids
        ), patch.object(sub, "reconcile_stranded_subagent_runs", side_effect=fake_reconcile):
            return await subagent_router.stop_subagent_rerun(
                _fake_request(),
                subagent_router.SubagentRerunStopForm(
                    parent_chat_id="chat1",
                    parent_message_id="m1",
                    entry_key="sa1",
                    subagent_id="sa1",
                ),
                user=SimpleNamespace(id="u1"),
            )

    res = asyncio.run(go())
    assert res == {"status": True, "task_ids": [], "stopped": 0}
    assert reconciles == [
        (
            parent,
            {"parent_live": False, "live_rerun_entry_keys": [], "user_id": "u1"},
        )
    ]
