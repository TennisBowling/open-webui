"""Tool results are lazy-by-default on the wire.

The collapsed tool card renders from the CALL arguments + the slim stub
(summary/size/status); the body is fetched only on expand. Inlining only pays
below the stub's own overhead, so the default inline ceiling is small (512B)
for every tool. ask_user stays inline (its card shows the submitted answer
directly); subagent tools keep their pre-existing 64KB threshold (dual-store
recovery machinery — behavior deliberately unchanged).
"""

from open_webui.utils.middleware import (
    LAZY_RESULT_EXEMPT_TOOL_NAMES,
    SUBAGENT_INLINE_RESULT_MAX,
    TOOL_INLINE_RESULT_MAX,
    _slim_tool_result,
)


def _result(content: str, tool_call_id: str = "tc1") -> dict:
    return {"tool_call_id": tool_call_id, "content": content}


def test_small_results_stay_inline():
    small = _result("x" * TOOL_INLINE_RESULT_MAX)
    slim, body = _slim_tool_result(small, "some_generic_tool", store_body=True)
    assert slim is small
    assert body is None


def test_generic_results_above_ceiling_go_lazy():
    big = _result("x" * (TOOL_INLINE_RESULT_MAX + 1))
    slim, body = _slim_tool_result(big, "some_generic_tool", store_body=True)
    assert slim["content"] == ""
    assert slim["result_lazy"] is True
    assert slim["result_ref"] == "tc1"
    assert slim["size"] == TOOL_INLINE_RESULT_MAX + 1
    assert body["content"] == big["content"]


def test_web_results_use_the_same_small_ceiling():
    # Previously web tools inlined up to 2KB — now they follow the global
    # small ceiling like everything else.
    big = _result("x" * (TOOL_INLINE_RESULT_MAX + 1))
    slim, body = _slim_tool_result(big, "web_search", store_body=True)
    assert slim["result_lazy"] is True
    assert body is not None


def test_ask_user_is_exempt_regardless_of_size():
    assert "ask_user" in LAZY_RESULT_EXEMPT_TOOL_NAMES
    big = _result("x" * (TOOL_INLINE_RESULT_MAX * 10))
    slim, body = _slim_tool_result(big, "ask_user", store_body=True)
    assert slim is big
    assert body is None


def test_subagent_keeps_legacy_threshold():
    mid = _result("x" * (TOOL_INLINE_RESULT_MAX + 1))
    slim, body = _slim_tool_result(mid, "subagent_launch", store_body=True)
    assert slim is mid  # under 64KB: unchanged behavior, stays inline
    assert body is None

    big = _result("x" * (SUBAGENT_INLINE_RESULT_MAX + 1))
    slim, body = _slim_tool_result(big, "subagent_launch", store_body=True)
    assert slim["result_lazy"] is True
    assert body is not None


def test_non_string_content_untouched():
    structured = {"tool_call_id": "tc1", "content": ["not", "a", "string"]}
    slim, body = _slim_tool_result(structured, "some_generic_tool", store_body=True)
    assert slim is structured
    assert body is None
