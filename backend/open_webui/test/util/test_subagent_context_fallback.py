"""Focused tests for the subagent long-context model handoff.

The handoff lives in the guarded inner-turn lifecycle shared by launch,
continue, and detached rerun. These tests keep it there: one failed pair is
replaced, the canonical hidden-chat model changes in the same transaction, and
non-context errors never select another model.
"""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from test.util.db import configure_test_database

configure_test_database()

from open_webui.utils import subagent as sub  # noqa: E402
from open_webui.models import chats as chats_model  # noqa: E402


def _request(fallback_model_id="long", *, include_fallback=True):
    models = {"research": {"id": "research"}}
    if include_fallback:
        models["long"] = {"id": "long"}
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(
                    SUBAGENT_CONTEXT_FALLBACK_MODEL=fallback_model_id
                ),
                MODELS=models,
            )
        )
    )


def test_context_fallback_resolver_rejects_missing_unavailable_and_same_model():
    assert (
        sub._resolve_subagent_context_fallback_model(
            _request(fallback_model_id=""), "research"
        )
        is None
    )
    assert (
        sub._resolve_subagent_context_fallback_model(
            _request(include_fallback=False), "research"
        )
        is None
    )
    assert (
        sub._resolve_subagent_context_fallback_model(
            _request(fallback_model_id="research"), "research"
        )
        is None
    )
    assert sub._resolve_subagent_context_fallback_model(_request(), "research") == {
        "id": "long"
    }


def test_guarded_runner_retries_failed_pair_on_fallback_and_persists_switch():
    calls = []
    events = []

    async def fake_inner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise sub.SubagentContextLimitError(
                "Your input exceeds the context window of this model."
            )
        return "recovered answer"

    async def emitter(event):
        events.append(event)

    async def go():
        with (
            patch.object(sub, "_run_inner_chat", side_effect=fake_inner),
            patch.object(sub, "SUBAGENT_RUN_TIMEOUT_SECONDS", 0),
            patch.object(sub, "_get_subagent_concurrency_sem", return_value=None),
        ):
            return await sub._run_inner_chat_guarded(
                request=_request(),
                subagent_model={"id": "research"},
                subagent_chat_id="hidden-1",
                user_msg_id="user-1",
                assistant_msg_id="assistant-1",
                subagent_meta={"subagent_id": "hidden-1"},
                parent_event_emitter=emitter,
                history_transition={"expected_current_id": None},
                history_prepared_callback=object(),
            )

    assert asyncio.run(go()) == "recovered answer"
    assert len(calls) == 2
    retry = calls[1]
    assert retry["subagent_model"] == {"id": "long"}
    assert retry["history_prepared_callback"] is None
    assert retry["history_transition"] == {
        "expected_current_id": "assistant-1",
        "revert_user_message_id": "user-1",
        "revert_assistant_message_id": "assistant-1",
        "expected_model_id": "research",
        "set_model_id": "long",
    }
    assert events == [
        {
            "type": "chat:subagent:start",
            "data": {
                "subagent_id": "hidden-1",
                "context_fallback": True,
                "fallback_from_model_id": "research",
                "model_id": "long",
            },
        }
    ]


def test_live_retry_exhaustion_shapes_enter_the_same_context_handoff():
    from open_webui.utils.middleware import _is_context_fallback_provider_error

    for observed_error in (
        {
            "content": (
                "The model returned no response after retrying 5 times. "
                "Please try again."
            ),
            "code": "empty_response_retries_exhausted",
            "retry_exhausted": True,
        },
        {
            "content": (
                "upstream connect error or disconnect/reset before headers. "
                "reset reason: connection termination"
            ),
            "code": "provider_connection_retries_exhausted",
            "retry_exhausted": True,
        },
    ):
        assert _is_context_fallback_provider_error(observed_error)


def test_guarded_runner_does_not_switch_for_output_token_limit():
    calls = 0
    events = []

    async def fake_inner(**kwargs):
        nonlocal calls
        calls += 1
        raise sub.SubagentNonRetryableError(
            "Model reached the completion token limit before producing final text"
        )

    async def emitter(event):
        events.append(event)

    async def go():
        with (
            patch.object(sub, "_run_inner_chat", side_effect=fake_inner),
            patch.object(sub, "SUBAGENT_RUN_TIMEOUT_SECONDS", 0),
            patch.object(sub, "_get_subagent_concurrency_sem", return_value=None),
        ):
            return await sub._run_inner_chat_guarded(
                request=_request(),
                subagent_model={"id": "research"},
                subagent_chat_id="hidden-1",
                user_msg_id="user-1",
                assistant_msg_id="assistant-1",
                subagent_meta={"subagent_id": "hidden-1"},
                parent_event_emitter=emitter,
            )

    try:
        asyncio.run(go())
        assert False, "expected SubagentNonRetryableError"
    except sub.SubagentNonRetryableError as error:
        assert not isinstance(error, sub.SubagentFallbackExhaustedError)
    assert calls == 1
    assert events == []


def test_failed_fallback_cannot_make_outer_retry_restore_original_model():
    calls = 0

    async def fake_inner(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sub.SubagentContextLimitError("context window exceeded")
        raise RuntimeError("temporary provider failure")

    async def go():
        with (
            patch.object(sub, "_run_inner_chat", side_effect=fake_inner),
            patch.object(sub, "SUBAGENT_RUN_TIMEOUT_SECONDS", 0),
            patch.object(sub, "_get_subagent_concurrency_sem", return_value=None),
        ):
            return await sub._run_inner_chat_guarded(
                request=_request(),
                subagent_model={"id": "research"},
                subagent_chat_id="hidden-1",
                user_msg_id="user-1",
                assistant_msg_id="assistant-1",
                subagent_meta={"subagent_id": "hidden-1"},
                parent_event_emitter=lambda event: asyncio.sleep(0),
            )

    try:
        asyncio.run(go())
        assert False, "expected SubagentFallbackExhaustedError"
    except sub.SubagentFallbackExhaustedError as error:
        assert "fallback model long failed" in str(error).lower()
    assert calls == 2


def test_history_preparation_forwards_atomic_model_compare_and_set():
    captured = {}

    async def fake_prepare(chat_id, user_message, assistant_message, **kwargs):
        captured.update(
            {
                "chat_id": chat_id,
                "assistant": assistant_message,
                "kwargs": kwargs,
            }
        )
        return {"current_id": assistant_message["id"]}

    async def go():
        with patch.object(
            sub.Chats, "prepare_subagent_turn_atomic", side_effect=fake_prepare
        ):
            await sub._append_history_for_inner_run(
                "hidden-1",
                "retry this request",
                "user-1",
                "assistant-1",
                "long",
                history_transition={
                    "expected_current_id": "assistant-1",
                    "revert_user_message_id": "user-1",
                    "revert_assistant_message_id": "assistant-1",
                    "expected_model_id": "research",
                    "set_model_id": "long",
                },
            )

    asyncio.run(go())
    assert captured["assistant"]["model"] == "long"
    assert captured["kwargs"] == {
        "expected_current_id": "assistant-1",
        "reset_history": False,
        "revert_user_message_id": "user-1",
        "revert_assistant_message_id": "assistant-1",
        "expected_model_id": "research",
        "set_model_id": "long",
    }


def test_atomic_turn_replacement_commits_transcript_and_canonical_model_together():
    chat_row = SimpleNamespace(
        chat={
            "models": ["research"],
            "history": {
                "currentId": "assistant-1",
                "messages": {
                    "user-1": {
                        "id": "user-1",
                        "parentId": None,
                        "childrenIds": ["assistant-1"],
                        "role": "user",
                        "content": "retry this request",
                    },
                    "assistant-1": {
                        "id": "assistant-1",
                        "parentId": "user-1",
                        "childrenIds": [],
                        "role": "assistant",
                        "content": "",
                        "model": "research",
                    },
                },
            },
        },
        model_id_primary="research",
        updated_at=0,
    )

    class FakeQuery:
        def filter(self, *args):
            return self

        def with_for_update(self):
            return self

        def first(self):
            return chat_row

    class FakeDB:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0

        def query(self, *args):
            return FakeQuery()

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    db = FakeDB()

    @contextmanager
    def fake_get_db():
        yield db

    with (
        patch.object(chats_model, "get_db", fake_get_db),
        patch.object(chats_model, "_chat_message_table_supported", return_value=False),
        patch.object(chats_model, "_invalidate_cached_sibling_stubs"),
    ):
        result = sub.Chats._impl.prepare_subagent_turn_atomic(
            "hidden-1",
            {
                "id": "user-1",
                "role": "user",
                "content": "retry this request",
            },
            {
                "id": "assistant-1",
                "role": "assistant",
                "content": "",
                "model": "long",
            },
            expected_current_id="assistant-1",
            revert_user_message_id="user-1",
            revert_assistant_message_id="assistant-1",
            expected_model_id="research",
            set_model_id="long",
        )

    assert result["model_id"] == "long"
    assert db.commits == 1
    assert db.rollbacks == 0
    assert chat_row.chat["models"] == ["long"]
    assert chat_row.model_id_primary == "long"
    assert chat_row.chat["history"]["messages"]["assistant-1"]["model"] == "long"
