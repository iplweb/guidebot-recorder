"""Phase A1: pre-recording setup schema fields on Config.

Covers the ``setup`` / ``verifyUserLoggedIn`` / ``maxAgeHours`` fields plus the
config-hash tweak that folds ``setup`` into the projection only when set.
"""

import pytest
from pydantic import ValidationError

from guidebot_recorder.models.config import (
    Config,
    TtsConfig,
    VerifyLoggedIn,
    Viewport,
    config_hash,
)

# Pre-existing hash of a minimal config (800x600, tts.lang=pl-PL, no locale),
# captured from the CURRENT code before adding setup. Adding the new fields with
# `setup` unset MUST keep this hash intact so existing scenarios don't recompile.
GOLDEN_MINIMAL_HASH = "969da785a7020c0b5a95b258c906bf3755ab7559dda483d8700b4753ac83c488"


def _cfg(**overrides):
    base = {
        "title": "t",
        "viewport": Viewport(width=800, height=600),
        "tts": TtsConfig(provider="edge", voice="v", lang="pl-PL"),
    }
    base.update(overrides)
    return Config(**base)


# --- verifyUserLoggedIn: string shorthand vs object form -------------------


def test_verify_user_logged_in_string_shorthand_wraps_into_object():
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 800, "height": 600},
            "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
            "verifyUserLoggedIn": "Wyloguj",
        }
    )

    assert cfg.verify_user_logged_in == VerifyLoggedIn(contains_text="Wyloguj")
    assert cfg.verify_user_logged_in.contains_text == "Wyloguj"
    assert cfg.verify_user_logged_in.url is None
    assert cfg.verify_user_logged_in.timeout == 8


def test_verify_user_logged_in_object_form():
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 800, "height": 600},
            "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
            "verifyUserLoggedIn": {"containsText": "X", "url": "/d", "timeout": 3},
        }
    )

    assert cfg.verify_user_logged_in == VerifyLoggedIn(contains_text="X", url="/d", timeout=3)


def test_verify_user_logged_in_defaults_to_none():
    assert _cfg().verify_user_logged_in is None


def test_verify_logged_in_rejects_unknown_key():
    with pytest.raises(ValidationError):
        VerifyLoggedIn.model_validate({"containsText": "X", "bogus": 1})


def test_verify_user_logged_in_object_rejects_unknown_key_through_config():
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "title": "t",
                "viewport": {"width": 800, "height": 600},
                "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
                "verifyUserLoggedIn": {"containsText": "X", "bogus": 1},
            }
        )


def test_verify_logged_in_requires_contains_text():
    with pytest.raises(ValidationError):
        VerifyLoggedIn.model_validate({"url": "/d"})


# --- setup / maxAgeHours round-trip via alias -----------------------------


def test_setup_and_max_age_hours_round_trip_via_alias():
    cfg = Config.model_validate(
        {
            "title": "t",
            "viewport": {"width": 800, "height": 600},
            "tts": {"provider": "edge", "voice": "v", "lang": "pl-PL"},
            "setup": "setups/login.yaml",
            "maxAgeHours": 12.5,
        }
    )

    assert cfg.setup == "setups/login.yaml"
    assert cfg.max_age_hours == 12.5


def test_setup_and_max_age_hours_default_to_none():
    cfg = _cfg()

    assert cfg.setup is None
    assert cfg.max_age_hours is None


# --- config_hash: backward compatibility & setup sensitivity ---------------


def test_config_hash_of_minimal_config_matches_pre_change_golden():
    assert config_hash(_cfg()) == GOLDEN_MINIMAL_HASH


def test_config_hash_unchanged_when_setup_unset():
    baseline = _cfg()
    with_defaults = _cfg(verify_user_logged_in=None, max_age_hours=None)

    assert config_hash(with_defaults) == config_hash(baseline)


def test_config_hash_changes_when_setup_is_set():
    baseline = _cfg()
    with_setup = _cfg(setup="setups/login.yaml")

    assert config_hash(with_setup) != config_hash(baseline)


def test_config_hash_changes_with_setup_value():
    a = _cfg(setup="setups/a.yaml")
    b = _cfg(setup="setups/b.yaml")

    assert config_hash(a) != config_hash(b)


def test_config_hash_unchanged_when_only_verify_or_max_age_differ():
    baseline = _cfg()
    with_verify = _cfg(
        verify_user_logged_in=VerifyLoggedIn(contains_text="Wyloguj"),
        max_age_hours=48.0,
    )

    assert config_hash(with_verify) == config_hash(baseline)
