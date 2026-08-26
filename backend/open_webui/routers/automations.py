import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from open_webui.constants import ERROR_MESSAGES
from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.automations import (
    AutomationRunModel,
    AutomationRuns,
    Automations,
)
from open_webui.utils.auth import get_verified_user
from open_webui.utils.automation_runner import (
    create_automation,
    schedule_text,
    start_manual_run,
    update_automation,
)
from open_webui.utils.automation_schedule import AutomationScheduleError

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

router = APIRouter()


class AutomationForm(BaseModel):
    title: str
    prompt: str
    schedule: Optional[str] = None
    run_at: Optional[str] = None
    offset_minutes: Optional[int] = None
    timezone: Optional[str] = None
    model_id: str
    tool_ids: list[str] = []
    features: dict = {}


class AutomationUpdateForm(BaseModel):
    title: Optional[str] = None
    prompt: Optional[str] = None
    schedule: Optional[str] = None
    run_at: Optional[str] = None
    offset_minutes: Optional[int] = None
    timezone: Optional[str] = None
    model_id: Optional[str] = None
    tool_ids: Optional[list[str]] = None
    features: Optional[dict] = None
    paused: Optional[bool] = None


def _require_enabled(request: Request) -> None:
    if not bool(getattr(request.app.state.config, "ENABLE_AUTOMATIONS", False)):
        raise HTTPException(status_code=403, detail="Automations are disabled")


def _public(automation) -> dict:
    # schedule_text is derived, not stored: the RRULE is the source of truth and
    # a cached description would drift the moment the timezone changed.
    return {**automation.model_dump(), "schedule_text": schedule_text(automation)}


async def _get_owned(automation_id: str, user):
    automation = await Automations.get_automation_by_id_and_user_id(
        automation_id, user.id
    )
    if automation is None:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    return automation


@router.get("/")
async def get_automations(request: Request, user=Depends(get_verified_user)):
    _require_enabled(request)
    return [
        _public(automation)
        for automation in await Automations.get_automations_by_user_id(user.id)
    ]


@router.post("/create")
async def create_new_automation(
    request: Request, form_data: AutomationForm, user=Depends(get_verified_user)
):
    _require_enabled(request)
    try:
        automation = await create_automation(
            request,
            user,
            title=form_data.title,
            prompt=form_data.prompt,
            schedule=form_data.schedule,
            run_at=form_data.run_at,
            offset_minutes=form_data.offset_minutes,
            timezone=form_data.timezone,
            model_id=form_data.model_id,
            tool_ids=form_data.tool_ids,
            features=form_data.features,
        )
    except AutomationScheduleError as e:
        # The same clean, user-facing wording the builtin tool relays to the
        # model — one validation vocabulary for both front doors.
        raise HTTPException(status_code=400, detail=str(e))
    return _public(automation)


@router.get("/runs/unread/count")
async def get_unread_run_count(request: Request, user=Depends(get_verified_user)):
    _require_enabled(request)
    return {"count": await AutomationRuns.count_unseen(user.id)}


@router.post("/runs/read")
async def mark_runs_read(request: Request, user=Depends(get_verified_user)):
    _require_enabled(request)
    await AutomationRuns.mark_all_seen(user.id)
    return {"status": True}


@router.get("/{automation_id}")
async def get_automation_by_id(
    request: Request, automation_id: str, user=Depends(get_verified_user)
):
    _require_enabled(request)
    return _public(await _get_owned(automation_id, user))


@router.post("/{automation_id}/update")
async def update_automation_by_id(
    request: Request,
    automation_id: str,
    form_data: AutomationUpdateForm,
    user=Depends(get_verified_user),
):
    _require_enabled(request)
    try:
        automation = await update_automation(
            request,
            user,
            automation_id,
            **form_data.model_dump(exclude_unset=True),
        )
    except AutomationScheduleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _public(automation)


@router.post("/{automation_id}/toggle")
async def toggle_automation_by_id(
    request: Request, automation_id: str, user=Depends(get_verified_user)
):
    _require_enabled(request)
    automation = await _get_owned(automation_id, user)
    try:
        automation = await update_automation(
            request, user, automation_id, paused=automation.active
        )
    except AutomationScheduleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _public(automation)


@router.post("/{automation_id}/run")
async def run_automation_by_id(
    request: Request, automation_id: str, user=Depends(get_verified_user)
):
    _require_enabled(request)
    automation = await _get_owned(automation_id, user)
    # Detached: a run is a full generation and the caller only needs to know it
    # started. Progress arrives on the socket, the outcome lands in run history.
    start_manual_run(request.app, automation)
    return {"status": True}


@router.get("/{automation_id}/runs", response_model=list[AutomationRunModel])
async def get_automation_runs(
    request: Request, automation_id: str, user=Depends(get_verified_user)
):
    _require_enabled(request)
    await _get_owned(automation_id, user)
    return await AutomationRuns.get_runs_by_automation_id(automation_id, user.id)


@router.delete("/{automation_id}")
async def delete_automation_by_id(
    request: Request, automation_id: str, user=Depends(get_verified_user)
):
    _require_enabled(request)
    return {"status": await Automations.delete_automation_by_id_and_user_id(
        automation_id, user.id
    )}
