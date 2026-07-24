"""`compile` step-description helpers: `_short` and `_target_desc`.

(The `_short` case for `closeWindow` lives with the other closeWindow tests in
``test_compile_closewindow.py``.)
"""

import guidebot_recorder.recorder.compile as compile_module
from guidebot_recorder.models.scenario import Step
from guidebot_recorder.models.target import RoleTarget, TextTarget
from guidebot_recorder.recorder.compile import _short


def test_compile_short_description_uses_object_navigate_url():
    step = Step.model_validate({"navigate": {"url": "https://example.com/login", "type": True}})

    assert _short(step) == "https://example.com/login"


def test_compile_short_description_uses_slide_title():
    step = Step.model_validate({"slide": {"title": "Krok 1", "subtitle": "Kliknij przycisk"}})

    assert _short(step) == "Krok 1"


def test_compile_short_description_falls_back_to_slide_subtitle():
    step = Step.model_validate({"slide": {"subtitle": "Kliknij przycisk"}})

    assert _short(step) == "Kliknij przycisk"


def test_compile_short_description_for_highlight():
    step = Step.model_validate({"highlight": "tabela z wynikami"})

    assert _short(step) == "◯ tabela z wynikami"


def test_target_desc_shows_nth_and_scope():
    """`--verbose` bez `nth` i `scope` ukrywałby dokładnie to, co dodajemy."""

    target = RoleTarget(
        role="button",
        name="×",
        nth=2,
        scope=TextTarget(text="Charakter formalny"),
    )

    desc = compile_module._target_desc(target)

    assert "nth=2" in desc
    assert "Charakter formalny" in desc
    assert compile_module._target_desc(RoleTarget(role="button", name="Zaloguj")) == (
        'role=button name="Zaloguj"'
    )
