"""Tests for the agentic loop / subagent bounds (Fix 4).

These guard:
* the subagent wall-clock timeout converting a hung subagent into a catchable
  ``SubagentTimeoutError`` (which the retry loop turns into a non-empty error
  result for the parent model), and
* the per-worker subagent concurrency semaphore.

NOTE on isolation: the bound values are read into module-level constants at
import. When the whole suite runs, some other test imports ``open_webui.env``
first, so we cannot rely on setting ``os.environ`` here to change those
constants. Instead these tests patch the already-bound constants in
``open_webui.utils.subagent`` directly, which is what the runtime actually reads.
"""

import asyncio
import os
import shutil
import tempfile

_TMPDIR = tempfile.mkdtemp()
_DB_PATH = os.path.join(_TMPDIR, "subagent_bounds_test.db")
_HERE = os.path.dirname(__file__)
_DEV_DB = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "data", "webui.db"))
if os.path.exists(_DEV_DB):
    shutil.copy(_DEV_DB, _DB_PATH)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB_PATH}")

import open_webui.utils.subagent as sa  # noqa: E402


def test_default_bounds_are_sane():
    # The shipped defaults: a cap exists, a timeout exists, concurrency bounded.
    from open_webui.env import (
        AGENTIC_MAX_TOOL_ROUNDS,
        SUBAGENT_RUN_TIMEOUT_SECONDS,
        SUBAGENT_MAX_CONCURRENCY,
    )

    assert AGENTIC_MAX_TOOL_ROUNDS > 0
    assert SUBAGENT_RUN_TIMEOUT_SECONDS > 0
    assert SUBAGENT_MAX_CONCURRENCY > 0


def test_subagent_guard_times_out_hung_inner_chat(monkeypatch):
    monkeypatch.setattr(sa, "SUBAGENT_RUN_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(sa, "SUBAGENT_MAX_CONCURRENCY", 2)

    async def _hang(**kwargs):
        await asyncio.sleep(30)
        return "unreachable"

    monkeypatch.setattr(sa, "_run_inner_chat", _hang)

    async def _run():
        try:
            await sa._run_inner_chat_guarded(prompt="x")
            return False
        except sa.SubagentTimeoutError:
            return True

    assert asyncio.run(_run()) is True


def test_subagent_guard_passes_through_result_when_fast(monkeypatch):
    monkeypatch.setattr(sa, "SUBAGENT_RUN_TIMEOUT_SECONDS", 30)

    async def _fast(**kwargs):
        return "final answer"

    monkeypatch.setattr(sa, "_run_inner_chat", _fast)
    assert asyncio.run(sa._run_inner_chat_guarded(prompt="x")) == "final answer"


def test_subagent_guard_disabled_timeout_still_runs(monkeypatch):
    # Timeout disabled (<= 0) must still execute and return the result.
    monkeypatch.setattr(sa, "SUBAGENT_RUN_TIMEOUT_SECONDS", 0)

    async def _fast(**kwargs):
        return "ok"

    monkeypatch.setattr(sa, "_run_inner_chat", _fast)
    assert asyncio.run(sa._run_inner_chat_guarded(prompt="x")) == "ok"


def test_concurrency_semaphore_bound(monkeypatch):
    monkeypatch.setattr(sa, "SUBAGENT_MAX_CONCURRENCY", 3)
    # Reset the lazily-created singleton so we observe a fresh bind.
    monkeypatch.setattr(sa, "_subagent_concurrency_sem", None)
    sem = sa._get_subagent_concurrency_sem()
    assert isinstance(sem, asyncio.Semaphore)
    assert sem._value == 3


def test_concurrency_semaphore_disabled(monkeypatch):
    monkeypatch.setattr(sa, "SUBAGENT_MAX_CONCURRENCY", 0)
    monkeypatch.setattr(sa, "_subagent_concurrency_sem", None)
    assert sa._get_subagent_concurrency_sem() is None

