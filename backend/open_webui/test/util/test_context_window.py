"""Unit tests for ``utils.context_window`` — per-model context-window discovery.

The key-name variants below are REAL captures taken 2026-07-30 from the
connections configured on this instance, not invented shapes. The whole point of
the module is tolerating exactly this spread, so the tests assert against what
those gateways actually return.
"""

from open_webui.utils.context_window import (
    apply_context_length,
    resolve_context_length,
)


def test_openrouter_flat_key():
    assert resolve_context_length({"id": "openai/gpt-5.6-sol", "context_length": 1050000}) == 1050000


def test_codex_proxy_spelling():
    """The ChatGPT-plan proxy used upstream Codex's `context_window` spelling."""
    assert resolve_context_length({"id": "gpt-5.6-sol", "context_window": 272000}) == 272000


def test_nested_top_provider_when_flat_key_absent():
    model = {"id": "x", "top_provider": {"context_length": 400000, "max_completion_tokens": 128000}}
    assert resolve_context_length(model) == 400000


def test_flat_key_wins_over_nested():
    model = {"id": "x", "context_length": 1000, "top_provider": {"context_length": 2000}}
    assert resolve_context_length(model) == 1000


def test_reads_through_preserved_raw_provider_row():
    """`merge_models_lists` keeps the verbatim provider object under "openai";
    a spelling that survived only there must still be found."""
    model = {"id": "x", "openai": {"id": "x", "context_window": 272000}}
    assert resolve_context_length(model) == 272000


def test_self_referential_raw_row_terminates():
    model = {"id": "x"}
    model["openai"] = model  # provider echoing itself must not spin
    assert resolve_context_length(model) is None


def test_llama_swap_declares_nothing():
    """Unknown must stay distinguishable from small — llama-swap returns neither
    key, and a caller must be able to tell that from a genuinely tiny window."""
    assert resolve_context_length({"id": "qwen3.6-27B", "object": "model"}) is None


def test_zero_and_negative_are_unknown_not_zero():
    """Providers have been seen returning 0 here. Zero means "not declared", not
    "no room" — returning 0 would read as an instantly-exceeded window."""
    assert resolve_context_length({"context_length": 0}) is None
    assert resolve_context_length({"context_length": -1}) is None


def test_stringified_number_is_accepted():
    assert resolve_context_length({"context_length": "128000"}) == 128000


def test_junk_values_are_unknown():
    assert resolve_context_length({"context_length": "unlimited"}) is None
    assert resolve_context_length({"context_length": None}) is None
    assert resolve_context_length({"context_length": True}) is None
    assert resolve_context_length(None) is None
    assert resolve_context_length("not a dict") is None


def test_apply_is_idempotent_and_never_clobbers_with_none():
    model = {"id": "x", "context_window": 272000}
    assert apply_context_length(model) == 272000
    assert model["context_length"] == 272000
    # Re-running over an already-normalized dict is a no-op...
    assert apply_context_length(model) == 272000
    assert model["context_length"] == 272000
    # ...and a model with no declared window is left without the key entirely,
    # rather than getting an explicit None that reads as "declared: nothing".
    bare = {"id": "y"}
    assert apply_context_length(bare) is None
    assert "context_length" not in bare
