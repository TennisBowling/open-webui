"""
Analytics Router for Token Usage "Wrapped" Feature

Provides API endpoints for:
- Per-conversation token stats (for chat UI display)
- User wrapped summary and heatmap data
- Global/site-wide analytics (admin only)
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from open_webui.models.analytics import (
    Analytics,
    ConversationTokenUsageResponse,
    HeatmapResponse,
    ModelUsageResponse,
    TopChatResponse,
    WrappedSummaryResponse,
    GlobalWrappedResponse,
    UserUsageResponse,
    SubagentAnalyticsResponse,
    TotalSpendResponse,
    DailySpendPoint,
    CacheAnalyticsResponse,
)
from open_webui.models.pricing import Pricing
from open_webui.models.chats import Chats
from open_webui.constants import ERROR_MESSAGES
from open_webui.env import SRC_LOG_LEVELS
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.pricing import run_pricing_sync, invalidate_pricing_cache

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

router = APIRouter()


def _resolve_window(
    year: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> tuple[int, int]:
    """Resolve a [start_ts, end_ts) unix-second window.

    If both start_ts and end_ts are given, use them. Otherwise fall back to the
    full year (defaulting to the current year), preserving the existing
    year-scoped behavior.
    """
    if start_ts is not None and end_ts is not None:
        return int(start_ts), int(end_ts)
    if year is None:
        year = datetime.now(timezone.utc).year
    ys = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    ye = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
    return ys, ye


############################
# Conversation Token Stats
############################

@router.get("/chat/{chat_id}", response_model=Optional[ConversationTokenUsageResponse])
async def get_chat_token_stats(chat_id: str, user=Depends(get_verified_user)):
    """
    Get token usage statistics for a specific conversation.
    
    Returns:
    - total_input_tokens: Total input tokens for all messages
    - total_output_tokens: Total output tokens for all messages
    - total_tokens: Combined total
    - last_input_tokens: Input tokens for most recent message
    - last_output_tokens: Output tokens for most recent message
    - message_count: Number of message exchanges
    
    Used to display token stats next to model name in chat UI.
    """
    log.info(f"📊 [get_chat_token_stats] Called for chat_id={chat_id}, user_id={user.id}")

    # Authorize WITHOUT hydrating the chat: this endpoint fires once per chat
    # open, and the old get_chat_by_id_and_user_id call materialized every
    # message row (3MB+ on long chats) plus full ORM/Pydantic validation just
    # to answer "is this yours?". user_owns_chat is the purpose-built
    # predicate (single indexed lookup, ~0.3ms).
    owns = await Chats.user_owns_chat(chat_id, user.id)
    if not owns and user.role != "admin":
        log.info(f"📊 [get_chat_token_stats] User not authorized (not admin, no chat access)")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    # Admin inspecting someone else's chat (rare, off the hot path): existence
    # check only.
    if not owns and user.role == "admin":
        if not await Chats.get_chat_by_id(chat_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=ERROR_MESSAGES.NOT_FOUND,
            )

    stats = await Analytics.get_conversation_token_usage(chat_id)
    log.info(f"📊 [get_chat_token_stats] Stats from DB: {stats}")

    # Per-chat USD cost (folds subagent spend via attributed_chat_id).
    cost = await Analytics.get_chat_cost(chat_id)

    # Return empty stats if no token data yet
    if not stats:
        log.info(f"📊 [get_chat_token_stats] No stats found, returning empty response")
        return ConversationTokenUsageResponse(
            chat_id=chat_id,
            user_id=user.id,
            total_input_tokens=0,
            total_output_tokens=0,
            total_tokens=0,
            total_cache_read_tokens=0,
            last_input_tokens=0,
            last_output_tokens=0,
            last_cache_read_tokens=0,
            message_count=0,
            cost=cost or 0.0,
            created_at=0,
            updated_at=0,
        )

    stats.cost = cost or 0.0
    log.info(f"📊 [get_chat_token_stats] Returning stats with total_tokens={stats.total_tokens}")
    return stats


############################
# User Wrapped Data
############################


@router.get("/user/wrapped", response_model=WrappedSummaryResponse)
async def get_user_wrapped(
    year: Optional[int] = None,
    user=Depends(get_verified_user)
):
    """
    Get comprehensive "Wrapped" summary for the authenticated user.
    
    Includes:
    - Total conversations, messages, and tokens
    - Days active
    - Most active day with details
    - Favorite (most-used) model
    - Top 10 chats by token count
    
    Args:
        year: Optional year to filter (defaults to current year)
    """
    if year is None:
        year = datetime.now(timezone.utc).year
    
    wrapped = await Analytics.get_user_wrapped(user.id, year)
    
    # Enrich top chats with titles from Chats table
    enriched_chats = []
    for chat in wrapped.top_chats:
        chat_data = await Chats.get_chat_by_id(chat.chat_id)
        title = chat_data.title if chat_data else None
        enriched_chats.append(TopChatResponse(
            chat_id=chat.chat_id,
            title=title,
            model_id=chat.model_id,
            total_tokens=chat.total_tokens,
            total_input_tokens=chat.total_input_tokens,
            total_output_tokens=chat.total_output_tokens,
            total_cache_read_tokens=chat.total_cache_read_tokens,
            last_cache_read_tokens=chat.last_cache_read_tokens,
            message_count=chat.message_count,
        ))
    
    wrapped.top_chats = enriched_chats
    return wrapped


@router.get("/user/heatmap", response_model=HeatmapResponse)
async def get_user_heatmap(
    year: Optional[int] = None,
    user=Depends(get_verified_user)
):
    """
    Get activity heatmap data for the authenticated user.
    
    Returns daily token counts for all days in the specified year,
    with intensity levels (0-4) for visualization.
    
    Similar to GitHub's contribution graph.
    
    Args:
        year: Optional year (defaults to current year)
    """
    if year is None:
        year = datetime.now(timezone.utc).year
    
    return await Analytics.get_heatmap_data(user.id, year)


@router.get("/user/models", response_model=list[ModelUsageResponse])
async def get_user_model_usage(
    year: Optional[int] = None,
    user=Depends(get_verified_user)
):
    """
    Get per-model token usage breakdown for the authenticated user.
    
    Returns list of models used with:
    - Token counts (input, output, total)
    - Usage counts (conversations, messages)
    - Percentage of total usage
    
    Sorted by total tokens descending.
    """
    return await Analytics.get_model_usage_by_user(user.id, year)


@router.get("/user/top-chats", response_model=list[TopChatResponse])
async def get_user_top_chats(
    year: Optional[int] = None,
    limit: int = 10,
    user=Depends(get_verified_user)
):
    """
    Get user's top conversations by token count.
    
    Args:
        year: Optional year filter
        limit: Max results (default 10)
    """
    chats = await Analytics.get_top_chats_by_user(user.id, year, limit)
    
    # Enrich with chat titles
    enriched = []
    for chat in chats:
        chat_data = await Chats.get_chat_by_id(chat.chat_id)
        title = chat_data.title if chat_data else None
        enriched.append(TopChatResponse(
            chat_id=chat.chat_id,
            title=title,
            model_id=chat.model_id,
            total_tokens=chat.total_tokens,
            total_input_tokens=chat.total_input_tokens,
            total_output_tokens=chat.total_output_tokens,
            total_cache_read_tokens=chat.total_cache_read_tokens,
            last_cache_read_tokens=chat.last_cache_read_tokens,
            message_count=chat.message_count,
        ))
    
    return enriched


############################
# Global/Admin Analytics
############################


@router.get("/global/wrapped", response_model=GlobalWrappedResponse)
async def get_global_wrapped(
    year: Optional[int] = None,
    user=Depends(get_admin_user)
):
    """
    Get site-wide "Wrapped" statistics.
    
    Admin only endpoint.
    
    Includes:
    - Total active users
    - Total conversations and messages
    - Total tokens processed
    - Top models by usage
    - Busiest day
    """
    if year is None:
        year = datetime.now(timezone.utc).year
    
    return await Analytics.get_global_wrapped(year)


@router.get("/global/models", response_model=list[ModelUsageResponse])
async def get_global_model_usage(
    year: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    limit: int = 50,
    user=Depends(get_admin_user)
):
    """
    Get site-wide model usage + USD cost breakdown over a window.

    Admin only endpoint. Returns models ordered by computed cost.
    """
    ws, we = _resolve_window(year, start_ts, end_ts)
    return await Analytics.get_global_model_cost(ws, we, limit)


@router.get("/global/cache", response_model=CacheAnalyticsResponse)
async def get_global_cache_analytics(
    group_by: str = "gateway",
    year: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    user=Depends(get_admin_user),
):
    """Cache intelligence over a window: per-provider/model survival curve,
    estimated cache TTL, hit rate, dollars saved by caching, and per-user
    leaders. ``group_by`` ∈ {gateway, vendor, model}. Admin only."""
    if group_by not in ("gateway", "vendor", "model"):
        group_by = "gateway"
    ws, we = _resolve_window(year, start_ts, end_ts)
    return await Analytics.get_cache_analytics(ws, we, group_by)


@router.get("/global/users", response_model=list[UserUsageResponse])
async def get_global_user_usage(
    year: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    limit: int = 100,
    user=Depends(get_admin_user)
):
    """Get per-user token/cache/message stats + USD cost for admins."""
    if year is None and (start_ts is None or end_ts is None):
        year = datetime.now(timezone.utc).year
    users = await Analytics.get_global_user_usage(year, limit)

    # Attach per-user cost from token_usage_event over the same window.
    ws, we = _resolve_window(year, start_ts, end_ts)
    cost_map = await Analytics.get_user_cost_map(ws, we)
    for u in users:
        c = cost_map.get(u.user_id)
        if c:
            u.cost = round(c.get("cost", 0.0), 6)
            u.unpriced_tokens = int(c.get("unpriced_tokens", 0))
    return users


@router.get("/global/spend", response_model=TotalSpendResponse)
async def get_global_spend(
    year: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    user=Depends(get_admin_user)
):
    """Site-wide spend KPI over a window (admin)."""
    ws, we = _resolve_window(year, start_ts, end_ts)
    return await Analytics.get_total_spend(ws, we)


@router.get("/global/spend-trend", response_model=list[DailySpendPoint])
async def get_global_spend_trend(
    year: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    user=Depends(get_admin_user)
):
    """Daily spend series over a window (admin)."""
    ws, we = _resolve_window(year, start_ts, end_ts)
    return await Analytics.get_spend_trend(ws, we)


@router.get("/global/top-chats-by-cost", response_model=list[TopChatResponse])
async def get_global_top_chats_by_cost(
    year: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    limit: int = 10,
    user=Depends(get_admin_user)
):
    """Most expensive chats over a window (admin)."""
    ws, we = _resolve_window(year, start_ts, end_ts)
    chats = await Analytics.get_top_chats_by_cost(ws, we, limit)
    # Enrich with chat titles
    enriched = []
    for chat in chats:
        chat_data = await Chats.get_chat_by_id(chat.chat_id)
        chat.title = chat_data.title if chat_data else None
        enriched.append(chat)
    return enriched


@router.get("/global/subagents", response_model=SubagentAnalyticsResponse)
async def get_global_subagent_usage(
    year: Optional[int] = None,
    user=Depends(get_admin_user)
):
    """Get site-wide subagent usage analytics."""
    if year is None:
        year = datetime.now(timezone.utc).year
    return await Analytics.get_global_subagent_usage(year)


@router.get("/global/heatmap", response_model=HeatmapResponse)
async def get_global_heatmap(
    year: Optional[int] = None,
    user=Depends(get_admin_user)
):
    """
    Get site-wide activity heatmap data.
    
    Admin only endpoint.
    
    Aggregates all users' activity for the specified year.
    Note: This uses a special "global" user_id internally.
    """
    if year is None:
        year = datetime.now(timezone.utc).year
    
    # For global heatmap, we need to aggregate across all users
    # We'll use a special method or aggregate from daily_token_usage
    try:
        from open_webui.internal.db import get_db
        from open_webui.models.analytics import DailyTokenUsage, HeatmapDataPoint
        from sqlalchemy import func, select
        from datetime import timedelta
        
        async with get_db() as db:
            year_start = f"{year}-01-01"
            year_end = f"{year}-12-31"
            
            # Aggregate all users' daily data
            daily_totals = (
                await db.execute(
                    select(
                        DailyTokenUsage.date,
                        func.sum(DailyTokenUsage.total_tokens).label('total'),
                    )
                    .where(
                        DailyTokenUsage.date >= year_start,
                        DailyTokenUsage.date <= year_end,
                    )
                    .group_by(DailyTokenUsage.date)
                )
            ).all()
            
            # Build date -> tokens map
            date_tokens = {row.date: row.total for row in daily_totals}
            max_tokens = max(date_tokens.values()) if date_tokens else 0
            
            # Calculate levels
            def calculate_level(tokens: int) -> int:
                if tokens == 0:
                    return 0
                if max_tokens == 0:
                    return 0
                ratio = tokens / max_tokens
                if ratio < 0.25:
                    return 1
                elif ratio < 0.5:
                    return 2
                elif ratio < 0.75:
                    return 3
                else:
                    return 4
            
            # Generate all days
            data_points = []
            current_date = datetime(year, 1, 1, tzinfo=timezone.utc)
            end_date = datetime(year, 12, 31, tzinfo=timezone.utc)
            
            while current_date <= end_date:
                date_str = current_date.strftime('%Y-%m-%d')
                tokens = date_tokens.get(date_str, 0)
                data_points.append(HeatmapDataPoint(
                    date=date_str,
                    tokens=tokens,
                    level=calculate_level(tokens)
                ))
                current_date += timedelta(days=1)
            
            return HeatmapResponse(
                year=year,
                data=data_points,
                max_tokens=max_tokens,
                total_days_active=len([d for d in data_points if d.tokens > 0])
            )
    except Exception as e:
        log.error(f"Error getting global heatmap: {e}")
        return HeatmapResponse(year=year, data=[], max_tokens=0, total_days_active=0)


############################
# Pricing management (admin)
############################


class PricingOverrideForm(BaseModel):
    model_id: str
    mode: str  # alias | manual | zero
    alias_slug: Optional[str] = None
    prompt_rate: Optional[float] = None
    completion_rate: Optional[float] = None
    cache_read_rate: Optional[float] = None
    note: Optional[str] = None


@router.get("/pricing/catalog")
async def get_pricing_catalog(user=Depends(get_admin_user)):
    """List the synced OpenRouter pricing catalog (admin)."""
    catalog = await Pricing.list_catalog()
    synced_at = await Pricing.catalog_synced_at()
    return {"catalog": catalog, "synced_at": synced_at}


@router.get("/pricing/overrides")
async def get_pricing_overrides(user=Depends(get_admin_user)):
    """List overrides + a per-model resolution preview over all stored model_ids.

    This is the admin "mapping cockpit": every distinct model_id seen in
    token_usage_event, its token volume, and how it currently resolves
    (catalog / override / unpriced).
    """
    from open_webui.internal.db import get_db
    from open_webui.utils.pricing import resolve_rate, get_cached_pricing_map
    from sqlalchemy import text as sql_text

    overrides = await Pricing.list_overrides()
    pricing_map = await Pricing.get_pricing_map()

    def _build(db):
        rows = db.execute(sql_text(
            """
            SELECT COALESCE(NULLIF(model_id, ''), 'unknown') AS model_id,
                CAST(COALESCE(SUM(total_tokens), 0) AS BIGINT) AS total_tokens,
                CAST(COALESCE(SUM(CASE WHEN embedded_cost IS NULL THEN total_tokens ELSE 0 END), 0) AS BIGINT) AS rate_card_tokens
            FROM token_usage_event
            GROUP BY model_id
            ORDER BY total_tokens DESC
            """
        )).mappings().all()
        out = []
        for r in rows:
            mid = r["model_id"]
            rate = resolve_rate(mid, pricing_map)
            # A model is "priced" if it has no rate-card tokens (all embedded) or a rate resolves.
            priced = int(r["rate_card_tokens"]) == 0 or rate is not None
            out.append({
                "model_id": mid,
                "total_tokens": int(r["total_tokens"]),
                "rate_card_tokens": int(r["rate_card_tokens"]),
                "priced": priced,
                "rate_source": rate.get("source") if rate else None,
                "effective_rate": {
                    "prompt": rate.get("prompt"),
                    "completion": rate.get("completion"),
                    "cache_read": rate.get("cache_read"),
                } if rate else None,
            })
        return out

    # run the sync DB read off the event loop
    from open_webui.internal.db import run_sync_db

    def _runner():
        with get_db() as db:
            return _build(db)

    resolution = await run_sync_db(_runner)
    return {"overrides": overrides, "resolution": resolution}


@router.post("/pricing/overrides")
async def upsert_pricing_override(form: PricingOverrideForm, user=Depends(get_admin_user)):
    """Create/update a pricing override (admin)."""
    if form.mode not in ("alias", "manual", "zero"):
        raise HTTPException(status_code=400, detail="mode must be alias, manual, or zero")
    if form.mode == "alias":
        if not form.alias_slug:
            raise HTTPException(status_code=400, detail="alias_slug required for alias mode")
        catalog = {c["slug"] for c in await Pricing.list_catalog()}
        if form.alias_slug not in catalog:
            raise HTTPException(status_code=400, detail=f"alias_slug '{form.alias_slug}' not in catalog")
    result = await Pricing.upsert_override(
        model_id=form.model_id,
        mode=form.mode,
        alias_slug=form.alias_slug,
        prompt_rate=form.prompt_rate,
        completion_rate=form.completion_rate,
        cache_read_rate=form.cache_read_rate,
        note=form.note,
        updated_by=user.id,
    )
    invalidate_pricing_cache()
    if not result:
        raise HTTPException(status_code=500, detail="Failed to save override")
    return result


@router.delete("/pricing/overrides/{model_id:path}")
async def delete_pricing_override(model_id: str, user=Depends(get_admin_user)):
    """Delete a pricing override, reverting to catalog/unpriced (admin)."""
    ok = await Pricing.delete_override(model_id)
    invalidate_pricing_cache()
    return {"status": "ok" if ok else "error"}


@router.post("/pricing/sync")
async def sync_pricing(user=Depends(get_admin_user)):
    """Manually sync the OpenRouter pricing catalog now (admin)."""
    return await run_pricing_sync()
