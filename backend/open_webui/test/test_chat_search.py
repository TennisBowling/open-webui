import uuid
import asyncio

from test.util.db import configure_test_database

configure_test_database(required=True)

from open_webui.models.chats import ChatForm, Chats, _fuzzy_snippet  # noqa: E402
from open_webui.routers.chats import _semantic_search_text  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_postgres_chat_search_indexes_inserted_message():
    user_id = f"search-user-{uuid.uuid4()}"
    chat = run(
        Chats.insert_new_chat(
            user_id,
            ChatForm(
                chat={
                    "title": "search smoke",
                    "history": {"currentId": None, "messages": {}},
                    "models": ["test-model"],
                }
            ),
        )
    )
    assert chat is not None

    run(
        Chats.upsert_message_to_chat_by_id_and_message_id(
            chat.id,
            "m1",
            {
                "id": "m1",
                "role": "user",
                "content": "the postgres async migration search needle",
                "parentId": None,
            },
            return_model=False,
        )
    )

    result = run(Chats.search_chats(user_id, "migration search needle"))
    assert result.total >= 1
    assert any(hit.id == chat.id for hit in result.hits)


def test_postgres_chat_search_matches_title_tokens_in_any_order():
    user_id = f"search-title-user-{uuid.uuid4()}"
    chat = run(
        Chats.insert_new_chat(
            user_id,
            ChatForm(
                chat={
                    "title": "Axolotl deployment observability handbook",
                    "history": {"currentId": None, "messages": {}},
                }
            ),
        )
    )
    assert chat is not None

    result = run(Chats.search_chats(user_id, "observability axolotl"))
    assert result.total == 1
    assert result.hits[0].id == chat.id
    assert result.used_fuzzy is False


def test_postgres_chat_search_falls_back_to_typos():
    user_id = f"search-fuzzy-user-{uuid.uuid4()}"
    chat = run(
        Chats.insert_new_chat(
            user_id,
            ChatForm(
                chat={
                    "title": "Kubernetes observability dashboard",
                    "history": {"currentId": None, "messages": {}},
                }
            ),
        )
    )
    assert chat is not None

    result = run(Chats.search_chats(user_id, "kubernettes observabilty dashbord"))
    assert result.total == 1
    assert result.hits[0].id == chat.id
    assert result.used_fuzzy is True


def test_fuzzy_snippet_highlights_context_and_escapes_html():
    snippet = _fuzzy_snippet(
        "Ignore <script>alert(1)</script>; inspect the kubernetes deployment dashboard.",
        "kubernettes deployment",
    )
    assert snippet is not None
    assert "<script>" not in snippet
    assert "&lt;script&gt;" in snippet
    assert "<mark>" in snippet
    assert "kubernetes" in snippet


def test_semantic_query_gate_skips_work_the_ranker_will_discard():
    assert _semantic_search_text("docker") == ""
    assert _semantic_search_text("tag:infra docker") == ""
    assert _semantic_search_text("docker compose") == "docker compose"
    assert _semantic_search_text("gpu-passthrough") == "gpu-passthrough"


def test_postgres_chat_queue_round_trip():
    user_id = f"queue-user-{uuid.uuid4()}"
    chat = run(
        Chats.insert_new_chat(
            user_id,
            ChatForm(
                chat={
                    "title": "queue smoke",
                    "history": {"currentId": None, "messages": {}},
                    "queue": [],
                }
            ),
        )
    )
    run(Chats.append_queue_item_by_id(chat.id, {"id": "q1", "mode": "after_final"}))
    state = run(Chats.get_queue_state_by_id(chat.id))
    assert state["queue"][0]["id"] == "q1"

    popped = run(Chats.pop_queue_head_and_mark_draining_by_id(chat.id, lambda item: {"item_id": item["id"]}))
    assert popped["item"]["id"] == "q1"
    assert popped["draining"] == {"item_id": "q1"}
