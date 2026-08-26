"""
Default prompt scaffolding for the Data Visualization feature.

These constants are intentionally empty on a fresh install. The admin pastes the
real prompt text into the admin UI (Admin Settings > Data Visualization), and
PersistentConfig writes those values to the DB. The constants here are just
defaults the config falls back to before any admin override exists.

LAZY-LOADING: only the shared-core prompt is injected into the system prompt on
every request. The per-use-case module guides (diagram/chart/art) are NOT
concatenated — the model fetches the one it needs on demand by calling the
`load_viz_guide(use_case)` tool. `assemble_data_viz_system_prompt` appends a
small router footer listing the currently-available guides.
"""

from typing import Optional

SHARED_CORE: str = ""
MODULE_DIAGRAM: str = ""
MODULE_MOCKUP_INTERACTIVE: str = ""
MODULE_CHART_DATAVIZ: str = ""
MODULE_ART: str = ""


# Loadable use-case guides. Each entry: (canonical use_case key, config prefix,
# one-line description shown in the router footer). The model passes a use_case
# string to load_viz_guide; aliases below map common variants to a canonical key.
VIZ_GUIDES: tuple[tuple[str, str, str], ...] = (
    ("diagram", "DATA_VIZ_MODULE_DIAGRAM", "SVG flowcharts, structural & illustrative diagrams"),
    ("mockup", "DATA_VIZ_MODULE_MOCKUP_INTERACTIVE", "UI mockups, cards, dashboards, interactive explainers"),
    ("chart", "DATA_VIZ_MODULE_CHART_DATAVIZ", "Chart.js charts, geographic maps (D3 choropleth)"),
    ("art", "DATA_VIZ_MODULE_ART", "illustration & generative art"),
)

# Map the strings the model might pass -> canonical use_case key.
_VIZ_GUIDE_ALIASES: dict[str, str] = {
    "diagram": "diagram", "diagrams": "diagram", "flowchart": "diagram",
    "structural": "diagram", "illustrative": "diagram", "erd": "diagram",
    "mockup": "mockup", "mockups": "mockup", "interactive": "mockup",
    "ui": "mockup", "form": "mockup", "forms": "mockup", "dashboard": "mockup",
    "card": "mockup", "cards": "mockup",
    "chart": "chart", "charts": "chart", "graph": "chart", "plot": "chart",
    "map": "chart", "maps": "chart", "choropleth": "chart",
    "art": "art", "illustration": "art", "generative": "art",
}


def _viz_guide_prefix(use_case: str) -> Optional[str]:
    """Resolve a (possibly aliased) use_case string to its config prefix."""
    key = _VIZ_GUIDE_ALIASES.get((use_case or "").strip().lower())
    if not key:
        return None
    for canonical, prefix, _desc in VIZ_GUIDES:
        if canonical == key:
            return prefix
    return None


def available_viz_guides(config) -> list[tuple[str, str]]:
    """(use_case_key, description) for guides that are enabled AND non-empty.

    Mockup ships empty (its patterns live in the shared core), so it is naturally
    excluded — a separate guide only appears when it has real content.
    """
    out: list[tuple[str, str]] = []
    for key, prefix, desc in VIZ_GUIDES:
        enabled = getattr(config, f"{prefix}_ENABLED", False)
        prompt = (getattr(config, f"{prefix}_PROMPT", "") or "").strip()
        if enabled and prompt:
            out.append((key, desc))
    return out


def get_viz_guide(config, use_case: str) -> str:
    """Resolve a use_case to its guide text (the module prompt).

    Never raises — for an unknown, disabled, or empty use_case it returns a short
    actionable note (so the model gets useful feedback as the tool result and can
    fall back to the core guidance it already has)."""
    raw = (use_case or "").strip()
    avail = available_viz_guides(config)
    menu = ", ".join(f"'{k}'" for k, _ in avail) or "(none configured)"
    prefix = _viz_guide_prefix(raw)
    if not prefix:
        return (
            f"No guide named '{raw}'. Available guides: {menu}. "
            "Mockups, cards, dashboards, and forms use the core guidance you already have."
        )
    enabled = getattr(config, f"{prefix}_ENABLED", False)
    text = (getattr(config, f"{prefix}_PROMPT", "") or "").strip()
    if not enabled or not text:
        return (
            f"No separate '{raw}' guide is configured — use the core guidance you "
            f"already have. Available guides: {menu}."
        )
    return text


# Injected only when the feature is enabled but every admin-editable prompt is
# blank (e.g. a fresh install). Gives the model the minimum it needs to use the
# show_widget tool correctly instead of guessing. Mirrors the tool's docstring
# contract (sandboxed iframe, HTML fragment OR raw SVG, no full document).
MINIMAL_FALLBACK_PROMPT: str = (
    "You can render inline visualizations with the `show_widget` tool. Pass a "
    "snake_case `title` and `widget_code` that is EITHER a raw HTML fragment "
    "OR a raw SVG string (starting with `<svg`). Do NOT include <!DOCTYPE>, "
    "<html>, <head>, or <body> tags — the fragment is mounted inside a "
    "sandboxed iframe. You may load scripts only from cdnjs.cloudflare.com, "
    "cdn.jsdelivr.net, esm.sh, or unpkg.com. No localStorage/sessionStorage and "
    "no `position: fixed`. For detailed patterns, call "
    "`load_viz_guide('diagram'|'chart'|'art')` before building that widget."
)


def assemble_data_viz_system_prompt(config) -> str:
    """
    Assemble the data-visualization system prompt for lazy-loading.

    Injects ONLY the shared-core prompt (the always-applicable foundation), plus
    a small router footer listing the use-case guides the model can fetch on
    demand via `load_viz_guide`. The per-use-case module prompts are NOT
    concatenated here — that is the whole point of lazy-loading. Returns "" when
    the shared core is empty so the caller can fall back to MINIMAL_FALLBACK_PROMPT.
    """
    shared_core = (getattr(config, "DATA_VIZ_SHARED_CORE_PROMPT", "") or "").strip()
    if not shared_core:
        return ""

    guides = available_viz_guides(config)
    if not guides:
        return shared_core

    lines = "\n".join(f"- `{key}` — {desc}" for key, desc in guides)
    footer = (
        "\n\n## Use-case guides — load before building\n"
        "The guidance above applies to EVERY widget. For use-case-specific "
        "patterns, call `load_viz_guide(use_case)` (use_case is one of the keys "
        "below) BEFORE you build that widget:\n"
        f"{lines}\n"
        "Mockups, cards, dashboards, interactive explainers, and forms use the "
        "core guidance above — no guide needed."
    )
    return shared_core + footer


def assemble_full_data_viz_prompt(config) -> str:
    """Shared core + EVERY enabled module guide concatenated, with NO lazy-load
    footer. Used where the model cannot fetch guides on demand — specifically the
    auto-repair model call (a single non-tool completion that benefits from the
    full design context and must not be told to call a tool it doesn't have).
    Returns "" when nothing is configured.
    """
    parts: list[str] = []
    shared_core = (getattr(config, "DATA_VIZ_SHARED_CORE_PROMPT", "") or "").strip()
    if shared_core:
        parts.append(shared_core)
    for _key, prefix, _desc in VIZ_GUIDES:
        enabled = getattr(config, f"{prefix}_ENABLED", False)
        prompt = (getattr(config, f"{prefix}_PROMPT", "") or "").strip()
        if enabled and prompt:
            parts.append(prompt)
    return "\n\n".join(parts)
