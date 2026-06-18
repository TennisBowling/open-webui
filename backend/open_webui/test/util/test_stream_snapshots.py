from open_webui.utils.stream_state import terminal_status_from_message


def test_terminal_status_prefers_error():
    assert terminal_status_from_message({"done": True, "error": {"content": "x"}}) == "error"


def test_terminal_status_detects_user_stop():
    assert terminal_status_from_message({"done": True, "userStopped": True}) == "cancelled"


def test_terminal_status_ignores_in_progress_message():
    assert terminal_status_from_message({"done": False}) is None
