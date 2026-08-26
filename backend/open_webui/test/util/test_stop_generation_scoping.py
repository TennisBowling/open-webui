import asyncio
from types import SimpleNamespace

from test.util.db import configure_test_database

configure_test_database()

import open_webui.main as main  # noqa: E402


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))


def _user():
    return SimpleNamespace(id="user-1", role="user")


def _operation(generation_id, message_id, turn_id, task_id):
    return {
        "generation_id": generation_id,
        "chat_id": "chat-1",
        "message_id": message_id,
        "turn_id": turn_id,
        "task_id": task_id,
    }


def _install_stop_fakes(
    monkeypatch,
    *,
    initial_operations,
    post_operations,
    messages=None,
    rerun_task_ids=(),
):
    calls = {
        "generation_latches": [],
        "turn_latches": [],
        "stopped_task_ids": [],
        "upserts": [],
        "rerun_prefixes": [],
    }

    async def get_chat(_chat_id):
        return SimpleNamespace(user_id="user-1")

    async def list_operations(_redis, _chat_id):
        return [dict(operation) for operation in initial_operations]

    async def get_operation(_redis, generation_id):
        for operation in [*initial_operations, *post_operations]:
            if operation["generation_id"] == generation_id:
                return dict(operation)
        return None

    async def latch_cancellation(
        _redis,
        chat_id,
        *,
        generation_ids=(),
        turn_ids=(),
    ):
        wanted_generations = set(generation_ids)
        wanted_turns = set(turn_ids)
        calls["generation_latches"].extend(
            (chat_id, generation_id) for generation_id in wanted_generations
        )
        calls["turn_latches"].extend((chat_id, turn_id) for turn_id in wanted_turns)
        matched = []
        for operation in post_operations:
            if (
                operation["generation_id"] not in wanted_generations
                and operation["turn_id"] not in wanted_turns
            ):
                continue
            matched.append(dict(operation))
            if operation["generation_id"] not in wanted_generations:
                wanted_generations.add(operation["generation_id"])
                calls["generation_latches"].append(
                    (chat_id, operation["generation_id"])
                )
        return matched

    async def stop_and_wait(_redis, ids, **_kwargs):
        calls["stopped_task_ids"].extend(ids)
        return []

    async def list_rerun_task_ids(_redis, prefix):
        calls["rerun_prefixes"].append(prefix)
        return list(rerun_task_ids or [])

    async def mark_stopped(
        chat_id,
        message_id,
        generation_id,
        turn_id,
        *,
        require_unfinished: bool = False,
    ):
        message = (messages or {}).get(message_id) or {}
        if (
            message.get("role") != "assistant"
            or message.get("generation_id") != generation_id
            or message.get("turn_id") != turn_id
        ):
            return False
        if require_unfinished and message.get("done") is True:
            return False
        calls["upserts"].append(
            (chat_id, message_id, {"done": True, "userStopped": True})
        )
        return True

    monkeypatch.setattr(main.Chats, "get_chat_by_id", get_chat)
    monkeypatch.setattr(main, "get_generation_operation", get_operation)
    monkeypatch.setattr(main, "list_generation_operations_by_item", list_operations)
    monkeypatch.setattr(main, "latch_generation_cancellation", latch_cancellation)
    monkeypatch.setattr(main, "stop_tasks_and_wait", stop_and_wait)
    monkeypatch.setattr(main, "list_item_task_ids_by_prefix", list_rerun_task_ids)
    monkeypatch.setattr(main.Chats, "mark_generation_stopped_if_current", mark_stopped)
    return calls


def test_stale_stop_does_not_cancel_or_mutate_newer_reused_message(
    monkeypatch,
):
    async def run():
        newer = _operation("generation-new", "assistant-1", "attempt-new", "task-new")
        calls = _install_stop_fakes(
            monkeypatch,
            initial_operations=[newer],
            post_operations=[newer],
            messages={
                "assistant-1": {
                    "id": "assistant-1",
                    "role": "assistant",
                    "parentId": "user-1",
                    "generation_id": "generation-new",
                    "turn_id": "attempt-new",
                }
            },
        )

        result = await main.stop_chat_generations_endpoint(
            _request(),
            "chat-1",
            main.StopChatGenerationsForm(
                generations=[
                    main.StopGenerationTarget(
                        generation_id="generation-old",
                        message_id="assistant-1",
                        turn_id="attempt-old",
                    )
                ]
            ),
            _user(),
        )

        assert calls["turn_latches"] == [("chat-1", "attempt-old")]
        assert calls["generation_latches"] == [("chat-1", "generation-old")]
        assert calls["stopped_task_ids"] == []
        assert calls["upserts"] == []
        assert result["task_ids"] == []

    asyncio.run(run())


def test_stop_before_registration_latches_without_placeholder(monkeypatch):
    async def run():
        calls = _install_stop_fakes(
            monkeypatch,
            initial_operations=[],
            post_operations=[],
            messages={},
        )

        result = await main.stop_chat_generations_endpoint(
            _request(),
            "chat-1",
            main.StopChatGenerationsForm(
                generations=[
                    main.StopGenerationTarget(
                        generation_id="generation-1",
                        message_id="assistant-1",
                        turn_id="attempt-1",
                    )
                ]
            ),
            _user(),
        )

        assert calls["turn_latches"] == [("chat-1", "attempt-1")]
        assert calls["generation_latches"] == [("chat-1", "generation-1")]
        assert calls["upserts"] == []
        assert result["generation_ids"] == ["generation-1"]
        assert result["turn_ids"] == ["attempt-1"]

    asyncio.run(run())


def test_empty_stop_is_an_idempotent_noop(monkeypatch):
    async def run():
        calls = _install_stop_fakes(
            monkeypatch,
            initial_operations=[],
            post_operations=[],
            messages={},
        )

        result = await main.stop_chat_generations_endpoint(
            _request(),
            "chat-1",
            main.StopChatGenerationsForm(),
            _user(),
        )

        assert calls["turn_latches"] == []
        assert calls["generation_latches"] == []
        assert calls["stopped_task_ids"] == []
        assert calls["upserts"] == []
        assert result["status"] is True

    asyncio.run(run())


def test_turn_stop_collects_late_multi_model_sibling(monkeypatch):
    async def run():
        first_pending = _operation("generation-1", "assistant-1", "attempt-1", "")
        first_bound = _operation("generation-1", "assistant-1", "attempt-1", "task-1")
        late_sibling = _operation("generation-2", "assistant-2", "attempt-1", "task-2")
        calls = _install_stop_fakes(
            monkeypatch,
            initial_operations=[first_pending],
            post_operations=[first_bound, late_sibling],
            messages={
                "assistant-1": {
                    "id": "assistant-1",
                    "role": "assistant",
                    "parentId": "user-1",
                    "generation_id": "generation-1",
                    "turn_id": "attempt-1",
                },
                "assistant-2": {
                    "id": "assistant-2",
                    "role": "assistant",
                    "parentId": "user-1",
                    "generation_id": "generation-2",
                    "turn_id": "attempt-1",
                },
            },
        )

        result = await main.stop_chat_generations_endpoint(
            _request(),
            "chat-1",
            main.StopChatGenerationsForm(
                generations=[
                    main.StopGenerationTarget(
                        generation_id="generation-1",
                        message_id="assistant-1",
                        turn_id="attempt-1",
                    )
                ]
            ),
            _user(),
        )

        assert calls["stopped_task_ids"] == ["task-1", "task-2"]
        assert ("chat-1", "generation-2") in calls["generation_latches"]
        assert {message_id for _, message_id, _ in calls["upserts"]} == {
            "assistant-1",
            "assistant-2",
        }
        assert result["generation_ids"] == [
            "generation-1",
            "generation-2",
        ]

    asyncio.run(run())


def test_stop_racing_a_clean_finish_does_not_relabel_the_answer(monkeypatch):
    """Stop stays live until the last token, so it routinely lands microseconds
    after a turn completes. The completed row must keep its clean terminal
    state: marking it userStopped mislabels a full answer as cancelled and
    pauses the message queue behind it."""

    async def run():
        finished = _operation("generation-1", "assistant-1", "attempt-1", "task-1")
        calls = _install_stop_fakes(
            monkeypatch,
            initial_operations=[finished],
            post_operations=[finished],
            messages={
                "assistant-1": {
                    "id": "assistant-1",
                    "role": "assistant",
                    "parentId": "user-1",
                    "generation_id": "generation-1",
                    "turn_id": "attempt-1",
                    # The clean finalizer already committed this turn.
                    "done": True,
                }
            },
        )

        result = await main.stop_chat_generations_endpoint(
            _request(),
            "chat-1",
            main.StopChatGenerationsForm(
                generations=[
                    main.StopGenerationTarget(
                        generation_id="generation-1",
                        message_id="assistant-1",
                        turn_id="attempt-1",
                    )
                ]
            ),
            _user(),
        )

        # Intent is still latched (a straggler sibling must not start), but the
        # finished row is left exactly as the clean finalizer wrote it.
        assert calls["generation_latches"] == [("chat-1", "generation-1")]
        assert calls["upserts"] == []
        assert result["status"] is True

    asyncio.run(run())


def test_chat_wide_stop_also_cancels_detached_subagent_reruns(monkeypatch):
    """Stop means "halt this chat". Detached redos own task-registry entries
    outside the generation registry, so they have to be collected by prefix and
    cancelled in the same wait — otherwise a redo outlives the Stop that killed
    its chat and keeps writing into it."""

    async def run():
        live = _operation("generation-1", "assistant-1", "attempt-1", "task-1")
        calls = _install_stop_fakes(
            monkeypatch,
            initial_operations=[live],
            post_operations=[live],
            messages={
                "assistant-1": {
                    "id": "assistant-1",
                    "role": "assistant",
                    "parentId": "user-1",
                    "generation_id": "generation-1",
                    "turn_id": "attempt-1",
                }
            },
            rerun_task_ids=["rerun-task-1", "rerun-task-2"],
        )

        result = await main.stop_chat_generations_endpoint(
            _request(),
            "chat-1",
            main.StopChatGenerationsForm(
                generations=[
                    main.StopGenerationTarget(
                        generation_id="generation-1",
                        message_id="assistant-1",
                        turn_id="attempt-1",
                    )
                ],
                include_subagent_reruns=True,
            ),
            _user(),
        )

        assert calls["rerun_prefixes"] == ["subagent-rerun:chat-1:"]
        assert calls["stopped_task_ids"] == ["task-1", "rerun-task-1", "rerun-task-2"]
        assert result["subagent_rerun_task_ids"] == ["rerun-task-1", "rerun-task-2"]

    asyncio.run(run())


def test_stop_with_only_a_running_redo_still_cancels_it(monkeypatch):
    """No generation to latch (the parent turn already finished) but a redo is
    still running: the request must not early-return as a no-op."""

    async def run():
        calls = _install_stop_fakes(
            monkeypatch,
            initial_operations=[],
            post_operations=[],
            messages={},
            rerun_task_ids=["rerun-task-1"],
        )

        result = await main.stop_chat_generations_endpoint(
            _request(),
            "chat-1",
            main.StopChatGenerationsForm(include_subagent_reruns=True),
            _user(),
        )

        assert calls["stopped_task_ids"] == ["rerun-task-1"]
        assert result["subagent_rerun_task_ids"] == ["rerun-task-1"]

    asyncio.run(run())


def test_narrow_stop_leaves_detached_redos_running(monkeypatch):
    """The drain-raced-a-Stop guard cancels one named generation. It must not
    take unrelated background redos down with it."""

    async def run():
        live = _operation("generation-1", "assistant-1", "attempt-1", "task-1")
        calls = _install_stop_fakes(
            monkeypatch,
            initial_operations=[live],
            post_operations=[live],
            messages={
                "assistant-1": {
                    "id": "assistant-1",
                    "role": "assistant",
                    "parentId": "user-1",
                    "generation_id": "generation-1",
                    "turn_id": "attempt-1",
                }
            },
            rerun_task_ids=["rerun-task-1"],
        )

        result = await main.stop_chat_generations_endpoint(
            _request(),
            "chat-1",
            main.StopChatGenerationsForm(
                generations=[
                    main.StopGenerationTarget(
                        generation_id="generation-1",
                        message_id="assistant-1",
                        turn_id="attempt-1",
                    )
                ]
            ),
            _user(),
        )

        assert calls["rerun_prefixes"] == []
        assert calls["stopped_task_ids"] == ["task-1"]
        assert result["subagent_rerun_task_ids"] == []

    asyncio.run(run())
