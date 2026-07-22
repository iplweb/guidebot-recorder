"""``select.mode`` — literały na kroku i jego starcie z globalnym ``config.selects.mode``.

Dwie warstwy tej samej opcji: ``Select.mode`` jako pole (co się parsuje, co
jest odrzucane, jaka jest wartość domyślna) oraz walidator na ``Scenario``,
który wyłapuje jedyną niemożliwą kombinację — krok proszący o ``shim`` pod
globalnym ``native``. Reszta kształtu ``select:`` (alias ``from``, wymagane
``option``) siedzi w ``test_scenario_commands.py``.
"""

import pytest
from pydantic import ValidationError

from guidebot_recorder.models.config import Config, SelectsConfig, TtsConfig, Viewport
from guidebot_recorder.models.scenario import (
    Scenario,
    Select,
    Step,
    StepPathError,
    WhenBlock,
)


# Task 1: Select mode override tests
def test_select_accepts_mode_shim():
    sel = Select.model_validate({"from": "list", "option": "tabela", "mode": "shim"})
    assert sel.mode == "shim"


def test_select_accepts_mode_native():
    sel = Select.model_validate({"from": "list", "option": "tabela", "mode": "native"})
    assert sel.mode == "native"


def test_select_mode_defaults_to_none():
    sel = Select.model_validate({"from": "list", "option": "tabela"})
    assert sel.mode is None


def test_select_rejects_invalid_mode():
    with pytest.raises(ValidationError):
        Select.model_validate({"from": "list", "option": "tabela", "mode": "invalid"})


def test_select_mode_in_step():
    s = Step.model_validate(
        {"select": {"from": "list", "option": "tabela", "mode": "native"}, "say": "wybieram"}
    )
    assert s.select.mode == "native"


def _config(**kwargs) -> Config:
    return Config(
        title="t",
        viewport=Viewport(width=8, height=6),
        tts=TtsConfig(provider="edge", voice="v", lang="pl-PL"),
        **kwargs,
    )


# --- per-step `select.mode` against the global one ---------------------------


def _select_step(mode: str | None = None) -> Step:
    payload = {"from": "lista województw", "option": "Mazowieckie"}
    if mode is not None:
        payload["mode"] = mode
    return Step(select=Select(**payload))


def _step_path_error(excinfo) -> StepPathError:
    """The original validator exception pydantic carried through in ``ctx``."""

    origin = excinfo.value.errors()[0]["ctx"]["error"]
    assert isinstance(origin, StepPathError)
    return origin


def test_a_step_asking_for_shim_under_a_global_native_is_rejected_at_load():
    """`config.selects.mode: native` installs no widget, so no step can opt back in.

    There is nothing per-step about the global hatch: it decides whether the
    shim script is injected into the browser context at all. A step asking for
    `mode: shim` underneath it used to load fine and then fail mid-render, after
    the cursor had already clicked an unrelated element on camera. The author
    learns at load time instead.

    The step is named by the positional path on :class:`StepPathError`, not by a
    ``krok N:`` prefix baked into the text: this validator sits on ``Scenario``
    (``loc == ()``), so the path is the only thing the loader can turn into
    `plik:linia` plus the offending YAML fragment. Anything else would leave a
    `select:` step worse diagnosed than a `click:` step in the same file.
    """

    with pytest.raises(ValidationError) as excinfo:
        Scenario(
            config=_config(selects=SelectsConfig(mode="native")),
            steps=[_select_step("shim")],
        )

    assert _step_path_error(excinfo).path == (0,)  # says which step
    message = str(excinfo.value)
    assert "config.selects.mode: native" in message  # ...and which setting fights it
    # the step number belongs to the banner headline, never to the text
    assert "krok 0:" not in message


def test_the_step_index_of_a_rejected_shim_mode_points_into_its_branch():
    with pytest.raises(ValidationError) as excinfo:
        Scenario(
            config=_config(selects=SelectsConfig(mode="native")),
            steps=[
                Step(say="wstęp"),
                WhenBlock(when="baner", steps=[Step(say="x"), _select_step("shim")]),
            ],
        )

    assert _step_path_error(excinfo).path == (1, 1)
    assert "krok 1.1" not in str(excinfo.value)


def test_the_other_direction_and_every_inherited_mode_still_load():
    """Only the impossible combination is rejected — nothing else moves."""

    # native step under a global shim: the escape hatch this feature is for
    Scenario(config=_config(), steps=[_select_step("native")])
    # a redundant but harmless `mode: shim` under the default global shim
    Scenario(config=_config(), steps=[_select_step("shim")])
    # no per-step mode at all, under either global setting
    Scenario(config=_config(), steps=[_select_step()])
    Scenario(config=_config(selects=SelectsConfig(mode="native")), steps=[_select_step()])
