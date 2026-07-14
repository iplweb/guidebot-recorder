import pytest
from pydantic import ValidationError

from guidebot_recorder.models.scenario import Step


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
