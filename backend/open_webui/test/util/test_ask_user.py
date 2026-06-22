"""Unit tests for the built-in ``ask_user`` tool.

Two layers are covered:

* The model-layer ``question_states`` helpers on ``Chats`` (atomic draft/answer
  round-trip, skip signal, isolation between tool calls) — exercised against the
  migrated Postgres dev DB, like ``test_chat_queue_drain.py``.
* The pure tool logic in ``utils/ask_user_tool`` (question coercion, answer
  formatting, and the blocking poll returning on answer / skip / timeout and
  propagating CancelledError) — exercised with no DB via monkeypatched lookups.

Run with ``python3`` so the async model methods are driven via ``asyncio.run``
(no pytest-asyncio in this environment — see the backend test-env notes).
"""

import asyncio
import uuid

import pytest

from test.util.db import configure_test_database

configure_test_database(required=True)

from open_webui.models.chats import Chats, ChatForm  # noqa: E402


class _SyncChats:
    def __init__(self, target):
        self._target = target

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def _call(*args, **kwargs):
            return asyncio.run(attr(*args, **kwargs))

        return _call


SyncChats = _SyncChats(Chats)


@pytest.fixture()
def chat_id():
    chat = SyncChats.insert_new_chat(
        f"user-{uuid.uuid4()}",
        ChatForm(
            chat={
                "title": "ask_user test",
                "history": {"currentId": "m0", "messages": {}},
            }
        ),
    )
    assert chat is not None
    return chat.id


# --- model-layer question_states helpers -------------------------------------


def test_no_answer_before_submit(chat_id):
    assert SyncChats.get_question_answer_by_id(chat_id, "tc1") is None


def test_draft_then_answer_roundtrip(chat_id):
    # A partial draft must NOT register as a terminal answer.
    SyncChats.set_question_state_by_id(
        chat_id, "tc1", {"draft": {"0": {"selected": ["A"], "other": ""}}}
    )
    assert SyncChats.get_question_answer_by_id(chat_id, "tc1") is None
    entry = SyncChats.get_question_state_by_id(chat_id, "tc1")
    assert entry["draft"]["0"]["selected"] == ["A"]

    # Submitting the answer is the terminal signal.
    SyncChats.set_question_state_by_id(
        chat_id,
        "tc1",
        {"answer": {"0": {"selected": ["B"], "other": ""}}, "submitted_at": 123},
    )
    result = SyncChats.get_question_answer_by_id(chat_id, "tc1")
    assert result is not None
    assert result["answer"]["0"]["selected"] == ["B"]
    # The draft is preserved alongside the answer (merge, not clobber).
    entry = SyncChats.get_question_state_by_id(chat_id, "tc1")
    assert entry["draft"]["0"]["selected"] == ["A"]
    assert entry["answer"]["0"]["selected"] == ["B"]


def test_skip_signal(chat_id):
    SyncChats.set_question_state_by_id(chat_id, "tc9", {"skipped": True})
    result = SyncChats.get_question_answer_by_id(chat_id, "tc9")
    assert result == {"skipped": True}


def test_tool_calls_isolated(chat_id):
    # Two question cards in the same chat must not see each other's answers.
    SyncChats.set_question_state_by_id(
        chat_id, "tcA", {"answer": {"0": {"selected": ["yes"]}}}
    )
    assert SyncChats.get_question_answer_by_id(chat_id, "tcB") is None
    assert SyncChats.get_question_answer_by_id(chat_id, "tcA")["answer"]["0"][
        "selected"
    ] == ["yes"]


def test_missing_chat_is_none():
    assert SyncChats.get_question_answer_by_id("does-not-exist", "tc1") is None


# --- pure tool logic ---------------------------------------------------------


def test_coerce_questions_shapes():
    from open_webui.utils.ask_user_tool import _coerce_questions

    qs = _coerce_questions(
        [
            {
                "question": "Pick",
                "header": "H",
                "options": [{"label": "A", "description": "aa"}, "B"],
                "multiSelect": True,
            },
            {"question": "Free?"},  # no options -> free-form
            {"no_question_key": "dropped"},  # invalid -> dropped
        ]
    )
    assert len(qs) == 2
    assert qs[0]["options"][0] == {"label": "A", "description": "aa"}
    assert qs[0]["options"][1] == {"label": "B", "description": ""}
    assert qs[0]["multiSelect"] is True
    # Free-form question always allows text even though allowOther wasn't given.
    assert qs[1]["options"] == []
    assert qs[1]["allowOther"] is True


def test_coerce_questions_json_string():
    from open_webui.utils.ask_user_tool import _coerce_questions

    qs = _coerce_questions('[{"question": "Q?"}]')
    assert len(qs) == 1 and qs[0]["question"] == "Q?"


def test_format_answer():
    from open_webui.utils.ask_user_tool import _format_answer

    qs = [
        {"question": "Color?"},
        {"question": "Notes?"},
    ]
    out = _format_answer(
        qs, {"0": {"selected": ["Red"]}, "1": {"other": "freehand"}}
    )
    assert "Color?" in out and "Red" in out
    assert "Other: freehand" in out


def test_poll_returns_on_answer(monkeypatch):
    import open_webui.utils.ask_user_tool as mod
    import open_webui.models.chats as chats_mod

    calls = {"n": 0}

    async def fake_get(chat_id, tcid):
        calls["n"] += 1
        if calls["n"] >= 2:
            return {"answer": {"0": {"selected": ["X"]}}}
        return None

    monkeypatch.setattr(chats_mod.Chats, "get_question_answer_by_id", fake_get)
    monkeypatch.setattr(mod, "ASK_USER_POLL_INTERVAL_SECONDS", 1, raising=False)

    out = asyncio.run(
        mod._ask_via_durable_poll("chat1", "tc1", [{"question": "Q?"}])
    )
    assert "X" in out


def test_poll_returns_on_skip(monkeypatch):
    import open_webui.utils.ask_user_tool as mod
    import open_webui.models.chats as chats_mod

    async def fake_get(chat_id, tcid):
        return {"skipped": True}

    monkeypatch.setattr(chats_mod.Chats, "get_question_answer_by_id", fake_get)
    out = asyncio.run(
        mod._ask_via_durable_poll("chat1", "tc1", [{"question": "Q?"}])
    )
    assert "skipped" in out.lower()


def test_poll_times_out(monkeypatch):
    import open_webui.utils.ask_user_tool as mod
    import open_webui.models.chats as chats_mod

    async def never(chat_id, tcid):
        return None

    monkeypatch.setattr(chats_mod.Chats, "get_question_answer_by_id", never)
    monkeypatch.setattr(mod, "ASK_USER_POLL_INTERVAL_SECONDS", 1, raising=False)
    monkeypatch.setattr(mod, "ASK_USER_TIMEOUT_SECONDS", 1, raising=False)

    out = asyncio.run(
        mod._ask_via_durable_poll("chat1", "tc1", [{"question": "Q?"}])
    )
    assert "did not answer" in out.lower()


def test_poll_propagates_cancel(monkeypatch):
    import open_webui.utils.ask_user_tool as mod
    import open_webui.models.chats as chats_mod

    async def never(chat_id, tcid):
        return None

    monkeypatch.setattr(chats_mod.Chats, "get_question_answer_by_id", never)
    monkeypatch.setattr(mod, "ASK_USER_POLL_INTERVAL_SECONDS", 10, raising=False)
    monkeypatch.setattr(mod, "ASK_USER_TIMEOUT_SECONDS", 0, raising=False)

    async def driver():
        task = asyncio.ensure_future(
            mod._ask_via_durable_poll("chat1", "tc1", [{"question": "Q?"}])
        )
        await asyncio.sleep(0.05)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(driver())


def test_spec_builder_strips_dunder_and_marks_barrier():
    from open_webui.utils.tools import get_ask_user_tool_specs

    specs = get_ask_user_tool_specs({})
    au = specs["ask_user"]
    assert au["id"] == "builtin:ask_user"
    assert au["metadata"]["parallelizable"] is False
    spec = au["spec"]
    fn = spec.get("function", spec)
    props = fn["parameters"]["properties"]
    assert "questions" in props
    assert not [k for k in props if k.startswith("__")]
