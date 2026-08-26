import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from open_webui.env import SRC_LOG_LEVELS
from open_webui.models.push import PushSubscriptions
from open_webui.utils.auth import get_verified_user

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

router = APIRouter()


class PushSubscribeForm(BaseModel):
    """The browser's ``PushSubscription.toJSON()`` shape, posted verbatim."""

    endpoint: str
    keys: dict


@router.post("/subscribe")
async def subscribe(
    request: Request, form_data: PushSubscribeForm, user=Depends(get_verified_user)
):
    if not (form_data.keys.get("p256dh") and form_data.keys.get("auth")):
        raise HTTPException(status_code=400, detail="Incomplete push subscription")
    subscription = await PushSubscriptions.upsert(
        user.id,
        form_data.endpoint,
        {"p256dh": form_data.keys["p256dh"], "auth": form_data.keys["auth"]},
        request.headers.get("user-agent"),
    )
    if subscription is None:
        raise HTTPException(status_code=400, detail="Failed to save push subscription")
    return {"status": True}


class PushUnsubscribeForm(BaseModel):
    endpoint: str


@router.post("/unsubscribe")
async def unsubscribe(
    form_data: PushUnsubscribeForm, user=Depends(get_verified_user)
):
    return {
        "status": await PushSubscriptions.delete_by_user_and_endpoint(
            user.id, form_data.endpoint
        )
    }
