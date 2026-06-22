"""
Token Analytics Models for "Wrapped" Feature

This module provides database models and helper classes for tracking token usage
analytics per conversation, per day, and per model. This data is used to generate
"Spotify Wrapped" style analytics for users and site-wide statistics.

Tables:
- ConversationTokenUsage: Tracks tokens per chat conversation
- DailyTokenUsage: Aggregates daily token stats per user for heatmaps
- ModelTokenUsage: Tracks usage per model per user for breakdowns
"""

import logging
import inspect
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict

from open_webui.internal.db import Base, get_db, run_sync_db
from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.users import User

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, String, Text, Integer, Index, JSON, Float, func, desc, text as sql_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


####################
# SQLAlchemy Models
####################


class ConversationTokenUsage(Base):
    """
    Tracks token usage for each conversation/chat.
    Updated after each message exchange.
    """
    __tablename__ = "conversation_token_usage"

    id = Column(String, primary_key=True)  # UUID
    chat_id = Column(String, index=True, unique=True)  # References chat.id
    user_id = Column(String, index=True)  # References user.id
    model_id = Column(String)  # Primary model used in conversation

    # Cumulative totals for the conversation
    total_input_tokens = Column(BigInteger, default=0)
    total_output_tokens = Column(BigInteger, default=0)
    total_tokens = Column(BigInteger, default=0)
    total_cache_read_tokens = Column(BigInteger, default=0)

    # Last message/request stats (for real-time display)
    last_input_tokens = Column(BigInteger, default=0)
    last_output_tokens = Column(BigInteger, default=0)
    last_cache_read_tokens = Column(BigInteger, default=0)

    # Metadata
    message_count = Column(Integer, default=0)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)

    __table_args__ = (
        Index("conv_token_user_idx", "user_id"),
        Index("conv_token_chat_idx", "chat_id"),
        Index("conv_token_total_idx", "total_tokens"),  # For "longest chats" queries
    )


class DailyTokenUsage(Base):
    """
    Aggregated daily token usage per user.
    Used for activity heatmaps and daily trends.
    """
    __tablename__ = "daily_token_usage"

    id = Column(String, primary_key=True)  # UUID
    user_id = Column(String, index=True)  # References user.id
    date = Column(String, index=True)  # YYYY-MM-DD format

    # Token counts
    total_input_tokens = Column(BigInteger, default=0)
    total_output_tokens = Column(BigInteger, default=0)
    total_tokens = Column(BigInteger, default=0)
    total_cache_read_tokens = Column(BigInteger, default=0)

    # Activity metrics
    conversation_count = Column(Integer, default=0)  # Unique chats active that day
    message_count = Column(Integer, default=0)  # Total messages sent

    # Timestamps
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)

    __table_args__ = (
        Index("daily_user_date_idx", "user_id", "date", unique=True),
        Index("daily_date_idx", "date"),
        Index("daily_total_idx", "total_tokens"),
    )


class ModelTokenUsage(Base):
    """
    Tracks per-model token usage.
    Supports both per-user and global (user_id=NULL) aggregations.
    """
    __tablename__ = "model_token_usage"

    id = Column(String, primary_key=True)  # UUID
    user_id = Column(String, index=True, nullable=True)  # NULL = global aggregate
    model_id = Column(String, index=True)

    # Token counts
    total_input_tokens = Column(BigInteger, default=0)
    total_output_tokens = Column(BigInteger, default=0)
    total_tokens = Column(BigInteger, default=0)
    total_cache_read_tokens = Column(BigInteger, default=0)

    # Usage counts
    conversation_count = Column(Integer, default=0)
    message_count = Column(Integer, default=0)

    # Timestamps
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)

    __table_args__ = (
        Index("model_user_model_idx", "user_id", "model_id", unique=True),
        Index("model_total_idx", "total_tokens"),
    )


class TokenUsageEvent(Base):
    """One row per provider usage payload/model call.

    ``source_chat_id`` is the chat that actually ran the model call. For
    subagents this is the hidden subagent chat. ``attributed_chat_id`` is the
    visible chat that should receive Wrapped / top-chat credit.
    """

    __tablename__ = "token_usage_event"

    id = Column(String, primary_key=True)
    user_id = Column(String, index=True)
    source_chat_id = Column(String, index=True)
    attributed_chat_id = Column(String, index=True)
    message_id = Column(String, index=True, nullable=True)
    parent_message_id = Column(String, nullable=True)
    model_id = Column(String, index=True)
    prompt_tokens = Column(BigInteger, default=0)
    completion_tokens = Column(BigInteger, default=0)
    total_tokens = Column(BigInteger, default=0)
    cache_read_tokens = Column(BigInteger, default=0)
    request_count = Column(Integer, default=1)
    source_type = Column(String, default="chat")
    raw_usage = Column(JSON)
    # Authoritative per-call USD cost for OpenRouter-routed rows (NULL = rate-card
    # row, priced at read time). Precomputed at ingestion so reads never parse JSON.
    embedded_cost = Column(Float, nullable=True)
    created_at = Column(BigInteger)

    __table_args__ = (
        Index("token_usage_event_attr_chat_idx", "attributed_chat_id"),
        Index("token_usage_event_source_chat_idx", "source_chat_id"),
        Index("token_usage_event_user_ts_idx", "user_id", "created_at"),
        Index("token_usage_event_model_idx", "model_id"),
    )


####################
# Pydantic Response Models
####################


class ConversationTokenUsageResponse(BaseModel):
    """Response model for conversation token stats"""
    model_config = ConfigDict(from_attributes=True)

    chat_id: str
    user_id: str
    model_id: Optional[str] = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cache_read_tokens: int = 0
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    last_cache_read_tokens: int = 0
    message_count: int = 0
    cost: float = 0.0
    created_at: int
    updated_at: int


class DailyTokenUsageResponse(BaseModel):
    """Response model for daily token stats"""
    model_config = ConfigDict(from_attributes=True)

    date: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cache_read_tokens: int = 0
    conversation_count: int = 0
    message_count: int = 0


class HeatmapDataPoint(BaseModel):
    """Single data point for activity heatmap"""
    date: str
    tokens: int
    level: int  # 0-4 scale for color intensity


class HeatmapResponse(BaseModel):
    """Response model for heatmap data"""
    year: int
    data: List[HeatmapDataPoint]
    max_tokens: int
    total_days_active: int


class ModelUsageResponse(BaseModel):
    """Response model for model usage breakdown"""
    model_id: str
    model_name: Optional[str] = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cache_read_tokens: int = 0
    conversation_count: int = 0
    message_count: int = 0
    percentage: float = 0.0
    cost: float = 0.0
    unpriced_tokens: int = 0
    rate_source: Optional[str] = None


class TopChatResponse(BaseModel):
    """Response model for top chats by tokens"""
    chat_id: str
    title: Optional[str] = None
    model_id: Optional[str] = None
    total_tokens: int
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int = 0
    last_cache_read_tokens: int = 0
    message_count: int
    cost: float = 0.0


class WrappedSummaryResponse(BaseModel):
    """Response model for wrapped summary stats"""
    year: int
    total_conversations: int = 0
    total_messages: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cache_read_tokens: int = 0
    days_active: int = 0
    most_active_day: Optional[Dict] = None
    favorite_model: Optional[Dict] = None
    top_chats: List[TopChatResponse] = []


class GlobalWrappedResponse(BaseModel):
    """Response model for site-wide wrapped stats"""
    year: int
    total_users_active: int = 0
    total_conversations: int = 0
    total_messages: int = 0
    total_tokens: int = 0
    total_cache_read_tokens: int = 0
    top_models: List[ModelUsageResponse] = []
    busiest_day: Optional[Dict] = None


class UserUsageResponse(BaseModel):
    """Admin response model for per-user analytics."""

    user_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cache_read_tokens: int = 0
    conversation_count: int = 0
    message_count: int = 0
    days_active: int = 0
    avg_tokens_per_active_day: int = 0
    avg_tokens_per_message: int = 0
    cache_read_rate: float = 0.0
    last_active_at: Optional[int] = None
    cost: float = 0.0
    unpriced_tokens: int = 0


class SubagentAnalyticsResponse(BaseModel):
    """Admin response model for subagent usage analytics."""

    year: int
    total_subagent_chats: int = 0
    parent_chat_count: int = 0
    request_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cache_read_tokens: int = 0
    token_share_percent: float = 0.0
    avg_tokens_per_subagent: int = 0
    avg_requests_per_subagent: float = 0.0
    avg_subagents_per_parent: float = 0.0
    status_counts: Dict[str, int] = {}
    top_parent_chats: List[Dict] = []
    top_subagents: List[Dict] = []
    top_users: List[Dict] = []
    top_models: List[ModelUsageResponse] = []


class TotalSpendResponse(BaseModel):
    """Admin response model for total spend over a window."""

    total_cost: float = 0.0
    embedded_cost: float = 0.0
    rate_card_cost: float = 0.0
    total_tokens: int = 0
    unpriced_tokens: int = 0
    priced_model_count: int = 0
    unpriced_model_count: int = 0
    start_ts: Optional[int] = None
    end_ts: Optional[int] = None


class DailySpendPoint(BaseModel):
    """Single day of spend for the cost trend chart."""

    date: str
    cost: float = 0.0
    embedded_cost: float = 0.0
    rate_card_cost: float = 0.0


####################
# Analytics Table Class
####################


class AnalyticsTable:
    """
    Database operations for token analytics.
    Handles conversation, daily, and model-level token tracking.
    """

    # ==================
    # Per-request Token Usage Events
    # ==================

    async def record_token_usage_event(
        self,
        user_id: Optional[str],
        source_chat_id: Optional[str],
        attributed_chat_id: Optional[str],
        model_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cache_read_tokens: int = 0,
        message_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
        source_type: str = "chat",
        raw_usage: Optional[dict] = None,
        embedded_cost: Optional[float] = None,
    ) -> bool:
        """Persist one provider usage payload/model-call.

        This is the source of truth for future rebuilds. For subagents,
        source_chat_id is the hidden subagent chat and attributed_chat_id is
        the visible parent chat.
        """
        if not user_id or not attributed_chat_id:
            return False
        try:
            now = int(time.time())
            async with get_db() as db:
                db.add(
                    TokenUsageEvent(
                        id=str(uuid.uuid4()),
                        user_id=user_id,
                        source_chat_id=source_chat_id or attributed_chat_id,
                        attributed_chat_id=attributed_chat_id,
                        message_id=message_id,
                        parent_message_id=parent_message_id,
                        model_id=model_id or "unknown",
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        cache_read_tokens=cache_read_tokens,
                        request_count=1,
                        source_type=source_type or "chat",
                        raw_usage=raw_usage or {},
                        embedded_cost=embedded_cost,
                        created_at=now,
                    )
                )
                await db.commit()
                return True
        except Exception as e:
            # Older DBs may not have the event table until migrations run. Do
            # not block the aggregate counters if event persistence fails.
            log.error(f"Error recording token usage event: {e}")
            return False

    # ==================
    # Conversation Token Usage
    # ==================

    async def get_conversation_token_usage(self, chat_id: str) -> Optional[ConversationTokenUsageResponse]:
        """Get token usage stats for a specific conversation"""
        log.info(f"📊 [Analytics.get_conversation_token_usage] Looking up chat_id={chat_id}")
        try:
            async with get_db() as db:
                record = (
                    await db.execute(
                        sql_text("SELECT * FROM conversation_token_usage WHERE chat_id = :chat_id"),
                        {"chat_id": chat_id},
                    )
                ).mappings().first()
                log.info(f"📊 [Analytics.get_conversation_token_usage] Query result: {record}")
                if record:
                    log.info(f"📊 [Analytics.get_conversation_token_usage] Found record with total_tokens={record['total_tokens']}")
                    return ConversationTokenUsageResponse(**dict(record))
                log.info(f"📊 [Analytics.get_conversation_token_usage] No record found for chat_id={chat_id}")
                return None
        except Exception as e:
            log.error(f"📊 [Analytics.get_conversation_token_usage] Error getting conversation token usage for chat {chat_id}: {e}", exc_info=True)
            return None

    async def update_conversation_token_usage(
        self,
        chat_id: str,
        user_id: str,
        model_id: str,
        token_in: int,
        token_out: int,
        token_total: int,
        cache_read_tokens: int = 0
    ) -> Optional[ConversationTokenUsageResponse]:
        """
        Update or create conversation token usage record.
        Called after each message in a chat.
        """
        log.info(f"📊 [Analytics.update_conversation] Starting: chat_id={chat_id}, user_id={user_id}, model_id={model_id}")
        log.info(f"📊 [Analytics.update_conversation] Tokens: in={token_in}, out={token_out}, total={token_total}")
        try:
            async with get_db() as db:
                now = int(time.time())
                stmt = pg_insert(ConversationTokenUsage).values(
                        id=str(uuid.uuid4()),
                        chat_id=chat_id,
                        user_id=user_id,
                        model_id=model_id,
                        total_input_tokens=token_in,
                        total_output_tokens=token_out,
                        total_tokens=token_total,
                        total_cache_read_tokens=cache_read_tokens,
                        last_input_tokens=token_in,
                        last_output_tokens=token_out,
                        last_cache_read_tokens=cache_read_tokens,
                        message_count=1,
                        created_at=now,
                        updated_at=now
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[ConversationTokenUsage.chat_id],
                    set_={
                        "user_id": user_id,
                        "model_id": model_id or ConversationTokenUsage.model_id,
                        "total_input_tokens": ConversationTokenUsage.total_input_tokens + token_in,
                        "total_output_tokens": ConversationTokenUsage.total_output_tokens + token_out,
                        "total_tokens": ConversationTokenUsage.total_tokens + token_total,
                        "total_cache_read_tokens": ConversationTokenUsage.total_cache_read_tokens + cache_read_tokens,
                        "last_input_tokens": token_in,
                        "last_output_tokens": token_out,
                        "last_cache_read_tokens": cache_read_tokens,
                        "message_count": ConversationTokenUsage.message_count + 1,
                        "updated_at": now,
                    },
                ).returning(ConversationTokenUsage)
                result = await db.execute(stmt)
                await db.commit()
                record = result.scalars().first()
                return ConversationTokenUsageResponse.model_validate(record) if record else None
        except Exception as e:
            log.error(f"📊 [Analytics.update_conversation] ERROR: {e}", exc_info=True)
            return None

    def get_top_chats_by_user(
        self,
        user_id: str,
        year: Optional[int] = None,
        limit: int = 10
    ) -> List[ConversationTokenUsageResponse]:
        """Get user's top conversations by total token count."""
        try:
            with get_db() as db:
                if year:
                    start_ts = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
                    end_ts = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
                    rows = db.execute(
                        sql_text(
                            """
                            WITH yearly AS (
                                SELECT *
                                FROM token_usage_event
                                WHERE user_id = :user_id
                                  AND created_at >= :start_ts
                                  AND created_at < :end_ts
                            ), latest AS (
                                SELECT
                                    attributed_chat_id,
                                    model_id,
                                    prompt_tokens,
                                    completion_tokens,
                                    cache_read_tokens,
                                    ROW_NUMBER() OVER (
                                        PARTITION BY attributed_chat_id
                                        ORDER BY created_at DESC, id DESC
                                    ) AS rn
                                FROM yearly
                            )
                            SELECT
                                y.attributed_chat_id AS chat_id,
                                y.user_id AS user_id,
                                COALESCE(l.model_id, 'unknown') AS model_id,
                                CAST(COALESCE(SUM(y.prompt_tokens), 0) AS INTEGER) AS total_input_tokens,
                                CAST(COALESCE(SUM(y.completion_tokens), 0) AS INTEGER) AS total_output_tokens,
                                CAST(COALESCE(SUM(y.total_tokens), 0) AS INTEGER) AS total_tokens,
                                CAST(COALESCE(SUM(y.cache_read_tokens), 0) AS INTEGER) AS total_cache_read_tokens,
                                CAST(COALESCE(l.prompt_tokens, 0) AS INTEGER) AS last_input_tokens,
                                CAST(COALESCE(l.completion_tokens, 0) AS INTEGER) AS last_output_tokens,
                                CAST(COALESCE(l.cache_read_tokens, 0) AS INTEGER) AS last_cache_read_tokens,
                                CAST(COALESCE(SUM(y.request_count), 0) AS INTEGER) AS message_count,
                                MIN(y.created_at) AS created_at,
                                MAX(y.created_at) AS updated_at
                            FROM yearly y
                            LEFT JOIN latest l ON l.attributed_chat_id = y.attributed_chat_id AND l.rn = 1
                            GROUP BY y.attributed_chat_id
                            ORDER BY total_tokens DESC
                            LIMIT :limit
                            """
                        ),
                        {"user_id": user_id, "start_ts": start_ts, "end_ts": end_ts, "limit": limit},
                    ).mappings().all()
                    return [ConversationTokenUsageResponse(**dict(r)) for r in rows]

                records = db.query(ConversationTokenUsage).filter_by(user_id=user_id).order_by(
                    desc(ConversationTokenUsage.total_tokens)
                ).limit(limit).all()
                return [ConversationTokenUsageResponse.model_validate(r) for r in records]
        except Exception as e:
            log.error(f"Error getting top chats for user {user_id}: {e}")
            return []

    # ==================
    # Daily Token Usage
    # ==================

    async def update_daily_token_usage(
        self,
        user_id: str,
        token_in: int,
        token_out: int,
        token_total: int,
        chat_id: Optional[str] = None,
        cache_read_tokens: int = 0
    ) -> bool:
        """
        Update daily token aggregation for a user.
        Called after each message.
        """
        try:
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            now = int(time.time())

            async with get_db() as db:
                stmt = pg_insert(DailyTokenUsage).values(
                        id=str(uuid.uuid4()),
                        user_id=user_id,
                        date=today,
                        total_input_tokens=token_in,
                        total_output_tokens=token_out,
                        total_tokens=token_total,
                        total_cache_read_tokens=cache_read_tokens,
                        conversation_count=1,
                        message_count=1,
                        created_at=now,
                        updated_at=now
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[DailyTokenUsage.user_id, DailyTokenUsage.date],
                    set_={
                        "total_input_tokens": DailyTokenUsage.total_input_tokens + token_in,
                        "total_output_tokens": DailyTokenUsage.total_output_tokens + token_out,
                        "total_tokens": DailyTokenUsage.total_tokens + token_total,
                        "total_cache_read_tokens": DailyTokenUsage.total_cache_read_tokens + cache_read_tokens,
                        "message_count": DailyTokenUsage.message_count + 1,
                        "updated_at": now,
                    },
                )
                await db.execute(stmt)
                await db.commit()
                log.debug(f"Updated daily token usage for user {user_id} on {today}")
                return True
        except Exception as e:
            log.error(f"Error updating daily token usage for user {user_id}: {e}")
            return False

    def get_heatmap_data(
        self,
        user_id: str,
        year: Optional[int] = None
    ) -> HeatmapResponse:
        """
        Get daily token usage data for heatmap visualization.
        Returns all days of the specified year with token counts.
        """
        if year is None:
            year = datetime.now(timezone.utc).year

        try:
            with get_db() as db:
                # Query all daily records for the year
                year_start = f"{year}-01-01"
                year_end = f"{year}-12-31"

                records = db.query(DailyTokenUsage).filter(
                    DailyTokenUsage.user_id == user_id,
                    DailyTokenUsage.date >= year_start,
                    DailyTokenUsage.date <= year_end
                ).all()

                # Build date -> tokens map
                date_tokens = {r.date: r.total_tokens for r in records}
                max_tokens = max(date_tokens.values()) if date_tokens else 0

                # Calculate levels (0-4 scale)
                def calculate_level(tokens: int) -> int:
                    if tokens == 0:
                        return 0
                    if max_tokens == 0:
                        return 0
                    # Quartile-based levels
                    ratio = tokens / max_tokens
                    if ratio < 0.25:
                        return 1
                    elif ratio < 0.5:
                        return 2
                    elif ratio < 0.75:
                        return 3
                    else:
                        return 4

                # Generate data points for all days in the year
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
                    current_date = current_date.replace(day=current_date.day + 1) if current_date.day < 28 else \
                                   (current_date + __import__('datetime').timedelta(days=1))

                # Fix the date iteration using timedelta properly
                from datetime import timedelta
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
            log.error(f"Error getting heatmap data for user {user_id}: {e}")
            return HeatmapResponse(year=year, data=[], max_tokens=0, total_days_active=0)

    def get_most_active_day(
        self,
        user_id: str,
        year: Optional[int] = None
    ) -> Optional[Dict]:
        """Get the user's most active day by token count"""
        if year is None:
            year = datetime.now(timezone.utc).year

        try:
            with get_db() as db:
                year_start = f"{year}-01-01"
                year_end = f"{year}-12-31"

                record = db.query(DailyTokenUsage).filter(
                    DailyTokenUsage.user_id == user_id,
                    DailyTokenUsage.date >= year_start,
                    DailyTokenUsage.date <= year_end
                ).order_by(desc(DailyTokenUsage.total_tokens)).first()

                if record:
                    # Parse date to get day of week
                    date_obj = datetime.strptime(record.date, '%Y-%m-%d')
                    return {
                        "date": record.date,
                        "tokens": record.total_tokens,
                        "messages": record.message_count,
                        "day_of_week": date_obj.strftime('%A')
                    }
                return None
        except Exception as e:
            log.error(f"Error getting most active day for user {user_id}: {e}")
            return None

    # ==================
    # Model Token Usage
    # ==================

    async def update_model_token_usage(
        self,
        user_id: Optional[str],
        model_id: str,
        token_in: int,
        token_out: int,
        token_total: int,
        cache_read_tokens: int = 0
    ) -> bool:
        """
        Update model token usage.
        Updates both per-user and global (user_id=None) records.
        """
        try:
            now = int(time.time())

            async with get_db() as db:
                if user_id:
                    user_stmt = pg_insert(ModelTokenUsage).values(
                            id=str(uuid.uuid4()),
                            user_id=user_id,
                            model_id=model_id,
                            total_input_tokens=token_in,
                            total_output_tokens=token_out,
                            total_tokens=token_total,
                            total_cache_read_tokens=cache_read_tokens,
                            conversation_count=1,
                            message_count=1,
                            created_at=now,
                            updated_at=now
                    )
                    user_stmt = user_stmt.on_conflict_do_update(
                        index_elements=[ModelTokenUsage.user_id, ModelTokenUsage.model_id],
                        index_where=ModelTokenUsage.user_id.is_not(None),
                        set_={
                            "total_input_tokens": ModelTokenUsage.total_input_tokens + token_in,
                            "total_output_tokens": ModelTokenUsage.total_output_tokens + token_out,
                            "total_tokens": ModelTokenUsage.total_tokens + token_total,
                            "total_cache_read_tokens": ModelTokenUsage.total_cache_read_tokens + cache_read_tokens,
                            "message_count": ModelTokenUsage.message_count + 1,
                            "updated_at": now,
                        },
                    )
                    await db.execute(user_stmt)

                global_stmt = pg_insert(ModelTokenUsage).values(
                        id=str(uuid.uuid4()),
                        user_id=None,
                        model_id=model_id,
                        total_input_tokens=token_in,
                        total_output_tokens=token_out,
                        total_tokens=token_total,
                        total_cache_read_tokens=cache_read_tokens,
                        conversation_count=1,
                        message_count=1,
                        created_at=now,
                        updated_at=now
                )
                global_stmt = global_stmt.on_conflict_do_update(
                    index_elements=[ModelTokenUsage.model_id],
                    index_where=ModelTokenUsage.user_id.is_(None),
                    set_={
                        "total_input_tokens": ModelTokenUsage.total_input_tokens + token_in,
                        "total_output_tokens": ModelTokenUsage.total_output_tokens + token_out,
                        "total_tokens": ModelTokenUsage.total_tokens + token_total,
                        "total_cache_read_tokens": ModelTokenUsage.total_cache_read_tokens + cache_read_tokens,
                        "message_count": ModelTokenUsage.message_count + 1,
                        "updated_at": now,
                    },
                )
                await db.execute(global_stmt)
                await db.commit()
                log.debug(f"Updated model token usage for model {model_id}")
                return True
        except Exception as e:
            log.error(f"Error updating model token usage for model {model_id}: {e}")
            return False

    def get_model_usage_by_user(
        self,
        user_id: str,
        year: Optional[int] = None
    ) -> List[ModelUsageResponse]:
        """Get per-model token usage breakdown for a user."""
        try:
            with get_db() as db:
                if year:
                    start_ts = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
                    end_ts = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
                    rows = db.execute(
                        sql_text(
                            """
                            SELECT
                                COALESCE(NULLIF(model_id, ''), 'unknown') AS model_id,
                                CAST(COALESCE(SUM(prompt_tokens), 0) AS INTEGER) AS total_input_tokens,
                                CAST(COALESCE(SUM(completion_tokens), 0) AS INTEGER) AS total_output_tokens,
                                CAST(COALESCE(SUM(total_tokens), 0) AS INTEGER) AS total_tokens,
                                CAST(COALESCE(SUM(cache_read_tokens), 0) AS INTEGER) AS total_cache_read_tokens,
                                COUNT(DISTINCT attributed_chat_id) AS conversation_count,
                                CAST(COALESCE(SUM(request_count), 0) AS INTEGER) AS message_count
                            FROM token_usage_event
                            WHERE user_id = :user_id
                              AND created_at >= :start_ts
                              AND created_at < :end_ts
                            GROUP BY model_id
                            ORDER BY total_tokens DESC
                            """
                        ),
                        {"user_id": user_id, "start_ts": start_ts, "end_ts": end_ts},
                    ).mappings().all()
                    total_all = sum(int(r["total_tokens"] or 0) for r in rows)
                    return [
                        ModelUsageResponse(
                            model_id=r["model_id"],
                            total_input_tokens=int(r["total_input_tokens"] or 0),
                            total_output_tokens=int(r["total_output_tokens"] or 0),
                            total_tokens=int(r["total_tokens"] or 0),
                            total_cache_read_tokens=int(r["total_cache_read_tokens"] or 0),
                            conversation_count=int(r["conversation_count"] or 0),
                            message_count=int(r["message_count"] or 0),
                            percentage=round((int(r["total_tokens"] or 0) / total_all * 100), 1) if total_all else 0.0,
                        )
                        for r in rows
                    ]

                records = db.query(ModelTokenUsage).filter_by(user_id=user_id).all()

                if not records:
                    return []

                # Calculate total for percentages
                total_all = sum(r.total_tokens for r in records)

                result = []
                for r in records:
                    percentage = (r.total_tokens / total_all * 100) if total_all > 0 else 0
                    result.append(ModelUsageResponse(
                        model_id=r.model_id,
                        total_input_tokens=r.total_input_tokens,
                        total_output_tokens=r.total_output_tokens,
                        total_tokens=r.total_tokens,
                        total_cache_read_tokens=getattr(r, "total_cache_read_tokens", 0) or 0,
                        conversation_count=r.conversation_count,
                        message_count=r.message_count,
                        percentage=round(percentage, 1)
                    ))

                # Sort by total tokens descending
                result.sort(key=lambda x: x.total_tokens, reverse=True)
                return result
        except Exception as e:
            log.error(f"Error getting model usage for user {user_id}: {e}")
            return []

    def get_favorite_model(
        self,
        user_id: str,
        year: Optional[int] = None
    ) -> Optional[Dict]:
        """Get user's most-used model"""
        try:
            usage = self.get_model_usage_by_user(user_id, year)
            if usage:
                top = usage[0]
                return {
                    "model_id": top.model_id,
                    "total_tokens": top.total_tokens,
                    "percentage": top.percentage,
                }
            return None
        except Exception as e:
            log.error(f"Error getting favorite model for user {user_id}: {e}")
            return None

    def get_global_model_usage(self, limit: int = 10, year: Optional[int] = None) -> List[ModelUsageResponse]:
        """Get site-wide model usage breakdown.

        With a year, derive from token_usage_event so admin Wrapped includes
        raw usage and legacy subagent aggregates. Without a year, fall back to
        the existing all-time model_token_usage aggregate.
        """
        try:
            with get_db() as db:
                if year is not None:
                    year_start_ts = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
                    year_end_ts = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
                    rows = db.execute(
                        sql_text(
                            """
                            SELECT
                                COALESCE(NULLIF(model_id, ''), 'unknown') AS model_id,
                                CAST(COALESCE(SUM(prompt_tokens), 0) AS INTEGER) AS total_input_tokens,
                                CAST(COALESCE(SUM(completion_tokens), 0) AS INTEGER) AS total_output_tokens,
                                CAST(COALESCE(SUM(total_tokens), 0) AS INTEGER) AS total_tokens,
                                CAST(COALESCE(SUM(cache_read_tokens), 0) AS INTEGER) AS total_cache_read_tokens,
                                CAST(COALESCE(SUM(request_count), 0) AS INTEGER) AS message_count,
                                COUNT(DISTINCT attributed_chat_id) AS conversation_count
                            FROM token_usage_event
                            WHERE user_id NOT LIKE 'shared-%'
                              AND created_at >= :start_ts
                              AND created_at < :end_ts
                            GROUP BY model_id
                            ORDER BY total_tokens DESC
                            LIMIT :limit
                            """
                        ),
                        {"start_ts": year_start_ts, "end_ts": year_end_ts, "limit": limit},
                    ).mappings().all()

                    total_all = sum(int(r["total_tokens"] or 0) for r in rows)
                    return [
                        ModelUsageResponse(
                            model_id=r["model_id"],
                            total_input_tokens=int(r["total_input_tokens"] or 0),
                            total_output_tokens=int(r["total_output_tokens"] or 0),
                            total_tokens=int(r["total_tokens"] or 0),
                            total_cache_read_tokens=int(r["total_cache_read_tokens"] or 0),
                            conversation_count=int(r["conversation_count"] or 0),
                            message_count=int(r["message_count"] or 0),
                            percentage=round((int(r["total_tokens"] or 0) / total_all * 100), 1) if total_all else 0.0,
                        )
                        for r in rows
                    ]

                records = db.query(ModelTokenUsage).filter_by(
                    user_id=None
                ).order_by(desc(ModelTokenUsage.total_tokens)).limit(limit).all()

                if not records:
                    return []

                # Calculate total for percentages
                total_all = sum(r.total_tokens for r in records)

                result = []
                for r in records:
                    percentage = (r.total_tokens / total_all * 100) if total_all > 0 else 0
                    result.append(ModelUsageResponse(
                        model_id=r.model_id,
                        total_input_tokens=r.total_input_tokens,
                        total_output_tokens=r.total_output_tokens,
                        total_tokens=r.total_tokens,
                        total_cache_read_tokens=getattr(r, "total_cache_read_tokens", 0) or 0,
                        conversation_count=r.conversation_count,
                        message_count=r.message_count,
                        percentage=round(percentage, 1)
                    ))

                return result
        except Exception as e:
            log.error(f"Error getting global model usage: {e}")
            return []

    def get_global_user_usage(
        self,
        year: Optional[int] = None,
        limit: int = 100,
    ) -> List[UserUsageResponse]:
        """Get per-user usage leaderboard for admins."""
        if year is None:
            year = datetime.now(timezone.utc).year

        try:
            with get_db() as db:
                year_start = f"{year}-01-01"
                year_end = f"{year}-12-31"
                year_start_ts = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
                year_end_ts = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())

                daily = db.query(
                    DailyTokenUsage.user_id.label("user_id"),
                    func.sum(DailyTokenUsage.total_input_tokens).label("total_input"),
                    func.sum(DailyTokenUsage.total_output_tokens).label("total_output"),
                    func.sum(DailyTokenUsage.total_tokens).label("total"),
                    func.sum(DailyTokenUsage.total_cache_read_tokens).label("total_cache_read"),
                    func.sum(DailyTokenUsage.message_count).label("messages"),
                    func.count(DailyTokenUsage.id).label("days_active"),
                ).filter(
                    DailyTokenUsage.user_id.notlike("shared-%"),
                    DailyTokenUsage.date >= year_start,
                    DailyTokenUsage.date <= year_end,
                ).group_by(DailyTokenUsage.user_id).subquery()

                convs = db.query(
                    ConversationTokenUsage.user_id.label("user_id"),
                    func.count(ConversationTokenUsage.id).label("conversations"),
                ).filter(
                    ConversationTokenUsage.user_id.notlike("shared-%"),
                    ConversationTokenUsage.created_at >= year_start_ts,
                    ConversationTokenUsage.created_at < year_end_ts,
                ).group_by(ConversationTokenUsage.user_id).subquery()

                rows = db.query(
                    daily.c.user_id,
                    User.name,
                    User.email,
                    User.role,
                    User.last_active_at,
                    daily.c.total_input,
                    daily.c.total_output,
                    daily.c.total,
                    daily.c.total_cache_read,
                    daily.c.messages,
                    daily.c.days_active,
                    convs.c.conversations,
                ).outerjoin(
                    User, User.id == daily.c.user_id
                ).outerjoin(
                    convs, convs.c.user_id == daily.c.user_id
                ).order_by(desc(daily.c.total)).limit(limit).all()

                result = []
                for row in rows:
                    total = int(row.total or 0)
                    total_input = int(row.total_input or 0)
                    total_cache = int(row.total_cache_read or 0)
                    messages = int(row.messages or 0)
                    days_active = int(row.days_active or 0)
                    result.append(UserUsageResponse(
                        user_id=row.user_id,
                        name=row.name,
                        email=row.email,
                        role=row.role,
                        last_active_at=row.last_active_at,
                        total_input_tokens=total_input,
                        total_output_tokens=int(row.total_output or 0),
                        total_tokens=total,
                        total_cache_read_tokens=total_cache,
                        conversation_count=int(row.conversations or 0),
                        message_count=messages,
                        days_active=days_active,
                        avg_tokens_per_active_day=(total // days_active) if days_active else 0,
                        avg_tokens_per_message=(total // messages) if messages else 0,
                        cache_read_rate=round((total_cache / total_input * 100), 1) if total_input else 0.0,
                    ))
                return result
        except Exception as e:
            log.error(f"Error getting global user usage: {e}")
            return []

    def get_global_subagent_usage(self, year: Optional[int] = None) -> SubagentAnalyticsResponse:
        """Get admin subagent usage insights."""
        if year is None:
            year = datetime.now(timezone.utc).year

        try:
            with get_db() as db:
                year_start_ts = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
                year_end_ts = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())

                base_filter = """
                    e.source_chat_id IS NOT NULL
                    AND e.attributed_chat_id IS NOT NULL
                    AND e.source_chat_id != e.attributed_chat_id
                    AND e.created_at >= :start_ts
                    AND e.created_at < :end_ts
                    AND e.user_id NOT LIKE 'shared-%'
                """
                params = {"start_ts": year_start_ts, "end_ts": year_end_ts}

                totals = db.execute(
                    sql_text(
                        f"""
                        SELECT
                            COUNT(DISTINCT e.source_chat_id) AS subagent_chats,
                            COUNT(DISTINCT e.attributed_chat_id) AS parent_chats,
                            COALESCE(SUM(e.request_count), 0) AS requests,
                            COALESCE(SUM(e.prompt_tokens), 0) AS input_tokens,
                            COALESCE(SUM(e.completion_tokens), 0) AS output_tokens,
                            COALESCE(SUM(e.total_tokens), 0) AS total_tokens,
                            COALESCE(SUM(e.cache_read_tokens), 0) AS cache_tokens
                        FROM token_usage_event e
                        WHERE {base_filter}
                        """
                    ),
                    params,
                ).mappings().first() or {}

                site_total = db.execute(
                    sql_text(
                        """
                        SELECT COALESCE(SUM(total_tokens), 0) AS total
                        FROM token_usage_event
                        WHERE created_at >= :start_ts
                          AND created_at < :end_ts
                          AND user_id NOT LIKE 'shared-%'
                        """
                    ),
                    params,
                ).mappings().first()
                site_total_tokens = int((site_total or {}).get("total") or 0)
                subagent_total_tokens = int(totals.get("total_tokens") or 0)
                subagent_chats = int(totals.get("subagent_chats") or 0)
                parent_chats = int(totals.get("parent_chats") or 0)
                requests = int(totals.get("requests") or 0)

                top_parent_chats = [dict(row) for row in db.execute(
                    sql_text(
                        f"""
                        SELECT
                            e.attributed_chat_id AS chat_id,
                            COALESCE(c.title, e.attributed_chat_id) AS title,
                            COUNT(DISTINCT e.source_chat_id) AS subagent_count,
                            COALESCE(SUM(e.request_count), 0) AS request_count,
                            COALESCE(SUM(e.prompt_tokens), 0) AS total_input_tokens,
                            COALESCE(SUM(e.completion_tokens), 0) AS total_output_tokens,
                            COALESCE(SUM(e.total_tokens), 0) AS total_tokens,
                            COALESCE(SUM(e.cache_read_tokens), 0) AS total_cache_read_tokens
                        FROM token_usage_event e
                        LEFT JOIN chat c ON c.id = e.attributed_chat_id
                        WHERE {base_filter}
                        GROUP BY e.attributed_chat_id
                        ORDER BY total_tokens DESC
                        LIMIT 20
                        """
                    ),
                    params,
                ).mappings().all()]

                top_subagents = [dict(row) for row in db.execute(
                    sql_text(
                        f"""
                        SELECT
                            e.source_chat_id AS subagent_chat_id,
                            COALESCE(sc.title, e.source_chat_id) AS title,
                            e.attributed_chat_id AS parent_chat_id,
                            COALESCE(pc.title, e.attributed_chat_id) AS parent_title,
                            COALESCE(SUM(e.request_count), 0) AS request_count,
                            COALESCE(SUM(e.prompt_tokens), 0) AS total_input_tokens,
                            COALESCE(SUM(e.completion_tokens), 0) AS total_output_tokens,
                            COALESCE(SUM(e.total_tokens), 0) AS total_tokens,
                            COALESCE(SUM(e.cache_read_tokens), 0) AS total_cache_read_tokens,
                            MAX(e.source_type) AS source_type
                        FROM token_usage_event e
                        LEFT JOIN chat sc ON sc.id = e.source_chat_id
                        LEFT JOIN chat pc ON pc.id = e.attributed_chat_id
                        WHERE {base_filter}
                        GROUP BY e.source_chat_id, e.attributed_chat_id
                        ORDER BY total_tokens DESC
                        LIMIT 30
                        """
                    ),
                    params,
                ).mappings().all()]

                top_users = [dict(row) for row in db.execute(
                    sql_text(
                        f"""
                        SELECT
                            e.user_id,
                            u.name,
                            u.email,
                            COUNT(DISTINCT e.source_chat_id) AS subagent_count,
                            COUNT(DISTINCT e.attributed_chat_id) AS parent_chat_count,
                            COALESCE(SUM(e.request_count), 0) AS request_count,
                            COALESCE(SUM(e.total_tokens), 0) AS total_tokens,
                            COALESCE(SUM(e.cache_read_tokens), 0) AS total_cache_read_tokens
                        FROM token_usage_event e
                        LEFT JOIN user u ON u.id = e.user_id
                        WHERE {base_filter}
                        GROUP BY e.user_id
                        ORDER BY total_tokens DESC
                        LIMIT 20
                        """
                    ),
                    params,
                ).mappings().all()]

                model_rows = db.execute(
                    sql_text(
                        f"""
                        SELECT
                            e.model_id,
                            COALESCE(SUM(e.prompt_tokens), 0) AS total_input_tokens,
                            COALESCE(SUM(e.completion_tokens), 0) AS total_output_tokens,
                            COALESCE(SUM(e.total_tokens), 0) AS total_tokens,
                            COALESCE(SUM(e.cache_read_tokens), 0) AS total_cache_read_tokens,
                            COUNT(DISTINCT e.attributed_chat_id) AS conversation_count,
                            COALESCE(SUM(e.request_count), 0) AS message_count
                        FROM token_usage_event e
                        WHERE {base_filter}
                        GROUP BY e.model_id
                        ORDER BY total_tokens DESC
                        LIMIT 15
                        """
                    ),
                    params,
                ).mappings().all()
                model_total = sum(int(row["total_tokens"] or 0) for row in model_rows)
                top_models = [
                    ModelUsageResponse(
                        model_id=row["model_id"],
                        total_input_tokens=int(row["total_input_tokens"] or 0),
                        total_output_tokens=int(row["total_output_tokens"] or 0),
                        total_tokens=int(row["total_tokens"] or 0),
                        total_cache_read_tokens=int(row["total_cache_read_tokens"] or 0),
                        conversation_count=int(row["conversation_count"] or 0),
                        message_count=int(row["message_count"] or 0),
                        percentage=round((int(row["total_tokens"] or 0) / model_total * 100), 1) if model_total else 0.0,
                    )
                    for row in model_rows
                ]

                status_counts = {}
                try:
                    for row in db.execute(
                        sql_text(
                            """
                            SELECT
                                COALESCE(j.value->>'status', 'unknown') AS status,
                                COUNT(*) AS count
                            FROM chat_message cm
                            JOIN chat c ON c.id = cm.chat_id
                            JOIN LATERAL jsonb_each(COALESCE(cm.meta->'subagent_runs', '{}'::jsonb)) j(key, value) ON true
                            WHERE c.user_id NOT LIKE 'shared-%'
                              AND jsonb_typeof(COALESCE(cm.meta->'subagent_runs', '{}'::jsonb)) = 'object'
                              AND COALESCE((j.value->>'started_at')::bigint, cm.timestamp, c.updated_at, c.created_at, 0) >= :start_ts
                              AND COALESCE((j.value->>'started_at')::bigint, cm.timestamp, c.updated_at, c.created_at, 0) < :end_ts
                            GROUP BY status
                            """
                        ),
                        params,
                    ).mappings().all():
                        status_counts[str(row["status"])] = int(row["count"] or 0)
                except Exception as e:
                    log.debug(f"Error getting subagent status counts: {e}")

                return SubagentAnalyticsResponse(
                    year=year,
                    total_subagent_chats=subagent_chats,
                    parent_chat_count=parent_chats,
                    request_count=requests,
                    total_input_tokens=int(totals.get("input_tokens") or 0),
                    total_output_tokens=int(totals.get("output_tokens") or 0),
                    total_tokens=subagent_total_tokens,
                    total_cache_read_tokens=int(totals.get("cache_tokens") or 0),
                    token_share_percent=round((subagent_total_tokens / site_total_tokens * 100), 1) if site_total_tokens else 0.0,
                    avg_tokens_per_subagent=(subagent_total_tokens // subagent_chats) if subagent_chats else 0,
                    avg_requests_per_subagent=round((requests / subagent_chats), 1) if subagent_chats else 0.0,
                    avg_subagents_per_parent=round((subagent_chats / parent_chats), 1) if parent_chats else 0.0,
                    status_counts=status_counts,
                    top_parent_chats=top_parent_chats,
                    top_subagents=top_subagents,
                    top_users=top_users,
                    top_models=top_models,
                )
        except Exception as e:
            log.error(f"Error getting global subagent usage: {e}")
            return SubagentAnalyticsResponse(year=year)

    # ==================
    # Wrapped Summary
    # ==================

    def get_user_wrapped(
        self,
        user_id: str,
        year: Optional[int] = None
    ) -> WrappedSummaryResponse:
        """Get comprehensive wrapped summary for a user"""
        if year is None:
            year = datetime.now(timezone.utc).year

        try:
            with get_db() as db:
                year_start = f"{year}-01-01"
                year_end = f"{year}-12-31"
                year_start_ts = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
                year_end_ts = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())

                # Aggregate daily stats
                daily_stats = db.query(
                    func.sum(DailyTokenUsage.total_input_tokens).label('total_input'),
                    func.sum(DailyTokenUsage.total_output_tokens).label('total_output'),
                    func.sum(DailyTokenUsage.total_tokens).label('total'),
                    func.sum(DailyTokenUsage.total_cache_read_tokens).label('total_cache_read'),
                    func.sum(DailyTokenUsage.message_count).label('messages'),
                    func.count(DailyTokenUsage.id).label('days_active')
                ).filter(
                    DailyTokenUsage.user_id == user_id,
                    DailyTokenUsage.date >= year_start,
                    DailyTokenUsage.date <= year_end
                ).first()

                # Count conversations
                conv_count = db.query(func.count(ConversationTokenUsage.id)).filter(
                    ConversationTokenUsage.user_id == user_id,
                    ConversationTokenUsage.created_at >= year_start_ts,
                    ConversationTokenUsage.created_at < year_end_ts
                ).scalar() or 0

                # Get top chats
                top_chats_records = db.query(ConversationTokenUsage).filter(
                    ConversationTokenUsage.user_id == user_id,
                    ConversationTokenUsage.created_at >= year_start_ts,
                    ConversationTokenUsage.created_at < year_end_ts
                ).order_by(desc(ConversationTokenUsage.total_tokens)).limit(10).all()

                top_chats = [
                    TopChatResponse(
                        chat_id=r.chat_id,
                        model_id=r.model_id,
                        total_tokens=r.total_tokens,
                        total_input_tokens=r.total_input_tokens,
                        total_output_tokens=r.total_output_tokens,
                        total_cache_read_tokens=getattr(r, "total_cache_read_tokens", 0) or 0,
                        last_cache_read_tokens=getattr(r, "last_cache_read_tokens", 0) or 0,
                        message_count=r.message_count
                    ) for r in top_chats_records
                ]

                # Get most active day and favorite model
                most_active = self.get_most_active_day(user_id, year)
                favorite = self.get_favorite_model(user_id, year)

                return WrappedSummaryResponse(
                    year=year,
                    total_conversations=conv_count,
                    total_messages=daily_stats.messages or 0,
                    total_input_tokens=daily_stats.total_input or 0,
                    total_output_tokens=daily_stats.total_output or 0,
                    total_tokens=daily_stats.total or 0,
                    total_cache_read_tokens=daily_stats.total_cache_read or 0,
                    days_active=daily_stats.days_active or 0,
                    most_active_day=most_active,
                    favorite_model=favorite,
                    top_chats=top_chats
                )
        except Exception as e:
            log.error(f"Error getting user wrapped for user {user_id}: {e}")
            return WrappedSummaryResponse(year=year)

    def get_global_wrapped(self, year: Optional[int] = None) -> GlobalWrappedResponse:
        """Get site-wide wrapped statistics (admin only)"""
        if year is None:
            year = datetime.now(timezone.utc).year

        try:
            with get_db() as db:
                year_start = f"{year}-01-01"
                year_end = f"{year}-12-31"
                year_start_ts = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
                year_end_ts = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp())

                # Count unique active users
                users_active = db.query(func.count(func.distinct(DailyTokenUsage.user_id))).filter(
                    DailyTokenUsage.date >= year_start,
                    DailyTokenUsage.date <= year_end
                ).scalar() or 0

                # Total conversations
                total_convs = db.query(func.count(ConversationTokenUsage.id)).filter(
                    ConversationTokenUsage.created_at >= year_start_ts,
                    ConversationTokenUsage.created_at < year_end_ts
                ).scalar() or 0

                # Aggregate totals
                daily_totals = db.query(
                    func.sum(DailyTokenUsage.total_tokens).label('total'),
                    func.sum(DailyTokenUsage.total_cache_read_tokens).label('total_cache_read'),
                    func.sum(DailyTokenUsage.message_count).label('messages')
                ).filter(
                    DailyTokenUsage.date >= year_start,
                    DailyTokenUsage.date <= year_end
                ).first()

                # Busiest day across all users
                busiest_day = db.query(
                    DailyTokenUsage.date,
                    func.sum(DailyTokenUsage.total_tokens).label('total')
                ).filter(
                    DailyTokenUsage.date >= year_start,
                    DailyTokenUsage.date <= year_end
                ).group_by(DailyTokenUsage.date).order_by(desc('total')).first()

                busiest = None
                if busiest_day:
                    date_obj = datetime.strptime(busiest_day.date, '%Y-%m-%d')
                    busiest = {
                        "date": busiest_day.date,
                        "tokens": busiest_day.total,
                        "day_of_week": date_obj.strftime('%A')
                    }

                # Top models
                top_models = self.get_global_model_usage(limit=10, year=year)

                return GlobalWrappedResponse(
                    year=year,
                    total_users_active=users_active,
                    total_conversations=total_convs,
                    total_messages=daily_totals.messages or 0,
                    total_tokens=daily_totals.total or 0,
                    total_cache_read_tokens=daily_totals.total_cache_read or 0,
                    top_models=top_models,
                    busiest_day=busiest
                )
        except Exception as e:
            log.error(f"Error getting global wrapped: {e}")
            return GlobalWrappedResponse(year=year)

    # ==================
    # Utility Methods
    # ==================

    def increment_conversation_count_for_day(self, user_id: str, date: str) -> bool:
        """Increment the conversation count for a specific day"""
        try:
            with get_db() as db:
                record = db.query(DailyTokenUsage).filter_by(
                    user_id=user_id,
                    date=date
                ).first()

                if record:
                    record.conversation_count += 1
                    db.commit()
                    return True
                return False
        except Exception as e:
            log.error(f"Error incrementing conversation count: {e}")
            return False

    def increment_model_conversation_count(
        self,
        user_id: Optional[str],
        model_id: str
    ) -> bool:
        """Increment the conversation count for a model"""
        try:
            with get_db() as db:
                # User-specific
                if user_id:
                    user_record = db.query(ModelTokenUsage).filter_by(
                        user_id=user_id,
                        model_id=model_id
                    ).first()
                    if user_record:
                        user_record.conversation_count += 1

                # Global
                global_record = db.query(ModelTokenUsage).filter_by(
                    user_id=None,
                    model_id=model_id
                ).first()
                if global_record:
                    global_record.conversation_count += 1

                db.commit()
                return True
        except Exception as e:
            log.error(f"Error incrementing model conversation count: {e}")
            return False

    # ==================
    # Cost aggregation (USD)
    # ==================

    # Shared per-(dimension, model_id) cost aggregate. Typed columns ONLY — the
    # embedded/rate-card split is the typed predicate ``embedded_cost IS NULL``,
    # so the read path never parses JSON.
    _COST_SELECT = """
        COALESCE(SUM(total_tokens), 0) AS total_tokens,
        COALESCE(SUM(embedded_cost), 0) AS embedded_cost,
        COALESCE(SUM(CASE WHEN embedded_cost IS NULL
            THEN GREATEST(prompt_tokens - cache_read_tokens, 0) ELSE 0 END), 0) AS rc_prompt,
        COALESCE(SUM(CASE WHEN embedded_cost IS NULL
            THEN cache_read_tokens ELSE 0 END), 0) AS rc_cache_read,
        COALESCE(SUM(CASE WHEN embedded_cost IS NULL
            THEN completion_tokens ELSE 0 END), 0) AS rc_completion,
        COALESCE(SUM(CASE WHEN embedded_cost IS NULL
            THEN total_tokens ELSE 0 END), 0) AS rc_total_tokens
    """

    def _cost_rows(self, db, dim_expr: str, start_ts: int, end_ts: int,
                   extra_where: str = "", params: Optional[dict] = None) -> list:
        """Run the shared cost group-by and return mapping rows with a 'dim' key."""
        where = "user_id NOT LIKE 'shared-%' AND created_at >= :start_ts AND created_at < :end_ts"
        if extra_where:
            where += f" AND {extra_where}"
        sql = f"""
            SELECT {dim_expr} AS dim, model_id,
            {self._COST_SELECT}
            FROM token_usage_event
            WHERE {where}
            GROUP BY {dim_expr}, model_id
        """
        q = {"start_ts": start_ts, "end_ts": end_ts}
        if params:
            q.update(params)
        return [dict(r) for r in db.execute(sql_text(sql), q).mappings().all()]

    def get_global_model_cost(self, start_ts: int, end_ts: int, limit: int = 50) -> List[ModelUsageResponse]:
        """Per-model token + cost breakdown over a window (admin)."""
        from open_webui.utils.pricing import fold_cost_rows, get_cached_pricing_map
        try:
            with get_db() as db:
                # token columns per model (for the response), plus cost via fold.
                rows = self._cost_rows(db, "model_id", start_ts, end_ts)
                # also need input/output split per model for the UI
                tok = db.execute(sql_text(
                    """
                    SELECT COALESCE(NULLIF(model_id, ''), 'unknown') AS model_id,
                        CAST(COALESCE(SUM(prompt_tokens), 0) AS BIGINT) AS total_input_tokens,
                        CAST(COALESCE(SUM(completion_tokens), 0) AS BIGINT) AS total_output_tokens,
                        CAST(COALESCE(SUM(total_tokens), 0) AS BIGINT) AS total_tokens,
                        CAST(COALESCE(SUM(cache_read_tokens), 0) AS BIGINT) AS total_cache_read_tokens,
                        CAST(COALESCE(SUM(request_count), 0) AS BIGINT) AS message_count,
                        COUNT(DISTINCT attributed_chat_id) AS conversation_count
                    FROM token_usage_event
                    WHERE user_id NOT LIKE 'shared-%' AND created_at >= :start_ts AND created_at < :end_ts
                    GROUP BY model_id
                    """
                ), {"start_ts": start_ts, "end_ts": end_ts}).mappings().all()

                pricing_map = get_cached_pricing_map()
                # fold cost per model_id (dim == model_id)
                folded = fold_cost_rows(
                    [{**r, "dim": r["model_id"]} for r in rows], pricing_map
                )
                total_all = sum(int(t["total_tokens"] or 0) for t in tok)
                result = []
                for t in tok:
                    mid = t["model_id"]
                    c = folded.get(mid, {})
                    result.append(ModelUsageResponse(
                        model_id=mid,
                        total_input_tokens=int(t["total_input_tokens"] or 0),
                        total_output_tokens=int(t["total_output_tokens"] or 0),
                        total_tokens=int(t["total_tokens"] or 0),
                        total_cache_read_tokens=int(t["total_cache_read_tokens"] or 0),
                        conversation_count=int(t["conversation_count"] or 0),
                        message_count=int(t["message_count"] or 0),
                        percentage=round((int(t["total_tokens"] or 0) / total_all * 100), 1) if total_all else 0.0,
                        cost=round(c.get("cost", 0.0), 6),
                        unpriced_tokens=int(c.get("unpriced_tokens", 0)),
                        rate_source=c.get("rate_source"),
                    ))
                result.sort(key=lambda x: x.cost, reverse=True)
                return result[:limit]
        except Exception as e:
            log.error(f"Error getting global model cost: {e}")
            return []

    def get_user_cost_map(self, start_ts: int, end_ts: int) -> Dict[str, Dict]:
        """Per-user cost folded from token_usage_event over a window.

        Returns {user_id: {cost, unpriced_tokens, ...}} for joining onto the
        existing per-user token leaderboard.
        """
        from open_webui.utils.pricing import fold_cost_rows, get_cached_pricing_map
        try:
            with get_db() as db:
                rows = self._cost_rows(db, "user_id", start_ts, end_ts)
                return fold_cost_rows(rows, get_cached_pricing_map())
        except Exception as e:
            log.error(f"Error getting user cost map: {e}")
            return {}

    def get_total_spend(self, start_ts: int, end_ts: int) -> TotalSpendResponse:
        """Site-wide spend KPI over a window (admin)."""
        from open_webui.utils.pricing import fold_cost_rows, get_cached_pricing_map
        try:
            with get_db() as db:
                rows = self._cost_rows(db, "model_id", start_ts, end_ts)
                pricing_map = get_cached_pricing_map()
                folded = fold_cost_rows(
                    [{**r, "dim": r["model_id"]} for r in rows], pricing_map
                )
                total_cost = sum(b["cost"] for b in folded.values())
                embedded = sum(b["embedded_cost"] for b in folded.values())
                rate_card = sum(b["rate_card_cost"] for b in folded.values())
                total_tokens = sum(b["total_tokens"] for b in folded.values())
                unpriced = sum(b["unpriced_tokens"] for b in folded.values())
                priced_models = sum(1 for b in folded.values() if b["unpriced_tokens"] == 0)
                unpriced_models = sum(1 for b in folded.values() if b["unpriced_tokens"] > 0)
                return TotalSpendResponse(
                    total_cost=round(total_cost, 6),
                    embedded_cost=round(embedded, 6),
                    rate_card_cost=round(rate_card, 6),
                    total_tokens=int(total_tokens),
                    unpriced_tokens=int(unpriced),
                    priced_model_count=priced_models,
                    unpriced_model_count=unpriced_models,
                    start_ts=start_ts,
                    end_ts=end_ts,
                )
        except Exception as e:
            log.error(f"Error getting total spend: {e}")
            return TotalSpendResponse(start_ts=start_ts, end_ts=end_ts)

    def get_spend_trend(self, start_ts: int, end_ts: int) -> List[DailySpendPoint]:
        """Daily spend series over a window (admin)."""
        from open_webui.utils.pricing import fold_cost_rows, get_cached_pricing_map
        try:
            with get_db() as db:
                # dim = day string (UTC) from created_at
                dim_expr = "to_char(to_timestamp(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
                rows = self._cost_rows(db, dim_expr, start_ts, end_ts)
                folded = fold_cost_rows(rows, get_cached_pricing_map())
                points = [
                    DailySpendPoint(
                        date=day,
                        cost=round(b["cost"], 6),
                        embedded_cost=round(b["embedded_cost"], 6),
                        rate_card_cost=round(b["rate_card_cost"], 6),
                    )
                    for day, b in folded.items()
                ]
                points.sort(key=lambda p: p.date)
                return points
        except Exception as e:
            log.error(f"Error getting spend trend: {e}")
            return []

    def get_top_chats_by_cost(self, start_ts: int, end_ts: int, limit: int = 10) -> List[TopChatResponse]:
        """Most expensive chats over a window (admin), by attributed_chat_id."""
        from open_webui.utils.pricing import fold_cost_rows, get_cached_pricing_map
        try:
            with get_db() as db:
                rows = self._cost_rows(db, "attributed_chat_id", start_ts, end_ts)
                folded = fold_cost_rows(rows, get_cached_pricing_map())
                # token split per chat for the response
                tok = db.execute(sql_text(
                    """
                    SELECT attributed_chat_id AS chat_id,
                        CAST(COALESCE(SUM(prompt_tokens), 0) AS BIGINT) AS total_input_tokens,
                        CAST(COALESCE(SUM(completion_tokens), 0) AS BIGINT) AS total_output_tokens,
                        CAST(COALESCE(SUM(total_tokens), 0) AS BIGINT) AS total_tokens,
                        CAST(COALESCE(SUM(cache_read_tokens), 0) AS BIGINT) AS total_cache_read_tokens,
                        CAST(COALESCE(SUM(request_count), 0) AS BIGINT) AS message_count
                    FROM token_usage_event
                    WHERE user_id NOT LIKE 'shared-%' AND created_at >= :start_ts AND created_at < :end_ts
                    GROUP BY attributed_chat_id
                    """
                ), {"start_ts": start_ts, "end_ts": end_ts}).mappings().all()
                tok_by_id = {t["chat_id"]: t for t in tok}
                ranked = sorted(folded.items(), key=lambda kv: kv[1]["cost"], reverse=True)[:limit]
                result = []
                for chat_id, b in ranked:
                    if not chat_id:
                        continue
                    t = tok_by_id.get(chat_id, {})
                    result.append(TopChatResponse(
                        chat_id=chat_id,
                        model_id=None,
                        total_tokens=int(t.get("total_tokens") or 0),
                        total_input_tokens=int(t.get("total_input_tokens") or 0),
                        total_output_tokens=int(t.get("total_output_tokens") or 0),
                        total_cache_read_tokens=int(t.get("total_cache_read_tokens") or 0),
                        message_count=int(t.get("message_count") or 0),
                        cost=round(b["cost"], 6),
                    ))
                return result
        except Exception as e:
            log.error(f"Error getting top chats by cost: {e}")
            return []

    def get_chat_cost(self, chat_id: str) -> float:
        """Per-chat USD cost (folds subagent spend via attributed_chat_id)."""
        from open_webui.utils.pricing import fold_cost_rows, get_cached_pricing_map
        if not chat_id:
            return 0.0
        try:
            with get_db() as db:
                sql = f"""
                    SELECT :chat_id AS dim, model_id,
                    {self._COST_SELECT}
                    FROM token_usage_event
                    WHERE attributed_chat_id = :chat_id
                    GROUP BY model_id
                """
                rows = [dict(r) for r in db.execute(
                    sql_text(sql), {"chat_id": chat_id}
                ).mappings().all()]
                folded = fold_cost_rows(rows, get_cached_pricing_map())
                return round(folded.get(chat_id, {}).get("cost", 0.0), 6)
        except Exception as e:
            log.error(f"Error getting chat cost for {chat_id}: {e}")
            return 0.0


class _AsyncAnalyticsProxy:
    def __init__(self, impl: AnalyticsTable):
        self._impl = impl

    def __getattr__(self, name):
        attr = getattr(self._impl, name)
        if not callable(attr):
            return attr

        async def _wrapped(*args, **kwargs):
            if inspect.iscoroutinefunction(attr):
                return await attr(*args, **kwargs)
            return await run_sync_db(lambda: attr(*args, **kwargs))

        return _wrapped


# Global instance
Analytics = _AsyncAnalyticsProxy(AnalyticsTable())
