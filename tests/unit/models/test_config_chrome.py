"""Blok `chrome:` — domyślne wartości, aliasy YAML i udział w `config_hash()`.

Podział pól przebiega tu wzdłuż jednej linii: do hasha wchodzą tylko `enabled`
i `height` (geometria zmienia kadr, więc unieważnia skompilowany sidecar),
a pola kosmetyczne i parametry pisania po pasku adresu — nie. Testy hasha
zostają razem z testami parsowania, bo jedne i drugie pilnują tego samego
podziału z dwóch stron.
"""

import pytest
from pydantic import ValidationError

from guidebot_recorder.models.config import (
    ChromeConfig,
    Config,
    config_hash,
)

from ._config_helpers import _cfg


def test_chrome_defaults_to_disabled_without_changing_legacy_config():
    cfg = _cfg()

    assert cfg.chrome == ChromeConfig()
    assert cfg.chrome.enabled is False
    assert cfg.chrome.show_url is True
    assert cfg.chrome.type_on_navigate is True


def test_chrome_config_accepts_yaml_aliases_and_cosmetic_fields():
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1280, "height": 720},
            "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
            "chrome": {
                "enabled": True,
                "showUrl": False,
                "typeOnNavigate": False,
                "height": 64,
                "barColor": "#101010",
                "textColor": "#fefefe",
                "radius": 16,
                "showLock": False,
                "closeColor": "red",
                "minimizeColor": "yellow",
                "maximizeColor": "green",
            },
        }
    )

    assert cfg.chrome == ChromeConfig(
        enabled=True,
        show_url=False,
        type_on_navigate=False,
        height=64,
        bar_color="#101010",
        text_color="#fefefe",
        radius=16,
        show_lock=False,
        close_color="red",
        minimize_color="yellow",
        maximize_color="green",
    )


def test_chrome_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ChromeConfig.model_validate({"enabled": True, "unknown": "value"})


def test_cosmetic_chrome_fields_are_excluded_from_config_hash():
    # Same enabled/height (the only two geometry fields that reach the hash),
    # everything else cosmetic/typing — the hash must stay put.
    baseline = _cfg(chrome=ChromeConfig(enabled=True, height=56))
    customized = _cfg(
        chrome=ChromeConfig(
            enabled=True,
            height=56,
            show_url=False,
            type_on_navigate=False,
            bar_color="#000",
            text_color="#fff",
            radius=20,
            show_lock=False,
            close_color="#100",
            minimize_color="#110",
            maximize_color="#010",
            interact_on_navigate=False,
            char_delay_ms=1,
            char_jitter_ms=1,
            segment_pause_ms=1,
            pre_navigate_pause_ms=1,
            focus_color="#000000",
            show_caret=False,
        )
    )

    assert config_hash(customized) == config_hash(baseline)


def test_new_chrome_typing_fields_defaults():
    chrome = ChromeConfig()

    assert chrome.interact_on_navigate is True
    assert chrome.char_delay_ms == 60
    assert chrome.char_jitter_ms == 55
    assert chrome.segment_pause_ms == 180
    assert chrome.pre_navigate_pause_ms == 400
    assert chrome.focus_color == "#3b82f6"
    assert chrome.show_caret is True


def test_new_chrome_typing_fields_parse_from_camelcase_aliases():
    chrome = ChromeConfig.model_validate(
        {
            "interactOnNavigate": False,
            "charDelayMs": 42,
            "charJitterMs": 7,
            "segmentPauseMs": 90,
            "preNavigatePauseMs": 250,
            "focusColor": "#abcdef",
            "showCaret": False,
        }
    )

    assert chrome.interact_on_navigate is False
    assert chrome.char_delay_ms == 42
    assert chrome.char_jitter_ms == 7
    assert chrome.segment_pause_ms == 90
    assert chrome.pre_navigate_pause_ms == 250
    assert chrome.focus_color == "#abcdef"
    assert chrome.show_caret is False


def test_new_chrome_typing_fields_parse_from_snake_case():
    chrome = ChromeConfig.model_validate(
        {
            "interact_on_navigate": False,
            "char_delay_ms": 42,
            "char_jitter_ms": 7,
            "segment_pause_ms": 90,
            "pre_navigate_pause_ms": 250,
            "focus_color": "#abcdef",
            "show_caret": False,
        }
    )

    assert chrome.interact_on_navigate is False
    assert chrome.char_delay_ms == 42
    assert chrome.char_jitter_ms == 7
    assert chrome.segment_pause_ms == 90
    assert chrome.pre_navigate_pause_ms == 250
    assert chrome.focus_color == "#abcdef"
    assert chrome.show_caret is False


def test_config_hash_changes_when_chrome_enabled_flips():
    disabled = _cfg(chrome=ChromeConfig(enabled=False))
    enabled = _cfg(chrome=ChromeConfig(enabled=True))

    assert config_hash(enabled) != config_hash(disabled)


def test_config_hash_changes_when_chrome_height_changes():
    short = _cfg(chrome=ChromeConfig(enabled=True, height=56))
    tall = _cfg(chrome=ChromeConfig(enabled=True, height=72))

    assert config_hash(short) != config_hash(tall)


def test_config_hash_unchanged_when_cosmetic_chrome_field_changes():
    baseline = _cfg(chrome=ChromeConfig(enabled=True))
    recolored = _cfg(chrome=ChromeConfig(enabled=True, bar_color="#123456"))

    assert config_hash(recolored) == config_hash(baseline)


def test_config_hash_unchanged_when_typing_chrome_field_changes():
    baseline = _cfg(chrome=ChromeConfig(enabled=True))
    faster = _cfg(chrome=ChromeConfig(enabled=True, char_delay_ms=10))

    assert config_hash(faster) == config_hash(baseline)
