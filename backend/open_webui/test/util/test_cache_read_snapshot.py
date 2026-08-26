"""Regression tests for the per-chat "last request" cache-read snapshot.

The in-chat token pill renders ``conversation_token_usage.last_cache_read_tokens``
as its "R" segment. That value must reflect the cached-input read of the chat's
MOST RECENT own-turn request — including 0 when that request was a cold-cache
miss (``prompt_tokens_details.cached_tokens == 0``).

Before the fix, ``update_conversation_token_usage`` advanced the snapshot one
dimension at a time, each on its own ``> 0`` guard::

    if cache_read_tokens > 0:
        conflict_set["last_cache_read_tokens"] = cache_read_tokens

so a cold-cache request never cleared a prior warm-cache hit. The symptom seen in
production: a chat whose latest request reported ``cached_tokens == 0`` still
showed "R 243.8k" (a stale value from an earlier request) while "Latest Input"
correctly reflected the new request — an internally inconsistent snapshot that
mixed two different requests.

The fix snapshots all three ``last_*`` values atomically off a single
own-turn-prompt gate (``is_own_turn and token_in > 0``), which is safe because a
real own-turn call always reports a non-zero prompt and carries its completion
and cache-read in the SAME usage object. These tests pin that the snapshot always
reflects exactly one coherent request.

DB-touching; self-skips when no Postgres is reachable (see the sibling
``test_token_usage_empty_guard.py`` for the rationale).
"""

import asyncio
import uuid

import pytest

try:
    from open_webui.socket.main import process_token_usage
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


def _run(coro):
    from open_webui.internal.db import engine

    async def _wrapped():
        try:
            return await coro
        finally:
            await engine.dispose(close=False)

    return asyncio.run(_wrapped())


async def _conv_for(chat_id):
    async with get_db() as db:
        row = (
            await db.execute(
                sql_text(
                    "SELECT total_input_tokens, total_output_tokens, total_tokens, "
                    "total_cache_read_tokens, last_input_tokens, last_output_tokens, "
                    "last_cache_read_tokens, message_count "
                    "FROM conversation_token_usage WHERE chat_id = :c"
                ),
                {"c": chat_id},
            )
        ).mappings().first()
    return row


def _usage(prompt, completion, cached=0):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


async def _emit(chat_id, user_id, usage, **kw):
    await process_token_usage(
        "gemini-3.1-pro-preview",
        usage,
        chat_id=chat_id,
        user_id=user_id,
        message_id=str(uuid.uuid4()),
        **kw,
    )


# --------------------------------------------------------------------------- #


def test_cold_request_clears_stale_warm_cache():
    """The core regression: a warm-cache request followed by a cold-cache request
    must leave last_cache_read_tokens at 0, not pinned to the earlier hit."""
    chat_id = f"test-coldcache-{uuid.uuid4()}"
    user_id = f"test-user-{uuid.uuid4()}"

    # Round 1: a big cache hit (273004 prompt, 243797 cached — the prod shape).
    _run(_emit(chat_id, user_id, _usage(273004, 708, cached=243797)))
    conv = _run(_conv_for(chat_id))
    assert conv["last_cache_read_tokens"] == 243797

    # Round 2: a cold-cache request (cache expired) — prompt present, cached == 0.
    _run(_emit(chat_id, user_id, _usage(248112, 432, cached=0)))

    conv = _run(_conv_for(chat_id))
    # The whole point: R reflects the LATEST request, which read 0 from cache.
    assert conv["last_cache_read_tokens"] == 0
    # ... and the snapshot stays internally consistent (same request as input).
    assert conv["last_input_tokens"] == 248112
    assert conv["last_output_tokens"] == 432
    # Totals still accumulate both rounds' cache reads.
    assert conv["total_cache_read_tokens"] == 243797
    assert conv["total_input_tokens"] == 273004 + 248112
    assert conv["message_count"] == 2


def test_warm_then_warmer_updates_to_latest():
    """Two warm-cache requests: the snapshot tracks the most recent one."""
    chat_id = f"test-warmwarm-{uuid.uuid4()}"
    user_id = f"test-user-{uuid.uuid4()}"

    _run(_emit(chat_id, user_id, _usage(1000, 10, cached=400)))
    _run(_emit(chat_id, user_id, _usage(2000, 20, cached=1500)))

    conv = _run(_conv_for(chat_id))
    assert conv["last_cache_read_tokens"] == 1500
    assert conv["last_input_tokens"] == 2000
    assert conv["total_cache_read_tokens"] == 400 + 1500


def test_reasoning_only_chunk_preserves_cache_snapshot():
    """A reasoning/cost-only chunk (prompt_tokens=0) passes usage_has_data but must
    NOT clear the cache snapshot — the prior real request's R is kept."""
    chat_id = f"test-reasoning-cache-{uuid.uuid4()}"
    user_id = f"test-user-{uuid.uuid4()}"

    _run(_emit(chat_id, user_id, _usage(5000, 100, cached=3200)))
    # Reasoning-only trailing event: visible tokens 0, reasoning > 0, no prompt.
    _run(
        _emit(
            chat_id,
            user_id,
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "completion_tokens_details": {"reasoning_tokens": 512},
            },
        )
    )

    conv = _run(_conv_for(chat_id))
    # Cache snapshot preserved from the real request, not zeroed by the chunk.
    assert conv["last_cache_read_tokens"] == 3200
    assert conv["last_input_tokens"] == 5000


def test_subagent_cold_request_does_not_clear_parent_cache():
    """A subagent run rolls tokens into the parent TOTALS but must not advance the
    parent's snapshot — even a cold-cache subagent call must not zero the parent
    pill's R, which belongs to the parent's own last turn."""
    parent_chat = f"test-parent-cache-{uuid.uuid4()}"
    subagent_chat = f"test-subagent-cache-{uuid.uuid4()}"
    user_id = f"test-user-{uuid.uuid4()}"

    # Parent's own warm-cache turn.
    _run(
        _emit(parent_chat, user_id, _usage(8000, 200, cached=6000), source_chat_id=parent_chat)
    )
    # Subagent cold-cache call attributed to the parent, finishing afterward.
    _run(
        _emit(
            parent_chat,
            user_id,
            _usage(120000, 9, cached=0),
            source_chat_id=subagent_chat,
            source_type="subagent",
        )
    )

    conv = _run(_conv_for(parent_chat))
    # Snapshot stays the parent's own last turn; the subagent's cold 0 is ignored.
    assert conv["last_cache_read_tokens"] == 6000
    assert conv["last_input_tokens"] == 8000
    # Totals DO include the subagent usage.
    assert conv["total_cache_read_tokens"] == 6000
    assert conv["total_input_tokens"] == 8000 + 120000


def test_first_event_cold_cache_seeds_zero():
    """A chat whose very first own-turn request is a cold-cache miss seeds the
    snapshot at 0 (not left null/garbage), with input/output set."""
    chat_id = f"test-firstcold-{uuid.uuid4()}"
    user_id = f"test-user-{uuid.uuid4()}"

    _run(_emit(chat_id, user_id, _usage(700, 30, cached=0)))

    conv = _run(_conv_for(chat_id))
    assert conv["last_cache_read_tokens"] == 0
    assert conv["last_input_tokens"] == 700
    assert conv["last_output_tokens"] == 30
