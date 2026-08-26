"""``reasoning.context`` — telling the model it may USE the reasoning we replay.

Preserving reasoning across turns is two separate things, and open-webui only did
the first one:

1. **The bytes.** ``reasoning_details`` items (encrypted / summary / text) are
   captured, persisted per round, and replayed verbatim on every later request.
   That machinery is thorough and documented in ``REASONING_DETAILS.md``.
2. **The permission.** ``reasoning.context`` decides whether the model is allowed
   to *look* at prior-turn reasoning items in the input at all:

   * ``all_turns``    – may reference reasoning from every turn in the input.
   * ``current_turn`` – prior-turn reasoning items are IGNORED.
   * ``auto``         – whatever the model's own default is.

Without (2), (1) can be pure waste. MEASURED 2026-07-30 against OpenRouter by
reading the ``reasoning`` object echoed back by ``POST /responses``, which reports
the *resolved* value rather than the requested one:

    openai/gpt-5.4      -> context: "current_turn"   <-- replayed reasoning DISCARDED
    openai/gpt-5.5      -> context: "current_turn"   <-- replayed reasoning DISCARDED
    openai/gpt-5.6-sol  -> context: "all_turns"
    gpt-5.6-sol (ChatGPT-plan Codex backend) -> context: "all_turns"

So on 5.4/5.5 every encrypted block we faithfully round-tripped was being thrown
away by the model.

Why this needs a negative cache instead of a capability lookup
-------------------------------------------------------------
The field is not universally accepted, and the failure is a hard 400, not a
silent ignore::

    openai/gpt-5.2 -> 400 "Unsupported value: 'all_turns' is not supported with
                      the 'gpt-5.2-2025-12-11' model."

while 5.4 / 5.4-mini / 5.5 / 5.6-* all accept it. (OpenRouter's own docs claim
"GPT-5.6 and newer only" — that is wrong; 5.4 and 5.5 take it.) Non-OpenAI models
tolerate the field: Claude, Gemini, DeepSeek and GLM all returned 200, because
OpenRouter drops it for providers that have no equivalent.

Crucially, OpenRouter's catalog exposes **no field that distinguishes them**.
``supported_parameters`` is byte-identical between gpt-5.2 and gpt-5.4, and the
per-model ``reasoning`` object has no ``context`` key. There is nothing to look
up, so capability is *discovered by trying*: send it, and if the model rejects it
with this specific error, remember that and never send it to that model again.

The cache is process-wide and deliberately not persisted — it costs at most one
retried request per model per restart, and a restart is exactly when you want a
model that has since gained support to be re-probed.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from open_webui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["OPENAI"])


# The values upstream accepts. Anything else is rejected by OpenRouter itself with
# `reasoning.context: Invalid option: expected one of "auto"|"all_turns"|"current_turn"`,
# so an unrecognised setting must never reach the wire.
VALID_REASONING_CONTEXTS = frozenset({"auto", "all_turns", "current_turn"})

# Models that answered the probe with "this field is not supported". Guarded
# because the agentic loop can have several rounds in flight across worker
# threads; a plain set would still be *correct* here (worst case is a duplicate
# probe) but the lock keeps the invariant obvious.
_unsupported_models: set[str] = set()
_lock = threading.Lock()


def is_known_unsupported(model_id: Optional[str]) -> bool:
    """Whether ``model_id`` has already rejected ``reasoning.context``."""
    if not model_id:
        return False
    with _lock:
        return model_id in _unsupported_models


def mark_unsupported(model_id: Optional[str]) -> None:
    """Record that ``model_id`` rejects ``reasoning.context``, so later requests
    omit it from the start instead of paying for a retry every time."""
    if not model_id:
        return
    with _lock:
        if model_id in _unsupported_models:
            return
        _unsupported_models.add(model_id)
    log.info(
        "reasoning.context is not supported by %s — omitting it for this model "
        "for the remainder of the process.",
        model_id,
    )


def reset_cache() -> None:
    """Test hook: forget every probed model."""
    with _lock:
        _unsupported_models.clear()


def is_rejection(status: int, body: Optional[str]) -> bool:
    """Whether an upstream error is specifically "this model does not accept
    ``reasoning.context``" — as opposed to any other 400.

    Deliberately narrow. A false positive here silently strips a parameter the
    user asked for; a false negative merely costs the fix on that model. Both
    the OpenAI-native shape and OpenRouter's wrapper (which nests the provider's
    body verbatim under ``metadata.raw``, escaped) are plain substring-matched,
    so no JSON parsing is needed and neither shape needs its own branch.

    Real captures this matches::

        {"error": {"message": "Unsupported value: 'all_turns' is not supported
         with the 'gpt-5.2-2025-12-11' model. Supported values are: 'auto'."}}

        {"error": {"message": "Provider returned error", "code": 400, "metadata":
         {"raw": "{\\n \\"error\\": {\\n \\"message\\": \\"Unsupported value:
         'all_turns' is not supported with the ...\\"}}"}}
    """
    if status != 400 or not body:
        return False
    lowered = body.lower()
    # The param name must appear, so a rejection of some *other* unsupported
    # value (a bad `service_tier`, say) can never strip reasoning.context.
    mentions_param = "reasoning.context" in lowered or (
        "context" in lowered and "reasoning" in lowered
    )
    if not mentions_param:
        return False
    return any(
        needle in lowered
        for needle in (
            "unsupported value",
            "is not supported with",
            "unsupported parameter",
            "unknown parameter",
            "invalid option",
        )
    )


def apply_to_payload(payload: dict, model_id: Optional[str], mode: str) -> bool:
    """Attach ``reasoning.context`` to an outbound chat-completions payload.

    Returns True when the field was actually added, so the caller knows whether a
    subsequent 400 is worth re-probing. No-ops when the mode is invalid, when the
    model already failed the probe, or when a caller upstream already set an
    explicit value (an explicit choice always wins over this default).
    """
    if mode not in VALID_REASONING_CONTEXTS:
        return False
    if is_known_unsupported(model_id):
        return False

    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, dict):
        return False
    # `none` means "do not reason at all" — pairing it with a context mode is
    # contradictory, and some providers reject the combination.
    if reasoning.get("effort") == "none":
        return False
    if reasoning.get("context"):
        return False

    reasoning["context"] = mode
    return True


def strip_from_payload(payload: dict) -> None:
    """Remove ``reasoning.context`` after a rejection, dropping a now-empty
    ``reasoning`` object entirely rather than sending ``{}`` (which some
    providers treat as "reasoning explicitly requested")."""
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, dict):
        return
    reasoning.pop("context", None)
    if not reasoning:
        payload.pop("reasoning", None)
