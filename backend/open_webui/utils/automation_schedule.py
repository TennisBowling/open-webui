"""Schedule math for automations — the single validation source shared by the
router, the model-callable tool, and the scheduler.

A schedule is stored as three columns: a bare iCal RRULE body (``rrule``), an
anchor instant (``dtstart``, epoch seconds UTC) and an IANA zone
(``timezone``). ``rrule IS NULL`` means a one-off that fires once at
``dtstart``.

All recurrence math is done on TIMEZONE-AWARE local datetimes so wall-clock
schedules survive DST: dateutil advances the local calendar fields and the zone
supplies the offset, which is why "every day at 08:00 America/Chicago" stays
08:00 across both transitions rather than drifting to 07:00 or 09:00.

Every failure raises ``AutomationScheduleError`` with a message that is safe to
show the user AND to hand back to a model verbatim (the builtin tool relays it
as ``ERROR: <message>``), so validation wording never has to be duplicated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from itertools import islice
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

# Occurrences must be at least this far apart. 3540 (59 minutes) rather than a
# flat 3600 so an hourly rule that lands on a DST edge — or any rule whose first
# gap is a minute short for calendar reasons — isn't rejected for arithmetic it
# didn't choose.
MIN_INTERVAL_SECONDS = 3540

MIN_INTERVAL_MESSAGE = (
    "Automations can run at most once per hour. Pick a schedule with at least "
    "an hour between runs."
)

_WEEKDAY_NAMES = {
    "MO": "Monday",
    "TU": "Tuesday",
    "WE": "Wednesday",
    "TH": "Thursday",
    "FR": "Friday",
    "SA": "Saturday",
    "SU": "Sunday",
}

_FREQ_NAMES = {
    "HOURLY": ("hour", "hours"),
    "DAILY": ("day", "days"),
    "WEEKLY": ("week", "weeks"),
    "MONTHLY": ("month", "months"),
    "YEARLY": ("year", "years"),
}


class AutomationScheduleError(Exception):
    """A schedule the user (or the model) asked for cannot be honored.

    The message is user- and model-facing: keep it plain, specific, and free of
    internals."""


def validate_timezone(value: Optional[str]) -> str:
    """Return a usable IANA zone name, defaulting to UTC when unset."""
    name = (value or "").strip() or "UTC"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise AutomationScheduleError(
            f"'{name}' is not a recognized timezone. Use an IANA name like "
            "'America/Chicago' or 'Europe/London'."
        )
    return name


def normalize_rrule(value: str) -> str:
    """Reduce an RRULE to the bare body we store, and prove it parses.

    Models (and users pasting from a calendar app) hand us anything from
    ``FREQ=DAILY`` to a full ``DTSTART:...\\nRRULE:FREQ=DAILY`` block. The
    anchor is OURS — it lives in the ``dtstart`` column — so any DTSTART in the
    input is dropped rather than silently overriding it.
    """
    body = (value or "").strip()
    if not body:
        raise AutomationScheduleError("The schedule is empty.")

    lines = [line.strip() for line in body.replace("\r", "").split("\n") if line.strip()]
    kept = [line for line in lines if not line.upper().startswith("DTSTART")]
    if not kept:
        raise AutomationScheduleError(
            "The schedule has no RRULE — it only sets a start time."
        )
    body = kept[0]
    if body.upper().startswith("RRULE:"):
        body = body[len("RRULE:") :]
    body = body.strip().upper()

    try:
        rrulestr(f"RRULE:{body}", dtstart=datetime(2026, 1, 1, tzinfo=dt_timezone.utc))
    except Exception:
        raise AutomationScheduleError(
            f"'{body}' is not a valid recurrence rule. Example: "
            "FREQ=DAILY;BYHOUR=8;BYMINUTE=0."
        )
    return body


def _local(epoch: int, tz: str) -> datetime:
    return datetime.fromtimestamp(epoch, ZoneInfo(tz))


def _build_rule(rrule: str, dtstart: int, tz: str):
    return rrulestr(f"RRULE:{rrule}", dtstart=_local(dtstart, tz))


def resolve_schedule(
    *,
    rrule: Optional[str] = None,
    run_at: Optional[str] = None,
    offset_minutes: Optional[int] = None,
    timezone: Optional[str] = None,
    now: Optional[datetime] = None,
) -> tuple[Optional[str], int, str]:
    """Turn the three mutually-exclusive schedule spellings into storage form.

    Returns ``(rrule_body_or_None, dtstart_epoch, timezone)``. ``rrule`` anchors
    on "now" (BYHOUR/BYMINUTE in the rule decide the wall-clock time from
    there), ``run_at`` is an ISO datetime read in the automation's zone, and
    ``offset_minutes`` is the relative one-off ("in 15 minutes").
    """
    tz = validate_timezone(timezone)
    provided = [name for name, value in
                (("schedule", rrule), ("run_at", run_at), ("offset_minutes", offset_minutes))
                if value is not None and value != ""]
    if len(provided) != 1:
        raise AutomationScheduleError(
            "Provide exactly one of schedule (a recurrence rule), run_at (an "
            "exact date and time), or offset_minutes (a delay from now)."
        )

    now = now or datetime.now(ZoneInfo(tz))

    if rrule is not None and rrule != "":
        return normalize_rrule(rrule), int(now.timestamp()), tz

    if offset_minutes is not None and offset_minutes != "":
        try:
            minutes = int(offset_minutes)
        except (TypeError, ValueError):
            raise AutomationScheduleError("offset_minutes must be a whole number of minutes.")
        if minutes < 1:
            raise AutomationScheduleError("offset_minutes must be at least 1.")
        return None, int((now + timedelta(minutes=minutes)).timestamp()), tz

    try:
        parsed = datetime.fromisoformat(str(run_at).strip().replace("Z", "+00:00"))
    except ValueError:
        raise AutomationScheduleError(
            f"'{run_at}' is not a valid date and time. Use ISO format, e.g. "
            "2026-08-01T09:30."
        )
    # A bare local time is the common case (the model writes the user's wall
    # clock); an explicit offset is honored as given.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz))
    if parsed <= now:
        raise AutomationScheduleError("That date and time is already in the past.")
    return None, int(parsed.timestamp()), tz


def compute_next_run_at(
    rrule: Optional[str], dtstart: int, tz: str, after: Optional[int] = None
) -> Optional[int]:
    """Next firing time strictly after ``after`` (default: now), or None.

    None means "nothing left to schedule" — a one-off whose instant has passed,
    or a bounded rule (COUNT/UNTIL) that has run out.
    """
    after = int(after if after is not None else datetime.now(dt_timezone.utc).timestamp())
    if not rrule:
        return dtstart if dtstart > after else None

    occurrence = _build_rule(rrule, dtstart, tz).after(_local(after, tz), inc=False)
    return int(occurrence.timestamp()) if occurrence else None


def enforce_min_interval(rrule: Optional[str], dtstart: int, tz: str) -> None:
    """Reject schedules that would fire more than once an hour.

    Checks the gap between the first two occurrences, which is what a user
    actually experiences. A rule with a single occurrence (COUNT=1, or an UNTIL
    that only admits one) has no gap to violate and is effectively a one-off —
    it passes.
    """
    if not rrule:
        return
    occurrences = list(islice(_build_rule(rrule, dtstart, tz), 2))
    if len(occurrences) < 2:
        return
    if (occurrences[1] - occurrences[0]).total_seconds() < MIN_INTERVAL_SECONDS:
        raise AutomationScheduleError(MIN_INTERVAL_MESSAGE)


def _rrule_parts(rrule: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for chunk in rrule.split(";"):
        key, _, value = chunk.partition("=")
        if key:
            parts[key.strip().upper()] = value.strip().upper()
    return parts


def describe_schedule(rrule: Optional[str], dtstart: int, tz: str) -> str:
    """One-line human description, e.g. "Every weekday at 08:00 (America/Chicago)"."""
    if not rrule:
        return f"Once on {_local(dtstart, tz):%b %-d, %Y at %H:%M} ({tz})"

    parts = _rrule_parts(rrule)
    freq = parts.get("FREQ", "")
    singular, plural = _FREQ_NAMES.get(freq, ("time", "times"))
    interval = int(parts.get("INTERVAL") or 1)
    every = f"Every {singular}" if interval == 1 else f"Every {interval} {plural}"

    byday = [d[-2:] for d in parts.get("BYDAY", "").split(",") if d]
    if freq == "WEEKLY" and byday:
        if set(byday) == {"MO", "TU", "WE", "TH", "FR"}:
            every = "Every weekday" if interval == 1 else f"{every} on weekdays"
        else:
            days = ", ".join(_WEEKDAY_NAMES.get(day, day) for day in byday)
            every = f"{every} on {days}" if interval > 1 else f"Every {days}"

    if parts.get("BYMONTHDAY"):
        every = f"{every} on day {parts['BYMONTHDAY']}"

    # The time of day comes from BYHOUR/BYMINUTE when the rule pins it, and from
    # the anchor otherwise (that is exactly how the recurrence itself resolves it).
    if parts.get("BYHOUR"):
        hour = int(parts["BYHOUR"].split(",")[0])
        minute = int((parts.get("BYMINUTE") or "0").split(",")[0])
    else:
        local = _local(dtstart, tz)
        hour, minute = local.hour, local.minute

    if freq == "HOURLY":
        return f"{every} ({tz})"
    return f"{every} at {hour:02d}:{minute:02d} ({tz})"
