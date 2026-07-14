"""Tests for loading a source scenario YAML → Scenario (with ${ENV} expanded)."""

import pytest

from guidebot_recorder.models.scenario import Scenario
from guidebot_recorder.scenario.loader import load_scenario

SCENARIO_YAML = """\
config:
  title: "Logowanie do systemu"
  viewport: { width: 1280, height: 720 }
  locale: pl-PL
  tts: { provider: edge, voice: "pl-PL-Marek", lang: pl-PL }
steps:
  - say: "Witaj. Zaraz pokażę, jak zalogować się do systemu."
  - navigate: ${BASE_URL}
  - teach: "Aby się zalogować, kliknij przycisk Zaloguj"
  - enterText: { into: "pole email", text: "${DEMO_EMAIL}" }
    say: "Teraz wpisuję swój adres e-mail."
"""

ENV = {"BASE_URL": "https://app.example.com", "DEMO_EMAIL": "a@b.example"}


def _write(tmp_path, text=SCENARIO_YAML):
    p = tmp_path / "login.scenario.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_returns_scenario(tmp_path):
    scenario = load_scenario(_write(tmp_path), env=ENV)
    assert isinstance(scenario, Scenario)
    assert len(scenario.steps) == 4
    assert scenario.config.title == "Logowanie do systemu"


def test_env_substituted_in_value_fields(tmp_path):
    scenario = load_scenario(_write(tmp_path), env=ENV)
    assert scenario.steps[1].navigate == "https://app.example.com"
    assert scenario.steps[3].enter_text.text == "a@b.example"


def test_missing_env_raises(tmp_path):
    with pytest.raises(KeyError):
        load_scenario(_write(tmp_path), env={})


def test_env_defaults_to_os_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://env.example")
    monkeypatch.setenv("DEMO_EMAIL", "env@b.example")
    scenario = load_scenario(_write(tmp_path))  # env=None → os.environ
    assert scenario.steps[1].navigate == "https://env.example"
