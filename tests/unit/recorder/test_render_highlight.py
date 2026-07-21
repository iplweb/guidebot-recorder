"""Kontrakt sidecara dla kroku `highlight` — bez przeglądarki."""

from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.scenario import Step
from guidebot_recorder.models.target import (
    TestidTarget as ByTestidTarget,  # alias: pytest próbuje zbierać `Test*` jako klasę testów
)
from guidebot_recorder.recorder.render import _compiled_action_is_current, _compiled_from

STEP = Step.model_validate({"highlight": "tabela z wynikami"})


def _cached(action: str) -> CachedAction:
    return CachedAction(
        action=action,
        target=ByTestidTarget(testid="wyniki"),
        identity=None,
        expect="none",
        fingerprint=Fingerprint(
            command_kind="highlight",
            compiled_from="tabela z wynikami",
            expect="none",
            config_hash="hash",
        ),
    )


def test_compiled_from_is_the_highlight_target():
    assert _compiled_from(STEP) == "tabela z wynikami"


def test_a_frozen_highlight_is_replayed_as_is():
    assert _compiled_action_is_current(STEP, _cached("highlight"), "hash")


def test_a_sidecar_that_froze_some_other_action_is_rejected():
    """Bez wpisu w mapie `expected_action` niezgodna akcja przeszłaby po cichu."""

    assert not _compiled_action_is_current(STEP, _cached("hover"), "hash")


def test_visual_knobs_do_not_force_a_recompile():
    """`padding`/`loops`/`hold`/`color` nie zmieniają celu, więc nie ruszają cache'u."""

    restyled = Step.model_validate(
        {"highlight": {"what": "tabela z wynikami", "padding": 30, "color": "#000"}}
    )

    assert _compiled_action_is_current(restyled, _cached("highlight"), "hash")
