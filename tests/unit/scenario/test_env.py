"""Testy substytucji `${ENV_VAR}` (Task 6, §3.2)."""

import pytest

from guidebot_recorder.scenario.env import (
    substitute_env,
    substitute_scenario_values,
)


def test_basic():
    assert substitute_env("${A}/x", {"A": "1"}) == "1/x"


def test_multiple_vars():
    assert substitute_env("${A}-${B}", {"A": "1", "B": "2"}) == "1-2"


def test_no_var_passthrough():
    assert substitute_env("plain text", {}) == "plain text"


def test_escape():
    assert substitute_env("$${A}", {}) == "${A}"


def test_escape_does_not_consume_following():
    # `$${A}` musi dać literalne `${A}`, a nie odczytać zmiennej A
    assert substitute_env("$${A}", {"A": "1"}) == "${A}"


def test_escape_next_to_real():
    assert substitute_env("$${A} ${A}", {"A": "1"}) == "${A} 1"


def test_missing_raises():
    with pytest.raises(KeyError):
        substitute_env("${NOPE}", {})


# --- substitute_scenario_values: TYLKO enterText.text i navigate -------------


def test_scenario_values_navigate_and_entertext():
    raw = {
        "config": {"title": "t"},
        "steps": [
            {"navigate": "${BASE}/login"},
            {"enterText": {"into": "pole email", "text": "${EMAIL}"}},
        ],
    }
    out = substitute_scenario_values(raw, {"BASE": "http://x", "EMAIL": "a@b"})
    assert out["steps"][0]["navigate"] == "http://x/login"
    assert out["steps"][1]["enterText"]["text"] == "a@b"


def test_scenario_values_leaves_narration_untouched():
    # say / teach / translations / enterText.into / wait.until NIE są substytuowane
    raw = {
        "steps": [
            {"say": "koszt ${SECRET}"},
            {
                "teach": "kliknij ${SECRET}",
                "translations": {"en-US": "click ${SECRET}"},
            },
            {"enterText": {"into": "${SECRET}", "text": "x"}},
            {"wait": {"until": "aż ${SECRET}", "state": "visible"}},
        ],
    }
    out = substitute_scenario_values(raw, {"SECRET": "TOP"})
    assert out["steps"][0]["say"] == "koszt ${SECRET}"
    assert out["steps"][1]["teach"] == "kliknij ${SECRET}"
    assert out["steps"][1]["translations"]["en-US"] == "click ${SECRET}"
    assert out["steps"][2]["enterText"]["into"] == "${SECRET}"
    assert out["steps"][3]["wait"]["until"] == "aż ${SECRET}"


def test_scenario_values_does_not_mutate_input():
    raw = {"steps": [{"navigate": "${BASE}"}]}
    substitute_scenario_values(raw, {"BASE": "http://x"})
    assert raw["steps"][0]["navigate"] == "${BASE}"


def test_scenario_values_missing_env_raises():
    raw = {"steps": [{"navigate": "${NOPE}"}]}
    with pytest.raises(KeyError):
        substitute_scenario_values(raw, {})


def test_scenario_values_substitutes_object_navigate_url_only():
    raw = {
        "steps": [
            {"navigate": {"url": "${BASE}/login", "type": False}},
        ]
    }

    out = substitute_scenario_values(raw, {"BASE": "https://example.com"})

    assert out["steps"][0]["navigate"] == {
        "url": "https://example.com/login",
        "type": False,
    }
    assert raw["steps"][0]["navigate"]["url"] == "${BASE}/login"


def test_scenario_values_object_navigate_missing_env_raises():
    raw = {"steps": [{"navigate": {"url": "${NOPE}", "type": True}}]}

    with pytest.raises(KeyError):
        substitute_scenario_values(raw, {})
