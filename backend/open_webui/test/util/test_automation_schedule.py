"""Unit tests for ``utils.automation_schedule`` — the schedule math shared by
the automations router, the builtin tool, and the scheduler.

Pure functions only (no DB, no app state): these are the rules the whole
feature's correctness rests on — a wall-clock schedule that drifts an hour at a
DST boundary silently delivers every run at the wrong time.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from open_webui.utils.automation_schedule import (
    AutomationScheduleError,
    compute_next_run_at,
    describe_schedule,
    enforce_min_interval,
    normalize_rrule,
    resolve_schedule,
    validate_timezone,
)

CHICAGO = "America/Chicago"


def _epoch(year, month, day, hour, minute=0, tz=CHICAGO):
    return int(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp())


def _local(epoch, tz=CHICAGO):
    return datetime.fromtimestamp(epoch, ZoneInfo(tz))


# --- normalization / validation ---------------------------------------------


def test_normalize_strips_prefix_and_dtstart():
    assert normalize_rrule("RRULE:FREQ=DAILY") == "FREQ=DAILY"
    assert (
        normalize_rrule("DTSTART:20260101T080000Z\nRRULE:FREQ=WEEKLY;BYDAY=MO")
        == "FREQ=WEEKLY;BYDAY=MO"
    )


def test_normalize_rejects_garbage():
    with pytest.raises(AutomationScheduleError):
        normalize_rrule("every day please")


def test_validate_timezone_defaults_to_utc_and_rejects_nonsense():
    assert validate_timezone(None) == "UTC"
    assert validate_timezone(CHICAGO) == CHICAGO
    with pytest.raises(AutomationScheduleError):
        validate_timezone("Mars/Olympus_Mons")


# --- the once-per-hour floor -------------------------------------------------


def test_minutely_is_rejected():
    with pytest.raises(AutomationScheduleError):
        enforce_min_interval("FREQ=MINUTELY;INTERVAL=5", _epoch(2026, 3, 1, 12), CHICAGO)


def test_hourly_is_accepted():
    enforce_min_interval("FREQ=HOURLY", _epoch(2026, 3, 1, 12), CHICAGO)


def test_single_occurrence_rule_passes():
    """COUNT=1 has no second occurrence, so there is no gap to violate — it is a
    one-off wearing an RRULE, and it must not be rejected by the floor check."""
    enforce_min_interval(
        "FREQ=MINUTELY;COUNT=1", _epoch(2026, 3, 1, 12), CHICAGO
    )


# --- DST ---------------------------------------------------------------------


def test_dst_spring_forward_keeps_wall_clock_time():
    """2026-03-08 is the US spring-forward. A daily 08:00 run stays 08:00 local
    across it — so the UTC gap over that day is 23 hours, not 24."""
    rrule = "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"
    dtstart = _epoch(2026, 3, 6, 8)

    nxt = compute_next_run_at(rrule, dtstart, CHICAGO, _epoch(2026, 3, 6, 9))
    following = compute_next_run_at(rrule, dtstart, CHICAGO, nxt)

    assert _local(nxt).date() == datetime(2026, 3, 7).date()
    assert _local(following).date() == datetime(2026, 3, 8).date()
    assert _local(nxt).hour == _local(following).hour == 8
    assert following - nxt == 23 * 3600  # the short day, at the same wall clock


def test_dst_fall_back_keeps_wall_clock_time():
    """2026-11-01 is the US fall-back: the day is 25 hours long, still 08:00."""
    rrule = "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"
    dtstart = _epoch(2026, 10, 30, 8)

    nxt = compute_next_run_at(rrule, dtstart, CHICAGO, _epoch(2026, 10, 30, 9))
    following = compute_next_run_at(rrule, dtstart, CHICAGO, nxt)

    assert _local(nxt).date() == datetime(2026, 10, 31).date()
    assert _local(following).date() == datetime(2026, 11, 1).date()
    assert _local(nxt).hour == _local(following).hour == 8
    assert following - nxt == 25 * 3600


# --- next-run resolution -----------------------------------------------------


def test_weekly_byday_lands_on_the_named_day():
    rrule = "FREQ=WEEKLY;BYDAY=WE;BYHOUR=9;BYMINUTE=30"
    dtstart = _epoch(2026, 6, 1, 12)  # a Monday
    nxt = compute_next_run_at(rrule, dtstart, CHICAGO, dtstart)
    local = _local(nxt)
    assert local.weekday() == 2  # Wednesday
    assert (local.hour, local.minute) == (9, 30)


def test_one_off_in_the_future_then_exhausted():
    dtstart = _epoch(2026, 6, 1, 12)
    assert compute_next_run_at(None, dtstart, CHICAGO, dtstart - 60) == dtstart
    assert compute_next_run_at(None, dtstart, CHICAGO, dtstart) is None


def test_count_limited_rule_runs_out():
    rrule = "FREQ=DAILY;COUNT=1;BYHOUR=8;BYMINUTE=0"
    dtstart = _epoch(2026, 6, 1, 7)
    first = compute_next_run_at(rrule, dtstart, CHICAGO, dtstart)
    assert first is not None
    assert compute_next_run_at(rrule, dtstart, CHICAGO, first) is None


# --- resolve_schedule --------------------------------------------------------


def test_resolve_requires_exactly_one_spelling():
    with pytest.raises(AutomationScheduleError):
        resolve_schedule(timezone=CHICAGO)
    with pytest.raises(AutomationScheduleError):
        resolve_schedule(rrule="FREQ=DAILY", offset_minutes=15, timezone=CHICAGO)


def test_resolve_offset_minutes_is_relative_to_now():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=ZoneInfo(CHICAGO))
    rrule, dtstart, tz = resolve_schedule(offset_minutes=15, timezone=CHICAGO, now=now)
    assert rrule is None
    assert tz == CHICAGO
    assert dtstart == int(now.timestamp()) + 15 * 60


def test_resolve_run_at_is_read_in_the_automation_timezone():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=ZoneInfo(CHICAGO))
    _, dtstart, _ = resolve_schedule(run_at="2026-06-02T09:30", timezone=CHICAGO, now=now)
    assert _local(dtstart).hour == 9 and _local(dtstart).minute == 30


def test_resolve_run_at_in_the_past_is_rejected():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=ZoneInfo(CHICAGO))
    with pytest.raises(AutomationScheduleError):
        resolve_schedule(run_at="2026-05-31T09:30", timezone=CHICAGO, now=now)


# --- descriptions ------------------------------------------------------------


def test_describe_recurring_and_one_off():
    dtstart = _epoch(2026, 6, 1, 12)
    assert (
        describe_schedule("FREQ=DAILY;BYHOUR=8;BYMINUTE=0", dtstart, CHICAGO)
        == "Every day at 08:00 (America/Chicago)"
    )
    assert (
        describe_schedule(
            "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE=15", dtstart, CHICAGO
        )
        == "Every weekday at 07:15 (America/Chicago)"
    )
    assert describe_schedule(None, dtstart, CHICAGO).startswith("Once on Jun 1, 2026 at 12:00")
