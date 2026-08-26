"""Regression tests for the empty-usage guard in token tracking.

Some providers (notably the bare-id "C" gemini provider) emit a fully
zero-filled ``usage`` object on MANY intermediate streaming chunks, e.g.::

    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
     "prompt_tokens_details": {"cached_tokens": 0},
     "completion_tokens_details": {"reasoning_tokens": 0}}

That payload is syntactically non-empty (a bare ``if usage:`` is truthy) but
carries no countable tokens. Before the fix, ``process_token_usage`` recorded
these as all-zero ``token_usage_event`` rows AND overwrote
``conversation_token_usage.last_input_tokens`` to 0 — which is what the in-chat
pill renders as "Latest Input". The symptom: the input counter reads 0 after an
agentic / tool-call turn even though every real round had a large prompt.

``usage_has_data`` is the single source of truth for "does this usage payload
carry information"; these tests pin both the pure predicate and the end-to-end
guarantee that a trailing empty payload does not clobber the per-chat counter.

The DB-touching tests need a live Postgres (the runtime is Postgres-only and
importing ``open_webui.config`` runs ``alembic upgrade head``). They self-skip
if no usable ``DATABASE_URL`` is reachable, so the pure ``usage_has_data`` cases
still run wherever the package imports.
"""

import asyncio
import os
import uuid

import pytest

# Importing socket.main transitively imports config, which runs migrations
# against DATABASE_URL. If that's not reachable, skip the whole module rather
# than erroring at collection.
try:
    from open_webui.socket.main import usage_has_data, process_token_usage
    from open_webui.internal.db import get_db
    from sqlalchemy import text as sql_text

    _IMPORT_OK = True
    _IMPORT_ERR = None
except Exception as e:  # pragma: no cover - environment-dependent
    _IMPORT_OK = False
    _IMPORT_ERR = e

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK, reason=f"open_webui import/DB unavailable: {_IMPORT_ERR}"
)


# The exact zero-filled payload observed in production for gemini-3.1-pro-preview.
GEMINI_EMPTY = {
    "total_tokens": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "prompt_tokens_details": {"cached_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 0},
}

# The BYOK variant that additionally carried a zero cost.
GEMINI_EMPTY_BYOK = {
    "cost": 0,
    "is_byok": True,
    "cost_details": {
        "upstream_inference_cost": 0,
        "upstream_inference_prompt_cost": 0,
        "upstream_inference_completions_cost": 0,
    },
    "total_tokens": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 0},
}

REAL_USAGE = {
    "prompt_tokens": 34094,
    "completion_tokens": 39,
    "total_tokens": 34133,
    "prompt_tokens_details": {"cached_tokens": 12000},
}


# --------------------------------------------------------------------------- #
# Pure predicate
# --------------------------------------------------------------------------- #


def test_usage_has_data_rejects_empty():
    assert usage_has_data(None) is False
    assert usage_has_data({}) is False
    assert usage_has_data(GEMINI_EMPTY) is False
    assert usage_has_data(GEMINI_EMPTY_BYOK) is False
    # Nested-only zeros are still empty.
    assert usage_has_data({"prompt_tokens_details": {"cached_tokens": 0}}) is False


def test_usage_has_data_accepts_real():
    assert usage_has_data(REAL_USAGE) is True
    assert usage_has_data({"prompt_tokens": 1}) is True
    assert usage_has_data({"completion_tokens": 1}) is True
    assert usage_has_data({"total_tokens": 5}) is True
    # Cached-only (cache-hit turn) is meaningful.
    assert usage_has_data({"prompt_tokens_details": {"cached_tokens": 7}}) is True
    # Cost-bearing but token-less rows are preserved for cost accounting.
    assert usage_has_data({"prompt_tokens": 0, "cost": 0.002}) is True
    assert (
        usage_has_data({"cost_details": {"upstream_inference_cost": 0.01}}) is True
    )
    # A reasoning-only step (visible completion 0 but real reasoning work) is kept.
    assert (
        usage_has_data(
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "completion_tokens_details": {"reasoning_tokens": 338},
            }
        )
        is True
    )


# --------------------------------------------------------------------------- #
# End-to-end DB guarantees
# --------------------------------------------------------------------------- #


async def _events_for(chat_id):
    async with get_db() as db:
        rows = (
            await db.execute(
                sql_text(
                    "SELECT prompt_tokens, completion_tokens, total_tokens "
                    "FROM token_usage_event WHERE attributed_chat_id = :c"
                ),
                {"c": chat_id},
            )
        ).all()
    return rows


async def _conv_for(chat_id):
    async with get_db() as db:
        row = (
            await db.execute(
                sql_text(
                    "SELECT total_input_tokens, total_output_tokens, "
                    "last_input_tokens, last_output_tokens, message_count "
                    "FROM conversation_token_usage WHERE chat_id = :c"
                ),
                {"c": chat_id},
            )
        ).mappings().first()
    return row


def _run(coro):
    # asyncpg pins its pool to the first event loop; dispose between runs so a
    # fresh asyncio.run() doesn't trip "Future attached to a different loop".
    from open_webui.internal.db import engine

    async def _wrapped():
        try:
            return await coro
        finally:
            await engine.dispose(close=False)

    return asyncio.run(_wrapped())


def test_empty_payload_records_nothing():
    """An all-zero usage payload must not create an event or a conversation row."""
    chat_id = f"test-empty-{uuid.uuid4()}"
    user_id = f"test-user-{uuid.uuid4()}"

    _run(
        process_token_usage(
            "gemini-3.1-pro-preview",
            GEMINI_EMPTY,
            chat_id=chat_id,
            user_id=user_id,
            message_id=str(uuid.uuid4()),
        )
    )

    assert _run(_events_for(chat_id)) == []
    assert _run(_conv_for(chat_id)) is None


def test_trailing_empty_does_not_clobber_last_input():
    """The core regression: a real round followed by empty chunks must leave
    ``last_input_tokens`` at the real value, not 0."""
    chat_id = f"test-clobber-{uuid.uuid4()}"
    user_id = f"test-user-{uuid.uuid4()}"

    # One real round, then several trailing zero chunks (the gemini pattern).
    _run(
        process_token_usage(
            "gemini-3.1-pro-preview",
            REAL_USAGE,
            chat_id=chat_id,
            user_id=user_id,
            message_id=str(uuid.uuid4()),
        )
    )
    for _ in range(3):
        _run(
            process_token_usage(
                "gemini-3.1-pro-preview",
                dict(GEMINI_EMPTY),
                chat_id=chat_id,
                user_id=user_id,
                message_id=str(uuid.uuid4()),
            )
        )

    conv = _run(_conv_for(chat_id))
    assert conv is not None
    assert conv["last_input_tokens"] == REAL_USAGE["prompt_tokens"]
    assert conv["last_output_tokens"] == REAL_USAGE["completion_tokens"]
    # Exactly one real event, message_count not inflated by the zero chunks.
    assert len(_run(_events_for(chat_id))) == 1
    assert conv["message_count"] == 1
    assert conv["total_input_tokens"] == REAL_USAGE["prompt_tokens"]


def test_real_payload_records_event_and_conversation():
    chat_id = f"test-real-{uuid.uuid4()}"
    user_id = f"test-user-{uuid.uuid4()}"

    _run(
        process_token_usage(
            "gemini-3.1-pro-preview",
            REAL_USAGE,
            chat_id=chat_id,
            user_id=user_id,
            message_id=str(uuid.uuid4()),
        )
    )

    events = _run(_events_for(chat_id))
    assert len(events) == 1
    assert events[0].prompt_tokens == REAL_USAGE["prompt_tokens"]

    conv = _run(_conv_for(chat_id))
    assert conv is not None
    assert conv["last_input_tokens"] == REAL_USAGE["prompt_tokens"]


def test_reasoning_only_event_does_not_zero_last_input():
    """A reasoning/cost-only event (prompt_tokens=0) passes usage_has_data (real
    work) but must NOT zero the pill's Latest Input — the prior real value is kept,
    while totals still accumulate."""
    chat_id = f"test-reasoning-{uuid.uuid4()}"
    user_id = f"test-user-{uuid.uuid4()}"

    _run(
        process_token_usage(
            "google/gemini-3.1-pro-preview",
            REAL_USAGE,
            chat_id=chat_id,
            user_id=user_id,
            message_id=str(uuid.uuid4()),
        )
    )
    # Reasoning-only trailing event: visible tokens 0, reasoning > 0.
    _run(
        process_token_usage(
            "google/gemini-3.1-pro-preview",
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "completion_tokens_details": {"reasoning_tokens": 338},
            },
            chat_id=chat_id,
            user_id=user_id,
            message_id=str(uuid.uuid4()),
        )
    )

    conv = _run(_conv_for(chat_id))
    assert conv is not None
    # Latest Input preserved from the real event, not zeroed by the reasoning chunk.
    assert conv["last_input_tokens"] == REAL_USAGE["prompt_tokens"]
    # The reasoning event still recorded (it carries real work) -> 2 events.
    assert len(_run(_events_for(chat_id))) == 2


def test_subagent_event_does_not_advance_parent_last_input():
    """A subagent run rolls its tokens into the parent chat's TOTALS but must not
    set the parent pill's Latest Input — that stays the parent's own last turn."""
    parent_chat = f"test-parent-{uuid.uuid4()}"
    subagent_chat = f"test-subagent-{uuid.uuid4()}"
    user_id = f"test-user-{uuid.uuid4()}"

    # Parent's own visible turn.
    _run(
        process_token_usage(
            "gpt-5.5",
            {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050},
            chat_id=parent_chat,
            user_id=user_id,
            source_chat_id=parent_chat,  # own turn: source == attributed
            message_id=str(uuid.uuid4()),
        )
    )
    # A subagent finishing AFTER the parent's last turn, attributed to the parent.
    _run(
        process_token_usage(
            "gpt-5.5",
            {"prompt_tokens": 99999, "completion_tokens": 7, "total_tokens": 100006},
            chat_id=parent_chat,
            user_id=user_id,
            source_chat_id=subagent_chat,  # subagent: source != attributed
            message_id=str(uuid.uuid4()),
            source_type="subagent",
        )
    )

    conv = _run(_conv_for(parent_chat))
    assert conv is not None
    # Latest Input is the parent's own last turn, NOT the subagent's 99999.
    assert conv["last_input_tokens"] == 1000
    assert conv["last_output_tokens"] == 50
    # Totals DO include the subagent usage.
    assert conv["total_input_tokens"] == 1000 + 99999
    assert conv["total_output_tokens"] == 50 + 7


async def _insert_event(user_id, chat_id, model_id, pt, ct, tt, ts):
    async with get_db() as db:
        await db.execute(
            sql_text(
                "INSERT INTO token_usage_event "
                "(id,user_id,source_chat_id,attributed_chat_id,model_id,"
                " prompt_tokens,completion_tokens,total_tokens,cache_read_tokens,"
                " request_count,source_type,raw_usage,created_at) "
                "VALUES (:i,:u,:c,:c,:m,:p,:co,:t,0,1,'chat','{}',:ts)"
            ),
            {
                "i": str(uuid.uuid4()), "u": user_id, "c": chat_id, "m": model_id,
                "p": pt, "co": ct, "t": tt, "ts": ts,
            },
        )
        await db.commit()


def test_count_queries_exclude_junk_events():
    """Analytics request/message counts must not count all-zero junk events.

    Historical junk rows still live in token_usage_event (we don't delete them),
    so the read-path aggregations carry a non-zero exclusion predicate. One real
    event + two junk events for a model must report message_count == 1.
    """
    from open_webui.models.analytics import Analytics

    user_id = f"tu-{uuid.uuid4()}"
    chat_id = f"tc-{uuid.uuid4()}"
    model_id = "gemini-3.1-pro-preview"
    ts = 1788000000  # within 2026
    year = 2026

    async def _seed():
        await _insert_event(user_id, chat_id, model_id, 500, 10, 510, ts)
        await _insert_event(user_id, chat_id, model_id, 0, 0, 0, ts + 1)
        await _insert_event(user_id, chat_id, model_id, 0, 0, 0, ts + 2)

    _run(_seed())
    rows = _run(Analytics.get_model_usage_by_user(user_id, year))
    assert len(rows) == 1
    assert rows[0].model_id == model_id
    # Only the real request is counted, not the two junk frames.
    assert rows[0].message_count == 1
    assert rows[0].total_tokens == 510

