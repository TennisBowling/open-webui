"""Unit tests for Contract 1 (per-component bootstrap revalidation) and the
models-component unwrap (Contract-adjacent item 2).

Contract 1: the client sends an ``x-bootstrap-etags`` header — a compact JSON
object mapping component name -> the etag it already has cached. The server
omits components whose etag still matches (listing them under ``unchanged``) and
returns everything else in ``components``. An absent/unparseable header, or a map
that matches nothing, must yield EXACTLY today's full body (no ``unchanged`` key)
so old/new clients stay backward/forward safe.

Pure request-shaping logic — no DB, sockets, or Postgres required.
"""

from test.util.db import configure_test_database

configure_test_database(required=False)

from open_webui.routers.bootstrap import (  # noqa: E402
    _build_bootstrap_body,
    _parse_bootstrap_etags,
    _etag,
    _bundle_etag,
)


def _fixture():
    components = {"config": {"a": 1}, "user": {"id": "u1"}, "models": [{"id": "m"}]}
    etags = {name: _etag(data) for name, data in components.items()}
    bundle = _bundle_etag(etags)
    return components, etags, bundle


# --- _parse_bootstrap_etags -------------------------------------------------


def test_parse_absent_header_is_empty():
    assert _parse_bootstrap_etags(None) == {}
    assert _parse_bootstrap_etags("") == {}


def test_parse_unparseable_is_empty():
    assert _parse_bootstrap_etags("{not json") == {}
    assert _parse_bootstrap_etags("[1,2,3]") == {}  # not a dict
    assert _parse_bootstrap_etags("42") == {}


def test_parse_filters_non_string_values():
    assert _parse_bootstrap_etags('{"config":"abc","user":5,"x":null}') == {
        "config": "abc"
    }


# --- _build_bootstrap_body: full-body / backward-safe -----------------------


def test_absent_header_yields_full_body_no_unchanged():
    components, etags, bundle = _fixture()
    body = _build_bootstrap_body(components, etags, bundle, {})
    assert body["components"] == components
    assert body["components_etags"] == etags
    assert body["bundle_etag"] == bundle
    assert "unchanged" not in body


def test_map_matching_nothing_yields_full_body_no_unchanged():
    components, etags, bundle = _fixture()
    # client etags are all stale
    stale = {name: "deadbeef" for name in etags}
    body = _build_bootstrap_body(components, etags, bundle, stale)
    assert body["components"] == components
    assert "unchanged" not in body


# --- _build_bootstrap_body: per-component omission --------------------------


def test_matching_components_are_omitted_and_listed_unchanged():
    components, etags, bundle = _fixture()
    # client has fresh config + user, stale models
    client = {"config": etags["config"], "user": etags["user"], "models": "stale"}
    body = _build_bootstrap_body(components, etags, bundle, client)

    assert set(body["unchanged"]) == {"config", "user"}
    # omitted from the body
    assert "config" not in body["components"]
    assert "user" not in body["components"]
    # models (stale) still shipped
    assert body["components"]["models"] == components["models"]
    # etags map + bundle always complete
    assert body["components_etags"] == etags
    assert body["bundle_etag"] == bundle


def test_all_matching_ships_empty_components_but_still_valid():
    components, etags, bundle = _fixture()
    body = _build_bootstrap_body(components, etags, bundle, dict(etags))
    assert body["components"] == {}
    assert set(body["unchanged"]) == set(etags)
    assert body["components_etags"] == etags


def test_client_lists_component_server_did_not_return_is_ignored():
    components, etags, bundle = _fixture()
    client = dict(etags)
    client["folders"] = "whatever"  # not in this bundle
    body = _build_bootstrap_body(components, etags, bundle, client)
    # folders isn't a resolved component, so it can't be "unchanged"
    assert "folders" not in body["unchanged"]
    assert set(body["unchanged"]) == set(etags)
