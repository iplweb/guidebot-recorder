"""Testy CLI dla rodziny poleceń zestawów: `compile-set` i `render-set`.

Odrębne polecenia, odrębne wejście (manifest `localized-render-set`, nie
scenariusz) i odrębna ścieżka w `cli.py`: `_load_set_or_exit` → `load_render_set`
→ `RenderSetPlan` → `run_compile_set` / `run_render_set`. Polecenia
jednoscenariuszowe (`validate`, `compile`, `render`, `setup`, `guide`) siedzą
w `test_cli.py`, a `setup` ma jeszcze własny plik `test_cli_setup.py`.

Wspólne z `test_cli.py` są tylko cztery rzeczy — zepsuty scenariusz, asercja
bannera, atrapa Playwrighta i zamrożony sidecar pozycyjny — i przychodzą jawnym
importem z `_cli_helpers`.
"""

import shutil
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

import guidebot_recorder.cli as cli_module
from guidebot_recorder.cli import app
from guidebot_recorder.models.compiled import CompiledScenario
from guidebot_recorder.recorder.render_set import CompileSetResult
from guidebot_recorder.scenario.compiled import compiled_path, write_compiled
from guidebot_recorder.scenario.loader import load_scenario

from ._cli_helpers import (
    BAD_TWO_COMMANDS,
    _assert_validation_banner,
    _freeze_positional_sidecar,
    _install_fake_playwright,
)

runner = CliRunner()


def _write_render_set(tmp_path, *, provider="edge"):
    scenario = tmp_path / "en.scenario.yaml"
    scenario.write_text(
        textwrap.dedent(
            f"""\
            config:
              title: English
              viewport: {{width: 640, height: 480}}
              locale: en-US
              tts:
                provider: {provider}
                voice: en
                lang: en-US
                trackLanguage: eng
            steps:
              - say: "Welcome"
            """
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "localized.render-set.yaml"
    manifest.write_text(
        textwrap.dedent(
            """\
            kind: localized-render-set
            version: 1
            variants:
              en-US:
                scenario: en.scenario.yaml
                output: en.mp4
            """
        ),
        encoding="utf-8",
    )
    return manifest, scenario


#: ten sam scenariusz co `POSITIONAL` w `test_cli.py`, w kształcie wariantu
#: zestawu (manifest wymaga `locale`)
POSITIONAL_VARIANT = textwrap.dedent(
    """\
    config:
      title: t
      viewport: {width: 640, height: 480}
      locale: en-US
      tts: {provider: edge, voice: en, lang: en-US, trackLanguage: eng}
    steps:
      - click: "Usuń"
    """
)


@pytest.mark.parametrize(
    ("command", "extra"),
    [("render-set", ["--output-dir", "out"]), ("compile-set", [])],
)
def test_set_commands_pass_a_validation_banner_through_untouched(
    tmp_path, monkeypatch, command, extra
):
    """`_load_set_or_exit` nie może opakować bannera w `BŁĄD zestawu: …`.

    Banner niesie własny nagłówek i wielolinijkowy fragment YAML — prefiks
    z pierwszą linią zrósłby się w `BŁĄD zestawu: BŁĄD walidacji — …`, a reszta
    zwisałaby bez kontekstu.

    Dziś warstwa `scenario.render_set` sama zamienia każdy błąd wariantu na
    `RenderSetValidationError`, więc ta ścieżka jest asekuracyjna — testujemy ją
    wstrzykując błąd wprost, żeby obsługa w CLI nie została martwym kodem.
    """

    manifest, scenario = _write_render_set(tmp_path)
    scenario.write_text(BAD_TWO_COMMANDS, encoding="utf-8")

    def exploding_load_render_set(path):
        return load_scenario(scenario)  # rzuca ScenarioValidationError

    monkeypatch.setattr(cli_module, "load_render_set", exploding_load_render_set)

    result = runner.invoke(app, [command, str(manifest), *extra])

    _assert_validation_banner(result, scenario, exit_code=1)
    assert "BŁĄD zestawu:" not in result.output


def test_render_set_requires_all_compiled_sidecars_before_browser_launch(tmp_path):
    manifest, _scenario = _write_render_set(tmp_path)

    result = runner.invoke(
        app,
        ["render-set", str(manifest), "--output-dir", str(tmp_path / "out")],
    )

    assert result.exit_code == 2
    assert "uruchom `guidebot compile-set`" in result.output
    assert not (tmp_path / "out").exists()


def test_render_set_rejects_non_edge_provider_before_browser_launch(tmp_path):
    manifest, _scenario = _write_render_set(tmp_path, provider="custom")

    result = runner.invoke(
        app,
        ["render-set", str(manifest), "--output-dir", str(tmp_path / "out")],
    )

    assert result.exit_code == 2
    assert "obsługuje provider TTS `edge`" in result.output


def test_compile_set_skips_browser_when_every_variant_is_current(tmp_path):
    manifest, scenario = _write_render_set(tmp_path)
    write_compiled(
        compiled_path(scenario),
        CompiledScenario(source=scenario.name, actions=[None]),
    )

    result = runner.invoke(app, ["compile-set", str(manifest)])

    assert result.exit_code == 0
    assert "wszystkie warianty aktualne" in result.output


def test_compile_set_success_wires_dependencies_and_closes_browser(tmp_path, monkeypatch):
    manifest, scenario = _write_render_set(tmp_path)
    browser, launches = _install_fake_playwright(monkeypatch)
    reasoner = object()
    calls = []

    monkeypatch.setattr(cli_module, "compile_up_to_date", lambda path: False)
    monkeypatch.setattr(cli_module, "CodexReasoner", lambda: reasoner)

    async def compile_set(plan, received_browser, received_reasoner, **kwargs):
        calls.append((plan, received_browser, received_reasoner, kwargs))
        return CompileSetResult(compiled=("en-US",), reused=())

    monkeypatch.setattr(cli_module, "run_compile_set", compile_set)

    result = runner.invoke(
        app,
        [
            "compile-set",
            str(manifest),
            "--headed",
            "--pause-on-error",
            "--timeout",
            "8.5",
            "--verbose",
        ],
    )

    assert result.exit_code == 0
    assert launches == [False]
    assert len(calls) == 1
    plan, received_browser, received_reasoner, kwargs = calls[0]
    assert [variant.scenario for variant in plan.variants] == [scenario]
    assert received_browser is browser
    assert received_reasoner is reasoner
    assert kwargs == {
        "timeout": 8.5,
        "force": False,
        "pause_on_error": True,
        "verbose": True,
    }
    assert browser.closed is True
    assert "skompilowano warianty: en-US" in result.output


def test_render_set_success_wires_dependencies_and_closes_browser(tmp_path, monkeypatch):
    manifest, scenario = _write_render_set(tmp_path)
    out_dir = tmp_path / "out"
    browser, launches = _install_fake_playwright(monkeypatch)
    provider = object()
    checked = []
    calls = []

    monkeypatch.setattr(cli_module, "ensure_render_set_compiled", checked.append)
    monkeypatch.setattr(cli_module, "EdgeTtsProvider", lambda: provider)

    async def render_set(
        plan,
        received_out_dir,
        received_provider,
        cache_dir,
        received_browser,
        **kwargs,
    ):
        calls.append(
            (
                plan,
                received_out_dir,
                received_provider,
                cache_dir,
                received_browser,
                kwargs,
            )
        )
        return [out_dir / "en.mp4"]

    monkeypatch.setattr(cli_module, "run_render_set", render_set)

    result = runner.invoke(
        app,
        [
            "render-set",
            str(manifest),
            "--output-dir",
            str(out_dir),
            "--headed",
            "--pause-on-error",
            "--timeout",
            "9.5",
            "--verbose",
        ],
    )

    assert result.exit_code == 0
    assert launches == [False]
    assert len(checked) == 1
    assert len(calls) == 1
    plan, received_out_dir, received_provider, cache_dir, received_browser, kwargs = calls[0]
    assert checked == [plan]
    assert [variant.scenario for variant in plan.variants] == [scenario]
    assert received_out_dir == out_dir
    assert received_provider is provider
    assert cache_dir == Path(".guidebot/audio")
    assert received_browser is browser
    reasoner = kwargs.pop("reasoner")
    assert kwargs == {"timeout": 9.5, "pause_on_error": True, "verbose": True}
    # Render sets heal pending optional branches too, so they get a reasoner on
    # the same terms as a plain render: only when the Codex CLI is installed.
    assert (reasoner is not None) == (shutil.which("codex") is not None)
    assert browser.closed is True
    assert f"zrenderowano: {out_dir / 'en.mp4'}" in result.output


def test_compile_set_opens_the_browser_for_a_frozen_positional_index(tmp_path, monkeypatch):
    """Ta sama bramka dla zestawu — `compile-set` kończy pracę przed `run_compile_set`.

    „Ta sama" znaczy: co
    `test_compile_opens_the_browser_for_a_frozen_positional_index`
    w `test_cli.py`.
    """

    manifest, scenario = _write_render_set(tmp_path)
    scenario.write_text(POSITIONAL_VARIANT, encoding="utf-8")
    _freeze_positional_sidecar(scenario)
    browser, launches = _install_fake_playwright(monkeypatch)
    calls = []

    async def compile_set(plan, *args, **kwargs):
        calls.append(plan)
        return CompileSetResult(compiled=("en-US",), reused=())

    monkeypatch.setattr(cli_module, "CodexReasoner", lambda: object())
    monkeypatch.setattr(cli_module, "run_compile_set", compile_set)

    result = runner.invoke(app, ["compile-set", str(manifest)])

    assert result.exit_code == 0
    assert "wszystkie warianty aktualne" not in result.output
    assert launches == [True]
    assert len(calls) == 1
    assert browser.closed is True
