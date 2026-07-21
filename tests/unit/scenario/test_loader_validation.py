"""Mapowanie błędów pydantica na `plik:linia` + fragment YAML.

Sedno tych testów to **filtr wariantów unii**: `Scenario.steps` to
`list[Step | WhenBlock]`, więc jeden błąd użytkownika daje kilka wpisów
w `exc.errors()` — reszta to śmieci z odrzuconego wariantu. Użytkownik ma
zobaczyć jeden banner, ten właściwy.
"""

from __future__ import annotations

import textwrap

import pytest

from guidebot_recorder.scenario.loader import (
    ScenarioValidationError,
    format_validation_error,
    load_scenario,
)
from guidebot_recorder.scenario.source import build_source

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
    """Jeden banner — i jedna, niesprzeczna numeracja kroku.

    Nagłówek dawał `krok 1/2` (płaski, 1-based, linia **rodzica**), a treść
    doklejała własne `krok 0:` (0-based, lokalne dla bloku). Wskazanie ma iść na
    dziecko, które zagnieżdża, a numer kroku ma być tylko jeden.
    """

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
    # linia zagnieżdżonego `when:`, nie linia bloku-rodzica (7)
    assert f"{path}:9" in message
    assert f"{path}:7" not in message
    assert "krok 3/3" in message  # bramka + dziecko + `say` z HEAD
    assert "zagnieżdżony blok `when` nie jest wspierany" in message
    # treść nie dokłada konkurencyjnego numeru — w całym bannerze jest jeden
    assert "krok 0" not in message
    assert message.count("krok ") == 1
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


def test_empty_file_gives_a_banner_instead_of_an_attribute_error(tmp_path):
    """Pusty plik: ``AttributeError: 'NoneType' object has no attribute 'get'``.

    Leciał z ``substitute_scenario_values`` — czyli *przed* diagnostyką, więc
    użytkownik nie dostawał nawet nazwy pliku.
    """

    message, path = _error(tmp_path, "")

    assert _banners(message) == 1
    assert str(path) in message
    assert "NoneType" not in message
    assert "mapy" in message


def test_non_mapping_document_gives_a_banner(tmp_path):
    """Lista na najwyższym poziomie to też nie scenariusz."""

    message, path = _error(
        tmp_path,
        """\
    - say: "Pierwszy."
""",
    )

    assert _banners(message) == 1
    assert str(path) in message
    assert "AttributeError" not in message


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


def test_fallback_banner_never_dumps_the_raw_pydantic_message(tmp_path):
    """Awaryjna gałąź nie ma prawa nieść `str(exc)`.

    Pydantikowe `str(exc)` zawiera `input_value=…`, czyli wartość **po**
    podstawieniu `${ENV}` — czyli dokładnie ten sekret, którego snippet
    diagnostyki starannie unika.
    """

    class _NoRelevantErrors(ValueError):
        def __str__(self) -> str:
            return "1 validation error for Scenario\n  input_value='hunter2', input_type=str"

        def errors(self) -> list[dict]:
            return []

    path = tmp_path / "flow.scenario.yaml"
    text = textwrap.dedent(HEAD)
    path.write_text(text, encoding="utf-8")
    source = build_source(path, text)

    message = format_validation_error(_NoRelevantErrors(), source, {"steps": []})

    assert _banners(message) == 1
    assert "hunter2" not in message
    assert "input_value" not in message


def test_validation_error_stays_a_value_error():
    """Istniejące `pytest.raises(ValueError, ...)` w testach loadera mają przechodzić."""

    assert issubclass(ScenarioValidationError, ValueError)
