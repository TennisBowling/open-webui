import asyncio

import pytest

import open_webui.tasks as task_registry


@pytest.fixture(autouse=True)
def _clear_generation_state():
    task_registry.generation_operations.clear()
    task_registry.item_generation_operations.clear()
    task_registry.generation_cancel_intents.clear()
    task_registry.generation_turn_cancel_intents.clear()
    task_registry.chat_work_blocks.clear()
    task_registry.generation_supersede_transitions.clear()
    task_registry.tasks.clear()
    task_registry.item_tasks.clear()
    yield
    task_registry.generation_operations.clear()
    task_registry.item_generation_operations.clear()
    task_registry.generation_cancel_intents.clear()
    task_registry.generation_turn_cancel_intents.clear()
    task_registry.chat_work_blocks.clear()
    task_registry.generation_supersede_transitions.clear()
    task_registry.tasks.clear()
    task_registry.item_tasks.clear()


def _operation(generation_id: str, message_id: str, turn_id: str = "user-1"):
    return {
        "generation_id": generation_id,
        "chat_id": "chat-1",
        "message_id": message_id,
        "turn_id": turn_id,
        "task_id": "",
    }


def test_generation_registration_requires_complete_identity():
    async def run():
        operation = _operation("generation-1", "")
        with pytest.raises(ValueError, match="message_id"):
            await task_registry.register_generation_operation(None, operation)

    asyncio.run(run())


def test_stop_before_registration_prevents_task_start():
    async def run():
        operation = _operation("generation-1", "assistant-1")
        await task_registry.mark_generation_cancelled(
            None, operation["chat_id"], operation["generation_id"]
        )
        assert (
            await task_registry.register_generation_operation(None, operation)
            == "cancelled"
        )

    asyncio.run(run())


def test_same_turn_allows_model_siblings_but_rejects_another_turn():
    async def run():
        assert (
            await task_registry.register_generation_operation(
                None, _operation("generation-1", "assistant-1")
            )
            == "acquired"
        )
        assert (
            await task_registry.register_generation_operation(
                None, _operation("generation-2", "assistant-2")
            )
            == "acquired"
        )
        assert (
            await task_registry.register_generation_operation(
                None,
                _operation("generation-3", "assistant-3", turn_id="user-2"),
            )
            == "turn_conflict"
        )

    asyncio.run(run())


def test_supersede_atomically_transfers_turn_and_latches_every_old_sibling():
    async def run():
        first = _operation("generation-1", "assistant-1", "turn-old")
        second = _operation("generation-2", "assistant-2", "turn-old")
        replacement = _operation("generation-3", "assistant-3", "turn-new")
        assert (
            await task_registry.register_generation_operation(None, first) == "acquired"
        )
        assert (
            await task_registry.register_generation_operation(None, second)
            == "acquired"
        )

        result = await task_registry.supersede_generation_operation(None, replacement)

        assert result["registration"] == "acquired"
        assert {item["generation_id"] for item in result["displaced"]} == {
            "generation-1",
            "generation-2",
        }
        assert await task_registry.is_generation_cancelled(
            None, "chat-1", "generation-1"
        )
        assert await task_registry.is_generation_cancelled(
            None, "chat-1", "generation-2"
        )
        assert await task_registry.is_generation_turn_cancelled(
            None, "chat-1", "turn-old"
        )
        assert [
            item["generation_id"]
            for item in await task_registry.list_generation_operations_by_item(
                None, "chat-1"
            )
        ] == ["generation-3"]
        assert (
            await task_registry.register_generation_operation(
                None, _operation("generation-4", "assistant-4", "turn-other")
            )
            == "turn_conflict"
        )

    asyncio.run(run())


def test_supersede_keeps_parallel_siblings_of_the_replacement_turn():
    async def run():
        first = _operation("generation-1", "assistant-1", "turn-new")
        sibling = _operation("generation-2", "assistant-2", "turn-new")
        assert (await task_registry.supersede_generation_operation(None, first)) == {
            "registration": "acquired",
            "displaced": [],
        }
        assert (await task_registry.supersede_generation_operation(None, sibling)) == {
            "registration": "acquired",
            "displaced": [],
        }
        assert {
            item["generation_id"]
            for item in await task_registry.list_generation_operations_by_item(
                None, "chat-1"
            )
        } == {"generation-1", "generation-2"}

    asyncio.run(run())


def test_displaced_task_is_cancelled_before_replacement_can_run():
    async def run():
        old = _operation("generation-old", "assistant-old", "turn-old")
        replacement = _operation("generation-new", "assistant-new", "turn-new")
        started = asyncio.Event()
        unwound = asyncio.Event()

        async def old_provider_work():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                unwound.set()

        assert (
            await task_registry.register_generation_operation(None, old) == "acquired"
        )
        _task_id, _task = await task_registry.create_task(
            None,
            old_provider_work(),
            id="chat-1",
            generation_operation=old,
        )
        await started.wait()

        result = await task_registry.supersede_generation_operation(None, replacement)
        displaced_task_ids = [
            operation["task_id"]
            for operation in result["displaced"]
            if operation["task_id"]
        ]
        assert displaced_task_ids == [_task_id]
        assert (
            await task_registry.stop_tasks_and_wait(
                None, displaced_task_ids, timeout=1.0
            )
            == []
        )
        assert unwound.is_set()

        await task_registry.unregister_generation_operation(None, replacement)
        await asyncio.sleep(0)

    asyncio.run(run())


def test_parallel_replacement_sibling_waits_on_the_same_teardown_barrier():
    async def run():
        old = _operation("generation-old", "assistant-old", "turn-old")
        first = _operation("generation-new-1", "assistant-new-1", "turn-new")
        second = _operation("generation-new-2", "assistant-new-2", "turn-new")
        started = asyncio.Event()
        cancelling = asyncio.Event()
        allow_unwind = asyncio.Event()

        async def old_provider_work():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await allow_unwind.wait()
                raise

        assert (
            await task_registry.register_generation_operation(None, old) == "acquired"
        )
        old_task_id, _task = await task_registry.create_task(
            None,
            old_provider_work(),
            id="chat-1",
            generation_operation=old,
        )
        await started.wait()

        first_result = await task_registry.supersede_generation_operation(None, first)
        first_wait = asyncio.create_task(
            task_registry.stop_tasks_and_wait(
                None,
                [item["task_id"] for item in first_result["displaced"]],
                timeout=1.0,
            )
        )
        await cancelling.wait()

        second_result = await task_registry.supersede_generation_operation(None, second)
        assert [item["task_id"] for item in second_result["displaced"]] == [old_task_id]
        second_wait = asyncio.create_task(
            task_registry.stop_tasks_and_wait(
                None,
                [item["task_id"] for item in second_result["displaced"]],
                timeout=1.0,
            )
        )
        await asyncio.sleep(0)
        assert not second_wait.done()

        allow_unwind.set()
        assert await first_wait == []
        assert await second_wait == []
        await task_registry.finish_generation_supersede(None, "chat-1", "turn-new")
        await task_registry.unregister_generation_operation(None, first)
        await task_registry.unregister_generation_operation(None, second)
        await asyncio.sleep(0)

    asyncio.run(run())


def test_generation_operation_is_the_only_duplicate_request_claim():
    async def run():
        operation = _operation("generation-1", "assistant-1")
        assert (
            await task_registry.register_generation_operation(None, operation)
            == "acquired"
        )
        assert (
            await task_registry.register_generation_operation(None, dict(operation))
            == "duplicate"
        )

        release = asyncio.Event()

        async def generation():
            await release.wait()

        task_id, task = await task_registry.create_task(
            None,
            generation(),
            id=operation["chat_id"],
            generation_operation=operation,
        )
        duplicate = await task_registry.get_generation_operation(
            None, operation["generation_id"]
        )
        assert duplicate is not None
        assert duplicate["task_id"] == task_id

        release.set()
        await task
        await asyncio.sleep(0)

    asyncio.run(run())


def test_turn_stop_rejects_every_late_sibling_but_not_a_new_turn():
    async def run():
        await task_registry.mark_generation_turn_cancelled(None, "chat-1", "user-1")
        assert (
            await task_registry.register_generation_operation(
                None, _operation("generation-1", "assistant-1")
            )
            == "cancelled"
        )
        assert (
            await task_registry.register_generation_operation(
                None, _operation("generation-2", "assistant-2")
            )
            == "cancelled"
        )
        assert (
            await task_registry.register_generation_operation(
                None,
                _operation("generation-3", "assistant-3", turn_id="user-2"),
            )
            == "acquired"
        )

    asyncio.run(run())


def test_cancellation_latch_closes_and_marks_the_whole_turn():
    async def run():
        first = _operation("generation-1", "assistant-1")
        sibling = _operation("generation-2", "assistant-2")
        assert (
            await task_registry.register_generation_operation(None, first) == "acquired"
        )
        assert (
            await task_registry.register_generation_operation(None, sibling)
            == "acquired"
        )

        matched = await task_registry.latch_generation_cancellation(
            None,
            "chat-1",
            generation_ids=["generation-1"],
            turn_ids=["user-1"],
        )

        assert {operation["generation_id"] for operation in matched} == {
            "generation-1",
            "generation-2",
        }
        assert not await task_registry.refresh_generation_operation(None, sibling)

    asyncio.run(run())


def test_deleting_chat_blocks_every_future_turn():
    async def run():
        await task_registry.acquire_chat_work_block(None, "chat-1")
        assert (
            await task_registry.register_generation_operation(
                None, _operation("generation-1", "assistant-1")
            )
            == "cancelled"
        )
        assert (
            await task_registry.register_generation_operation(
                None,
                _operation("generation-2", "assistant-2", turn_id="user-2"),
            )
            == "cancelled"
        )

    asyncio.run(run())


def test_concurrent_delete_barriers_release_only_their_owner():
    async def run():
        first = await task_registry.acquire_chat_work_block(None, "chat-1")
        second = await task_registry.acquire_chat_work_block(None, "chat-1")

        await task_registry.release_chat_work_block(None, "chat-1", first)
        assert await task_registry.is_chat_work_blocked(None, "chat-1")
        assert (
            await task_registry.register_generation_operation(
                None, _operation("generation-1", "assistant-1")
            )
            == "cancelled"
        )

        await task_registry.release_chat_work_block(None, "chat-1", second)
        assert not await task_registry.is_chat_work_blocked(None, "chat-1")
        assert (
            await task_registry.register_generation_operation(
                None, _operation("generation-2", "assistant-2")
            )
            == "acquired"
        )

    asyncio.run(run())


def test_chat_work_block_rejects_detached_task_admission():
    async def run():
        await task_registry.acquire_chat_work_block(None, "chat-1")
        started = False

        async def rerun():
            nonlocal started
            started = True

        with pytest.raises(task_registry.ChatWorkBlockedError):
            await task_registry.create_task(
                None,
                rerun(),
                id="subagent-rerun:chat-1:entry-1",
                admission_chat_id="chat-1",
            )

        assert not started
        assert task_registry.tasks == {}
        assert task_registry.item_tasks == {}

    asyncio.run(run())


def test_operation_refresh_observes_durable_stop_intent():
    async def run():
        operation = _operation("generation-1", "assistant-1")
        assert (
            await task_registry.register_generation_operation(None, operation)
            == "acquired"
        )
        await task_registry.mark_generation_cancelled(
            None, operation["chat_id"], operation["generation_id"]
        )
        assert not await task_registry.refresh_generation_operation(None, operation)

    asyncio.run(run())


def test_preflight_heartbeat_refreshes_until_task_is_bound(monkeypatch):
    async def run():
        operation = _operation("generation-1", "assistant-1")
        refreshed = asyncio.Event()
        refresh_calls = 0

        async def refresh_generation_operation(_redis, current):
            nonlocal refresh_calls
            assert current is operation
            refresh_calls += 1
            refreshed.set()
            return True

        monkeypatch.setattr(task_registry, "GENERATION_OPERATION_HEARTBEAT_SECONDS", 0)
        monkeypatch.setattr(
            task_registry,
            "refresh_generation_operation",
            refresh_generation_operation,
        )

        heartbeat = asyncio.create_task(
            task_registry.heartbeat_generation_operation_until_bound(
                object(), operation
            )
        )
        await asyncio.wait_for(refreshed.wait(), timeout=1)
        operation["task_id"] = "task-1"
        await asyncio.wait_for(heartbeat, timeout=1)

        assert refresh_calls == 1

    asyncio.run(run())


def test_stale_task_cleanup_cannot_delete_rebound_operation():
    async def run():
        operation = _operation("generation-1", "assistant-1")
        assert (
            await task_registry.register_generation_operation(None, operation)
            == "acquired"
        )
        task_registry.generation_operations[operation["generation_id"]] = {
            **operation,
            "task_id": "task-new",
        }

        await task_registry.unregister_generation_operation(
            None, {**operation, "task_id": "task-old"}
        )
        assert (
            await task_registry.get_generation_operation(
                None, operation["generation_id"]
            )
        )["task_id"] == "task-new"

        await task_registry.unregister_generation_operation(
            None, {**operation, "task_id": "task-new"}
        )
        assert (
            await task_registry.get_generation_operation(
                None, operation["generation_id"]
            )
            is None
        )

    asyncio.run(run())


def test_cancellation_during_registration_never_opens_start_gate():
    async def run():
        operation = _operation("generation-1", "assistant-1")
        assert (
            await task_registry.register_generation_operation(None, operation)
            == "acquired"
        )
        await task_registry.mark_generation_cancelled(
            None, operation["chat_id"], operation["generation_id"]
        )

        started = False

        async def generation():
            nonlocal started
            started = True

        with pytest.raises(task_registry.GenerationCancelledError):
            await task_registry.create_task(
                None,
                generation(),
                id=operation["chat_id"],
                generation_operation=operation,
            )

        assert started is False
        assert (
            await task_registry.list_generation_operations_by_item(
                None, operation["chat_id"]
            )
            == []
        )

    asyncio.run(run())


def test_generation_operation_lives_until_registered_task_finishes():
    async def run():
        operation = _operation("generation-1", "assistant-1")
        assert (
            await task_registry.register_generation_operation(None, operation)
            == "acquired"
        )

        release = asyncio.Event()

        async def generation():
            await release.wait()

        task_id, task = await task_registry.create_task(
            None,
            generation(),
            id=operation["chat_id"],
            generation_operation=operation,
        )
        active = await task_registry.get_generation_operation(
            None, operation["generation_id"]
        )
        assert active is not None
        assert active["task_id"] == task_id

        release.set()
        await task
        await asyncio.sleep(0)
        assert (
            await task_registry.get_generation_operation(
                None, operation["generation_id"]
            )
            is None
        )

    asyncio.run(run())


class TestChatTurnLeaseIsDerived:
    """The chat's active turn must never be storable state that can strand.

    It used to be a hand-maintained dict popped as a side effect of
    `unregister_generation_operation`, behind an early-returning ownership
    guard. Any unregister that took that early return left a CHAT-GLOBAL lease
    held with no error and no log — and since in-memory operations have no TTL
    (unlike the Redis path), the strand was permanent: every later send to that
    chat returned `turn_conflict` until the process restarted.
    """

    def test_stale_unregister_cannot_strand_the_chat(self):
        async def scenario():
            first = _operation("gen-1", "msg-1", turn_id="turn-1")
            assert (
                await task_registry.register_generation_operation(None, first)
                == "acquired"
            )
            bound = dict(first)
            assert await task_registry.bind_generation_operation_task(
                None, bound, "task-1"
            )
            bound["task_id"] = "task-1"

            # A caller unregisters with the PRE-bind identity: the ownership
            # guard rejects it, so the operation stays registered.
            await task_registry.unregister_generation_operation(None, first)
            assert "gen-1" in task_registry.generation_operations

            # The next user turn must still be admitted.
            second = _operation("gen-2", "msg-2", turn_id="turn-2")
            assert (
                await task_registry.register_generation_operation(None, second)
                == "acquired"
            )

        asyncio.run(scenario())

    def test_operation_whose_task_died_stops_holding_the_turn(self):
        async def scenario():
            first = _operation("gen-1", "msg-1", turn_id="turn-1")
            await task_registry.register_generation_operation(None, first)

            async def _work():
                return None

            task = asyncio.create_task(_work())
            await task
            task_registry.tasks["task-1"] = task
            task_registry.generation_operations["gen-1"]["task_id"] = "task-1"

            # Nothing unregistered it, but its owner is gone — the local
            # equivalent of a Redis lease lapsing.
            second = _operation("gen-2", "msg-2", turn_id="turn-2")
            assert (
                await task_registry.register_generation_operation(None, second)
                == "acquired"
            )
            assert "gen-1" not in task_registry.generation_operations

        asyncio.run(scenario())

    def test_a_live_sibling_turn_still_blocks_a_different_turn(self):
        """The fix must not weaken the guarantee it replaces."""

        async def scenario():
            first = _operation("gen-1", "msg-1", turn_id="turn-1")
            await task_registry.register_generation_operation(None, first)

            async def _work():
                await asyncio.sleep(30)

            task = asyncio.create_task(_work())
            task_registry.tasks["task-1"] = task
            task_registry.generation_operations["gen-1"]["task_id"] = "task-1"
            try:
                # Same turn: a sibling model response is allowed.
                sibling = _operation("gen-1b", "msg-1b", turn_id="turn-1")
                assert (
                    await task_registry.register_generation_operation(None, sibling)
                    == "acquired"
                )
                # Different turn: still rejected.
                other = _operation("gen-2", "msg-2", turn_id="turn-2")
                assert (
                    await task_registry.register_generation_operation(None, other)
                    == "turn_conflict"
                )
            finally:
                task.cancel()

        asyncio.run(scenario())


class TestBindHonoursCancellation:
    """`bind_generation_operation_task` must refuse a cancelled generation.

    The Redis Lua checks the generation, turn, and chat-work-block keys before
    binding. The in-memory branch omitted all three, so with Redis unconfigured
    a run the user had already stopped bound successfully.
    """

    def test_bind_refuses_a_cancelled_generation(self):
        async def scenario():
            operation = _operation("gen-1", "msg-1", turn_id="turn-1")
            await task_registry.register_generation_operation(None, operation)
            await task_registry.mark_generation_cancelled(None, "chat-1", "gen-1")
            assert not await task_registry.bind_generation_operation_task(
                None, dict(operation), "task-1"
            )

        asyncio.run(scenario())

    def test_bind_refuses_a_cancelled_turn(self):
        async def scenario():
            operation = _operation("gen-1", "msg-1", turn_id="turn-1")
            await task_registry.register_generation_operation(None, operation)
            await task_registry.mark_generation_turn_cancelled(None, "chat-1", "turn-1")
            assert not await task_registry.bind_generation_operation_task(
                None, dict(operation), "task-1"
            )

        asyncio.run(scenario())

    def test_bind_still_succeeds_for_a_live_generation(self):
        async def scenario():
            operation = _operation("gen-1", "msg-1", turn_id="turn-1")
            await task_registry.register_generation_operation(None, operation)
            assert await task_registry.bind_generation_operation_task(
                None, dict(operation), "task-1"
            )

        asyncio.run(scenario())
