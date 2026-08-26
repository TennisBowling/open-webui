"""Tests for data-viz lazy-loading (load_viz_guide tool + assemble footer).

The per-use-case module guides are no longer concatenated into the system prompt;
the model fetches one on demand via load_viz_guide. These tests lock that in:
assemble injects ONLY shared-core + a router footer (no module bodies), the guide
resolver handles valid/alias/disabled/empty/unknown, the tool method is safe, and
both show_widget AND load_viz_guide are registered as builtin:data_viz tools.

``data_viz_tool`` imports ``utils.chat`` (binds the DB engine at import), so
DATABASE_URL is pointed at a dummy first — same pattern as the other util tests.
"""

import asyncio
import os

from test.util.db import configure_test_database

configure_test_database()
os.environ.pop("WEBSOCKET_REDIS_URL", None)

import open_webui.utils.data_viz_prompts as dvp  # noqa: E402
import open_webui.utils.data_viz_tool as dvt  # noqa: E402
from open_webui.utils.tools import get_data_viz_tool_specs  # noqa: E402


class _Cfg:
    """Mock PersistentConfig namespace."""

    def __init__(self, core="CORE", **modules):
        self.DATA_VIZ_SHARED_CORE_PROMPT = core
        # default: all 4 modules enabled with body text
        defaults = {
            "DIAGRAM": ("GUIDE_DIAGRAM", True),
            "MOCKUP_INTERACTIVE": ("", True),  # ships empty by design
            "CHART_DATAVIZ": ("GUIDE_CHART", True),
            "ART": ("GUIDE_ART", True),
        }
        defaults.update(modules)
        for prefix, (body, enabled) in defaults.items():
            setattr(self, f"DATA_VIZ_MODULE_{prefix}_PROMPT", body)
            setattr(self, f"DATA_VIZ_MODULE_{prefix}_ENABLED", enabled)


def _run(coro):
    return asyncio.run(coro)


# ── assemble ───────────────────────────────────────────────────────────────
def test_assemble_injects_core_and_footer_not_module_bodies():
    out = dvp.assemble_data_viz_system_prompt(_Cfg())
    assert "CORE" in out
    # The whole point: module BODIES must NOT be in the system prompt.
    assert "GUIDE_DIAGRAM" not in out
    assert "GUIDE_CHART" not in out
    assert "GUIDE_ART" not in out
    # Footer is the router.
    assert "load_viz_guide" in out
    assert "`diagram`" in out and "`chart`" in out and "`art`" in out


def test_assemble_footer_excludes_disabled_and_empty_guides():
    cfg = _Cfg(ART=("GUIDE_ART", False), DIAGRAM=("", True))  # art disabled, diagram empty
    out = dvp.assemble_data_viz_system_prompt(cfg)
    assert "`chart`" in out
    assert "`art`" not in out  # disabled
    assert "`diagram`" not in out  # empty
    assert "`mockup`" not in out  # ships empty


def test_assemble_empty_core_returns_empty():
    assert dvp.assemble_data_viz_system_prompt(_Cfg(core="")) == ""


def test_assemble_core_only_when_no_guides_available():
    cfg = _Cfg(
        DIAGRAM=("", True), MOCKUP_INTERACTIVE=("", True),
        CHART_DATAVIZ=("", True), ART=("", True),
    )
    out = dvp.assemble_data_viz_system_prompt(cfg)
    assert out == "CORE"  # no footer when nothing is loadable


def test_full_assembly_for_repair_includes_module_bodies_and_no_footer():
    # The repair model has no tools, so it gets the OLD-style full concat
    # (core + every enabled module body) and must NOT see the load_viz_guide
    # footer (it can't call a tool).
    out = dvp.assemble_full_data_viz_prompt(_Cfg())
    assert "CORE" in out
    assert "GUIDE_DIAGRAM" in out and "GUIDE_CHART" in out and "GUIDE_ART" in out
    assert "load_viz_guide" not in out
    # disabled/empty modules excluded
    cfg = _Cfg(ART=("GUIDE_ART", False))
    assert "GUIDE_ART" not in dvp.assemble_full_data_viz_prompt(cfg)
    assert dvp.assemble_full_data_viz_prompt(_Cfg(core="")) == "\n\n".join(
        ["GUIDE_DIAGRAM", "GUIDE_CHART", "GUIDE_ART"]
    )


# ── available_viz_guides ───────────────────────────────────────────────────
def test_available_viz_guides_reflects_enabled_and_nonempty():
    avail = dict(dvp.available_viz_guides(_Cfg()))
    assert set(avail) == {"diagram", "chart", "art"}  # mockup empty -> excluded
    cfg = _Cfg(CHART_DATAVIZ=("GUIDE_CHART", False))
    assert "chart" not in dict(dvp.available_viz_guides(cfg))


# ── get_viz_guide ──────────────────────────────────────────────────────────
def test_get_viz_guide_valid_returns_body():
    assert dvp.get_viz_guide(_Cfg(), "diagram") == "GUIDE_DIAGRAM"
    assert dvp.get_viz_guide(_Cfg(), "chart") == "GUIDE_CHART"


def test_get_viz_guide_aliases_and_case_insensitive():
    assert dvp.get_viz_guide(_Cfg(), "Flowchart") == "GUIDE_DIAGRAM"
    assert dvp.get_viz_guide(_Cfg(), "ERD") == "GUIDE_DIAGRAM"
    assert dvp.get_viz_guide(_Cfg(), "  MAPS ") == "GUIDE_CHART"
    assert dvp.get_viz_guide(_Cfg(), "illustration") == "GUIDE_ART"


def test_get_viz_guide_disabled_empty_unknown_are_notes_not_raises():
    cfg = _Cfg(ART=("GUIDE_ART", False))
    note = dvp.get_viz_guide(cfg, "art")
    assert "GUIDE_ART" not in note and "core guidance" in note
    # mockup ships empty
    note2 = dvp.get_viz_guide(_Cfg(), "mockup")
    assert "core guidance" in note2
    # unknown
    note3 = dvp.get_viz_guide(_Cfg(), "wat")
    assert "No guide named 'wat'" in note3
    # None / empty
    assert isinstance(dvp.get_viz_guide(_Cfg(), None), str)
    assert isinstance(dvp.get_viz_guide(_Cfg(), ""), str)


# ── load_viz_guide tool method ─────────────────────────────────────────────
def _fake_request(cfg):
    class _S:
        pass

    s = _S()
    s.config = cfg

    class _A:
        pass

    a = _A()
    a.state = s

    class _R:
        pass

    r = _R()
    r.app = a
    return r


def test_load_viz_guide_tool_returns_guide():
    tool = dvt.DataVizTools()
    out = _run(tool.load_viz_guide("diagram", __request__=_fake_request(_Cfg())))
    assert out == "GUIDE_DIAGRAM"


def test_load_viz_guide_tool_unknown_and_no_request_are_safe():
    tool = dvt.DataVizTools()
    out = _run(tool.load_viz_guide("nope", __request__=_fake_request(_Cfg())))
    assert "No guide named 'nope'" in out
    out2 = _run(tool.load_viz_guide("diagram", __request__=None))
    assert "core guidance" in out2  # graceful, no crash


# ── tool spec registration ─────────────────────────────────────────────────
def test_both_tools_registered_and_magic_params_stripped():
    tools = get_data_viz_tool_specs({})
    assert "show_widget" in tools
    assert "load_viz_guide" in tools
    assert tools["load_viz_guide"]["id"] == "builtin:data_viz"
    assert callable(tools["load_viz_guide"]["callable"])
    # the model-facing schema must expose use_case and NOT the __request__ magic
    spec = tools["load_viz_guide"]["spec"]
    fn = spec.get("function", spec)
    props = (fn.get("parameters", {}) or {}).get("properties", {}) or {}
    assert "use_case" in props
    assert not any(k.startswith("__") for k in props)
