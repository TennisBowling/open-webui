"""Automations: the create/update service, the scheduler sweep, and execution.

Three layers live here because they must agree exactly:

* the **service** (`create_automation` / `update_automation`) is the ONE place
  schedules are validated and `next_run_at` is computed — the router and the
  model-callable builtin tool both call it, so a user and a model get identical
  behavior and identical error wording;
* the **sweep** (`sweep_due_automations`) is the lifespan loop's per-tick work:
  find what is due, claim it exactly-once via a compare-and-set on
  `next_run_at`, and either fire it or record it as missed;
* the **run** (`fire_automation`) creates a fresh hidden chat and drives it
  through `start_generation` — the same request-free entrypoint the message
  queue drain uses, so an automation's turn is byte-identical to a tab-driven
  one (assembly, tools, persistence, socket delivery).

The prompt is replayed VERBATIM into that fresh chat: an automation has no
memory of the conversation that created it, which is why the tool's system
prompt insists the prompt be a standalone instruction with no scheduling text.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from open_webui.env import (
    AUTOMATION_MAX_CONCURRENCY,
    AUTOMATION_MISFIRE_GRACE_SECONDS,
    AUTOMATION_RUN_TIMEOUT_SECONDS,
    SRC_LOG_LEVELS,
)
from open_webui.models.automations import (
    AutomationModel,
    AutomationRuns,
    Automations,
    PREVIEW_MAX_LENGTH,
)
from open_webui.models.chats import ChatImportForm, Chats
from open_webui.models.users import Users
from open_webui.utils.automation_schedule import (
    AutomationScheduleError,
    compute_next_run_at,
    describe_schedule,
    enforce_min_interval,
    resolve_schedule,
    validate_timezone,
)
from open_webui.utils.headless_request import HeadlessRequest

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


# Cap on how many automations one sweep pass may START. Mirrors the queue
# reconciler's per-pass cap: everyone's schedule lands on the hour, and a pass
# that fired all of them at once would spike the worker. Anything left over is
# still due on the next tick 30s later.
_MAX_STARTS_PER_PASS = 3

# How long past the run timeout a `running` row must sit before the stuck sweep
# calls it dead. Generous — the only cost of waiting is a stale spinner, the
# cost of being wrong is failing a live run.
_STUCK_RUN_GRACE_SECONDS = 300

# Tools that cannot work in an unattended run and are stripped from every
# automation's tool list regardless of what was configured or smuggled:
# `automations` (an automation must not schedule more automations), `subagent`
# (unbounded fan-out with nobody watching), `data_viz` (show_widget round-trips
# to a live frontend to render+confirm — there isn't one), and `ask_user`
# (blocks on a human answer that will never come).
_UNSAFE_RUN_TOOL_IDS = (
    "builtin:automations",
    "builtin:subagent",
    "builtin:data_viz",
    "builtin:ask_user",
)

# Features an unattended run may keep. Everything else either needs a live
# client or changes nothing without one.
_RUN_FEATURE_ALLOWLIST = ("web_search", "image_generation")

_automation_concurrency_sem: Optional[asyncio.Semaphore] = None


def _get_automation_concurrency_sem() -> asyncio.Semaphore:
    global _automation_concurrency_sem
    if _automation_concurrency_sem is None:
        _automation_concurrency_sem = asyncio.Semaphore(
            max(1, AUTOMATION_MAX_CONCURRENCY)
        )
    return _automation_concurrency_sem


def sanitize_run_tool_ids(tool_ids) -> list[str]:
    if not isinstance(tool_ids, list):
        return []
    return [
        str(tool_id)
        for tool_id in tool_ids
        if isinstance(tool_id, str) and tool_id not in _UNSAFE_RUN_TOOL_IDS
    ]


def sanitize_run_features(features) -> dict:
    if not isinstance(features, dict):
        return {}
    return {
        feature: True
        for feature in _RUN_FEATURE_ALLOWLIST
        if features.get(feature)
    }


# --- create/update service ---------------------------------------------------
#
# Shared verbatim by routers/automations.py and utils/automations_tool.py. Every
# failure is an AutomationScheduleError whose message is safe to show a user AND
# to relay to a model.


def _enforce_active_cap(request, active_count: int) -> None:
    limit = int(getattr(request.app.state.config, "AUTOMATIONS_MAX_ACTIVE_PER_USER", 10))
    if active_count >= limit:
        raise AutomationScheduleError(
            f"You already have {limit} active automations, which is the limit. "
            "Delete or pause one before adding another."
        )


async def create_automation(
    request,
    user,
    *,
    title: str,
    prompt: str,
    schedule: Optional[str] = None,
    run_at: Optional[str] = None,
    offset_minutes: Optional[int] = None,
    timezone: Optional[str] = None,
    model_id: str,
    tool_ids: Optional[list[str]] = None,
    features: Optional[dict] = None,
) -> AutomationModel:
    title = (title or "").strip()
    prompt = (prompt or "").strip()
    if not title:
        raise AutomationScheduleError("The automation needs a title.")
    if not prompt:
        raise AutomationScheduleError("The automation needs a prompt to run.")
    if not (model_id or "").strip():
        raise AutomationScheduleError("The automation needs a model to run with.")

    rrule, dtstart, tz = resolve_schedule(
        rrule=schedule,
        run_at=run_at,
        offset_minutes=offset_minutes,
        timezone=timezone,
    )
    enforce_min_interval(rrule, dtstart, tz)
    next_run_at = compute_next_run_at(rrule, dtstart, tz)
    if next_run_at is None:
        raise AutomationScheduleError(
            "That schedule has no future runs. Pick a time in the future."
        )

    _enforce_active_cap(request, await Automations.count_active_by_user_id(user.id))

    automation = await Automations.insert_new_automation(
        user.id,
        title=title,
        prompt=prompt,
        rrule=rrule,
        dtstart=dtstart,
        timezone=tz,
        model_id=model_id,
        tool_ids=sanitize_run_tool_ids(tool_ids),
        features=sanitize_run_features(features),
        next_run_at=next_run_at,
    )
    if automation is None:
        raise AutomationScheduleError("The automation could not be saved.")
    return automation


async def update_automation(
    request,
    user,
    automation_id: str,
    *,
    title: Optional[str] = None,
    prompt: Optional[str] = None,
    schedule: Optional[str] = None,
    run_at: Optional[str] = None,
    offset_minutes: Optional[int] = None,
    timezone: Optional[str] = None,
    model_id: Optional[str] = None,
    tool_ids: Optional[list[str]] = None,
    features: Optional[dict] = None,
    paused: Optional[bool] = None,
) -> AutomationModel:
    existing = await Automations.get_automation_by_id_and_user_id(
        automation_id, user.id
    )
    if existing is None:
        raise AutomationScheduleError("That automation doesn't exist.")

    updated: dict = {}
    if title is not None:
        if not title.strip():
            raise AutomationScheduleError("The automation needs a title.")
        updated["title"] = title.strip()
    if prompt is not None:
        if not prompt.strip():
            raise AutomationScheduleError("The automation needs a prompt to run.")
        updated["prompt"] = prompt.strip()
    if model_id is not None:
        if not model_id.strip():
            raise AutomationScheduleError("The automation needs a model to run with.")
        updated["model_id"] = model_id.strip()
    if tool_ids is not None:
        updated["tool_ids"] = sanitize_run_tool_ids(tool_ids)
    if features is not None:
        updated["features"] = sanitize_run_features(features)

    schedule_changed = any(
        value is not None and value != ""
        for value in (schedule, run_at, offset_minutes)
    )
    if schedule_changed:
        rrule, dtstart, tz = resolve_schedule(
            rrule=schedule,
            run_at=run_at,
            offset_minutes=offset_minutes,
            timezone=timezone or existing.timezone,
        )
        enforce_min_interval(rrule, dtstart, tz)
        updated["rrule"] = rrule
        updated["dtstart"] = dtstart
        updated["timezone"] = tz
    elif timezone is not None:
        # A zone change alone re-reads the SAME wall-clock rule against a
        # different zone, so the next run really does move.
        rrule, dtstart, tz = (
            existing.rrule,
            existing.dtstart,
            validate_timezone(timezone),
        )
        updated["timezone"] = tz
    else:
        rrule, dtstart, tz = existing.rrule, existing.dtstart, existing.timezone

    # A new schedule re-arms the automation. Both "paused" and "one-off that
    # already fired" read as inactive, and giving either a new time is
    # unambiguously a request for it to run again. An explicit `paused` in the
    # same call still wins.
    active = True if schedule_changed else existing.active
    if paused is not None:
        active = not paused
    reactivating = active and not existing.active
    if active != existing.active:
        updated["active"] = active

    if active:
        if reactivating:
            _enforce_active_cap(
                request, await Automations.count_active_by_user_id(user.id)
            )
        next_run_at = compute_next_run_at(rrule, dtstart, tz)
        if next_run_at is None and (schedule_changed or reactivating):
            raise AutomationScheduleError(
                "That schedule has no future runs. Pick a time in the future."
            )
        updated["next_run_at"] = next_run_at
    else:
        # Pausing disarms the row: it leaves the due index and stops counting
        # against the cap until it is resumed.
        updated["next_run_at"] = None

    automation = await Automations.update_automation_by_id_and_user_id(
        automation_id, user.id, updated
    )
    if automation is None:
        raise AutomationScheduleError("The automation could not be saved.")
    return automation


# --- scheduler ---------------------------------------------------------------


def _track_run_task(app, task: asyncio.Task) -> None:
    tasks = getattr(app.state, "automation_run_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.automation_run_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def sweep_due_automations(app) -> int:
    """One scheduler tick. Returns the number of runs started."""
    now = int(time.time())

    # Self-heal first so a run this pass starts can't be confused with one a
    # dead worker left behind.
    await AutomationRuns.sweep_stuck_runs(
        now - (AUTOMATION_RUN_TIMEOUT_SECONDS + _STUCK_RUN_GRACE_SECONDS)
    )

    started = 0
    for automation in await Automations.get_due_automations(now):
        if started >= _MAX_STARTS_PER_PASS:
            break

        observed = automation.next_run_at
        misfired = (now - observed) > AUTOMATION_MISFIRE_GRACE_SECONDS
        # A misfire skips the whole backlog in one step rather than walking it
        # occurrence by occurrence: coming back from a day of downtime must
        # advance the schedule quietly, not fire (or record) 24 hourly runs.
        new_next = compute_next_run_at(
            automation.rrule,
            automation.dtstart,
            automation.timezone,
            now if misfired else observed,
        )

        if not await Automations.claim_due(automation.id, observed, new_next):
            # Another worker claimed this occurrence.
            continue

        if misfired:
            log.info(
                "automation %s missed its %s run (grace %ss) — skipping to %s",
                automation.id,
                observed,
                AUTOMATION_MISFIRE_GRACE_SECONDS,
                new_next,
            )
            await AutomationRuns.create_run(
                automation.id, automation.user_id, status="missed"
            )
            await Automations.set_last_run_status(
                automation.id, "missed", deactivate=new_next is None
            )
            continue

        _track_run_task(
            app, asyncio.create_task(fire_automation(app, automation, new_next))
        )
        started += 1

    return started


# --- execution ---------------------------------------------------------------


async def _resolve_model(app, model_id: str, user) -> Optional[dict]:
    """The model this run must use, or None if it is gone (D4: no silent
    fallback — a run against a different model is not the run the user asked
    for)."""
    if not app.state.MODELS:
        from open_webui.utils.models import get_all_models

        await get_all_models(HeadlessRequest(app), user=user)
    return app.state.MODELS.get(model_id)


async def _run_outcome(chat_id: str, response_message_id: str) -> tuple[str, str, str]:
    """Classify the finished turn from the PERSISTED assistant message.

    Reads the same durable fields every other terminal path trusts, rather than
    inspecting the generation's return value — the pipeline owns that message
    and it is what the user will see when they open the run chat.
    Returns ``(status, preview, error)``.
    """
    from open_webui.utils.middleware import _provider_error_text
    from open_webui.utils.stream_state import terminal_status_from_message

    message = await Chats.get_message_by_id_and_message_id(chat_id, response_message_id)
    status = terminal_status_from_message(message)

    if status == "done":
        content = (message or {}).get("content")
        preview = content.strip() if isinstance(content, str) else ""
        return "completed", preview[:PREVIEW_MAX_LENGTH], ""
    if status == "error":
        return "error", "", _provider_error_text((message or {}).get("error"))
    if status == "cancelled":
        return "error", "", "The run was stopped before it finished."
    return "error", "", "The run ended without a response."


async def _finalize_run(
    app,
    automation: AutomationModel,
    run_id: str,
    chat_id: Optional[str],
    new_next: Optional[int],
    status: str,
    *,
    error: str = "",
    preview: str = "",
) -> None:
    await AutomationRuns.finalize_run(
        run_id, status, error=error or None, preview=preview or None
    )
    # A one-off has no next occurrence (new_next is None), so firing retires it.
    # That is also what keeps completed reminders from eating the active cap.
    await Automations.set_last_run_status(
        automation.id, status, deactivate=new_next is None
    )
    await notify_run_finished(
        app,
        automation,
        run_id=run_id,
        chat_id=chat_id,
        status=status,
        preview=preview,
        error=error,
    )


async def fire_automation(
    app, automation: AutomationModel, new_next: Optional[int]
) -> None:
    """Execute one claimed occurrence in a fresh hidden chat."""
    run = await AutomationRuns.create_run(automation.id, automation.user_id)
    if run is None:
        log.error("automation %s: could not record a run; skipping", automation.id)
        return

    user = await Users.get_user_by_id(automation.user_id)
    if user is None:
        await _finalize_run(
            app,
            automation,
            run.id,
            None,
            new_next,
            "error",
            error="The account that owns this automation no longer exists.",
        )
        return

    model = await _resolve_model(app, automation.model_id, user)
    if model is None:
        await _finalize_run(
            app,
            automation,
            run.id,
            None,
            new_next,
            "error",
            error=f"The model '{automation.model_id}' is no longer available.",
        )
        return

    local_now = datetime.now(ZoneInfo(automation.timezone))
    chat = await Chats.import_chat(
        user.id,
        ChatImportForm(
            **{
                "chat": {
                    "title": f"{automation.title} · {local_now:%b %-d, %Y}",
                    "models": [automation.model_id],
                    "history": {"messages": {}, "currentId": None},
                    "messages": [],
                    "params": {},
                },
                "meta": {"automation_of": automation.id},
            }
        ),
    )
    if chat is None:
        await _finalize_run(
            app,
            automation,
            run.id,
            None,
            new_next,
            "error",
            error="The chat for this run could not be created.",
        )
        return
    await AutomationRuns.set_chat_id(run.id, chat.id)

    response_message_id = str(uuid.uuid4())
    send_spec = {
        "model": automation.model_id,
        # First message of an empty chat: assembly persists the user row with a
        # null parent and walks from there (same path a browser's first send
        # takes when the completion POST arrives before any save).
        "leaf_message_id": None,
        "response_message_id": response_message_id,
        "new_user_message": {
            "id": str(uuid.uuid4()),
            "parentId": None,
            "role": "user",
            "content": automation.prompt,
            "files": [],
            "models": [automation.model_id],
        },
        "tool_ids": sanitize_run_tool_ids(automation.tool_ids),
        "features": sanitize_run_features(automation.features),
        "timezone": automation.timezone,
        # The chat's title is the automation's; nobody is browsing to it from
        # the sidebar, so generating one (and tags) is pure spend.
        "background_tasks": {"title_generation": False, "tags_generation": False},
        # Marks the run headless-without-a-human for the pipeline: this is what
        # keeps ask_user from being auto-injected into a run that could never
        # answer it (see _should_enable_ask_user_tool).
        "automation_run": automation.id,
    }

    from open_webui.main import start_generation

    try:
        async with _get_automation_concurrency_sem():
            await asyncio.wait_for(
                start_generation(chat.id, send_spec, user),
                timeout=AUTOMATION_RUN_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError:
        await _finalize_run(
            app,
            automation,
            run.id,
            chat.id,
            new_next,
            "timeout",
            error=(
                f"The run exceeded the {AUTOMATION_RUN_TIMEOUT_SECONDS}s time limit "
                "and was stopped."
            ),
        )
        return
    except asyncio.CancelledError:
        # Shutdown. Record it now (on a detached task the cancellation cannot
        # abort) so the run doesn't come back as a mystery `running` row.
        asyncio.ensure_future(
            _finalize_run(
                app,
                automation,
                run.id,
                chat.id,
                new_next,
                "error",
                error="The server shut down while this run was in flight.",
            )
        )
        raise
    except Exception as e:
        log.exception("automation %s run failed", automation.id)
        await _finalize_run(
            app, automation, run.id, chat.id, new_next, "error", error=str(e)
        )
        return

    status, preview, error = await _run_outcome(chat.id, response_message_id)
    await _finalize_run(
        app,
        automation,
        run.id,
        chat.id,
        new_next,
        status,
        error=error,
        preview=preview,
    )


def start_manual_run(app, automation: AutomationModel) -> None:
    """Fire an automation out of band (the page's "Run now").

    ``new_next=automation.next_run_at`` on purpose: a manual run must not retire
    a one-off or advance a schedule — the scheduled occurrence still owes the
    user its run. Tracked on ``app.state`` like a swept run so the task isn't
    garbage-collected mid-flight.
    """
    _track_run_task(
        app,
        asyncio.create_task(fire_automation(app, automation, automation.next_run_at)),
    )


# --- notification ------------------------------------------------------------


async def notify_run_finished(
    app,
    automation: AutomationModel,
    *,
    run_id: str,
    chat_id: Optional[str],
    status: str,
    preview: str = "",
    error: str = "",
) -> None:
    """Tell the user their automation finished — in-app and via web push.

    Fans out to every session (not just an elected primary): there is no live
    parent generation to route through, and the whole point is reaching the tab
    the user is actually looking at — or, with no tab at all, their device.
    """
    from open_webui.socket.main import emit_user_fanout
    from open_webui.utils.webpush import send_web_push_to_user

    body = preview or error or ""
    try:
        await emit_user_fanout(
            automation.user_id,
            {
                "chat_id": chat_id,
                "message_id": None,
                "data": {
                    "type": "automation:completed",
                    "data": {
                        "automation_id": automation.id,
                        "run_id": run_id,
                        "chat_id": chat_id,
                        "title": automation.title,
                        "status": status,
                        "content": body,
                    },
                },
            },
        )
    except Exception:
        log.exception("automation %s: in-app notification failed", automation.id)

    try:
        await send_web_push_to_user(
            app,
            automation.user_id,
            {
                "title": automation.title,
                "body": body,
                "url": f"/c/{chat_id}" if chat_id else "/automations",
                "tag": f"automation-{automation.id}",
            },
        )
    except Exception:
        log.exception("automation %s: web push failed", automation.id)


def schedule_text(automation: AutomationModel) -> str:
    return describe_schedule(automation.rrule, automation.dtstart, automation.timezone)
