"""Provider error classification: retryability, context-limit detection, canonical terminal payloads.

Extracted verbatim from utils/middleware.py (2026-08-02 de-spaghettification).
Pure functions over provider/gateway error shapes. Conservative by
design: only well-known deterministic phrases mark an error nonretryable.
"""

from typing import Any, Optional

from open_webui.utils import fast_json as json


def _nonstreaming_round_length_error(res: dict) -> str | None:
    choices = res.get("choices") if isinstance(res, dict) else None
    if not choices:
        return None
    choice = choices[0] or {}
    if choice.get("finish_reason") != "length":
        return None
    message = choice.get("message") or {}
    if message.get("tool_calls") or message.get("content"):
        return None
    return (
        "Model reached the completion token limit before producing final text "
        "or a tool call. Increase the output token limit or lower reasoning effort."
    )


# Deterministic upstream-error signals: re-issuing the IDENTICAL request can never
# fix these (an over-long input only stays over-long; a max-output truncation
# truncates again), so a round that fails this way must surface immediately instead
# of burning AGENTIC_EMPTY_ROUND_MAX_RETRIES retries of the same doomed call. This
# is the root cause behind "a research subagent ran 20-30 min and THEN errored":
# its accumulated transcript overflowed the model's context window, and every layer
# (round-level retry, inner-chat-level retry) kept re-sending the same over-limit
# payload. CONSERVATIVE by design — only well-known deterministic phrases match, so
# a genuinely transient 5xx / 429 / timeout / connection drop is still retried.
_CONTEXT_LIMIT_PROVIDER_ERROR_NEEDLES = (
    "exceeds the context window",
    "context window",
    "context length",
    "context_length_exceeded",
    "maximum context length",
    "maximum context size",
    "prompt is too long",
    "input is too long",
    "input too long",
    "too many tokens",
    "reduce the length",
    "string too long",
)

_NONRETRYABLE_PROVIDER_ERROR_NEEDLES = (
    *_CONTEXT_LIMIT_PROVIDER_ERROR_NEEDLES,
    # finish_reason=="length" surfaced by _nonstreaming_round_length_error
    "completion token limit",
    # Gemini rejects a structurally invalid transcript deterministically. Sending
    # the identical role sequence again cannot recover and used to burn all five
    # agentic retries before surfacing the same 400.
    "requests ending with a model turn are not supported",
)

# Some gateways do not forward the model's context-limit response. The two
# shapes below are what the same over-limit subagent turns in production
# actually finalized as:
#
# * six empty/answerless responses, synthesized locally as "no response after
#   retrying";
# * an Envoy-style disconnect/reset before headers.
#
# They remain RETRYABLE inside the per-round loop: a one-off empty response or
# connection reset is transient. Only after that loop exhausts every retry do
# we stamp a durable machine-readable code which lets the subagent lifecycle
# hand the turn to its configured long-context successor.
_CONTEXT_MASKING_CONNECTION_ERROR_NEEDLES = (
    "upstream connect error or disconnect/reset before headers",
    "reset reason: connection termination",
)

_CONTEXT_FALLBACK_ERROR_CODES = frozenset(
    {
        "context_length_exceeded",
        "empty_response_retries_exhausted",
        "provider_connection_retries_exhausted",
    }
)


def _provider_error_text(err: Any) -> str:
    """Best-effort extract a human-readable message from a terminal_error-like
    payload: a raw string, a ``{"content"/"message"/"detail": ...}`` dict, or a
    nested OpenAI-style ``{"error": {...}}``."""
    if err is None:
        return ""
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        for key in ("content", "message", "detail"):
            value = err.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                nested = _provider_error_text(value)
                if nested:
                    return nested
        nested_err = err.get("error")
        if nested_err is not None and nested_err is not err:
            return _provider_error_text(nested_err)
        try:
            return json.dumps(err)
        except Exception:
            return str(err)
    return str(err)


def _provider_error_code(err: Any) -> str:
    """Extract a provider/internal error code without stringifying the payload."""
    if not isinstance(err, dict):
        return ""
    for key in ("code", "type"):
        value = err.get(key)
        if isinstance(value, str) and value:
            return value.strip().lower()
    nested = err.get("error")
    if nested is not None and nested is not err:
        return _provider_error_code(nested)
    return ""


def _is_nonretryable_provider_error(err: Any) -> bool:
    """True when an upstream failure is a DETERMINISTIC client error (over-long
    input / context-window exceeded / empty max-output truncation) that retrying
    the identical request cannot recover. Used to short-circuit the empty/failed
    round retry loop so the turn surfaces the real error fast instead of stalling
    for minutes on doomed retries."""
    text = _provider_error_text(err).lower()
    if not text:
        return False
    return any(needle in text for needle in _NONRETRYABLE_PROVIDER_ERROR_NEEDLES)


def _is_context_limit_provider_error(err: Any) -> bool:
    """True only for an over-long INPUT/context-window failure.

    This narrower classifier is intentionally separate from
    ``_is_nonretryable_provider_error``. An empty ``finish_reason=length`` is
    deterministic too, but changing to a model with a larger input window does
    not fix an output-token cap. Subagents use this predicate to decide whether
    their configured long-context successor should take over.
    """
    if _provider_error_code(err) in {
        "context_length_exceeded",
        "context_window_exceeded",
        "max_context_length_exceeded",
    }:
        return True
    text = _provider_error_text(err).lower()
    if not text:
        return False
    return any(needle in text for needle in _CONTEXT_LIMIT_PROVIDER_ERROR_NEEDLES)


def _is_context_fallback_provider_error(err: Any) -> bool:
    """Whether a finalized subagent error should trigger the long-context model.

    Explicit context-limit responses are definitive. The two retry-exhausted
    cases are deliberately narrower: they match the structured codes stamped by
    this middleware, plus their legacy text forms for turns persisted before
    those codes existed. They are NOT part of
    ``_is_nonretryable_provider_error`` because the round loop must still retry
    an isolated empty response or connection reset before declaring exhaustion.
    """
    if _is_context_limit_provider_error(err):
        return True
    if _provider_error_code(err) in _CONTEXT_FALLBACK_ERROR_CODES:
        return True
    text = _provider_error_text(err).lower()
    if "the model returned no response after retrying" in text and "times" in text:
        return True
    return any(needle in text for needle in _CONTEXT_MASKING_CONNECTION_ERROR_NEEDLES)


def _provider_error_payload(
    err: Any,
    *,
    retries_exhausted: bool = False,
    empty_response: bool = False,
) -> dict:
    """Normalize a terminal provider failure while preserving its cause.

    Historically the final persistence step reduced every error to
    ``{"content": ...}``, erasing whether the round retry loop had exhausted.
    That made a gateway-masked context overflow indistinguishable from a
    one-off network reset. This canonical payload keeps the reader-facing text
    and only the small, safe machine fields needed by the subagent handoff.
    """
    content = _provider_error_text(err)
    if not content:
        content = "The model request failed and could not be recovered."
    payload: dict[str, Any] = {"content": content}

    existing_code = _provider_error_code(err)
    if existing_code in _CONTEXT_FALLBACK_ERROR_CODES:
        payload["code"] = existing_code
    elif _is_context_limit_provider_error(err):
        payload["code"] = "context_length_exceeded"
    elif retries_exhausted and empty_response:
        payload["code"] = "empty_response_retries_exhausted"
    elif retries_exhausted and any(
        needle in content.lower()
        for needle in _CONTEXT_MASKING_CONNECTION_ERROR_NEEDLES
    ):
        payload["code"] = "provider_connection_retries_exhausted"

    was_exhausted = retries_exhausted or (
        isinstance(err, dict) and err.get("retry_exhausted") is True
    )
    if was_exhausted:
        payload["retry_exhausted"] = True
    return payload


def _safe_error_response_text(resp: Any) -> Optional[str]:
    """Read a best-effort text body from an UNCONSUMED error/unknown round response
    (a Starlette Response with ``.body``, or a bare error dict) for classification.
    Returns None when there's nothing readable (so the caller treats it as an
    unknown — retryable — failure)."""
    try:
        if hasattr(resp, "body"):
            body = resp.body
            if isinstance(body, (bytes, bytearray)):
                return body.decode("utf-8", "replace")
            return str(body)
        if isinstance(resp, dict):
            return json.dumps(resp.get("error", resp))
    except Exception:
        return None
    return None
