"""Wspólne dane i pomocniki testów CLI, dzielone przez rodziny poleceń.

`test_cli.py` obsługuje polecenia jednoscenariuszowe (`validate`, `compile`,
`render`, `setup`, `guide`), `test_cli_sets.py` — polecenia zestawów
(`compile-set`, `render-set`, wchodzące przez `_load_set_or_exit`). Obie rodziny
potrzebują tego samego zepsutego scenariusza, tej samej asercji bannera
walidacji, tej samej atrapy Playwrighta i tego samego zamrożonego sidecara
z namiarem pozycyjnym — i tylko te cztery rzeczy są wspólne, więc mieszkają
tutaj.

Nie `conftest.py`: repo świadomie go nie ma, żeby czytając plik testowy widzieć
wszystko, co go zasila. Import jest jawny.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import guidebot_recorder.cli as cli_module
from guidebot_recorder.models.action import CachedAction, Fingerprint
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.models.config import config_hash
from guidebot_recorder.models.identity import Identity
from guidebot_recorder.models.target import RoleTarget
from guidebot_recorder.scenario.compiled import compiled_path, write_compiled
from guidebot_recorder.scenario.loader import load_scenario

BAD_TWO_COMMANDS = textwrap.dedent(
    """\
    config:
      title: t
      viewport: {width: 1, height: 1}
      tts: {provider: e, voice: v, lang: pl}
    steps:
      - click: "X"
        navigate: "http://x"
    """
)


#: linia `- click: "X"` w :data:`BAD_TWO_COMMANDS`
BAD_LINE = 6


def _assert_validation_banner(result, path, *, exit_code):
    """Banner walidacji dotarł na wyjście w jednym kawałku i bez tracebacku."""

    assert result.exit_code == exit_code
    output = result.output
    assert "Traceback" not in output
    # nagłówek bannera z myślnikiem — CLI nie dokleja własnego prefiksu
    assert "BŁĄD walidacji —" in output
    assert "BŁĄD walidacji: BŁĄD" not in output
    # `plik:linia` w jednym kawałku: Rich łamał tę ścieżkę w połowie
    assert f"{path}:{BAD_LINE}" in output
    assert "^ tutaj" in output
    assert "dozwolona dokładnie jedna" in output


def _install_fake_playwright(monkeypatch):
    class FakeBrowser:
        closed = False

        async def close(self):
            self.closed = True

    browser = FakeBrowser()
    launches = []

    class FakeChromium:
        async def launch(self, *, headless):
            launches.append(headless)
            return browser

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightManager:
        async def __aenter__(self):
            return FakePlaywright()

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    monkeypatch.setattr(cli_module, "async_playwright", FakePlaywrightManager)
    return browser, launches


def _freeze_positional_sidecar(scenario: Path) -> None:
    """Zamroź sidecar, który *odpowiada źródłu*, ale niesie zmierzony `nth`.

    Odcisk kroku (`compiler_version`, `command_kind`, `compiled_from`,
    `config_hash`) jest kompletny, więc `compile_up_to_date` mówi „aktualne" —
    i tylko osobne pytanie o namiar pozycyjny może kazać otworzyć przeglądarkę.
    """

    config = load_scenario(scenario).config
    write_compiled(
        compiled_path(scenario),
        CompiledScenario(
            source=scenario.name,
            actions=[
                CachedAction(
                    action="click",
                    target=RoleTarget(role="button", name="Usuń", exact=True, nth=1),
                    identity=Identity(
                        tag="button", ancestry_digest="a", dom_path_digest="p"
                    ),
                    expect="none",
                    fingerprint=Fingerprint(
                        command_kind="click",
                        compiled_from="Usuń",
                        expect="none",
                        config_hash=config_hash(config),
                    ),
                )
            ],
        ),
    )
