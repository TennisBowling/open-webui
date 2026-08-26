"""Per-model context-window discovery from ``GET /v1/models``.

Every connection this app talks to is an OpenAI-compatible gateway, and all of
them expose a model list — but they do NOT agree on what the context window is
called, and one of them omits it entirely. MEASURED 2026-07-30 across the
connections configured on this instance:

    OpenRouter          -> "context_length"  (also nested: top_provider.context_length)
    zenrouter  :4143    -> "context_length"
    surplus    :4144    -> "context_length"
    codex-proxy:4142    -> "context_window"  (upstream Codex's own spelling)
    llama-swap :8085    -> neither key present

So a single hard-coded key name silently reads ``None`` for whole connections.
This module is the one place that knows the spellings.

The same model can also carry DIFFERENT windows on different connections — the
ChatGPT-plan proxy serves ``gpt-5.6-sol`` at 272,000 while OpenRouter serves the
same slug at 1,050,000 — so the window is a property of the (model, connection)
pair as returned by that connection's own model list. Never key a window off the
model id alone.

"Unknown" is a first-class answer and must stay distinguishable from "small".
A caller deciding whether to compact has to be able to tell "this model has no
declared window" (do nothing) from "this model's window is 4096" (act now).
Hence ``Optional[int]`` rather than a 0 sentinel: ``routers/audio.py`` uses the
``or 0`` idiom for a display field, which is fine there and would be a bug here.
"""

from __future__ import annotations

from typing import Any, Optional

# Checked in order, first usable value wins. `context_length` is OpenRouter's
# spelling and the de-facto standard among the OpenAI-compatible gateways;
# `context_window` is upstream OpenAI/Codex's. The nested `top_provider` form is
# OpenRouter's per-provider block, which can be present when the flat field is
# absent on some catalog rows.
_FLAT_KEYS = ("context_length", "context_window", "max_input_tokens")


def _coerce(value: Any) -> Optional[int]:
    """Return a positive int, or None for anything that can't be one.

    Providers have been observed returning `0`, `null`, and stringified numbers
    in this field. Zero and negatives mean "not declared", not "no room".
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def resolve_context_length(model: Optional[dict]) -> Optional[int]:
    """Best-effort context window for one model dict from a ``/v1/models`` list.

    Looks at the flat spellings first, then OpenRouter's nested ``top_provider``,
    then the raw provider payload that ``merge_models_lists`` preserves under
    ``model["openai"]`` (so a field that survived only on the untouched original
    is still found). Returns None when the connection declares no window.
    """
    if not isinstance(model, dict):
        return None

    for key in _FLAT_KEYS:
        found = _coerce(model.get(key))
        if found is not None:
            return found

    top_provider = model.get("top_provider")
    if isinstance(top_provider, dict):
        found = _coerce(top_provider.get("context_length"))
        if found is not None:
            return found

    # `merge_models_lists` keeps the verbatim provider object here; recurse once
    # so a spelling that only exists on the raw row is still picked up. Guarded
    # against self-reference so a provider echoing itself can't spin.
    raw = model.get("openai")
    if isinstance(raw, dict) and raw is not model:
        return resolve_context_length(raw)

    return None


def apply_context_length(model: dict) -> Optional[int]:
    """Normalize a model dict in place to carry a canonical ``context_length``.

    Idempotent, and never overwrites a good value with None — re-running over an
    already-normalized list is a no-op. Returns the resolved value so callers can
    log or branch on it.
    """
    if not isinstance(model, dict):
        return None
    resolved = resolve_context_length(model)
    if resolved is not None:
        model["context_length"] = resolved
    return resolved
