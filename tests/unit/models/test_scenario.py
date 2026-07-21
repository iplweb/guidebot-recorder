from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from guidebot_recorder.models.config import Config, TtsConfig, Viewport
from guidebot_recorder.models.scenario import (
    Desktop,
    FlatStep,
    NavigateConfig,
    Scenario,
    Scroll,
    Select,
    Slide,
    Step,
    StepPathError,
    WaitUntil,
    WhenBlock,
)
from guidebot_recorder.scenario.source import build_source


def test_scroll_command_kind_no_target():
    s = Step.model_validate({"scroll": "down", "say": "przewijam"})
    assert s.command_kind() == "scroll"
    assert not s.requires_target()
    assert s.scroll_config() == Scroll(to="down")


def test_scroll_object_form_with_amount():
    s = Step.model_validate({"scroll": {"to": "bottom"}})
    assert s.scroll_config().to == "bottom"
    s2 = Step.model_validate({"scroll": {"to": "down", "amount": 250}})
    assert s2.scroll_config().amount == 250


def test_scroll_rejects_bad_direction():
    with pytest.raises(ValidationError):
        Step.model_validate({"scroll": "sideways"})


def test_select_command_kind_and_target():
    s = Step.model_validate(
        {"select": {"from": "the report type list", "option": "tabela"}, "say": "wybieram"}
    )
    assert s.command_kind() == "select"
    assert s.requires_target()
    assert s.select.from_ == "the report type list"
    assert s.select.option == "tabela"


def test_select_from_alias_and_extra_forbidden():
    sel = Select.model_validate({"from": "list", "option": "tabela"})
    assert sel.from_ == "list"
    with pytest.raises(ValidationError):
        Select.model_validate({"from": "list", "option": "tabela", "extra": 1})


def test_select_requires_option():
    with pytest.raises(ValidationError):
        Step.model_validate({"select": {"from": "list"}})


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
    s = Step.model_validate({"navigate": {"url": "https://example.com", "type": animate}})

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


def _config(**kwargs) -> Config:
    return Config(
        title="t",
        viewport=Viewport(width=8, height=6),
        tts=TtsConfig(provider="edge", voice="v", lang="pl-PL"),
        **kwargs,
    )


# --- WhenBlock ---------------------------------------------------------------


def test_when_block_parses_with_defaults():
    block = WhenBlock.model_validate(
        {"when": "baner cookies", "steps": [{"teach": "kliknij zgadzam się"}]}
    )

    assert block.when == "baner cookies"
    assert block.state == "visible"
    assert block.timeout == 10.0
    assert [s.command_kind() for s in block.steps] == ["teach"]


def test_when_block_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        WhenBlock.model_validate({"when": "x", "steps": [], "bogus": 1})


def test_nested_when_block_is_rejected():
    with pytest.raises(ValidationError):
        WhenBlock.model_validate(
            {
                "when": "outer",
                "steps": [{"when": "inner", "steps": [{"say": "hi"}]}],
            }
        )


def test_scenario_accepts_mix_of_steps_and_blocks():
    scenario = Scenario.model_validate(
        {
            "config": _config().model_dump(by_alias=True),
            "steps": [
                {"navigate": "https://example.com"},
                {
                    "when": "baner cookies",
                    "timeout": 3,
                    "steps": [{"teach": "kliknij zgadzam się"}],
                },
                {"teach": "kliknij ikonę konta"},
            ],
        }
    )

    assert isinstance(scenario.steps[0], Step)
    assert isinstance(scenario.steps[1], WhenBlock)
    assert scenario.steps[1].timeout == 3
    assert isinstance(scenario.steps[2], Step)


# --- Step.optional -----------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"teach": "kliknij X"},
        {"click": "X"},
        {"hover": "X"},
        {"enterText": {"into": "email", "text": "a@b"}},
        {"wait": {"until": "aż pojawi się X"}},
        {"wait": 2.0},
    ],
)
def test_optional_allowed_on_target_bearing_and_numeric_wait_steps(payload):
    step = Step.model_validate({**payload, "optional": True})
    assert step.optional is True


@pytest.mark.parametrize(
    "payload",
    [
        {"say": "cześć"},
        {"navigate": "https://example.com"},
        {"slide": {"title": "T"}},
    ],
)
def test_optional_rejected_on_steps_without_a_target(payload):
    with pytest.raises(ValidationError):
        Step.model_validate({**payload, "optional": True})


def test_optional_defaults_to_false_and_stays_allowed_when_absent():
    assert Step.model_validate({"say": "cześć"}).optional is False
    assert Step.model_validate({"navigate": "https://x"}).optional is False


# --- translations recurse into blocks ----------------------------------------


def _multi_track_config() -> Config:
    return Config(
        title="t",
        viewport=Viewport(width=8, height=6),
        tts=TtsConfig(provider="edge", voice="pl", lang="pl-PL", trackLanguage="pol"),
        audioTracks=[
            TtsConfig(provider="edge", voice="en", lang="en-US", trackLanguage="eng"),
        ],
    )


def test_translation_validator_recurses_into_block_children():
    with pytest.raises(ValidationError):
        Scenario(
            config=_multi_track_config(),
            steps=[WhenBlock(when="baner", steps=[Step(teach="kliknij X")])],
        )


def test_translation_validator_accepts_translated_block_children():
    scenario = Scenario(
        config=_multi_track_config(),
        steps=[
            WhenBlock(
                when="baner",
                steps=[Step(teach="kliknij X", translations={"en-US": "click X"})],
            )
        ],
    )
    assert scenario.steps[0].steps[0].translations == {"en-US": "click X"}


def test_translation_validator_rejects_translations_without_narration_in_block():
    with pytest.raises(ValidationError):
        Scenario(
            config=_config(),
            steps=[
                WhenBlock(
                    when="baner",
                    steps=[Step(wait=2.0, translations={"en-US": "x"})],
                )
            ],
        )


# --- flat_steps --------------------------------------------------------------


def test_flat_steps_without_blocks_is_identity():
    scenario = Scenario(
        config=_config(),
        steps=[Step(navigate="https://x"), Step(teach="kliknij")],
    )

    flat = scenario.flat_steps()

    assert [f.step for f in flat] == list(scenario.steps)
    assert [f.branch for f in flat] == [None, None]
    assert [f.is_gate for f in flat] == [False, False]


def test_flat_steps_expands_block_into_gate_plus_children():
    block = WhenBlock(
        when="baner cookies",
        state="visible",
        timeout=3.5,
        steps=[Step(teach="kliknij zgadzam się"), Step(say="akceptujemy")],
    )
    scenario = Scenario(
        config=_config(),
        steps=[Step(navigate="https://x"), block, Step(teach="konto")],
    )

    flat = scenario.flat_steps()

    assert len(flat) == 5
    assert [f.branch for f in flat] == [None, 1, 1, 1, None]
    assert [f.is_gate for f in flat] == [False, True, False, False, False]

    gate = flat[1].step
    assert gate.command_kind() == "wait"
    assert isinstance(gate.wait, WaitUntil)
    assert gate.wait.until == "baner cookies"
    assert gate.wait.state == "visible"
    assert gate.wait.timeout == 3.5
    assert gate.requires_target()

    assert flat[2].step is block.steps[0]
    assert flat[3].step is block.steps[1]
    assert flat[4].step is scenario.steps[2]


def test_flat_steps_branch_index_is_the_top_level_block_index():
    scenario = Scenario(
        config=_config(),
        steps=[
            WhenBlock(when="a", steps=[Step(say="x")]),
            WhenBlock(when="b", steps=[Step(say="y")]),
        ],
    )

    flat = scenario.flat_steps()

    assert [f.branch for f in flat] == [0, 0, 1, 1]
    assert [f.is_gate for f in flat] == [True, False, True, False]


def test_flat_step_is_a_named_tuple():
    flat = FlatStep(step=Step(say="x"), branch=None, is_gate=False)
    assert tuple(flat) == (flat.step, None, False, None)


def test_flat_step_location_defaults_to_none():
    assert FlatStep(step=Step(say="x"), branch=None, is_gate=False).location is None


# --- lokalizacja kroków w źródle (ScenarioSource) -----


SOURCE_TEXT = """\
config:
  title: "t"
  viewport: { width: 8, height: 6 }
  tts: { provider: edge, voice: v, lang: pl-PL }
steps:
  - say: "pierwszy"
  - when: "baner"
    state: visible
    steps:
      - click: "ok"
  - say: "ostatni"
"""


def _scenario_with_source() -> tuple[Scenario, object]:
    source = build_source(Path("test.scenario.yaml"), SOURCE_TEXT)
    scenario = Scenario.model_validate(YAML(typ="safe").load(SOURCE_TEXT))
    scenario.attach_source(source)
    return scenario, source


def test_scenario_has_no_source_until_one_is_attached():
    scenario = Scenario(config=_config(), steps=[Step(say="x")])

    assert scenario.source is None
    assert scenario.flat_steps()[0].location is None


def test_attach_source_exposes_it_through_the_source_property():
    scenario, source = _scenario_with_source()

    assert scenario.source is source


def test_flat_steps_are_stamped_with_locations_from_the_source():
    scenario, source = _scenario_with_source()

    flat = scenario.flat_steps()

    assert len(flat) == 4
    assert [entry.location for entry in flat] == list(source.steps)
    assert flat[1].location.is_gate is True
    assert flat[2].location.gate_line == flat[1].location.line


def test_source_survives_model_copy():
    scenario, source = _scenario_with_source()

    assert scenario.model_copy().source is source


# --- StepPathError -----


def test_step_path_error_is_a_value_error_carrying_a_positional_path():
    error = StepPathError("brak tłumaczeń dla ścieżek: en-US", path=(3, 1))

    assert isinstance(error, ValueError)
    assert error.path == (3, 1)
    assert str(error) == "brak tłumaczeń dla ścieżek: en-US"


# --- caption (PDF guide) -----


def test_step_accepts_optional_caption():
    step = Step(click="the login button", caption='Kliknij duży niebieski przycisk "Zaloguj".')
    assert step.caption == 'Kliknij duży niebieski przycisk "Zaloguj".'


def test_step_caption_defaults_to_none():
    assert Step(say="hello").caption is None


def test_step_with_only_caption_is_still_empty_step():
    with pytest.raises(ValidationError):
        Step(caption="tekst bez komendy i bez say")


def test_desktop_is_a_visual_only_command():
    s = Step.model_validate({"desktop": {"icon": "firefox", "label": "FF"}, "say": "otwieram"})
    assert s.command_kind() == "desktop"
    assert not s.requires_target()
    assert s.desktop is not None and s.desktop.icon == "firefox"


def test_desktop_defaults():
    d = Desktop()
    assert d.icon == "chrome"
    assert d.is_builtin_icon()
    assert d.hold == 1.0


def test_desktop_and_another_command_is_rejected():
    with pytest.raises(ValidationError):
        Step.model_validate({"desktop": {"icon": "chrome"}, "navigate": "https://x"})


def test_optional_is_refused_on_a_desktop_step():
    # No target to be absent, so `optional` promises tolerance we cannot deliver.
    with pytest.raises(ValidationError):
        Step.model_validate({"desktop": {"icon": "chrome"}, "optional": True})


def test_close_window_command_kind_and_no_target():
    step = Step.model_validate({"closeWindow": True})
    assert step.command_kind() == "closeWindow"
    assert step.requires_target() is False
    assert step.narration() is None


def test_close_window_accepts_narration():
    step = Step.model_validate({"closeWindow": True, "say": "Wracamy."})
    assert step.command_kind() == "closeWindow"
    assert step.narration() == "Wracamy."


def test_close_window_false_is_rejected():
    # `_exactly_one_command` tests `is not None`, so a plain `bool` field would let
    # `closeWindow: false` count as a present command that does nothing. Literal[True]
    # turns that into a validation error instead of a silent no-op.
    with pytest.raises(ValidationError):
        Step.model_validate({"closeWindow": False})


def test_close_window_is_mutually_exclusive_with_other_primaries():
    with pytest.raises(ValidationError):
        Step.model_validate({"closeWindow": True, "click": "ok"})


def test_close_window_rejects_optional():
    # No target, not a numeric wait -> `_optional_only_where_it_can_be_honoured` rejects it.
    with pytest.raises(ValidationError):
        Step.model_validate({"closeWindow": True, "optional": True})
