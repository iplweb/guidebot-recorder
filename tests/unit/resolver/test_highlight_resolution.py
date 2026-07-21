"""`highlight` w warstwie resolvera: mapowanie akcji i szczelność słownika Reasonera."""

import pytest

from guidebot_recorder.models.action import REASONER_ACTIONS
from guidebot_recorder.models.scenario import Step
from guidebot_recorder.resolver.reasoner import _response_schema_json
from guidebot_recorder.resolver.resolution import action_for, step_instruction


def test_step_instruction_is_the_highlight_target():
    step = Step.model_validate({"highlight": "tabela z wynikami"})

    assert step_instruction(step) == "tabela z wynikami"


def test_action_for_highlight_stays_highlight():
    assert action_for("highlight", "click") == "highlight"


def test_reasoner_may_not_invent_a_highlight_for_a_teach_step():
    """`teach` to jedyna ścieżka, w której o akcji decyduje model, a nie plik."""

    with pytest.raises(ValueError, match="highlight"):
        action_for("teach", "highlight")


def test_reasoner_vocabulary_is_not_derived_from_action_kind():
    assert "highlight" not in REASONER_ACTIONS


def test_response_schema_never_offers_highlight_to_the_model():
    assert "highlight" not in _response_schema_json()
