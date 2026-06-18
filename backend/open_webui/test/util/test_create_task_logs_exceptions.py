"""A detached background task that raises must be LOGGED, not silently swallowed.

Before this fix, ``create_task``'s done-callback only scheduled cleanup and never
read ``t.exception()`` — so an exception in a fire-and-forget task (e.g. a headless
queue-drain generation that fails before emitting anything) vanished, leaving the
queue wedged with no diagnostic. The callback now logs the failure (and still runs
cleanup, removing the task from the registry).

No DB or Redis needed — ``tasks.py`` only touches Redis when a redis handle is
passed, and we pass ``None`` (the single-worker path).
"""

import asyncio
import logging

import open_webui.tasks as tasks_mod
from open_webui.tasks import create_task, tasks, item_tasks


def test_failing_task_is_logged_and_cleaned_up(caplog):
    async def _boom():
        raise RuntimeError("kaboom")

    async def _run():
        with caplog.at_level(logging.ERROR, logger=tasks_mod.log.name):
            task_id, task = await create_task(None, _boom(), id="chat-xyz")
            # Wait for the task + its done-callback (which schedules cleanup).
            try:
                await task
            except RuntimeError:
                pass
            # Let the done-callback's scheduled cleanup_task coroutine run.
            await asyncio.sleep(0.05)
            return task_id

    task_id = asyncio.run(_run())

    # The failure was logged with the traceback.
    assert any(
        "kaboom" in rec.getMessage() or (rec.exc_info and "kaboom" in str(rec.exc_info))
        for rec in caplog.records
    ), "expected the task failure to be logged"

    # And the task was removed from both registries.
    assert task_id not in tasks
    assert "chat-xyz" not in item_tasks or task_id not in item_tasks.get("chat-xyz", [])


def test_successful_task_does_not_log_error(caplog):
    async def _ok():
        return 42

    async def _run():
        with caplog.at_level(logging.ERROR, logger=tasks_mod.log.name):
            task_id, task = await create_task(None, _ok(), id="chat-ok")
            await task
            await asyncio.sleep(0.05)
            return task_id

    task_id = asyncio.run(_run())
    assert not any(rec.levelno >= logging.ERROR for rec in caplog.records)
    assert task_id not in tasks
