"""Pure model_id → provider classification for analytics grouping.

There is no stored ``model_id → provider`` mapping in this deployment; the only
signal available at analytics time is the ``model_id`` string itself. Routing
regime is read off the id shape:

  * bare ids (no ``/``)                 → the direct "C" gateway
  * ``vendor/model`` slugs              → OpenRouter; upstream = the prefix vendor
  * ``hf.co/…``, ``*.gguf``, ``name:tag`` → local models

These helpers are intentionally string-only and side-effect free so they can be
unit-tested and reused anywhere. The same base model served by two providers has
two different ids (e.g. ``gemini-3.1-pro-preview`` = "C" vs
``google/gemini-3.1-pro-preview`` = OpenRouter) and must stay distinct — callers
key by the raw ``model_id`` and only collapse to a provider bucket for display.
"""

from typing import Tuple

# Gateway / connection-level bucket keys (also used as their own display labels).
GATEWAY_C = "C"
GATEWAY_OPENROUTER = "OpenRouter"
LOCAL = "Local"
UNKNOWN = "Unknown"

# Pretty display labels for known OpenRouter upstream vendor slugs (vendor mode).
# Anything not listed falls back to a title-cased slug.
_VENDOR_LABELS = {
    "google": "Google",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "deepseek": "DeepSeek",
    "z-ai": "Z-AI",
    "minimax": "MiniMax",
    "moonshotai": "Moonshot AI",
    "qwen": "Qwen",
    "xiaomi": "Xiaomi",
    "openrouter": "OpenRouter",
    "x-ai": "xAI",
    "meta-llama": "Meta",
    "mistralai": "Mistral",
    "cohere": "Cohere",
    "nvidia": "NVIDIA",
    "microsoft": "Microsoft",
    "perplexity": "Perplexity",
    "amazon": "Amazon",
}


def _is_local(model_id: str) -> bool:
    """Heuristic for a locally-served (Ollama / GGUF / HuggingFace) model id."""
    mid = model_id
    if mid.startswith("hf.co/"):
        return True
    if mid.endswith(".gguf"):
        return True
    # Ollama-style ``name:tag`` with NO provider slug (e.g. ``qwen3-vl:30b``,
    # ``Qwen3-30B-A3B:latest``). A ``/`` means it is a routed slug — e.g.
    # ``minimax/minimax-m2.5:free`` — whose ``:free`` is an OpenRouter variant,
    # not a local tag, so the ``/`` guard keeps those out.
    if "/" not in mid and ":" in mid:
        return True
    return False


def classify_provider(model_id: str, mode: str = "gateway") -> Tuple[str, str]:
    """Return ``(key, label)`` for grouping ``model_id`` under ``mode``.

    ``mode == "gateway"``: key/label ∈ {"C", "OpenRouter", "Local", "Unknown"}.
    ``mode == "vendor"``:  bare → "C"; ``vendor/model`` → ``(<slug>, <pretty>)``;
                            local → "Local"; empty/"unknown" → "Unknown".

    ``key`` is the stable fold key; ``label`` is for display. (Model-level
    grouping does not use this helper — it keys by ``model_id`` and labels from
    the configured ``model.name``.)
    """
    mid = (model_id or "").strip()
    if not mid or mid == "unknown":
        return UNKNOWN, UNKNOWN
    if _is_local(mid):
        return LOCAL, LOCAL
    if "/" in mid:
        if mode == "vendor":
            slug = mid.split("/", 1)[0].lower()
            return slug, _VENDOR_LABELS.get(slug, slug.replace("-", " ").title())
        return GATEWAY_OPENROUTER, GATEWAY_OPENROUTER
    # Bare id (no slug) → the direct "C" gateway.
    return GATEWAY_C, GATEWAY_C
