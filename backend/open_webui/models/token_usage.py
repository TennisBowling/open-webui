import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from open_webui.env import SRC_LOG_LEVELS
from open_webui.internal.db import Base, get_db

from sqlalchemy import BigInteger, Column, Text, delete, select
from sqlalchemy.dialects.postgresql import JSONB

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])


def calculate_next_reset_timestamp(reset_time: str, reset_timezone: str) -> int:
    try:
        import pytz
    except ImportError:
        reset_timezone = "UTC"

    try:
        hour, minute = map(int, reset_time.split(":"))
        if reset_timezone == "UTC":
            tz = timezone.utc
            now = datetime.now(tz)
        else:
            try:
                tz = pytz.timezone(reset_timezone)
                now = datetime.now(tz)
            except Exception:
                tz = timezone.utc
                now = datetime.now(tz)

        today_reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        next_reset = today_reset + timedelta(days=1) if now >= today_reset else today_reset
        return int(next_reset.timestamp())
    except Exception as e:
        log.error(f"Error calculating next reset timestamp: {e}")
        now = datetime.now(timezone.utc)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return int(tomorrow.timestamp())


def calculate_rolling_window_next_reset(last_reset_timestamp: int, window_duration: int) -> Optional[int]:
    if not last_reset_timestamp or not window_duration:
        return None
    return last_reset_timestamp + window_duration


def should_reset_daily(reset_time: str, reset_timezone: str, last_reset_date: Optional[str]) -> bool:
    try:
        import pytz
    except ImportError:
        reset_timezone = "UTC"

    try:
        hour, minute = map(int, reset_time.split(":"))
        if reset_timezone == "UTC":
            tz = timezone.utc
            now = datetime.now(tz)
        else:
            try:
                tz = pytz.timezone(reset_timezone)
                now = datetime.now(tz)
            except Exception:
                tz = timezone.utc
                now = datetime.now(tz)

        current_date = now.strftime("%Y-%m-%d")
        today_reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return now >= today_reset and last_reset_date != current_date
    except Exception as e:
        log.error(f"Error checking daily reset: {e}")
        return False


class TokenGroup(Base):
    __tablename__ = "token_group"

    name = Column(Text, unique=True, primary_key=True)
    models = Column(JSONB)
    limit = Column(BigInteger)
    reset_time = Column(Text, default="00:00")
    reset_timezone = Column(Text, default="UTC")
    window_duration = Column(BigInteger, nullable=True)
    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


class TokenUsage(Base):
    __tablename__ = "token_usage"

    group_name = Column(Text, unique=True, primary_key=True)
    token_in = Column(BigInteger, default=0)
    token_out = Column(BigInteger, default=0)
    token_total = Column(BigInteger, default=0)
    updated_at = Column(BigInteger)
    last_reset_date = Column(Text, nullable=True)
    last_reset_timestamp = Column(BigInteger, nullable=True)


class TokenGroups:
    async def get_token_groups(self, apply_resets: bool = True) -> Dict:
        try:
            async with get_db() as db:
                group_rows = (await db.execute(select(TokenGroup))).scalars().all()
                usage_rows = (await db.execute(select(TokenUsage))).scalars().all()
                usage_by_name = {usage.group_name: usage for usage in usage_rows}
                current_timestamp = int(time.time())
                groups: dict[str, dict] = {}

                for group in group_rows:
                    usage = usage_by_name.get(group.name)
                    if not usage:
                        usage = TokenUsage(
                            group_name=group.name,
                            token_in=0,
                            token_out=0,
                            token_total=0,
                            updated_at=current_timestamp,
                        )
                        db.add(usage)
                        usage_by_name[group.name] = usage

                    reset_type = "rolling_window" if group.window_duration else "daily"
                    next_reset_at = None
                    should_reset = False

                    if group.window_duration:
                        if usage.last_reset_timestamp:
                            next_reset_at = calculate_rolling_window_next_reset(
                                usage.last_reset_timestamp, group.window_duration
                            )
                            if apply_resets and next_reset_at and current_timestamp > next_reset_at:
                                should_reset = True
                                next_reset_at = current_timestamp + group.window_duration
                    else:
                        reset_time = group.reset_time or "00:00"
                        reset_timezone = group.reset_timezone or "UTC"
                        next_reset_at = calculate_next_reset_timestamp(reset_time, reset_timezone)
                        if apply_resets and should_reset_daily(
                            reset_time, reset_timezone, usage.last_reset_date
                        ):
                            should_reset = True

                    if should_reset and apply_resets:
                        locked = (
                            await db.execute(
                                select(TokenUsage)
                                .where(TokenUsage.group_name == group.name)
                                .with_for_update()
                            )
                        ).scalars().first()
                        if locked:
                            locked.token_in = 0
                            locked.token_out = 0
                            locked.token_total = 0
                            locked.updated_at = current_timestamp
                            if group.window_duration:
                                locked.last_reset_timestamp = current_timestamp
                            else:
                                locked.last_reset_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                            usage = locked

                    groups[group.name] = {
                        "models": group.models or [],
                        "limit": group.limit,
                        "window_duration": group.window_duration,
                        "usage": {
                            "in": usage.token_in or 0,
                            "out": usage.token_out or 0,
                            "total": usage.token_total or 0,
                        },
                        "next_reset_at": next_reset_at,
                        "reset_type": reset_type,
                    }

                if apply_resets:
                    await db.commit()
                return groups
        except Exception as e:
            log.error(f"Error getting token groups: {e}")
            return {}

    async def create_token_group(
        self,
        name: str,
        models: List[str],
        limit: int,
        reset_time: str = "00:00",
        reset_timezone: str = "UTC",
        window_duration: int = None,
    ) -> bool:
        try:
            async with get_db() as db:
                existing = await db.get(TokenGroup, name)
                if existing:
                    return False

                now = int(time.time())
                db.add(
                    TokenGroup(
                        name=name,
                        models=models,
                        limit=limit,
                        reset_time=reset_time,
                        reset_timezone=reset_timezone,
                        window_duration=window_duration,
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.add(
                    TokenUsage(
                        group_name=name,
                        token_in=0,
                        token_out=0,
                        token_total=0,
                        updated_at=now,
                    )
                )
                await db.commit()
                return True
        except Exception as e:
            log.error(f"Error creating token group {name}: {e}")
            return False

    async def update_token_group(
        self,
        name: str,
        models: List[str] = None,
        limit: int = None,
        window_duration: int = None,
    ) -> bool:
        try:
            async with get_db() as db:
                group = await db.get(TokenGroup, name)
                if not group:
                    return False
                if models is not None:
                    group.models = models
                if limit is not None:
                    group.limit = limit
                if window_duration is not None:
                    group.window_duration = window_duration
                group.updated_at = int(time.time())
                await db.commit()
                return True
        except Exception as e:
            log.error(f"Error updating token group {name}: {e}")
            return False

    async def delete_token_group(self, name: str) -> bool:
        try:
            async with get_db() as db:
                await db.execute(delete(TokenUsage).where(TokenUsage.group_name == name))
                await db.execute(delete(TokenGroup).where(TokenGroup.name == name))
                await db.commit()
                return True
        except Exception as e:
            log.error(f"Error deleting token group {name}: {e}")
            return False

    async def update_token_usage(self, model_id: str, token_in: int, token_out: int, token_total: int):
        try:
            current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            current_timestamp = int(time.time())

            async with get_db() as db:
                groups = (
                    await db.execute(
                        select(TokenGroup).where(TokenGroup.models.contains([model_id]))
                    )
                ).scalars().all()

                for group in groups:
                    usage = (
                        await db.execute(
                            select(TokenUsage)
                            .where(TokenUsage.group_name == group.name)
                            .with_for_update()
                        )
                    ).scalars().first()

                    if not usage:
                        usage = TokenUsage(
                            group_name=group.name,
                            token_in=0,
                            token_out=0,
                            token_total=0,
                            updated_at=current_timestamp,
                            last_reset_date=current_date,
                            last_reset_timestamp=current_timestamp,
                        )
                        db.add(usage)

                    if group.window_duration:
                        last_reset_ts = usage.last_reset_timestamp
                        if not last_reset_ts or current_timestamp > last_reset_ts + group.window_duration:
                            usage.token_in = token_in
                            usage.token_out = token_out
                            usage.token_total = token_total
                            usage.last_reset_timestamp = current_timestamp
                        else:
                            usage.token_in = (usage.token_in or 0) + token_in
                            usage.token_out = (usage.token_out or 0) + token_out
                            usage.token_total = (usage.token_total or 0) + token_total
                    else:
                        if usage.last_reset_date != current_date:
                            usage.token_in = token_in
                            usage.token_out = token_out
                            usage.token_total = token_total
                            usage.last_reset_date = current_date
                        else:
                            usage.token_in = (usage.token_in or 0) + token_in
                            usage.token_out = (usage.token_out or 0) + token_out
                            usage.token_total = (usage.token_total or 0) + token_total

                    usage.updated_at = current_timestamp

                await db.commit()
        except Exception as e:
            log.error(f"Error updating token usage for model {model_id}: {e}")

    async def force_reset_all_usage(self):
        try:
            current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            current_timestamp = int(time.time())
            async with get_db() as db:
                usage_records = (await db.execute(select(TokenUsage))).scalars().all()
                for usage in usage_records:
                    usage.token_in = 0
                    usage.token_out = 0
                    usage.token_total = 0
                    usage.last_reset_date = current_date
                    usage.last_reset_timestamp = current_timestamp
                    usage.updated_at = current_timestamp
                await db.commit()
                return True
        except Exception as e:
            log.error(f"Error during manual token usage reset: {e}")
            return False


token_groups = TokenGroups()
