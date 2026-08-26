from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import Response

from open_webui.routers import bootstrap
from open_webui.utils.auth import create_token


@pytest.mark.asyncio
async def test_resolve_user_includes_effective_permissions(monkeypatch):
    fresh_user = SimpleNamespace(
        id="user-1",
        email="user@example.com",
        name="Test User",
        role="user",
        profile_image_url="/user.png",
    )
    default_permissions = {
        "chat": {"file_upload": True},
        "features": {"web_search": True, "subagents": True},
    }
    effective_permissions = {
        "chat": {"file_upload": True},
        "features": {"web_search": True, "subagents": True},
    }
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(USER_PERMISSIONS=default_permissions)
            )
        )
    )

    monkeypatch.setattr(bootstrap.Users, "get_user_by_id", lambda user_id: fresh_user)

    calls = []

    def fake_get_permissions(user_id, permissions):
        calls.append((user_id, permissions))
        return effective_permissions

    monkeypatch.setattr(bootstrap, "get_permissions", fake_get_permissions)

    result = await bootstrap._resolve_user(request, SimpleNamespace(id="user-1"))

    assert result == {
        "id": "user-1",
        "email": "user@example.com",
        "name": "Test User",
        "role": "user",
        "profile_image_url": "/user.png",
        "permissions": effective_permissions,
    }
    assert calls == [("user-1", default_permissions)]


class _FakeRequest:
    """Minimal stand-in exposing the .headers/.cookies surface
    _renew_token_cookie reads from a real Starlette Request."""

    def __init__(self, headers=None, cookies=None):
        self.headers = headers or {}
        self.cookies = cookies or {}


def test_renew_token_cookie_sets_cookie_from_bearer_header():
    # Covers both the 200 and 304 bootstrap response paths, which both call
    # _renew_token_cookie(request, response) directly before returning —
    # this is the shared code path, so testing it once here covers both.
    token = create_token({"id": "user-1"}, expires_delta=timedelta(minutes=30))
    request = _FakeRequest(headers={"Authorization": f"Bearer {token}"})
    response = Response()

    bootstrap._renew_token_cookie(request, response)

    set_cookie_headers = response.headers.getlist("set-cookie")
    assert any(h.startswith("token=") for h in set_cookie_headers)


def test_renew_token_cookie_falls_back_to_existing_cookie():
    token = create_token({"id": "user-1"}, expires_delta=timedelta(minutes=30))
    request = _FakeRequest(cookies={"token": token})
    response = Response()

    bootstrap._renew_token_cookie(request, response)

    set_cookie_headers = response.headers.getlist("set-cookie")
    assert any(h.startswith("token=") for h in set_cookie_headers)


def test_renew_token_cookie_skips_when_no_token():
    request = _FakeRequest()
    response = Response()

    bootstrap._renew_token_cookie(request, response)

    assert response.headers.getlist("set-cookie") == []


def test_renew_token_cookie_skips_when_expired():
    token = create_token({"id": "user-1"}, expires_delta=timedelta(minutes=-30))
    request = _FakeRequest(headers={"Authorization": f"Bearer {token}"})
    response = Response()

    bootstrap._renew_token_cookie(request, response)

    assert response.headers.getlist("set-cookie") == []
