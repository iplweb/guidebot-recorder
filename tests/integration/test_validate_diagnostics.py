"""E2E: `guidebot validate` na zepsutym scenariuszu mówi, gdzie w pliku jest błąd.

Prawdziwy proces, prawdziwy skrypt konsolowy — nie `CliRunner` — bo sprawdzamy
także kod wyjścia i kanał (stderr), a nie samą treść bannera.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

GUIDEBOT = Path(sys.executable).with_name("guidebot")

BROKEN = textwrap.dedent(
    """\
    config:
      title: Logowanie
      viewport: {width: 1280, height: 720}
      tts: {provider: edge, voice: v, lang: pl-PL}
    steps:
      - say: "Zaczynamy."
      - click: "Zapisz"
        navigate: "https://example.test"
    """
)


@pytest.mark.skipif(not GUIDEBOT.exists(), reason="skrypt `guidebot` niezainstalowany w env")
def test_validate_points_at_the_offending_line(tmp_path):
    path = tmp_path / "flow.scenario.yaml"
    path.write_text(BROKEN, encoding="utf-8")

    out = subprocess.run(
        [str(GUIDEBOT), "validate", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert out.returncode == 1
    assert out.stdout.strip() == ""
    stderr = out.stderr
    # `plik:linia` w formie podanej w wywołaniu — do skopiowania do edytora
    assert f"{path}:7" in stderr
    assert "krok 2/2" in stderr
    # dosłowny fragment YAML z numerem linii i karetką pod winną linią
    assert '      7 |   - click: "Zapisz"' in stderr
    assert "^ tutaj" in stderr
    assert "dozwolona dokładnie jedna" in stderr
    # jeden banner, nie ściana błędów z odrzuconego wariantu unii
    # (nagłówek bannera ma myślnik; `BŁĄD walidacji:` z dwukropkiem dokłada CLI)
    assert stderr.count("BŁĄD walidacji —") == 1
    assert "Extra inputs" not in stderr


@pytest.mark.skipif(not GUIDEBOT.exists(), reason="skrypt `guidebot` niezainstalowany w env")
def test_validate_stays_quiet_on_a_good_scenario(tmp_path):
    path = tmp_path / "ok.scenario.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config:
              title: Logowanie
              viewport: {width: 1280, height: 720}
              tts: {provider: edge, voice: v, lang: pl-PL}
            steps:
              - say: "Zaczynamy."
            """
        ),
        encoding="utf-8",
    )

    out = subprocess.run(
        [str(GUIDEBOT), "validate", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert out.returncode == 0
    assert out.stdout.strip() == "OK"
