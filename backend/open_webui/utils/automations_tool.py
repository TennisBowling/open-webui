"""Built-in ``automations`` tools: let the model schedule work for later.

Four methods (create / update / list / delete) over the user's own automations.
They are thin wrappers around the shared service in
``utils/automation_runner.py``, so a model gets exactly the validation — and
exactly the error wording — a user gets from the automations page.

The one thing the model has to get right is the title/prompt split, which is why
``AUTOMATIONS_SYSTEM_PROMPT`` says it three ways: the prompt is REPLAYED VERBATIM
into a fresh chat that remembers nothing about the conversation that scheduled
it, so "remind me every morning" would be a broken prompt while "Write today's
standup summary" is a good one. Scheduling belongs in the schedule argument, not
in the prose.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Request
from pydantic import BaseModel

from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.automations import Automations
from open_webui.models.users import Users
from open_webui.utils.automation_runner import (
    create_automation,
    sanitize_run_tool_ids,
    schedule_text,
    update_automation,
)
from open_webui.utils.automation_schedule import AutomationScheduleError

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


AUTOMATIONS_SYSTEM_PROMPT = """## Automations

You can schedule work to run later with the `automations` tools \
(`create_automation`, `update_automation`, `list_automations`, \
`delete_automation`).

**Title vs prompt — the thing to get right.** The prompt is replayed VERBATIM in \
a brand-new chat that has NO memory of this conversation. Write it as a complete, \
standalone instruction that makes sense on its own. NEVER put scheduling words in \
it ("every morning", "remind me in an hour", "again tomorrow") — the schedule is \
already the schedule, and the prompt would then read as nonsense to the model \
that runs it. The title is just the short label the user sees in their \
automations list.

  * User: "every weekday at 7am send me a summary of AI news"
  * title: `AI news digest`
  * prompt: `Search the web for the most significant AI news from the last 24 \
hours and write a short digest with links.`
  * schedule: `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE=0`

**Timing.** Pass exactly one of:
  * `schedule` — an iCal RRULE body for anything recurring. Examples:
    `FREQ=DAILY;BYHOUR=8;BYMINUTE=0`,
    `FREQ=WEEKLY;BYDAY=SU;BYHOUR=18;BYMINUTE=30`,
    `FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0`.
  * `run_at` — an ISO date and time for a one-off at a specific moment, in the \
user's local time (e.g. `2026-08-01T09:30`).
  * `offset_minutes` — a one-off relative to now ("in 15 minutes" -> 15).

Automations can run at most once per hour.

**Behavior.** Confirm the details first if the request is ambiguous about what \
should happen or when. After creating or updating one, tell the user in plain \
language when it will next run. If a tool returns `ERROR: ...`, relay the reason \
to the user — it explains exactly what to change."""


async def _resolve_user(user_dict: Optional[dict]):
    """The tool layer injects ``__user__`` as a plain dict; every service here
    wants the real ``UserModel`` (same resolution the subagent tools do)."""
    if not isinstance(user_dict, dict) or not user_dict.get("id"):
        return None
    return await Users.get_user_by_id(user_dict["id"])


class AutomationTools:
    """Built-in tools: create/update/list/delete the user's scheduled tasks."""

    class Valves(BaseModel):
        """Configuration placeholder — settings are managed via the admin panel."""

        pass

    def __init__(self):
        self.valves = self.Valves()

    async def create_automation(
        self,
        title: str,
        prompt: str,
        schedule: Optional[str] = None,
        run_at: Optional[str] = None,
        offset_minutes: Optional[int] = None,
        timezone: Optional[str] = None,
        __request__: Optional[Request] = None,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ) -> str:
        """
        Schedule a prompt to run automatically, later and repeatedly.

        Args:
            title: Short label for the user's automations list, e.g.
                "AI news digest". Not sent to the model that runs it.
            prompt: The complete, standalone instruction to run. Replayed
                VERBATIM in a fresh chat with no memory of this conversation, so
                it must make sense on its own and must NOT contain any
                scheduling words.
            schedule: iCal RRULE body for a recurring automation, e.g.
                "FREQ=DAILY;BYHOUR=8;BYMINUTE=0". Provide exactly one of
                schedule / run_at / offset_minutes.
            run_at: ISO date and time for a one-off, in the user's local time,
                e.g. "2026-08-01T09:30".
            offset_minutes: Minutes from now for a one-off ("in 15 minutes" ->
                15).
            timezone: IANA timezone the times are expressed in. Defaults to the
                user's own timezone.

        Returns:
            A confirmation naming the automation and its next run time, or
            "ERROR: <reason>" — relay the reason to the user.
        """
        user = await _resolve_user(__user__)
        if user is None or __request__ is None:
            return "ERROR: automations are unavailable here."

        metadata = __metadata__ or {}
        try:
            automation = await create_automation(
                __request__,
                user,
                title=title,
                prompt=prompt,
                schedule=schedule,
                run_at=run_at,
                offset_minutes=offset_minutes,
                timezone=timezone or metadata.get("timezone"),
                model_id=(metadata.get("model") or {}).get("id") or "",
                # Seed the run's toolbox from what this chat has enabled, minus
                # everything an unattended run can't use (the service sanitizes;
                # this keeps the intent obvious at the call site).
                tool_ids=sanitize_run_tool_ids(metadata.get("tool_ids")),
                features=metadata.get("features"),
            )
        except AutomationScheduleError as e:
            return f"ERROR: {e}"

        log.info("automations: created %s for user %s", automation.id, user.id)
        return _confirmation("Created", automation)

    async def update_automation(
        self,
        automation_id: str,
        title: Optional[str] = None,
        prompt: Optional[str] = None,
        schedule: Optional[str] = None,
        run_at: Optional[str] = None,
        offset_minutes: Optional[int] = None,
        timezone: Optional[str] = None,
        paused: Optional[bool] = None,
        __request__: Optional[Request] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Change an existing automation. Only the fields you pass are touched.

        Args:
            automation_id: The id from `list_automations` or a create result.
            title: New short label.
            prompt: New standalone instruction (same rules as create: verbatim,
                no scheduling words).
            schedule: New iCal RRULE body. Provide at most one of schedule /
                run_at / offset_minutes.
            run_at: New one-off ISO date and time in the user's local time.
            offset_minutes: New one-off delay in minutes from now.
            timezone: New IANA timezone for the schedule's times.
            paused: True to pause the automation, False to resume it.

        Returns:
            A confirmation naming the automation and its next run time, or
            "ERROR: <reason>" — relay the reason to the user.
        """
        user = await _resolve_user(__user__)
        if user is None or __request__ is None:
            return "ERROR: automations are unavailable here."

        try:
            automation = await update_automation(
                __request__,
                user,
                automation_id,
                title=title,
                prompt=prompt,
                schedule=schedule,
                run_at=run_at,
                offset_minutes=offset_minutes,
                timezone=timezone,
                paused=paused,
            )
        except AutomationScheduleError as e:
            return f"ERROR: {e}"

        return _confirmation("Paused" if not automation.active else "Updated", automation)

    async def list_automations(
        self,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        List the user's automations with their ids, schedules and next run times.

        Returns:
            One line per automation, or a note that there are none.
        """
        user = await _resolve_user(__user__)
        if user is None:
            return "ERROR: automations are unavailable here."

        automations = await Automations.get_automations_by_user_id(user.id)
        if not automations:
            return "No automations are scheduled."

        lines = []
        for automation in automations:
            state = "paused" if not automation.active else _next_run_text(automation)
            lines.append(
                f"- {automation.title} (id {automation.id}): "
                f"{schedule_text(automation)} — {state}"
            )
        return "\n".join(lines)

    async def delete_automation(
        self,
        automation_id: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Delete an automation so it never runs again.

        Args:
            automation_id: The id from `list_automations`.

        Returns:
            A confirmation, or "ERROR: <reason>".
        """
        user = await _resolve_user(__user__)
        if user is None:
            return "ERROR: automations are unavailable here."

        automation = await Automations.get_automation_by_id_and_user_id(
            automation_id, user.id
        )
        if automation is None:
            return "ERROR: that automation doesn't exist."
        if not await Automations.delete_automation_by_id_and_user_id(
            automation_id, user.id
        ):
            return "ERROR: the automation could not be deleted."
        return f"Deleted '{automation.title}'."


def _next_run_text(automation) -> str:
    if automation.next_run_at is None:
        return "no further runs"
    local = datetime.fromtimestamp(
        automation.next_run_at, ZoneInfo(automation.timezone)
    )
    return f"next run {local:%b %-d, %Y at %H:%M} ({automation.timezone})"


def _confirmation(verb: str, automation) -> str:
    return (
        f"{verb} '{automation.title}' (id {automation.id}). "
        f"{schedule_text(automation)}; {_next_run_text(automation)}."
    )


_automation_tools_instance: Optional[AutomationTools] = None


def get_automation_tools_instance() -> AutomationTools:
    """Get or create the singleton AutomationTools instance."""
    global _automation_tools_instance
    if _automation_tools_instance is None:
        _automation_tools_instance = AutomationTools()
    return _automation_tools_instance
