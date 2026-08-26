"""Regression: get_message_list must terminate on corrupted (cyclic) trees.

A message persisted with parentId == its own id (observed live: the
assemble_conversation_from_leaf fallback resolved parent to the message
itself when the completion POST raced/omitted the user-row save) used to
spin the walk forever ON THE EVENT LOOP — a server-wide 100%-CPU freeze in
background_tasks_handler. The same walk exists client-side in
createMessagesList (frozen tab).
"""

from open_webui.utils.misc import get_message_list


def test_self_parented_message_terminates():
    messages = {
        "a": {"id": "a", "parentId": "a", "role": "user", "content": "hi"},
    }
    result = get_message_list(messages, "a")
    assert [m["id"] for m in result] == ["a"]


def test_two_node_cycle_terminates():
    messages = {
        "a": {"id": "a", "parentId": "b", "role": "user", "content": "hi"},
        "b": {"id": "b", "parentId": "a", "role": "assistant", "content": "yo"},
    }
    result = get_message_list(messages, "b")
    assert [m["id"] for m in result] == ["a", "b"]


def test_normal_chain_unaffected():
    messages = {
        "u1": {"id": "u1", "parentId": None, "role": "user", "content": "q1"},
        "a1": {"id": "a1", "parentId": "u1", "role": "assistant", "content": "r1"},
        "u2": {"id": "u2", "parentId": "a1", "role": "user", "content": "q2"},
        "a2": {"id": "a2", "parentId": "u2", "role": "assistant", "content": "r2"},
    }
    result = get_message_list(messages, "a2")
    assert [m["id"] for m in result] == ["u1", "a1", "u2", "a2"]


def test_missing_parent_stops_cleanly():
    messages = {
        "a": {"id": "a", "parentId": "ghost", "role": "assistant", "content": "x"},
    }
    result = get_message_list(messages, "a")
    assert [m["id"] for m in result] == ["a"]
