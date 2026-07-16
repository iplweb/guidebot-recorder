import pytest
from pydantic import ValidationError

from guidebot_recorder.models.scenario import NavigateConfig, Scenario, Slide, Step
from guidebot_recorder.models.config import Config, Viewport, TtsConfig


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


def test_slide_requires_at_least_one_text_field():
    with pytest.raises(ValidationError):
        Slide()
    s = Slide(title="Logowanie")
    assert s.hold == 2.5


def test_step_slide_command_kind_and_no_target():
    step = Step(slide=Slide(title="T"), say="narracja")
    assert step.command_kind() == "slide"
    assert step.requires_target() is False
    assert step.narration() == "narracja"


def test_slide_is_mutually_exclusive_with_other_primaries():
    with pytest.raises(ValidationError):
        Step(slide=Slide(title="T"), click="ok")


def test_slide_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        Slide(title="x", bogus=1)


def test_silent_slide_forbids_translations():
    # A silent slide has narration() is None, so the Scenario validator must
    # reject any translations attached to it.
    with pytest.raises(ValidationError):
        Scenario(
            config=Config(
                title="t",
                viewport=Viewport(width=8, height=6),
                tts=TtsConfig(provider="edge", voice="v", lang="pl-PL"),
            ),
            steps=[Step(slide=Slide(title="T"), translations={"en-US": "x"})],
        )


def test_say_slide_requires_translations_for_each_audio_track():
    config = Config(
        title="t",
        viewport=Viewport(width=8, height=6),
        tts=TtsConfig(provider="edge", voice="pl", lang="pl-PL", trackLanguage="pol"),
        audioTracks=[
            TtsConfig(provider="edge", voice="en", lang="en-US", trackLanguage="eng"),
        ],
    )

    # A slide WITH `say` narrates → translations required for each audio track.
    with pytest.raises(ValidationError):
        Scenario(
            config=config,
            steps=[Step(slide=Slide(title="T"), say="cześć")],
        )

    # Supplying the missing translation makes it valid.
    scenario = Scenario(
        config=config,
        steps=[Step(slide=Slide(title="T"), say="cześć", translations={"en-US": "hi"})],
    )
    assert scenario.steps[0].narration() == "cześć"
    assert scenario.steps[0].translations == {"en-US": "hi"}
