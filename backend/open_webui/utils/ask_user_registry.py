"""In-process wakeup registry for the built-in ``ask_user`` tool.

When the model calls ``ask_user`` the generation blocks inside the tool callable
waiting for the user to submit an answer. The DURABLE delivery channel is the
chat blob (``question_states``), polled every few seconds — that's what makes an
answer survive a reload / zero open tabs / a different tab. This registry is a
best-effort FAST PATH on top of that poll: when the answer arrives on the same
worker (the common case), the patch handler ``signal()``s an asyncio.Event so
the blocked callable wakes immediately instead of waiting for the next poll tick.

Single-worker deployment assumption (see the concurrency-scaling notes): all
coroutines share one event loop, so a plain process-local dict of Events is
sufficient and correct. The waiter creates its Event before it starts waiting;
the signaller only sets an Event that already exists. If the two ever land on
different workers (latent multi-worker), the signal is simply missed and the
poll backstop still delivers the answer — never a correctness bug, only latency.
"""

from __future__ import annotations

import asyncio
from typing import Dict

_events: Dict[str, asyncio.Event] = {}


def _key(chat_id: str, tool_call_id: str) -> str:
    # NUL separator can't appear in ids, so this is unambiguous.
    return f"{chat_id}\x00{tool_call_id}"


def get_or_create_event(chat_id: str, tool_call_id: str) -> asyncio.Event:
    """Called by the waiter (the ask_user callable) on its own event loop,
    BEFORE it begins waiting, so a later ``signal`` has something to set."""
    key = _key(chat_id, tool_call_id)
    event = _events.get(key)
    if event is None:
        event = asyncio.Event()
        _events[key] = event
    return event


def signal(chat_id: str, tool_call_id: str) -> None:
    """Called by the patch handler when an answer/skip is written. Wakes the
    blocked waiter if it's registered on this worker; no-op otherwise."""
    event = _events.get(_key(chat_id, tool_call_id))
    if event is not None:
        event.set()


def discard(chat_id: str, tool_call_id: str) -> None:
    """Drop the Event once the waiter is done, so the registry doesn't grow
    without bound. Best-effort."""
    _events.pop(_key(chat_id, tool_call_id), None)
