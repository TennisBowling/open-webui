"""Unit tests for the pure model_id → provider classifier.

These assert the gateway/vendor/local split over the real set of model ids seen
in this deployment, with special attention to the invariant that the same base
model served by two providers (bare "C" vs OpenRouter slug) never merges.
"""

from open_webui.utils.providers import classify_provider


def test_two_geminis_never_merge():
    # gateway
    assert classify_provider("gemini-3.1-pro-preview", "gateway")[0] == "C"
    assert classify_provider("google/gemini-3.1-pro-preview", "gateway")[0] == "OpenRouter"
    # vendor
    assert classify_provider("gemini-3.1-pro-preview", "vendor")[0] == "C"
    assert classify_provider("google/gemini-3.1-pro-preview", "vendor")[0] == "google"


def test_bare_ids_are_C():
    for mid in ["gpt-5.5", "claude-opus-4.8", "o3", "o4-mini", "gpt-5", "gemini-3-flash-preview",
                "claude-opus-4.7-1m-internal", "gpt-5-chat-latest", "gpt-4.1-nano"]:
        assert classify_provider(mid, "gateway")[0] == "C", mid
        assert classify_provider(mid, "vendor")[0] == "C", mid


def test_openrouter_vendor_split():
    cases = {
        "openai/gpt-5.2": ("OpenRouter", "openai", "OpenAI"),
        "anthropic/claude-opus-4.5": ("OpenRouter", "anthropic", "Anthropic"),
        "z-ai/glm-4.7": ("OpenRouter", "z-ai", "Z-AI"),
        "deepseek/deepseek-v4-pro": ("OpenRouter", "deepseek", "DeepSeek"),
        "minimax/minimax-m2.5:free": ("OpenRouter", "minimax", "MiniMax"),
        "moonshotai/kimi-k2-0905:exacto": ("OpenRouter", "moonshotai", "Moonshot AI"),
        "openrouter/hunter-alpha": ("OpenRouter", "openrouter", "OpenRouter"),
    }
    for mid, (gw, vk, vl) in cases.items():
        assert classify_provider(mid, "gateway")[0] == gw, mid
        assert classify_provider(mid, "vendor")[0] == vk, mid
        assert classify_provider(mid, "vendor")[1] == vl, mid


def test_local_models():
    for mid in ["hf.co/PantheonUnbound/Satyr-V0.1-4B:Q8_0",
                "Qwen3.5-27B-Uncensored-HauhauCS-Aggressive-IQ4_XS.gguf",
                "qwen3-vl:30b", "Qwen3-30B-A3B:latest", "Qwen3-32B:latest",
                "gemma-4-26B-A4B-it-UD-Q4_K_M.gguf",
                "hf.co/unsloth/Qwen3-32B-GGUF:Q4_K_XL"]:
        assert classify_provider(mid, "gateway")[0] == "Local", mid
        assert classify_provider(mid, "vendor")[0] == "Local", mid


def test_unknown_and_empty():
    for mid in ["unknown", "", None]:
        assert classify_provider(mid, "gateway")[0] == "Unknown"
        assert classify_provider(mid, "vendor")[0] == "Unknown"


def test_slash_overrides_local_tag():
    # A "/" means a routed slug; the ":free"/":exacto"/":nitro" suffix is an
    # OpenRouter variant, NOT a local Ollama tag.
    assert classify_provider("minimax/minimax-m2.5:free", "gateway")[0] == "OpenRouter"
    assert classify_provider("openai/gpt-oss-120b:nitro", "vendor")[0] == "openai"
