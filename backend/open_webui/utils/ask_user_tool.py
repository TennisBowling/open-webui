"""Built-in ``ask_user`` tool: pause the generation to ask the user a question.

The model calls ``ask_user`` mid-task when it hits a genuine decision point. The
tool renders an inline question card in the chat (the tool CALL carries the
questions; the frontend `AskUserBlock` renders it from the call arguments) and
then BLOCKS the running generation until the user submits an answer. The user's
answer comes back as the tool RESULT — so the committed Q&A lives in the
assistant message's content_blocks and replays to the model on the next round
like any other tool result. No new content-block type is needed.

Durability is the whole point. The wait is not a socket round-trip (that dies on
reload and times out in seconds). Instead the callable polls a durable field on
the chat blob — ``question_states[tool_call_id]`` — that the frontend writes via
an atomic patch op. Because the generation runs as a detached server task, it
keeps waiting across tab reloads, tab closes, and zero open tabs; any tab can
submit the answer and the poll picks it up. An in-process event provides an
immediate same-worker wakeup; the poll is the durable backstop.

For temp / ``local:`` chats there is no server-side DB blob to poll, so we fall
back to the legacy socket-ack prompt (``__event_call__``) — acceptable there
since those chats already lack durable queueing and only live in one tab.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, List, Optional

from fastapi import Request
from pydantic import BaseModel, Field

from open_webui.env import (
    ASK_USER_POLL_INTERVAL_SECONDS,
    ASK_USER_TIMEOUT_SECONDS,
    SRC_LOG_LEVELS,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


class AskUserOption(BaseModel):
    label: str = Field(
        description="The short display text for this choice (1-5 words)."
    )
    description: Optional[str] = Field(
        default=None,
        description="Optional one-line explanation of what this choice means or implies.",
    )


class AskUserQuestion(BaseModel):
    question: str = Field(
        description="The full question text. Clear and specific; end with a question mark."
    )
    header: Optional[str] = Field(
        default=None,
        description="Very short label for the question (max ~12 chars), e.g. 'Auth method'.",
    )
    options: Optional[List[AskUserOption]] = Field(
        default=None,
        description=(
            "2-4 distinct choices. Omit (or pass an empty list) to ask a pure "
            "free-form question with just a text box."
        ),
    )
    multiSelect: Optional[bool] = Field(
        default=False,
        description="True to let the user select more than one option.",
    )
    allowOther: Optional[bool] = Field(
        default=True,
        description="True to offer an 'Other' free-text escape hatch alongside the options.",
    )


def _coerce_questions(questions: Any) -> List[dict]:
    """Normalize the model's ``questions`` argument into a list of plain dicts.

    The native function-calling layer may hand us pydantic models, dicts, or a
    JSON string (some providers stringify nested arrays). Be permissive: the
    card and the answer-formatting both tolerate missing optional fields."""
    if isinstance(questions, str):
        try:
            questions = json.loads(questions)
        except Exception:
            return []
    if isinstance(questions, dict):
        questions = [questions]
    if not isinstance(questions, list):
        return []

    out: List[dict] = []
    for q in questions:
        if isinstance(q, BaseModel):
            q = q.model_dump()
        if not isinstance(q, dict):
            continue
        text = str(q.get("question") or "").strip()
        if not text:
            continue
        raw_options = q.get("options")
        options: List[dict] = []
        if isinstance(raw_options, list):
            for opt in raw_options:
                if isinstance(opt, BaseModel):
                    opt = opt.model_dump()
                if isinstance(opt, str):
                    opt = {"label": opt}
                if isinstance(opt, dict) and str(opt.get("label") or "").strip():
                    options.append(
                        {
                            "label": str(opt["label"]).strip(),
                            "description": (
                                str(opt.get("description")).strip()
                                if opt.get("description")
                                else ""
                            ),
                        }
                    )
        out.append(
            {
                "question": text,
                "header": str(q.get("header") or "").strip(),
                "options": options,
                "multiSelect": bool(q.get("multiSelect")),
                # Free-form questions (no options) always allow text; for option
                # questions, default the "Other" escape hatch to on.
                "allowOther": bool(q.get("allowOther", True)) or not options,
            }
        )
    return out


def _format_answer(questions: List[dict], answer: dict) -> str:
    """Render the user's submitted answer into the plain string the model reads
    as the tool result. ``answer`` maps question-index (as str) → {selected, other}."""
    lines = ["The user answered:"]
    for idx, q in enumerate(questions):
        entry = answer.get(str(idx)) or answer.get(idx) or {}
        selected = entry.get("selected") if isinstance(entry, dict) else None
        other = (entry.get("other") if isinstance(entry, dict) else "") or ""
        picks: List[str] = []
        if isinstance(selected, list):
            picks.extend(str(s) for s in selected if str(s).strip())
        elif isinstance(selected, str) and selected.strip():
            picks.append(selected.strip())
        if other.strip():
            picks.append(f"Other: {other.strip()}")
        answer_text = "; ".join(picks) if picks else "(no answer)"
        lines.append(f"Q: {q['question']}")
        lines.append(f"A: {answer_text}")
    return "\n".join(lines)


class AskUserTools:
    """Builtin tool collection for the inline ask-the-user-a-question card."""

    class Valves(BaseModel):
        """Placeholder; no per-instance configuration."""

        pass

    def __init__(self):
        self.valves = self.Valves()

    async def ask_user(
        self,
        questions: List[AskUserQuestion],
        __request__: Optional[Request] = None,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__: Optional[Callable] = None,
        __event_call__: Optional[Callable] = None,
        __model__: Optional[dict] = None,
    ) -> str:
        """Ask the user one or more questions and PAUSE until they answer. Use only at genuine decision points where their input changes what you do next; never to narrate progress or for things you can decide yourself.

        :param questions: 1-4 questions, in order. Put a recommended option first if any.
        :return: The user's answer(s) as text, or a notice if they skipped or didn't answer in time.
        """
        normalized = _coerce_questions(questions)
        if not normalized:
            return (
                "[ask_user produced no valid questions — provide a non-empty "
                "'questions' list, each with a 'question' string.]"
            )

        metadata = __metadata__ or {}
        chat_id = metadata.get("chat_id")
        tool_call_id = metadata.get("tool_call_id") or ""
        if not tool_call_id:
            # The loop pins the current tool_call_id in a ContextVar before
            # awaiting the callable; metadata may not carry it, so fall back.
            try:
                from open_webui.utils.middleware import current_tool_call_id_var

                tool_call_id = current_tool_call_id_var.get() or ""
            except Exception:
                tool_call_id = ""

        # Temp/local chats (and any case with no durable blob) can't be polled —
        # degrade to the legacy socket-ack prompt so the feature still works in a
        # single live tab. Never blocks beyond the socket timeout.
        is_local = (
            not chat_id
            or not isinstance(chat_id, str)
            or chat_id.startswith("local:")
            or not tool_call_id
        )
        if is_local:
            return await _ask_via_socket(normalized, __event_call__)

        return await _ask_via_durable_poll(chat_id, tool_call_id, normalized)


async def _ask_via_durable_poll(
    chat_id: str, tool_call_id: str, questions: List[dict]
) -> str:
    """Block polling the chat blob for a submitted answer/skip. The question
    card itself is already persisted (the tool_calls block was checkpointed
    before this callable ran), so a reload re-renders it and any tab can
    answer. CancelledError (user Stop) propagates untouched."""
    from open_webui.models.chats import Chats
    from open_webui.utils import ask_user_registry

    # Register the wakeup event BEFORE the first poll so a near-instant submit
    # can't slip between a check and the wait.
    event = ask_user_registry.get_or_create_event(chat_id, tool_call_id)
    poll = max(1, int(ASK_USER_POLL_INTERVAL_SECONDS or 2))
    timeout = int(ASK_USER_TIMEOUT_SECONDS or 0)
    waited = 0.0

    try:
        while True:
            result = await Chats.get_question_answer_by_id(chat_id, tool_call_id)
            if result is not None:
                if result.get("skipped"):
                    return "[The user skipped the question without answering. Proceed with your best judgment and state any assumption you made.]"
                return _format_answer(questions, result.get("answer") or {})

            if timeout > 0 and waited >= timeout:
                return "[The user did not answer in time. Proceed with your best judgment and state any assumption you made.]"

            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=poll)
            except asyncio.TimeoutError:
                pass
            waited += poll
    finally:
        ask_user_registry.discard(chat_id, tool_call_id)


async def _ask_via_socket(
    questions: List[dict], event_call: Optional[Callable]
) -> str:
    """Fallback for temp/local chats: a single socket round-trip. Bounded by the
    socket ack timeout; returns a skip notice if unanswered/unreachable."""
    if event_call is None:
        return "[ask_user is not available in this context. Proceed with your best judgment.]"
    try:
        resp = await event_call(
            {
                "type": "ask_user:prompt",
                "data": {"questions": questions},
            }
        )
    except Exception:
        log.exception("ask_user socket prompt failed")
        return "[The question could not be delivered. Proceed with your best judgment.]"

    if not isinstance(resp, dict):
        return "[The user did not answer. Proceed with your best judgment.]"
    if resp.get("skipped"):
        return "[The user skipped the question without answering. Proceed with your best judgment and state any assumption you made.]"
    answer = resp.get("answer")
    if isinstance(answer, dict):
        return _format_answer(questions, answer)
    return "[The user did not answer. Proceed with your best judgment.]"


_ask_user_tools_instance: Optional[AskUserTools] = None


def get_ask_user_tools_instance() -> AskUserTools:
    """Singleton accessor. Mirrors ``get_view_image_tools_instance``."""
    global _ask_user_tools_instance
    if _ask_user_tools_instance is None:
        _ask_user_tools_instance = AskUserTools()
    return _ask_user_tools_instance
