"""Blok `selects:` — tryb shimowania listy rozwijanej i jego okna czasowe.

Granice liczbowe są tu niesymetryczne i to jest sedno pliku: `settle_ms`
dopuszcza `0` (świadomie — patrz docstring odpowiedniego testu), a
`max_visible_options` i `open_hold_ms` już nie. Z pól bloku tylko `mode`
wchodzi do `config_hash()`, bo zmienia to, co widać w kadrze; pozostałe trzy
to strojenie czasu i hasha nie ruszają.
"""

import pytest
from pydantic import ValidationError

from guidebot_recorder.models.config import (
    Config,
    config_hash,
)

from ._config_helpers import _cfg


# Task 1: SelectsConfig tests
def test_selects_config_defaults():
    from guidebot_recorder.models.config import SelectsConfig

    s = SelectsConfig()
    assert s.mode == "shim"
    assert s.settle_ms == 1000
    assert s.max_visible_options == 8
    assert s.open_hold_ms == 350


def test_selects_config_accepts_camelcase_and_snake_case_aliases():
    from guidebot_recorder.models.config import SelectsConfig

    s_camel = SelectsConfig.model_validate(
        {"mode": "native", "settleMs": 500, "maxVisibleOptions": 4, "openHoldMs": 200}
    )
    s_snake = SelectsConfig.model_validate(
        {"mode": "native", "settle_ms": 500, "max_visible_options": 4, "open_hold_ms": 200}
    )

    assert s_camel.mode == "native"
    assert s_camel.settle_ms == 500
    assert s_camel.max_visible_options == 4
    assert s_camel.open_hold_ms == 200
    assert s_snake.mode == "native"
    assert s_snake.settle_ms == 500
    assert s_snake.max_visible_options == 4
    assert s_snake.open_hold_ms == 200


def test_selects_config_rejects_unknown_fields():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig.model_validate({"mode": "shim", "unknown": "value"})


def test_selects_config_rejects_invalid_mode():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig(mode="invalid")  # type: ignore


def test_selects_config_rejects_negative_settle_ms():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig(settle_ms=-1)


def test_selects_config_accepts_zero_settle_ms():
    """`0` means "no settle window", which is a legitimate thing to ask for.

    The floor used to be 1 ms, so the one page shape the window exists to
    accommodate — a site with no widget library at all, where waiting only
    delays every classification pass — had no way to switch it off. The widget
    already clamps at zero and simply schedules the pass on the next task.
    """

    from guidebot_recorder.models.config import SelectsConfig

    assert SelectsConfig(settle_ms=0).settle_ms == 0


def test_selects_config_rejects_max_visible_options_zero():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig(max_visible_options=0)


def test_selects_config_rejects_negative_max_visible_options():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig(max_visible_options=-1)


def test_selects_config_rejects_negative_open_hold_ms():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig(open_hold_ms=-1)


def test_selects_config_rejects_zero_open_hold_ms():
    from guidebot_recorder.models.config import SelectsConfig

    with pytest.raises(ValidationError):
        SelectsConfig(open_hold_ms=0)


def test_config_defaults_selects_to_built_in():
    cfg = _cfg()

    from guidebot_recorder.models.config import SelectsConfig

    assert cfg.selects == SelectsConfig()
    assert cfg.selects.mode == "shim"
    assert cfg.selects.settle_ms == 1000


def test_selects_config_from_config_yaml():
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 1280, "height": 720},
            "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
            "selects": {"mode": "native", "settleMs": 500, "maxVisibleOptions": 5},
        }
    )

    assert cfg.selects.mode == "native"
    assert cfg.selects.settle_ms == 500
    assert cfg.selects.max_visible_options == 5
    assert cfg.selects.open_hold_ms == 350  # default


def test_config_hash_stable_with_default_selects():
    """Default selects must not change the hash to keep legacy scenarios stable."""
    from guidebot_recorder.models.config import SelectsConfig

    baseline = _cfg()
    with_default_selects = _cfg()
    with_default_selects.selects = SelectsConfig()

    assert config_hash(baseline) == config_hash(with_default_selects)


def test_config_hash_changes_when_selects_mode_is_native():
    """Changing mode from default shim to native must invalidate the hash."""
    from guidebot_recorder.models.config import SelectsConfig

    default_mode = _cfg()
    native_mode = _cfg()
    native_mode.selects = SelectsConfig(mode="native")

    assert config_hash(default_mode) != config_hash(native_mode)


def test_config_hash_unchanged_when_selects_settle_ms_changes():
    """Changing settle_ms must not change the hash (like other cosmetic fields)."""
    from guidebot_recorder.models.config import SelectsConfig

    baseline = _cfg()
    tweaked = _cfg()
    tweaked.selects = SelectsConfig(settle_ms=500)

    assert config_hash(baseline) == config_hash(tweaked)


def test_config_hash_unchanged_when_selects_max_visible_options_changes():
    """Changing max_visible_options must not change the hash (like other cosmetic fields)."""
    from guidebot_recorder.models.config import SelectsConfig

    baseline = _cfg()
    tweaked = _cfg()
    tweaked.selects = SelectsConfig(max_visible_options=4)

    assert config_hash(baseline) == config_hash(tweaked)


def test_config_hash_unchanged_when_selects_open_hold_ms_changes():
    """Changing open_hold_ms must not change the hash (like other cosmetic fields)."""
    from guidebot_recorder.models.config import SelectsConfig

    baseline = _cfg()
    tweaked = _cfg()
    tweaked.selects = SelectsConfig(open_hold_ms=200)

    assert config_hash(baseline) == config_hash(tweaked)
