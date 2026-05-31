import pytest

from open_webui.utils.mcp.oauth import (
    MCPOAuthError,
    parse_www_authenticate,
    protected_resource_urls,
    validate_public_url,
)


def test_parse_www_authenticate_resource_metadata():
    header = (
        'Bearer realm="OAuth", '
        'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp", '
        'error="invalid_token"'
    )

    parsed = parse_www_authenticate(header)

    assert parsed["realm"] == "OAuth"
    assert parsed["resource_metadata"] == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
    assert parsed["error"] == "invalid_token"


def test_protected_resource_urls_path_first_then_root():
    assert protected_resource_urls("https://mcp.notion.com/mcp") == [
        "https://mcp.notion.com/.well-known/oauth-protected-resource/mcp",
        "https://mcp.notion.com/.well-known/oauth-protected-resource",
    ]


def test_validate_public_url_blocks_private_hosts():
    with pytest.raises(MCPOAuthError):
        validate_public_url("http://169.254.169.254/latest/meta-data")


def test_validate_public_url_allows_https_public():
    validate_public_url("https://mcp.notion.com/mcp")
