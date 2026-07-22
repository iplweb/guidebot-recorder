"""Blok `popup:` — geometria, przejścia i pochodne `effective_transition`/`is_bare`.

Cały popup jest „tylko do renderu": żadne z jego pól nie wchodzi do
`config_hash()`, co pilnują trzy testy na końcu pliku. Reszta to parsowanie
(camelCase i snake_case) oraz dwie własności wyliczane z pary
`floating` × `transition` — starszego przełącznika i nowszego, jawnego trybu,
gdzie jawny zawsze wygrywa.
"""

import pytest
from pydantic import ValidationError

from guidebot_recorder.models.config import (
    Config,
    PopupConfig,
    config_hash,
)

from ._config_helpers import _cfg


def test_popup_defaults():
    popup = PopupConfig()

    assert popup.floating is True
    assert popup.scale == 0.85
    assert popup.corner_radius == 14
    assert popup.shadow is True
    assert popup.backdrop_dim == 0.45
    assert popup.backdrop_blur == 0
    assert popup.open_ms == 320
    assert popup.close_ms == 240


def test_config_defaults_popup_to_built_in():
    cfg = _cfg()

    assert cfg.popup == PopupConfig()
    assert cfg.popup.floating is True


def test_popup_config_parses_from_camelcase_aliases():
    popup = PopupConfig.model_validate(
        {
            "floating": False,
            "scale": 0.5,
            "cornerRadius": 20,
            "shadow": False,
            "backdropDim": 0.6,
            "backdropBlur": 8,
            "openMs": 500,
            "closeMs": 300,
        }
    )

    assert popup.floating is False
    assert popup.scale == 0.5
    assert popup.corner_radius == 20
    assert popup.shadow is False
    assert popup.backdrop_dim == 0.6
    assert popup.backdrop_blur == 8
    assert popup.open_ms == 500
    assert popup.close_ms == 300


def test_popup_config_parses_from_snake_case():
    popup = PopupConfig.model_validate(
        {
            "floating": False,
            "scale": 0.5,
            "corner_radius": 20,
            "shadow": False,
            "backdrop_dim": 0.6,
            "backdrop_blur": 8,
            "open_ms": 500,
            "close_ms": 300,
        }
    )

    assert popup.floating is False
    assert popup.scale == 0.5
    assert popup.corner_radius == 20
    assert popup.shadow is False
    assert popup.backdrop_dim == 0.6
    assert popup.backdrop_blur == 8
    assert popup.open_ms == 500
    assert popup.close_ms == 300


def test_popup_config_from_config_yaml_alias():
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1280, "height": 720},
            "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
            "popup": {"cornerRadius": 20, "backdropDim": 0.6, "openMs": 500},
        }
    )

    assert cfg.popup.corner_radius == 20
    assert cfg.popup.backdrop_dim == 0.6
    assert cfg.popup.open_ms == 500


def test_popup_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        PopupConfig.model_validate({"floating": True, "unknown": "value"})


def test_config_rejects_unknown_popup_fields():
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "title": "t",
                "viewport": {"width": 1280, "height": 720},
                "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
                "popup": {"unknown": "value"},
            }
        )


def test_config_hash_unchanged_when_any_popup_field_changes():
    baseline = _cfg(popup=PopupConfig())
    customized = _cfg(
        popup=PopupConfig(
            floating=False,
            scale=0.5,
            corner_radius=20,
            shadow=False,
            backdrop_dim=0.6,
            backdrop_blur=8,
            open_ms=500,
            close_ms=300,
        )
    )

    assert config_hash(customized) == config_hash(baseline)


def test_popup_transition_defaults():
    popup = PopupConfig()

    assert popup.transition is None
    assert popup.slide_ms == 400


@pytest.mark.parametrize("mode", ["cut", "float", "slide"])
def test_popup_transition_parses_each_literal(mode):
    popup = PopupConfig.model_validate({"transition": mode})

    assert popup.transition == mode


def test_popup_slide_ms_parses_from_camelcase_alias():
    popup = PopupConfig.model_validate({"slideMs": 900})

    assert popup.slide_ms == 900


def test_popup_slide_ms_parses_from_snake_case():
    popup = PopupConfig.model_validate({"slide_ms": 900})

    assert popup.slide_ms == 900


def test_popup_rejects_invalid_transition():
    with pytest.raises(ValidationError):
        PopupConfig.model_validate({"transition": "fade"})


def test_popup_still_forbids_unknown_fields_with_transition():
    with pytest.raises(ValidationError):
        PopupConfig.model_validate({"transition": "slide", "unknown": "value"})


def test_effective_transition_derives_from_floating_when_unset():
    assert PopupConfig(floating=True).effective_transition == "float"
    assert PopupConfig(floating=False).effective_transition == "cut"


@pytest.mark.parametrize(
    ("floating", "transition", "expected"),
    [
        (False, "float", "float"),
        (True, "cut", "cut"),
        (True, "slide", "slide"),
        (False, "slide", "slide"),
        (True, "float", "float"),
        (False, "cut", "cut"),
    ],
)
def test_effective_transition_explicit_always_wins(floating, transition, expected):
    popup = PopupConfig(floating=floating, transition=transition)

    assert popup.effective_transition == expected


@pytest.mark.parametrize(
    ("floating", "transition", "expected"),
    [
        # driven via floating (transition unset)
        (True, None, True),
        (False, None, False),
        # driven via explicit transition
        (False, "float", True),
        (False, "slide", True),
        (True, "cut", False),
    ],
)
def test_is_bare_matrix(floating, transition, expected):
    popup = PopupConfig(floating=floating, transition=transition)

    assert popup.is_bare is expected


def test_config_hash_unchanged_when_transition_changes():
    baseline = _cfg(popup=PopupConfig(transition="cut"))
    changed = _cfg(popup=PopupConfig(transition="slide"))

    assert config_hash(changed) == config_hash(baseline)


def test_config_hash_unchanged_when_slide_ms_changes():
    baseline = _cfg(popup=PopupConfig(slide_ms=400))
    changed = _cfg(popup=PopupConfig(slide_ms=900))

    assert config_hash(changed) == config_hash(baseline)
