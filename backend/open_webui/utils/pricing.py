"""
Cost-calculation engine for token usage.

Pure, unit-testable functions for turning a provider ``usage`` blob (or token
counts) into USD cost, plus the OpenRouter catalog sync.

Two regimes, decided PER REQUEST (per ``token_usage_event`` row):

1. Embedded (OpenRouter-routed): ``raw_usage`` carries an authoritative ``cost``.
   Per-call cost = ``cost`` if non-zero else ``cost_details.upstream_inference_cost``
   (BYOK fallback). Never recomputed from tokens. This is persisted to the typed
   ``token_usage_event.embedded_cost`` column at ingestion, so the read path never
   re-parses JSON.

2. Rate-card (bare-id "C" provider, legacy aggregates, locals): priced from a
   per-token rate resolved via ``resolve_rate``. Implicit caching only:
   ``(prompt - cache_read) * prompt_rate + cache_read * cache_read_rate
     + completion * completion_rate``.
   No cache-write term anywhere; reasoning is already inside ``completion_tokens``.
"""

import logging
import time
from typing import Dict, List, Optional

import aiohttp

from open_webui.env import (
    SRC_LOG_LEVELS,
    AIOHTTP_CLIENT_SESSION_SSL,
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST,
)
from open_webui.models.pricing import Pricing, PricingSync

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

OPENROUTER_CATALOG_URL = "https://openrouter.ai/api/v1/models"


####################
# Pure cost functions
####################


def _to_float(value) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def embedded_cost(raw_usage: Optional[dict]) -> Optional[float]:
    """Authoritative per-call USD cost for OpenRouter-routed rows, else None.

    Returns None when ``raw_usage`` has no ``cost`` key (i.e. not OpenRouter-routed
    -> the row must be rate-carded). Otherwise: ``cost`` if non-zero, else the BYOK
    fallback ``cost_details.upstream_inference_cost``.
    """
    if not isinstance(raw_usage, dict) or "cost" not in raw_usage:
        return None
    cost = _to_float(raw_usage.get("cost"))
    if cost != 0:
        return cost
    details = raw_usage.get("cost_details") or {}
    if isinstance(details, dict):
        return _to_float(details.get("upstream_inference_cost"))
    return 0.0


def resolve_rate(model_id: str, pricing_map: Dict) -> Optional[Dict]:
    """Resolve per-token rates for a stored model_id. None => unpriced.

    Order: override (alias/manual/zero) -> direct catalog slug match -> None.
    Returns a dict {prompt, completion, cache_read, source} or None.
    """
    catalog = pricing_map.get("catalog", {})
    overrides = pricing_map.get("overrides", {})

    ov = overrides.get(model_id)
    if ov:
        mode = ov.get("mode")
        if mode == "zero":
            return {"prompt": 0.0, "completion": 0.0, "cache_read": 0.0, "source": "override_zero"}
        if mode == "manual":
            return {
                "prompt": _to_float(ov.get("prompt_rate")),
                "completion": _to_float(ov.get("completion_rate")),
                "cache_read": _to_float(ov.get("cache_read_rate")),
                "source": "override_manual",
            }
        if mode == "alias":
            slug = ov.get("alias_slug")
            rates = catalog.get(slug)
            if rates:
                return {
                    "prompt": rates["prompt"],
                    "completion": rates["completion"],
                    "cache_read": rates["cache_read"],
                    "source": "override_alias",
                }
            return None  # aliased to a slug we haven't synced -> unpriced

    rates = catalog.get(model_id)
    if rates:
        return {
            "prompt": rates["prompt"],
            "completion": rates["completion"],
            "cache_read": rates["cache_read"],
            "source": "catalog",
        }
    return None


def compute_cost(prompt_ex_cache: int, completion: int, cache_read: int, rate: Optional[Dict]) -> float:
    """Rate-card cost for one request (implicit caching, no cache-write term)."""
    if not rate:
        return 0.0
    return (
        max(prompt_ex_cache, 0) * rate.get("prompt", 0.0)
        + max(cache_read, 0) * rate.get("cache_read", 0.0)
        + max(completion, 0) * rate.get("completion", 0.0)
    )


def fold_cost_rows(rows: List[Dict], pricing_map: Dict) -> Dict[str, Dict]:
    """Fold per-(dim, model_id) aggregate rows into per-dim cost totals.

    Each input row must have: dim, model_id, total_tokens, embedded_cost,
    rc_prompt, rc_cache_read, rc_completion, rc_total_tokens.

    Returns {dim: {cost, embedded_cost, rate_card_cost, total_tokens,
                   unpriced_tokens, rate_source}}.
    """
    out: Dict[str, Dict] = {}
    for r in rows:
        dim = r.get("dim")
        if dim is None:
            dim = ""
        bucket = out.get(dim)
        if bucket is None:
            bucket = {
                "cost": 0.0,
                "embedded_cost": 0.0,
                "rate_card_cost": 0.0,
                "total_tokens": 0,
                "unpriced_tokens": 0,
                "rate_source": None,
            }
            out[dim] = bucket

        emb = float(r.get("embedded_cost") or 0.0)
        rc_prompt = int(r.get("rc_prompt") or 0)
        rc_cache = int(r.get("rc_cache_read") or 0)
        rc_completion = int(r.get("rc_completion") or 0)
        rc_total = int(r.get("rc_total_tokens") or 0)

        rate = resolve_rate(r.get("model_id") or "", pricing_map) if rc_total > 0 else None
        if rc_total > 0 and rate is None:
            bucket["unpriced_tokens"] += rc_total
            rc_cost = 0.0
        else:
            rc_cost = compute_cost(rc_prompt, rc_completion, rc_cache, rate)
            if rate is not None and bucket["rate_source"] is None:
                bucket["rate_source"] = rate.get("source")

        bucket["embedded_cost"] += emb
        bucket["rate_card_cost"] += rc_cost
        bucket["cost"] += emb + rc_cost
        bucket["total_tokens"] += int(r.get("total_tokens") or 0)

    return out


####################
# Pricing map cache
####################

_PRICING_MAP_CACHE = {"map": None, "ts": 0.0}
_PRICING_MAP_TTL = 60.0


def get_cached_pricing_map() -> Dict:
    """In-process, TTL-cached pricing map for hot read paths (e.g. the chat pill).

    Uses the SYNC table directly so it can be called from sync aggregation code.
    """
    now = time.time()
    cached = _PRICING_MAP_CACHE
    if cached["map"] is not None and (now - cached["ts"]) < _PRICING_MAP_TTL:
        return cached["map"]
    pmap = PricingSync.get_pricing_map()
    cached["map"] = pmap
    cached["ts"] = now
    return pmap


def invalidate_pricing_cache() -> None:
    _PRICING_MAP_CACHE["map"] = None
    _PRICING_MAP_CACHE["ts"] = 0.0


####################
# OpenRouter sync
####################


def _parse_catalog_item(item: dict) -> Optional[dict]:
    slug = item.get("id")
    pricing = item.get("pricing")
    if not slug or not isinstance(pricing, dict):
        return None
    prompt_rate = _to_float(pricing.get("prompt"))
    completion_rate = _to_float(pricing.get("completion"))
    is_free = str(slug).endswith(":free") or (prompt_rate == 0 and completion_rate == 0)
    return {
        "slug": slug,
        "model_name": item.get("name"),
        "prompt_rate": prompt_rate,
        "completion_rate": completion_rate,
        "cache_read_rate": _to_float(pricing.get("input_cache_read")),
        "web_search_rate": _to_float(pricing.get("web_search")),
        "is_free": is_free,
        "raw_pricing": pricing,
    }


async def fetch_openrouter_catalog() -> List[dict]:
    """Fetch the OpenRouter bulk model catalog (keyless, public)."""
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
        async with session.get(OPENROUTER_CATALOG_URL, ssl=AIOHTTP_CLIENT_SESSION_SSL) as r:
            if r.status != 200:
                raise RuntimeError(f"OpenRouter catalog HTTP {r.status}")
            payload = await r.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("OpenRouter catalog: unexpected payload shape")

    # Warm the reasoning-discovery cache from the same payload — the raw catalog
    # items carry a per-model ``reasoning`` object we'd otherwise have to fetch
    # again. Best-effort; never let it break the pricing sync.
    try:
        from open_webui.utils import openrouter_reasoning

        openrouter_reasoning.populate_from_catalog_items(data)
    except Exception as e:
        log.debug(f"reasoning cache warm from catalog skipped: {e}")

    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        parsed = _parse_catalog_item(item)
        if parsed:
            rows.append(parsed)
    return rows


async def run_pricing_sync() -> dict:
    """Fetch + upsert the OpenRouter catalog. Returns a status dict.

    On failure, the existing catalog is left untouched (upsert, never wipe).
    """
    try:
        rows = await fetch_openrouter_catalog()
        synced_at = int(time.time())
        count = await Pricing.upsert_catalog(rows, synced_at)
        invalidate_pricing_cache()
        log.info(f"Pricing sync: upserted {count} catalog rows")
        return {"status": "ok", "synced_count": count, "synced_at": synced_at}
    except Exception as e:
        log.error(f"Pricing sync failed: {e}")
        return {"status": "error", "error": str(e), "synced_count": 0, "synced_at": None}
