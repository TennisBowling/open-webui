"""Unit tests for ``utils.reasoning_context`` — the ``reasoning.context`` probe.

Pure logic: no DB, no network. The error strings asserted below are REAL captures
taken 2026-07-30 from OpenRouter and from the ChatGPT-plan Codex backend, not
invented shapes — the whole point of the classifier is that it recognises what
those two actually emit.
"""

import pytest

from open_webui.utils import reasoning_context


@pytest.fixture(autouse=True)
def _clear_cache():
    reasoning_context.reset_cache()
    yield
    reasoning_context.reset_cache()


# Verbatim body from `openai/gpt-5.2` via OpenRouter (the wrapper nests the
# provider's own JSON, escaped, under metadata.raw).
OPENROUTER_WRAPPED_REJECTION = (
    '{"error":{"message":"Provider returned error","code":400,"metadata":'
    '{"raw":"{\\n  \\"error\\": {\\n    \\"message\\": \\"Unsupported value: '
    "'all_turns' is not supported with the 'gpt-5.2-2025-12-11' model. "
    'Supported values are: \'auto\'.\\",\\n    \\"param\\": '
    '\\"reasoning.context\\"\\n  }\\n}"}}}'
)

# The same failure spoken natively (no OpenRouter wrapper).
NATIVE_REJECTION = (
    '{"error":{"message":"Unsupported value: \'all_turns\' is not supported with '
    "the 'gpt-5.2-2025-12-11' model. Supported values are: 'auto'.\","
    '"type":"invalid_request_error","param":"reasoning.context"}}'
)

# OpenRouter's own enum validation, which fires before the provider is reached.
OPENROUTER_ENUM_REJECTION = (
    '{"error":{"message":"reasoning.context: Invalid option: expected one of '
    '\\"auto\\"|\\"all_turns\\"|\\"current_turn\\"","code":400}}'
)


def test_recognises_openrouter_wrapped_rejection():
    assert reasoning_context.is_rejection(400, OPENROUTER_WRAPPED_REJECTION)


def test_recognises_native_rejection():
    assert reasoning_context.is_rejection(400, NATIVE_REJECTION)


def test_recognises_openrouter_enum_rejection():
    assert reasoning_context.is_rejection(400, OPENROUTER_ENUM_REJECTION)


def test_ignores_unrelated_400():
    """A context-window overflow is also a 400 and also says "context" — it must
    NOT be mistaken for a reasoning.context rejection, or the fix would silently
    disable itself on any long chat."""
    body = (
        '{"error":{"message":"This model\'s maximum context length is 272000 '
        'tokens, however you requested 300000 tokens.","code":'
        '"context_length_exceeded"}}'
    )
    assert not reasoning_context.is_rejection(400, body)


def test_ignores_other_unsupported_value_errors():
    """An unsupported `service_tier` mentions "Unsupported value" but not the
    reasoning param, so it must not strip reasoning.context."""
    body = (
        '{"error":{"message":"Unsupported value: \'flex\' is not supported with '
        "the 'gpt-4o' model.\",\"param\":\"service_tier\"}}"
    )
    assert not reasoning_context.is_rejection(400, body)


def test_non_400_is_never_a_rejection():
    assert not reasoning_context.is_rejection(500, NATIVE_REJECTION)
    assert not reasoning_context.is_rejection(200, NATIVE_REJECTION)


def test_empty_body_is_not_a_rejection():
    assert not reasoning_context.is_rejection(400, None)
    assert not reasoning_context.is_rejection(400, "")


def test_apply_adds_context_to_reasoning_object():
    payload = {"model": "openai/gpt-5.5", "reasoning": {"effort": "high"}}
    assert reasoning_context.apply_to_payload(payload, "openai/gpt-5.5", "all_turns")
    assert payload["reasoning"] == {"effort": "high", "context": "all_turns"}


def test_apply_is_noop_without_a_reasoning_object():
    """Non-reasoning requests must stay byte-identical — adding a `reasoning`
    object where the caller wanted none would turn reasoning ON."""
    payload = {"model": "openai/gpt-4o"}
    assert not reasoning_context.apply_to_payload(payload, "openai/gpt-4o", "all_turns")
    assert payload == {"model": "openai/gpt-4o"}


def test_apply_respects_an_explicit_caller_choice():
    payload = {"model": "openai/gpt-5.5", "reasoning": {"context": "current_turn"}}
    assert not reasoning_context.apply_to_payload(payload, "openai/gpt-5.5", "all_turns")
    assert payload["reasoning"]["context"] == "current_turn"


def test_apply_skips_effort_none():
    payload = {"model": "openai/gpt-5.5", "reasoning": {"effort": "none"}}
    assert not reasoning_context.apply_to_payload(payload, "openai/gpt-5.5", "all_turns")
    assert "context" not in payload["reasoning"]


def test_apply_rejects_invalid_mode():
    """A typo'd env value must send nothing rather than a value OpenRouter 400s on."""
    payload = {"model": "openai/gpt-5.5", "reasoning": {"effort": "high"}}
    assert not reasoning_context.apply_to_payload(payload, "openai/gpt-5.5", "bogus")
    assert "context" not in payload["reasoning"]
    # "" is the documented "leave it to the model" setting.
    assert not reasoning_context.apply_to_payload(payload, "openai/gpt-5.5", "")
    assert "context" not in payload["reasoning"]


def test_marked_model_is_skipped_on_later_requests():
    reasoning_context.mark_unsupported("openai/gpt-5.2")
    payload = {"model": "openai/gpt-5.2", "reasoning": {"effort": "high"}}
    assert not reasoning_context.apply_to_payload(payload, "openai/gpt-5.2", "all_turns")
    assert "context" not in payload["reasoning"]
    # ...and only that model is affected.
    other = {"model": "openai/gpt-5.5", "reasoning": {"effort": "high"}}
    assert reasoning_context.apply_to_payload(other, "openai/gpt-5.5", "all_turns")


def test_strip_removes_context_and_keeps_other_reasoning_fields():
    payload = {"reasoning": {"effort": "high", "context": "all_turns"}}
    reasoning_context.strip_from_payload(payload)
    assert payload["reasoning"] == {"effort": "high"}


def test_strip_drops_a_now_empty_reasoning_object():
    payload = {"model": "m", "reasoning": {"context": "all_turns"}}
    reasoning_context.strip_from_payload(payload)
    assert "reasoning" not in payload


def test_full_probe_cycle():
    """The whole contract end to end: apply -> rejected -> strip -> never again."""
    payload = {"model": "openai/gpt-5.2", "reasoning": {"effort": "high"}}
    assert reasoning_context.apply_to_payload(payload, "openai/gpt-5.2", "all_turns")

    assert reasoning_context.is_rejection(400, OPENROUTER_WRAPPED_REJECTION)
    reasoning_context.mark_unsupported(payload["model"])
    reasoning_context.strip_from_payload(payload)
    assert payload == {"model": "openai/gpt-5.2", "reasoning": {"effort": "high"}}

    # A second request for the same model never pays the probe again.
    retry = {"model": "openai/gpt-5.2", "reasoning": {"effort": "high"}}
    assert not reasoning_context.apply_to_payload(retry, "openai/gpt-5.2", "all_turns")
