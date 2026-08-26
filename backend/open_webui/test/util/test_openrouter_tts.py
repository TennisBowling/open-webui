from open_webui.routers.audio import normalize_openrouter_tts_model


def test_normalize_openrouter_tts_model_keeps_voice_and_pricing_metadata():
    model = normalize_openrouter_tts_model(
        {
            "id": "hexgrad/kokoro-82m",
            "name": "hexgrad: Kokoro 82M",
            "description": "Lightweight multilingual speech synthesis.",
            "context_length": 4096,
            "architecture": {"output_modalities": ["speech"]},
            "pricing": {"prompt": "0.00000062", "completion": "0"},
            "supported_voices": ["af_heart", "am_michael", None, ""],
        }
    )

    assert model == {
        "id": "hexgrad/kokoro-82m",
        "name": "hexgrad: Kokoro 82M",
        "description": "Lightweight multilingual speech synthesis.",
        "context_length": 4096,
        "pricing": {"prompt": "0.00000062", "completion": "0"},
        "voices": ["af_heart", "am_michael"],
    }


def test_normalize_openrouter_tts_model_rejects_non_speech_models():
    assert (
        normalize_openrouter_tts_model(
            {
                "id": "example/text-model",
                "architecture": {"output_modalities": ["text"]},
            }
        )
        is None
    )


def test_normalize_openrouter_tts_model_handles_models_without_preset_voices():
    model = normalize_openrouter_tts_model(
        {
            "id": "minimax/speech",
            "architecture": {"output_modalities": ["speech"]},
            "supported_voices": None,
        }
    )

    assert model is not None
    assert model["voices"] == []
