from open_webui.utils.tool_calling import (
    merge_streamed_tool_call_field,
    mcp_model_facing_tool_name,
    parse_tool_call_arguments,
)


def _dedupe_repeated_tool_name(name: str | None) -> str:
    if not name:
        return ""
    for unit_len in range(1, (len(name) // 2) + 1):
        if len(name) % unit_len == 0:
            unit = name[:unit_len]
            if unit and unit * (len(name) // unit_len) == name:
                return unit
    return name


def _merge_tool_call_fragments(chunks):
    value = ""
    for chunk in chunks:
        value = merge_streamed_tool_call_field(value, chunk)
    return value


def test_tool_call_arguments_use_json_before_ast(monkeypatch):
    def fail_if_called(_raw):
        raise AssertionError("ast.literal_eval should not run for valid JSON")

    monkeypatch.setattr(
        "open_webui.utils.tool_calling.ast.literal_eval", fail_if_called
    )

    assert parse_tool_call_arguments(
        '{"path":"notes.tex","raw":true,"value":null}'
    ) == {"path": "notes.tex", "raw": True, "value": None}


def test_tool_call_arguments_keep_ast_as_compatibility_fallback():
    assert parse_tool_call_arguments("{'path': 'notes.tex', 'raw': True}") == {
        "path": "notes.tex",
        "raw": True,
    }


def test_streamed_tool_call_arguments_preserve_overlap_boundary_chars():
    expected = '{"url":"https://www.airbnb.com/rooms/685384908686339135"}'

    assert (
        _merge_tool_call_fragments(
            [
                '{"url":"ht',
                "tps://ww",
                "w.airbnb.com/ro",
                "oms/6853849086",
                '86339135"}',
            ]
        )
        == expected
    )


def test_streamed_tool_call_arguments_preserve_repeated_runs():
    expected = (
        '{"a":"'
        + ("a" * 20)
        + '","x":"'
        + ("x" * 30)
        + '","schemes":"'
        + ("https://" * 3)
        + '","digits":"'
        + "0123456789" * 4
        + '","ab":"'
        + ("ab" * 15)
        + '"}'
    )

    assert (
        _merge_tool_call_fragments(
            [
                '{"a":"',
                "a" * 9,
                "a" * 11,
                '","x":"',
                "x" * 12,
                "x" * 18,
                '","schemes":"',
                "https://",
                "https://",
                "https://",
                '","digits":"',
                "0123456789" * 2,
                "0123456789" * 2,
                '","ab":"',
                "ab" * 5,
                "ab" * 10,
                '"}',
            ]
        )
        == expected
    )


def test_streamed_tool_call_field_accepts_cumulative_prefix_resends():
    assert (
        _merge_tool_call_fragments(
            [
                '{"url"',
                '{"url":"https://example.test"}',
                '{"url":"https://example.test"}',
            ]
        )
        == '{"url":"https://example.test"}'
    )


def test_streamed_tool_call_names_merge_without_argument_overlap_rules():
    name = ""
    for chunk in ["web_", "web_search", "web_search"]:
        name = _dedupe_repeated_tool_name(merge_streamed_tool_call_field(name, chunk))
    assert name == "web_search"

    name = ""
    for chunk in ["web_", "search"]:
        name = _dedupe_repeated_tool_name(merge_streamed_tool_call_field(name, chunk))
    assert name == "web_search"


def test_container_mcp_tools_keep_direct_names_when_valid_and_unique():
    assert (
        mcp_model_facing_tool_name(
            container_server_id="container",
            server_id="container",
            tool_name="write",
            existing_names=set(),
        )
        == "write"
    )
    assert (
        mcp_model_facing_tool_name(
            container_server_id="container",
            server_id="container",
            tool_name="bash",
            existing_names=set(),
        )
        == "bash"
    )


def test_non_container_mcp_tools_still_use_aliases():
    name = mcp_model_facing_tool_name(
        container_server_id="container",
        server_id="other-server",
        tool_name="write",
        existing_names=set(),
    )

    assert name.startswith("mcp_")
    assert name.endswith("_write")


def test_container_mcp_direct_name_collision_falls_back_to_alias():
    name = mcp_model_facing_tool_name(
        container_server_id="container",
        server_id="container",
        tool_name="write",
        existing_names={"write"},
    )

    assert name.startswith("mcp_")
    assert name.endswith("_write")
