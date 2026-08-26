from types import SimpleNamespace

import pytest
import requests
from pydantic import ValidationError

from open_webui.routers import audio


def make_stt_config(**overrides):
    values = {
        "OPENAI_API_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_API_KEY": "",
        "OPENROUTER_API_KEY": "",
        "OPENROUTER_TEMPERATURE": None,
        "ENGINE": "openrouter",
        "MODEL": "openai/whisper-large-v3",
        "SUPPORTED_CONTENT_TYPES": [],
        "WHISPER_MODEL": "base",
        "DEEPGRAM_API_KEY": "",
        "AZURE_API_KEY": "",
        "AZURE_REGION": "",
        "AZURE_LOCALES": "",
        "AZURE_BASE_URL": "",
        "AZURE_MAX_SPEAKERS": "",
    }
    return audio.STTConfigForm(**(values | overrides))


def test_openrouter_stt_temperature_is_limited_to_documented_range():
    assert make_stt_config(OPENROUTER_TEMPERATURE=0.5).OPENROUTER_TEMPERATURE == 0.5

    with pytest.raises(ValidationError):
        make_stt_config(OPENROUTER_TEMPERATURE=1.1)


def test_normalize_openrouter_stt_model_keeps_picker_metadata():
    model = audio.normalize_openrouter_stt_model(
        {
            "id": "openai/whisper-large-v3",
            "name": "OpenAI: Whisper Large V3",
            "description": "Multilingual transcription.",
            "architecture": {
                "input_modalities": ["audio"],
                "output_modalities": ["transcription"],
            },
            "pricing": {"prompt": "0.0015", "completion": "0"},
            "supported_parameters": ["temperature", "response_format", None, ""],
        }
    )

    assert model == {
        "id": "openai/whisper-large-v3",
        "name": "OpenAI: Whisper Large V3",
        "description": "Multilingual transcription.",
        "context_length": 0,
        "pricing": {"prompt": "0.0015", "completion": "0"},
        "supported_parameters": ["temperature", "response_format"],
    }


def test_normalize_openrouter_stt_model_rejects_non_transcription_models():
    assert (
        audio.normalize_openrouter_stt_model(
            {
                "id": "example/chat-model",
                "architecture": {"output_modalities": ["text"]},
            }
        )
        is None
    )


def test_transcribe_openrouter_audio_forwards_settings_and_retries_without_language(
    monkeypatch, tmp_path
):
    audio_file = tmp_path / "recording.webm"
    audio_file.write_bytes(b"audio")
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}")

    responses = [
        FakeResponse(400, {"error": {"message": "Unsupported language hint"}}),
        FakeResponse(200, {"text": "Hello from OpenRouter", "usage": {"cost": 0.01}}),
    ]

    def fake_post(**kwargs):
        calls.append(
            {
                "url": kwargs["url"],
                "headers": kwargs["headers"],
                "data": dict(kwargs["data"]),
                "timeout": kwargs["timeout"],
            }
        )
        return responses[len(calls) - 1]

    monkeypatch.setattr(audio.requests, "post", fake_post)
    monkeypatch.setattr(audio, "WHISPER_LANGUAGE", "")

    config = SimpleNamespace(
        STT_OPENROUTER_API_KEY="sk-or-v1-test",
        STT_OPENROUTER_TEMPERATURE=0.2,
        STT_MODEL="openai/whisper-large-v3",
        WEBUI_URL="https://chat.example.com",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(config=config, WEBUI_NAME="Example Chat")
        ),
        base_url="http://localhost/",
    )

    result = audio.transcribe_openrouter_audio(
        request, str(audio_file), {"language": "en"}
    )

    assert result["text"] == "Hello from OpenRouter"
    assert len(calls) == 2
    assert calls[0]["data"] == {
        "model": "openai/whisper-large-v3",
        "response_format": "json",
        "language": "en",
        "temperature": "0.2",
    }
    assert "language" not in calls[1]["data"]
    assert calls[1]["headers"]["HTTP-Referer"] == "https://chat.example.com"
    assert calls[1]["headers"]["X-OpenRouter-Title"] == "Example Chat"


def test_transcribe_openrouter_audio_requires_a_key(tmp_path):
    audio_file = tmp_path / "recording.wav"
    audio_file.write_bytes(b"audio")
    config = SimpleNamespace(
        STT_OPENROUTER_API_KEY="",
        STT_MODEL="openai/whisper-1",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(config=config))
    )

    with pytest.raises(ValueError, match="API key"):
        audio.transcribe_openrouter_audio(request, str(audio_file))
