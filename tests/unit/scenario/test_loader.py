"""Testy wczytania YAML → Scenario + zachowanego round-trip doc (Task 7)."""

import pytest
from ruamel.yaml.comments import CommentedMap

from guidebot_recorder.models.scenario import Scenario
from guidebot_recorder.scenario.loader import LoadedScenario, load_scenario

# Przykład wg §3.2 (przed compile — bez cachedAction), 4 kroki.
SCENARIO_YAML = """\
config:
  title: "Logowanie do systemu"
  viewport: { width: 1280, height: 720 }
  locale: pl-PL
  tts: { provider: edge, voice: "pl-PL-Marek", lang: pl-PL }
steps:
  - say: "Witaj. Zaraz pokażę, jak zalogować się do systemu."
  - navigate: ${BASE_URL}
  - teach: "Aby się zalogować, kliknij przycisk Zaloguj"   # ważny komentarz
  - enterText: { into: "pole email", text: "${DEMO_EMAIL}" }
    say: "Teraz wpisuję swój adres e-mail."
"""


def _write(tmp_path, text=SCENARIO_YAML):
    p = tmp_path / "login.scenario.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_returns_scenario_and_doc(tmp_path):
    p = _write(tmp_path)
    loaded = load_scenario(
        p, env={"BASE_URL": "https://app.example.com", "DEMO_EMAIL": "a@b.example"}
    )
    assert isinstance(loaded, LoadedScenario)
    assert isinstance(loaded.scenario, Scenario)
    assert isinstance(loaded.doc, CommentedMap)


def test_scenario_has_four_steps(tmp_path):
    p = _write(tmp_path)
    loaded = load_scenario(
        p, env={"BASE_URL": "https://app.example.com", "DEMO_EMAIL": "a@b.example"}
    )
    assert len(loaded.scenario.steps) == 4
    assert loaded.scenario.config.title == "Logowanie do systemu"


def test_env_substituted_in_scenario_only(tmp_path):
    p = _write(tmp_path)
    loaded = load_scenario(
        p, env={"BASE_URL": "https://app.example.com", "DEMO_EMAIL": "a@b.example"}
    )
    # w Scenario: podstawione
    assert loaded.scenario.steps[1].navigate == "https://app.example.com"
    assert loaded.scenario.steps[3].enter_text.text == "a@b.example"
    # w doc: NIE tknięte (sekrety nie trafiają do zapisu round-trip)
    assert loaded.doc["steps"][1]["navigate"] == "${BASE_URL}"
    assert loaded.doc["steps"][3]["enterText"]["text"] == "${DEMO_EMAIL}"


def test_doc_preserves_comment(tmp_path):
    p = _write(tmp_path)
    loaded = load_scenario(
        p, env={"BASE_URL": "https://app.example.com", "DEMO_EMAIL": "a@b.example"}
    )
    import io

    from ruamel.yaml import YAML

    buf = io.StringIO()
    YAML(typ="rt").dump(loaded.doc, buf)
    assert "# ważny komentarz" in buf.getvalue()


def test_missing_env_raises(tmp_path):
    p = _write(tmp_path)
    with pytest.raises(KeyError):
        load_scenario(p, env={})  # brak BASE_URL/DEMO_EMAIL


def test_env_defaults_to_os_environ(tmp_path, monkeypatch):
    p = _write(tmp_path)
    monkeypatch.setenv("BASE_URL", "https://env.example")
    monkeypatch.setenv("DEMO_EMAIL", "env@b.example")
    loaded = load_scenario(p)  # env=None → os.environ
    assert loaded.scenario.steps[1].navigate == "https://env.example"
