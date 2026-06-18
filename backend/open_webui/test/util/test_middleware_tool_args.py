from open_webui.utils.tool_calling import (
    mcp_model_facing_tool_name,
    parse_tool_call_arguments,
)


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
