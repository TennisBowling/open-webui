"""Unit tests for OpenRouter reasoning-effort discovery.

Covers the pure mapper (`map_openrouter_reasoning`) and the process-wide cache
(TTL, single-flight, last-good retention, catalog warming). Network is never
hit — `_fetch_catalog_reasoning` is monkeypatched. Async paths are driven with
`asyncio.run` inside sync tests because this harness has no asyncio plugin.
"""

import asyncio

import open_webui.utils.openrouter_reasoning as orr


def setup_function(_):
    orr.reset_cache_for_tests()


# --------------------------------------------------------------------------- #
# Pure mapper
# --------------------------------------------------------------------------- #


def test_map_supported_efforts_descending_to_ascending():
    # OpenRouter returns highest-first; we normalize to canonical ascending.
    out = orr.map_openrouter_reasoning(
        {"supported_efforts": ["max", "xhigh", "high", "medium", "low"]}
    )
    assert out["supported_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert out["is_reasoning"] is True


def test_map_keeps_max_and_minimal():
    out = orr.map_openrouter_reasoning(
        {
            "supported_efforts": ["high", "medium", "low", "minimal"],
            "default_effort": "medium",
        }
    )
    assert out["supported_efforts"] == ["minimal", "low", "medium", "high"]
    assert out["default_effort"] == "medium"


def test_map_drops_unknown_efforts():
    out = orr.map_openrouter_reasoning(
        {"supported_efforts": ["high", "ultra", "low", 7, None]}
    )
    assert out["supported_efforts"] == ["low", "high"]


def test_map_mandatory_flag_preserved():
    out = orr.map_openrouter_reasoning(
        {"mandatory": True, "supported_efforts": ["high", "medium", "low", "minimal"]}
    )
    assert out["mandatory"] is True


def test_map_reasoning_model_without_effort_granularity():
    # e.g. gemini-2.5-flash: reasons but exposes no effort selection.
    out = orr.map_openrouter_reasoning({"mandatory": False})
    assert out == {"mandatory": False, "is_reasoning": True}
    assert "supported_efforts" not in out


def test_map_null_supported_efforts_omitted():
    out = orr.map_openrouter_reasoning({"mandatory": False, "supported_efforts": None})
    assert "supported_efforts" not in out
    assert out["is_reasoning"] is True


def test_map_empty_dict_is_reasoning_marker():
    assert orr.map_openrouter_reasoning({}) == {"is_reasoning": True}


def test_map_non_dict_returns_none():
    assert orr.map_openrouter_reasoning(None) is None
    assert orr.map_openrouter_reasoning("nope") is None
    assert orr.map_openrouter_reasoning(["high"]) is None


def test_map_supports_max_tokens_and_default_enabled():
    out = orr.map_openrouter_reasoning(
        {
            "supports_max_tokens": True,
            "default_enabled": True,
            "supported_efforts": ["high"],
        }
    )
    assert out["supports_max_tokens"] is True
    assert out["default_enabled"] is True


def test_map_default_effort_unknown_dropped():
    out = orr.map_openrouter_reasoning(
        {"supported_efforts": ["high"], "default_effort": "bogus"}
    )
    assert "default_effort" not in out


# --------------------------------------------------------------------------- #
# Cache warming from catalog items
# --------------------------------------------------------------------------- #


def test_populate_from_catalog_items_extracts_reasoning():
    items = [
        {
            "id": "openai/gpt-5-mini",
            "reasoning": {"supported_efforts": ["high", "low"]},
        },
        {"id": "some/plain-model"},  # no reasoning → skipped
        {"id": "google/gemini-2.5-flash", "reasoning": {"mandatory": False}},
        "not-a-dict",
    ]
    n = orr.populate_from_catalog_items(items)
    assert n == 2
    assert orr.discover_reasoning_for_slug("openai/gpt-5-mini")[
        "supported_efforts"
    ] == [
        "low",
        "high",
    ]
    assert orr.discover_reasoning_for_slug("google/gemini-2.5-flash") == {
        "mandatory": False,
        "is_reasoning": True,
    }


def test_populate_empty_does_not_wipe_existing():
    orr.populate_from_catalog_items(
        [{"id": "a/b", "reasoning": {"supported_efforts": ["high"]}}]
    )
    assert not orr.cache_is_cold()
    # An items list with no reasoning objects must not clobber the good map.
    assert orr.populate_from_catalog_items([{"id": "c/d"}]) == 0
    assert orr.discover_reasoning_for_slug("a/b") is not None


def test_discover_for_unknown_slug_is_none():
    orr.populate_from_catalog_items(
        [{"id": "a/b", "reasoning": {"supported_efforts": ["high"]}}]
    )
    assert orr.discover_reasoning_for_slug("x/y") is None
    assert orr.discover_reasoning_for_slug("") is None


# --------------------------------------------------------------------------- #
# Async cache: single-flight, TTL, last-good retention
# --------------------------------------------------------------------------- #


def test_get_reasoning_map_fetches_and_caches(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch():
        calls["n"] += 1
        return {"openai/gpt-5-mini": {"supported_efforts": ["high", "low"]}}

    monkeypatch.setattr(orr, "_fetch_catalog_reasoning", fake_fetch)

    m1 = asyncio.run(orr.get_reasoning_map())
    assert "openai/gpt-5-mini" in m1
    assert calls["n"] == 1

    # Fresh cache → no second fetch.
    asyncio.run(orr.get_reasoning_map())
    assert calls["n"] == 1


def test_get_reasoning_map_retains_last_good_on_failure(monkeypatch):
    async def good_fetch():
        return {"a/b": {"supported_efforts": ["high"]}}

    monkeypatch.setattr(orr, "_fetch_catalog_reasoning", good_fetch)
    asyncio.run(orr.get_reasoning_map())
    assert orr.discover_reasoning_for_slug("a/b") is not None

    # Force a refresh that fails — the last-good map must survive.
    async def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(orr, "_fetch_catalog_reasoning", boom)
    m = asyncio.run(orr.get_reasoning_map(force=True))
    assert "a/b" in m
    assert orr.discover_reasoning_for_slug("a/b") is not None


def test_get_reasoning_map_empty_fetch_does_not_wipe(monkeypatch):
    async def good_fetch():
        return {"a/b": {"supported_efforts": ["high"]}}

    monkeypatch.setattr(orr, "_fetch_catalog_reasoning", good_fetch)
    asyncio.run(orr.get_reasoning_map())

    async def empty_fetch():
        return {}

    monkeypatch.setattr(orr, "_fetch_catalog_reasoning", empty_fetch)
    m = asyncio.run(orr.get_reasoning_map(force=True))
    assert m.get("a/b") is not None


def test_discover_async_forces_refresh(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch():
        calls["n"] += 1
        return {"a/b": {"supported_efforts": ["high", "medium"]}}

    monkeypatch.setattr(orr, "_fetch_catalog_reasoning", fake_fetch)
    out = asyncio.run(orr.discover_reasoning_for_slug_async("a/b", force=True))
    assert out["supported_efforts"] == ["medium", "high"]
    assert calls["n"] == 1
    # force=True again re-fetches.
    asyncio.run(orr.discover_reasoning_for_slug_async("a/b", force=True))
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# Input-modality discovery
#
# Regression cover for the "video capability is invisible" bug: a connection
# configured with a `model_ids` allowlist never fetches the real provider list
# (Open WebUI synthesizes bare {id, name, owned_by} stubs), so `architecture` —
# and therefore the video input modality — is absent from every model on that
# connection. The modality map is what restores it.
# --------------------------------------------------------------------------- #


def test_extract_modalities_map_reads_architecture():
    out = orr._extract_modalities_map(
        [
            {
                "id": "google/gemini-3.6-flash",
                "architecture": {"input_modalities": ["text", "video"]},
            },
            {"id": "deepseek/chat", "architecture": {"input_modalities": ["text"]}},
        ]
    )
    assert out["google/gemini-3.6-flash"] == ["text", "video"]
    assert out["deepseek/chat"] == ["text"]


def test_extract_modalities_map_skips_malformed_entries():
    out = orr._extract_modalities_map(
        [
            None,
            "nonsense",
            {"id": "no-arch"},
            {"id": "arch-not-dict", "architecture": []},
            {"architecture": {"input_modalities": ["video"]}},  # no id
            {"id": "bad-modalities", "architecture": {"input_modalities": "video"}},
            {"id": "ok", "architecture": {"input_modalities": ["video", 7, None]}},
        ]
    )
    # Non-string modality entries are dropped rather than poisoning the list.
    assert out == {"ok": ["video"]}


def test_catalog_warm_populates_modalities_even_without_reasoning():
    # A payload can carry modalities but no reasoning objects; the modality map
    # must still be recorded (they are tracked independently on purpose).
    orr.populate_from_catalog_items(
        [{"id": "m/v", "architecture": {"input_modalities": ["text", "video"]}}]
    )
    assert orr.get_cached_modalities_map() == {"m/v": ["text", "video"]}


def test_modalities_survive_a_reasoning_only_refresh():
    orr.populate_from_catalog_items(
        [{"id": "m/v", "architecture": {"input_modalities": ["video"]}}]
    )
    # A later payload with reasoning but no architecture must not wipe modalities.
    orr.populate_from_catalog_items(
        [{"id": "m/r", "reasoning": {"supported_efforts": ["low"]}}]
    )
    assert orr.get_cached_modalities_map() == {"m/v": ["video"]}


def test_reset_clears_modalities():
    orr.populate_from_catalog_items(
        [{"id": "m/v", "architecture": {"input_modalities": ["video"]}}]
    )
    orr.reset_cache_for_tests()
    assert orr.get_cached_modalities_map() == {}
