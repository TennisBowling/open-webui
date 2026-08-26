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

import pytest

import open_webui.tasks as tasks_mod
from open_webui.tasks import create_task, tasks, item_tasks


class _LeasePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self

        return queue

    async def execute(self):
        results = []
        for name, args, kwargs in self.ops:
            results.append(await getattr(self.redis, name)(*args, **kwargs))
        self.ops = []
        return results


class _LeaseRedis:
    def __init__(self):
        self.hashes = {}
        self.sets = {}
        self.values = {}

    def pipeline(self):
        return _LeasePipeline(self)

    async def hset(self, name, key, value):
        self.hashes.setdefault(name, {})[str(key)] = value
        return 1

    async def hdel(self, name, key):
        return int(self.hashes.setdefault(name, {}).pop(str(key), None) is not None)

    async def hget(self, name, key):
        return self.hashes.get(name, {}).get(str(key))

    async def hkeys(self, name):
        return list(self.hashes.get(name, {}))

    async def hmget(self, name, keys):
        return [self.hashes.get(name, {}).get(str(key)) for key in keys]

    async def sadd(self, name, value):
        before = len(self.sets.setdefault(name, set()))
        self.sets[name].add(str(value))
        return int(len(self.sets[name]) != before)

    async def srem(self, name, value):
        values = self.sets.setdefault(name, set())
        existed = str(value) in values
        values.discard(str(value))
        return int(existed)

    async def smembers(self, name):
        return set(self.sets.get(name, set()))

    async def scard(self, name):
        return len(self.sets.get(name, set()))

    async def set(self, name, value, **kwargs):
        if kwargs.get("nx") and name in self.values:
            return False
        self.values[name] = value
        return True

    async def exists(self, name):
        return int(name in self.values)

    async def get(self, name):
        return self.values.get(name)

    async def mget(self, names):
        return [self.values.get(name) for name in names]

    async def eval(self, script, _numkeys, key, *args):
        expected = args[0]
        if self.values.get(key) != expected:
            return 0
        if "ARGV[2]" in script:
            self.values[key] = args[1]
            return 1
        self.values.pop(key, None)
        return 1

    async def delete(self, name):
        existed = name in self.values or name in self.sets
        self.values.pop(name, None)
        self.sets.pop(name, None)
        return int(existed)


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


def test_redis_backed_task_cannot_finish_before_registration(monkeypatch):
    async def _run():
        save_entered = asyncio.Event()
        allow_save = asyncio.Event()
        worker_started = asyncio.Event()
        saved = set()

        async def fake_save(_redis, task_id, item_id):
            save_entered.set()
            await allow_save.wait()
            saved.add((task_id, item_id))

        async def fake_cleanup(_redis, task_id, item_id):
            saved.discard((task_id, item_id))

        monkeypatch.setattr(tasks_mod, "redis_save_task", fake_save)
        monkeypatch.setattr(tasks_mod, "redis_cleanup_task", fake_cleanup)

        async def worker():
            worker_started.set()
            return "done"

        create_call = asyncio.create_task(
            create_task(object(), worker(), id="subagent-rerun:chat:entry")
        )
        await save_entered.wait()
        await asyncio.sleep(0)
        assert not worker_started.is_set()

        allow_save.set()
        task_id, task = await create_call
        assert (task_id, "subagent-rerun:chat:entry") in saved
        assert await task == "done"
        await asyncio.sleep(0.05)
        assert (task_id, "subagent-rerun:chat:entry") not in saved
        assert task_id not in tasks

    asyncio.run(_run())


def test_expired_redis_task_lease_prunes_ghost_registry_members():
    async def _run():
        redis = _LeaseRedis()
        await tasks_mod.redis_save_task(redis, "task-1", "subagent-rerun:chat:entry")

        lease_key = tasks_mod._task_lease_key("task-1")
        assert lease_key in redis.values
        assert await tasks_mod.redis_list_item_tasks(
            redis, "subagent-rerun:chat:entry"
        ) == ["task-1"]

        # Simulate SIGKILL/server loss: no cleanup callback runs, only the
        # expiring lease disappears. The next read must self-heal both indexes.
        redis.values.pop(lease_key)
        assert (
            await tasks_mod.redis_list_item_tasks(redis, "subagent-rerun:chat:entry")
            == []
        )
        assert "task-1" not in redis.hashes[tasks_mod.REDIS_TASKS_KEY]
        assert (
            "task-1"
            not in redis.sets[
                f"{tasks_mod.REDIS_ITEM_TASKS_KEY}:subagent-rerun:chat:entry"
            ]
        )

    asyncio.run(_run())


def test_live_task_refreshes_its_redis_lease(monkeypatch):
    async def _run():
        redis = _LeaseRedis()
        worker_started = asyncio.Event()
        worker_finish = asyncio.Event()
        monkeypatch.setattr(tasks_mod, "TASK_LEASE_HEARTBEAT_SECONDS", 0.01)

        async def worker():
            worker_started.set()
            await worker_finish.wait()

        task_id, task = await create_task(redis, worker(), id="chat-live")
        await worker_started.wait()
        redis.values.pop(tasks_mod._task_lease_key(task_id))
        await asyncio.sleep(0.03)
        assert await redis.exists(tasks_mod._task_lease_key(task_id))

        worker_finish.set()
        await task
        await asyncio.sleep(0.03)
        assert not await redis.exists(tasks_mod._task_lease_key(task_id))

    asyncio.run(_run())


def test_task_cancels_before_prolonged_registry_loss_can_expire_lease(
    monkeypatch,
):
    async def _run():
        redis = _LeaseRedis()
        worker_started = asyncio.Event()
        worker_cancelled = asyncio.Event()
        original_save = tasks_mod.redis_save_task
        save_calls = 0

        async def flaky_save(redis_arg, task_id, item_id):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 1:
                return await original_save(redis_arg, task_id, item_id)
            raise ConnectionError("redis unavailable")

        monkeypatch.setattr(tasks_mod, "redis_save_task", flaky_save)
        monkeypatch.setattr(tasks_mod, "TASK_LEASE_HEARTBEAT_SECONDS", 0.005)
        monkeypatch.setattr(tasks_mod, "TASK_REGISTRY_FAILURE_CANCEL_SECONDS", 0.015)

        async def worker():
            worker_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                worker_cancelled.set()

        task_id, task = await create_task(redis, worker(), id="chat-partitioned")
        await worker_started.wait()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.25)
        await asyncio.wait_for(worker_cancelled.wait(), timeout=0.25)
        await asyncio.sleep(0.03)
        assert task_id not in tasks
        assert task_id not in item_tasks.get("chat-partitioned", [])

    asyncio.run(_run())


def test_stop_tasks_and_wait_uses_registry_cleanup_as_ack(monkeypatch):
    async def _run():
        redis = _LeaseRedis()
        await tasks_mod.redis_save_task(redis, "task-1", "chat-delete")
        stopped = []

        async def fake_stop(redis_arg, task_id):
            stopped.append(task_id)

            async def settle():
                await asyncio.sleep(0.01)
                await tasks_mod.redis_cleanup_task(redis_arg, task_id, "chat-delete")

            asyncio.create_task(settle())
            return {"status": True}

        monkeypatch.setattr(tasks_mod, "stop_task", fake_stop)
        remaining = await tasks_mod.stop_tasks_and_wait(
            redis,
            ["task-1", "task-1"],
            timeout=0.2,
            poll_interval=0.002,
        )
        assert stopped == ["task-1"]
        assert remaining == []

    asyncio.run(_run())
