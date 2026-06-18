from typing import Optional


def terminal_status_from_message(message: Optional[dict]) -> Optional[str]:
    if not isinstance(message, dict):
        return None
    if message.get("error"):
        return "error"
    if message.get("userStopped") is True:
        return "cancelled"
    if message.get("done") is True:
        return "done"
    return None
