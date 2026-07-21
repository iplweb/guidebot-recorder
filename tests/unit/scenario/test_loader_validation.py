"""Mapowanie błędów pydantica na `plik:linia` + fragment YAML.

Sedno tych testów to **filtr wariantów unii**: `Scenario.steps` to
`list[Step | WhenBlock]`, więc jeden błąd użytkownika daje kilka wpisów
w `exc.errors()` — reszta to śmieci z odrzuconego wariantu. Użytkownik ma
zobaczyć jeden banner, ten właściwy.
"""

from __future__ import annotations

import textwrap

import pytest

from guidebot_recorder.scenario.loader import ScenarioValidationError, load_scenario

ENV: dict[str, str] = {}


def _write(tmp_path, text):
    path = tmp_path / "flow.scenario.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _banners(text: str) -> int:
    """Ile bannerów skleiło się w komunikacie."""

    return text.count("BŁĄD walidacji")


def _error(tmp_path, text, env=None):
    path = _write(tmp_path, text)
    with pytest.raises(ScenarioValidationError) as excinfo:
        load_scenario(path, env=ENV if env is None else env)
    return str(excinfo.value), path


HEAD = """\
    config:
      title: t
      viewport: {width: 1, height: 1}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - say: "Pierwszy."
"""


def test_two_commands_in_a_step_give_exactly_one_banner(tmp_path):
    """Zmierzone: pydantic daje tu 5 błędów, z czego 4 to śmieci z `WhenBlock`."""

    message, path = _error(
        tmp_path,
        HEAD
        + """\
      - click: "Zapisz"
        navigate: "https://example.test"
""",
    )

    assert _banners(message) == 1
    assert f"{path}:7" in message
    assert "krok 2/2" in message
    assert 'click: "Zapisz"' in message
    assert "dozwolona dokładnie jedna" in message
    # śmieci z odrzuconego wariantu unii nie mają prawa się pokazać
    assert "Extra inputs" not in message
    assert "WhenBlock" not in message


def test_nested_when_block_gives_exactly_one_banner(tmp_path):
    """Zmierzone: 3 błędy, z czego 2 to śmieci z wariantu `Step`."""

    message, path = _error(
        tmp_path,
        HEAD
        + """\
      - when: "baner zgody"
        steps:
          - when: "zagnieżdżony"
            steps:
              - click: "Y"
""",
    )

    assert _banners(message) == 1
    assert f"{path}:7" in message
    assert "zagnieżdżony blok `when` nie jest wspierany" in message
    assert "Extra inputs" not in message


def test_missing_field_points_at_the_key_that_owns_it(tmp_path):
    """Brakujący klucz nie istnieje w źródle — celujemy w linię rodzica."""

    message, path = _error(
        tmp_path,
        HEAD
        + """\
      - enterText: {into: "pole email"}
""",
    )

    assert _banners(message) == 1
    assert f"{path}:7" in message
    assert "enterText.text" in message
    assert "Field required" in message


def test_step_validator_reports_the_offending_line(tmp_path):
    message, path = _error(
        tmp_path,
        HEAD
        + """\
      - say: "Drugi."
        optional: true
""",
    )

    assert _banners(message) == 1
    assert f"{path}:7" in message
    assert "krok 2/2" in message
    assert "`optional: true` nie ma zastosowania" in message


def test_scenario_validator_reaches_a_child_of_a_when_block(tmp_path):
    """`_complete_audio_translations` ma `loc == ()`; linię daje `StepPathError.path`."""

    message, path = _error(
        tmp_path,
        """\
    config:
      title: t
      viewport: {width: 1, height: 1}
      tts: {provider: edge, voice: v, lang: pl-PL, trackLanguage: pol}
      audioTracks:
        - {provider: edge, voice: en, lang: en-US, trackLanguage: eng}
    steps:
      - say: "Pierwszy."
        translations: {en-US: "First."}
      - when: "baner zgody"
        steps:
          - teach: "Kliknij Akceptuję."
""",
    )

    assert _banners(message) == 1
    # linia dziecka bloku (`teach:`), nie linia bloku `when:`
    assert f"{path}:12" in message
    assert "krok 3/3" in message
    assert "brak tłumaczeń dla ścieżek: en-US" in message
    # numer kroku przeniósł się do nagłówka — z treści zniknął
    assert "krok 1.0:" not in message


def test_scenario_validator_on_a_top_level_step(tmp_path):
    message, path = _error(
        tmp_path,
        """\
    config:
      title: t
      viewport: {width: 1, height: 1}
      tts: {provider: edge, voice: v, lang: pl-PL, trackLanguage: pol}
      audioTracks:
        - {provider: edge, voice: en, lang: en-US, trackLanguage: eng}
    steps:
      - say: "Pierwszy."
        translations: {en-US: "First."}
      - say: "Drugi."
""",
    )

    assert _banners(message) == 1
    assert f"{path}:10" in message
    assert "krok 2/2" in message


def test_config_error_has_no_step_member(tmp_path):
    """Linia w `config:` nie należy do żadnego kroku — banner bez `(krok n/total)`."""

    message, path = _error(
        tmp_path,
        """\
    config:
      title: t
      viewport: {width: 1}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - say: "Pierwszy."
""",
    )

    assert _banners(message) == 1
    assert f"{path}:3" in message
    assert "(krok " not in message
    assert "viewport: {width: 1}" in message
    assert "config.viewport.height" in message


def test_falls_back_to_a_bare_headline_without_a_location(tmp_path):
    """Brak `steps:` w ogóle — nie ma czego wskazać, zostaje sama treść."""

    message, path = _error(
        tmp_path,
        """\
    config:
      title: t
      viewport: {width: 1, height: 1}
      tts: {provider: edge, voice: v, lang: pl-PL}
    """,
    )

    assert _banners(message) == 1
    assert str(path) in message
    assert f"{path}:" not in message
    assert "steps" in message


def test_snippet_never_leaks_a_substituted_secret(tmp_path):
    """Snippet pochodzi z tekstu sprzed podstawienia `${ENV}`."""

    message, _path = _error(
        tmp_path,
        HEAD
        + """\
      - enterText: {into: "hasło", text: "${SECRET}"}
        click: "Zaloguj"
""",
        env={"SECRET": "hunter2"},
    )

    assert "hunter2" not in message
    assert "${SECRET}" in message


def test_validation_error_stays_a_value_error():
    """Istniejące `pytest.raises(ValueError, ...)` w testach loadera mają przechodzić."""

    assert issubclass(ScenarioValidationError, ValueError)
