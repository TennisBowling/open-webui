"""
Usage tracking for subscription-based providers.

Some OpenAI-compatible connections front a *subscription* (e.g. a ChatGPT plan
exposed through a Codex-style proxy) instead of pay-per-token billing. Those
providers report percentage-based rate-limit windows from a usage endpoint —
there is no meaningful token limit to count against, so they can't ride the
token-group system. This module owns that state:

- A connection opts in via the per-connection config flag `subscription_usage`
  in OPENAI_API_CONFIGS (plus optional `usage_url`; defaults to
  `<scheme>://<host>/usage` derived from the connection's base URL).
- A background poller (started from the app lifespan) fetches each opted-in
  connection's usage endpoint and keeps a normalized in-memory snapshot keyed
  by connection index. The provider is authoritative — nothing persists.
- Only when the snapshot actually changes is a `subscription-usage:update`
  socket event pushed to every connected session (full replace). The frontend
  never polls.
- The snapshot is served synchronously to the bootstrap bundle and to
  GET /api/usage/groups; both kick a background refresh when it's stale
  (the poller skips fetches while no sessions are connected).

Expected usage-endpoint shape (Codex-style):
  {"rate_limits": [{"limit_id": "codex",
                    "primary": {"used_percent": 3.5, "window_minutes": 10080,
                                "resets_at": 1785339012},
                    "secondary": {...}?,
                    "credits": {...}?}]}
"""

import asyncio
import logging
import time
from typing import Optional
from urllib.parse import urlparse

import aiohttp

from open_webui.env import SRC_LOG_LEVELS, SUBSCRIPTION_USAGE_POLL_INTERVAL

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

FETCH_TIMEOUT_SECONDS = 10

_app = None
# Connection index (str) -> normalized usage entry. Contains ONLY fields that
# change when the provider's actual usage changes — no fetch timestamps — so
# it can double as both the change-detection baseline and a bootstrap
# component body without churning the bundle etag between real changes.
_state: dict[str, dict] = {}
_last_fetch_at = 0.0  # monotonic; 0 = never fetched
_refresh_task: Optional[asyncio.Task] = None


def init_subscription_usage(app):
    global _app
    _app = app


def get_subscription_usage_state() -> dict:
    return _state


def derive_usage_url(base_url: str) -> Optional[str]:
    # The usage endpoint lives at the server root (`/usage`), not under the
    # OpenAI-compatible base path (`/v1`), so derive from scheme+host only.
    try:
        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            return None
        return f"{parsed.scheme}://{parsed.netloc}/usage"
    except Exception:
        return None


def get_subscription_connections(app) -> list[dict]:
    """All OpenAI connections flagged `subscription_usage`, with a resolved usage URL."""
    try:
        base_urls = app.state.config.OPENAI_API_BASE_URLS or []
        api_keys = app.state.config.OPENAI_API_KEYS or []
        api_configs = app.state.config.OPENAI_API_CONFIGS or {}
    except Exception:
        return []

    connections = []
    for idx, url in enumerate(base_urls):
        # Same idx-with-legacy-URL-key fallback as routers.openai's
        # _get_connection_api_config (not imported: routers pull in this module
        # transitively and the lookup is two lines).
        config = api_configs.get(str(idx), api_configs.get(url, {})) or {}
        if not config.get("subscription_usage"):
            continue
        usage_url = (config.get("usage_url") or "").strip() or derive_usage_url(url)
        if not usage_url:
            continue
        connections.append(
            {
                "idx": idx,
                "base_url": url,
                "key": api_keys[idx] if idx < len(api_keys) else "",
                "usage_url": usage_url,
            }
        )
    return connections


def _normalize_window(limit_id: str, scope: str, window: dict) -> Optional[dict]:
    if not isinstance(window, dict) or window.get("used_percent") is None:
        return None
    try:
        # Round so float jitter from the provider can't register as a "change"
        # and trigger no-op pushes.
        used_percent = round(float(window["used_percent"]), 2)
    except (TypeError, ValueError):
        return None
    normalized = {
        "id": f"{limit_id}:{scope}",
        "limit_id": limit_id,
        "scope": scope,
        "used_percent": used_percent,
    }
    for field in ("window_minutes", "resets_at"):
        value = window.get(field)
        if value is not None:
            try:
                normalized[field] = int(value)
            except (TypeError, ValueError):
                pass
    return normalized


def _normalize_usage_payload(raw: dict) -> Optional[dict]:
    """Codex-style usage payload -> {"windows": [...], "credits": {...}?} or None."""
    if not isinstance(raw, dict):
        return None
    windows = []
    credits = None
    for entry in raw.get("rate_limits") or []:
        if not isinstance(entry, dict):
            continue
        limit_id = str(entry.get("limit_id") or "usage")
        for scope in ("primary", "secondary"):
            window = _normalize_window(limit_id, scope, entry.get(scope))
            if window:
                windows.append(window)
        if credits is None and isinstance(entry.get("credits"), dict):
            credits = entry["credits"]
    if not windows:
        return None
    normalized = {"windows": windows}
    if credits is not None:
        normalized["credits"] = credits
    return normalized


async def _fetch_connection_usage(
    session: aiohttp.ClientSession, connection: dict
) -> Optional[dict]:
    headers = {}
    if connection["key"]:
        headers["Authorization"] = f"Bearer {connection['key']}"
    async with session.get(
        connection["usage_url"],
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS),
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"usage endpoint returned HTTP {response.status}")
        raw = await response.json(content_type=None)

    normalized = _normalize_usage_payload(raw)
    if normalized is None:
        raise RuntimeError("usage endpoint returned an unrecognized payload shape")

    name = str(
        (raw.get("rate_limits") or [{}])[0].get("limit_id") or ""
    ).strip() or urlparse(connection["base_url"]).netloc
    return {
        "url_idx": connection["idx"],
        "name": name[:1].upper() + name[1:],
        **normalized,
    }


async def refresh_subscription_usage() -> dict:
    """Fetch every subscription connection's usage; push on actual change."""
    global _state, _last_fetch_at
    app = _app
    if app is None:
        return _state

    connections = get_subscription_connections(app)
    _last_fetch_at = time.monotonic()

    new_state: dict[str, dict] = {}
    if connections:
        session = getattr(app.state, "http_session", None)
        results = await asyncio.gather(
            *(_fetch_connection_usage(session, conn) for conn in connections),
            return_exceptions=True,
        )
        for connection, result in zip(connections, results):
            key = str(connection["idx"])
            if isinstance(result, Exception) or result is None:
                log.warning(
                    f"subscription usage fetch failed for {connection['usage_url']}: {result}"
                )
                # Keep the last-good entry — a transient proxy hiccup shouldn't
                # blank the bar. A connection with no successful fetch yet just
                # has no entry (no bar) until the endpoint responds.
                previous = _state.get(key)
                if previous is not None:
                    new_state[key] = previous
            else:
                new_state[key] = result

    changed = new_state != _state
    _state = new_state
    if changed:
        try:
            from open_webui.socket.main import push_subscription_usage_update

            await push_subscription_usage_update(_state)
        except Exception:
            log.exception("subscription-usage:update push failed")
    return _state


def maybe_schedule_refresh(delay: float = 0.0):
    """Debounced background refresh; no-op if one is already pending."""
    global _refresh_task
    if _app is None:
        return
    if _refresh_task is not None and not _refresh_task.done():
        return

    async def _run():
        try:
            if delay:
                await asyncio.sleep(delay)
            await refresh_subscription_usage()
        except Exception:
            log.exception("subscription usage refresh failed")

    try:
        _refresh_task = asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        # No running loop (sync test context); the poller covers it.
        pass


def kick_refresh_if_stale():
    """Read-path staleness backstop (bootstrap / GET /api/usage/groups).

    The poller skips fetches while no sessions are connected, so the first
    read after an idle stretch may see old numbers. Kick an async refresh —
    if anything moved, the change lands via the socket push and the
    mount-time /api/usage/groups fetch moments later.
    """
    if _app is None:
        return
    if not _state and not get_subscription_connections(_app):
        return
    if time.monotonic() - _last_fetch_at > SUBSCRIPTION_USAGE_POLL_INTERVAL:
        maybe_schedule_refresh()


async def subscription_usage_poller(app):
    """Lifespan task: keep the snapshot fresh while sessions are connected."""
    init_subscription_usage(app)
    # Off the boot path; also lets app.state.http_session finish wiring.
    await asyncio.sleep(5)
    try:
        # Seed once regardless of sessions so bootstrap has data immediately.
        await refresh_subscription_usage()
    except Exception:
        log.exception("initial subscription usage fetch failed")

    while True:
        try:
            await asyncio.sleep(SUBSCRIPTION_USAGE_POLL_INTERVAL)
            if not get_subscription_connections(app):
                continue
            # Nobody watching -> no fetch. The read-path staleness kick
            # (kick_refresh_if_stale) covers the next client that shows up.
            from open_webui.socket.main import SESSION_POOL

            if not list(SESSION_POOL.keys()):
                continue
            await refresh_subscription_usage()
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("subscription usage poller iteration failed")
