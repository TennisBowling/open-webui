"""Tests for the subagent reload / background-persistence hardening pass.

Covers:
* ``sweep_subagent_runs_terminal`` (B2) — the finalizer backstop that flips every
  non-terminal ``subagent_runs`` entry terminal (result-aware: prefers 'done' and
  even PROMOTES a wrongly-downgraded finished run), so a finalized parent never
  leaves a subagent stuck "Researching…".
* ``_json_from_db`` / ``_lenient_json_loads`` (B4) — iterative + lenient decode so
  a double-encoded jsonb-STRING ``meta`` (asyncpg returns jsonb as raw text)
  resolves to the inner object and renders instead of breaking.
* ``list_item_task_ids_by_prefix`` (B3) — surfaces subagent reruns under a chat.

Pure-function/async-via-asyncio.run tests (no live DB; the atomic writer is
mocked so the mutator runs purely in-memory). ``pytest-asyncio`` is absent here,
so async coroutines are driven with ``asyncio.run`` inside sync test functions.
"""

import asyncio
import json
from unittest.mock import patch

from test.util.db import configure_test_database

configure_test_database()

from open_webui.utils import subagent as sub  # noqa: E402
from open_webui.models.chats import _json_from_db, _lenient_json_loads  # noqa: E402
from open_webui import tasks as tasks_mod  # noqa: E402


# ---------------------------------------------------------------------------
# B2 — sweep_subagent_runs_terminal
# ---------------------------------------------------------------------------


def _run_sweep(existing, fallback="cancelled"):
    """Invoke the sweep with the atomic writer mocked to run the mutator on
    ``existing`` and capture its returned update dict."""
    captured = {}

    async def fake_atomic(chat_id, msg_id, mutator):
        captured["out"] = mutator(existing)
        return captured["out"]

    async def go():
        with patch.object(
            sub.Chats, "update_message_fields_atomic", side_effect=fake_atomic
        ):
            changed = await sub.sweep_subagent_runs_terminal(
                "c1", "m1", fallback_status=fallback
            )
        return changed, captured.get("out")

    return asyncio.run(go())


def test_sweep_running_no_result_to_cancelled_stamped():
    ex = {
        "subagent_runs": {"a": {"status": "running", "started_at": 100}},
        "content_blocks": [],
    }
    changed, out = _run_sweep(ex)
    r = out["subagent_runs"]["a"]
    assert changed is True
    assert r["status"] == "cancelled"
    assert r["ended_at"] is not None
    assert r["started_at"] == 100


def test_sweep_running_with_content_block_result_to_done_backfills_final_text():
    ex = {
        "subagent_runs": {
            "a": {"status": "running", "tool_call_id": "tc1", "started_at": 50}
        },
        "content_blocks": [
            {
                "type": "tool_calls",
                "results": [{"tool_call_id": "tc1", "content": "the answer"}],
            }
        ],
    }
    changed, out = _run_sweep(ex)
    r = out["subagent_runs"]["a"]
    assert r["status"] == "done"
    assert r["final_text"] == "the answer"


def test_sweep_running_with_final_text_to_done():
    ex = {
        "subagent_runs": {
            "a": {"status": "running", "final_text": "done text", "started_at": 1}
        },
        "content_blocks": [],
    }
    _, out = _run_sweep(ex)
    assert out["subagent_runs"]["a"]["status"] == "done"


def test_sweep_promotes_wrongly_cancelled_finished_run():
    # H1: a finished subagent (has final_text) whose terminal write lost a race
    # and got stamped 'cancelled' must be PROMOTED back to 'done'.
    ex = {
        "subagent_runs": {
            "a": {
                "status": "cancelled",
                "final_text": "real answer",
                "started_at": 1,
                "ended_at": 9,
            }
        },
        "content_blocks": [],
    }
    _, out = _run_sweep(ex)
    assert out["subagent_runs"]["a"]["status"] == "done"


def test_sweep_error_run_with_error_string_result_stays_error():
    # A FAILED subagent returns an ERROR STRING as its content_blocks result
    # (non-empty). That must NOT be read as a real answer and promote it to
    # 'done' — promotion gates on final_text only, so an error run stays 'error'.
    ex = {
        "subagent_runs": {
            "a": {
                "status": "error",
                "tool_call_id": "tc1",
                "started_at": 1,
                "ended_at": 9,
            }
        },
        "content_blocks": [
            {
                "type": "tool_calls",
                "results": [
                    {
                        "tool_call_id": "tc1",
                        "content": "Subagent 3 (foo) ERROR after retry: boom",
                    }
                ],
            }
        ],
    }
    changed, out = _run_sweep(ex)
    # Already terminal+stamped and not promotable -> no change at all.
    assert changed is False and out is None


def test_sweep_running_with_result_still_done_not_error():
    # A still-RUNNING run whose real answer landed in content_blocks (SHAPE C)
    # is promoted to done (a running run never has an ERROR result yet).
    ex = {
        "subagent_runs": {
            "a": {"status": "running", "tool_call_id": "tc1", "started_at": 1}
        },
        "content_blocks": [
            {
                "type": "tool_calls",
                "results": [{"tool_call_id": "tc1", "content": "the real answer"}],
            }
        ],
    }
    _, out = _run_sweep(ex)
    r = out["subagent_runs"]["a"]
    assert r["status"] == "done" and r["final_text"] == "the real answer"


def test_sweep_leaves_genuine_cancelled_untouched():
    ex = {
        "subagent_runs": {"a": {"status": "cancelled", "started_at": 1, "ended_at": 9}},
        "content_blocks": [],
    }
    changed, out = _run_sweep(ex)
    assert changed is False and out is None


def test_sweep_leaves_done_untouched():
    ex = {
        "subagent_runs": {
            "a": {"status": "done", "final_text": "x", "started_at": 1, "ended_at": 9}
        },
        "content_blocks": [],
    }
    changed, _ = _run_sweep(ex)
    assert changed is False


def test_sweep_error_fallback_for_error_path():
    ex = {
        "subagent_runs": {"a": {"status": "running", "started_at": 1}},
        "content_blocks": [],
    }
    _, out = _run_sweep(ex, fallback="error")
    assert out["subagent_runs"]["a"]["status"] == "error"


def test_sweep_backfills_missing_started_at():
    ex = {"subagent_runs": {"a": {"status": "running"}}, "content_blocks": []}
    _, out = _run_sweep(ex)
    r = out["subagent_runs"]["a"]
    assert r["started_at"] == r["ended_at"]


def test_sweep_mixed_batch():
    ex = {
        "subagent_runs": {
            "a": {"status": "done", "final_text": "x", "started_at": 1, "ended_at": 2},
            "b": {"status": "running", "started_at": 3},
            "c": {"status": "cancelled", "started_at": 4, "ended_at": 5},
        },
        "content_blocks": [],
    }
    _, out = _run_sweep(ex)
    runs = out["subagent_runs"]
    assert runs["a"]["status"] == "done"
    assert runs["b"]["status"] == "cancelled"
    assert runs["c"]["status"] == "cancelled"


def test_sweep_noop_when_no_runs():
    changed, out = _run_sweep({"subagent_runs": {}, "content_blocks": []})
    assert changed is False and out is None


# ---------------------------------------------------------------------------
# B4 — _json_from_db / _lenient_json_loads
# ---------------------------------------------------------------------------


def test_json_from_db_passthrough_and_none():
    assert _json_from_db(None) is None
    assert _json_from_db({"a": 1}) == {"a": 1}
    assert _json_from_db([1, 2]) == [1, 2]


def test_json_from_db_single_encoded_object():
    assert _json_from_db('{"a": 1}') == {"a": 1}


def test_json_from_db_double_encoded_resolves_to_object():
    # asyncpg returns a jsonb-STRING scalar as the JSON text of a quoted string;
    # one decode yields the inner JSON text, a second yields the object.
    double = json.dumps(json.dumps({"subagent_runs": {"x": {"status": "done"}}}))
    out = _json_from_db(double)
    assert isinstance(out, dict)
    assert out["subagent_runs"]["x"]["status"] == "done"


def test_json_from_db_does_not_overdecode_inner_string_values():
    # A dict whose VALUE looks like JSON must stay a string, not be re-decoded.
    assert _json_from_db('{"content": "{\\"x\\":1}"}') == {"content": '{"x":1}'}


def test_lenient_json_loads_recovers_invalid_backslash_escape():
    # Legacy migration embedded raw bytes like \@ that strict json rejects.
    bad = '{"p": "Carhartt\\@WIP"}'
    out = _lenient_json_loads(bad)
    assert out["p"] == "Carhartt\\@WIP"


def test_lenient_json_loads_keeps_valid_escapes():
    assert _lenient_json_loads('{"p": "C:\\\\Users\\\\x"}') == {"p": "C:\\Users\\x"}


def test_json_from_db_unrecoverable_string_returns_none():
    # A bare non-JSON string that never resolves to an object/list -> None
    # (callers ignore non-dict/list meta).
    assert _json_from_db('"just a string"') is None


# ---------------------------------------------------------------------------
# B3 — list_item_task_ids_by_prefix (in-memory fallback)
# ---------------------------------------------------------------------------


def test_list_item_task_ids_by_prefix_in_memory():
    saved = dict(tasks_mod.item_tasks)
    try:
        tasks_mod.item_tasks.clear()
        tasks_mod.item_tasks.update(
            {
                "chatA": ["t1"],
                "subagent-rerun:chatA:e1": ["t2"],
                "subagent-rerun:chatA:e2": ["t3", "t4"],
                "subagent-rerun:chatB:e1": ["t5"],
            }
        )
        out = asyncio.run(
            tasks_mod.list_item_task_ids_by_prefix(None, "subagent-rerun:chatA:")
        )
        assert set(out) == {"t2", "t3", "t4"}
    finally:
        tasks_mod.item_tasks.clear()
        tasks_mod.item_tasks.update(saved)


def test_collect_chat_work_state_uses_operations_for_parent_generations():
    saved = dict(tasks_mod.item_tasks)
    saved_operations = dict(tasks_mod.generation_operations)
    saved_item_operations = {
        item_id: set(generation_ids)
        for item_id, generation_ids in tasks_mod.item_generation_operations.items()
    }
    try:
        tasks_mod.item_tasks.clear()
        tasks_mod.item_tasks.update(
            {
                # A bare task under the chat is not generation authority.
                "chatA": ["unowned-task"],
                "subagent-rerun:chatA:e1": ["rerun-task-1"],
                "subagent-rerun:chatA:e2": ["rerun-task-2"],
                "subagent-rerun:chatB:e1": ["other-chat-task"],
            }
        )
        tasks_mod.generation_operations.clear()
        tasks_mod.generation_operations["generation-1"] = {
            "generation_id": "generation-1",
            "chat_id": "chatA",
            "message_id": "assistant-1",
            "turn_id": "turn-1",
            "task_id": "parent-task",
        }
        tasks_mod.item_generation_operations.clear()
        tasks_mod.item_generation_operations["chatA"] = {"generation-1"}

        state = asyncio.run(tasks_mod.collect_chat_work_state(None, "chatA"))

        assert state["generations"] == [
            {
                "generation_id": "generation-1",
                "chat_id": "chatA",
                "message_id": "assistant-1",
                "turn_id": "turn-1",
                "task_id": "parent-task",
            }
        ]
        assert state["rerun_task_ids"] == ["rerun-task-1", "rerun-task-2"]
        assert state["subagent_rerun_entry_keys"] == ["e1", "e2"]
    finally:
        tasks_mod.item_tasks.clear()
        tasks_mod.item_tasks.update(saved)
        tasks_mod.generation_operations.clear()
        tasks_mod.generation_operations.update(saved_operations)
        tasks_mod.item_generation_operations.clear()
        tasks_mod.item_generation_operations.update(saved_item_operations)


# ---------------------------------------------------------------------------
# D1 — update_message_subagent_run_atomic: targeted single-run jsonb_set write
# (only the changed subagent_runs[sid] key is serialized, never the whole map)
# ---------------------------------------------------------------------------

from contextlib import contextmanager  # noqa: E402

import open_webui.models.chats as chats_mod  # noqa: E402
from open_webui.models.chats import ChatTable  # noqa: E402


class _FakeDB:
    """Captures every (sql, params) passed to execute so a test can assert on
    the exact serialized payload of a targeted jsonb_set write."""

    def __init__(self):
        self.calls = []
        self.committed = False
        self.rowcount = 1  # simulate the targeted UPDATE matching the message row

    def execute(self, sql, params=None):
        self.calls.append((str(sql), dict(params or {})))
        _rc = self.rowcount

        class _R:
            rowcount = _rc

            def fetchone(self_inner):
                return None

        return _R()

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


def _run_targeted_upsert(existing_msg, partial, *, migrated=True):
    """Drive ChatTable.update_message_subagent_run_atomic with the message read
    and migration probe stubbed, returning (fake_db, result)."""
    impl = ChatTable()
    fake_db = _FakeDB()

    @contextmanager
    def fake_get_db():
        yield fake_db

    with (
        patch.object(
            impl,
            "_lock_chat_message_for_update",
            return_value=(object(), migrated, existing_msg, migrated),
        ),
        patch.object(chats_mod, "get_db", fake_get_db),
        patch.object(
            impl, "upsert_message_to_chat_by_id_and_message_id", return_value=None
        ) as legacy_upsert,
    ):
        result = impl.update_message_subagent_run_atomic(
            "c1", "m1", "sid_target", lambda _existing: partial
        )
    return fake_db, result, legacy_upsert


def test_targeted_upsert_serializes_only_the_changed_run():
    # The mutator returns the WHOLE N-entry map (as the real subagent mutator
    # does), but the targeted write must serialize ONLY subagent_runs[sid].
    big_map = {f"sid_{i}": {"status": "running", "started_at": i} for i in range(50)}
    big_map["sid_target"] = {"status": "done", "ended_at": 999, "final_text": "x"}
    partial = {"subagent_runs": big_map}

    fake_db, result, legacy_upsert = _run_targeted_upsert({}, partial)

    assert result == partial
    legacy_upsert.assert_not_called()  # targeted path, not whole-message merge

    # Exactly one write, and its :run bind is the single target run — not the map.
    run_writes = [
        (sql, p) for (sql, p) in fake_db.calls if "ARRAY['subagent_runs', :sid]" in sql
    ]
    assert len(run_writes) == 1
    sql, params = run_writes[0]
    assert params["sid"] == "sid_target"
    decoded = json.loads(params["run"])
    assert decoded == {"status": "done", "ended_at": 999, "final_text": "x"}
    # The serialized payload must NOT contain the other 50 siblings.
    assert "sid_0" not in params["run"]
    assert "started_at" not in params["run"]
    assert fake_db.committed


def test_targeted_upsert_writes_content_blocks_and_content_separately():
    partial = {
        "subagent_runs": {"sid_target": {"status": "running"}},
        "content_blocks": [{"type": "tool_calls", "content": [], "results": []}],
        "content": "hello",
    }
    fake_db, result, _ = _run_targeted_upsert({}, partial)
    assert result == partial

    cb_writes = [p for (sql, p) in fake_db.calls if "'{content_blocks}'" in sql]
    assert len(cb_writes) == 1
    assert json.loads(cb_writes[0]["cb"]) == partial["content_blocks"]

    content_writes = [p for (sql, p) in fake_db.calls if "content = :c" in sql]
    assert len(content_writes) == 1
    assert content_writes[0]["c"] == "hello"
    assert content_writes[0]["ij"] == 0


def test_targeted_upsert_falls_back_to_whole_message_when_not_migrated():
    partial = {"subagent_runs": {"sid_target": {"status": "running"}}}
    fake_db, result, legacy_upsert = _run_targeted_upsert({}, partial, migrated=False)
    assert result == partial
    # Legacy chats have no chat_message row — must use the whole-message merge.
    legacy_upsert.assert_called_once()
    # No targeted jsonb_set ran.
    assert not [c for c in fake_db.calls if "ARRAY['subagent_runs', :sid]" in c[0]]


def test_targeted_upsert_skips_when_mutator_returns_nothing():
    fake_db, result, legacy_upsert = _run_targeted_upsert({}, None)
    assert result is None
    legacy_upsert.assert_not_called()
    assert fake_db.calls == []


def test_targeted_upsert_falls_back_to_full_upsert_when_row_missing():
    # If the chat_message row doesn't exist yet (a subagent launched before the
    # first checkpoint persisted the assistant row), the targeted UPDATE matches
    # 0 rows — the method MUST fall back to the full upsert (which INSERTs) so the
    # run is never lost. (Without this, jsonb_set on a missing row silently no-ops.)
    impl = ChatTable()
    fake_db = _FakeDB()
    fake_db.rowcount = 0  # targeted UPDATE matched no row

    @contextmanager
    def fake_get_db():
        yield fake_db

    partial = {"subagent_runs": {"sid_target": {"status": "running"}}}
    with (
        patch.object(
            impl,
            "_lock_chat_message_for_update",
            return_value=(object(), True, {}, False),
        ),
        patch.object(chats_mod, "get_db", fake_get_db),
        patch.object(
            impl, "upsert_message_to_chat_by_id_and_message_id", return_value=None
        ) as full_upsert,
    ):
        result = impl.update_message_subagent_run_atomic(
            "c1", "m1", "sid_target", lambda _e: partial
        )
    assert result == partial
    full_upsert.assert_called_once()


# ---------------------------------------------------------------------------
# broadcast_subagent_terminals — the finalize-time AUTHORITATIVE terminal
# re-delivery (root fix). After the sweep makes subagent_runs authoritative, the
# parent finalizer fans every run's terminal out to ALL the user's tabs via
# emit_user_fanout, so a card that missed the stream-scoped per-update terminal
# still resolves without a reload.
# ---------------------------------------------------------------------------


def _run_broadcast(subagent_runs, user_id="user1"):
    sent = []

    async def _fake_get_msg(cid, mid):
        return {"subagent_runs": subagent_runs}

    async def _fake_fanout(uid, envelope):
        sent.append((uid, envelope))

    with (
        patch.object(sub.Chats, "get_message_by_id_and_message_id", _fake_get_msg),
        patch.object(sub, "emit_user_fanout", _fake_fanout),
        patch.object(sub, "STREAM_PROTOCOL_VERSION", "v2.1"),
    ):
        asyncio.run(sub.broadcast_subagent_terminals("chat1", "msg1", user_id))
    return sent


def test_broadcast_done_run_carries_final_text():
    runs = {
        "sa1": {
            "subagent_id": "sa1",
            "entry_key": "sa1",
            "status": "done",
            "final_text": "the answer",
            "num": 1,
            "name": "researcher",
            "tool_call_id": "call_x",
        }
    }
    sent = _run_broadcast(runs)
    assert len(sent) == 1
    uid, env = sent[0]
    assert uid == "user1"
    assert env["chat_id"] == "chat1" and env["message_id"] == "msg1"
    data = env["data"]["data"]
    assert env["data"]["type"] == "chat:subagent:update"
    assert data["subagent_id"] == "sa1"
    assert data["parent_message_id"] == "msg1"
    inner = data["inner_event"]
    assert inner["type"] == "chat:done"
    assert inner["data"]["final_text"] == "the answer"


def test_broadcast_maps_error_cancelled_and_skips_running():
    runs = {
        "sa_err": {
            "subagent_id": "sa_err",
            "status": "error",
            "error": {"message": "boom"},
        },
        "sa_cancel": {"subagent_id": "sa_cancel", "status": "cancelled"},
        "sa_run": {"subagent_id": "sa_run", "status": "running"},  # must be skipped
    }
    sent = _run_broadcast(runs)
    by_sid = {
        e["data"]["data"]["subagent_id"]: e["data"]["data"]["inner_event"]
        for _u, e in sent
    }
    assert set(by_sid) == {"sa_err", "sa_cancel"}  # the 'running' run is skipped
    assert by_sid["sa_err"]["type"] == "chat:message:error"
    assert by_sid["sa_err"]["data"]["error"] == "boom"
    assert by_sid["sa_cancel"]["type"] == "chat:tasks:cancel"


def test_broadcast_noop_without_user_id():
    runs = {"sa1": {"subagent_id": "sa1", "status": "done", "final_text": "x"}}
    assert _run_broadcast(runs, user_id=None) == []


def test_broadcast_done_without_final_text_emits_bare_done():
    runs = {"sa1": {"subagent_id": "sa1", "status": "done"}}
    sent = _run_broadcast(runs)
    assert len(sent) == 1
    inner = sent[0][1]["data"]["data"]["inner_event"]
    assert inner["type"] == "chat:done"
    assert "final_text" not in inner["data"]
