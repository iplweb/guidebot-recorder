"""Effective per-step select mode (spec §5).

How a step's ``mode:`` override resolves against ``config.selects.mode``, how it
enters the compiled fingerprint (and stays out of the reasoner prompt), and why
the one unreachable combination is rejected at load time. The dispatch that acts
on the resolved mode lives in ``test_selects_wiring_dispatch.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.config import SelectsConfig
from guidebot_recorder.models.scenario import Scenario, Select, Step, StepPathError, select_mode
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.recorder.render import _compiled_action_is_current, _compiled_from
from guidebot_recorder.resolver.resolution import compiled_from, step_instruction

from ._selects_wiring_helpers import _config, _select_step


def test_select_mode_inherits_the_config_mode() -> None:
    step = Step(select=Select(**{"from": "lista", "option": "Mazowieckie"}))

    assert select_mode(step, _config()) == "shim"
    assert select_mode(step, _config(selects=SelectsConfig(mode="native"))) == "native"


def test_a_step_can_opt_out_of_the_shim_but_never_opt_back_into_it() -> None:
    """The override is one-way, and the scenario says so before a browser opens.

    This used to be spelled "overrides the config mode in both directions" and
    asserted the dispatch flag `select_mode` returns — which was true and
    meaningless: under `config.selects.mode: native` nothing installs the widget,
    so a step asking for `shim` reached a page with no shim on it and failed
    mid-render, having already clicked an unrelated element on camera. The flag
    said "shim"; the outcome was a broken run.

    The reachable direction keeps working, and the unreachable one is now a load
    error, so no run can start in that state at all.
    """

    native_step = Step(
        select=Select(**{"from": "lista", "option": "Mazowieckie", "mode": "native"})
    )
    shim_step = Step(select=Select(**{"from": "lista", "option": "Mazowieckie", "mode": "shim"}))

    # Opting one control out of a global shim: supported, and the whole point.
    assert select_mode(native_step, _config()) == "native"
    assert select_mode(shim_step, _config()) == "shim"

    # Opting one control *into* a shim that was never installed: rejected while
    # the scenario loads, naming the step and the setting that fights it. The
    # step is named by `StepPathError.path`, which the loader turns into
    # `plik:linia` + the YAML fragment — see `tests/unit/scenario/
    # test_loader_validation.py` for the banner this produces end to end.
    with pytest.raises(ValidationError) as excinfo:
        Scenario(config=_config(selects=SelectsConfig(mode="native")), steps=[shim_step])

    origin = excinfo.value.errors()[0]["ctx"]["error"]
    assert isinstance(origin, StepPathError)
    assert origin.path == (0,)
    assert "config.selects.mode: native" in str(excinfo.value)


def test_a_steps_select_mode_is_part_of_its_own_fingerprint() -> None:
    """Deleting `mode: native` from a step must force a recompile.

    The spec claims the per-step mode "enters the fingerprint through
    `compiled_from` like any other step content". It did not: both fingerprint
    builders returned `select.from_` alone, so removing the escape hatch left
    `compile_up_to_date()` true, no browser was launched, and the drivability
    probe — one of the two mitigations the spec's error handling leans on —
    never ran.
    """

    assert compiled_from(_select_step()) == "Województwo"  # unchanged when unset
    assert compiled_from(_select_step("native")) != compiled_from(_select_step())
    assert compiled_from(_select_step("shim")) != compiled_from(_select_step("native"))
    # render and compile must agree about it, or a render would recompile forever
    assert _compiled_from(_select_step("native")) == compiled_from(_select_step("native"))


def test_the_reasoner_still_sees_only_the_step_sentence() -> None:
    """The fingerprint is not the prompt.

    `step_instruction` is what the LLM resolves against; folding a YAML
    keyword into it would put `mode: native` in front of the reasoner as if it
    were part of the author's description of the control.
    """

    assert step_instruction(_select_step("native")) == "Województwo"
    assert step_instruction(_select_step()) == "Województwo"


def test_a_frozen_action_is_stale_once_the_step_drops_its_mode() -> None:
    """The outcome the fingerprint exists for, asserted end to end."""

    frozen = CachedAction(
        action="select",
        target=RoleTarget(role="combobox", name="Województwo", exact=True),
        expect="none",
        fingerprint=Fingerprint(
            command_kind="select",
            compiled_from=compiled_from(_select_step("native")),
            expect="none",
            config_hash="h",
        ),
    )

    assert _compiled_action_is_current(_select_step("native"), frozen, "h") is True
    assert _compiled_action_is_current(_select_step(), frozen, "h") is False
