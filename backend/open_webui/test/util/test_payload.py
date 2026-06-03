from copy import deepcopy

from open_webui.utils.payload import apply_ephemeral_cache_control_to_last_message


def test_apply_ephemeral_cache_control_to_last_message_can_be_disabled():
    payload = {
        "messages": [
            {"role": "user", "content": "hello"},
        ]
    }

    original = deepcopy(payload)
    result = apply_ephemeral_cache_control_to_last_message(payload, enabled=False)

    assert result == original
    assert "cache_control" not in result["messages"][0]


def test_apply_ephemeral_cache_control_to_last_message_marks_last_text_message():
    payload = {
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
    }

    result = apply_ephemeral_cache_control_to_last_message(payload, enabled=True)

    assert result["messages"][-1]["content"] == [
        {
            "type": "text",
            "text": "world",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_apply_ephemeral_cache_control_marks_text_not_trailing_image():
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,abc"},
                    },
                ],
            }
        ]
    }

    result = apply_ephemeral_cache_control_to_last_message(payload, enabled=True)
    parts = result["messages"][-1]["content"]

    assert parts[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in parts[1]


def test_apply_ephemeral_cache_control_skips_image_only_message():
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,abc"},
                    },
                ],
            }
        ]
    }

    result = apply_ephemeral_cache_control_to_last_message(payload, enabled=True)

    assert result == payload
