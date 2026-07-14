import pytest
from pydantic import ValidationError

from guidebot_recorder.models.scenario import NavigateConfig, Step


def test_single_command_ok():
    s = Step.model_validate({"teach": "kliknij X"})
    assert s.command_kind() == "teach"
    assert s.requires_target()


def test_two_commands_forbidden():
    with pytest.raises(ValidationError):
        Step.model_validate({"click": "X", "navigate": "http://x"})


def test_say_with_action_ok():
    s = Step.model_validate({"enterText": {"into": "email", "text": "a@b"}, "say": "wpisuję"})
    assert s.command_kind() == "enterText"
    assert s.say == "wpisuję"


def test_pure_say_needs_no_target():
    s = Step.model_validate({"say": "witaj"})
    assert s.command_kind() == "say"
    assert not s.requires_target()


def test_wait_until_requires_target():
    s = Step.model_validate({"wait": {"until": "aż pojawi się X"}})
    assert s.command_kind() == "wait"
    assert s.requires_target()


def test_wait_seconds_no_target():
    s = Step.model_validate({"wait": 2.0})
    assert not s.requires_target()


def test_navigate_string_keeps_backwards_compatible_value_and_helpers():
    s = Step.model_validate({"navigate": "https://example.com"})

    assert s.navigate == "https://example.com"
    assert s.navigate_url() == "https://example.com"
    assert s.navigate_type_override() is None


@pytest.mark.parametrize("animate", [True, False])
def test_navigate_object_exposes_url_and_type_override(animate):
    s = Step.model_validate(
        {"navigate": {"url": "https://example.com", "type": animate}}
    )

    assert s.navigate == NavigateConfig(url="https://example.com", type=animate)
    assert s.navigate_url() == "https://example.com"
    assert s.navigate_type_override() is animate
    assert s.command_kind() == "navigate"
    assert not s.requires_target()


def test_navigate_object_type_is_optional():
    s = Step.model_validate({"navigate": {"url": "/login"}})

    assert s.navigate_url() == "/login"
    assert s.navigate_type_override() is None


@pytest.mark.parametrize(
    "navigate",
    [
        {"type": True},
        {"url": "https://example.com", "unknown": True},
    ],
)
def test_navigate_object_rejects_invalid_shape(navigate):
    with pytest.raises(ValidationError):
        Step.model_validate({"navigate": navigate})
