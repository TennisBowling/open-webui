from typing import Optional
import io
import base64
import hashlib
import json
import asyncio
import logging

from open_webui.models.models import (
    ModelForm,
    ModelModel,
    ModelResponse,
    ModelUserResponse,
    Models,
)

from pydantic import BaseModel
from open_webui.constants import ERROR_MESSAGES
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    status,
    Response,
)
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import FileResponse, StreamingResponse


from open_webui.utils.auth import (
    get_admin_user,
    get_verified_user,
    get_current_user,
    bearer_security,
)
from open_webui.utils.access_control import has_access_async, has_permission_async
from open_webui.config import BYPASS_ADMIN_ACCESS_CONTROL, STATIC_DIR

log = logging.getLogger(__name__)

router = APIRouter()


def validate_model_id(model_id: str) -> bool:
    return model_id and len(model_id) <= 256


###########################
# GetModels
###########################


@router.get("/", response_model=list[ModelUserResponse])
async def get_models(id: Optional[str] = None, user=Depends(get_verified_user)):
    if user.role == "admin" and BYPASS_ADMIN_ACCESS_CONTROL:
        return await Models.get_models()
    else:
        return await Models.get_models_by_user_id(user.id)


###########################
# GetBaseModels
###########################


@router.get("/base", response_model=list[ModelResponse])
async def get_base_models(user=Depends(get_admin_user)):
    return await Models.get_base_models()


############################
# CreateNewModel
############################


@router.post("/create", response_model=Optional[ModelModel])
async def create_new_model(
    request: Request,
    form_data: ModelForm,
    user=Depends(get_verified_user),
):
    if user.role != "admin" and not await has_permission_async(
        user.id, "workspace.models", request.app.state.config.USER_PERMISSIONS
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    model = await Models.get_model_by_id(form_data.id)
    if model:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.MODEL_ID_TAKEN,
        )

    if not validate_model_id(form_data.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.MODEL_ID_TOO_LONG,
        )

    else:
        model = await Models.insert_new_model(form_data, user.id)
        if model:
            return model
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.DEFAULT(),
            )


############################
# ExportModels
############################


@router.get("/export", response_model=list[ModelModel])
async def export_models(user=Depends(get_admin_user)):
    return await Models.get_models()


############################
# ImportModels
############################


class ModelsImportForm(BaseModel):
    models: list[dict]


@router.post("/import", response_model=bool)
async def import_models(
    user: str = Depends(get_admin_user), form_data: ModelsImportForm = (...)
):
    try:
        data = form_data.models
        if isinstance(data, list):
            for model_data in data:
                # Here, you can add logic to validate model_data if needed
                model_id = model_data.get("id")

                if model_id and validate_model_id(model_id):
                    existing_model = await Models.get_model_by_id(model_id)
                    if existing_model:
                        # Update existing model
                        model_data["meta"] = model_data.get("meta", {})
                        model_data["params"] = model_data.get("params", {})

                        updated_model = ModelForm(
                            **{**existing_model.model_dump(), **model_data}
                        )
                        await Models.update_model_by_id(model_id, updated_model)
                    else:
                        # Insert new model
                        model_data["meta"] = model_data.get("meta", {})
                        model_data["params"] = model_data.get("params", {})
                        new_model = ModelForm(**model_data)
                        await Models.insert_new_model(user_id=user.id, form_data=new_model)
            return True
        else:
            raise HTTPException(status_code=400, detail="Invalid JSON format")
    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=500, detail=str(e))


############################
# SyncModels
############################


class SyncModelsForm(BaseModel):
    models: list[ModelModel] = []


@router.post("/sync", response_model=list[ModelModel])
async def sync_models(
    request: Request, form_data: SyncModelsForm, user=Depends(get_admin_user)
):
    return await Models.sync_models(user.id, form_data.models)


###########################
# GetModelById
###########################


# Note: We're not using the typical url path param here, but instead using a query parameter to allow '/' in the id
@router.get("/model", response_model=Optional[ModelResponse])
async def get_model_by_id(id: str, user=Depends(get_verified_user)):
    model = await Models.get_model_by_id(id)
    if model:
        if (
            (user.role == "admin" and BYPASS_ADMIN_ACCESS_CONTROL)
            or model.user_id == user.id
            or await has_access_async(user.id, "read", model.access_control)
        ):
            return model
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


###########################
# GetModelById
###########################


async def get_optional_user(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    auth_token: Optional[HTTPAuthorizationCredentials] = Depends(bearer_security),
):
    # Best-effort variant of get_current_user for endpoints that must degrade
    # gracefully (e.g. <img> tags) instead of surfacing a 401 JSON body, which
    # the browser renders as a broken-image glyph.
    try:
        return await get_current_user(request, response, background_tasks, auth_token)
    except HTTPException:
        return None


@router.get("/model/profile/image")
async def get_model_profile_image(
    id: str, request: Request, user=Depends(get_optional_user)
):
    # Served as an <img src> (cookie-authenticated, no Authorization header) —
    # get_current_user already falls back to the `token` cookie, so this works.
    # Unauthenticated requests (e.g. an expired/missing cookie) fall back to
    # the public favicon instead of a 401, which the browser would otherwise
    # render as a broken-image glyph. Never serve the real avatar or redirect
    # to a configured external URL for unauthenticated callers.
    if user is None:
        return FileResponse(
            f"{STATIC_DIR}/favicon.png",
            headers={"Cache-Control": "public, max-age=300"},
        )

    model = await Models.get_model_by_id(id)
    if model:
        if model.meta.profile_image_url:
            if model.meta.profile_image_url.startswith("http"):
                return Response(
                    status_code=status.HTTP_302_FOUND,
                    headers={
                        "Location": model.meta.profile_image_url,
                        "Cache-Control": "private, max-age=3600",
                    },
                )
            elif model.meta.profile_image_url.startswith("data:image"):
                try:
                    data_uri = model.meta.profile_image_url
                    # ETag == the `v` hash the list URL is versioned by, so an
                    # unchanged avatar 304s and a changed one busts the cache.
                    etag = (
                        '"'
                        + hashlib.sha256(data_uri.encode("utf-8")).hexdigest()[:16]
                        + '"'
                    )
                    cache_control = "private, max-age=31536000, immutable"

                    inm = request.headers.get("if-none-match")
                    if inm and etag.strip('"') in {
                        part.strip().strip('"') for part in inm.split(",")
                    }:
                        return Response(
                            status_code=status.HTTP_304_NOT_MODIFIED,
                            headers={"ETag": etag, "Cache-Control": cache_control},
                        )

                    header, base64_data = data_uri.split(",", 1)
                    image_data = base64.b64decode(base64_data)
                    image_buffer = io.BytesIO(image_data)

                    return StreamingResponse(
                        image_buffer,
                        media_type="image/png",
                        headers={
                            "Content-Disposition": "inline; filename=image.png",
                            "ETag": etag,
                            "Cache-Control": cache_control,
                        },
                    )
                except Exception as e:
                    pass
        return FileResponse(
            f"{STATIC_DIR}/favicon.png",
            headers={"Cache-Control": "public, max-age=300"},
        )
    else:
        return FileResponse(
            f"{STATIC_DIR}/favicon.png",
            headers={"Cache-Control": "public, max-age=300"},
        )


############################
# ToggleModelById
############################


@router.post("/model/toggle", response_model=Optional[ModelResponse])
async def toggle_model_by_id(id: str, user=Depends(get_verified_user)):
    model = await Models.get_model_by_id(id)
    if model:
        if (
            user.role == "admin"
            or model.user_id == user.id
            or await has_access_async(user.id, "write", model.access_control)
        ):
            model = await Models.toggle_model_by_id(id)

            if model:
                return model
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ERROR_MESSAGES.DEFAULT("Error updating function"),
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.UNAUTHORIZED,
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )


############################
# UpdateModelById
############################


@router.post("/model/update", response_model=Optional[ModelModel])
async def update_model_by_id(
    id: str,
    form_data: ModelForm,
    user=Depends(get_verified_user),
):
    model = await Models.get_model_by_id(id)

    if not model:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        model.user_id != user.id
        and not await has_access_async(user.id, "write", model.access_control)
        and user.role != "admin"
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    model = await Models.update_model_by_id(id, form_data)
    return model


############################
# DeleteModelById
############################


@router.delete("/model/delete", response_model=bool)
async def delete_model_by_id(id: str, user=Depends(get_verified_user)):
    model = await Models.get_model_by_id(id)
    if not model:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        user.role != "admin"
        and model.user_id != user.id
        and not await has_access_async(user.id, "write", model.access_control)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    result = await Models.delete_model_by_id(id)
    return result


@router.delete("/delete/all", response_model=bool)
async def delete_all_models(user=Depends(get_admin_user)):
    result = await Models.delete_all_models()
    return result
