"""Resolution rules for `model_supports_video_input`.

The precedence here is the whole contract: an admin's explicit tick must beat
provider metadata in BOTH directions, and absence must mean "no" (unlike vision,
which defaults to True) because attaching a multi-megabyte clip to a model that
cannot read it is a hard failure rather than a degraded response.
"""

from open_webui.utils.models import model_supports_video_input


def _caps(**kwargs):
    return {"info": {"meta": {"capabilities": kwargs}}}


def test_absent_metadata_means_not_supported():
    # Deliberately unlike vision's default-True: video payloads are far too
    # large to attach speculatively.
    assert model_supports_video_input({"id": "some/model"}) is False


def test_detects_flattened_input_modalities():
    # The only signal available when the connection uses a model_ids allowlist,
    # since that path synthesizes stubs with no `architecture`.
    assert (
        model_supports_video_input(
            {"id": "g", "input_modalities": ["text", "image", "video"]}
        )
        is True
    )


def test_detects_nested_architecture():
    assert (
        model_supports_video_input(
            {"id": "g", "architecture": {"input_modalities": ["text", "video"]}}
        )
        is True
    )


def test_detects_architecture_under_openai_echo():
    assert (
        model_supports_video_input(
            {"id": "g", "openai": {"architecture": {"input_modalities": ["video"]}}}
        )
        is True
    )


def test_text_only_model_is_not_supported():
    assert (
        model_supports_video_input({"id": "d", "input_modalities": ["text"]}) is False
    )


def test_explicit_true_overrides_missing_metadata():
    # The escape hatch for local/proxied models that report no modalities.
    assert model_supports_video_input({"id": "local", **_caps(video=True)}) is True


def test_explicit_false_overrides_capable_provider():
    model = {
        "id": "g",
        "input_modalities": ["text", "video"],
        **_caps(video=False),
    }
    assert model_supports_video_input(model) is False


def test_unrelated_capabilities_do_not_imply_video():
    model = {"id": "g", "input_modalities": ["text", "image"], **_caps(vision=True)}
    assert model_supports_video_input(model) is False


def test_malformed_input_is_safe():
    for bad in (None, [], "model", 7, {"id": "g", "input_modalities": "video"}):
        assert model_supports_video_input(bad) is False
