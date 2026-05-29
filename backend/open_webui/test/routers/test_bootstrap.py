from types import SimpleNamespace

import pytest

from open_webui.routers import bootstrap


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
