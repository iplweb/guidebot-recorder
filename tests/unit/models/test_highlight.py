"""Krok `highlight` — normalizacja skrótu, walidacja pól, scalanie z configiem."""

import pytest
from pydantic import ValidationError

from guidebot_recorder.models.config import HighlightConfig
from guidebot_recorder.models.scenario import Highlight, Step


def test_shorthand_string_normalizes_to_the_object_form():
    s = Step.model_validate({"highlight": "przycisk Zapisz"})

    assert s.highlight_config() == Highlight(what="przycisk Zapisz")


def test_full_form_keeps_every_field():
    s = Step.model_validate(
        {
            "highlight": {
                "what": "tabela z wynikami",
                "padding": 12,
                "loops": 3,
                "hold": 1.5,
                "color": "#22c55e",
            }
        }
    )

    h = s.highlight_config()
    assert (h.what, h.padding, h.loops, h.hold, h.color) == (
        "tabela z wynikami",
        12,
        3,
        1.5,
        "#22c55e",
    )


def test_command_kind_and_target_requirement():
    s = Step.model_validate({"highlight": "przycisk Zapisz", "say": "o, tutaj"})

    assert s.command_kind() == "highlight"
    assert s.requires_target()


def test_optional_is_allowed_because_the_step_has_a_target():
    s = Step.model_validate({"highlight": "banner zgody", "optional": True})

    assert s.optional


def test_highlight_counts_as_a_command_so_two_commands_are_rejected():
    with pytest.raises(ValidationError):
        Step.model_validate({"highlight": "przycisk", "click": "przycisk"})


def test_blank_target_is_rejected_at_load_time():
    with pytest.raises(ValidationError):
        Step.model_validate({"highlight": "   "})


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        Step.model_validate({"highlight": {"what": "tabela", "shape": "rect"}})


@pytest.mark.parametrize(
    "overrides",
    [
        {"loops": 0},
        {"loops": 6},
        {"padding": -1},
        {"hold": -1},
    ],
)
def test_out_of_range_knobs_are_rejected(overrides):
    with pytest.raises(ValidationError):
        Step.model_validate({"highlight": {"what": "tabela", **overrides}})


def test_resolved_falls_back_to_config_defaults():
    defaults = HighlightConfig()

    r = Highlight(what="tabela").resolved(defaults)

    assert (r.what, r.padding, r.loops, r.hold, r.color) == (
        "tabela",
        defaults.padding,
        defaults.loops,
        defaults.hold,
        defaults.color,
    )


def test_resolved_lets_the_step_win_over_the_config():
    defaults = HighlightConfig(padding=8, loops=2, hold=0.6, color="#fff")

    r = Highlight(what="tabela", padding=20, loops=4, hold=2.0, color="#000").resolved(defaults)

    assert (r.padding, r.loops, r.hold, r.color) == (20, 4, 2.0, "#000")
