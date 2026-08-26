"""OpenRouter per-model reasoning-effort discovery.

OpenRouter's public, keyless catalog (``GET https://openrouter.ai/api/v1/models``)
returns a per-model ``reasoning`` object describing which effort levels the model
accepts and whether reasoning is mandatory, e.g.::

    {
      "id": "openai/gpt-5-mini",
      "reasoning": {
        "supported_efforts": ["high", "medium", "low", "minimal"],  # descending
        "default_effort": "medium",
        "default_enabled": true,
        "mandatory": true
      }
    }

Semantics (per OpenRouter docs):

  * ``supported_efforts``  – effort values, highest first. ``null`` ⟹ all gateway
    effort values accepted; **omitted** ⟹ the model exposes no effort selection.
  * ``default_effort``     – pre-select this when reasoning is enabled.
  * ``default_enabled``    – default on/off when the user hasn't chosen.
  * ``mandatory``          – reasoning cannot be turned off (never send ``none``).
  * ``supports_max_tokens``– show a token-budget control instead of effort.

The per-model ``/models/{id}/endpoints`` proxy does **not** carry this object, so
discovery reads the bulk catalog once and caches the ``slug → reasoning`` map.

This module is intentionally free of any FastAPI/request coupling: a pure mapper
(:func:`map_openrouter_reasoning`) plus a process-wide TTL cache with single-flight
refresh and last-good retention. The catalog is the same URL the pricing sync
already fetches, so :func:`populate_from_catalog_items` lets that job warm this
cache with zero extra network calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

import aiohttp

from open_webui.env import (
    SRC_LOG_LEVELS,
    AIOHTTP_CLIENT_SESSION_SSL,
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

OPENROUTER_CATALOG_URL = "https://openrouter.ai/api/v1/models"

# The effort vocabulary this app understands, ordered ascending by strength. Any
# discovered effort outside this set is dropped (forward-compatible: unknown
# future values simply won't appear in selectors rather than break them). Kept in
# lockstep with the frontend ``REASONING_EFFORT_ORDER`` in
# ``src/lib/constants/reasoning.ts``.
KNOWN_EFFORTS: List[str] = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
_KNOWN_EFFORT_RANK = {name: i for i, name in enumerate(KNOWN_EFFORTS)}

# Cache TTL. Reasoning capabilities change rarely; a long TTL keeps the hot model
# path free of network calls. Refreshed opportunistically by the pricing sync.
CACHE_TTL_SECONDS = 6 * 3600

# Process-wide cache. ``map`` is ``slug -> raw reasoning dict`` (as returned by
# OpenRouter, before mapping). ``modalities`` is ``slug -> input_modalities list``
# from the same catalog payload — kept here rather than in its own module purely
# so both are filled by a single catalog fetch. ``fetched_at`` is 0 until the
# first successful fetch; on fetch failure the last-good maps are retained.
_CACHE: Dict[str, object] = {"map": {}, "modalities": {}, "fetched_at": 0.0}
_LOCK = asyncio.Lock()


def _order_efforts(efforts) -> List[str]:
    """Filter to KNOWN_EFFORTS, de-dup, and order ascending by strength.

    OpenRouter returns descending; we normalize to the app's canonical ascending
    order so every selector renders consistently regardless of source.
    """
    if not isinstance(efforts, list):
        return []
    seen = set()
    out = []
    for e in efforts:
        if isinstance(e, str) and e in _KNOWN_EFFORT_RANK and e not in seen:
            seen.add(e)
            out.append(e)
    out.sort(key=lambda e: _KNOWN_EFFORT_RANK[e])
    return out


def map_openrouter_reasoning(reasoning: Optional[dict]) -> Optional[dict]:
    """Normalize a raw OpenRouter ``reasoning`` object into the app's shape.

    Returns ``None`` when the input isn't a reasoning object (non-reasoning
    model). Otherwise returns a dict with only the keys that were present /
    meaningful, so callers can distinguish "reasoning model, no effort
    granularity" (``supported_efforts`` absent) from an explicit effort list.

    Pure and side-effect free.
    """
    if not isinstance(reasoning, dict):
        return None

    mapped: Dict[str, object] = {}

    # ``supported_efforts`` may be a list (explicit set), ``null`` (all efforts
    # accepted), or absent (no effort selection). We only emit the key when it's
    # a non-empty, known-value list; a null/absent/empty value leaves it out so
    # the effective-reasoning resolver falls back to defaults.
    raw_efforts = reasoning.get("supported_efforts")
    ordered = _order_efforts(raw_efforts)
    if ordered:
        mapped["supported_efforts"] = ordered

    default_effort = reasoning.get("default_effort")
    if isinstance(default_effort, str) and default_effort in _KNOWN_EFFORT_RANK:
        mapped["default_effort"] = default_effort

    if isinstance(reasoning.get("default_enabled"), bool):
        mapped["default_enabled"] = reasoning["default_enabled"]

    if isinstance(reasoning.get("mandatory"), bool):
        mapped["mandatory"] = reasoning["mandatory"]

    if isinstance(reasoning.get("supports_max_tokens"), bool):
        mapped["supports_max_tokens"] = reasoning["supports_max_tokens"]

    # A reasoning object with none of the above (e.g. ``{}``) is still a signal
    # that the model reasons — mark it so callers don't treat it as "no data".
    mapped["is_reasoning"] = True
    return mapped


def _extract_reasoning_map(items: List[dict]) -> Dict[str, dict]:
    """Build ``slug -> raw reasoning dict`` from a list of catalog item dicts."""
    result: Dict[str, dict] = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        slug = item.get("id")
        reasoning = item.get("reasoning")
        if isinstance(slug, str) and slug and isinstance(reasoning, dict):
            result[slug] = reasoning
    return result


def _extract_modalities_map(items: List[dict]) -> Dict[str, List[str]]:
    """Build ``slug -> architecture.input_modalities`` from catalog items.

    Needed because a connection configured with a ``model_ids`` allowlist never
    fetches the real provider list — Open WebUI synthesizes bare
    ``{id, name, owned_by}`` stubs instead, so `architecture` (and therefore the
    video/audio input modalities) is absent from every model on that connection.
    """
    result: Dict[str, List[str]] = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        slug = item.get("id")
        architecture = item.get("architecture")
        if not (isinstance(slug, str) and slug and isinstance(architecture, dict)):
            continue
        modalities = architecture.get("input_modalities")
        if isinstance(modalities, list):
            result[slug] = [m for m in modalities if isinstance(m, str)]
    return result


def get_cached_modalities_map() -> Dict[str, List[str]]:
    """Last-known ``slug -> input_modalities``. Never triggers a fetch."""
    return _CACHE["modalities"]  # type: ignore[return-value]


def populate_from_catalog_items(items: List[dict]) -> int:
    """Warm the cache from already-fetched catalog items (e.g. the pricing sync).

    Only overwrites the cache when the incoming payload actually contains
    reasoning objects, so a partial/odd fetch can't wipe a good map. Returns the
    number of slugs recorded.
    """
    # Modalities are recorded independently of reasoning: a payload can be rich
    # in one and empty in the other, and losing the modality map would silently
    # disable video auto-detection.
    modalities_map = _extract_modalities_map(items)
    if modalities_map:
        _CACHE["modalities"] = modalities_map

    reasoning_map = _extract_reasoning_map(items)
    if not reasoning_map:
        # Nothing useful — keep whatever we have. (Fetch still counts as "fresh"
        # only via the network path below; this opportunistic warm never bumps
        # fetched_at on empty input.)
        return 0
    _CACHE["map"] = reasoning_map
    _CACHE["fetched_at"] = time.time()
    log.debug(
        f"openrouter reasoning cache warmed from catalog: {len(reasoning_map)} slugs"
    )
    return len(reasoning_map)


async def _fetch_catalog_reasoning() -> Dict[str, dict]:
    """Fetch the OpenRouter catalog and extract the reasoning map (keyless).

    Side effect: also records the input-modality map from the same payload, so
    modality discovery costs no extra request. The return type is unchanged
    because callers (and tests that monkeypatch this) only want reasoning.
    """
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
        async with session.get(
            OPENROUTER_CATALOG_URL, ssl=AIOHTTP_CLIENT_SESSION_SSL
        ) as r:
            if r.status != 200:
                raise RuntimeError(f"OpenRouter catalog HTTP {r.status}")
            payload = await r.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("OpenRouter catalog: unexpected payload shape")
    modalities_map = _extract_modalities_map(data)
    if modalities_map:
        _CACHE["modalities"] = modalities_map
    return _extract_reasoning_map(data)


def _is_fresh() -> bool:
    return (
        bool(_CACHE["map"])
        and (time.time() - float(_CACHE["fetched_at"])) < CACHE_TTL_SECONDS
    )


async def get_reasoning_map(force: bool = False) -> Dict[str, dict]:
    """Return the ``slug -> raw reasoning dict`` map, refreshing if stale.

    Single-flight via a lock so concurrent callers don't stampede OpenRouter. On
    fetch failure the last-good map is retained and returned (never wiped).
    """
    if not force and _is_fresh():
        return _CACHE["map"]  # type: ignore[return-value]

    async with _LOCK:
        # Re-check inside the lock — another waiter may have just refreshed.
        if not force and _is_fresh():
            return _CACHE["map"]  # type: ignore[return-value]
        try:
            reasoning_map = await _fetch_catalog_reasoning()
            # Only replace on a non-empty fetch; an empty result (odd payload)
            # must not clobber a previously-good map.
            if reasoning_map:
                _CACHE["map"] = reasoning_map
                _CACHE["fetched_at"] = time.time()
                log.info(
                    f"openrouter reasoning cache refreshed: {len(reasoning_map)} slugs"
                )
            elif not _CACHE["map"]:
                # First-ever fetch returned nothing usable — stamp so we don't
                # hammer on every request; the empty map falls back to defaults.
                _CACHE["fetched_at"] = time.time()
        except Exception as e:
            log.warning(f"openrouter reasoning fetch failed (using last-good): {e}")
            # Stamp fetched_at only if we have SOMETHING, to avoid tight retry
            # loops while still allowing a cold cache to retry soon.
            if _CACHE["map"]:
                _CACHE["fetched_at"] = time.time()
        return _CACHE["map"]  # type: ignore[return-value]


def get_cached_reasoning_map() -> Dict[str, dict]:
    """Non-blocking read of the current cache (may be empty if never warmed)."""
    return _CACHE["map"]  # type: ignore[return-value]


def cache_is_cold() -> bool:
    return not bool(_CACHE["map"])


# At most one in-flight background warm at a time, so hot paths (model-list
# enrichment) can request a warm without spawning a task per call.
_warm_task: Optional["asyncio.Task"] = None


def ensure_warm_background() -> None:
    """Kick a single background refresh if the cache is stale — non-blocking."""
    global _warm_task
    if _is_fresh():
        return
    if _warm_task is not None and not _warm_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _warm_task = loop.create_task(get_reasoning_map())


def discover_reasoning_for_slug(slug: str) -> Optional[dict]:
    """Mapped discovery for a bare OpenRouter slug from the cache (no fetch)."""
    if not slug:
        return None
    raw = get_cached_reasoning_map().get(slug)
    return map_openrouter_reasoning(raw)


async def discover_reasoning_for_slug_async(
    slug: str, force: bool = False
) -> Optional[dict]:
    """Mapped discovery for a slug, refreshing the cache if stale/forced."""
    if not slug:
        return None
    reasoning_map = await get_reasoning_map(force=force)
    return map_openrouter_reasoning(reasoning_map.get(slug))


def reset_cache_for_tests() -> None:
    _CACHE["map"] = {}
    _CACHE["modalities"] = {}
    _CACHE["fetched_at"] = 0.0
