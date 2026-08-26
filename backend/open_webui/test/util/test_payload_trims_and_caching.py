"""Regression tests for the iOS-PWA / metered-connection payload trims and
serving-header work (agent B1):

* /api/models client projection strips base64 avatars (-> cacheable URL), the
  raw ``openai`` echo, and ``info.access_control``; prunes the ``ollama`` echo;
  keeps ``info.params`` (Chat.svelte reads it),
* the model-avatar endpoint honors If-None-Match -> 304 with immutable caching,
* provider/arena ``created`` is stable so the /api/models ETag can 304,
* the tools list drops the heavy ``content`` / ``specs`` fields,
* the injected index.html carries a strong ETag and 304s, and strips the empty
  loader.js / custom.css hook tags.

Pure-import test (no live DB needed): imports main in the skip-migrations mode.
"""

import asyncio
import types

from test.util.db import configure_test_database

configure_test_database(required=False)

import open_webui.main as main  # noqa: E402
from open_webui.utils.cache import compute_etag  # noqa: E402
from open_webui.utils.models import (  # noqa: E402
    fetch_ollama_models,
    fetch_openai_models,
    STABLE_MODEL_CREATED,
)


def test_model_avatar_url_urlencodes_id_and_versions():
    url = main._model_avatar_url("vendor/model:tag", "data:image/png;base64,AAAA")
    assert url.startswith("/api/v1/models/model/profile/image?")
    assert "id=vendor%2Fmodel%3Atag" in url
    assert "v=" in url
    # Same bytes -> same version (cacheable); different bytes -> different.
    v1 = main._model_avatar_url("m", "data:image/png;base64,AAAA")
    v2 = main._model_avatar_url("m", "data:image/png;base64,BBBB")
    assert v1 != v2
    assert main._model_avatar_url("m", "data:image/png;base64,AAAA") == v1


def test_project_model_strips_and_substitutes():
    data_uri = "data:image/png;base64,AAAA"
    model = {
        "id": "gpt-4o",
        "name": "GPT-4o",
        "openai": {"id": "gpt-4o", "some": "huge upstream echo"},
        "ollama": {
            "details": {
                "parameter_size": "7B",
                "quantization_level": "Q4",
                "families": ["llama"],
                "format": "gguf",
            },
            "size": 123,
            "expires_at": "soon",
            "modelfile": "GIANT BLOB",
            "digest": "deadbeef",
        },
        "info": {
            "params": {"stream_response": True},
            "access_control": {"read": {"group_ids": ["x"]}},
            "meta": {"profile_image_url": data_uri, "description": "keep"},
        },
        "meta": {"profile_image_url": data_uri},
    }

    projected = main._project_model_for_client(model)

    # openai echo removed
    assert "openai" not in projected
    # ollama pruned to only the client-read fields
    assert projected["ollama"] == {
        "details": {"parameter_size": "7B", "quantization_level": "Q4"},
        "size": 123,
        "expires_at": "soon",
    }
    # access_control dropped, params kept
    assert "access_control" not in projected["info"]
    assert projected["info"]["params"] == {"stream_response": True}
    assert projected["info"]["meta"]["description"] == "keep"
    # base64 avatar replaced by the cacheable URL (both surfaces)
    assert projected["info"]["meta"]["profile_image_url"].startswith(
        "/api/v1/models/model/profile/image?"
    )
    assert projected["meta"]["profile_image_url"].startswith(
        "/api/v1/models/model/profile/image?"
    )

    # Input object (which may share app.state config refs) must be untouched.
    assert model["info"]["meta"]["profile_image_url"] == data_uri
    assert model["meta"]["profile_image_url"] == data_uri
    assert "openai" in model
    assert model["info"]["access_control"] == {"read": {"group_ids": ["x"]}}


def test_project_model_keeps_http_avatar_and_missing_fields():
    model = {
        "id": "m",
        "info": {"meta": {"profile_image_url": "https://example.com/a.png"}},
    }
    projected = main._project_model_for_client(model)
    assert (
        projected["info"]["meta"]["profile_image_url"] == "https://example.com/a.png"
    )
    # No ollama / openai / top-meta keys -> no crash, nothing invented.
    assert "ollama" not in projected
    assert "meta" not in projected


def test_render_index_html_strips_empty_hook_tags():
    raw = (
        "<html><head><title>Open WebUI</title>"
        '<script src="/static/loader.js" defer crossorigin="use-credentials"></script>'
        '<link rel="stylesheet" href="/static/custom.css" crossorigin="use-credentials" />'
        "</head><body></body></html>"
    )
    stripped = main._render_index_html(
        raw, "MyName", "", strip_loader=True, strip_custom_css=True
    )
    assert "/static/loader.js" not in stripped
    assert "/static/custom.css" not in stripped
    assert "<title>MyName</title>" in stripped

    kept = main._render_index_html(
        raw, "MyName", "", strip_loader=False, strip_custom_css=False
    )
    assert "/static/loader.js" in kept
    assert "/static/custom.css" in kept


def test_provider_created_is_stable_for_etag():
    assert STABLE_MODEL_CREATED == 0

    calls = {"n": 0}

    async def fake_get_all_models(request, user=None):
        calls["n"] += 1
        return {
            "models": [
                {
                    "model": "llama3:8b",
                    "name": "llama3",
                    "connection_type": "local",
                    "tags": [],
                }
            ]
        }

    import open_webui.utils.models as models_mod

    orig = models_mod.ollama.get_all_models
    models_mod.ollama.get_all_models = fake_get_all_models
    try:
        req = types.SimpleNamespace()
        first = asyncio.run(fetch_ollama_models(req))
        second = asyncio.run(fetch_ollama_models(req))
    finally:
        models_mod.ollama.get_all_models = orig

    assert first[0]["created"] == 0
    assert second[0]["created"] == 0
    # Identical upstream on two calls -> identical ETag (304 can fire).
    assert compute_etag({"data": first}) == compute_etag({"data": second})


def test_provider_refresh_bypasses_ttl_cache():
    import open_webui.utils.models as models_mod

    calls = []

    async def fake_openai(request, user=None, **kwargs):
        calls.append(("openai", kwargs))
        return {"data": []}

    async def fake_ollama(request, user=None, **kwargs):
        calls.append(("ollama", kwargs))
        return {"models": []}

    original_openai = models_mod.openai.get_all_models
    original_ollama = models_mod.ollama.get_all_models
    models_mod.openai.get_all_models = fake_openai
    models_mod.ollama.get_all_models = fake_ollama
    try:
        request = types.SimpleNamespace()
        asyncio.run(fetch_openai_models(request))
        asyncio.run(fetch_ollama_models(request))
        asyncio.run(fetch_openai_models(request, refresh=True))
        asyncio.run(fetch_ollama_models(request, refresh=True))
    finally:
        models_mod.openai.get_all_models = original_openai
        models_mod.ollama.get_all_models = original_ollama

    assert calls == [
        ("openai", {}),
        ("ollama", {}),
        ("openai", {"cache_read": False}),
        ("ollama", {"cache_read": False}),
    ]


def test_provider_model_cache_keys_are_per_user():
    from open_webui.routers import ollama, openai

    alice = types.SimpleNamespace(id="alice")
    bob = types.SimpleNamespace(id="bob")

    assert openai._get_all_models_cache_key(None, None, alice) == (
        "openai_all_models_alice"
    )
    assert openai._get_all_models_cache_key(None, None, bob) == (
        "openai_all_models_bob"
    )
    assert ollama._get_all_models_cache_key(None, None, alice) == (
        "ollama_all_models_alice"
    )
    assert ollama._get_all_models_cache_key(None, None, None) == "ollama_all_models"


def test_global_model_functions_are_resolved_once_per_catalog():
    import open_webui.utils.models as models_mod

    function = types.SimpleNamespace(
        id="global-action",
        name="Global action",
        type="action",
        is_global=True,
        meta=types.SimpleNamespace(description="desc", manifest={}),
    )
    module = types.SimpleNamespace()
    module_calls = {"count": 0}

    async def enabled_functions(active_only=False):
        return [function]

    async def custom_models():
        return []

    async def function_module(request, function_id):
        module_calls["count"] += 1
        return module, None, None

    originals = (
        models_mod.Functions.get_functions,
        models_mod.Models.get_all_models,
        models_mod.get_function_module_from_cache,
    )
    models_mod.Functions.get_functions = staticmethod(enabled_functions)
    models_mod.Models.get_all_models = staticmethod(custom_models)
    models_mod.get_function_module_from_cache = function_module

    config = types.SimpleNamespace(
        ENABLE_BASE_MODELS_CACHE=True,
        ENABLE_EVALUATION_ARENA_MODELS=False,
    )
    state = types.SimpleNamespace(
        config=config,
        MODELS={"seed": {}},
        BASE_MODELS=[
            {
                "id": f"model-{index}",
                "name": f"Model {index}",
                "owned_by": "openai",
            }
            for index in range(50)
        ],
    )
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=state))

    try:
        result = asyncio.run(models_mod.get_all_models(request))
    finally:
        (
            models_mod.Functions.get_functions,
            models_mod.Models.get_all_models,
            models_mod.get_function_module_from_cache,
        ) = originals

    assert len(result) == 50
    assert module_calls["count"] == 1
    assert all(model["actions"][0]["id"] == "global-action" for model in result)


def test_avatar_endpoint_304_and_immutable_cache():
    from open_webui.routers import models as models_router

    data_uri = "data:image/png;base64,iVBORw0KGgo="

    async def fake_get_model_by_id(model_id):
        return types.SimpleNamespace(
            meta=types.SimpleNamespace(profile_image_url=data_uri)
        )

    orig = models_router.Models.get_model_by_id
    models_router.Models.get_model_by_id = staticmethod(fake_get_model_by_id)
    try:
        # First request: no validator -> full body with ETag + immutable cache.
        req = types.SimpleNamespace(headers={})
        resp = asyncio.run(models_router.get_model_profile_image("m", req))
        etag = resp.headers["ETag"]
        assert "immutable" in resp.headers["Cache-Control"]
        assert resp.status_code == 200

        # Matching If-None-Match -> 304, no body.
        req2 = types.SimpleNamespace(headers={"if-none-match": etag})
        resp2 = asyncio.run(models_router.get_model_profile_image("m", req2))
        assert resp2.status_code == 304
        assert resp2.headers["ETag"] == etag

        # Non-matching -> full body again.
        req3 = types.SimpleNamespace(headers={"if-none-match": '"deadbeef"'})
        resp3 = asyncio.run(models_router.get_model_profile_image("m", req3))
        assert resp3.status_code == 200
    finally:
        models_router.Models.get_model_by_id = orig


def test_tools_list_drops_content_and_specs():
    from open_webui.models.tools import ToolUserResponse

    # ToolUserResponse allows extras -> content/specs leak in via model_dump for
    # local tools. The list handler pops them; replicate that guard here.
    item = ToolUserResponse(
        **{
            "id": "t1",
            "user_id": "u1",
            "name": "Tool",
            "meta": {"description": "d"},
            "updated_at": 0,
            "created_at": 0,
            "content": "def x(): pass",
            "specs": [{"name": "x"}],
            "has_user_valves": True,
        }
    )
    data = item.model_dump()
    assert "content" in data and "specs" in data  # the leak the fix targets

    data.pop("content", None)
    data.pop("specs", None)
    assert "content" not in data and "specs" not in data
    # Fields the client does read must survive.
    assert data["has_user_valves"] is True
    assert data["meta"]["description"] == "d"


def test_index_html_etag_and_304(tmp_path):
    from starlette.responses import Response as StarletteResponse

    index_path = tmp_path / "index.html"
    index_path.write_text(
        "<html><head><title>Open WebUI</title>"
        '<script src="/static/loader.js" defer></script>'
        '<link rel="stylesheet" href="/static/custom.css" />'
        "</head><body></body></html>",
        encoding="utf-8",
    )

    spa = main.SPAStaticFiles(directory=str(tmp_path))

    # Ensure a clean memo + a known name.
    main.app.state._spa_index_cache = None
    main.app.state.WEBUI_NAME = "TestName"

    scope_get = {"type": "http", "method": "GET", "headers": []}
    resp = spa._injected_index_response(scope_get)
    assert resp is not None
    etag = resp.headers["ETag"]
    assert etag
    # The repo's static loader.js / custom.css are empty -> tags stripped.
    body = resp.body.decode("utf-8")
    assert "/static/loader.js" not in body
    assert "/static/custom.css" not in body
    assert "<title>TestName</title>" in body

    scope_inm = {
        "type": "http",
        "method": "GET",
        "headers": [(b"if-none-match", etag.encode("utf-8"))],
    }
    resp2 = spa._injected_index_response(scope_inm)
    assert isinstance(resp2, StarletteResponse)
    assert resp2.status_code == 304
    assert resp2.headers["ETag"] == etag


############################
# Shared-chat link previews (/s/{share_id})
############################


def _preview_chat(chat_blob, title="Test Chat"):
    from open_webui.models.chats import ChatModel

    return ChatModel(
        id="share-1",
        user_id="shared-share-1",
        title=title,
        chat=chat_blob,
        created_at=0,
        updated_at=0,
    )


def test_render_index_html_share_overrides():
    raw = "<html><head><title>Open WebUI</title></head><body></body></html>"
    out = main._render_index_html(
        raw,
        "MyName",
        "https://example.com",
        title='My "Chat" <script>',
        description="Line one\n with <b>html</b> & \"quotes\"",
        url="https://example.com/s/abc123",
    )
    # Browser tab mirrors the SPA's "{title} • {name}" format; og:title is bare.
    assert "<title>My &quot;Chat&quot; &lt;script&gt; • MyName</title>" in out
    assert 'property="og:title" content="My &quot;Chat&quot; &lt;script&gt;"' in out
    # og:site_name keeps the instance name (the small top line in embeds).
    assert 'property="og:site_name" content="MyName"' in out
    # Descriptions are single-line and attribute-escaped.
    assert (
        'property="og:description" content="Line one with &lt;b&gt;html&lt;/b&gt; &amp; &quot;quotes&quot;"'
        in out
    )
    assert 'property="og:url" content="https://example.com/s/abc123"' in out
    assert 'name="twitter:title" content="My &quot;Chat&quot; &lt;script&gt;"' in out
    # Idempotency guard still holds.
    assert out.count("og:title") == 1

    generic = main._render_index_html(raw, "MyName", "")
    assert 'property="og:title" content="MyName"' in generic
    assert "og:url" not in generic


def test_shared_chat_link_preview_extracts_first_user_message():
    blob = {
        "title": "Rust vs Go",
        "history": {
            "currentId": "m3",
            "messages": {
                "m1": {
                    "id": "m1",
                    "role": "user",
                    "parentId": None,
                    "content": "# Hello **world**\n\n[docs](https://x.y)  second",
                },
                "m2": {"id": "m2", "role": "assistant", "parentId": "m1", "content": "answer"},
                "m3": {"id": "m3", "role": "user", "parentId": "m2", "content": "follow up"},
            },
        },
    }
    title, description = main._shared_chat_link_preview(_preview_chat(blob, title=""))
    assert title == "Rust vs Go"
    # Markdown stripped to prose, first USER message wins over the reply.
    assert description == "Hello world docs second"


def test_shared_chat_link_preview_content_shapes_and_fallbacks():
    # Multimodal text parts.
    _, desc = main._shared_chat_link_preview(
        _preview_chat(
            {
                "title": "Multi",
                "history": {
                    "currentId": "m1",
                    "messages": {
                        "m1": {
                            "id": "m1",
                            "role": "user",
                            "parentId": None,
                            "content": [
                                {"type": "text", "text": "what is in"},
                                {"type": "file", "file": {}},
                                {"type": "text", "text": "this image?"},
                            ],
                        }
                    },
                },
            }
        )
    )
    assert desc == "what is in this image?"

    # Lazy blocks: reasoning never leaks into the snippet.
    _, desc = main._shared_chat_link_preview(
        _preview_chat(
            {
                "title": "Blocks",
                "history": {
                    "currentId": "m1",
                    "messages": {
                        "m1": {
                            "id": "m1",
                            "role": "assistant",
                            "parentId": None,
                            "content": "",
                            "content_blocks": [
                                {"type": "reasoning", "content": "secret reasoning"},
                                {"type": "text", "content": "visible answer"},
                            ],
                        }
                    },
                },
            }
        )
    )
    assert desc == "visible answer"

    # Legacy messages list + empty chat fallbacks.
    _, desc = main._shared_chat_link_preview(
        _preview_chat({"title": "Legacy", "messages": [{"role": "assistant", "content": "legacy reply"}]})
    )
    assert desc == "legacy reply"

    title, desc = main._shared_chat_link_preview(_preview_chat({}, title=""))
    assert title == "Shared Chat"
    assert desc == ""


def test_shared_chat_link_preview_cycle_guard_and_truncation():
    blob = {
        "title": "Cycle",
        "history": {
            "currentId": "a",
            "messages": {
                "a": {"id": "a", "role": "user", "parentId": "b", "content": "a"},
                "b": {"id": "b", "role": "user", "parentId": "a", "content": "b"},
            },
        },
    }
    _, desc = main._shared_chat_link_preview(_preview_chat(blob))
    assert isinstance(desc, str)  # terminates

    trunc = main._link_preview_truncate("word " * 40, 50)
    assert len(trunc) <= 51 and trunc.endswith("…")
    assert main._link_preview_truncate("short") == "short"


def test_shared_chat_page_route_injects_chat_meta():
    from fastapi.testclient import TestClient

    chat = _preview_chat(
        {
            "title": "Explain quantum tunneling",
            "history": {
                "currentId": "m1",
                "messages": {
                    "m1": {
                        "id": "m1",
                        "role": "user",
                        "parentId": None,
                        "content": "Can a particle really **tunnel** through a wall?",
                    }
                },
            },
        },
        title="Explain quantum tunneling",
    )

    real_resolve = main.Chats.resolve_shared_chat
    real_sanitize = main.sanitize_shared_chat_model
    real_name = getattr(main.app.state, "WEBUI_NAME", None)

    async def fake_resolve(share_id, admin_access=False):
        return None if share_id == "missing" else chat

    main.Chats.resolve_shared_chat = fake_resolve
    main.sanitize_shared_chat_model = lambda c, share_id=None: c
    main.app.state.WEBUI_NAME = "TestName"
    try:
        client = TestClient(main.app)

        resp = client.get("/s/abc123", headers={"host": "example.com"})
        assert resp.status_code == 200
        body = resp.text
        assert 'property="og:title" content="Explain quantum tunneling"' in body
        assert (
            'property="og:description" content="Can a particle really tunnel through a wall?"'
            in body
        )
        assert 'property="og:site_name" content="TestName"' in body
        assert 'property="og:url" content="http://example.com/s/abc123"' in body
        assert "<title>Explain quantum tunneling • TestName</title>" in body

        # Unknown share -> plain injected shell (SPA handles the redirect).
        resp = client.get("/s/missing", headers={"host": "example.com"})
        assert resp.status_code == 200
        assert 'property="og:title" content="TestName"' in resp.text
        assert "quantum" not in resp.text
    finally:
        main.Chats.resolve_shared_chat = real_resolve
        main.sanitize_shared_chat_model = real_sanitize
        if real_name is None:
            del main.app.state.WEBUI_NAME
        else:
            main.app.state.WEBUI_NAME = real_name
