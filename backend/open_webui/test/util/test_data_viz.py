"""Regression tests for the data-visualization show_widget tool.

Covers the hardening fixes:
  - ``_override_key`` is FNV-1a 64-bit, byte-identical to the frontend, and
    hashes the NUL-stripped code (so backend pre-persist == frontend post-persist).
  - ``show_widget`` only enters the auto-repair loop on a GENUINE frontend
    ``{"status": "error"}`` response. The non-interactive event callers return
    ``{"status": False, ...}`` (headless drain → ``{"headless": True}``;
    broadcast-incapable ``get_event_call`` → ``{"error": ...}``). Those must fail
    soft as "no client confirmation" and NEVER trigger a (previously crashing)
    repair loop.
  - A real render error with auto-repair enabled repairs once and persists the
    fix via a ``data_viz:override`` event keyed by the ORIGINAL code's key.

``data_viz_tool`` imports ``utils.chat`` (which binds the DB engine at import),
so DATABASE_URL is pointed at a throwaway copy of the migrated dev DB first —
same pattern as the other util tests.
"""

import asyncio
import os

from test.util.db import configure_test_database

configure_test_database()
os.environ.pop("WEBSOCKET_REDIS_URL", None)

import open_webui.utils.data_viz_tool as dvt  # noqa: E402


def _make_request(auto_repair=True, max_attempts=3):
    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.DATA_VIZ_AUTO_REPAIR_ENABLED = auto_repair
    cfg.DATA_VIZ_AUTO_REPAIR_MAX_ATTEMPTS = max_attempts

    class _State:
        pass

    state = _State()
    state.config = cfg
    state.MODELS = {}

    class _App:
        pass

    app = _App()
    app.state = state

    class _ReqState:
        pass

    req_state = _ReqState()
    req_state.metadata = {}

    class _Req:
        pass

    req = _Req()
    req.app = app
    req.state = req_state
    return req


def _run(coro):
    return asyncio.run(coro)


def test_override_key_parity_and_nul_strip():
    # Byte-identical to the frontend fnv1a16 (verified cross-language).
    assert dvt._override_key('<svg width="10">x</svg>') == "54e6a56683300c86"
    assert dvt._override_key("") == "cbf29ce484222325"
    # NUL is stripped before hashing so backend (pre-persist) and frontend
    # (post-persist, NUL already stripped by the DB layer) agree.
    assert dvt._override_key("a\x00b") == dvt._override_key("ab")


def test_show_widget_no_event_call_returns_rendered():
    tool = dvt.DataVizTools()
    out = _run(
        tool.show_widget(
            title="t",
            widget_code="<div>x</div>",
            __event_call__=None,
            __request__=_make_request(),
        )
    )
    assert out == "Widget 't' rendered."


def test_show_widget_ok_first_try():
    tool = dvt.DataVizTools()

    async def ec(payload):
        return {"status": "ok"}

    out = _run(
        tool.show_widget(
            title="t",
            widget_code="<div>x</div>",
            __event_call__=ec,
            __request__=_make_request(),
        )
    )
    assert out == "Widget 't' rendered."


def test_show_widget_headless_status_false_does_not_repair(monkeypatch):
    """C4/C7: {"status": False, ...} is a non-frontend caller, not a render
    error. Must fail soft and never invoke the repair model."""
    called = {"repair": 0}

    async def fake_repair(**kwargs):
        called["repair"] += 1
        return {"widget_code": "<div>fixed</div>", "summary": "x"}

    monkeypatch.setattr(dvt, "call_repair_model", fake_repair)
    tool = dvt.DataVizTools()

    async def ec_headless(payload):
        return {"status": False, "headless": True}

    out = _run(
        tool.show_widget(
            title="t",
            widget_code="<div>x</div>",
            __event_call__=ec_headless,
            __request__=_make_request(),
        )
    )
    assert out == "Widget 't' rendered (no client confirmation)."

    async def ec_broadcast(payload):
        return {"status": False, "error": "Cannot use call() to broadcast."}

    out2 = _run(
        tool.show_widget(
            title="t",
            widget_code="<div>x</div>",
            __event_call__=ec_broadcast,
            __request__=_make_request(),
        )
    )
    assert out2 == "Widget 't' rendered (no client confirmation)."
    assert called["repair"] == 0


def test_show_widget_error_triggers_repair_and_persists_override(monkeypatch):
    emitted = []

    async def emitter(event):
        emitted.append(event)

    async def fake_repair(**kwargs):
        return {"widget_code": "<div>fixed</div>", "summary": "fixed the bug"}

    monkeypatch.setattr(dvt, "call_repair_model", fake_repair)
    tool = dvt.DataVizTools()

    responses = [
        {"status": "error", "error_message": "boom"},
        {"status": "ok"},
    ]

    async def ec(payload):
        return responses.pop(0)

    out = _run(
        tool.show_widget(
            title="t",
            widget_code="<div>x</div>",
            __event_call__=ec,
            __event_emitter__=emitter,
            __request__=_make_request(),
        )
    )
    assert "auto-fixed" in out.lower()
    overrides = [e for e in emitted if e.get("type") == "data_viz:override"]
    assert len(overrides) == 1
    # Keyed by the ORIGINAL code (so the frontend can look it up on reload),
    # carrying the repaired code.
    assert overrides[0]["data"]["key"] == dvt._override_key("<div>x</div>")
    assert overrides[0]["data"]["widget_code"] == "<div>fixed</div>"


def test_show_widget_error_repair_disabled_returns_raw_error():
    tool = dvt.DataVizTools()

    async def ec(payload):
        return {"status": "error", "error_message": "boom boom"}

    out = _run(
        tool.show_widget(
            title="t",
            widget_code="<div>x</div>",
            __event_call__=ec,
            __request__=_make_request(auto_repair=False),
        )
    )
    assert out.startswith("ERROR: widget 't' threw boom boom")
