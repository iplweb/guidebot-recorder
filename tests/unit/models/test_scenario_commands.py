"""Kształt pojedynczej komendy w kroku scenariusza (``Step``, ``Slide``, ``Desktop``).

Tu mieszka wszystko, co da się sprawdzić na samym kroku: która komenda jest
rozpoznana, czy wymaga celu, jak wygląda jej forma skrócona i rozwinięta oraz
które kombinacje są odrzucane. Walidatory żyjące dopiero na ``Scenario``
(tłumaczenia, ``when:``-bloki, ``flat_steps``) siedzą w
``test_scenario_blocks.py``; starcie ``select.mode`` z globalnym
``config.selects.mode`` — w ``test_scenario_select_mode.py``.
"""

import pytest
from pydantic import ValidationError

from guidebot_recorder.models.scenario import (
    Desktop,
    NavigateConfig,
    Scroll,
    Select,
    Slide,
    Step,
)


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


# --- `expect:` is not an authoring control ------------------------------------


def test_expect_on_a_step_is_rejected():
    """`expect:` never reached a reader — the compiler derives it and overwrites it.

    Nothing in the package ever consulted the authored value: `heuristic_expect`
    freezes its own observation into the compiled sidecar's `CachedAction`/
    `Fingerprint`, which is where every real `.expect` read in the package
    happens. Setting it on a step used to load fine and silently do nothing;
    now it fails loudly, the same way `closeWindow: false` does.
    """

    with pytest.raises(ValidationError) as excinfo:
        Step.model_validate({"say": "Cześć.", "expect": "none"})

    message = str(excinfo.value)
    assert "expect" in message
    assert "kompilator" in message


def test_expect_is_rejected_regardless_of_its_value():
    for value in ("navigation", "idle", "none"):
        with pytest.raises(ValidationError):
            Step.model_validate({"click": "Zapisz", "expect": value})


def test_expect_is_rejected_even_alongside_a_valid_command():
    with pytest.raises(ValidationError):
        Step.model_validate({"navigate": "https://example.test", "expect": "navigation"})
