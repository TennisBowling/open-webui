"""Admin config + user-initiated rerun endpoints for the subagents feature.

The ``/config`` GET/POST pair mirrors the shape of
``routers/retrieval.py``'s admin-config endpoints. The ``/rerun`` POST is the
user-side hook for the "Redo this turn" / "Restart from beginning" buttons
inside a SubagentBlock in the parent chat UI — it spawns the rerun as a
background asyncio task and returns immediately; live progress streams back
to the user via the same ``chat:subagent:update`` socket events the original
launch used.
"""

import asyncio
import logging
from typing import Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from open_webui.env import SRC_LOG_LEVELS
from open_webui.tasks import (
    ChatWorkBlockedError,
    create_task,
    is_chat_work_blocked,
    list_item_keys_by_prefix,
    list_task_ids_by_item_id,
    stop_task,
)
from open_webui.socket.main import broadcast_sidebar_event
from open_webui.utils.auth import get_admin_user, get_verified_user

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

router = APIRouter()


class SubagentConfigForm(BaseModel):
    # All optional so the admin UI can PATCH-style update only fields it cares
    # about without overwriting the others.
    ENABLE_SUBAGENTS: Optional[bool] = None
    SUBAGENT_DEFAULT_MODEL: Optional[str] = None
    SUBAGENT_CONTEXT_FALLBACK_MODEL: Optional[str] = None
    SUBAGENT_SYSTEM_PROMPT: Optional[str] = None
    SUBAGENT_SYSTEM_PROMPT_APPEND: Optional[str] = None
    SUBAGENT_PARENT_PROMPT: Optional[str] = None
    SUBAGENT_DEFAULT_REASONING_EFFORT: Optional[str] = None
    SUBAGENT_DEFAULT_SERVICE_TIER: Optional[str] = None
    SUBAGENT_ALLOW_EXTERNAL_TOOLS: Optional[bool] = None
    SUBAGENT_EXTERNAL_TOOLS_PROMPT: Optional[str] = None


def _serialize(request: Request) -> dict:
    config = request.app.state.config
    return {
        "status": True,
        "ENABLE_SUBAGENTS": config.ENABLE_SUBAGENTS,
        "SUBAGENT_DEFAULT_MODEL": config.SUBAGENT_DEFAULT_MODEL,
        "SUBAGENT_CONTEXT_FALLBACK_MODEL": getattr(
            config, "SUBAGENT_CONTEXT_FALLBACK_MODEL", ""
        ),
        "SUBAGENT_SYSTEM_PROMPT": config.SUBAGENT_SYSTEM_PROMPT,
        "SUBAGENT_SYSTEM_PROMPT_APPEND": getattr(
            config, "SUBAGENT_SYSTEM_PROMPT_APPEND", ""
        ),
        "SUBAGENT_PARENT_PROMPT": config.SUBAGENT_PARENT_PROMPT,
        "SUBAGENT_DEFAULT_REASONING_EFFORT": config.SUBAGENT_DEFAULT_REASONING_EFFORT,
        "SUBAGENT_DEFAULT_SERVICE_TIER": config.SUBAGENT_DEFAULT_SERVICE_TIER,
        "SUBAGENT_ALLOW_EXTERNAL_TOOLS": getattr(
            config, "SUBAGENT_ALLOW_EXTERNAL_TOOLS", False
        ),
        "SUBAGENT_EXTERNAL_TOOLS_PROMPT": getattr(
            config, "SUBAGENT_EXTERNAL_TOOLS_PROMPT", ""
        ),
    }


@router.get("/config")
async def get_subagents_config(request: Request, user=Depends(get_admin_user)):
    return _serialize(request)


@router.post("/config/update")
async def update_subagents_config(
    request: Request, form_data: SubagentConfigForm, user=Depends(get_admin_user)
):
    config = request.app.state.config
    if form_data.ENABLE_SUBAGENTS is not None:
        config.ENABLE_SUBAGENTS = form_data.ENABLE_SUBAGENTS
    if form_data.SUBAGENT_DEFAULT_MODEL is not None:
        config.SUBAGENT_DEFAULT_MODEL = form_data.SUBAGENT_DEFAULT_MODEL
    if form_data.SUBAGENT_CONTEXT_FALLBACK_MODEL is not None:
        config.SUBAGENT_CONTEXT_FALLBACK_MODEL = (
            form_data.SUBAGENT_CONTEXT_FALLBACK_MODEL
        )
    if form_data.SUBAGENT_SYSTEM_PROMPT is not None:
        config.SUBAGENT_SYSTEM_PROMPT = form_data.SUBAGENT_SYSTEM_PROMPT
    if form_data.SUBAGENT_SYSTEM_PROMPT_APPEND is not None:
        try:
            config.SUBAGENT_SYSTEM_PROMPT_APPEND = (
                form_data.SUBAGENT_SYSTEM_PROMPT_APPEND
            )
        except (AttributeError, KeyError):
            pass  # config key not registered yet — requires server restart
    if form_data.SUBAGENT_PARENT_PROMPT is not None:
        config.SUBAGENT_PARENT_PROMPT = form_data.SUBAGENT_PARENT_PROMPT
    if form_data.SUBAGENT_DEFAULT_REASONING_EFFORT is not None:
        config.SUBAGENT_DEFAULT_REASONING_EFFORT = (
            form_data.SUBAGENT_DEFAULT_REASONING_EFFORT
        )
    if form_data.SUBAGENT_DEFAULT_SERVICE_TIER is not None:
        config.SUBAGENT_DEFAULT_SERVICE_TIER = form_data.SUBAGENT_DEFAULT_SERVICE_TIER
    if form_data.SUBAGENT_ALLOW_EXTERNAL_TOOLS is not None:
        config.SUBAGENT_ALLOW_EXTERNAL_TOOLS = form_data.SUBAGENT_ALLOW_EXTERNAL_TOOLS
    if form_data.SUBAGENT_EXTERNAL_TOOLS_PROMPT is not None:
        config.SUBAGENT_EXTERNAL_TOOLS_PROMPT = form_data.SUBAGENT_EXTERNAL_TOOLS_PROMPT
    return _serialize(request)


class SubagentRerunForm(BaseModel):
    parent_chat_id: str
    parent_message_id: str
    # Caller's current socket session — used so the rerun's events route to
    # the user's open browser tab. The session_id is what
    # ``get_event_emitter`` uses to ``sio.emit(... to=session_id)``.
    session_id: str
    # Key into ``parent_message.subagent_runs`` for the block the user clicked
    # the redo button on.
    entry_key: str
    # ``"this_turn"``: re-run just the user→assistant pair this entry owns.
    # ``"from_launch"``: wipe the subagent chat and re-run from the original
    # launch's prompt + background.
    scope: Literal["this_turn", "from_launch"]


class SubagentRerunStopForm(BaseModel):
    parent_chat_id: str
    parent_message_id: str
    entry_key: str
    subagent_id: Optional[str] = None


class SubagentAdoptForm(BaseModel):
    parent_chat_id: str
    parent_message_id: str
    entry_key: str


class SubagentRewindForm(BaseModel):
    parent_chat_id: str
    source_parent_message_id: str
    branch_message_id: str
    entry_keys: list[str]
    operation_id: Optional[str] = None


async def _broadcast_rewind_commit(request: Request, user, result: dict) -> None:
    """Notify other sessions after an atomic rewind branch commit."""
    from open_webui.models.chats import Chats

    try:
        chat = await Chats.get_chat_by_id(result["parent_chat_id"])
        if chat is not None:
            await broadcast_sidebar_event(
                user.id,
                {
                    "type": "chat:updated",
                    "data": {
                        "id": chat.id,
                        "title": chat.title,
                        "updated_at": chat.updated_at,
                        "created_at": chat.created_at,
                        "pinned": bool(chat.pinned or False),
                        "archived": bool(chat.archived or False),
                        "folder_id": chat.folder_id,
                    },
                },
                skip_sid=request.headers.get("x-session-id"),
            )
    except Exception:
        log.exception("atomic rewind sidebar broadcast failed")


@router.post("/adopt")
async def adopt_subagent_result(
    form_data: SubagentAdoptForm,
    user=Depends(get_verified_user),
):
    """Use the selected answer from a manually repaired full subagent chat.

    The utility enforces the same transcript-consumption guard as a redo. A 409
    tells the client to create a rewind sibling, adopt there, and only then
    resume the parent model.
    """
    from open_webui.models.chats import Chats
    from open_webui.utils.subagent import (
        SubagentRerunBlockedError,
        adopt_subagent_current_result,
    )

    parent_chat = await Chats.get_chat_by_id_and_user_id(
        form_data.parent_chat_id, user.id
    )
    if parent_chat is None:
        raise HTTPException(status_code=404, detail="Parent chat not found")
    if await is_chat_work_blocked(request.app.state.redis, form_data.parent_chat_id):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "The parent chat is being deleted.",
                "code": "chat_work_blocked",
            },
        )

    try:
        result = await adopt_subagent_current_result(
            user=user,
            parent_chat_id=form_data.parent_chat_id,
            parent_message_id=form_data.parent_message_id,
            entry_key=form_data.entry_key,
        )
    except SubagentRerunBlockedError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e),
                "code": getattr(e, "code", "subagent_rerun_blocked"),
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": True, **result}


@router.post("/adopt/rewind")
async def rewind_and_adopt_subagent_results(
    request: Request,
    form_data: SubagentRewindForm,
    user=Depends(get_verified_user),
):
    """Commit one rewind sibling with all repaired answers, or commit nothing.

    The old UI sequence appended a sibling and then issued one independent
    ``/adopt`` request per run. An error, reload, or disconnect in the middle
    left a durable half-adopted message. This endpoint preflights every selected
    hidden leaf and delegates one guarded DB transaction that appends the
    sibling, patches all run/result pairs, and advances the branch pointer.
    """
    from open_webui.models.chats import ChatBranchConflictError
    from open_webui.utils.subagent import (
        SubagentRerunBlockedError,
        rewind_adopt_subagent_current_results,
    )

    entry_keys = [str(key or "") for key in form_data.entry_keys if str(key or "")]
    if not entry_keys:
        raise HTTPException(status_code=400, detail="No subagent answers selected")
    if len(entry_keys) > 100:
        raise HTTPException(
            status_code=400, detail="Too many subagent answers selected"
        )

    try:
        result = await rewind_adopt_subagent_current_results(
            user=user,
            parent_chat_id=form_data.parent_chat_id,
            source_parent_message_id=form_data.source_parent_message_id,
            branch_message_id=form_data.branch_message_id,
            entry_keys=entry_keys,
            operation_id=form_data.operation_id,
        )
    except (SubagentRerunBlockedError, ChatBranchConflictError) as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e),
                "code": getattr(e, "code", "rewind_adopt_conflict"),
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # The source tab installs the returned branch locally; every other active
    # session invalidates and fetches the new pointer.
    await _broadcast_rewind_commit(request, user, result)

    return {"status": True, **result}


@router.post("/rerun/rewind")
async def rewind_subagents_for_rerun(
    request: Request,
    form_data: SubagentRewindForm,
    user=Depends(get_verified_user),
):
    """Atomically create the checkpoint consumed by detached subagent reruns."""
    from open_webui.models.chats import ChatBranchConflictError
    from open_webui.utils.subagent import (
        SubagentRerunBlockedError,
        rewind_subagent_runs_for_rerun,
    )

    entry_keys = [str(key or "") for key in form_data.entry_keys if str(key or "")]
    if not entry_keys:
        raise HTTPException(status_code=400, detail="No subagents selected")
    if len(entry_keys) > 100:
        raise HTTPException(status_code=400, detail="Too many subagents selected")

    try:
        result = await rewind_subagent_runs_for_rerun(
            user=user,
            parent_chat_id=form_data.parent_chat_id,
            source_parent_message_id=form_data.source_parent_message_id,
            branch_message_id=form_data.branch_message_id,
            entry_keys=entry_keys,
            operation_id=form_data.operation_id,
        )
    except (SubagentRerunBlockedError, ChatBranchConflictError) as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e),
                "code": getattr(e, "code", "rewind_rerun_conflict"),
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await _broadcast_rewind_commit(request, user, result)
    return {"status": True, **result}


@router.post("/rerun")
async def rerun_subagent(
    request: Request,
    form_data: SubagentRerunForm,
    user=Depends(get_verified_user),
):
    """Spawn a registered subagent rerun task and return immediately.

    Live progress comes back via ``chat:subagent:update`` socket events
    scoped to the parent chat — the same path the original launch uses, so
    the existing handler in ``Chat.svelte`` updates ``$subagentLiveStates``
    and the SubagentBlock re-renders in place.
    """
    from open_webui.utils.subagent import (
        SubagentRerunBlockedError,
        finalize_detached_rerun_claim,
        rerun_subagent_turn,
        validate_subagent_rerun_allowed,
    )

    # Pre-validate access here (cheap + gives a synchronous 4xx) so we don't
    # silently swallow auth/lookup errors inside the background task.
    from open_webui.models.chats import Chats

    parent_chat = await Chats.get_chat_by_id_and_user_id(
        form_data.parent_chat_id, user.id
    )
    if parent_chat is None:
        raise HTTPException(status_code=404, detail="Parent chat not found")

    # C2: enforce the subagent gate here too — /rerun is a separate entry point and
    # must not bypass the global toggle / per-user permission the chat path enforces.
    if not getattr(request.app.state.config, "ENABLE_SUBAGENTS", False):
        raise HTTPException(status_code=403, detail="Subagents are disabled")
    if getattr(user, "role", None) != "admin":
        from open_webui.utils.access_control import has_permission_async

        if not await has_permission_async(
            user.id, "features.subagents", request.app.state.config.USER_PERMISSIONS
        ):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to use subagents",
            )

    # C17: the rerun forwards live events with force_fanout, which adds origin_sid
    # (the caller-supplied session_id) to the recipient set WITHOUT verifying it
    # belongs to the caller. Validate it; if it isn't one of this user's sessions,
    # drop it (USER_POOL fan-out to the caller's own tabs still delivers) so a client
    # can't push its rerun's events onto another user's screen.
    safe_session_id = ""
    if form_data.session_id:
        try:
            from open_webui.socket.main import SESSION_POOL

            _sess = SESSION_POOL.get(form_data.session_id)
            if isinstance(_sess, dict) and _sess.get("id") == user.id:
                safe_session_id = form_data.session_id
        except Exception:
            safe_session_id = ""

    try:
        preflight = await validate_subagent_rerun_allowed(
            user=user,
            parent_chat_id=form_data.parent_chat_id,
            parent_message_id=form_data.parent_message_id,
            entry_key=form_data.entry_key,
            scope=form_data.scope,
        )
    except SubagentRerunBlockedError as e:
        # Structured detail so the client can distinguish a "parent moved on"
        # block (offer the rewind & redo flow) from a hard block (toast the
        # message). `code` defaults to "subagent_rerun_blocked"; the four
        # parent-moved-on clauses tag it "subagent_parent_moved_on".
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e),
                "code": getattr(e, "code", "subagent_rerun_blocked"),
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    effective_parent_chat_id = str(
        preflight.get("parent_chat_id") or form_data.parent_chat_id
    )
    effective_parent_message_id = str(
        preflight.get("parent_message_id") or form_data.parent_message_id
    )
    write_message_id = str(
        preflight.get("write_message_id") or effective_parent_message_id
    )
    write_entry_key = str(preflight.get("write_entry_key") or form_data.entry_key)
    rerun_id = str(uuid4())

    async def _run():
        fallback_status = "error"
        failure_message: Optional[str] = None
        try:
            try:
                await rerun_subagent_turn(
                    request=request,
                    user=user,
                    parent_chat_id=effective_parent_chat_id,
                    parent_message_id=effective_parent_message_id,
                    session_id=safe_session_id,
                    entry_key=form_data.entry_key,
                    scope=form_data.scope,
                    rerun_id=rerun_id,
                )
            except SubagentRerunBlockedError as e:
                failure_message = str(e)
                log.info(f"subagent rerun blocked after preflight: {e}")
                try:
                    from open_webui.socket.main import get_event_emitter

                    await get_event_emitter(
                        {
                            "user_id": user.id,
                            # Use only the ownership-validated session. The raw
                            # caller-supplied id must never become an event target.
                            "session_id": safe_session_id,
                            "chat_id": effective_parent_chat_id,
                            "message_id": write_message_id,
                        }
                    )(
                        {
                            "type": "notification",
                            "data": {"type": "error", "content": str(e)},
                        }
                    )
                except Exception:
                    pass
            except asyncio.CancelledError:
                fallback_status = "cancelled"
                raise
            except Exception as e:
                failure_message = str(e)
                log.exception(f"subagent rerun task failed: {e}")
        finally:
            # Claim-scoped backstop for the setup window: if cancellation or an
            # unexpected exception escaped after terminal->running CAS but before
            # the inner handlers took over, resolve only THIS rerun generation.
            # A completed/errored run is already terminal and remains untouched.
            # The finalizer (and every ordinary terminal path) rotates the chat
            # validator in the same DB transaction as the terminal run/result.
            async def _finish_claim() -> None:
                await finalize_detached_rerun_claim(
                    parent_chat_id=effective_parent_chat_id,
                    parent_message_id=write_message_id,
                    entry_key=write_entry_key,
                    rerun_id=rerun_id,
                    fallback_status=fallback_status,
                    error_message=failure_message,
                )

            cleanup_task = asyncio.create_task(_finish_claim())
            # A bare shield is insufficient: a second Stop re-cancels the outer
            # task and makes shield raise immediately while the cleanup keeps
            # running detached. Keep re-awaiting the one protected task until its
            # generation-guarded terminal write has completed;
            # the original cancellation propagates when this finally block exits.
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
            if cleanup_task.cancelled():
                log.error("post-rerun cleanup was unexpectedly cancelled")
            elif cleanup_task.exception() is not None:
                error = cleanup_task.exception()
                log.error(
                    "post-rerun cleanup failed",
                    exc_info=(type(error), error, error.__traceback__),
                )

    # Register under a DISTINCT task id (not the parent chat id) so a
    # parent-generation cancel can't tear down an independent redo the user
    # kicked off. Keyed by the entry so a targeted "stop this redo" can find it.
    # An explicit chat-wide Stop DOES reach these — it opts in via
    # `include_subagent_reruns` and stops them by this same prefix.
    try:
        task_id, _ = await create_task(
            request.app.state.redis,
            _run(),
            id=f"subagent-rerun:{effective_parent_chat_id}:{form_data.entry_key}",
            admission_chat_id=effective_parent_chat_id,
        )
    except ChatWorkBlockedError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(error),
                "code": "chat_work_blocked",
            },
        ) from error
    return {"status": True, "task_id": task_id, "rerun_id": rerun_id}


@router.post("/rerun/stop")
async def stop_subagent_rerun(
    request: Request,
    form_data: SubagentRerunStopForm,
    user=Depends(get_verified_user),
):
    """Stop a detached subagent rerun task for one visible card.

    This is the TARGETED counterpart to the chat-wide Stop (which halts every
    rerun in the chat): it cancels exactly one card's redo and leaves the rest of
    the chat's background work alone. It matches both the clicked rerun key and
    the underlying subagent id because a ``from_launch`` rerun clicked on a
    continuation is registered under the clicked continuation key while the live
    run entry flips the launch key.
    """
    from open_webui.utils.subagent import (
        load_effective_parent_chat_for_subagent_action,
    )

    try:
        effective_parent_chat_id, parent_chat = (
            await load_effective_parent_chat_for_subagent_action(
                form_data.parent_chat_id, user
            )
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Parent chat not found")

    prefix = f"subagent-rerun:{effective_parent_chat_id}:"
    wanted = {
        str(v)
        for v in (form_data.entry_key, form_data.subagent_id)
        if isinstance(v, str) and v
    }

    def _matches(suffix: str) -> bool:
        if not suffix or not wanted:
            return False
        if suffix in wanted:
            return True
        for w in wanted:
            if suffix.startswith(f"{w}#") or w.startswith(f"{suffix}#"):
                return True
        return False

    item_keys: list[str] = []
    try:
        item_keys = await list_item_keys_by_prefix(request.app.state.redis, prefix)
    except Exception:
        log.exception(
            "listing subagent rerun item keys failed for chat %s",
            effective_parent_chat_id,
        )
        item_keys = []

    matching_items = [
        k
        for k in item_keys
        if _matches(k[len(prefix) :] if k.startswith(prefix) else k)
    ]
    # Fallback for an exact key when the prefix scan is unavailable/empty.
    exact_item = f"{prefix}{form_data.entry_key}"
    if exact_item not in matching_items:
        matching_items.append(exact_item)

    task_ids: list[str] = []
    for item_id in matching_items:
        try:
            task_ids.extend(
                await list_task_ids_by_item_id(request.app.state.redis, item_id)
            )
        except Exception:
            log.exception("listing tasks for subagent rerun item %s failed", item_id)

    # De-duplicate while preserving order.
    task_ids = list(dict.fromkeys(task_ids))
    for task_id in task_ids:
        await stop_task(request.app.state.redis, task_id)

    if not task_ids:
        # If the task registry no longer has the rerun, it may already have died.
        # Run the stranded reconciler so a stuck card can still resolve on the next
        # load/poll instead of staying forever at `running`.
        try:
            from open_webui.utils.subagent import reconcile_stranded_subagent_runs

            await reconcile_stranded_subagent_runs(
                parent_chat,
                parent_live=False,
                live_rerun_entry_keys=[],
                user_id=user.id,
            )
        except Exception:
            log.exception("subagent rerun stop reconcile failed")

    return {"status": True, "task_ids": task_ids, "stopped": len(task_ids)}
