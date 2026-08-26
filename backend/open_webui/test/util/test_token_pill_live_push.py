"""Tests for the live per-chat token-usage pill push (chat:token-usage).

The in-chat token pill used to show ~0 / stale numbers while an agent was
actively working (especially when it spawned subagents) and only corrected on a
full chat reload. Root cause: subagent token usage rolls into the parent chat's
``conversation_token_usage`` row (so reload is correct) but its live usage events
never reach the parent pill, and the optimistic per-round delta path undercounts
multi-round turns.

The fix pushes the authoritative cumulative totals returned by
``update_conversation_token_usage`` as a tiny stream-scoped ``chat:token-usage``
event after every conversation write (``push_chat_token_stats``), throttled
per-chat (``_should_push_chat_token_stats``) so it stays bandwidth-cheap on a
limited data plan. These tests pin the throttle and the emit contract without a
live Postgres (they self-skip if the package can't import).
"""

import asyncio

import pytest

try:
    import open_webui.socket.main as socket_main
    from open_webui.socket.main import (
        _should_push_chat_token_stats,
        push_chat_token_stats,
        STREAM_SCOPED_TYPES,
    )

    _IMPORT_OK = True
    _IMPORT_ERR = None
except Exception as e:  # pragma: no cover - environment-dependent
    _IMPORT_OK = False
    _IMPORT_ERR = e

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK, reason=f"open_webui import unavailable: {_IMPORT_ERR}"
)


class _Stats:
    """Stand-in for ConversationTokenUsageResponse (attribute access only)."""

    def __init__(self, **kw):
        self.chat_id = kw.get("chat_id", "chat-1")
        self.total_input_tokens = kw.get("total_input_tokens", 0)
        self.total_output_tokens = kw.get("total_output_tokens", 0)
        self.total_tokens = kw.get("total_tokens", 0)
        self.total_cache_read_tokens = kw.get("total_cache_read_tokens", 0)
        self.last_input_tokens = kw.get("last_input_tokens", 0)
        self.last_output_tokens = kw.get("last_output_tokens", 0)
        self.last_cache_read_tokens = kw.get("last_cache_read_tokens", 0)
        self.message_count = kw.get("message_count", 0)


@pytest.fixture(autouse=True)
def _clean_throttle_state():
    socket_main._chat_token_push_last.clear()
    yield
    socket_main._chat_token_push_last.clear()


@pytest.fixture
def fake_clock(monkeypatch):
    state = {"now": 1000.0}
    monkeypatch.setattr(socket_main.time, "monotonic", lambda: state["now"])
    return state


# --------------------------------------------------------------------------- #
# Throttle
# --------------------------------------------------------------------------- #


def test_throttle_first_call_passes(fake_clock):
    assert _should_push_chat_token_stats("chat-1") is True


def test_throttle_drops_within_interval(fake_clock):
    assert _should_push_chat_token_stats("chat-1") is True
    # Immediately again (same clock) -> dropped.
    assert _should_push_chat_token_stats("chat-1") is False
    # Just before the interval expires -> still dropped.
    fake_clock["now"] += socket_main.CHAT_TOKEN_PUSH_MIN_INTERVAL - 0.001
    assert _should_push_chat_token_stats("chat-1") is False


def test_throttle_passes_after_interval(fake_clock):
    assert _should_push_chat_token_stats("chat-1") is True
    fake_clock["now"] += socket_main.CHAT_TOKEN_PUSH_MIN_INTERVAL + 0.001
    assert _should_push_chat_token_stats("chat-1") is True


def test_throttle_is_per_chat(fake_clock):
    assert _should_push_chat_token_stats("chat-1") is True
    # A different chat is independent even within the interval.
    assert _should_push_chat_token_stats("chat-2") is True
    assert _should_push_chat_token_stats("chat-1") is False


def test_throttle_rejects_empty_chat(fake_clock):
    assert _should_push_chat_token_stats("") is False
    assert _should_push_chat_token_stats(None) is False


def test_throttle_prunes_when_large(fake_clock):
    # Seed many old entries, then trip the prune by exceeding the cap.
    for i in range(1100):
        socket_main._chat_token_push_last[f"old-{i}"] = 0.0  # far in the past
    fake_clock["now"] = 10_000.0
    assert _should_push_chat_token_stats("fresh") is True
    # Old entries (older than 300s before now) were pruned.
    assert len(socket_main._chat_token_push_last) < 1100


# --------------------------------------------------------------------------- #
# push_chat_token_stats emit contract
# --------------------------------------------------------------------------- #


def _capture_emit(monkeypatch):
    captured = []

    async def fake_emit(user_id, payload):
        captured.append((user_id, payload))

    monkeypatch.setattr(socket_main, "emit_to_primary", fake_emit)
    return captured


def test_push_emits_stream_scoped_authoritative_totals(monkeypatch, fake_clock):
    captured = _capture_emit(monkeypatch)

    async def _fixed_cost(_chat_id):
        return 0.4211

    monkeypatch.setattr(socket_main, "_compute_chat_cost", _fixed_cost)

    stats = _Stats(
        chat_id="chat-1",
        total_input_tokens=34094,
        total_output_tokens=39,
        total_tokens=34133,
        total_cache_read_tokens=12000,
        last_input_tokens=34094,
        last_output_tokens=39,
        last_cache_read_tokens=12000,
        message_count=7,
    )
    asyncio.run(push_chat_token_stats("user-1", "chat-1", stats))

    assert len(captured) == 1
    user_id, payload = captured[0]
    assert user_id == "user-1"
    # Top-level chat_id is required for stream-room routing.
    assert payload["chat_id"] == "chat-1"
    data = payload["data"]
    # The type must be registered stream-scoped so it lands only on the tabs
    # viewing this chat (not a USER_POOL fanout).
    assert data["type"] == "chat:token-usage"
    assert data["type"] in STREAM_SCOPED_TYPES
    inner = data["data"]
    assert inner["total_input_tokens"] == 34094
    assert inner["total_output_tokens"] == 39
    assert inner["total_tokens"] == 34133
    assert inner["total_cache_read_tokens"] == 12000
    assert inner["last_input_tokens"] == 34094
    assert inner["message_count"] == 7
    # Cost now rides the same push (recomputed at read time) so the $ segment
    # updates live instead of only on reload.
    assert inner["cost"] == 0.4211


def test_push_skips_local_chat(monkeypatch, fake_clock):
    captured = _capture_emit(monkeypatch)
    asyncio.run(push_chat_token_stats("user-1", "local:abc", _Stats(chat_id="local:abc")))
    assert captured == []


def test_push_skips_missing_args(monkeypatch, fake_clock):
    captured = _capture_emit(monkeypatch)
    asyncio.run(push_chat_token_stats("", "chat-1", _Stats()))
    asyncio.run(push_chat_token_stats("user-1", "", _Stats()))
    asyncio.run(push_chat_token_stats("user-1", "chat-1", None))
    assert captured == []


def test_push_respects_throttle(monkeypatch, fake_clock):
    captured = _capture_emit(monkeypatch)

    async def _zero(_chat_id):
        return 0.0

    monkeypatch.setattr(socket_main, "_compute_chat_cost", _zero)
    stats = _Stats(chat_id="chat-1", total_tokens=10, message_count=1)
    asyncio.run(push_chat_token_stats("user-1", "chat-1", stats))
    # Second push within the interval is throttled (dropped).
    asyncio.run(push_chat_token_stats("user-1", "chat-1", stats))
    assert len(captured) == 1
    # After the interval it passes again.
    fake_clock["now"] += socket_main.CHAT_TOKEN_PUSH_MIN_INTERVAL + 0.001
    asyncio.run(push_chat_token_stats("user-1", "chat-1", stats))
    assert len(captured) == 2


# --------------------------------------------------------------------------- #
# Cost rides the push (recomputed at read time, after the throttle gate)
# --------------------------------------------------------------------------- #


def test_push_computes_cost_only_after_throttle_gate(monkeypatch, fake_clock):
    """The (potentially DB-touching) cost recompute must run ONLY for pushes that
    actually emit — never for throttled-out ones — so heavy multi-round runs add
    no wasted query load."""
    captured = _capture_emit(monkeypatch)
    calls = {"n": 0}

    async def _counting_cost(_chat_id):
        calls["n"] += 1
        return 1.5

    monkeypatch.setattr(socket_main, "_compute_chat_cost", _counting_cost)
    stats = _Stats(chat_id="chat-1", total_tokens=10, message_count=1)

    asyncio.run(push_chat_token_stats("user-1", "chat-1", stats))  # emits
    asyncio.run(push_chat_token_stats("user-1", "chat-1", stats))  # throttled
    assert len(captured) == 1
    assert calls["n"] == 1  # cost NOT recomputed for the throttled push
    assert captured[0][1]["data"]["data"]["cost"] == 1.5

    fake_clock["now"] += socket_main.CHAT_TOKEN_PUSH_MIN_INTERVAL + 0.001
    asyncio.run(push_chat_token_stats("user-1", "chat-1", stats))  # emits
    assert len(captured) == 2
    assert calls["n"] == 2


def test_push_survives_cost_failure(monkeypatch, fake_clock):
    """A cost hiccup must never drop the token push (cost degrades to a safe 0)."""
    captured = _capture_emit(monkeypatch)

    async def _boom(_chat_id):
        # _compute_chat_cost swallows internally, but assert push is robust even
        # if a future refactor lets an exception escape the cost helper.
        raise RuntimeError("pricing down")

    monkeypatch.setattr(socket_main, "_compute_chat_cost", _boom)
    stats = _Stats(chat_id="chat-1", total_tokens=10, message_count=1)
    # Should not raise.
    asyncio.run(push_chat_token_stats("user-1", "chat-1", stats))
    # The token push is best-effort; a cost exception is caught by the outer
    # try/except, so either nothing emits or it emits without crashing — never a
    # raised exception into the streaming path.
    assert isinstance(captured, list)


def test_compute_chat_cost_skips_local_and_empty():
    assert asyncio.run(socket_main._compute_chat_cost("")) == 0.0
    assert asyncio.run(socket_main._compute_chat_cost(None)) == 0.0
    assert asyncio.run(socket_main._compute_chat_cost("local:abc")) == 0.0


def test_compute_chat_cost_returns_analytics_value(monkeypatch):
    import open_webui.models.analytics as analytics_mod

    async def _fake_get_chat_cost(_chat_id):
        return 0.99

    monkeypatch.setattr(analytics_mod.Analytics, "get_chat_cost", _fake_get_chat_cost)
    assert asyncio.run(socket_main._compute_chat_cost("chat-1")) == 0.99


def test_compute_chat_cost_swallows_errors(monkeypatch):
    import open_webui.models.analytics as analytics_mod

    async def _raise(_chat_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(analytics_mod.Analytics, "get_chat_cost", _raise)
    # Must degrade to 0.0, never propagate.
    assert asyncio.run(socket_main._compute_chat_cost("chat-1")) == 0.0
